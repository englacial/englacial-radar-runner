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
    collection(t, name, ...)    from the runner: the collection directory exists
    started(t)                  from the runner: the radar reports that it records
    ended(t, info)              from the runner: the radar is gone and the amplifier
                                has been made safe (or no radar was ever launched)
    shutdown(t)                 the manager itself is stopping

Actions
    start   data = {"trigger", "attempt", "resume"}   run the start sequence
    stop    data = {"kind", "reason"}                 stop the radar, or abort the start
    event   data = {...}                              one line for the event log

States (what the LEDs show)
    not_ready   a gate refuses a start
    ready       every gate is good; waiting for a trigger
    starting    start sequence running, or waiting to retry a failed start
    running     the radar records
    stopping    the radar is exiting and the amplifier is being made safe
    fault       latched; see below
    power_fail  the power service reports a loss of main power

A run exists while there is an `intent`: who asked for it. The intent survives
failed starts and interruptions by a gate, and ends with a stop request, a
fault, the manual-run timeout, or the radar finishing by itself.

Rules
  * Manual sources (button, command line) are equal. `toggle` is the button:
    it cancels a run that is wanted (starting, running, waiting to retry or to
    resume) and otherwise starts one. A manual start is refused, not
    remembered, while a gate is bad.
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
  * A gate that fails during a run (or `interrupt`) stops the radar and keeps the intent. When
    every gate has been good again for `resume_holdoff_s` the run resumes, at
    most `max_resumes` times: then fault.
  * `stable_run_s` of uninterrupted running clears both counts.
  * A failed "make the amplifier safe" command is a fault at once.
  * Fault is left by: `toggle` or `start` (clears it and starts if the gates
    allow), `clear_fault` (clears only), or the automatic source saying
    "moving" while it is not blocked (clears it and starts). `stop` never
    clears a fault: the power service calls it.
"""
from dataclasses import dataclass, field

NOT_READY, READY, STARTING, RUNNING = "not_ready", "ready", "starting", "running"
STOPPING, FAULT, POWER_FAIL = "stopping", "fault", "power_fail"

IDLE = "idle"                       # phases besides STARTING, RUNNING and STOPPING
BUTTON, COMMAND, AUTO = "button", "command", "auto"
MANUAL = (BUTTON, COMMAND)
MOVING, STATIONARY, UNKNOWN = "moving", "stationary", "unknown"

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
    manual_timeout_s: float = 50400.0       # 0 = no limit
    sources: tuple = (BUTTON, COMMAND)      # enabled trigger sources; add AUTO for the motion trigger

    @classmethod
    def from_config(cls, manager_cfg):
        run, triggers = manager_cfg["run"], manager_cfg["triggers"]
        delays = tuple(float(d) for d in run["retry_delays_s"]
                       if type(d) in (int, float) and d >= 0)
        sources = tuple(s for s, name in ((BUTTON, "button"), (COMMAND, "command"), (AUTO, "gps"))
                        if triggers[name])
        return cls(retry_delays_s=delays, max_resumes=max(0, run["max_resumes"]),
                   resume_holdoff_s=max(0.0, run["resume_holdoff_s"]),
                   stable_run_s=max(0.0, run["stable_run_s"]),
                   manual_timeout_s=max(0.0, run["manual_timeout_s"]), sources=sources)


class ManagerLogic:
    def __init__(self, cfg=None):
        self.cfg = cfg or LogicConfig()
        self.phase = IDLE
        self.intent = None          # None, or {"source", "label", "since"}
        self.fault = None           # None, or {"kind", "reason", "since"}
        self.failures = 0           # failed starts and crashes since the last stable run
        self.interruptions = 0      # stops by a gate since the last stable run
        self.retry_at = None        # earliest time of the next start after a failure
        self.resume = False         # the next start continues an interrupted run
        self.stop_kind = ""         # why the current stop was asked for
        self.stop_reason = ""
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
        if self.results is None:
            return ""
        for name, r in self.results.items():
            if r.run:
                return f"{name}: {r.run}"
        return ""

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
        self.intent = None
        self.retry_at = None
        self.resume = False
        self._block_auto()
        self._event("fault", kind=kind, reason=reason)

    def _clear_fault(self, why):
        if self.fault:
            self._event("fault_cleared", by=why, kind=self.fault["kind"])
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
        self.phase = STARTING
        self.collection = None
        data = {"trigger": self.intent["label"], "attempt": self.failures + 1, "resume": self.resume}
        self.resume = False
        self.retry_at = None
        self._event("start", **data)
        self._out.append(Action("start", data))

    def _stop(self, t, kind, reason):
        """Ask the runner to stop. Safe to call again: a later reason replaces the kind only."""
        first = self.phase != STOPPING
        self.phase = STOPPING
        self.stop_kind, self.stop_reason = kind, reason
        if first:
            self._event("stop", kind=kind, reason=reason)
            self._out.append(Action("stop", {"kind": kind, "reason": reason}))

    def _finish(self, t):
        """Decide what follows from the present situation, and report a state change."""
        self.t = t
        cfg = self.cfg
        if self.phase in (STARTING, RUNNING):
            intent = self.intent or {"source": None, "since": t}
            block = self._run_block()
            if block:
                self._stop(t, "gate", block)
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
        elif self.phase == IDLE and not self.closing:
            auto_go = AUTO in cfg.sources and self.auto_state == MOVING and not self.auto_blocked
            if self.fault and auto_go:
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
        if self.fault:
            self._clear_fault(source)
        if self.intent:
            if self.intent["source"] == AUTO:
                self.intent.update(source=source, label=source, since=t)
                return True, "taken over: now a manual run"
            return True, f"already {self.state}"
        if self.phase == STOPPING:
            return False, "the radar is stopping; try again when ready"
        block = self._start_block()
        if block:
            return False, f"not ready: {block}"
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
            self.stop_kind = "manual"       # a gate-caused stop must not resume afterwards
            return True, "already stopping"
        return True, "start cancelled" if had_intent else "not running"

    def interrupt(self, t, source, reason):
        """Stop now but keep the run wanted, as a failing gate does (the power service's stop hook).

        The run resumes once every gate has been good for the hold-off time, counted from now.
        """
        if self.phase in (STARTING, RUNNING):
            self._stop(t, "gate", reason)
            message = "stopping"
        else:
            message = "already stopping" if self.phase == STOPPING else "not running"
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

    def collection_created(self, t, name):
        self.collection = name

    def started(self, t):
        if self.phase == STARTING:
            self.phase = RUNNING
            self.run_since = t
        self._finish(t)

    def ended(self, t, info):
        """The run is over: no radar process, amplifier made safe.

        info: requested   the runner was asked to stop (or to abort the start)
              failure     "" or why the start failed / the radar exited with an error
              fatal       a retry cannot help (config limits, turn-on check)
              kind        short name of the failure, used as the fault kind when fatal
              amp_safe    True, False, or None when no radar process was launched
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
            self._set_fault(t, "amp_unsafe", "the amplifier could not be confirmed safe after the radar "
                            f"exited: {info.get('amp_safe_detail', '')}".rstrip(": "))
        elif was == STOPPING and info.get("requested", True):
            self.last_end = f"stopped ({self.stop_kind}: {self.stop_reason})"
            if self.stop_kind == "gate" and self.intent:
                self.interruptions += 1
                if self.interruptions > cfg.max_resumes:
                    self._set_fault(t, "resume_limit", f"stopped by a gate {self.interruptions} times "
                                    f"(limit {cfg.max_resumes}); last: {self.stop_reason}")
                else:
                    self.resume = True
                    # Never straight back on: `radarctl stop --wait` (the power
                    # service's hook) must be able to see the radar off.
                    self.retry_at = t + cfg.resume_holdoff_s
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
        else:
            self.last_end = "the radar finished by itself"
            if self.intent:
                self._drop_intent()
                self._block_auto()
        self.stop_kind = self.stop_reason = ""
        self._finish(t)

    def shutdown(self, t):
        """The manager is stopping: stop the radar and start nothing new."""
        self.closing = True
        self._drop_intent()
        if self.phase in (STARTING, RUNNING):
            self._stop(t, "shutdown", "the manager is stopping")
        elif self.phase == STOPPING:
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
            "collection": self.collection if self.phase != IDLE else None,
            "last_end": self.last_end,
            "runs_ended": self.runs_ended,
            "retries": {"failed_starts": self.failures, "max_retries": len(self.cfg.retry_delays_s),
                        "retry_in_s": (round(max(0.0, self.retry_at - t), 1)
                                       if self.retry_at is not None else None),
                        "interruptions": self.interruptions, "max_resumes": self.cfg.max_resumes},
            "auto": {"enabled": AUTO in self.cfg.sources, "source": self.auto_label,
                     "state": self.auto_state, "blocked": self.auto_blocked},
            "gates": {name: r.as_dict() for name, r in (self.results or {}).items()},
            "leds": {"ready_yellow": yellow, "running_green": green},
        }
