"""Decision logic of the safety watcher: a pure state machine.

No I/O and no clock in here. The caller reads the inputs and passes them to
`WatcherLogic.step(now, inputs)`, which returns a list of `Action`s for the
caller to carry out; when the reset tool it was asked to run has ended, the
caller reports it with `tool_done()`. All times are seconds since boot
(CLOCK_BOOTTIME), the clock of `amp.sample_uptime_s` in the telemetry status.

A "reading" is one amplifier sample of the telemetry status file. The same
sample seen again in a later round is not counted again. Nothing trips or
clears on one reading (the OLA's rows have no checksum): a limit trips when
`trip_samples` of the last `trip_window` readings are beyond it, and clears
only after `clear_hold_s` of readings on the good side of the recover level.
Loss of telemetry is judged by time alone.

    telemetry    starting -> ok <-> lost (-> settling -> ok)
    amp_temp,    unproven -> ok <-> trip     amplifier sensor and case sensor
    case_temp
    voltage      unproven -> ok <-> trip     28 V rail out of range while powered
                 ok <-> unpowered            28 V rail absent: not a trip, but the radar may not run
    amp_enabled  idle -> pending -> resetting -> stuck | blocked
                                             enabled current with no radar process: run the reset tool
    pin          ok <-> unknown              no telemetry, no radar, pin not known safe: run the reset tool
    radar stop   idle -> interrupted -> killed    status not ok and a radar process alive

The rules, with every number and where it comes from: README.md in this directory.
"""
import math
import signal
from collections import deque
from dataclasses import dataclass

ON, OFF = "on", "off"
INFO, WARNING, URGENT = "info", "warning", "urgent"         # severity of an event, for the alerts stage

STARTING, SETTLING, LOST, OK = "starting", "settling", "lost", "ok"
IDLE, PENDING, RESETTING, BLOCKED, STUCK = "idle", "pending", "resetting", "blocked", "stuck"
LATCHED = (RESETTING, BLOCKED, STUCK)
INTERRUPTED, KILLED = "interrupted", "killed"
SDR_READY, SDR_BOOTLOADER, SDR_ABSENT, SDR_UNKNOWN = "ready", "bootloader", "absent", "unknown"

# Exit codes of amp_safe (tools/sdr_gpio_safe/README.md). Only 0 means safe. 0 and 3 are the
# results of a tool that ran; every other one says "nothing was changed by this call".
TOOL_OK, TOOL_ASSUME_ENABLED, TOOL_FAILED, TOOL_STATE_FILE = 0, 3, 5, 8
TOOL_NAMES = {0: "OK", 2: "NOT_FOUND", 3: "ASSUME_ENABLED", 4: "TIMEOUT", 5: "FAILED", 6: "BUSY",
              7: "AMBIGUOUS", 8: "STATE_FILE", 64: "USAGE"}
TOOL_LOST_MARGIN_S = 10.0       # a tool run that the caller never reported is written off this long after its limits

# A value outside these is a damaged reading, not a measurement. The ADC's full scale is 409 C,
# 12 A and 45 V with the control board's dividers; the amplifier is rated -40 to 85 C and draws at
# most 8.65 A at 28 V. Small negative values are noise around zero with the 28 V off.
PLAUSIBLE = {"temp_c": (-60.0, 150.0), "case_temp_c": (-60.0, 150.0),
             "current_a": (-1.0, 12.0), "voltage_v": (-2.0, 40.0)}
WARN_HYSTERESIS_C = 2.0         # a temperature warning ends this far on the good side of its level
OK_FORGIVES = 0.25              # stop rule: a second of ok takes this much off the seconds counted as not ok
RECENT_S = 60.0                 # "recent" in the loop's own health: the last one to two minutes
LABELS = {"amp_temp": "amplifier temperature", "case_temp": "amplifier case temperature"}


@dataclass(frozen=True)
class Sample:
    """One amplifier reading. A value that is missing or not plausible is None."""
    uptime: float
    temp_c: float = None
    case_temp_c: float = None
    current_a: float = None
    voltage_v: float = None


@dataclass(frozen=True)
class Inputs:
    sample: Sample = None           # the amplifier reading, if the telemetry status has a usable one
    telemetry_reason: str = ""      # why not, when there is none
    radar: tuple = ()               # (pid, age in seconds) of each process whose executable is the radar binary
    override: bool = False          # the user's "run without amplifier telemetry"
    pin_safe: bool = False          # the shared record says that the amplifier pin is known to be safe
    sdr: str = SDR_UNKNOWN          # the SDR on USB: ready, bootloader (never initialised: pin safe), absent
    tool_busy: bool = False         # another run of the reset tool holds its lock right now


@dataclass(frozen=True)
class Action:
    """Something the caller must do.

    kind        data
    "event"     dict for the event log: event, severity, text, and the event's own fields
    "signal"    (signal number, pids): send it to those radar processes
    "run_tool"  None: run the reset tool, then call tool_done()
    """
    kind: str
    data: object = None


def _number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        return None
    return float(value)


def amp_sample(read, now, max_age_s):
    """The amplifier reading in the telemetry status: (Sample, "") or (None, why not).

    `read` is what radar_runner.common.status.read_status returned. Usable means: the file is
    fresh, `amp.fresh` is true, and the reading's own time is at most `max_age_s` back.
    """
    if not read.ok:
        return None, f"telemetry status file {read.state}: {read.error}"
    amp = read.data.get("amp")
    if not isinstance(amp, dict):
        return None, "the telemetry status has no amplifier readings"
    if amp.get("fresh") is not True:
        return None, f"amplifier readings not fresh: {str(amp.get('reason') or 'no reason given')[:200]}"
    up = _number(amp.get("sample_uptime_s"), 0.0, math.inf)
    if up is None:
        return None, "the amplifier reading has no time"
    if not -1.0 <= now - up <= max_age_s:
        return None, f"the amplifier reading is {now - up:.1f} s old"
    return Sample(up, **{name: _number(amp.get(name), *limits) for name, limits in PLAUSIBLE.items()}), ""


def _band(value, on_at, off_below):
    return ON if value >= on_at else OFF if value < off_below else None


class Latch:
    """A yes/no state that follows timed readings, each of which says ON, OFF or neither (None).

    It turns on when `k` of the last `m` readings say ON and those span at least `span_s`. So one
    wrong reading cannot turn it on, and one reading on the other side cannot keep it off. It
    turns off after OFF readings in a row for at least `hold_s` (and at least `k` of them); any
    other reading starts that count again. A gap of more than `max_gap_s` between two readings
    forgets the readings before it.

    `active=None` at the start means "not proven either way": it becomes False as soon as `k` of
    the last `m` readings say OFF, and stays None on readings in between.
    """

    def __init__(self, k, m, span_s, hold_s, max_gap_s, active=False):
        self.k, self.m, self.span_s, self.hold_s, self.max_gap_s = k, max(m, k), span_s, hold_s, max_gap_s
        self.active = active
        self.since = None           # time of the reading that changed it last
        self._recent = deque(maxlen=max(m, k))      # (time, verdict)
        self._off_n = 0             # OFF readings in a row, and the time of the first of them
        self._off_first = None
        self._last = None

    def _k_of_m(self, verdict):
        times = [t for t, v in self._recent if v == verdict]
        return len(times) >= self.k and times[-1] - times[0] >= self.span_s

    def update(self, t, verdict):
        """Feed one reading. Returns True if `active` changed."""
        if self._last is not None and t - self._last > self.max_gap_s:
            self._recent.clear()
            self._off_n = 0
        self._last = t
        self._recent.append((t, verdict))
        if verdict == OFF:
            if self._off_n == 0:
                self._off_first = t
            self._off_n += 1
        else:
            self._off_n = 0
        if self.active is not True:
            if self._k_of_m(ON):
                new = True
            elif self.active is None and self._k_of_m(OFF):
                new = False
            else:
                return False
        elif self._off_n >= self.k and t - self._off_first >= self.hold_s:
            new = False
            self._recent.clear()
        else:
            return False
        self.active, self.since = new, t
        return True

    @property
    def counting(self):
        return any(v == ON for _, v in self._recent)


class WatcherLogic:
    def __init__(self, cfg, now, restored=None):
        """`restored` is what persistent() returned earlier in this boot, if the caller kept it."""
        self.cfg = cfg
        self.started = now
        restored = restored if isinstance(restored, dict) else {}
        self._gap = cfg["telemetry_lost_s"]
        # Without a record of this boot, a limit is not proven either way until readings are on the
        # good side of its recover level: the watcher may have been restarted in the middle of a trip.
        was = restored.get("trips") if isinstance(restored.get("trips"), dict) else None
        self.trips = {name: self._latch(cfg["clear_hold_s"], None if was is None else bool(was.get(name)))
                      for name in ("amp_temp", "case_temp", "voltage")}
        self.unpowered = self._latch(cfg["clear_hold_s"])
        self.contradiction = self._latch(cfg["clear_hold_s"])       # enabled current without the 28 V
        self.warn = {name: self._latch(cfg["clear_hold_s"]) for name in ("amp_temp", "case_temp")}
        self.latest = None              # the newest reading
        self._sample_up = None

        self._good = self._in_a_row()   # complete readings are arriving (turned off by time)
        self._complete_up = None        # time of the newest complete reading
        self._was_lost = False
        self.telemetry = STARTING
        self.telemetry_since = now
        self.telemetry_reason = "no reading yet"

        self.enabled = IDLE             # the "amplifier enabled with no radar process" rule
        self.enabled_since = now
        self._high_first = None         # pending: time of the first enabled reading, of the last, and their number
        self._high_last = None
        self._high_n = 0
        self._low = self._in_a_row()    # resetting, blocked or stuck: shutdown current seen again
        self.attempts = 0               # reset tool runs since the rule fired
        self.ran = 0                    # ... of which the tool really ran (OK or ASSUME_ENABLED)
        self._unchanged_since = None    # since when every run has ended with "nothing was changed"
        self._next_attempt = None
        self._stuck_check = None
        if restored.get("enabled") in LATCHED:
            self.enabled = STUCK if restored["enabled"] == STUCK else RESETTING
            self.ran = cfg["reset"]["stuck_after_attempts"] if self.enabled == STUCK else 0
            self._next_attempt = now + (cfg["reset"]["stuck_retry_s"] if self.enabled == STUCK else 0.0)

        self.signalled = bool(restored.get("signalled"))    # the watcher itself signalled a radar: pin not known safe
        self.pin_unknown = False        # no telemetry, no radar, pin not known safe: reset tool on a timer
        self.pin_unknown_since = None
        self._next_blind = None
        self._no_radar_since = now
        self._pin = {"record_safe": None, "sdr": SDR_UNKNOWN}

        self.tool_running = False
        self._tool_started = None
        self._tool_for = None           # "enabled" or "pin"
        self.last_tool = None

        self.ok = False
        self.tripped = False
        self.reason = "the watcher has just started"
        self.problems = []
        self._not_ok_s = 0.0            # for the stop rule: time not ok, less time ok, between 0 and the grace period
        self._judged_at = now
        self._reported_ok = None        # the last value of `ok` that went into the event log
        self.warnings = {}              # key -> {"text": ..., "since_uptime_s": ...}

        self.stop = IDLE                # stopping a radar that outlived a not-ok status
        self._stop_started = None
        self._stop_at = None
        self.radar_pids = ()
        self._radar_waiting = ()        # radars too young to be signalled yet

        self._last_step = None
        self._gaps = [0.0, 0.0]         # longest gap between rounds in this and in the previous RECENT_S
        self._gaps_started = now
        self.loop = {"last_gap_s": 0.0, "recent_longest_gap_s": 0.0, "longest_gap_s": 0.0}
        self.counters = {"readings": 0, "trips": 0, "clears": 0, "telemetry_losses": 0, "stalls": 0,
                         "tool_runs": 0, "tool_ok": 0, "radar_sigint": 0, "radar_sigkill": 0}
        self._actions = []

    # ------------------------------------------------------------------ helpers

    def _latch(self, hold_s, active=False):
        c = self.cfg
        return Latch(c["trip_samples"], c["trip_window"], c["trip_span_s"], hold_s, self._gap, active)

    def _in_a_row(self):
        c = self.cfg
        return Latch(c["trip_samples"], c["trip_samples"], c["trip_span_s"], 0.0, self._gap)

    def _event(self, event, severity, text, **fields):
        self._actions.append(Action("event", {"event": event, "severity": severity, "text": text, **fields}))

    def _values(self):
        s = self.latest
        return {} if s is None else {"temp_c": s.temp_c, "case_temp_c": s.case_temp_c,
                                     "current_a": s.current_a, "voltage_v": s.voltage_v}

    def ready(self, now):
        """False right after the start, until the first readings are judged (or it is clear that none come).

        The caller publishes nothing until then, so that a restarted watcher does not report
        "not ok" for a second before it reports "ok".
        """
        return self._good.active or now - self.started >= self.cfg["telemetry_lost_s"]

    def persistent(self):
        """What must survive a restart of the watcher within one boot (plain JSON types)."""
        return {"trips": {name: latch.active is True for name, latch in self.trips.items()},
                "enabled": self.enabled if self.enabled in LATCHED else IDLE, "signalled": self.signalled}

    def wants_tool(self):
        """True while a run of the reset tool may be asked for soon: the caller then looks at the tool's lock."""
        return self.enabled in LATCHED or self.pin_unknown

    # ------------------------------------------------------------------ one round

    def step(self, now, inp):
        self._actions = []
        self._watch_loop(now)
        if self.tool_running and now - self._tool_started > (self.cfg["reset"]["timeout_s"] + TOOL_LOST_MARGIN_S
                                                             + self.cfg["reset"]["kill_margin_s"]):
            # The caller never reported the end of the run (an error between the decision and the start).
            self._tool_result(now, TOOL_ASSUME_ENABLED, "the watcher lost track of this run of the reset tool", None)
        sample = inp.sample
        new = sample is not None and (self._sample_up is None or sample.uptime > self._sample_up)
        if new:
            self._sample_up, self.latest = sample.uptime, sample
            self.counters["readings"] += 1
            self._limits(sample)
        self._telemetry(now, inp, new)
        self._amp_enabled(now, inp, new)
        self._pin_unknown(now, inp)
        self._overall(now, inp)
        self._stop_radar(now, inp)
        self._warnings(now, inp)
        return self._actions

    def _watch_loop(self, now):
        if now - self._gaps_started >= RECENT_S:
            self._gaps_started, self._gaps = now, [0.0, self._gaps[0]]
        if self._last_step is not None:
            gap = now - self._last_step
            self._gaps[0] = max(self._gaps[0], gap)
            self.loop["last_gap_s"] = round(gap, 3)
            self.loop["longest_gap_s"] = max(self.loop["longest_gap_s"], round(gap, 3))
            if gap > self.cfg["telemetry_lost_s"]:
                # Nothing was looked at for this long. The counts of readings start again by
                # themselves (Latch.max_gap_s, and the same test in _amp_enabled).
                self.counters["stalls"] += 1
                self._event("stall", WARNING, f"the watcher's own loop stalled for {gap:.1f} s")
        self.loop["recent_longest_gap_s"] = round(max(self._gaps), 3)
        self._last_step = now

    # ---- limits on one new reading

    def _limits(self, s):
        c, t, v = self.cfg, s.uptime, s.voltage_v
        # Both sensors are judged whenever they give a value, whatever the voltage reading says:
        # a failed voltage sense must not hide the amplifier's own sensor.
        for name, value in (("amp_temp", s.temp_c), ("case_temp", s.case_temp_c)):
            if value is None:
                continue
            self._trip(name, t, _band(value, c["temp_trip_c"], c["temp_clear_c"]),
                       f"{LABELS[name]} {value:.1f} C",
                       f"at or above {c['temp_trip_c']:g} C", f"below {c['temp_clear_c']:g} C")
            self.warn[name].update(t, _band(value, c["temp_warn_c"], c["temp_warn_c"] - WARN_HYSTERESIS_C))
        if v is None:
            return
        low, high, margin = c["voltage_min_v"], c["voltage_max_v"], c["voltage_recover_margin_v"]
        good = low + margin <= v <= high - margin
        absent = v < c["unpowered_below_v"]         # near 0 V: the amplifier is unpowered
        if self.unpowered.update(t, ON if absent else OFF if good else None):
            if self.unpowered.active:
                self._event("unpowered", INFO, f"amplifier 28 V absent ({v:.1f} V)", condition="voltage", **self._values())
            else:
                self._event("powered", INFO, f"amplifier 28 V present again ({v:.1f} V)", condition="voltage",
                            **self._values())
        self._trip("voltage", t, ON if not absent and not low <= v <= high else OFF if good else None,
                   f"amplifier supply {v:.2f} V", f"outside {low:g}-{high:g} V",
                   f"inside {low + margin:g}-{high - margin:g} V")
        # An amplifier cannot draw enabled current without its supply: one of the two readings is wrong.
        if s.current_a is not None:
            self.contradiction.update(t, ON if absent and s.current_a > c["enabled_current_a"] else OFF)

    def _trip(self, name, t, verdict, what, bad, good):
        d = self.trips[name]
        before = d.active
        if not d.update(t, verdict):
            return
        if d.active:
            self.counters["trips"] += 1
            self._event("trip", WARNING if name == "voltage" else URGENT,
                        f"{what}: {bad} in {d.k} of the last {d.m} readings", condition=name, **self._values())
        elif before:
            self.counters["clears"] += 1
            self._event("clear", INFO, f"{what}: {good} for {d.hold_s:g} s", condition=name, **self._values())

    # ---- is there usable telemetry

    def _missing(self, s):
        """Names of the values without which a reading does not count as usable."""
        need = ["current_a", "voltage_v", "temp_c"] + (["case_temp_c"] if self.cfg["require_case_temp"] else [])
        return [name for name in need if getattr(s, name) is None]

    def _telemetry(self, now, inp, new):
        lost_after = self.cfg["telemetry_lost_s"]
        if new:
            missing = self._missing(inp.sample)
            self._good.update(inp.sample.uptime, OFF if missing else ON)
            if missing:
                self.telemetry_reason = "missing or not plausible: " + ", ".join(missing)
            else:
                self._complete_up = inp.sample.uptime
                self.telemetry_reason = ""
        elif inp.sample is None:
            self.telemetry_reason = inp.telemetry_reason or "no usable reading"
        if now - self._unusable_since() > lost_after:
            if self._good.active:
                self.counters["telemetry_losses"] += 1
            if self._good.active or self._good.counting:
                self._good = self._in_a_row()
            self._was_lost = True
            state = LOST
        elif self._good.active:
            state = OK
        elif self._good.counting:
            state = SETTLING
        else:
            state = LOST if self._was_lost else STARTING
        if state != self.telemetry:
            self.telemetry, self.telemetry_since = state, now

    def _unusable_since(self):
        return self.started if self._complete_up is None else self._complete_up

    # ---- amplifier enabled with no radar process

    def _set_enabled(self, state, now):
        self.enabled, self.enabled_since = state, now

    def _amp_enabled(self, now, inp, new):
        c, r = self.cfg, self.cfg["reset"]
        radar = bool(inp.radar)
        s = self.latest
        current = s.current_a if new else None                  # a new reading of the current, if any
        high = current is not None and current > c["enabled_current_a"]
        # Is the newest reading recent and does it say "enabled"? Without that, nothing is started.
        evidence = (s is not None and now - s.uptime <= c["telemetry_lost_s"] and s.current_a is not None
                    and s.current_a > c["enabled_current_a"])
        if self.enabled == IDLE:
            if high and not radar:
                self._set_enabled(PENDING, now)
                self._high_first, self._high_last, self._high_n = s.uptime, s.uptime, 1
        elif self.enabled == PENDING:
            if radar or not evidence or (new and s.uptime - self._high_last > c["telemetry_lost_s"]):
                self._set_enabled(IDLE, now)
            elif high:
                self._high_last, self._high_n = s.uptime, self._high_n + 1
                held = s.uptime - self._high_first
                if held >= c["idle_enabled_s"] and self._high_n >= c["trip_samples"]:
                    self._set_enabled(RESETTING, now)
                    self.counters["trips"] += 1
                    self.attempts, self.ran, self._unchanged_since = 0, 0, None
                    self._next_attempt, self._stuck_check = now, None
                    self._low = self._in_a_row()
                    self._event("trip", WARNING, f"amplifier enabled with no radar process: {current:.2f} A for "
                                f"{held:.1f} s; running the SDR GPIO reset tool", condition="amp_enabled",
                                **self._values())
                    return      # the status says so first; the tool starts in a later round
        if self.enabled not in LATCHED or self.tool_running:
            return
        if current is not None and self._low.update(s.uptime, OFF if high else ON):
            was = self.enabled
            self._set_enabled(IDLE, now)
            self._low = self._in_a_row()
            self.counters["clears"] += 1
            self._event("clear", INFO, f"amplifier back at shutdown current ({current:.2f} A) after "
                        f"{self.attempts} run(s) of the reset tool" + (": no longer stuck enabled" if was == STUCK else ""),
                        condition="amp_enabled", **self._values())
            return
        if radar or not evidence:
            return      # never with a radar process alive, and never without a reading that says "enabled"
        if self.enabled != STUCK and self._stuck_check is not None and now >= self._stuck_check:
            self._set_enabled(STUCK, now)
            self._event("stuck", URGENT, f"AMPLIFIER STUCK ENABLED: {s.current_a:.2f} A with no radar process after "
                        f"the reset tool ran {self.ran} times (last: {self._tool_text()}). About 70 W of heat: "
                        f"cut the power. Retrying every {r['stuck_retry_s']:g} s",
                        condition="amp_enabled", **self._values())
        if (self.enabled == RESETTING and self._unchanged_since is not None
                and now - self._unchanged_since >= r["blocked_after_s"]):
            self._set_enabled(BLOCKED, now)
            self._event("blocked", URGENT, f"AMPLIFIER ENABLED ({s.current_a:.2f} A) with no radar process AND THE RESET "
                        f"TOOL CANNOT RUN: for {now - self._unchanged_since:.0f} s every try ended with "
                        f"{self._tool_text()} (BUSY: the SDR is held by another program). About 70 W of heat. "
                        f"Trying every {r['blocked_retry_s']:g} s", condition="amp_enabled", **self._values())
        if now >= self._next_attempt and not inp.tool_busy:
            self._run_tool(now, "enabled", f"amplifier at {s.current_a:.2f} A with no radar process")

    def _run_tool(self, now, purpose, why):
        self.tool_running, self._tool_started, self._tool_for = True, now, purpose
        self.counters["tool_runs"] += 1
        self._event("reset_tool", INFO, f"running the reset tool: {why}",
                    condition="amp_enabled" if purpose == "enabled" else "pin")
        self._actions.append(Action("run_tool"))

    def _tool_text(self):
        t = self.last_tool
        return "none" if t is None else f"{t['name']} (exit {t['code']})"

    def tool_done(self, now, code, text, duration_s=None):
        """The reset tool has ended with exit status `code`. Returns actions, as step() does."""
        self._actions = []
        self._tool_result(now, code, text, duration_s)
        return self._actions

    def _tool_result(self, now, code, text, duration_s):
        r = self.cfg["reset"]
        purpose, self.tool_running, self._tool_for = self._tool_for, False, None
        self.counters["tool_ok"] += code == TOOL_OK
        self.last_tool = {"uptime_s": round(now, 3), "code": code, "name": TOOL_NAMES.get(code, "UNKNOWN"),
                          "text": " ".join(str(text).split())[:400],
                          "duration_s": None if duration_s is None else round(duration_s, 2)}
        self._event("reset_tool_result", INFO if code == TOOL_OK else WARNING,
                    f"reset tool: {self._tool_text()}: {self.last_tool['text']}",
                    condition="amp_enabled" if purpose == "enabled" else "pin", code=code, name=self.last_tool["name"])
        if code == TOOL_OK:
            self.signalled = False                          # the tool verified the pin
        if purpose == "pin":
            self._next_blind = now + r["blind_retry_s"]
            return
        self.attempts += 1
        self._low = self._in_a_row()                        # judged on readings taken after the run
        if code in (TOOL_OK, TOOL_ASSUME_ENABLED):
            # The tool ran. 3: it may itself have left the amplifier enabled: again soon. 0: give
            # the current time to show it. Only these runs count towards "stuck".
            self.ran += 1
            self._unchanged_since = None
            delay = r["retry_prompt_s"] if code == TOOL_ASSUME_ENABLED else r["retry_s"]
            if self.enabled == BLOCKED:
                self._set_enabled(RESETTING, now)
            if self.enabled != STUCK and self.ran < r["stuck_after_attempts"]:
                self._next_attempt = now + delay
            else:
                self._stuck_check = now + delay             # still enabled then: stuck
                self._next_attempt = now + r["stuck_retry_s"]
        else:
            # Nothing was changed by this call (6 BUSY: something else has the SDR). Trying again
            # costs no enable pulse; it says nothing about whether a reset would help.
            if self._unchanged_since is None:
                self._unchanged_since = now
            self._next_attempt = now + (r["blocked_retry_s"] if self.enabled == BLOCKED else
                                        r["stuck_retry_s"] if self.enabled == STUCK else r["retry_s"])

    # ---- no telemetry, no radar, and the pin not known to be safe

    def _pin_unknown(self, now, inp):
        r = self.cfg["reset"]
        if inp.radar:
            self._no_radar_since = None
        elif self._no_radar_since is None:
            self._no_radar_since = now
        self._pin = {"record_safe": bool(inp.pin_safe), "sdr": inp.sdr}
        s = self.latest
        if (self.signalled and self.telemetry == OK and not inp.radar and self.enabled == IDLE and s.current_a is not None
                and s.current_a <= self.cfg["enabled_current_a"] and not self.unpowered.active):
            self.signalled = False                          # the current shows the amplifier in shutdown
        # With usable telemetry the current decides (_amp_enabled). Without it, for long enough
        # that it is not just a telemetry restart, the record of the pin is all there is. An SDR
        # that was never initialised since its power-up (boot loader) has its pins floating: safe.
        blind = (self.telemetry == LOST and now - self._unusable_since() >= r["blind_delay_s"]
                 and self._no_radar_since is not None and now - self._no_radar_since >= r["blind_delay_s"]
                 and (self.signalled or not inp.pin_safe) and inp.sdr == SDR_READY)
        if blind and not self.pin_unknown:
            self.pin_unknown, self.pin_unknown_since, self._next_blind = True, now, now
            self.counters["trips"] += 1
            why = "the watcher signalled a radar" if self.signalled else "the shared record does not say safe"
            self._event("trip", URGENT, "amplifier pin not known to be safe, no amplifier telemetry for "
                        f"{now - self._unusable_since():.0f} s and no radar process ({why}): running the SDR GPIO "
                        f"reset tool, then every {r['blind_retry_s']:g} s while this lasts", condition="pin")
        elif self.pin_unknown and not blind and not (self.tool_running and self._tool_for == "pin"):
            self.pin_unknown = False
            self.counters["clears"] += 1
            self._event("clear", INFO, "amplifier pin: " + (
                "known safe again" if inp.pin_safe and not self.signalled else
                "amplifier telemetry is back, the current decides" if self.telemetry != LOST else
                "a radar process is running" if inp.radar else f"SDR {inp.sdr}"), condition="pin")
        elif (self.pin_unknown and not self.tool_running and now > self.pin_unknown_since and now >= self._next_blind
              and not inp.tool_busy):
            self._run_tool(now, "pin", "amplifier pin not known to be safe, no telemetry, no radar process")

    # ---- the overall status

    def _overall(self, now, inp):
        c, s = self.cfg, self.latest
        problems = []           # (key, text, is a trip), most urgent first
        if self.enabled == STUCK:
            problems.append(("amp_enabled", "AMPLIFIER STUCK ENABLED with no radar process: "
                             f"the reset tool ran {self.ran} times and did not help (last: {self._tool_text()})", True))
        elif self.enabled == BLOCKED:
            problems.append(("amp_enabled", "AMPLIFIER ENABLED with no radar process and the reset tool cannot run "
                             f"(last: {self._tool_text()}; BUSY: the SDR is held by another program)", True))
        elif self.enabled == RESETTING:
            problems.append(("amp_enabled", "amplifier enabled with no radar process: resetting the SDR GPIO "
                             f"({self.attempts} runs of the reset tool so far)", True))
        if self.pin_unknown:
            problems.append(("pin", "amplifier pin not known to be safe, with no amplifier telemetry and no radar "
                             f"process: resetting the SDR GPIO (last: {self._tool_text()})", True))
        for name, value in (("amp_temp", s and s.temp_c), ("case_temp", s and s.case_temp_c)):
            at = "" if value is None else f" (now {value:.1f} C)"
            if self.trips[name].active:
                problems.append((name, f"{LABELS[name]} reached {c['temp_trip_c']:g} C{at}; "
                                 f"clears below {c['temp_clear_c']:g} C", True))
            elif self.trips[name].active is None and self.telemetry == OK:
                problems.append((name, f"{LABELS[name]} not yet seen below {c['temp_clear_c']:g} C since the "
                                 f"watcher started{at}: a stop may have been in force", False))
        at = "" if s is None or s.voltage_v is None else f" (now {s.voltage_v:.2f} V)"
        if self.trips["voltage"].active:
            problems.append(("voltage", f"amplifier supply outside {c['voltage_min_v']:g}-{c['voltage_max_v']:g} V{at}",
                             True))
        elif self.trips["voltage"].active is None and self.telemetry == OK and not self.unpowered.active:
            problems.append(("voltage", "amplifier supply not yet seen inside its recover band since the watcher "
                             f"started{at}", False))
        if self.contradiction.active:
            problems.append(("voltage", "readings contradict each other: enabled current without the 28 V"
                             f"{at}: the voltage or the current reading is wrong", False))
        elif self.unpowered.active and not c["allow_unpowered"]:
            problems.append(("voltage", "amplifier 28 V absent: the radar must not transmit into an unpowered amplifier",
                             False))
        if self.telemetry == STARTING:
            problems.append(("telemetry", f"waiting for the first amplifier readings: {self.telemetry_reason}", False))
        elif self.telemetry != OK and not inp.override:
            if self.telemetry == SETTLING:
                text = "amplifier readings are arriving; judging the first of them"
            elif self._complete_up is None:
                text = (f"no usable amplifier telemetry since the watcher started {now - self.started:.0f} s ago: "
                        f"{self.telemetry_reason}")
            else:
                text = f"no usable amplifier telemetry for {now - self._complete_up:.0f} s: {self.telemetry_reason}"
            problems.append(("telemetry", text, False))
        ok = not problems
        # For the stop rule: seconds not ok count up, seconds ok count down at a quarter of that.
        # Short spells of ok (telemetry that comes and goes) therefore do not start the grace
        # period afresh; four times the grace period of ok does.
        passed = min(max(now - self._judged_at, 0.0), 1.0)
        self._judged_at = now
        self._not_ok_s = min(max(self._not_ok_s + (-OK_FORGIVES * passed if self.ok else passed), 0.0),
                             c["stop_grace_s"])
        self.ok = ok
        self.tripped = any(trip for _, _, trip in problems)
        self.reason = problems[0][1] if problems else ""
        self.problems = [{"condition": key, "text": text, "trip": trip} for key, text, trip in problems]
        if self.ready(now) and ok != self._reported_ok:
            if not ok or self._reported_ok is not None:     # a first "ok" after the start is not news
                self._event("ok" if ok else "not_ok", INFO if ok else WARNING,
                            "the radar may run again" if ok else f"the radar may not run: {self.reason}")
            self._reported_ok = ok

    # ---- a radar process that outlives a not-ok status

    def _stop_radar(self, now, inp):
        c = self.cfg
        self.radar_pids = tuple(pid for pid, _ in inp.radar)
        # A radar is not signalled while it may still be setting up the SDR: ORCA has no SIGINT
        # handler until then, and a radar that dies there leaves the amplifier enabled.
        pids = tuple(pid for pid, age in inp.radar if age >= c["radar_setup_s"])
        self._radar_waiting = tuple(pid for pid, age in inp.radar if age < c["radar_setup_s"])
        if self.stop == IDLE:
            if self.ok or not pids:
                return
            if self._not_ok_s < c["stop_grace_s"]:
                return      # the manager should stop it; this is for a manager that is dead or hung
            self.stop, self._stop_started, self._stop_at = INTERRUPTED, now, now
            self.counters["radar_sigint"] += 1
            self._event("radar_stop", WARNING, f"radar process {list(pids)} still running after "
                        f"{c['stop_grace_s']:g} s of a status that is not ok ({self.reason}): sent SIGINT", pids=list(pids))
            self._signal(signal.SIGINT, pids)
        elif not pids:
            self._event("radar_stopped", INFO, f"radar process gone {now - self._stop_started:.1f} s after SIGINT"
                        + (" (after SIGKILL)" if self.stop == KILLED else ""))
            self.stop = IDLE
        elif self.stop == INTERRUPTED and now - self._stop_at >= c["radar_exit_s"]:
            self.stop, self._stop_at = KILLED, now
            self.counters["radar_sigkill"] += 1
            self._event("radar_kill", WARNING, f"radar process {list(pids)} did not exit within "
                        f"{c['radar_exit_s']:g} s of SIGINT: sent SIGKILL", pids=list(pids))
            self._signal(signal.SIGKILL, pids)
        elif self.stop == KILLED and now - self._stop_at >= c["kill_retry_s"]:
            self._stop_at = now
            self.counters["radar_sigkill"] += 1
            self._event("radar_kill", URGENT, f"radar process {list(pids)} is still there "
                        f"{now - self._stop_started:.0f} s after the stop began: sent SIGKILL again", pids=list(pids))
            self._signal(signal.SIGKILL, pids)

    def _signal(self, signum, pids):
        self.signalled = True       # a radar stopped from outside may leave the pin low: not known safe from here
        self._actions.append(Action("signal", (signum, pids)))

    # ---- warnings: shown and logged, never a reason to stop

    def _warnings(self, now, inp):
        c, s = self.cfg, self.latest
        found = {}
        hot = [f"{LABELS[name]} {value:.1f} C" for name, value in
               (("amp_temp", s and s.temp_c), ("case_temp", s and s.case_temp_c))
               if self.warn[name].active and value is not None]
        if hot:
            found["amp_temp_high"] = f"{', '.join(hot)}: at or above {c['temp_warn_c']:g} C (stop at {c['temp_trip_c']:g} C)"
        if inp.override:
            found["override"] = ("OVERRIDE set: the radar may run without amplifier telemetry"
                                 + (f" (now: {self.telemetry_reason})" if self.telemetry in (LOST, SETTLING) else ""))
        if self.contradiction.active:
            found["contradiction"] = "enabled current is read together with no 28 V: one of the two readings is wrong"
        if self.unpowered.active and c["allow_unpowered"]:
            found["unpowered_allowed"] = "amplifier 28 V absent; allowed by the setting allow_unpowered"
        if s is not None and s.case_temp_c is None and not c["require_case_temp"]:
            found["case_temp_missing"] = "no amplifier case temperature (not required by the setting require_case_temp)"
        if self._radar_waiting and not self.ok:
            found["radar_setting_up"] = (f"radar process {list(self._radar_waiting)} is younger than "
                                         f"{c['radar_setup_s']:g} s: not stopped while it may be setting up the SDR")
        for key in sorted(found.keys() - self.warnings.keys()):
            self._event("warning", WARNING, found[key], warning=key)
        for key in sorted(self.warnings.keys() - found.keys()):
            self._event("warning_cleared", INFO, f"no longer: {self.warnings[key]['text']}", warning=key)
        self.warnings = {key: {"text": text, "since_uptime_s": self.warnings.get(key, {}).get("since_uptime_s", round(now, 3))}
                         for key, text in found.items()}

    # ------------------------------------------------------------------ status

    def snapshot(self, now, override=False):
        """The status to publish (plain JSON types)."""
        c, s = self.cfg, self.latest

        def r(value, digits=3):
            return None if value is None else round(value, digits)

        def limit(name, value):
            d = self.trips[name]
            warned = name in self.warn and self.warn[name].active
            return {"state": "trip" if d.active else "unproven" if d.active is None else "warn" if warned else OK,
                    "value": r(value), "since_uptime_s": r(d.since)}

        voltage = limit("voltage", s and s.voltage_v)
        if self.unpowered.active and not self.trips["voltage"].active:
            voltage.update(state="unpowered", since_uptime_s=r(self.unpowered.since))
        voltage["contradiction"] = bool(self.contradiction.active)
        next_in = None
        if self.enabled in LATCHED and not self.tool_running and self._next_attempt is not None:
            next_in = r(max(self._next_attempt - now, 0.0), 1)
        return {
            "ok": self.ok,
            "tripped": self.tripped,
            "reason": self.reason,
            "problems": self.problems,
            "warnings": [{"warning": key, **value} for key, value in sorted(self.warnings.items())],
            "conditions": {
                "telemetry": {"state": self.telemetry, "since_uptime_s": r(self.telemetry_since),
                              "reason": "" if self.telemetry == OK else self.telemetry_reason,
                              "overridden": bool(override) and self.telemetry in (LOST, SETTLING),
                              "reading_age_s": None if s is None else r(max(now - s.uptime, 0.0), 2)},
                "amp_temp": limit("amp_temp", s and s.temp_c),
                "case_temp": limit("case_temp", s and s.case_temp_c),
                "voltage": voltage,
                "amp_enabled": {"state": self.enabled, "value": r(s and s.current_a),
                                "since_uptime_s": r(self.enabled_since), "tool_runs": self.attempts,
                                "tool_ran": self.ran, "next_run_in_s": next_in},
                "pin": {"state": "unknown" if self.pin_unknown else OK, "since_uptime_s": r(self.pin_unknown_since),
                        "record_safe": self._pin["record_safe"], "signalled_by_watcher": self.signalled,
                        "sdr": self._pin["sdr"]},
            },
            "amp": dict(self._values(), sample_uptime_s=r(s and s.uptime)),
            "radar": {"running": bool(self.radar_pids), "pids": list(self.radar_pids), "stop": self.stop,
                      "stop_since_uptime_s": r(self._stop_started) if self.stop != IDLE else None,
                      "setting_up": list(self._radar_waiting)},
            "reset_tool": {"running": self.tool_running, "last": self.last_tool},
            "override": {"no_amp_telemetry": bool(override)},
            "limits": {"temp_warn_c": c["temp_warn_c"], "temp_trip_c": c["temp_trip_c"], "temp_clear_c": c["temp_clear_c"],
                       "voltage_min_v": c["voltage_min_v"], "voltage_max_v": c["voltage_max_v"],
                       "enabled_current_a": c["enabled_current_a"]},
            "counters": dict(self.counters),
            "loop": dict(self.loop),
        }
