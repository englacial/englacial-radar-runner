"""The whole manager service with every stand-in: gates from status files, the stand-in ORCA
tree, a mock front panel, the real socket and radarctl. In-process (the service loop runs in a
thread) and as a real process started the way systemd starts it.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest
import yaml

from radar_runner.common import config as common_config
from radar_runner.common import status as S
from radar_runner.manager import ampsafe, radarctl
from radar_runner.manager import logic as L
from radar_runner.manager.panel import Panel
from radar_runner.manager.service import Manager

REPO = common_config.REPO


class Running:
    """A Manager whose loop runs in a thread, with helpers to drive and observe it."""

    def __init__(self, env, panel=None):
        self.env = env
        self.manager = Manager(env.config, panel=panel)
        self.result = None
        self._feeder_stop = threading.Event()
        self._feeder = threading.Thread(target=self._feed, daemon=True)
        self.good = True                        # keep the other services' status files fresh
        self.amp_current = 0.16
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _feed(self):
        while not self._feeder_stop.is_set():
            if self.good:
                self.env.all_good(self.amp_current)
            time.sleep(0.05)

    def _run(self):
        self.result = self.manager.run()

    def start(self, publish=True):
        if publish:
            self.env.all_good()
        else:
            self.good = False               # no status file of any other service
        self._feeder.start()
        self._thread.start()
        return self

    def status(self):
        r = S.read_status(os.path.join(self.env.run_dir, "manager.json"), 5.0)
        return r.data if r.ok else {}

    def state(self):
        return self.status().get("state")

    def wait_state(self, state, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state() == state:
                return self.status()
            time.sleep(0.02)
        raise AssertionError(f"state {state!r} not reached; status: {json.dumps(self.status(), indent=1)[:3000]}")

    def ctl(self, *args):
        """Run radarctl in-process against this manager. Returns (exit status, output)."""
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = radarctl.main(["--socket", self.manager.socket_path, *args])
        return code, out.getvalue()

    def events(self):
        path = os.path.join(self.env.state_dir, "manager-events.jsonl")
        try:
            with open(path) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:           # the manager has not written its first event yet
            return []

    def stop(self):
        self.manager.request_stop()
        self._thread.join(20)
        self._feeder_stop.set()
        assert not self._thread.is_alive()
        return self.result


@pytest.fixture
def mgr(env):
    running = Running(env).start()
    yield running
    if running._thread.is_alive():
        running.stop()


# ------------------------------------------------------------------ end to end

def test_start_and_stop_by_command_line_end_to_end(env, mgr):
    mgr.wait_state("ready")
    code, out = mgr.ctl("start")
    assert code == 0 and out.strip() == "starting"
    status = mgr.wait_state("running")
    assert status["trigger"] == "command" and status["radar"]["recording"] and status["radar"]["pid"] > 0
    assert status["collection"] == env.collections()[0] and status["idle"] is False
    assert status["leds"] == {"ready_yellow": "off", "running_green": "on"}
    time.sleep(0.4)
    code, out = mgr.ctl("stop", "--wait")
    assert code == 0 and out.split() == ["stopping", "stopped"]
    status = mgr.status()
    assert status["state"] == "ready" and status["idle"] is True and status["radar"] == {"active": False}
    # The collection is complete, on the "drive", and the amplifier was made safe once.
    meta = env.metadata()
    assert meta["trigger"] == "command" and meta["end"]["how"] == "stopped" and meta["end"]["amp_safe"] is True
    assert env.amp_safe_calls() == 1
    d = env.collection_dir()
    assert any(f.startswith("rx_samps.bin.") for f in os.listdir(d))
    # The event log tells the story.
    names = [e["event"] for e in mgr.events()]
    for expected in ("manager_started", "state", "request", "start", "collection", "stop", "ended"):
        assert expected in names
    assert all("uptime_s" in e and "time_utc" in e for e in mgr.events())


def test_fifty_start_stop_cycles_without_a_fault(env, mgr):
    mgr.wait_state("ready")
    for i in range(50):
        command = "toggle" if i % 2 else "start"
        assert mgr.ctl(command)[0] == 0
        mgr.wait_state("running")
        assert mgr.ctl("stop", "--wait")[0] == 0
        assert mgr.state() == "ready"
    names = env.collections()
    assert len(names) == 50 and names == sorted(names)
    assert [n[:11] for n in names] == [f"b00001_s{i:03d}" for i in range(1, 51)]
    assert env.amp_safe_calls() == 50
    assert not [e for e in mgr.events() if e["event"] == "fault"]
    assert all(env.metadata(i)["end"]["how"] == "stopped" for i in range(50))


def test_status_file_content(env, mgr):
    status = mgr.wait_state("ready")
    assert list(status["gates"]) == ["telemetry", "watcher", "power", "data", "sdr", "config", "orca", "amp_safe"]
    assert all(g["ok"] for g in status["gates"].values())
    assert status["overrides"] == {"no_amp_telemetry": False} and status["fault"] is None
    assert status["retries"]["failed_starts"] == 0 and status["not_ready"] == []
    assert status["leds"] == {"ready_yellow": "on", "running_green": "off"}
    assert status["sdr_state"] == "safe" and status["amp_safe_policy"] == "always"
    assert status["making_amp_safe"] is False
    assert status["pid"] == os.getpid() and "uptime_s" in status and status["boot_id"]


# ----------------------------------------------------------------- gates, live

def test_not_ready_reasons_and_recovery(env, mgr):
    mgr.wait_state("ready")
    mgr.good = False
    time.sleep(0.1)
    env.publish("watcher", {"ok": False, "tripped": True, "reason": "amplifier 56.2 C, limit 55 C"})
    status = mgr.wait_state("not_ready")
    assert status["not_ready"] == ["watcher: safety watcher: amplifier 56.2 C, limit 55 C"]
    assert status["leds"]["ready_yellow"] == "fast_blink"
    code, out = mgr.ctl("start")
    assert code == 1 and "not ready: watcher: safety watcher: amplifier 56.2 C" in out
    mgr.good = True
    mgr.wait_state("ready")


def test_run_is_stopped_by_lost_telemetry_and_resumes_by_itself(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.good = False                            # every status file goes stale
    status = mgr.wait_state("not_ready")
    assert status["run_wanted"] and status["detail"] == "will resume when ready"
    assert env.metadata()["end"]["how"] == "stopped"
    assert "telemetry" in env.metadata()["end"]["reason"]
    mgr.good = True
    mgr.wait_state("running")
    assert len(env.collections()) == 2 and env.metadata()["resume"] is True
    assert mgr.status()["retries"]["interruptions"] == 1
    mgr.ctl("stop", "--wait")


def test_power_loss_stops_the_radar_and_shows_power_fail(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.good = False
    time.sleep(0.1)
    env.all_good()
    env.publish("power", {"state": "loss", "radar_stop_requested": True})
    feeder_on = True

    def keep_others_fresh():
        while feeder_on:
            up = time.clock_gettime(time.CLOCK_BOOTTIME)
            env.publish("telemetry", {"amp": {"fresh": True, "current_a": 0.0, "voltage_v": 0.0,
                                              "sample_uptime_s": up}})
            env.publish("watcher", {"ok": True, "tripped": False})
            env.publish("power", {"state": "loss", "radar_stop_requested": True})
            time.sleep(0.05)
    t = threading.Thread(target=keep_others_fresh, daemon=True)
    t.start()
    try:
        status = mgr.wait_state("power_fail")
        assert status["leds"] == {"ready_yellow": "off", "running_green": "off"}
        assert status["idle"] and status["run_wanted"]
    finally:
        feeder_on = False
        t.join()
    mgr.good = True                             # power is back before the shutdown: the run resumes
    mgr.wait_state("running")
    mgr.ctl("stop", "--wait")


def test_power_hook_stop_interrupts_and_the_run_resumes(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    code, out = mgr.ctl("stop", "--interrupt", "--wait", "--timeout", "10")
    assert code == 0 and "stopped" in out
    assert env.metadata()["end"]["how"] == "stopped"
    mgr.wait_state("running")                   # by itself, after the hold-off
    assert len(env.collections()) == 2
    mgr.ctl("stop", "--wait")


def test_full_disk_stops_and_refuses(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    data = env.config["manager"]["gates"]["data"]
    data["start_min_free_gb"], data["stop_min_free_gb"] = 1e9, 1e8      # more than any drive has
    status = mgr.wait_state("not_ready")
    assert "data drive full" in status["not_ready"][0]
    assert "data drive full" in env.metadata()["end"]["reason"]
    assert mgr.ctl("stop")[0] == 0
    assert mgr.ctl("start")[0] == 1


def test_missing_sdr_and_usb2_link_are_not_ready(env, mgr):
    mgr.wait_state("ready")
    env.sdr(present=False)
    assert mgr.wait_state("not_ready")["not_ready"] == ["sdr: SDR not found on USB"]
    env.sdr(speed=480, product="USRP B200")
    time.sleep(0.2)
    assert "USB 2 link (480 Mbit/s" in mgr.wait_state("not_ready")["not_ready"][0]
    env.sdr(speed=480, product="WestBridge")    # boot loader: the speed says nothing yet
    status = mgr.wait_state("ready")
    assert "firmware not loaded yet" in status["gates"]["sdr"]["note"]


def test_config_change_is_picked_up_by_the_gate(env, mgr):
    mgr.wait_state("ready")
    with open(env.radar_config) as f:
        text = f.read()
    with open(env.radar_config, "w") as f:
        f.write(text.replace("tx_gain: 72.0", "tx_gain: 73.0"))
    status = mgr.wait_state("not_ready")
    assert "RF0.tx_gain 73 is above the cap of 72" in status["not_ready"][0]


# ---------------------------------------------------- failures, retries and faults

def test_failed_starts_are_retried_then_fault_then_cleared(env, mgr):
    mgr.wait_state("ready")
    env.behave(radar="fail_start")
    t0 = time.monotonic()
    mgr.ctl("start")
    status = mgr.wait_state("fault", timeout=20)
    assert status["fault"]["kind"] == "start_failures" and "3 failed starts in a row" in status["fault"]["reason"]
    assert status["leds"]["ready_yellow"] == "fast_blink"
    assert len(env.collections()) == 3                      # two retries, with delays 0.2 s and 0.3 s
    assert time.monotonic() - t0 >= 0.5
    assert env.amp_safe_calls() == 3                        # after every radar exit
    code, out = mgr.ctl("status")
    assert code == 0 and "fault:      start_failures" in out
    # stop does not clear it; clear-fault does, without starting.
    assert mgr.ctl("stop")[0] == 0 and mgr.state() == "fault"
    code, out = mgr.ctl("clear-fault")
    assert code == 0 and out.strip() == "fault cleared"
    mgr.wait_state("ready")
    assert len(env.collections()) == 3
    env.behave()
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.ctl("stop", "--wait")


def test_radar_crash_mid_run_recovers_by_itself(env, mgr):
    mgr.wait_state("ready")
    env.behave(radar="crash")
    mgr.ctl("start")
    mgr.wait_state("running")
    env.behave()                                # the next start behaves
    deadline = time.monotonic() + 15
    while len(env.collections()) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    status = mgr.wait_state("running")
    assert status["retries"]["failed_starts"] == 1 and len(env.collections()) == 2
    assert env.metadata(0)["end"]["how"] == "failed" and env.metadata(0)["end"]["exit_status"] == 3
    assert env.amp_safe_calls() == 1
    mgr.ctl("stop", "--wait")


def test_radar_killed_from_outside_is_noticed_and_restarted(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    pid = mgr.wait_state("running")["radar"]["pid"]
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 15
    while len(env.collections()) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    mgr.wait_state("running")
    assert env.metadata(0)["end"]["exit_status"] == -9 and env.amp_safe_calls() == 1
    mgr.ctl("stop", "--wait")


def test_amp_safe_failure_is_a_fault_with_its_own_reason(env, mgr):
    mgr.wait_state("ready")
    env.behave(amp_safe=3)
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.ctl("stop")
    status = mgr.wait_state("fault")
    assert status["fault"]["kind"] == "amp_unsafe" and "ASSUME_ENABLED" in status["fault"]["reason"]
    assert env.amp_safe_calls() == 2                        # retried at once, then the fault
    fault = [e for e in mgr.events() if e["event"] == "fault"][0]
    assert fault["kind"] == "amp_unsafe"


def test_config_limit_failure_at_start_is_a_fault(env, mgr):
    mgr.wait_state("ready")
    # The limits change between the gate's check and the start: the runner checks again.
    env.config["manager"]["limits"]["max_enable_duty"] = 0.01
    mgr.manager.gate_list = [g for g in mgr.manager.gate_list if g.name != "config"]
    mgr.ctl("start")
    status = mgr.wait_state("fault")
    assert status["fault"]["kind"] == "config" and "enable duty" in status["fault"]["reason"]
    assert env.collections() == [] and env.amp_safe_calls() == 0


# ------------------------------------------------------------------- override

def test_telemetry_override_is_persistent_shown_and_flags_the_collection(env, mgr):
    mgr.wait_state("ready")
    mgr.good = False
    time.sleep(0.1)

    def others():
        env.publish("watcher", {"ok": True, "tripped": False})
        env.publish("power", {"state": "normal", "radar_stop_requested": False})
    keep = True

    def feed():
        while keep:
            others()
            time.sleep(0.05)
    os.unlink(os.path.join(env.run_dir, "telemetry.json"))
    t = threading.Thread(target=feed, daemon=True)
    t.start()
    try:
        status = mgr.wait_state("not_ready")
        assert status["not_ready"] == ["telemetry: amplifier telemetry: status missing (no such file)"]
        code, out = mgr.ctl("override", "no-amp-telemetry", "on")
        assert code == 0 and "WITHOUT amplifier telemetry" in out
        status = mgr.wait_state("ready")
        assert status["overrides"] == {"no_amp_telemetry": True}
        assert "OVERRIDE" in status["gates"]["telemetry"]["note"]
        assert os.path.exists(os.path.join(env.state_dir, "override-no-amp-telemetry"))
        assert "RUNNING WITHOUT AMPLIFIER TELEMETRY IS ALLOWED" in mgr.ctl("status")[1]
        mgr.ctl("start")
        mgr.wait_state("running")
        mgr.ctl("stop", "--wait")
        assert env.metadata()["flags"] == ["no_amp_telemetry"]
        code, out = mgr.ctl("override", "no-amp-telemetry", "off")
        assert code == 0 and out.strip() == "override cleared"
        assert mgr.wait_state("not_ready")["overrides"] == {"no_amp_telemetry": False}
        assert not os.path.exists(os.path.join(env.state_dir, "override-no-amp-telemetry"))
    finally:
        keep = False
        t.join()


def test_override_survives_a_restart_of_the_manager(env):
    first = Running(env).start()
    first.wait_state("ready")
    assert first.ctl("override", "no-amp-telemetry", "on")[0] == 0
    assert first.stop() == 0
    second = Running(env).start()
    try:
        assert second.wait_state("ready")["overrides"] == {"no_amp_telemetry": True}
        assert second.events()[-1]["event"] != "fault"
        started = [e for e in second.events() if e["event"] == "manager_started"]
        assert len(started) == 2 and started[-1]["override_no_amp_telemetry"] is True
    finally:
        second.stop()


# ------------------------------------------------------------ amp-safe policy

def test_policy_when_needed_uses_the_telemetry_file_to_confirm_a_clean_exit(env):
    env.config["manager"]["amp_safe"].update(policy="when_needed", idle_enabled_s=60.0)
    mgr = Running(env).start()
    try:
        mgr.wait_state("ready")
        mgr.ctl("start")
        mgr.wait_state("running")
        assert mgr.status()["sdr_state"] == "unknown"       # from the launch until the exit is judged
        mgr.ctl("stop", "--wait")
        assert env.amp_safe_calls() == 0 and env.metadata()["end"]["amp_safe_command_run"] is False
        assert "amplifier current 0.16 A at 28.0 V" in env.metadata()["end"]["amp_safe_detail"]
        assert mgr.status()["sdr_state"] == "safe"
        # Enabled current after the exit: the command runs.
        mgr.ctl("start")
        mgr.wait_state("running")
        mgr.amp_current = 2.5
        time.sleep(0.2)
        mgr.ctl("stop", "--wait")
        assert env.amp_safe_calls() == 1
        assert "telemetry shows the amplifier enabled" in env.metadata()["end"]["amp_safe_detail"]
    finally:
        mgr.stop()


# --------------------------------------- the SDR's state with no radar running

def others_good(env, stop):
    """Keep the watcher and power status fresh, without any telemetry."""
    import threading as th

    def feed():
        while not stop.is_set():
            env.publish("watcher", {"ok": True, "tripped": False})
            env.publish("power", {"state": "normal", "radar_stop_requested": False})
            time.sleep(0.05)
    thread = th.Thread(target=feed, daemon=True)
    thread.start()
    return thread


def events_named(mgr, name):
    return [e for e in mgr.events() if e["event"] == name]


def test_manager_start_with_unknown_state_and_no_telemetry_runs_the_tool_once(env):
    # No record at all, the SDR has been in use (its firmware is loaded), nothing to read the current from.
    mgr = Running(env).start(publish=False)
    try:
        wait_for(lambda: events_named(mgr, "amp_safe_done"))
        done = events_named(mgr, "amp_safe_done")[0]
        assert done["ok"] and done["state"] == "safe" and "not known" in done["why"]
        time.sleep(1.0)                                     # several retry periods: no second run
        assert env.amp_safe_calls() == 1 and env.sdr_state() == "safe"
        assert mgr.status()["state"] == "not_ready" and mgr.status()["sdr_state"] == "safe"
    finally:
        mgr.stop()


def test_manager_start_with_the_state_recorded_unknown_and_good_telemetry_needs_no_tool(env):
    ampsafe.SdrState(env.state_dir).set("unknown", "radar launched, collection x")
    mgr = Running(env).start()
    try:
        status = mgr.wait_state("ready")
        assert status["sdr_state"] == "safe" and env.amp_safe_calls() == 0
        marked = events_named(mgr, "sdr_state")
        assert len(marked) == 1 and marked[0]["why"] == "telemetry shows shutdown current with no radar process"
    finally:
        mgr.stop()


def test_sdr_still_in_its_boot_loader_is_safe_without_the_tool(env):
    # After a power cycle a record left "unknown" by a hard power cut is stale.
    ampsafe.SdrState(env.state_dir).set("unknown", "radar launched, collection x")
    env.sdr(speed=480, product="WestBridge")
    mgr = Running(env).start(publish=False)
    try:
        wait_for(lambda: events_named(mgr, "sdr_state"))
        assert events_named(mgr, "sdr_state")[0]["why"] == "nothing has initialised the SDR since it was powered"
        assert env.amp_safe_calls() == 0 and env.sdr_state() == "safe"
    finally:
        mgr.stop()


def test_a_tool_that_cannot_run_is_retried_without_a_fault(env):
    env.behave(amp_safe=6)                                  # BUSY: the SDR is in use by something else
    mgr = Running(env).start(publish=False)
    try:
        wait_for(lambda: env.amp_safe_calls() >= 3)         # every retry_s (0.3 s here)
        assert env.sdr_state() == "unknown" and mgr.status()["fault"] is None
        assert all(not e["ok"] and e["status"] == 6 for e in events_named(mgr, "amp_safe_done"))
        env.behave()
        wait_for(lambda: env.sdr_state() == "safe")
        calls = env.amp_safe_calls()
        time.sleep(0.8)
        assert env.amp_safe_calls() == calls                # and then it is left alone
    finally:
        mgr.stop()


def test_assume_enabled_with_no_radar_running_is_a_fault(env):
    env.behave(amp_safe=3)
    stop = threading.Event()
    thread = others_good(env, stop)
    mgr = Running(env).start(publish=False)
    try:
        status = mgr.wait_state("fault")
        assert status["fault"]["kind"] == "amp_unsafe" and "ASSUME_ENABLED" in status["fault"]["reason"]
        assert env.amp_safe_calls() >= 2 and status["sdr_state"] == "unknown"
    finally:
        stop.set()
        thread.join()
        mgr.stop()


def test_enabled_current_with_no_radar_process_runs_the_tool(env, mgr):
    mgr.wait_state("ready")
    assert env.amp_safe_calls() == 0 and mgr.status()["sdr_state"] == "safe"
    mgr.amp_current = 2.5                                   # the amplifier is enabled, and nothing is running
    wait_for(lambda: env.amp_safe_calls() >= 1)
    run = events_named(mgr, "amp_safe_run")[0]
    assert "telemetry shows the amplifier enabled for" in run["why"] and "with no radar process" in run["why"]
    mgr.amp_current = 0.16
    time.sleep(0.2)
    calls = env.amp_safe_calls()
    time.sleep(0.8)
    assert env.amp_safe_calls() == calls and mgr.status()["fault"] is None


def test_a_short_enable_with_no_radar_does_not_run_the_tool(env, mgr):
    env.config["manager"]["amp_safe"]["idle_enabled_s"] = 1.0
    mgr.wait_state("ready")
    mgr.amp_current = 2.5                                   # as during the start of the tool or of UHD
    time.sleep(0.4)
    mgr.amp_current = 0.16
    time.sleep(1.2)
    assert env.amp_safe_calls() == 0


def test_default_policy_end_to_end_no_tool_run_in_normal_operation(env):
    env.config["manager"]["amp_safe"]["policy"] = "when_needed"
    mgr = Running(env).start()
    try:
        mgr.wait_state("ready")
        for _ in range(3):
            mgr.ctl("start")
            mgr.wait_state("running")
            mgr.ctl("stop", "--wait")
        assert env.amp_safe_calls() == 0 and mgr.status()["sdr_state"] == "safe"
        # A radar that has to be killed: now the tool runs ... unless the current shows shutdown,
        # which it does here: the reading decides.
        env.config["manager"]["orca"]["stop_timeout_s"] = 0.3
        env.behave(radar="ignore_sigint")
        mgr.ctl("start")
        mgr.wait_state("running")
        mgr.good = False                                    # no telemetry: the history decides
        time.sleep(0.2)
        mgr.wait_state("not_ready")
        assert env.metadata()["end"]["killed"] is True and env.metadata()["end"]["amp_safe_command_run"] is True
        assert env.amp_safe_calls() == 1 and env.sdr_state() == "safe"
    finally:
        mgr.stop()


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


# ------------------------------------------------------- button, LEDs, automatic source

def mock_panel(env, manager_holder):
    from gpiozero.pins.mock import MockFactory, MockPWMPin
    factory = MockFactory(pin_class=MockPWMPin)
    cfg = dict(env.config["manager"]["panel"], enabled=True, kernel_debounce_s=0.0, press_min_s=0.05,
               press_gap_s=0.2)
    panel = Panel(cfg, lambda: manager_holder[0].press(), pin_factory=factory)
    return panel, factory


def press(factory, hold_s=0.1):
    pin = factory.pin(4)
    pin.drive_low()
    time.sleep(hold_s)
    pin.drive_high()


def test_button_starts_and_stops_and_the_leds_follow_the_state(env):
    holder = []
    panel, factory = mock_panel(env, holder)
    mgr = Running(env, panel=panel)
    holder.append(mgr.manager)
    mgr.start()
    try:
        mgr.wait_state("ready")
        yellow, green = factory.pin(18), factory.pin(17)
        assert panel.shown == ("on", "off") and yellow.state == 1.0 and green.state == 0
        press(factory)
        status = mgr.wait_state("running")
        assert status["trigger"] == "button" and panel.shown == ("off", "on")
        assert yellow.state == 0.0 and green.state == 1
        assert env.metadata()["trigger"] == "button"
        time.sleep(0.3)
        press(factory, hold_s=0.01)             # a glitch: ignored
        time.sleep(0.2)
        assert mgr.state() == "running"
        press(factory)
        mgr.wait_state("ready")
        assert panel.shown == ("on", "off") and env.metadata()["end"]["how"] == "stopped"
        button = mgr.status()["panel"]["button"]
        assert button["presses"] == 2 and button["ignored"]["too_short"] == 1
        # While not ready a press does nothing.
        mgr.good = False
        mgr.wait_state("not_ready")
        assert panel.shown == ("fast_blink", "off")
        time.sleep(0.3)
        press(factory)
        time.sleep(0.3)
        assert mgr.state() == "not_ready" and len(env.collections()) == 1
    finally:
        assert mgr.stop() == 0
    assert panel.yellow is None                 # pins released: LEDs off


def test_automatic_source_starts_and_stops_and_respects_a_manual_stop(env):
    env.config["manager"]["triggers"]["gps"] = True
    mgr = Running(env).start()
    try:
        mgr.wait_state("ready")
        mgr.manager.report_auto("moving")
        status = mgr.wait_state("running")
        assert status["trigger"] == "gps" and env.metadata()["trigger"] == "gps"
        mgr.manager.report_auto("unknown")      # fix lost: nothing changes
        time.sleep(0.3)
        assert mgr.state() == "running"
        assert mgr.ctl("stop", "--wait")[0] == 0        # manual stop of an automatic run
        mgr.manager.report_auto("moving")
        time.sleep(0.5)
        status = mgr.status()
        assert status["state"] == "ready" and status["auto"]["blocked"] and len(env.collections()) == 1
        mgr.manager.report_auto("stationary")
        mgr.manager.report_auto("moving")
        mgr.wait_state("running")
        mgr.manager.report_auto("stationary")
        mgr.wait_state("ready")
        assert len(env.collections()) == 2 and "gps: stationary" in env.metadata()["end"]["reason"]
    finally:
        mgr.stop()


# --------------------------------------------------------------------- radarctl

def test_radarctl_reports_a_manager_that_is_not_running(tmp_path, capsys):
    code = radarctl.main(["--socket", str(tmp_path / "manager.sock"), "status"])
    assert code == 3
    err = capsys.readouterr().err
    assert "the radar manager is not running" in err and "manager.sock" in err
    (tmp_path / "manager.sock").write_text("")              # a stale file is the same
    for command in (["start"], ["stop", "--wait"], ["clear-fault"], ["override", "no-amp-telemetry", "on"]):
        assert radarctl.main(["--socket", str(tmp_path / "manager.sock"), *command]) == 3


def test_radarctl_runs_with_the_system_python_from_any_directory(env, mgr, tmp_path):
    mgr.wait_state("ready")
    script = os.path.join(REPO, "radar_runner", "manager", "radarctl.py")
    run = lambda *args: subprocess.run(["/usr/bin/python3", "-B", "-E", "-s", script, "--socket",
                                        mgr.manager.socket_path, *args], cwd=str(tmp_path),
                                       capture_output=True, text=True, timeout=30)
    out = run("status")
    assert out.returncode == 0 and "state:      ready" in out.stdout and "gates:" in out.stdout
    assert json.loads(run("status", "--json").stdout)["state"] == "ready"
    assert run("start").returncode == 0
    mgr.wait_state("running")
    out = run("stop", "--wait", "--timeout", "10")
    assert out.returncode == 0 and out.stdout.split() == ["stopping", "stopped"]
    assert run("frobnicate").returncode == 2
    wrapper = subprocess.run([os.path.join(REPO, "bin", "radarctl"), "--socket", mgr.manager.socket_path, "status"],
                             cwd=str(tmp_path), capture_output=True, text=True, timeout=30)
    assert wrapper.returncode == 0 and "state:      ready" in wrapper.stdout


def test_stop_wait_times_out_with_an_error(env, mgr):
    env.config["manager"]["amp_safe"]["timeout_s"] = 5.0
    mgr.wait_state("ready")
    env.behave(amp_safe="hang")
    mgr.ctl("start")
    mgr.wait_state("running")
    code, out = mgr.ctl("stop", "--wait", "--timeout", "0.5")
    assert code == 1 and "has not stopped after 0.5 s" in out


def test_socket_is_private_and_a_second_manager_refuses_to_start(env, mgr):
    mgr.wait_state("ready")
    mode = os.stat(mgr.manager.socket_path).st_mode & 0o777
    assert mode == 0o660
    second = Manager(env.config)
    assert second.run() == 1                                # setup fails: the socket is answered
    assert mgr.ctl("status")[0] == 0                        # the first one is untouched


def test_unknown_request_is_refused(env, mgr):
    mgr.wait_state("ready")
    reply = radarctl.request(mgr.manager.socket_path, {"cmd": "self-destruct"})
    assert reply["ok"] is False and "unknown command" in reply["message"]
    reply = radarctl.request(mgr.manager.socket_path, {"cmd": "override", "name": "no-amp-telemetry", "set": "yes"})
    assert reply["ok"] is False


# ------------------------------------------------------------------ as a process

def write_local_config(env):
    def without_empty(value):       # a file cannot set an empty text; the defaults already are
        if isinstance(value, dict):
            return {k: without_empty(v) for k, v in value.items() if v != ""}
        return value

    path = os.path.join(env.tmp, "local.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(without_empty({"paths": env.config["paths"], "manager": env.config["manager"]}), f)
    return path


def start_process(env):
    # mount_point "" cannot be set from a file: check instead that the data root is on a mount.
    env.config["manager"]["gates"]["data"]["mount_point"] = "/"
    local = write_local_config(env)
    proc = subprocess.Popen([sys.executable, "-B", "-u", "-m", "radar_runner.manager", "--local-config", local,
                             "--secrets", os.path.join(env.tmp, "none.yaml")], cwd=REPO,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc


def process_status(env):
    r = S.read_status(os.path.join(env.run_dir, "manager.json"), 5.0)
    return r.data if r.ok else {}


def wait_process_state(env, state, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        env.all_good()
        if process_status(env).get("state") == state:
            return process_status(env)
        time.sleep(0.05)
    raise AssertionError(f"state {state!r} not reached: {process_status(env)}")


def ctl(env, *args):
    return subprocess.run([sys.executable, "-B", "-m", "radar_runner.manager.radarctl", "--socket",
                           os.path.join(env.run_dir, "manager.sock"), *args], cwd=REPO, capture_output=True,
                          text=True, timeout=30)


def test_the_service_as_a_process_stops_the_radar_when_it_is_terminated(env):
    proc = start_process(env)
    try:
        wait_process_state(env, "ready")
        assert ctl(env, "start").returncode == 0
        pid = wait_process_state(env, "running")["radar"]["pid"]
        proc.send_signal(signal.SIGTERM)                    # as systemctl stop does
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                     # the radar is gone
    assert env.amp_safe_calls() == 1 and env.metadata()["end"]["reason"] == "the manager is stopping"
    r = S.read_status(os.path.join(env.run_dir, "manager.json"), 60.0)
    assert r.data["state"] == "stopped" and r.data["idle"] is True
    assert not os.path.exists(os.path.join(env.run_dir, "manager.sock"))
    assert "manager service started" in out and "radar started: pid" in out
    assert ctl(env, "status").returncode == 3


def test_a_killed_manager_does_not_leave_the_radar_running(env):
    proc = start_process(env)
    try:
        wait_process_state(env, "ready")
        assert ctl(env, "start").returncode == 0
        pid = wait_process_state(env, "running")["radar"]["pid"]
        proc.kill()                                         # no chance to clean up
        proc.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            raise AssertionError("the radar survived the death of the manager")
    finally:
        if proc.poll() is None:
            proc.kill()
    # What a crash leaves behind: metadata without an end, data in whole records, and the
    # record "unknown" that tells the unit's stop hook to make the amplifier safe.
    meta = env.metadata()
    assert meta["end"] is None and meta["recording_started"] is not None
    assert env.sdr_state() == "unknown"
    d = env.collection_dir()
    chunks = sorted((f for f in os.listdir(d) if f.startswith("rx_samps.bin.")),
                    key=lambda f: int(f.rsplit(".", 1)[1]))
    assert chunks and all(os.path.getsize(os.path.join(d, c)) == 2 * meta["data"]["record_bytes"]
                          for c in chunks[:-1])
    # The next manager starts normally and can run again.
    env.all_good()
    proc2 = start_process(env)
    try:
        status = wait_process_state(env, "ready")
        # It found the state unknown; fresh telemetry showed shutdown current, so the tool was not needed.
        assert status["sdr_state"] == "safe" and "telemetry shows shutdown current" in status["sdr_state_why"]
        assert env.amp_safe_calls() == 0
        assert ctl(env, "start").returncode == 0
        wait_process_state(env, "running")
        assert ctl(env, "stop", "--wait").returncode == 0
        assert process_status(env)["sdr_state"] == "safe"
    finally:
        proc2.send_signal(signal.SIGTERM)
        proc2.communicate(timeout=30)
    assert proc2.returncode == 0
