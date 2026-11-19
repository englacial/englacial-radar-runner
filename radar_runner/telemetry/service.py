"""The telemetry service: reads the sensors, logs them and publishes the latest values.

One process and one loop (radar_runner/common/service.py). It is the only
owner of the OLA's serial port and the only reader of the Pi's sensors; the
GPS belongs to gpsd and the UPS monitor to the power service, and this service
is a reader of both. Everything else reads /run/radar-runner/user/telemetry.json.

Each round takes milliseconds: no source is ever waited for. A source that
fails is reported in its own `health` and does not stop the others. Two things
that can be held up by a drive run in threads of their own, so that they cannot
delay the status file, which the safety watcher needs fresh: writing the log
files, and reading the Pi's sensors (a drive can take a second to report its
temperature).

What is logged and published, the config keys, and how to stop the service
for bench work: README.md in this directory.
"""
import os
import shlex
import subprocess
import time

from ..common import status as status_files
from ..common.bootcount import boot_count, raise_boot_count
from ..common.service import Service
from . import csvlog
from .gps import COLUMNS as GPS_COLUMNS
from .gps import Gps
from .ola import AMP_VALUES, Ola
from .pi import COLUMNS as PI_COLUMNS
from .pi import BackgroundReader, PiSensors, throttle_flags

OK, STALE, MISSING, ERROR = "ok", "stale", "missing", "error"
UPS_COLUMNS = ("status", "capacitor_v", "current_a", "power_present", "state", "mode",
               "radar_stop_requested", "low_voltage")
CLOCK_STEP_S = 0.5      # a change of wall clock against uptime larger than this is a step
RECENT_S = 60.0         # "recent" in the loop's own health: the last one to two minutes


def _number(text):
    try:
        return float(text)
    except ValueError:
        return text


class Telemetry(Service):
    """Telemetry service: logs the OLA, Pi, UPS and GPS readings and publishes the latest values."""
    name = "telemetry"

    def __init__(self, config, *, wall=time.time, uptime=status_files.boottime_s, ola=None, gps=None,
                 pi=None, store=None, **kw):
        super().__init__(config, **kw)
        self.cfg = config["telemetry"]
        self.interval_s = self.cfg["loop_interval_s"]
        self._wall, self._uptime = wall, uptime
        self.ola, self.gps, self.pi, self.store = ola, gps, pi, store
        self._pi_reader = None      # the thread that reads the Pi's sensors; None: read in the loop (tests)
        self.boot = 0
        self.boot_problem = ""
        self._latest = {}           # source -> {"uptime": ..., "wall": ..., "values": {...}}
        self._amp = None            # the latest amplifier reading
        self._internal = {}         # source -> text of an unexpected error
        self._ups_read = None
        self._next = {"pi": 0.0, "ups": 0.0, "status": 0.0}
        self._ola_logged_at = None
        self._ola_was_configured = False    # for the GPS receiver correction after each OLA start
        self._fix_due = None
        self._fix_proc = None
        self._clock = {"offset": None, "steps": 0, "last_step_s": None, "last_step_uptime_s": None}
        # How evenly the loop runs. A consumer sees a stall in last_gap_s and recent_longest_gap_s.
        self._loop = {"rounds": 0, "last_gap_s": 0.0, "recent_longest_gap_s": 0.0, "longest_gap_s": 0.0,
                      "longest_gap_uptime_s": None, "slowest_step_s": {}}
        self._last_round = None
        self._gap_buckets = [0.0, 0.0]      # longest gap in this and in the previous RECENT_S
        self._bucket_started = None

    # ---- start and stop

    def setup(self):
        cfg = self.cfg
        mount = "" if cfg["data_mount"] == "-" else cfg["data_mount"]
        primary = os.path.join(cfg["data_root"], "telemetry")
        if self.store is None:
            # At least one more than any boot number already in the log directories: the counter
            # may have been lost with a replaced SD card while the data drive stayed.
            floor = csvlog.highest_boot([primary, cfg["fallback_dir"]])
            self.boot, self.boot_problem = boot_count(self.config["paths"]["state_dir"], floor=floor)
            if self.boot_problem:
                self.log.warning("boot counter: %s", self.boot_problem)
            self.store = csvlog.ThreadedLogStore(csvlog.LogStore(
                primary_root=primary, fallback_root=cfg["fallback_dir"], boot=self.boot,
                boot_id=status_files.current_boot_id(), mount_point=mount,
                fallback_max_bytes=cfg["fallback_max_mb"] << 20, flush_interval_s=cfg["flush_interval_s"],
                fsync_interval_s=cfg["fsync_interval_s"]))
        if self.ola is None:
            self.ola = Ola(cfg["ola"])
        if self.gps is None:
            self.gps = Gps(cfg["gps"])
        if self.pi is None:
            # In a thread of its own: a drive can take a second to report its temperature.
            self.pi = PiSensors(data_mount=mount)
            self._pi_reader = BackgroundReader(self.pi.read, cfg["pi"]["interval_s"], wall=self._wall,
                                               uptime=self._uptime)
        self.log.info("boot %d; OLA on %s; gpsd at %s:%s", self.boot, cfg["ola"]["port"],
                      cfg["gps"]["host"], cfg["gps"]["port"])

    def cleanup(self):
        for part in (self._pi_reader, self.ola, self.gps, self.store):
            if part is None:
                continue
            try:
                part.close()
            except Exception:
                self.log.exception("error while closing")

    # ---- one round

    def _guarded(self, source, fn, *args):
        """Run one source's step. An unexpected error marks that source and nothing else."""
        started = self._monotonic()
        try:
            result = fn(*args)
            self._internal.pop(source, None)
            return result
        except Exception as e:
            self._failed(source, e)
            return None
        finally:
            self._timed(source, started)

    def _failed(self, source, e):
        text = f"internal error: {type(e).__name__}: {e}"
        if self._internal.get(source) != text:      # logged once, not every round
            self.log.error("%s: unexpected error", source, exc_info=e)
        self._internal[source] = text

    def _timed(self, step, started):
        took = self._monotonic() - started
        if took > self._loop["slowest_step_s"].get(step, -1.0):
            self._loop["slowest_step_s"][step] = round(took, 4)

    def publish(self, data):
        started = self._monotonic()
        super().publish(data)
        self._timed("status_file", started)

    def _record(self, source, columns, values, wall, up, log_it=True):
        self._latest[source] = {"uptime": up, "wall": wall, "columns": columns, "values": values}
        if log_it:
            date, line = csvlog.format_row(values, wall, up)
            self.store.write(source, columns, date, line)

    def _step_ola(self, mono, wall, up):
        ola = self.ola
        rows = ola.poll(mono)       # every row with its estimated age; nothing if the port was not read empty
        interval = self.cfg["ola"]["log_interval_s"]
        for row in rows:
            amp, age = row["amp"], row["age_s"]
            values = row["fields"] + [None if amp is None else round(amp[name], 3) for name in AMP_VALUES]
            log_it = not interval or self._ola_logged_at is None or up - age - self._ola_logged_at >= interval
            if log_it:
                self._ola_logged_at = up - age
            self._record("ola", row["columns"] + AMP_VALUES, values, wall - age, up - age, log_it)
        self._receiver_fix(mono, ola.configured)
        if rows and rows[-1]["amp"] is not None:
            # Only the newest row of a poll that read the port empty. All that is certain about its
            # age is that it arrived since the poll before: that poll's time is its sample time.
            self._amp = {"uptime": up - ola.window_s, "wall": wall - ola.window_s, "values": rows[-1]["amp"],
                         "row": rows[-1]}

    def _receiver_fix(self, mono, configured):
        """Correct one GPS receiver setting a little after every OLA start.

        Each time the OLA starts it turns NMEA output on the receiver's unused UART1 back on (in the
        receiver's RAM; seen 2026-11-19). With the OLA's 10 Hz rate that overflows the receiver's
        transmit buffer and gpsd loses messages. setup/gps_receiver.py turns it off again, in RAM
        only, through gpsd. It runs as a separate short process and is never waited for.
        """
        command = shlex.split(self.cfg["gps"].get("receiver_fix_command") or "")
        if self._fix_proc is not None and self._fix_proc.poll() is not None:
            self.log.info("GPS receiver setting check finished with status %s", self._fix_proc.returncode)
            self._fix_proc = None
        if configured and not self._ola_was_configured:
            self._fix_due = mono + self.cfg["gps"].get("receiver_fix_delay_s", 15.0)
        self._ola_was_configured = configured
        if command and self._fix_due is not None and mono >= self._fix_due and self._fix_proc is None:
            self._fix_due = None
            try:
                self._fix_proc = subprocess.Popen(list(command), stdin=subprocess.DEVNULL,
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.log.info("GPS receiver setting check started (the OLA has just started)")
            except OSError as e:
                self.log.warning("GPS receiver setting check could not start: %s", e)

    def _step_gps(self, mono, wall, up):
        sample = self.gps.poll(mono)
        if sample is not None:
            self._record("gps", GPS_COLUMNS, [sample[c] for c in GPS_COLUMNS], wall, up)

    def _step_pi(self, wall, up):
        values = self.pi.read()
        self._record("pi", PI_COLUMNS, [values[c] for c in PI_COLUMNS], wall, up)

    def _take_pi(self):
        """Pick up what the reader thread has finished; each sample carries the time its read began."""
        for wall, up, values in self._pi_reader.take():
            if isinstance(values, Exception):
                self._failed("pi", values)
            else:
                self._internal.pop("pi", None)
                self._record("pi", PI_COLUMNS, [values[c] for c in PI_COLUMNS], wall, up)

    def _step_ups(self, wall, up):
        read = status_files.read_status(self.config["paths"]["power_status_file"],
                                        self.cfg["ups"]["stale_after_s"], wall=self._wall, uptime=self._uptime)
        self._ups_read = read
        data = read.data if read.ok else {}     # an old file is not a reading
        values = [read.state] + [data.get(c) for c in UPS_COLUMNS[1:]]
        self._record("ups", UPS_COLUMNS, values, wall, up)

    def _watch_clock(self, wall, up):
        offset = wall - up
        clock = self._clock
        if clock["offset"] is not None and abs(offset - clock["offset"]) > CLOCK_STEP_S:
            clock["steps"] += 1
            clock["last_step_s"] = round(offset - clock["offset"], 3)
            clock["last_step_uptime_s"] = round(up, 3)
            self.log.info("the system clock was stepped by %+.3f s (logs carry uptime, so their order is kept)",
                          clock["last_step_s"])
        clock["offset"] = offset

    def _watch_loop(self, mono, up):
        """Keep the time between rounds: the last one, the longest of the last minute or two, the longest ever."""
        loop = self._loop
        loop["rounds"] += 1
        if self._bucket_started is None or mono - self._bucket_started >= RECENT_S:
            self._bucket_started, self._gap_buckets = mono, [0.0, self._gap_buckets[0]]
        if self._last_round is not None:
            gap = mono - self._last_round
            loop["last_gap_s"] = round(gap, 3)
            self._gap_buckets[0] = max(self._gap_buckets[0], gap)
            if gap > loop["longest_gap_s"]:
                loop["longest_gap_s"], loop["longest_gap_uptime_s"] = round(gap, 3), round(up, 3)
        loop["recent_longest_gap_s"] = round(max(self._gap_buckets), 3)
        self._last_round = mono

    def _due(self, source, mono):
        """True once per `interval_s` of a source that is read in the loop."""
        if mono < self._next[source]:
            return False
        interval = self.cfg[source]["interval_s"] if source in self.cfg else self.cfg[source + "_interval_s"]
        on_time = self._next[source] + interval
        self._next[source] = on_time if on_time > mono else mono + interval    # first call, or far behind
        return True

    def tick(self):
        mono, wall, up = self._monotonic(), self._wall(), self._uptime()
        self._watch_loop(mono, up)
        self._watch_clock(wall, up)
        self._guarded("ola", self._step_ola, mono, wall, up)
        self._guarded("gps", self._step_gps, mono, wall, up)
        if self._pi_reader is not None:
            self._guarded("pi_pickup", self._take_pi)
        elif self._due("pi", mono):
            self._guarded("pi", self._step_pi, wall, up)
        if self._due("ups", mono):
            self._guarded("ups", self._step_ups, wall, up)
        return self.snapshot(up) if self._due("status", mono) else None

    # ---- what is published

    def _source(self, source, health, reason, up, **extra):
        latest = self._latest.get(source)
        if source in self._internal:
            health, reason = ERROR, self._internal[source]
        entry = {"health": health, "reason": reason, "age_s": None, "sample_uptime_s": None, "values": {}}
        if latest is not None:
            entry["age_s"] = round(max(up - latest["uptime"], 0.0), 3)
            entry["sample_uptime_s"] = round(latest["uptime"], 3)
            entry["values"] = dict(zip(latest["columns"], latest["values"]))
        entry.update(extra)
        return entry

    def _age(self, source, up):
        latest = self._latest.get(source)
        return None if latest is None else up - latest["uptime"]

    def _ola_status(self, up, mono):
        ola, fresh_s = self.ola, self.cfg["ola"]["fresh_s"]
        age = self._age("ola", up)
        if ola.state == "closed":
            # No device file: missing. A device that is there but was given up on for now: error.
            health, reason = (ERROR if ola.port_present and ola.opens else MISSING), ola.error
        elif not ola.configured:
            health, reason = ERROR, ola.error or "not configured"
        elif age is None or age > fresh_s:
            health, reason = STALE, "no row yet" if age is None else f"no row for {age:.1f} s"
        else:
            health, reason = OK, ""
        entry = self._source("ola", health, reason, up, connection=ola.state, configured=ola.configured,
                             rows=ola.rows, bad_lines=ola.bad_lines, rows_dropped=ola.dropped_rows,
                             port_opens=ola.opens, adc_configured_count=ola.configures,
                             restarts_seen=ola.restarts, retry=ola.retry_state(mono))
        entry["values"] = {k: _number(v) if isinstance(v, str) else v for k, v in entry["values"].items()}
        return entry

    def _amp_status(self, up):
        """The amplifier readings, with an explicit statement of whether they can be acted on.

        `fresh` is true only if all of this holds: the OLA is set up for the differential readings,
        the newest reading arrived at most fresh_limit_s ago (counted from the earliest moment it
        can have arrived), and the loop itself has not just stalled for longer than that.
        """
        fresh_s = self.cfg["ola"]["fresh_s"]
        amp = {"fresh": False, "reason": "", "fresh_limit_s": fresh_s, "age_s": None, "sample_uptime_s": None,
               "sample_time_utc": None, "temp_c": None, "current_a": None, "voltage_v": None, "case_temp_c": None}
        if self._amp is None:
            amp["reason"] = self.ola.error or "no amplifier reading yet"
            return amp
        age = max(up - self._amp["uptime"], 0.0)
        values, row = self._amp["values"], self._amp["row"]
        amp.update(age_s=round(age, 3), sample_uptime_s=round(self._amp["uptime"], 3),
                   sample_time_utc=csvlog.utc_text(self._amp["wall"]),
                   temp_c=round(values["amp_temp_c"], 2), current_a=round(values["amp_current_a"], 3),
                   voltage_v=round(values["amp_voltage_v"], 3))
        # The TMP117 on the amplifier case: the last `degC` column, as pa_run.py takes it.
        case = [f for c, f in zip(row["columns"], row["fields"]) if c == "degC" or c.startswith("degC_")]
        if case and isinstance(_number(case[-1]), float):
            amp["case_temp_c"] = round(_number(case[-1]), 2)
        gap = self._loop["last_gap_s"]
        if "ola" in self._internal:
            amp["reason"] = self._internal["ola"]
        elif not self.ola.configured:
            amp["reason"] = f"the OLA is not set up for the amplifier readings ({self.ola.error or self.ola.state})"
        elif age > fresh_s:
            amp["reason"] = f"last reading {age:.1f} s old" + (f" ({self.ola.error})" if self.ola.error else "")
        elif gap > fresh_s:
            amp["reason"] = f"the service loop stalled for {gap:.1f} s"
        else:
            amp["fresh"] = True
        return amp

    def _gps_status(self, up, mono):
        gps, cfg = self.gps, self.cfg["gps"]
        latest = self._latest.get("gps")
        new_fix_age = None if gps.last_new_fix_at is None else mono - gps.last_new_fix_at
        no_data = gps.no_data_s(mono)
        if gps.error:
            health, reason = MISSING, gps.error
        elif latest is None:
            health, reason = MISSING, "no answer from gpsd yet"
        elif not gps.receivers:
            health, reason = MISSING, "gpsd has no receiver"
        elif no_data > cfg["no_data_error_s"]:
            # gpsd has the receiver but nothing usable comes out of it: what a choked output looks like.
            health = ERROR
            reason = (f"receiver present but no fix data for {no_data:.0f} s "
                      "(its settings and buffers: setup/check.py --receiver)")
        elif new_fix_age is None or new_fix_age > cfg["stale_after_s"]:
            health = STALE
            reason = "no fix time from the receiver" + ("" if new_fix_age is None else f" for {new_fix_age:.0f} s")
        else:
            mode = latest["values"][GPS_COLUMNS.index("mode")]
            health, reason = OK, "" if isinstance(mode, int) and mode >= 2 else "no position fix"
        return self._source("gps", health, reason, up, gpsd_connects=gps.connects,
                            new_fix_age_s=None if new_fix_age is None else round(new_fix_age, 2))

    def _pi_status(self, up):
        latest, age = self._latest.get("pi"), self._age("pi", up)
        extra = {}
        if latest is None:
            health, reason = MISSING, "not read yet"
        elif latest["values"][PI_COLUMNS.index("soc_temp_c")] is None:
            health, reason = ERROR, "cannot read the SoC temperature"
        elif age > 3 * self.cfg["pi"]["interval_s"] + 1:
            health, reason = STALE, f"last read {age:.1f} s ago"
        else:
            health, reason = OK, ""
        if latest is not None:
            try:
                now, since_boot = throttle_flags(int(latest["values"][PI_COLUMNS.index("throttled")], 16))
                extra = {"throttled_now": now, "throttled_since_boot": since_boot}
            except (TypeError, ValueError):
                pass
        return self._source("pi", health, reason, up, **extra)

    def _ups_status(self, up):
        read = self._ups_read
        if read is None:
            health, reason = MISSING, "not read yet"
        elif read.ok:
            health, reason = OK, ""
        elif read.state == status_files.MISSING:
            health, reason = MISSING, "the power service has not written its status file"
        elif read.state == status_files.STALE:
            health, reason = STALE, read.error
        else:
            health, reason = ERROR, read.error
        return self._source("ups", health, reason, up)

    def snapshot(self, up=None):
        up = self._uptime() if up is None else up
        mono = self._monotonic()
        sources = {"ola": self._ola_status(up, mono), "pi": self._pi_status(up), "ups": self._ups_status(up),
                   "gps": self._gps_status(up, mono)}
        logs = self.store.status()
        if logs.get("boot", self.boot) > self.boot:
            # The logger found this boot's number taken by another boot's directory and moved on:
            # the shared counter follows, so that every name of this boot carries the same number.
            self.boot, problem = raise_boot_count(self.config["paths"]["state_dir"], logs["boot"])
            self.boot_problem = problem or self.boot_problem
            self.log.warning("boot number raised to %d: the one before was in use on the log drive", self.boot)
        problems = [f"{name}: {s['health']}" for name, s in sources.items() if s["health"] != OK]
        if logs.get("writer", "running") != "running":
            problems.append(f"logging: writer {logs['writer']}")        # stalled or dead: nothing is being written
        if logs["location"] != csvlog.PRIMARY:
            problems.append(f"logging: {logs['location']}")
        clock = {k: v for k, v in self._clock.items() if k != "offset"}
        clock["synced"] = sources["pi"]["values"].get("clock_synced")
        return {
            "state": "degraded" if problems else "ok",
            "problems": problems,
            "boot_count": self.boot,
            "boot_count_problem": self.boot_problem,
            "amp": self._amp_status(up),
            "sources": sources,
            "clock": clock,
            "logging": logs,
            "loop": dict(self._loop, slowest_step_s=dict(self._loop["slowest_step_s"])),
        }
