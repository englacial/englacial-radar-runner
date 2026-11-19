"""The safety watcher service: reads the inputs, carries out what the logic decides, publishes.

One process and one loop (radar_runner/common/service.py), two rounds a second.
Every round reads the telemetry status file, looks whether the override file
exists and what the shared record of the amplifier pin says, lists the radar
processes from /proc, and passes all of that to the logic (logic.py). Nothing in a round
waits: the reset tool runs as a separate process that is looked at once per
round, and the event log is written by a thread of its own.

The watcher opens no device. It never reads the radar manager's status and
does not need the manager to be running. Rules and numbers: README.md.
"""
import copy
import fcntl
import glob
import json
import math
import os
import queue
import subprocess
import threading
import time

from ..common import config as config_module
from ..common import status as status_files
from ..common.service import Service
from . import logic as L

STOPPED = "the safety watcher is not running"
STATE_NAME = "watcher-state.json"   # in the run directory: what a restarted watcher must still know (this boot only)
RADAR_FROM_MANAGER = "manager"      # watcher.radar_exe: take the path from the radar manager's settings
RADAR_FALLBACK = "/home/englacial/orca/sdr/build/radar"
SDR_VENDOR, SDR_BOOTLOADER_PRODUCT = "2500", "WestBridge"
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")

# Settings that cannot work revert to their defaults, with a warning: (settings, test, what must hold).
_RULES = (
    (("loop_interval_s",), lambda c: 0.05 <= c["loop_interval_s"] <= 1.0, "between 0.05 and 1 s"),
    (("telemetry_lost_s",), lambda c: 1.0 <= c["telemetry_lost_s"] <= 30.0, "between 1 and 30 s"),
    (("trip_samples", "trip_window"), lambda c: 2 <= c["trip_samples"] <= c["trip_window"] <= 50,
     "at least 2 readings, and a window no smaller"),
    (("radar_setup_s",), lambda c: 0 <= c["radar_setup_s"] <= 300, "between 0 and 300 s"),
    (("trip_span_s", "clear_hold_s"), lambda c: 0 <= c["trip_span_s"] <= 60 and 0 <= c["clear_hold_s"] <= 3600,
     "not negative, at most 60 s and 3600 s"),
    (("temp_warn_c", "temp_trip_c", "temp_clear_c"),
     lambda c: c["temp_warn_c"] <= c["temp_trip_c"] and c["temp_clear_c"] <= c["temp_trip_c"],
     "warn and clear levels at or below the trip level"),
    (("unpowered_below_v", "voltage_min_v", "voltage_max_v", "voltage_recover_margin_v"),
     lambda c: (0 < c["unpowered_below_v"] < c["voltage_min_v"] < c["voltage_max_v"]
                and 0 <= 2 * c["voltage_recover_margin_v"] < c["voltage_max_v"] - c["voltage_min_v"]),
     "0 < unpowered_below_v < voltage_min_v < voltage_max_v, with room for the recover margin"),
    (("enabled_current_a", "idle_enabled_s"), lambda c: c["enabled_current_a"] > 0 and c["idle_enabled_s"] >= 1.0,
     "a positive current and at least 1 s"),
    (("stop_grace_s", "radar_exit_s", "kill_retry_s"),
     lambda c: c["stop_grace_s"] >= 0 and c["radar_exit_s"] >= 0 and c["kill_retry_s"] >= 1.0,
     "not negative; kill_retry_s at least 1 s"),
)
_RESET_RULES = (
    (("timeout_s", "kill_margin_s"), lambda r: 3.0 <= r["timeout_s"] <= 300.0 and r["kill_margin_s"] >= 1.0,
     "timeout_s between 3 and 300 s (amp_safe's own limits), kill_margin_s at least 1 s"),
    (("retry_prompt_s", "retry_s", "stuck_retry_s", "stuck_after_attempts"),
     lambda r: r["retry_prompt_s"] >= 0 and r["retry_s"] >= 1.0 and r["stuck_retry_s"] >= 10.0
     and r["stuck_after_attempts"] >= 1,
     "retry_s at least 1 s, stuck_retry_s at least 10 s, at least 1 attempt"),
    (("blocked_after_s", "blocked_retry_s", "blind_delay_s", "blind_retry_s"),
     lambda r: r["blocked_after_s"] >= 0 and r["blocked_retry_s"] >= 1.0 and r["blind_delay_s"] >= 5.0
     and r["blind_retry_s"] >= 30.0,
     "blocked_retry_s at least 1 s, blind_delay_s at least 5 s, blind_retry_s at least 30 s"),
)


def checked(cfg, defaults=None):
    """The `watcher` section with impossible values replaced by the defaults: (settings, warnings)."""
    defaults = config_module.DEFAULTS["watcher"] if defaults is None else defaults
    cfg, warnings = copy.deepcopy(cfg), []
    for section, base, prefix, rules in ((cfg, defaults, "watcher.", _RULES),
                                         (cfg["reset"], defaults["reset"], "watcher.reset.", _RESET_RULES)):
        for names, good, text in rules:
            if not good(section):
                for name in names:
                    section[name] = base[name]
                warnings.append(f"{', '.join(prefix + n for n in names)}: must be {text}: defaults used")
    return cfg, warnings


def radar_exe(config):
    """The radar binary: `watcher.radar_exe`, or with "manager" the path that the radar manager runs.

    Taken from the same settings as the manager uses (manager.orca: dir, radar_cwd, radar_command),
    so that the two cannot come to mean different programs.
    """
    explicit = config["watcher"]["radar_exe"]
    if explicit != RADAR_FROM_MANAGER:
        return explicit
    orca = (config.get("manager") or {}).get("orca")
    if orca is None:
        try:        # until the manager's settings are part of the shared defaults
            from ..manager.config import MANAGER_DEFAULTS
            orca = MANAGER_DEFAULTS["orca"]
        except Exception:
            return RADAR_FALLBACK
    try:
        return os.path.normpath(os.path.join(orca["dir"], orca["radar_cwd"], orca["radar_command"][0]))
    except (KeyError, IndexError, TypeError):
        return RADAR_FALLBACK


def record_says_safe(path):
    """True only if the shared record's first line is exactly "safe" (tools/sdr_gpio_safe/README.md)."""
    try:
        with open(path, "rb") as f:
            return f.read(64).split(b"\n", 1)[0] == b"safe"
    except OSError:
        return False


def sdr_state(devices="/sys/bus/usb/devices"):
    """The SDR as USB shows it, without touching it: ready, bootloader (no firmware since power-up) or absent."""
    found = L.SDR_ABSENT
    for vendor in glob.glob(os.path.join(devices, "*", "idVendor")):
        try:
            with open(vendor) as f:
                if f.read().strip() != SDR_VENDOR:
                    continue
        except OSError:
            continue
        try:
            with open(os.path.join(os.path.dirname(vendor), "product")) as f:
                product = f.read()
        except OSError:
            product = ""
        if SDR_BOOTLOADER_PRODUCT not in product:
            return L.SDR_READY
        found = L.SDR_BOOTLOADER
    return found


def lock_held(path):
    """True if another run of amp_safe holds its lock (on the script itself) right now."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)        # also releases the lock


class RadarProcesses:
    """Finds the radar processes: those whose executable is the radar binary.

    This is what decides "a radar is running", never what the radar manager reports. A binary
    that was rebuilt while its old process still runs shows as "<path> (deleted)" and counts.
    Only processes of the same user (or all, as root) can be seen this way.
    """

    def __init__(self, exe, proc="/proc"):
        self.exe, self.proc = exe, proc

    def _targets(self):
        exe = os.path.realpath(self.exe)
        return exe, exe + " (deleted)"

    def pids(self):
        targets, found = self._targets(), []
        for name in os.listdir(self.proc):
            if name.isdigit():
                try:
                    if os.readlink(f"{self.proc}/{name}/exe") in targets:
                        found.append(int(name))
                except OSError:
                    pass        # gone, a kernel thread, or another user's process
        return tuple(sorted(found))

    def with_ages(self, now):
        """(pid, seconds since the process started) for each radar process; `now` is seconds since boot."""
        found = []
        for pid in self.pids():
            try:
                with open(f"{self.proc}/{pid}/stat") as f:
                    started = int(f.read().rsplit(")", 1)[1].split()[19]) / CLOCK_TICKS
                found.append((pid, max(now - started, 0.0)))
            except (OSError, ValueError, IndexError):
                pass            # it has just exited
        return tuple(found)

    def signal(self, pid, signum):
        """Send the signal if `pid` is still a radar process. Returns "" or the error text."""
        try:
            if os.readlink(f"{self.proc}/{pid}/exe") in self._targets():
                os.kill(pid, signum)
            return ""
        except (FileNotFoundError, ProcessLookupError):
            return ""           # it has just exited
        except OSError as e:
            return e.strerror or str(e)


class EventLog:
    """Append-only event log, one JSON object per line: trips, clears, actions and their results.

    Each line carries `time`, `time_utc`, `uptime_s` and the event's own fields (`event`,
    `severity`, `text`, ...), like the radar manager's event log. Above `max_bytes` the file is
    renamed to <file>.1 (replacing the one before) and a new one is started. Lines are written and
    synced by a thread of its own, so that a slow SD card cannot hold up the watcher's loop; if
    that thread falls 1000 events behind, further events are dropped and counted. Never raises.
    """

    def __init__(self, path, max_bytes, *, wall=time.time, uptime=status_files.boottime_s, threaded=True):
        self.path, self.max_bytes = path, int(max_bytes)
        self._wall, self._uptime = wall, uptime
        self.error = ""
        self.written = 0
        self.dropped = 0
        self._queue = queue.Queue(maxsize=1000) if threaded else None
        self._thread = None
        if threaded:
            self._thread = threading.Thread(target=self._run, daemon=True, name="watcher-events")
            self._thread.start()

    def write(self, event):
        now = self._wall()
        try:
            line = json.dumps({"time": round(now, 3), "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                               "uptime_s": round(self._uptime(), 3), **event}, default=str) + "\n"
        except (TypeError, ValueError) as e:
            self.error = f"{type(e).__name__}: {e}"
            return
        if self._queue is None:
            self._append(line)
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while True:
            line = self._queue.get()
            if line is None:
                return
            self._append(line)

    def _append(self, line):
        try:
            try:
                if os.path.getsize(self.path) + len(line) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode())     # one write per line: a line is never split
                os.fsync(fd)
            finally:
                os.close(fd)
            self.written += 1
            self.error = ""
        except OSError as e:
            self.error = f"{type(e).__name__}: {e.strerror or e}"

    def close(self, timeout=2.0):
        """Write out what is waiting, for at most `timeout` seconds."""
        if self._thread is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                return
            self._thread.join(timeout)


class Watcher(Service):
    """Safety watcher: stops the radar and resets the SDR GPIO on unsafe amplifier readings."""
    name = "watcher"

    def __init__(self, config, *, wall=time.time, uptime=status_files.boottime_s, radar=None, events=None, **kw):
        super().__init__(config, **kw)
        self.cfg, problems = checked(config["watcher"])
        self.config_warnings += problems
        self.interval_s = self.cfg["loop_interval_s"]
        self._wall, self._uptime = wall, uptime
        paths = config["paths"]
        # Each of these is a name in the run or state directory, or a full path (join keeps a full path).
        self.telemetry_file = os.path.join(paths["run_dir"], self.cfg["telemetry_status_file"])
        self.override_file = os.path.join(paths["state_dir"], self.cfg["override_file"])
        self.state_file = os.path.join(paths["state_dir"], self.cfg["reset"]["state_file"])
        self.events_file = os.path.join(paths["state_dir"], self.cfg["events_file"])
        self.tool_output_file = os.path.join(paths["run_dir"], "watcher-reset-tool.out")
        self.radar_exe = radar_exe(dict(config, watcher=self.cfg))
        self.radar = radar if radar is not None else RadarProcesses(self.radar_exe)
        self.sdr_state = sdr_state          # replaced in tests
        self.memory = status_files.StatusWriter(os.path.join(paths["run_dir"], STATE_NAME), wall=wall, uptime=uptime)
        self._remembered = None
        self.events = events
        self.logic = None
        self._tool = None           # the running reset tool: {"proc", "started", "deadline"}
        self._leftover = []         # tools that were given up on, until they are gone
        self._record_unwritable = False     # amp_safe could not write its state file (a read-only card?)
        self._inputs_seen = L.Inputs()

    # ---- start and stop

    def setup(self):
        if self.events is None:
            self.events = EventLog(self.events_file, self.cfg["events_max_mb"] * 1e6, wall=self._wall,
                                   uptime=self._uptime)
        # What the watcher knew before a restart in this boot: trips, a stuck amplifier, a pin that is
        # not known safe. A file from another boot says nothing about now and is not used.
        before = status_files.read_status(self.memory.path, math.inf, wall=self._wall, uptime=self._uptime)
        restored = before.data.get("state") if before.ok and isinstance(before.data.get("state"), dict) else None
        self._record_unwritable = bool(restored and restored.get("record_unwritable"))
        self.logic = L.WatcherLogic(self.cfg, self._uptime(), restored)
        c = self.cfg
        self._emit({"event": "start", "severity": L.INFO, "text": (
            f"safety watcher started: stop at {c['temp_trip_c']:g} C, supply {c['voltage_min_v']:g}-"
            f"{c['voltage_max_v']:g} V, reset above {c['enabled_current_a']:g} A for {c['idle_enabled_s']:g} s "
            f"with no radar process ({self.radar_exe})"
            + ("; what the watcher knew before its restart is restored" if restored else ""))})

    def cleanup(self):
        if self.logic is None:
            return
        if self._tool is not None:
            # Killing it during the SDR's initialisation would leave the amplifier enabled.
            self._emit({"event": "reset_tool_left", "severity": L.WARNING,
                        "text": f"the watcher is stopping while the reset tool runs (pid {self._tool['proc'].pid}): "
                                "the tool is left to finish"})
        self._emit({"event": "stop", "severity": L.INFO, "text": "safety watcher stopping"})
        # Say so at once: a stopped watcher must not leave "ok" behind, not even for the 5 s it stays fresh.
        data = self._status(self._uptime(), self._inputs_seen)
        data.update(ok=False, reason=STOPPED, running=False,
                    problems=[{"condition": "watcher", "text": STOPPED, "trip": False}] + data["problems"])
        self.publish(data)
        self.events.close()

    # ---- one round

    def _read_inputs(self, now):
        lost_s = self.cfg["telemetry_lost_s"]
        read = status_files.read_status(self.telemetry_file, lost_s, wall=self._wall, uptime=self._uptime)
        sample, reason = L.amp_sample(read, now, lost_s)
        # The SDR on USB and the tool's lock are looked at only while they can matter.
        sdr = self.sdr_state() if sample is None else L.SDR_UNKNOWN
        busy = self.logic.wants_tool() and lock_held(self.cfg["reset"]["command"])
        # The radar processes last: what the logic decides about them is carried out right after.
        return L.Inputs(sample=sample, telemetry_reason=reason, override=os.path.lexists(self.override_file),
                        pin_safe=record_says_safe(self.state_file), sdr=sdr, tool_busy=busy,
                        radar=self.radar.with_ages(now))

    def tick(self):
        now = self._uptime()
        self._leftover = [p for p in self._leftover if p.poll() is None]
        self._poll_tool(now)
        self._inputs_seen = self._read_inputs(now)
        self._carry_out(self.logic.step(now, self._inputs_seen), now)
        self._remember()
        if not self.logic.ready(now):
            return None         # just started: nothing is published until the first readings are judged
        return self._status(now, self._inputs_seen)

    def _remember(self):
        state = dict(self.logic.persistent(), record_unwritable=self._record_unwritable)
        if state != self._remembered and not self.memory.write({"state": state}):
            self._remembered = state

    def _status(self, now, inp):
        data = self.logic.snapshot(now, inp.override)
        data["running"] = True
        data["override"]["file"] = self.override_file
        data["radar"]["exe"] = self.radar_exe
        data["reset_tool"].update(command=self.cfg["reset"]["command"], state_file=self.state_file,
                                  runnable=os.access(self.cfg["reset"]["command"], os.X_OK))
        data["events"] = {"file": self.events_file, "written": self.events.written, "dropped": self.events.dropped,
                          "error": self.events.error}
        return data

    # ---- carrying out what the logic decided

    def _emit(self, event):
        level = {L.INFO: self.log.info, L.WARNING: self.log.warning}.get(event.get("severity"), self.log.error)
        level("%s: %s", event.get("event"), event.get("text"))
        self.events.write(event)

    def _carry_out(self, actions, now):
        for action in actions:
            if action.kind == "event":
                self._emit(action.data)
            elif action.kind == "signal":
                signum, pids = action.data
                for pid in pids:
                    error = self.radar.signal(pid, signum)
                    if error:
                        self._emit({"event": "signal_failed", "severity": L.URGENT, "pid": pid,
                                    "text": f"cannot send signal {int(signum)} to radar process {pid}: {error}"})
            elif action.kind == "run_tool":
                self._start_tool(now)

    def _start_tool(self, now):
        r = self.cfg["reset"]
        argv = [r["command"], "--timeout", f"{r['timeout_s']:g}"]
        if not self._record_unwritable:
            argv += ["--state-file", self.state_file]
        # Its one line of output goes to a file, not a pipe: a pipe would break under the tool if the
        # watcher ended first. If the file cannot be made the tool runs all the same.
        try:
            out = open(self.tool_output_file, "wb")
        except OSError:
            out = subprocess.DEVNULL
        try:
            # In a session of its own: a Ctrl-C meant for a watcher run by hand must not reach the tool.
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError as e:
            self._tool_done(now, L.TOOL_FAILED, f"cannot start {argv[0]}: {e.strerror or e}; nothing was changed", 0.0)
            return
        finally:
            if out is not subprocess.DEVNULL:
                out.close()
        self._tool = {"proc": proc, "started": now, "deadline": now + r["timeout_s"] + r["kill_margin_s"]}

    def _poll_tool(self, now):
        tool = self._tool
        if tool is None:
            return
        proc = tool["proc"]
        code = proc.poll()
        if code is None and now < tool["deadline"]:
            return
        try:
            with open(self.tool_output_file, "rb") as f:
                text = f.read(65536).decode("utf-8", "replace").strip()
            os.unlink(self.tool_output_file)
        except OSError:
            text = ""
        if code is None:
            # amp_safe returns within its --timeout whatever the SDR does. It has not: give up on it.
            # Only the script is killed; the tool under it ends itself at its own time limit.
            proc.kill()
            self._leftover.append(proc)
            code = L.TOOL_ASSUME_ENABLED
            text = f"amp_safe did not return within {now - tool['started']:.0f} s and was killed; assume enabled. {text}"
        elif code not in L.TOOL_NAMES:
            # Killed by a signal or failed in a way it never should: the tool may have started.
            text = f"amp_safe ended with status {code}, which is no result; assume enabled. {text}"
            code = L.TOOL_ASSUME_ENABLED
        if code == L.TOOL_STATE_FILE:
            # amp_safe refuses to start the tool when it cannot keep the record. An amplifier that
            # is enabled matters more than the record: from now on the tool runs without it.
            self._record_unwritable = True
            text += " From now on the watcher runs the tool without --state-file."
        self._tool = None
        self._tool_done(now, code, text or "no output", now - tool["started"])

    def _tool_done(self, now, code, text, duration_s):
        actions = self.logic.tool_done(now, code, text, duration_s)
        wall = self._wall()
        self.logic.last_tool["time_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall))
        self._carry_out(actions, now)
