"""Decision logic of the safety watcher: a pure state machine.

No I/O and no clock in here. The caller reads the inputs and passes them to
`WatcherLogic.step(now, inputs)`, which returns a list of `Action`s for the
caller to carry out; when the reset tool it was asked to run has ended, the
caller reports it with `tool_done()`. All times are seconds since boot
(CLOCK_BOOTTIME), the clock of `amp.sample_uptime_s` in the telemetry status.

A "reading" is one amplifier sample of the telemetry status file. The same
sample seen again in a later round is not counted again. Nothing trips or
clears on one reading (the OLA's rows have no checksum): a limit must be
exceeded in `trip_samples` readings in a row, and a trip clears only after
`clear_hold_s` of readings on the good side of the recover level. Loss of
telemetry is judged by time alone.

    telemetry    starting -> ok <-> lost (-> settling -> ok)
    amp_temp,    ok <-> trip        amplifier sensor (only with the 28 V present) and case sensor
    case_temp
    voltage      ok <-> trip        28 V rail out of range while powered
                 ok <-> unpowered   28 V rail absent: not a trip, but the radar may not run
    amp_enabled  idle -> pending -> resetting -> stuck    amplifier drawing enabled current with
                                                         no radar process: run the reset tool
    radar stop   idle -> interrupted -> killed           status not ok and a radar process alive

The rules, with every number and where it comes from: README.md in this directory.
"""
import math
import signal
from dataclasses import dataclass

ON, OFF = "on", "off"
INFO, WARNING, URGENT = "info", "warning", "urgent"         # severity of an event, for the alerts stage

STARTING, SETTLING, LOST, OK = "starting", "settling", "lost", "ok"
IDLE, PENDING, RESETTING, STUCK = "idle", "pending", "resetting", "stuck"
INTERRUPTED, KILLED = "interrupted", "killed"

# Exit codes of amp_safe (tools/sdr_gpio_safe/README.md). Only 0 means safe.
TOOL_OK, TOOL_ASSUME_ENABLED, TOOL_FAILED = 0, 3, 5
TOOL_NAMES = {0: "OK", 2: "NOT_FOUND", 3: "ASSUME_ENABLED", 4: "TIMEOUT", 5: "FAILED", 6: "BUSY",
              7: "AMBIGUOUS", 8: "STATE_FILE", 64: "USAGE"}

# A value outside these is a damaged reading, not a measurement. The ADC's full scale is 409 C,
# 12 A and 45 V with the control board's dividers; the amplifier is rated -40 to 85 C and draws at
# most 8.65 A at 28 V. Small negative values are noise around zero with the 28 V off.
PLAUSIBLE = {"temp_c": (-60.0, 150.0), "case_temp_c": (-60.0, 150.0),
             "current_a": (-1.0, 12.0), "voltage_v": (-2.0, 40.0)}
WARN_HYSTERESIS_C = 2.0         # a temperature warning ends this far on the good side of its level
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
class PiSample:
    uptime: float
    soc_temp_c: float = None
    throttled: tuple = ()           # what is throttled right now, as the telemetry service names it


@dataclass(frozen=True)
class Inputs:
    sample: Sample = None           # the amplifier reading, if the telemetry status has a usable one
    telemetry_reason: str = ""      # why not, when there is none
    radar_pids: tuple = ()          # processes whose executable is the radar binary
    override: bool = False          # the user's "run without amplifier telemetry"
    power_lost: bool = False        # the power service says that main power is lost
    pi: PiSample = None
    telemetry_gap_s: float = None   # the telemetry loop's longest recent gap between rounds


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


def pi_sample(read):
    """The Pi's readings in the telemetry status, or None."""
    try:
        pi = read.data["sources"]["pi"]
        if not read.ok or pi["health"] != "ok":
            return None
        up = _number(pi["sample_uptime_s"], 0.0, math.inf)
        throttled = pi.get("throttled_now") or ()
        if up is None or not isinstance(throttled, (list, tuple)):
            return None
        return PiSample(up, _number(pi["values"].get("soc_temp_c"), -60.0, 150.0), tuple(map(str, throttled)))
    except (KeyError, TypeError, AttributeError):
        return None


def _band(value, on_at, off_below):
    return ON if value >= on_at else OFF if value < off_below else None


class Debounce:
    """A yes/no state that follows timed readings, each of which says ON, OFF or neither (None).

    It turns on after `n` ON readings in a row that span at least `span_s`, and off again after
    OFF readings in a row for at least `hold_s` (and at least `n` of them). Any other reading
    starts the count again, and so does a gap of more than `max_gap_s` between two readings.
    One wrong reading can therefore neither turn it on nor off.
    """

    def __init__(self, n, span_s, hold_s, max_gap_s):
        self.n, self.span_s, self.hold_s, self.max_gap_s = n, span_s, hold_s, max_gap_s
        self.active = False
        self.since = None           # time of the reading that changed it last
        self._count = 0
        self._first = None          # time of the first reading of the run being counted
        self._last = None           # time of the latest reading

    def update(self, t, verdict):
        """Feed one reading. Returns True if `active` changed."""
        if self._last is not None and t - self._last > self.max_gap_s:
            self._count = 0
        self._last = t
        if verdict != (OFF if self.active else ON):
            self._count = 0
            return False
        if self._count == 0:
            self._first = t
        self._count += 1
        if self._count < self.n or t - self._first < (self.hold_s if self.active else self.span_s):
            return False
        self.active, self.since, self._count = not self.active, t, 0
        return True

    def restart(self):
        self._count = 0

    @property
    def counting(self):
        return self._count > 0


class WatcherLogic:
    def __init__(self, cfg, now):
        self.cfg = cfg
        self.started = now
        n, span, hold, gap = cfg["trip_samples"], cfg["trip_span_s"], cfg["clear_hold_s"], cfg["telemetry_lost_s"]
        self.trips = {name: Debounce(n, span, hold, gap) for name in ("amp_temp", "case_temp", "voltage")}
        self.unpowered = Debounce(n, span, hold, gap)
        self.warn = {name: Debounce(n, span, hold, max(gap, 10.0))      # the Pi is read every 2 s
                     for name in ("amp_temp", "case_temp", "cold", "pi_temp", "pi_throttled")}
        self.latest = None              # the newest reading
        self._sample_up = None
        self._pi_up = None
        self._pi = None

        self._good = Debounce(n, span, 0.0, gap)    # complete readings are arriving (turned off by time)
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
        self._low = Debounce(n, span, 0.0, gap)     # resetting or stuck: shutdown current seen again
        self.attempts = 0               # reset tool runs since the rule fired
        self._next_attempt = None
        self._stuck_check = None
        self.tool_running = False
        self.last_tool = None

        self.ok = False
        self.tripped = False
        self.reason = "the watcher has just started"
        self.problems = []
        self._not_ok_since = now
        self._reported_ok = None        # the last value of `ok` that went into the event log
        self.warnings = {}              # key -> {"text": ..., "since_uptime_s": ...}

        self.stop = IDLE                # stopping a radar that outlived a not-ok status
        self._stop_started = None
        self._stop_at = None
        self._radar_since = None
        self.radar_pids = ()

        self._last_step = None
        self.loop = {"last_gap_s": 0.0, "longest_gap_s": 0.0}
        self.counters = {"readings": 0, "trips": 0, "clears": 0, "telemetry_losses": 0, "stalls": 0,
                         "tool_runs": 0, "tool_ok": 0, "radar_sigint": 0, "radar_sigkill": 0}
        self._actions = []

    # ------------------------------------------------------------------ helpers

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

    # ------------------------------------------------------------------ one round

    def step(self, now, inp):
        self._actions = []
        self._watch_loop(now)
        sample = inp.sample
        new = sample is not None and (self._sample_up is None or sample.uptime > self._sample_up)
        if new:
            self._sample_up, self.latest = sample.uptime, sample
            self.counters["readings"] += 1
            self._limits(sample, inp.power_lost)
        self._telemetry(now, inp, new)
        self._pi_limits(inp.pi)
        self._amp_enabled(now, inp, new)
        self._overall(now, inp)
        self._stop_radar(now, inp)
        self._warnings(now, inp)
        return self._actions

    def _watch_loop(self, now):
        if self._last_step is not None:
            gap = now - self._last_step
            self.loop["last_gap_s"] = round(gap, 3)
            self.loop["longest_gap_s"] = max(self.loop["longest_gap_s"], round(gap, 3))
            if gap > self.cfg["telemetry_lost_s"]:
                # Nothing was looked at for this long. The counts of readings in a row start again
                # by themselves (Debounce.max_gap_s, and the same test in _amp_enabled).
                self.counters["stalls"] += 1
                self._event("stall", WARNING, f"the watcher's own loop stalled for {gap:.1f} s")
        self._last_step = now

    # ---- limits on one new reading

    def _limits(self, s, power_lost):
        c, t, v = self.cfg, s.uptime, s.voltage_v
        powered = v is not None and v >= c["unpowered_below_v"]
        # The amplifier's own sensor needs the 28 V; the case sensor does not.
        temps = {"amp_temp": s.temp_c if powered else None, "case_temp": s.case_temp_c}
        for name, value in temps.items():
            if value is None:
                continue
            self._trip(name, t, _band(value, c["temp_trip_c"], c["temp_clear_c"]),
                       f"{LABELS[name]} {value:.1f} C",
                       f"at or above {c['temp_trip_c']:g} C", f"below {c['temp_clear_c']:g} C")
            self.warn[name].update(t, _band(value, c["temp_warn_c"], c["temp_warn_c"] - WARN_HYSTERESIS_C))
        known = [value for value in temps.values() if value is not None]
        if known:
            self.warn["cold"].update(t, ON if min(known) < c["cold_warn_c"]
                                     else OFF if min(known) > c["cold_warn_c"] + WARN_HYSTERESIS_C else None)
        if v is None:
            return
        low, high, margin = c["voltage_min_v"], c["voltage_max_v"], c["voltage_recover_margin_v"]
        good = low + margin <= v <= high - margin
        # Near 0 V the amplifier is unpowered. A rail on its way down while the power service
        # reports a power loss is the same thing, not a supply fault.
        absent = v < c["unpowered_below_v"] or (v < low and power_lost)
        if self.unpowered.update(t, ON if absent else OFF if good else None):
            if self.unpowered.active:
                self._event("unpowered", INFO, f"amplifier 28 V absent ({v:.1f} V)", condition="voltage", **self._values())
            else:
                self._event("powered", INFO, f"amplifier 28 V present again ({v:.1f} V)", condition="voltage",
                            **self._values())
        bad = v > high or (v < low and not absent)
        self._trip("voltage", t, ON if bad else OFF if good else None, f"amplifier supply {v:.2f} V",
                   f"outside {low:g}-{high:g} V", f"inside {low + margin:g}-{high - margin:g} V")

    def _trip(self, name, t, verdict, what, bad, good):
        d = self.trips[name]
        if not d.update(t, verdict):
            return
        if d.active:
            self.counters["trips"] += 1
            self._event("trip", WARNING if name == "voltage" else URGENT,
                        f"{what}: {bad} in {d.n} readings in a row", condition=name, **self._values())
        else:
            self.counters["clears"] += 1
            self._event("clear", INFO, f"{what}: {good} for {d.hold_s:g} s", condition=name, **self._values())

    def _pi_limits(self, pi):
        if pi is None or (self._pi_up is not None and pi.uptime <= self._pi_up):
            return
        self._pi_up, self._pi = pi.uptime, pi
        if pi.soc_temp_c is not None:
            level = self.cfg["pi_temp_warn_c"]
            self.warn["pi_temp"].update(pi.uptime, _band(pi.soc_temp_c, level, level - WARN_HYSTERESIS_C))
        self.warn["pi_throttled"].update(pi.uptime, ON if pi.throttled else OFF)

    # ---- is there usable telemetry

    def _missing(self, s):
        """Names of the values without which a reading does not count as usable."""
        need = ["current_a", "voltage_v"]
        if s.voltage_v is not None and s.voltage_v >= self.cfg["unpowered_below_v"]:
            need.append("temp_c")
        if self.cfg["require_case_temp"]:
            need.append("case_temp_c")
        return [name for name in need if getattr(s, name) is None]

    def _telemetry(self, now, inp, new):
        lost_after = self.cfg["telemetry_lost_s"]
        if new:
            missing = self._missing(inp.sample)
            if missing:
                self._good.restart()
                self.telemetry_reason = "missing or not plausible: " + ", ".join(missing)
            else:
                self._good.update(inp.sample.uptime, ON)
                self._complete_up = inp.sample.uptime
                self.telemetry_reason = ""
        elif inp.sample is None:
            self.telemetry_reason = inp.telemetry_reason or "no usable reading"
        since = self.started if self._complete_up is None else self._complete_up
        if now - since > lost_after:
            if self._good.active:
                self.counters["telemetry_losses"] += 1
                self._good = Debounce(self._good.n, self._good.span_s, 0.0, self._good.max_gap_s)
            self._good.restart()
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

    # ---- amplifier enabled with no radar process

    def _set_enabled(self, state, now):
        self.enabled, self.enabled_since = state, now

    def _amp_enabled(self, now, inp, new):
        c = self.cfg
        radar = bool(inp.radar_pids)
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
                    self.attempts, self._next_attempt, self._stuck_check = 0, now, None
                    self._low.restart()
                    self._event("trip", WARNING, f"amplifier enabled with no radar process: {current:.2f} A for "
                                f"{held:.1f} s; running the SDR GPIO reset tool", condition="amp_enabled",
                                **self._values())
        if self.enabled not in (RESETTING, STUCK) or self.tool_running:
            return
        if current is not None and self._low.update(s.uptime, OFF if high else ON):
            was = self.enabled
            self._set_enabled(IDLE, now)
            self._low = Debounce(self._low.n, self._low.span_s, 0.0, self._low.max_gap_s)
            self.counters["clears"] += 1
            self._event("clear", INFO, f"amplifier back at shutdown current ({current:.2f} A) after "
                        f"{self.attempts} run(s) of the reset tool" + (": no longer stuck enabled" if was == STUCK else ""),
                        condition="amp_enabled", **self._values())
            return
        if radar or not evidence:
            return      # never with a radar process alive, and never without a reading that says "enabled"
        if self.enabled == RESETTING and self._stuck_check is not None and now >= self._stuck_check:
            self._set_enabled(STUCK, now)
            self._event("stuck", URGENT, f"AMPLIFIER STUCK ENABLED: {s.current_a:.2f} A with no radar process after "
                        f"{self.attempts} runs of the reset tool (last: {self._tool_text()}). About 70 W of heat: "
                        f"cut the power. Retrying every {c['reset']['stuck_retry_s']:g} s",
                        condition="amp_enabled", **self._values())
        if now >= self._next_attempt:
            self.tool_running = True
            self.counters["tool_runs"] += 1
            self._event("reset_tool", INFO, f"running the reset tool (run {self.attempts + 1}): amplifier at "
                        f"{s.current_a:.2f} A with no radar process", condition="amp_enabled")
            self._actions.append(Action("run_tool"))

    def _tool_text(self):
        t = self.last_tool
        return "none" if t is None else f"{t['name']} (exit {t['code']})"

    def tool_done(self, now, code, text, duration_s=None):
        """The reset tool has ended with exit status `code`. Returns actions, as step() does."""
        self._actions = []
        r = self.cfg["reset"]
        self.tool_running = False
        self.attempts += 1
        self.counters["tool_ok"] += code == TOOL_OK
        self.last_tool = {"uptime_s": round(now, 3), "code": code, "name": TOOL_NAMES.get(code, "UNKNOWN"),
                          "text": " ".join(str(text).split())[:400],
                          "duration_s": None if duration_s is None else round(duration_s, 2)}
        self._event("reset_tool_result", INFO if code == TOOL_OK else WARNING,
                    f"reset tool run {self.attempts}: {self._tool_text()}: {self.last_tool['text']}",
                    condition="amp_enabled", code=code, name=self.last_tool["name"])
        # 3: the tool itself may have left the amplifier enabled: again soon. 0: give the current
        # time to show it. Everything else changed nothing (6 BUSY: something else has the SDR).
        delay = r["retry_prompt_s"] if code == TOOL_ASSUME_ENABLED else r["retry_s"]
        if self.enabled == RESETTING and self.attempts < r["stuck_after_attempts"]:
            self._next_attempt = now + delay
        else:
            self._stuck_check = now + delay                 # still enabled then: stuck
            self._next_attempt = now + r["stuck_retry_s"]
        self._low.restart()                                 # judged on readings taken after the run
        return self._actions

    # ---- the overall status

    def _overall(self, now, inp):
        c, s = self.cfg, self.latest
        problems = []           # (key, text, is a trip), most urgent first
        if self.enabled == STUCK:
            problems.append(("amp_enabled", "AMPLIFIER STUCK ENABLED with no radar process: "
                             f"{self.attempts} runs of the reset tool did not help (last: {self._tool_text()})", True))
        elif self.enabled == RESETTING:
            problems.append(("amp_enabled", "amplifier enabled with no radar process: resetting the SDR GPIO "
                             f"({self.attempts} runs of the reset tool so far)", True))
        for name, value in (("amp_temp", s and s.temp_c), ("case_temp", s and s.case_temp_c)):
            if self.trips[name].active:
                at = "" if value is None else f" (now {value:.1f} C)"
                problems.append((name, f"{LABELS[name]} reached {c['temp_trip_c']:g} C{at}; "
                                 f"clears below {c['temp_clear_c']:g} C", True))
        if self.trips["voltage"].active:
            at = "" if s is None or s.voltage_v is None else f" (now {s.voltage_v:.2f} V)"
            problems.append(("voltage", f"amplifier supply outside {c['voltage_min_v']:g}-{c['voltage_max_v']:g} V{at}",
                             True))
        if self.unpowered.active and not c["allow_unpowered"]:
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
        if ok != self.ok:
            self._not_ok_since = None if ok else now
        self.ok = ok
        if self.ready(now) and ok != self._reported_ok:
            if not ok or self._reported_ok is not None:     # a first "ok" after the start is not news
                self._event("ok" if ok else "not_ok", INFO if ok else WARNING,
                            "the radar may run again" if ok else f"the radar may not run: {problems[0][1]}")
            self._reported_ok = ok
        self.tripped = any(trip for _, _, trip in problems)
        self.reason = problems[0][1] if problems else ""
        self.problems = [{"condition": key, "text": text, "trip": trip} for key, text, trip in problems]

    # ---- a radar process that outlives a not-ok status

    def _stop_radar(self, now, inp):
        c = self.cfg
        pids = self.radar_pids = tuple(inp.radar_pids)
        if not pids:
            self._radar_since = None
        elif self._radar_since is None:
            self._radar_since = now
        if self.stop == IDLE:
            if self.ok or not pids:
                return
            waited = now - max(self._not_ok_since, self._radar_since)
            if waited < c["stop_grace_s"]:
                return      # the manager should stop it; this is for a manager that is dead or hung
            self.stop, self._stop_started, self._stop_at = INTERRUPTED, now, now
            self.counters["radar_sigint"] += 1
            self._event("radar_stop", WARNING, f"radar process {list(pids)} still running {waited:.1f} s after the "
                        f"status went not ok ({self.reason}): sent SIGINT", pids=list(pids))
            self._actions.append(Action("signal", (signal.SIGINT, pids)))
        elif not pids:
            self._event("radar_stopped", INFO, f"radar process gone {now - self._stop_started:.1f} s after SIGINT"
                        + (" (after SIGKILL)" if self.stop == KILLED else ""))
            self.stop = IDLE
        elif self.stop == INTERRUPTED and now - self._stop_at >= c["radar_exit_s"]:
            self.stop, self._stop_at = KILLED, now
            self.counters["radar_sigkill"] += 1
            self._event("radar_kill", WARNING, f"radar process {list(pids)} did not exit within "
                        f"{c['radar_exit_s']:g} s of SIGINT: sent SIGKILL", pids=list(pids))
            self._actions.append(Action("signal", (signal.SIGKILL, pids)))
        elif self.stop == KILLED and now - self._stop_at >= c["kill_retry_s"]:
            self._stop_at = now
            self.counters["radar_sigkill"] += 1
            self._event("radar_kill", URGENT, f"radar process {list(pids)} is still there "
                        f"{now - self._stop_started:.0f} s after the stop began: sent SIGKILL again", pids=list(pids))
            self._actions.append(Action("signal", (signal.SIGKILL, pids)))

    # ---- warnings: shown and logged, never a reason to stop

    def _warnings(self, now, inp):
        c, s, pi = self.cfg, self.latest, self._pi
        found = {}
        hot = [f"{LABELS[name]} {value:.1f} C" for name, value in
               (("amp_temp", s and s.temp_c), ("case_temp", s and s.case_temp_c))
               if self.warn[name].active and value is not None]
        if hot:
            found["amp_temp_high"] = f"{', '.join(hot)}: at or above {c['temp_warn_c']:g} C (stop at {c['temp_trip_c']:g} C)"
        if self.warn["cold"].active:
            found["amp_cold"] = f"amplifier below {c['cold_warn_c']:g} C"
        if self.warn["pi_temp"].active and pi is not None and pi.soc_temp_c is not None:
            found["pi_temp_high"] = f"Pi SoC temperature {pi.soc_temp_c:.1f} C, at or above {c['pi_temp_warn_c']:g} C"
        if self.warn["pi_throttled"].active:
            found["pi_throttled"] = "the Pi is throttled" + (f": {', '.join(pi.throttled)}" if pi and pi.throttled else "")
        if inp.telemetry_gap_s is not None and inp.telemetry_gap_s > c["telemetry_gap_warn_s"]:
            found["telemetry_gaps"] = f"the telemetry loop stalled for {inp.telemetry_gap_s:.1f} s in the last minutes"
        if inp.override:
            found["override"] = ("OVERRIDE set: the radar may run without amplifier telemetry"
                                 + (f" (now: {self.telemetry_reason})" if self.telemetry in (LOST, SETTLING) else ""))
        if self.unpowered.active and c["allow_unpowered"]:
            found["unpowered_allowed"] = "amplifier 28 V absent; allowed by the setting allow_unpowered"
        if s is not None and s.case_temp_c is None and not c["require_case_temp"]:
            found["case_temp_missing"] = "no amplifier case temperature (not required by the setting require_case_temp)"
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
            return {"state": "trip" if d.active else "warn" if warned else OK, "value": r(value),
                    "since_uptime_s": r(d.since)}

        voltage = limit("voltage", s and s.voltage_v)
        if self.unpowered.active:
            voltage.update(state="unpowered" if not self.trips["voltage"].active else "trip",
                           since_uptime_s=r(self.unpowered.since))
        next_in = None
        if self.enabled in (RESETTING, STUCK) and not self.tool_running and self._next_attempt is not None:
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
                                "tool_running": self.tool_running, "next_run_in_s": next_in},
            },
            "amp": dict(self._values(), sample_uptime_s=r(s and s.uptime)),
            "pi": {"soc_temp_c": self._pi and self._pi.soc_temp_c, "throttled": list(self._pi.throttled) if self._pi else []},
            "radar": {"running": bool(self.radar_pids), "pids": list(self.radar_pids), "stop": self.stop,
                      "stop_since_uptime_s": r(self._stop_started) if self.stop != IDLE else None},
            "reset_tool": {"running": self.tool_running, "last": self.last_tool},
            "override": {"no_amp_telemetry": bool(override)},
            "limits": {"temp_warn_c": c["temp_warn_c"], "temp_trip_c": c["temp_trip_c"], "temp_clear_c": c["temp_clear_c"],
                       "voltage_min_v": c["voltage_min_v"], "voltage_max_v": c["voltage_max_v"],
                       "enabled_current_a": c["enabled_current_a"]},
            "counters": dict(self.counters),
            "loop": dict(self.loop),
        }
