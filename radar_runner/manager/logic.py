"""Decision logic of the radar manager: a pure state machine.

No I/O and no clock in here. The caller passes in what happened, with the time
(seconds on one monotonic clock), and then collects the `Action`s to carry out
with `drain()`.

Inputs
    gates(t, results)           the latest result of every gate (gates.py)
    start/stop/toggle(t, src)   a manual request; src is "button" or "command"
    interrupt(t, src, reason)   stop now, resume later (the power service's stop hook)
    clear_fault(t, src)
    auto(t, state, label)       the automatic source (the GPS motion trigger):
                                "moving", "stationary" or "unknown" (no fix)
    collection_created(t, name) from the runner: the collection directory exists
    started(t)                  from the runner: the radar reports that it records
    ended(t, info)              from the runner: the radar is gone and the amplifier
                                step is done (or no radar was ever launched)
    amp_unsafe(t, detail)       with no radar: the amplifier pin could not be made safe
    amp_safe_again(t, why)      the record says the pin is safe (again)
    shutdown(t)                 the manager itself is stopping

Actions
    start   data = {"trigger", "attempt", "resume"}       run the start sequence
    stop    data = {"kind", "reason", "urgent", "power"}  stop the radar, or abort the start.
                                                          May come again to make a stop urgent
    event   data = {...}                                  one line for the event log

States (what the LEDs show)
    not_ready   a gate refuses a start
    ready       every gate is good; waiting for a trigger
    starting    start sequence running, or waiting to retry a failed start
    running     the radar records
    stopping    the radar is exiting and the amplifier is being made safe
    fault       latched; see below
    power_fail  the power service reports a loss of main power

A run exists while there is an `intent`: who asked for it. The intent survives
failed starts and interruptions, and ends with a stop request, a fault, the
manual-run timeout, or a radar with a pulse count finishing by itself.

Rules
  * Manual sources (button, command line) are equal. `toggle` is the button:
    it cancels a run that is wanted (starting, running, waiting to retry or to
    resume) and otherwise starts one. A manual start is refused, not
    remembered, while a gate is bad; it then changes nothing, also not a fault.
  * The automatic source is a level, not an event: while it says "moving", no
    run is wanted and nothing blocks it, a run is started as soon as the gates
    allow. It stops only runs that it started, and only on "stationary";
    "unknown" changes nothing.
  * A run started or taken over manually (`start` during an automatic run) is
    not stopped by the automatic source.
  * Every manual stop, a radar that finishes by itself, and every fault block
    the automatic source until it has reported "stationary" once since then
    (at once if it already says so).
  * A failed start or a radar that exits by itself with an error is retried
    after `retry_delays_s[n]`; when the delays are used up: fault. A fatal
    failure (the config fails its limits, the turn-on check fails) is a fault
    at once.
  * An interruption stops the radar and keeps the intent: a gate that fails
    during a run, `interrupt`, or a radar that runs until stopped and exits
    with status 0 unasked. When every gate has been good again for
    `resume_holdoff_s` the run resumes, at most `max_resumes` times: then fault.
  * `stable_run_s` of uninterrupted running clears both counts. Independent of
    that, at most `max_restarts_per_hour` automatic restarts (retries and
    resumes) are made in any hour: then fault (`restart_limit`). Nothing
    resets that count but time.
  * A stop is urgent when it comes from a loss of power (the power gate,
    `interrupt`) or from the manager's own exit: the runner then has the radar
    gone within a fixed time.
  * An amplifier pin that could not be confirmed safe is a fault at once
    (`amp_unsafe`).
  * Leaving a fault:
      - `amp_unsafe` clears by itself when the record says the pin is safe
        again (`amp_safe_again`), and by `clear_fault`. Nothing else clears it.
      - Every other fault is cleared by `toggle` or `start` when the gates
        allow a start (which then begins), by `clear_fault` (clears only), or
        by the automatic source saying "moving" while it is not blocked.
      - `stop` never clears a fault: the power service calls it.
    The last fault and how it was cleared stay in the status.
"""
from dataclasses import dataclass, field

NOT_READY, READY, STARTING, RUNNING = "not_ready", "ready", "starting", "running"
STOPPING, FAULT, POWER_FAIL = "stopping", "fault", "power_fail"

IDLE = "idle"                       # phases besides STARTING, RUNNING and STOPPING
BUTTON, COMMAND, AUTO = "button", "command", "auto"
MANUAL = (BUTTON, COMMAND)
MOVING, STATIONARY, UNKNOWN = "moving", "stationary", "unknown"
AMP_UNSAFE = "amp_unsafe"

# LED patterns per state: (yellow READY, green RUNNING)
OFF, ON, FAST, BREATHE = "off", "on", "fast_blink", "breathing"
LEDS = {
    NOT_READY: (FAST, OFF),
    READY: (ON, OFF),
    STARTING: (BREATHE, OFF),
    RUNNING: (OFF, ON),
    STOPPING: (BREATHE, OFF),
    FAULT: (FAST, OFF),
    POWER_FAIL: (OFF, OFF),
}

# The smallest values the `manager.run` settings may have. Below them the
# limits on restarts mean nothing (each radar start enables the amplifier for
# about half a second), so such a value is refused and the default used.
MINIMUMS = {"retry_delay_s": 5.0, "resume_holdoff_s": 2.0, "stable_run_s": 60.0}


@dataclass(frozen=True)
class Action:
    kind: str               # "start", "stop" or "event"
    data: dict = field(default_factory=dict)


@dataclass
class GateResult:
    """Result of one gate. An empty text means "good"."""
    start: str = ""         # why a start is refused
    run: str = ""           # why a running radar must stop (implies no start)
    note: str = ""          # shown in the status; changes nothing
    kind: str = ""          # "power_fail": the power service reports a loss; "override": passed by an override

    def as_dict(self):
        d = {"ok": not (self.start or self.run), "reason": self.run or self.start}
        if self.run:
            d["stops_run"] = True
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class LogicConfig:
    retry_delays_s: tuple = (10.0, 30.0, 90.0)
    max_resumes: int = 5
    resume_holdoff_s: float = 10.0
    stable_run_s: float = 300.0
    max_restarts_per_hour: int = 12
    manual_timeout_s: float = 50400.0       # 0 = no limit
    sources: tuple = (BUTTON, COMMAND)      # enabled trigger sources; add AUTO for the motion trigger

    @classmethod
    def from_config(cls, manager_cfg, minimums=None):
        """Return (config, warnings). A setting below its minimum is refused and the default used."""
        low = MINIMUMS if minimums is None else minimums
        run, triggers = manager_cfg["run"], manager_cfg["triggers"]
        default = cls()
        warnings = []

        def refuse(name, why):
            warnings.append(f"manager.run.{name} {why}: the default is used")

        delays = run["retry_delays_s"]
        if (not delays or not all(type(d) in (int, float) and d >= low["retry_delay_s"] for d in delays)
                or len(delays) > 10):
            refuse("retry_delays_s", f"must be 1 to 10 delays of at least {low['retry_delay_s']:g} s")
            delays = default.retry_delays_s
        holdoff = run["resume_holdoff_s"]
        if holdoff < low["resume_holdoff_s"]:
            refuse("resume_holdoff_s", f"must be at least {low['resume_holdoff_s']:g} s")
            holdoff = default.resume_holdoff_s
        stable = run["stable_run_s"]
        if stable < low["stable_run_s"]:
            refuse("stable_run_s", f"must be at least {low['stable_run_s']:g} s")
            stable = default.stable_run_s
        resumes = run["max_resumes"]
        if not 0 <= resumes <= 100:
            refuse("max_resumes", "must be between 0 and 100")
            resumes = default.max_resumes
        per_hour = run["max_restarts_per_hour"]
        if not 1 <= per_hour <= 120:
            refuse("max_restarts_per_hour", "must be between 1 and 120")
            per_hour = default.max_restarts_per_hour
        timeout = run["manual_timeout_s"]
        if timeout < 0:
            refuse("manual_timeout_s", "must not be negative")
            timeout = default.manual_timeout_s
        sources = tuple(s for s, name in ((BUTTON, "button"), (COMMAND, "command"), (AUTO, "gps"))
                        if triggers[name])
        return cls(retry_delays_s=tuple(float(d) for d in delays), max_resumes=resumes,
                   resume_holdoff_s=float(holdoff), stable_run_s=float(stable),
                   max_restarts_per_hour=per_hour, manual_timeout_s=float(timeout), sources=sources), warnings


class ManagerLogic:
    def __init__(self, cfg=None):
        self.cfg = cfg or LogicConfig()
        self.phase = IDLE
        self.intent = None          # None, or {"source", "label", "since"}
        self.fault = None           # None, or {"kind", "reason", "since"}
        self.last_fault = None      # the last fault, also after it was cleared: {"kind", "reason", "cleared_by"}
        self.failures = 0           # failed starts and crashes since the last stable run
        self.interruptions = 0      # interruptions since the last stable run
        self.restarts = []          # times of the automatic restarts of the last hour
        self.retry_at = None        # earliest time of the next start after a failure or an interruption
        self.resume = False         # the next start continues an interrupted run
        self.stop_kind = ""         # why the current stop was asked for
        self.stop_reason = ""
        self.stop_urgent = False
        self.last_end = ""          # how the last run ended, for the status
        self.auto_state = UNKNOWN
        self.auto_label = "gps"
        self.auto_blocked = False
        self.closing = False
        self.runs_ended = 0         # counts every "ended": lets a waiter see a stop that was followed by a restart
        self.results = None         # name -> GateResult; None until the first evaluation
        self.gates_good_since = None
        self.run_since = None       # start of the current stretch of running
        self.collection = None
        self.t = 0.0
        self._out = []
        self._state = NOT_READY
        self._gate_ok = {}

    # ------------------------------------------------------------------ helpers

    def drain(self):
        out, self._out = self._out, []
        return out

    def _event(self, name, **data):
        self._out.append(Action("event", {"event": name, **data}))

    def _start_block(self):
        """Why a start is refused right now, or ""."""
        if self.results is None:
            return "gates not evaluated yet"
        for name, r in self.results.items():
            if r.run or r.start:
                return f"{name}: {r.run or r.start}"
        return ""

    def _run_block(self):
        """(why a running radar must stop, is it a loss of power), or ("", False)."""
        if self.results is None:
            return "", False
        for name, r in self.results.items():
            if r.run:
                return f"{name}: {r.run}", r.kind == "power_fail"
        return "", False

    def not_ready_reasons(self):
        if self.results is None:
            return ["gates not evaluated yet"]
        return [f"{name}: {r.run or r.start}" for name, r in self.results.items() if r.run or r.start]

    @property
    def power_fail(self):
        return bool(self.results) and any(r.kind == "power_fail" for r in self.results.values())

    @property
    def state(self):
        if self.phase != IDLE:
            return self.phase
        if self.power_fail:
            return POWER_FAIL
        if self.fault:
            return FAULT
        if self._start_block():
            return NOT_READY
        if self.intent:
            return STARTING         # waiting to retry or to resume
        return READY

    def _block_auto(self):
        self.auto_blocked = self.auto_state != STATIONARY

    def _set_fault(self, t, kind, reason):
        self.fault = {"kind": kind, "reason": reason, "since": t}
        self.last_fault = {"kind": kind, "reason": reason, "cleared_by": None}
        self.intent = None
        self.retry_at = None
        self.resume = False
        self._block_auto()
        self._event("fault", kind=kind, reason=reason)

    def _clear_fault(self, why):
        if self.fault:
            self._event("fault_cleared", by=why, kind=self.fault["kind"], reason=self.fault["reason"])
            if self.last_fault:
                self.last_fault["cleared_by"] = why
            self.fault = None
            self.failures = 0
            self.interruptions = 0

    def _new_intent(self, t, source, label=None):
        self.intent = {"source": source, "label": label or source, "since": t}
        self.failures = 0
        self.interruptions = 0
        self.retry_at = None
        self.resume = False

    def _drop_intent(self):
        self.intent = None
        self.retry_at = None
        self.resume = False

    def _start(self, t):
        restart = self.failures > 0 or self.resume
        if restart:
            self.restarts = [x for x in self.restarts if t - x < 3600.0]
            if len(self.restarts) >= self.cfg.max_restarts_per_hour:
                self._set_fault(t, "restart_limit", f"{len(self.restarts)} automatic restarts within an hour "
                                f"(limit {self.cfg.max_restarts_per_hour}); last end: {self.last_end}")
                return
            self.restarts.append(t)
        self.phase = STARTING
        self.collection = None
        data = {"trigger": self.intent["label"], "attempt": self.failures + 1, "resume": self.resume}
        self.resume = False
        self.retry_at = None
        self._event("start", **data)
        self._out.append(Action("start", data))

    def _stop(self, t, kind, reason, urgent=False, power=False):
        """Ask the runner to stop. Called again, it can only make the stop urgent."""
        first = self.phase != STOPPING
        self.phase = STOPPING
        if first:
            self.stop_kind, self.stop_reason, self.stop_urgent = kind, reason, False
            self._event("stop", kind=kind, reason=reason, urgent=urgent)
        if first or (urgent and not self.stop_urgent):
            self.stop_urgent = self.stop_urgent or urgent
            self._out.append(Action("stop", {"kind": kind, "reason": reason, "urgent": urgent, "power": power}))

    def _interrupted(self, t, reason):
        """A run ended that is still wanted: resume later, a limited number of times."""
        self.interruptions += 1
        if self.interruptions > self.cfg.max_resumes:
            self._set_fault(t, "resume_limit", f"interrupted {self.interruptions} times "
                            f"(limit {self.cfg.max_resumes}); last: {reason}")
        else:
            self.resume = True
            # Never straight back on: `radarctl stop --wait` (the power
            # service's hook) must be able to see the radar off.
            self.retry_at = t + self.cfg.resume_holdoff_s

    def _finish(self, t):
        """Decide what follows from the present situation, and report a state change."""
        self.t = t
        cfg = self.cfg
        if self.phase in (STARTING, RUNNING):
            intent = self.intent or {"source": None, "since": t}
            block, power = self._run_block()
            if block:
                self._stop(t, "gate", block, urgent=power, power=power)
            elif (intent["source"] in MANUAL and cfg.manual_timeout_s
                  and t - intent["since"] >= cfg.manual_timeout_s):
                self._drop_intent()
                self._stop(t, "timeout", f"manual run limit of {cfg.manual_timeout_s:.0f} s reached")
            elif intent["source"] == AUTO and self.auto_state == STATIONARY:
                self._drop_intent()
                self._stop(t, "auto", f"{self.auto_label}: stationary")
            elif (self.phase == RUNNING and (self.failures or self.interruptions)
                  and t - self.run_since >= cfg.stable_run_s):
                self.failures = 0
                self.interruptions = 0
        elif self.phase == STOPPING:
            if self.power_fail and not self.stop_urgent:    # power went while a stop was under way
                self._stop(t, self.stop_kind, self.stop_reason, urgent=True, power=True)
        elif not self.closing:
            auto_go = AUTO in cfg.sources and self.auto_state == MOVING and not self.auto_blocked
            if self.fault and auto_go and self.fault["kind"] != AMP_UNSAFE:
                self._clear_fault(self.auto_label)
            if not self.fault:
                intent = self.intent
                if intent is None and auto_go:
                    self._new_intent(t, AUTO, self.auto_label)
                elif intent and intent["source"] == AUTO and self.auto_state == STATIONARY:
                    self._drop_intent()
                elif (intent and intent["source"] in MANUAL and cfg.manual_timeout_s
                      and t - intent["since"] >= cfg.manual_timeout_s):
                    self._drop_intent()
                    self._event("intent_expired", reason="manual run limit reached while not running")
                if self.intent and not self._start_block():
                    waited = self.retry_at is None or t >= self.retry_at
                    settled = (not self.resume or
                               t - self.gates_good_since >= cfg.resume_holdoff_s)
                    if waited and settled:
                        self._start(t)
        new = self.state
        if new != self._state:
            self._event("state", old=self._state, new=new,
                        reason=(self.fault or {}).get("reason", "") if new == FAULT else
                        "; ".join(self.not_ready_reasons()) if new in (NOT_READY, POWER_FAIL) else "")
            self._state = new

    # ------------------------------------------------------------------- inputs

    def gates(self, t, results):
        """Feed the result of every gate: a dict name -> GateResult, in display order."""
        for name, r in results.items():
            ok = not (r.start or r.run)
            if self._gate_ok.get(name) != ok:
                if name in self._gate_ok or not ok:        # not the first "good" after start-up
                    self._event("gate", gate=name, ok=ok, reason=r.run or r.start)
                self._gate_ok[name] = ok
        self.results = dict(results)
        if any(r.start or r.run for r in results.values()):
            self.gates_good_since = None
        elif self.gates_good_since is None:
            self.gates_good_since = t
        self._finish(t)

    def tick(self, t):
        """Let time pass (retry delays, hold-off, timeouts) without new gate results."""
        self._finish(t)

    def start(self, t, source):
        """A manual start request. Returns (accepted, message)."""
        reply = self._manual_start(t, source)
        self._event("request", request="start", source=source, accepted=reply[0], message=reply[1])
        self._finish(t)
        return reply

    def _manual_start(self, t, source):
        if source not in self.cfg.sources:
            return False, f"the {source} trigger is disabled"
        if self.closing:
            return False, "the manager is stopping"
        if self.intent:
            if self.intent["source"] == AUTO:
                self.intent.update(source=source, label=source, since=t)
                return True, "taken over: now a manual run"
            return True, f"already {self.state}"
        if self.phase == STOPPING:
            return False, "the radar is stopping; try again when ready"
        if self.fault and self.fault["kind"] == AMP_UNSAFE:
            return False, (f"fault: {self.fault['reason']} (it clears when the amplifier pin is confirmed safe, "
                           "or with clear-fault)")
        block = self._start_block()
        if block:                       # nothing changes: a fault stays, with its reason
            return False, f"not ready: {block}" + (f" (fault kept: {self.fault['reason']})" if self.fault else "")
        if self.fault:
            self._clear_fault(source)
        self._new_intent(t, source)
        return True, "starting"

    def stop(self, t, source):
        """A manual stop request. Always accepted. Returns (True, message)."""
        reply = self._manual_stop(t, source)
        self._event("request", request="stop", source=source, accepted=True, message=reply[1])
        self._finish(t)
        return reply

    def _manual_stop(self, t, source):
        had_intent = self.intent is not None
        self._drop_intent()
        if had_intent or self.phase != IDLE:
            self._block_auto()
        if self.phase in (STARTING, RUNNING):
            self._stop(t, "manual", f"stop by {source}")
            return True, "stopping"
        if self.phase == STOPPING:
            self.stop_kind = "manual"       # an interruption must not resume afterwards
            return True, "already stopping"
        return True, "start cancelled" if had_intent else "not running"

    def interrupt(self, t, source, reason):
        """Stop now, urgently, but keep the run wanted (the power service's stop hook).

        The run resumes once every gate has been good for the hold-off time, counted from now.
        """
        if self.phase in (STARTING, RUNNING):
            self._stop(t, "gate", reason, urgent=True, power=True)
            message = "stopping"
        elif self.phase == STOPPING:
            self._stop(t, self.stop_kind, self.stop_reason, urgent=True, power=True)
            message = "already stopping"
        else:
            message = "not running"
        if self.gates_good_since is not None:
            self.gates_good_since = t
        self._event("request", request="interrupt", source=source, accepted=True, message=message)
        self._finish(t)
        return True, message

    def toggle(self, t, source):
        """The button: cancel a wanted run, else start one. Returns (accepted, message)."""
        if source not in self.cfg.sources:
            reply = (False, f"the {source} trigger is disabled")
            self._event("request", request="toggle", source=source, accepted=False, message=reply[1])
            return reply
        if self.intent or self.phase in (STARTING, RUNNING):
            return self.stop(t, source)
        if self.phase == STOPPING:
            reply = (False, "the radar is stopping; ignored")
            self._event("request", request="toggle", source=source, accepted=False, message=reply[1])
            return reply
        return self.start(t, source)

    def clear_fault(self, t, source):
        if not self.fault:
            return True, "no fault"
        self._clear_fault(source)
        self._finish(t)
        return True, "fault cleared"

    def auto(self, t, state, label="gps"):
        """The automatic source reports "moving", "stationary" or "unknown"."""
        if state not in (MOVING, STATIONARY, UNKNOWN):
            state = UNKNOWN
        self.auto_label = label
        if state != self.auto_state:
            self._event("auto", source=label, state=state)
        self.auto_state = state
        if state == STATIONARY:
            self.auto_blocked = False
        self._finish(t)

    def amp_unsafe(self, t, detail):
        """With no radar running, the amplifier pin could not be made safe."""
        if self.phase == IDLE and (self.fault or {}).get("kind") != AMP_UNSAFE:
            self._set_fault(t, AMP_UNSAFE, f"the amplifier could not be confirmed safe: {detail}")
        self._finish(t)

    def amp_safe_again(self, t, why):
        """The record says the amplifier pin is safe: an `amp_unsafe` fault is over."""
        if self.fault and self.fault["kind"] == AMP_UNSAFE:
            self._clear_fault(f"the amplifier pin is known safe again: {why}")
        self._finish(t)

    def collection_created(self, t, name):
        self.collection = name

    def started(self, t):
        if self.phase == STARTING:
            self.phase = RUNNING
            self.run_since = t
        self._finish(t)

    def ended(self, t, info):
        """The run is over: no radar process, and the amplifier step is done.

        info: requested    the runner was asked to stop (or to abort the start)
              failure      "" or why the start failed / the radar exited with an error
              fatal        a retry cannot help (config limits, turn-on check)
              kind         short name of the failure, used as the fault kind when fatal
              interrupted  "" or why a radar that runs until stopped is gone, unasked, with status 0
              amp_safe     True: the pin is known safe; False: it is not;
                           None: not checked (power loss) or no radar was launched
              amp_safe_detail
        """
        if self.phase == IDLE:
            return
        was = self.phase
        self.phase = IDLE
        self.runs_ended += 1
        self.run_since = None
        cfg = self.cfg
        failure = info.get("failure", "")
        if info.get("amp_safe") is False:
            self.last_end = "amplifier not confirmed safe"
            self._set_fault(t, AMP_UNSAFE, "the amplifier could not be confirmed safe after the radar "
                            f"exited: {info.get('amp_safe_detail', '')}".rstrip(": "))
        elif was == STOPPING and info.get("requested", True):
            self.last_end = f"stopped ({self.stop_kind}: {self.stop_reason})"
            if self.stop_kind == "gate" and self.intent:
                self._interrupted(t, self.stop_reason)
        elif failure and info.get("fatal"):
            self.last_end = f"failed: {failure}"
            self._set_fault(t, info.get("kind") or "start", failure)
        elif failure:
            self.last_end = f"failed: {failure}"
            self.failures += 1
            if self.intent is None:
                pass                                        # stopped meanwhile: nothing to retry
            elif self.failures > len(cfg.retry_delays_s):
                self._set_fault(t, "start_failures", f"{self.failures} failed starts in a row; last: {failure}")
            else:
                delay = cfg.retry_delays_s[self.failures - 1]
                self.retry_at = t + delay
                self._event("retry", attempt=self.failures + 1, delay_s=delay, after=failure)
        elif info.get("interrupted"):
            self.last_end = f"interrupted: {info['interrupted']}"
            if self.intent:
                self._interrupted(t, info["interrupted"])
        else:
            self.last_end = "the radar finished by itself"
            if self.intent:
                self._drop_intent()
                self._block_auto()
        self.stop_kind = self.stop_reason = ""
        self.stop_urgent = False
        self._finish(t)

    def shutdown(self, t):
        """The manager is stopping: stop the radar, urgently, and start nothing new."""
        self.closing = True
        self._drop_intent()
        if self.phase in (STARTING, RUNNING):
            self._stop(t, "shutdown", "the manager is stopping", urgent=True)
        elif self.phase == STOPPING:
            self._stop(t, "shutdown", "the manager is stopping", urgent=True)
            self.stop_kind = "shutdown"
        self._finish(t)

    # ------------------------------------------------------------------- output

    def snapshot(self):
        """State for the status file (plain JSON types)."""
        t = self.t
        state = self.state
        yellow, green = LEDS[state]
        intent = self.intent
        if state == NOT_READY and intent:
            detail = "will resume when ready" if self.resume else "will start when ready"
        elif state == STARTING and self.phase == IDLE:
            wait = max(0.0, (self.retry_at or t) - t)
            detail = (f"{'resume' if self.resume else 'retry'} in {wait:.0f} s" if wait
                      else "waiting for the gates to settle")
        else:
            detail = ""
        return {
            "state": state,
            "detail": detail,
            "trigger": intent["label"] if intent else None,
            "run_wanted": intent is not None,
            "run_wanted_for_s": round(t - intent["since"], 1) if intent else None,
            "not_ready": self.not_ready_reasons() if (self.results is None or self._start_block()) else [],
            "fault": ({"kind": self.fault["kind"], "reason": self.fault["reason"],
                       "age_s": round(t - self.fault["since"], 1)} if self.fault else None),
            "last_fault": dict(self.last_fault) if self.last_fault else None,
            "collection": self.collection if self.phase != IDLE else None,
            "last_end": self.last_end,
            "runs_ended": self.runs_ended,
            "retries": {"failed_starts": self.failures, "max_retries": len(self.cfg.retry_delays_s),
                        "retry_in_s": (round(max(0.0, self.retry_at - t), 1)
                                       if self.retry_at is not None else None),
                        "interruptions": self.interruptions, "max_resumes": self.cfg.max_resumes,
                        "restarts_last_hour": len([x for x in self.restarts if t - x < 3600.0]),
                        "max_restarts_per_hour": self.cfg.max_restarts_per_hour},
            "auto": {"enabled": AUTO in self.cfg.sources, "source": self.auto_label,
                     "state": self.auto_state, "blocked": self.auto_blocked},
            "gates": {name: r.as_dict() for name, r in (self.results or {}).items()},
            "leds": {"ready_yellow": yellow, "running_green": green},
        }
