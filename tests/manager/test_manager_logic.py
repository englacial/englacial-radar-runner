"""The manager's state machine (radar_runner/manager/logic.py), in simulated time.

Pure logic: no processes, no files, no clock. Each test feeds inputs and
checks the actions ("start", "stop") and the state that the LEDs would show.
"""
import json

import pytest

from radar_runner.manager import logic as L
from radar_runner.manager.logic import GateResult, LogicConfig, ManagerLogic

GATES = ("telemetry", "watcher", "power", "data", "sdr", "config", "orca", "amp_safe")
OK = {"requested": True, "failure": "", "amp_safe": True}                 # a stop that went well
ABORTED = {"requested": True, "failure": "", "amp_safe": None}            # stopped before a radar existed


def failed(text="the radar exited with status 1", **more):
    return {"requested": False, "failure": text, "kind": "exit", "amp_safe": True, **more}


class Sim:
    """A logic with a clock and the gates, and a record of every action."""

    def __init__(self, sources=(L.BUTTON, L.COMMAND), **cfg):
        cfg.setdefault("retry_delays_s", (10.0, 30.0, 90.0))
        cfg.setdefault("max_resumes", 2)
        cfg.setdefault("resume_holdoff_s", 10.0)
        cfg.setdefault("stable_run_s", 300.0)
        cfg.setdefault("manual_timeout_s", 0.0)
        cfg.setdefault("max_restarts_per_hour", 1000)       # its own tests set it
        self.logic = ManagerLogic(LogicConfig(sources=tuple(sources), **cfg))
        self.t = 0.0
        self.bad = {}           # gate name -> GateResult
        self.actions = []
        self.events = []

    def _collect(self):
        for action in self.logic.drain():
            if action.kind == "event":
                self.events.append(action.data)
            else:
                self.actions.append(action)

    def take(self):
        """The start/stop actions since the last call, as [(kind, data)]."""
        self._collect()
        # "urgent" and "power" are shown only when set, so that an ordinary stop reads as before.
        out = [(a.kind, {k: v for k, v in a.data.items() if not (k in ("urgent", "power") and v is False)})
               for a in self.actions]
        self.actions = []
        return out

    def kinds(self):
        return [kind for kind, _ in self.take()]

    def gates(self, **bad):
        """Feed gate results. gates(telemetry="lost") fails start and run; a GateResult is used as it is."""
        self.bad = {name: (r if isinstance(r, GateResult) else GateResult(start=r, run=r))
                    for name, r in bad.items()}
        self.logic.gates(self.t, {name: self.bad.get(name, GateResult()) for name in GATES})
        self._collect()
        return self

    def advance(self, seconds, step=1.0):
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.t = min(end, self.t + step)
            self.logic.gates(self.t, {name: self.bad.get(name, GateResult()) for name in GATES})
        self._collect()
        return self

    def call(self, name, *args):
        result = getattr(self.logic, name)(self.t, *args)
        self._collect()
        return result

    def running(self, source=L.BUTTON):
        """Bring a fresh logic to RUNNING through a manual start."""
        self.gates()
        assert self.call("start", source)[0]
        assert self.kinds() == ["start"]
        self.call("started")
        assert self.state == L.RUNNING
        return self

    @property
    def state(self):
        return self.logic.state


# ------------------------------------------------------------------ states and LEDs

def test_led_patterns_match_the_plan_table():
    assert L.LEDS == {
        L.NOT_READY: ("fast_blink", "off"),
        L.READY: ("on", "off"),
        L.STARTING: ("breathing", "off"),
        L.RUNNING: ("off", "on"),
        L.STOPPING: ("breathing", "off"),
        L.FAULT: ("fast_blink", "off"),
        L.POWER_FAIL: ("off", "off"),
    }


def test_not_ready_until_the_gates_have_been_evaluated_and_are_good():
    s = Sim()
    assert s.state == L.NOT_READY and s.logic.not_ready_reasons() == ["gates not evaluated yet"]
    assert s.call("start", L.BUTTON) == (False, "not ready: gates not evaluated yet")
    s.gates(sdr=GateResult(start="SDR not found on USB"))
    assert s.state == L.NOT_READY and s.logic.snapshot()["not_ready"] == ["sdr: SDR not found on USB"]
    s.gates()
    assert s.state == L.READY and s.logic.snapshot()["not_ready"] == []
    assert s.take() == []


def test_the_whole_cycle_by_button():
    s = Sim().gates()
    assert s.call("toggle", L.BUTTON) == (True, "starting")
    assert s.take() == [("start", {"trigger": "button", "attempt": 1, "resume": False})]
    assert s.state == L.STARTING
    s.call("collection_created", "b00001_s001_x")
    s.call("started")
    assert s.state == L.RUNNING and s.logic.snapshot()["collection"] == "b00001_s001_x"
    assert s.logic.snapshot()["trigger"] == "button"
    assert s.call("toggle", L.BUTTON) == (True, "stopping")
    assert s.take() == [("stop", {"kind": "manual", "reason": "stop by button"})]
    assert s.state == L.STOPPING
    s.call("ended", OK)
    assert s.state == L.READY and s.take() == [] and s.logic.snapshot()["collection"] is None
    states = [(e["old"], e["new"]) for e in s.events if e["event"] == "state"]
    assert states == [("not_ready", "ready"), ("ready", "starting"), ("starting", "running"),
                      ("running", "stopping"), ("stopping", "ready")]


def test_button_and_command_line_behave_the_same():
    for source in (L.BUTTON, L.COMMAND):
        s = Sim().gates()
        assert s.call("start", source) == (True, "starting")
        assert s.take() == [("start", {"trigger": source, "attempt": 1, "resume": False})]
        s.call("started")
        other = L.COMMAND if source == L.BUTTON else L.BUTTON
        assert s.call("stop", other) == (True, "stopping")          # either one stops the other's run
        assert s.kinds() == ["stop"]


def test_a_manual_start_while_not_ready_is_refused_and_not_remembered():
    s = Sim().gates(telemetry="amplifier telemetry: status missing")
    ok, message = s.call("toggle", L.BUTTON)
    assert not ok and message == "not ready: telemetry: amplifier telemetry: status missing"
    s.gates().advance(60)
    assert s.state == L.READY and s.take() == []


def test_a_disabled_source_is_refused_but_stop_always_works():
    s = Sim(sources=(L.BUTTON,)).gates()
    assert s.call("start", L.COMMAND) == (False, "the command trigger is disabled")
    assert s.call("toggle", L.COMMAND) == (False, "the command trigger is disabled")
    assert s.call("toggle", L.BUTTON)[0] and s.kinds() == ["start"]
    assert s.call("stop", L.COMMAND) == (True, "stopping") and s.kinds() == ["stop"]


def test_stop_is_harmless_when_nothing_runs():
    s = Sim().gates()
    assert s.call("stop", L.COMMAND) == (True, "not running")
    assert s.take() == [] and s.state == L.READY and not s.logic.auto_blocked


def test_a_press_while_stopping_is_ignored():
    s = Sim().running()
    s.call("stop", L.COMMAND)
    s.take()
    assert s.call("toggle", L.BUTTON) == (False, "the radar is stopping; ignored")
    assert s.call("start", L.COMMAND) == (False, "the radar is stopping; try again when ready")
    s.call("ended", OK)
    assert s.state == L.READY and s.take() == []


def test_started_after_a_stop_request_does_not_bring_it_back_to_running():
    s = Sim().gates()
    s.call("start", L.BUTTON)
    s.call("stop", L.BUTTON)
    s.call("started")
    assert s.state == L.STOPPING
    s.call("ended", ABORTED)
    assert s.state == L.READY and s.kinds() == ["start", "stop"]


# ------------------------------------------------------------- retries of a failed start

def test_failed_start_is_retried_with_growing_delays_then_fault():
    s = Sim().gates()
    s.call("start", L.BUTTON)
    starts = [s.t]
    for delay in (10.0, 30.0, 90.0):
        assert s.kinds() == ["start"]
        s.call("ended", failed("build failed (status 2)"))
        assert s.state == L.STARTING                       # breathing while it waits to retry
        assert s.logic.snapshot()["detail"] == f"retry in {delay:.0f} s"
        s.advance(delay - 1)
        assert s.take() == []                              # not a second earlier
        s.advance(1)
        starts.append(s.t)
    assert [b - a for a, b in zip(starts, starts[1:])] == [10.0, 30.0, 90.0]
    assert s.take() == [("start", {"trigger": "button", "attempt": 4, "resume": False})]
    s.call("ended", failed("build failed (status 2)"))
    assert s.state == L.FAULT
    fault = s.logic.snapshot()["fault"]
    assert fault["kind"] == "start_failures" and "4 failed starts in a row" in fault["reason"]
    assert "build failed (status 2)" in fault["reason"]
    s.advance(600)
    assert s.take() == [] and s.state == L.FAULT           # stays there: no more amplifier enables


def test_radar_that_crashes_mid_run_is_retried_and_counts_like_a_failed_start():
    s = Sim().running()
    s.call("ended", failed("the radar exited with status 3"))
    assert s.state == L.STARTING and s.logic.failures == 1
    s.advance(10)
    assert s.kinds() == ["start"]
    s.call("started")
    s.call("ended", failed())
    s.advance(30)
    s.call("started")
    s.call("ended", failed())
    s.advance(90)
    s.call("started")
    s.call("ended", failed())
    assert s.state == L.FAULT and s.logic.fault["kind"] == "start_failures"


def test_a_stable_run_clears_the_failure_count():
    s = Sim(stable_run_s=300.0).running()
    s.call("ended", failed())
    s.advance(10)
    s.call("started")
    assert s.logic.failures == 1
    s.advance(299)
    assert s.logic.failures == 1
    s.advance(1)
    assert s.logic.failures == 0
    s.call("ended", failed())
    assert s.logic.snapshot()["detail"] == "retry in 10 s"     # back to the first delay


def test_retry_waits_for_the_gates():
    s = Sim().gates()
    s.call("start", L.BUTTON)
    s.call("ended", failed())
    s.gates(sdr=GateResult(start="SDR not found on USB")).advance(60)
    assert s.kinds() == ["start"] and s.state == L.NOT_READY
    assert s.logic.snapshot()["detail"] == "will start when ready"
    s.gates()
    assert s.kinds() == ["start"]


def test_a_press_cancels_a_pending_retry():
    s = Sim().gates()
    s.call("start", L.BUTTON)
    s.call("ended", failed())
    s.take()
    assert s.call("toggle", L.BUTTON) == (True, "start cancelled")
    s.advance(120)
    assert s.take() == [] and s.state == L.READY


def test_fatal_failure_is_a_fault_at_once():
    s = Sim().gates()
    s.call("start", L.BUTTON)
    s.call("ended", {"requested": False, "failure": "radar config refused: RF0.tx_gain 80 is above the cap",
                     "fatal": True, "kind": "config", "amp_safe": None})
    assert s.state == L.FAULT and s.logic.fault["kind"] == "config" and s.logic.failures == 0
    s.advance(600)
    assert s.kinds() == ["start"]


def test_radar_that_finishes_by_itself_ends_the_run():
    s = Sim().running()
    s.call("ended", {"requested": False, "failure": "", "amp_safe": True})
    s.advance(600)
    assert s.state == L.READY and s.take() == [] and not s.logic.snapshot()["run_wanted"]
    assert s.logic.last_end == "the radar finished by itself"


# -------------------------------------------------------------------- amplifier safe

@pytest.mark.parametrize("before", ["running", "stopping", "failed"])
def test_amplifier_not_confirmed_safe_is_a_fault_with_its_own_reason(before):
    s = Sim().running()
    info = {"requested": before == "stopping", "failure": "x" if before == "failed" else "",
            "amp_safe": False, "amp_safe_detail": "command run: status 3 (ASSUME_ENABLED) in each of 3 attempts"}
    if before == "stopping":
        s.call("stop", L.BUTTON)
    s.call("ended", info)
    assert s.state == L.FAULT
    fault = s.logic.snapshot()["fault"]
    assert fault["kind"] == "amp_unsafe" and "ASSUME_ENABLED" in fault["reason"]
    assert "could not be confirmed safe" in fault["reason"]
    s.advance(600)
    assert "start" not in s.kinds()[1:]


# -------------------------------------------------- gates during a run: stop and resume

def test_gate_failure_stops_the_run_and_it_resumes_when_the_gate_recovers():
    s = Sim().running()
    s.take()
    s.gates(telemetry="amplifier telemetry: status stale")
    assert s.take() == [("stop", {"kind": "gate", "reason": "telemetry: amplifier telemetry: status stale"})]
    assert s.state == L.STOPPING
    s.call("ended", OK)
    assert s.state == L.NOT_READY and s.logic.snapshot()["run_wanted"]
    assert s.logic.snapshot()["detail"] == "will resume when ready"
    s.advance(120)
    assert s.take() == []                                   # keeps waiting while the gate is bad
    s.gates().advance(9)
    assert s.take() == [] and s.state == L.STARTING         # good again, hold-off running
    s.advance(1)
    assert s.take() == [("start", {"trigger": "button", "attempt": 1, "resume": True})]
    s.call("started")
    assert s.state == L.RUNNING and s.logic.interruptions == 1


def test_resumes_are_limited_then_fault():
    s = Sim(max_resumes=2).running()
    for n in (1, 2):
        s.gates(watcher="safety watcher: amplifier 56.1 C")
        s.call("ended", OK)
        s.gates().advance(10)
        s.call("started")
        assert s.state == L.RUNNING and s.logic.interruptions == n
    s.gates(watcher="safety watcher: amplifier 56.1 C")
    s.call("ended", OK)
    assert s.state == L.FAULT
    fault = s.logic.fault
    assert fault["kind"] == "resume_limit" and "3 times (limit 2)" in fault["reason"] and "56.1 C" in fault["reason"]
    s.gates().advance(600)
    assert s.state == L.FAULT


def test_a_stable_run_clears_the_interruption_count():
    s = Sim(max_resumes=1, stable_run_s=300.0).running()
    for _ in range(4):                                      # more than max_resumes, but far apart
        s.gates(telemetry="lost")
        s.call("ended", OK)
        s.gates().advance(10)
        s.call("started")
        s.advance(300)
        assert s.logic.interruptions == 0
    assert s.state == L.RUNNING


def test_gate_failure_during_the_start_sequence_aborts_it_and_resumes():
    s = Sim().gates()
    s.call("start", L.BUTTON)
    s.take()
    s.gates(power=GateResult(start="power service: state loss", run="power service: state loss", kind="power_fail"))
    assert s.kinds() == ["stop"]
    s.call("ended", ABORTED)
    assert s.state == L.POWER_FAIL
    s.gates().advance(10)
    assert s.take() == [("start", {"trigger": "button", "attempt": 1, "resume": True})]


def test_a_manual_stop_while_a_gate_is_stopping_the_run_cancels_the_resume():
    s = Sim().running()
    s.gates(telemetry="lost")
    s.take()
    assert s.call("stop", L.COMMAND) == (True, "already stopping")
    s.call("ended", OK)
    s.gates().advance(60)
    assert s.take() == [] and s.state == L.READY


def test_a_press_while_waiting_to_resume_cancels_the_run():
    s = Sim().running()
    s.gates(telemetry="lost")
    s.call("ended", OK)
    s.take()
    assert s.call("toggle", L.BUTTON) == (True, "start cancelled")
    s.gates().advance(60)
    assert s.take() == [] and s.state == L.READY


def test_a_gate_that_only_refuses_starts_does_not_stop_a_run():
    s = Sim().running()
    s.take()
    s.gates(sdr=GateResult(start="SDR not found on USB"),          # the SDR leaves the bus while it loads
            data=GateResult(start="data drive nearly full: 19.0 GB free"),
            config=GateResult(start="radar config: changed and now refused"))
    s.advance(30)
    assert s.take() == [] and s.state == L.RUNNING


def test_interrupt_stops_now_and_resumes_after_the_hold_off():
    # The power service's stop hook can arrive before its status file shows the loss.
    s = Sim().running()
    s.take()
    assert s.call("interrupt", "power", "power service stop hook") == (True, "stopping")
    assert s.take() == [("stop", {"kind": "gate", "reason": "power service stop hook", "urgent": True,
                                  "power": True})]
    s.call("ended", OK)
    assert s.logic.snapshot()["run_wanted"]
    s.advance(9)
    assert s.take() == []
    s.advance(1)
    assert s.take() == [("start", {"trigger": "button", "attempt": 1, "resume": True})]
    assert s.call("interrupt", "power", "again")[1] == "stopping"
    s.call("ended", ABORTED)
    idle = Sim().gates()
    assert idle.call("interrupt", "power", "x") == (True, "not running") and idle.take() == []


# ------------------------------------------------------------- the plan's fault table

def test_fault_table_amplifier_telemetry_missing_at_start():
    s = Sim().gates(telemetry="amplifier telemetry: status missing (no such file)")
    assert s.state == L.NOT_READY
    assert not s.call("start", L.COMMAND)[0] and s.take() == []


def test_fault_table_telemetry_override_lets_it_start():
    s = Sim().gates(telemetry=GateResult(note="OVERRIDE, running without amplifier telemetry", kind="override"))
    assert s.state == L.READY and s.call("start", L.BUTTON)[0]
    assert "OVERRIDE" in s.logic.snapshot()["gates"]["telemetry"]["note"]


def test_fault_table_telemetry_lost_during_a_run():
    s = Sim().running()
    s.gates(telemetry="amplifier telemetry: no row for 3.2 s")
    assert s.kinds()[-1] == "stop"
    s.call("ended", OK)
    assert s.state == L.NOT_READY
    s.gates().advance(10)
    assert s.kinds() == ["start"]


def test_fault_table_over_temperature_or_28v_out_of_range():
    s = Sim().running()
    s.gates(watcher="safety watcher: 28 V rail at 31.0 V")
    assert s.kinds()[-1] == "stop"
    s.call("ended", OK)
    s.gates().advance(10)
    assert s.kinds() == ["start"]                           # resumes when the reading recovers


def test_fault_table_safety_watcher_crashes():
    s = Sim().running()
    s.gates(watcher="safety watcher: status stale (6.0 s old, limit 5 s)")
    assert s.kinds()[-1] == "stop"                          # stopped while the watcher's status is stale
    s.call("ended", OK)
    s.advance(30)
    assert s.take() == [] and s.state == L.NOT_READY
    s.gates().advance(10)
    assert s.kinds() == ["start"]


def test_fault_table_nvme_missing_or_not_writable():
    s = Sim().gates(data="data drive not mounted at /mnt/nvme")
    assert s.state == L.NOT_READY and not s.call("start", L.BUTTON)[0]
    r = Sim().running()
    r.gates(data="data root /mnt/nvme/radar-runner/collections is not writable")
    assert r.kinds()[-1] == "stop"


def test_fault_table_disk_nearly_full():
    # Below the start reserve: no start, but a running collection continues ...
    s = Sim().running()
    s.take()
    s.gates(data=GateResult(start="data drive nearly full: 19.0 GB free, no start below 20 GB"))
    assert s.take() == [] and s.state == L.RUNNING
    # ... below the stop threshold it is stopped, and stays off.
    s.gates(data="data drive full: 4.9 GB free, stop below 5 GB")
    assert s.kinds() == ["stop"]
    s.call("ended", OK)
    s.advance(3600)
    assert s.take() == [] and s.state == L.NOT_READY
    assert not Sim().gates(data=GateResult(start="data drive nearly full")).call("start", L.BUTTON)[0]


def test_fault_table_sdr_missing_or_on_usb_2():
    for reason in ("SDR not found on USB", "SDR on a USB 2 link (480 Mbit/s at 4-1)"):
        s = Sim().gates(sdr=GateResult(start=reason))
        assert s.state == L.NOT_READY and s.call("start", L.BUTTON) == (False, f"not ready: sdr: {reason}")


def test_fault_table_power_loss_stops_any_run_and_shows_power_fail():
    s = Sim().running()
    loss = GateResult(start="power service: state loss, radar stop requested",
                      run="power service: state loss, radar stop requested", kind="power_fail")
    s.gates(power=loss)
    assert s.kinds()[-1] == "stop" and s.state == L.STOPPING
    s.call("ended", OK)
    assert s.state == L.POWER_FAIL and L.LEDS[s.state] == ("off", "off")
    s.advance(30)
    assert s.take() == []
    s.gates().advance(10)                                   # power came back before the shutdown
    assert s.take() == [("start", {"trigger": "button", "attempt": 1, "resume": True})]


def test_fault_table_stale_power_status_is_not_ready_not_power_fail():
    s = Sim().gates(power="power service: status stale (7.0 s old, limit 5 s)")
    assert s.state == L.NOT_READY


def test_fault_table_turn_on_check_fails():
    s = Sim().gates()
    s.call("start", L.BUTTON)
    s.call("ended", {"requested": False, "failure": "turn-on check failed: no direct signal", "fatal": True,
                     "kind": "turn_on_check", "amp_safe": True})
    assert s.state == L.FAULT and s.logic.fault["kind"] == "turn_on_check"
    s.advance(600)
    assert s.kinds() == ["start"]                           # no retry by itself ...
    assert s.call("toggle", L.BUTTON) == (True, "starting")  # ... but at the next button press
    assert s.kinds() == ["start"] and s.state == L.STARTING


# ----------------------------------------------------------------------- leaving fault

def faulted(**kw):
    s = Sim(**kw).gates()
    s.call("start", L.BUTTON)
    s.call("ended", failed(fatal=True, kind="config"))
    assert s.state == L.FAULT
    s.take()
    return s


def test_fault_is_left_by_a_button_press_which_also_starts():
    s = faulted()
    assert s.call("toggle", L.BUTTON) == (True, "starting")
    assert s.kinds() == ["start"] and s.logic.fault is None


def test_fault_is_left_by_a_start_command_which_also_starts():
    s = faulted()
    assert s.call("start", L.COMMAND) == (True, "starting") and s.kinds() == ["start"]


def test_fault_is_left_by_clear_fault_without_starting():
    s = faulted()
    assert s.call("clear_fault", L.COMMAND) == (True, "fault cleared")
    assert s.state == L.READY and s.take() == []
    assert s.call("clear_fault", L.COMMAND) == (True, "no fault")


def test_stop_does_not_clear_a_fault():
    s = faulted()
    assert s.call("stop", L.COMMAND) == (True, "not running")       # as the power service's hook does
    assert s.call("interrupt", "power", "hook") == (True, "not running")
    assert s.state == L.FAULT


def test_a_press_in_fault_while_not_ready_changes_nothing():
    s = faulted()
    s.gates(telemetry="lost")
    assert s.state == L.FAULT                               # fault is shown before "not ready"
    for request in ("toggle", "start"):
        ok, message = s.call(request, L.BUTTON)
        assert not ok and message == ("not ready: telemetry: lost (fault kept: the radar exited with status 1)")
    assert s.state == L.FAULT and s.take() == []
    assert s.logic.snapshot()["fault"]["reason"] == "the radar exited with status 1"     # the reason is not lost
    s.gates()
    assert s.call("toggle", L.BUTTON) == (True, "starting")           # ready again: the press clears and starts
    assert s.kinds() == ["start"] and s.logic.fault is None


def test_fault_is_left_by_the_next_automatic_trigger_only_after_a_stop_of_the_truck():
    s = Sim(sources=(L.BUTTON, L.COMMAND, L.AUTO)).gates()
    s.call("auto", L.MOVING)
    assert s.kinds() == ["start"]
    s.call("ended", failed(fatal=True, kind="turn_on_check"))
    assert s.state == L.FAULT
    s.call("auto", L.MOVING)
    s.call("auto", L.UNKNOWN)
    s.call("auto", L.MOVING)                                # fix lost and back: not a new trigger
    s.advance(600)
    assert s.take() == [] and s.state == L.FAULT
    s.call("auto", L.STATIONARY)
    assert s.state == L.FAULT                               # stopping alone does not clear it
    s.call("auto", L.MOVING)
    assert s.take() == [("start", {"trigger": "gps", "attempt": 1, "resume": False})]
    assert s.logic.fault is None


def test_fault_does_not_survive_as_a_retry_loop():
    s = faulted()
    s.advance(3600)
    assert s.take() == []


# ----------------------------------------------------------------- the automatic source

def auto_sim(**kw):
    return Sim(sources=(L.BUTTON, L.COMMAND, L.AUTO), **kw)


def test_automatic_source_is_active_from_boot_and_waits_for_the_gates():
    s = auto_sim()
    s.call("auto", L.MOVING)                                # already driving before the gates are good
    s.gates(telemetry="amplifier telemetry: not configured yet")
    assert s.take() == [] and s.state == L.NOT_READY
    s.gates()
    assert s.take() == [("start", {"trigger": "gps", "attempt": 1, "resume": False})]
    s.call("started")
    assert s.logic.snapshot()["trigger"] == "gps"


def test_automatic_run_stops_only_on_a_positive_stationary_report():
    s = auto_sim().gates()
    s.call("auto", L.MOVING)
    s.call("started")
    s.take()
    s.call("auto", L.UNKNOWN)                               # loss of fix never changes the radar state
    s.advance(3600)
    assert s.take() == [] and s.state == L.RUNNING
    s.call("auto", L.STATIONARY)
    assert s.take() == [("stop", {"kind": "auto", "reason": "gps: stationary"})]
    s.call("ended", OK)
    assert s.state == L.READY
    s.call("auto", L.MOVING)                                # and the next drive starts again
    assert s.kinds() == ["start"]


def test_loss_of_fix_does_not_start_anything():
    s = auto_sim().gates()
    s.call("auto", L.UNKNOWN)
    s.advance(60)
    assert s.take() == [] and s.state == L.READY


def test_manual_stop_of_an_automatic_run_blocks_it_until_the_truck_has_stopped():
    s = auto_sim().gates()
    s.call("auto", L.MOVING)
    s.call("started")
    s.take()
    assert s.call("toggle", L.BUTTON) == (True, "stopping")         # a button press stops a GPS-started run
    assert s.kinds() == ["stop"]
    s.call("ended", OK)
    s.advance(600)
    assert s.take() == [] and s.state == L.READY and s.logic.auto_blocked
    s.call("auto", L.UNKNOWN)
    s.call("auto", L.MOVING)
    assert s.take() == []                                   # still the same drive
    s.call("auto", L.STATIONARY)
    assert not s.logic.auto_blocked and s.take() == []
    s.call("auto", L.MOVING)
    assert s.kinds() == ["start"]


def test_manual_stop_while_already_stationary_does_not_block():
    s = auto_sim().gates()
    s.call("auto", L.STATIONARY)
    s.call("start", L.BUTTON)
    s.call("started")
    s.call("stop", L.BUTTON)
    s.call("ended", OK)
    assert not s.logic.auto_blocked
    s.take()
    s.call("auto", L.MOVING)
    assert s.kinds() == ["start"]


def test_manual_stop_of_a_manual_run_while_driving_also_blocks_the_automatic_source():
    s = auto_sim().gates()
    s.call("auto", L.STATIONARY)
    s.call("start", L.BUTTON)
    s.call("started")
    s.call("auto", L.MOVING)
    s.take()
    s.call("stop", L.COMMAND)
    assert s.kinds() == ["stop"]
    s.call("ended", OK)
    s.advance(60)
    assert s.take() == [] and s.logic.auto_blocked          # otherwise it would restart at once


def test_a_manual_run_is_not_stopped_by_the_automatic_source():
    s = auto_sim().gates()
    s.call("start", L.BUTTON)
    s.call("started")
    s.take()
    s.call("auto", L.MOVING)
    s.call("auto", L.STATIONARY)
    s.advance(3600)
    assert s.take() == [] and s.state == L.RUNNING and s.logic.snapshot()["trigger"] == "button"


def test_start_command_takes_over_an_automatic_run():
    s = auto_sim().gates()
    s.call("auto", L.MOVING)
    s.call("started")
    s.take()
    assert s.call("start", L.COMMAND) == (True, "taken over: now a manual run")
    s.call("auto", L.STATIONARY)
    s.advance(60)
    assert s.take() == [] and s.state == L.RUNNING and s.logic.snapshot()["trigger"] == "command"


def test_automatic_run_that_fails_is_retried_and_cancelled_by_stationary():
    s = auto_sim().gates()
    s.call("auto", L.MOVING)
    s.call("ended", failed())
    s.take()
    s.call("auto", L.STATIONARY)
    s.advance(60)
    assert s.take() == [] and s.state == L.READY and not s.logic.snapshot()["run_wanted"]


def test_a_radar_that_finishes_by_itself_does_not_restart_in_a_loop_under_the_automatic_source():
    s = auto_sim().gates()
    s.call("auto", L.MOVING)
    s.call("started")
    s.take()
    s.call("ended", {"requested": False, "failure": "", "amp_safe": True})
    s.advance(600)
    assert s.take() == [] and s.logic.auto_blocked
    s.call("auto", L.STATIONARY)
    s.call("auto", L.MOVING)
    assert s.kinds() == ["start"]


def test_automatic_source_is_ignored_when_disabled():
    s = Sim().gates()
    s.call("auto", L.MOVING)
    s.advance(60)
    assert s.take() == [] and s.logic.snapshot()["auto"] == {"enabled": False, "source": "gps",
                                                             "state": "moving", "blocked": False}


def test_automatic_run_interrupted_by_a_gate_resumes_only_while_still_moving():
    s = auto_sim().gates()
    s.call("auto", L.MOVING)
    s.call("started")
    s.gates(telemetry="lost")
    s.call("ended", OK)
    s.take()
    s.call("auto", L.STATIONARY)                            # the truck stopped meanwhile
    s.gates().advance(60)
    assert s.take() == [] and s.state == L.READY


# ------------------------------------------------------------------- manual run timeout

def test_manual_run_is_stopped_by_the_long_timeout():
    s = Sim(manual_timeout_s=3600.0).running()
    s.take()
    s.advance(3599)
    assert s.take() == []
    s.advance(1)
    kind, data = s.take()[0]
    assert kind == "stop" and data["kind"] == "timeout"
    s.call("ended", OK)
    s.advance(600)
    assert s.state == L.READY and s.take() == []


def test_timeout_also_ends_a_run_that_waits_to_resume():
    s = Sim(manual_timeout_s=3600.0).running()
    s.gates(data="data drive full")
    s.call("ended", OK)
    s.take()
    s.advance(3600)
    s.gates().advance(60)
    assert s.take() == [] and s.state == L.READY


def test_no_timeout_when_set_to_zero():
    s = Sim(manual_timeout_s=0.0).running()
    s.take()
    s.advance(200000, step=1000.0)
    assert s.take() == [] and s.state == L.RUNNING


# ---------------------------------------------------------------------------- shutdown

def test_shutdown_stops_the_radar_and_starts_nothing_more():
    s = auto_sim().gates()
    s.call("auto", L.MOVING)
    s.call("started")
    s.take()
    s.call("shutdown")
    assert s.take() == [("stop", {"kind": "shutdown", "reason": "the manager is stopping", "urgent": True})]
    s.call("ended", OK)
    s.advance(60)
    assert s.take() == []
    assert s.call("start", L.COMMAND) == (False, "the manager is stopping")


# ------------------------------------------------------------------- events and status

def test_events_record_gate_changes_requests_starts_stops_and_faults():
    s = Sim().gates(telemetry="lost")
    s.gates()
    s.call("start", L.BUTTON)
    s.call("ended", failed(fatal=True, kind="config"))
    names = [e["event"] for e in s.events]
    for expected in ("gate", "state", "request", "start", "fault"):
        assert expected in names
    gate_events = [e for e in s.events if e["event"] == "gate"]
    assert gate_events == [{"event": "gate", "gate": "telemetry", "ok": False, "reason": "lost"},
                           {"event": "gate", "gate": "telemetry", "ok": True, "reason": ""}]
    fault = [e for e in s.events if e["event"] == "fault"][0]
    assert fault["kind"] == "config"
    json.dumps(s.events)


def test_snapshot_is_plain_json_in_every_state():
    s = Sim(manual_timeout_s=10.0)
    json.dumps(s.logic.snapshot())
    s.gates(sdr=GateResult(start="x", note="n"))
    json.dumps(s.logic.snapshot())
    s.gates()
    s.call("start", L.BUTTON)
    json.dumps(s.logic.snapshot())
    s.call("ended", failed())
    snap = s.logic.snapshot()
    assert snap["retries"] == {"failed_starts": 1, "max_retries": 3, "retry_in_s": 10.0,
                               "interruptions": 0, "max_resumes": 2, "restarts_last_hour": 0,
                               "max_restarts_per_hour": 1000}
    assert snap["leds"] == {"ready_yellow": "breathing", "running_green": "off"}
    json.dumps(snap)


def test_config_from_the_manager_section():
    from radar_runner.common.config import DEFAULTS
    cfg, warnings = LogicConfig.from_config(DEFAULTS["manager"])
    assert warnings == []
    assert cfg.retry_delays_s == (10.0, 30.0, 90.0) and cfg.max_resumes == 5
    assert cfg.max_restarts_per_hour == 12 and cfg.stable_run_s == 300.0 and cfg.resume_holdoff_s == 10.0
    assert cfg.sources == (L.BUTTON, L.COMMAND) and cfg.manual_timeout_s == 50400.0


def manager_cfg(**run):
    import copy
    from radar_runner.common.config import DEFAULTS
    cfg = copy.deepcopy(DEFAULTS["manager"])
    cfg["run"].update(run)
    return cfg


@pytest.mark.parametrize("setting, value, text", [
    ("retry_delays_s", [0, 0, 0], "retry_delays_s must be 1 to 10 delays of at least 5 s"),
    ("retry_delays_s", [10.0, 1.0], "retry_delays_s must be"),
    ("retry_delays_s", [], "retry_delays_s must be"),
    ("retry_delays_s", ["soon"], "retry_delays_s must be"),
    ("retry_delays_s", [10.0] * 11, "retry_delays_s must be"),
    ("stable_run_s", 0.0, "stable_run_s must be at least 60 s"),
    ("stable_run_s", 59.0, "stable_run_s must be at least 60 s"),
    ("resume_holdoff_s", 0.0, "resume_holdoff_s must be at least 2 s"),
    ("max_resumes", -1, "max_resumes must be between 0 and 100"),
    ("max_restarts_per_hour", 0, "max_restarts_per_hour must be between 1 and 120"),
    ("max_restarts_per_hour", 100000, "max_restarts_per_hour must be between 1 and 120"),
    ("manual_timeout_s", -5.0, "manual_timeout_s must not be negative"),
])
def test_settings_that_would_defeat_the_restart_limits_are_refused(setting, value, text):
    cfg, warnings = LogicConfig.from_config(manager_cfg(**{setting: value}))
    assert len(warnings) == 1 and text in warnings[0] and warnings[0].endswith("the default is used")
    assert cfg == LogicConfig()                             # the defaults, all of them


def test_settings_at_their_minimums_are_accepted():
    cfg, warnings = LogicConfig.from_config(manager_cfg(retry_delays_s=[5.0], stable_run_s=60.0,
                                                        resume_holdoff_s=2.0, max_restarts_per_hour=1,
                                                        max_resumes=0, manual_timeout_s=0.0))
    assert warnings == [] and cfg.retry_delays_s == (5.0,) and cfg.max_restarts_per_hour == 1


# --------------------------------------------------------- the restart cap per hour

def test_a_gate_that_drops_every_five_minutes_cannot_restart_the_radar_for_ever():
    # Each run is "stable" (300 s), so the interruption count is cleared every time: the
    # per-hour cap is what stops it.
    s = Sim(max_resumes=2, stable_run_s=300.0, max_restarts_per_hour=6).running()
    restarts = 0
    for _ in range(50):
        s.advance(300)                                      # five minutes of running
        s.gates(telemetry="lost")
        s.call("ended", OK)
        s.gates().advance(10)
        if s.state == L.FAULT:
            break
        restarts += 1
        s.call("started")
    assert restarts == 6 and s.state == L.FAULT
    fault = s.logic.snapshot()["fault"]
    assert fault["kind"] == "restart_limit" and "6 automatic restarts within an hour (limit 6)" in fault["reason"]
    assert s.logic.snapshot()["retries"]["restarts_last_hour"] == 6


def test_restart_cap_counts_retries_of_failed_starts_too():
    s = Sim(max_restarts_per_hour=2, retry_delays_s=(10.0, 10.0, 10.0, 10.0)).gates()
    s.call("start", L.BUTTON)
    for _ in range(2):
        s.call("ended", failed())
        s.advance(10)
        assert s.state == L.STARTING
    s.call("ended", failed())
    s.advance(10)
    assert s.state == L.FAULT and s.logic.fault["kind"] == "restart_limit"


def test_restart_cap_is_not_reset_by_a_new_run_or_a_cleared_fault_only_by_time():
    s = Sim(max_restarts_per_hour=1).running()
    s.gates(telemetry="lost")
    s.call("ended", OK)
    s.gates().advance(10)                                   # the one allowed restart
    s.call("started")
    s.gates(telemetry="lost")
    s.call("ended", OK)
    s.gates().advance(10)
    assert s.logic.fault["kind"] == "restart_limit"
    assert s.call("toggle", L.BUTTON) == (True, "starting")           # a person may start it again ...
    s.call("started")
    s.gates(telemetry="lost")
    s.call("ended", OK)
    s.gates().advance(10)
    assert s.logic.fault["kind"] == "restart_limit"         # ... but the next automatic restart is refused again
    s.call("clear_fault", L.COMMAND)
    s.advance(3600)                                         # an hour later the count is empty
    s.call("start", L.BUTTON)
    s.call("started")
    s.gates(telemetry="lost")
    s.call("ended", OK)
    s.gates().advance(10)
    assert s.state == L.STARTING


def test_a_first_start_is_not_a_restart():
    s = Sim(max_restarts_per_hour=1).gates()
    for _ in range(5):                                      # five manual runs in a row: no restart among them
        s.call("start", L.BUTTON)
        s.call("started")
        s.call("stop", L.BUTTON)
        s.call("ended", OK)
    assert s.state == L.READY and s.logic.restarts == []


# -------------------------------------------------------------------- urgent stops

POWER_LOSS = GateResult(start="power service: state loss, radar stop requested",
                        run="power service: state loss, radar stop requested", kind="power_fail")


def test_a_stop_for_power_loss_is_urgent_and_marked_as_power():
    s = Sim().running()
    s.take()
    s.gates(power=POWER_LOSS)
    assert s.take() == [("stop", {"kind": "gate", "reason": "power: power service: state loss, radar stop requested",
                                  "urgent": True, "power": True})]


def test_a_stop_for_another_gate_or_by_hand_is_not_urgent():
    s = Sim().running()
    s.take()
    s.gates(telemetry="lost")
    assert "urgent" not in s.take()[0][1]
    t = Sim().running()
    t.take()
    t.call("stop", L.BUTTON)
    assert "urgent" not in t.take()[0][1]


def test_a_stop_under_way_becomes_urgent_when_power_is_lost_or_the_manager_exits():
    for how in ("power gate", "interrupt", "shutdown"):
        s = Sim().running()
        s.call("stop", L.BUTTON)
        assert s.take() == [("stop", {"kind": "manual", "reason": "stop by button"})]
        if how == "power gate":
            s.gates(power=POWER_LOSS)
        elif how == "interrupt":
            s.call("interrupt", "power", "power service stop hook")
        else:
            s.call("shutdown")
        again = s.take()
        assert len(again) == 1 and again[0][0] == "stop" and again[0][1]["urgent"] is True, how
        s.gates(power=POWER_LOSS)
        assert s.take() == []                               # only once
        s.call("ended", OK)
        s.gates().advance(60)
        assert s.take() == []                               # it was a manual stop: nothing resumes


def test_urgency_is_forgotten_with_the_run():
    s = Sim().running()
    s.call("interrupt", "power", "hook")
    s.call("ended", {"requested": True, "failure": "", "amp_safe": None})      # power loss: nothing was checked
    assert s.logic.fault is None and s.logic.snapshot()["run_wanted"]
    s.advance(10)
    assert s.kinds()[-1] == "start"
    s.call("started")
    s.take()
    s.call("stop", L.BUTTON)
    assert "urgent" not in s.take()[0][1]


# --------------------------------------------- a radar that exits unasked with status 0

def test_unasked_exit_with_status_0_of_a_radar_that_runs_until_stopped_is_an_interruption():
    s = Sim(max_resumes=2).running()
    s.take()
    exit0 = {"requested": False, "failure": "", "amp_safe": True,
             "interrupted": "the radar exited with status 0 without being asked to (num_pulses is -1)"}
    s.call("ended", exit0)
    snap = s.logic.snapshot()
    assert snap["run_wanted"] and snap["detail"] == "resume in 10 s" and s.logic.interruptions == 1
    assert snap["last_end"].startswith("interrupted: the radar exited with status 0")
    s.advance(10)
    assert s.take() == [("start", {"trigger": "button", "attempt": 1, "resume": True})]
    s.call("ended", exit0)
    s.advance(10)
    s.call("ended", exit0)                                  # the third time: the resume limit
    assert s.state == L.FAULT and s.logic.fault["kind"] == "resume_limit"
    assert "interrupted 3 times (limit 2)" in s.logic.fault["reason"]


def test_a_radar_with_a_pulse_count_still_finishes_by_itself():
    s = Sim().running()
    s.call("ended", {"requested": False, "failure": "", "amp_safe": True, "interrupted": ""})
    assert s.state == L.READY and not s.logic.snapshot()["run_wanted"]


# ---------------------------------------------------------------- the amp_unsafe fault

def amp_fault():
    s = Sim().running()
    s.call("stop", L.BUTTON)
    s.call("ended", {"requested": True, "failure": "", "amp_safe": False,
                     "amp_safe_detail": "tool run 3 times: status 3 (ASSUME_ENABLED)"})
    assert s.state == L.FAULT and s.logic.fault["kind"] == "amp_unsafe"
    s.take()
    return s


def test_amp_unsafe_is_not_cleared_by_a_press_a_start_or_the_automatic_source():
    s = amp_fault()
    for request in ("toggle", "start"):
        ok, message = s.call(request, L.BUTTON)
        assert not ok and "it clears when the amplifier pin is confirmed safe" in message
    assert s.state == L.FAULT and s.take() == []
    a = Sim(sources=(L.BUTTON, L.COMMAND, L.AUTO)).gates()
    a.call("auto", L.STATIONARY)
    a.call("amp_unsafe", "tool run 3 times: status 3 (ASSUME_ENABLED)")
    a.call("auto", L.MOVING)
    a.advance(60)
    assert a.state == L.FAULT and a.take() == []


def test_amp_unsafe_clears_by_itself_when_the_record_says_safe_again_and_the_reason_stays_visible():
    s = amp_fault()
    s.call("amp_safe_again", "amp_safe OK (exit 0)")
    assert s.state == L.READY and s.logic.fault is None and s.take() == []     # cleared, nothing started
    last = s.logic.snapshot()["last_fault"]
    assert last["kind"] == "amp_unsafe" and "ASSUME_ENABLED" in last["reason"]
    assert last["cleared_by"] == "the amplifier pin is known safe again: amp_safe OK (exit 0)"
    cleared = [e for e in s.events if e["event"] == "fault_cleared"]
    assert cleared and "ASSUME_ENABLED" in cleared[0]["reason"]


def test_amp_safe_again_does_not_clear_other_faults():
    s = faulted()
    s.call("amp_safe_again", "x")
    assert s.state == L.FAULT and s.logic.fault["kind"] == "config"


def test_amp_unsafe_is_cleared_by_the_clear_fault_command():
    s = amp_fault()
    assert s.call("clear_fault", L.COMMAND) == (True, "fault cleared")
    assert s.state == L.READY and s.logic.snapshot()["last_fault"]["cleared_by"] == "command"


def test_amp_unsafe_with_no_radar_running():
    s = Sim().gates()
    s.call("amp_unsafe", "tool run 3 times: status 3 (ASSUME_ENABLED)")
    assert s.state == L.FAULT and s.logic.fault["kind"] == "amp_unsafe"
    events = len(s.events)
    s.call("amp_unsafe", "again")                           # the next failed attempt: not a new fault
    assert len([e for e in s.events[events:] if e["event"] == "fault"]) == 0
    r = Sim().running()
    r.call("amp_unsafe", "x")                               # a radar is running: its end decides
    assert r.state == L.RUNNING and r.logic.fault is None


def test_power_loss_exit_leaves_no_fault_although_nothing_was_checked():
    s = Sim().running()
    s.gates(power=POWER_LOSS)
    s.call("ended", {"requested": True, "failure": "", "amp_safe": None,
                     "amp_safe_detail": "power loss: nothing checked"})
    assert s.state == L.POWER_FAIL and s.logic.fault is None


def test_last_fault_is_kept_in_the_status_after_any_clearing():
    s = faulted()
    s.call("toggle", L.BUTTON)
    last = s.logic.snapshot()["last_fault"]
    assert last == {"kind": "config", "reason": "the radar exited with status 1", "cleared_by": "button"}
    assert s.logic.snapshot()["fault"] is None
