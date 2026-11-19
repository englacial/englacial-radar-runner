"""The whole manager service with every stand-in: gates from status files, the stand-in ORCA
tree and reset tool, a mock front panel, the real socket and radarctl. In-process (the service
loop runs in a thread) and as a real process started the way systemd starts it.

No SDR is opened, no real pin is driven, and the real reset tool is never run.
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
from radar_runner.manager.panel import Panel
from radar_runner.manager.service import Manager

REPO = common_config.REPO
TEST_MINIMUMS = {"retry_delay_s": 0.0, "resume_holdoff_s": 0.0, "stable_run_s": 0.0, "min_start_gap_s": 0.0}


class Running:
    """A Manager whose loop runs in a thread, with helpers to drive and observe it."""

    def __init__(self, env, panel=None, minimums=TEST_MINIMUMS):
        self.env = env
        self.manager = Manager(env.config, panel=panel, minimums=minimums)
        self.result = None
        self._feeder_stop = threading.Event()
        self._feeder = threading.Thread(target=self._feed, daemon=True)
        self.feed = {"telemetry", "watcher", "power"}       # the status files kept fresh
        self.amp_current, self.amp_voltage = 0.16, 28.0
        self.sample_age = 0.0                               # how far back the amplifier sample is dated
        self.power = {"state": "normal", "radar_stop_requested": False}
        self._thread = threading.Thread(target=self._run, daemon=True)

    def publish(self):
        env = self.env
        if "telemetry" in self.feed:
            up = time.clock_gettime(time.CLOCK_BOOTTIME) - self.sample_age
            env.publish("telemetry", {"state": "ok",
                                      "amp": {"fresh": True, "reason": "", "current_a": self.amp_current,
                                              "voltage_v": self.amp_voltage, "sample_uptime_s": up},
                                      "loop": {"last_gap_s": 0.25, "recent_longest_gap_s": 0.25}})
        if "watcher" in self.feed:
            env.publish("watcher", {"ok": True, "tripped": False, "reason": ""})
        if "power" in self.feed:
            env.publish("power", self.power)

    def _feed(self):
        while not self._feeder_stop.is_set():
            self.publish()
            time.sleep(0.05)

    def _run(self):
        self.result = self.manager.run()

    def start(self, feed=("telemetry", "watcher", "power")):
        self.feed = set(feed)
        self.publish()
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

    def wait_sdr_safe(self, timeout=10.0):
        """Wait until the record says the pin is safe (at a start it takes one reading after the start)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status().get("sdr_state") == "safe":
                return self.status()
            time.sleep(0.02)
        raise AssertionError(f"the SDR record did not become safe: {self.status().get('sdr_state_why')}")

    def ctl(self, *args):
        """Run radarctl in-process against this manager. Returns (exit status, output)."""
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = radarctl.main(["--socket", self.manager.socket_path, *args])
        return code, out.getvalue()

    def cycle(self):
        """Start, run, stop, and wait until the manager is ready again."""
        assert self.ctl("start")[0] == 0
        self.wait_state("running")
        assert self.ctl("stop", "--wait")[0] == 0
        return self.wait_state("ready")

    def events(self, name=None):
        path = os.path.join(self.env.state_dir, "manager-events.jsonl")
        try:
            with open(path) as f:
                events = [json.loads(line) for line in f]
        except FileNotFoundError:           # the manager has not written its first event yet
            return []
        return events if name is None else [e for e in events if e["event"] == name]

    def stop(self):
        self.manager.request_stop()
        self._thread.join(30)
        self._feeder_stop.set()
        assert not self._thread.is_alive()
        return self.result


@pytest.fixture
def mgr(env):
    running = Running(env).start()
    yield running
    if running._thread.is_alive():
        running.stop()


def wait_for(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{what} not reached")


# ------------------------------------------------------------------ end to end

def test_start_and_stop_by_command_line_end_to_end(env, mgr):
    mgr.wait_state("ready")
    code, out = mgr.ctl("start")
    assert code == 0 and out.strip() == "starting"
    status = mgr.wait_state("running")
    assert status["trigger"] == "command" and status["radar"]["recording"] and status["radar"]["pid"] > 0
    assert status["collection"] == env.collections()[0] and status["idle"] is False
    assert status["radar_gone"] is False and status["sdr_state"] == "unknown"
    assert status["leds"] == {"ready_yellow": "off", "running_green": "on"}
    time.sleep(0.4)
    code, out = mgr.ctl("stop", "--wait")
    assert code == 0 and out.split() == ["stopping", "stopped"]
    status = mgr.wait_state("ready")
    assert status["idle"] is True and status["radar"] == {"active": False} and status["radar_gone"] is True
    # The collection is complete, on the "drive"; the pin is safe after the clean exit, without the tool.
    meta = env.metadata()
    assert meta["trigger"] == "command" and meta["end"]["how"] == "stopped" and meta["end"]["amp_safe"] is True
    assert env.amp_safe_calls() == 0 and status["sdr_state"] == "safe"
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
        mgr.wait_state("ready")
    names = env.collections()
    assert len(names) == 50 and names == sorted(names)
    assert [n[:11] for n in names] == [f"b00001_s{i:03d}" for i in range(1, 51)]
    assert env.amp_safe_calls() == 0                        # fifty clean exits: the tool never ran
    assert not mgr.events("fault")
    assert all(env.metadata(i)["end"]["how"] == "stopped" for i in range(50))
    assert mgr.status()["retries"]["restarts_last_hour"] == 0


def test_status_file_content(env, mgr):
    mgr.wait_state("ready")
    status = mgr.wait_sdr_safe()
    assert list(status["gates"]) == ["telemetry", "watcher", "power", "data", "sdr", "config", "orca", "amp_safe"]
    assert all(g["ok"] for g in status["gates"].values())
    assert status["overrides"] == {"no_amp_telemetry": False} and status["fault"] is None
    assert status["last_fault"] is None
    assert status["retries"] == {"failed_starts": 0, "max_retries": 2, "retry_in_s": None, "interruptions": 0,
                                 "max_resumes": 5, "restarts_last_hour": 0, "max_restarts_per_hour": 12,
                                 "starts_last_hour": 0, "max_starts_per_hour": 600}
    assert status["not_ready"] == [] and status["leds"] == {"ready_yellow": "on", "running_green": "off"}
    assert status["sdr_state"] == "safe" and status["making_amp_safe"] is False
    assert status["amp_enabled_while_idle"] == "" and status["radar_gone"] is True and status["idle"] is True
    assert status["pid"] == os.getpid() and "uptime_s" in status and status["boot_id"]
    assert "config_warnings" not in status


def test_settings_below_their_minimums_are_refused_and_shown(env):
    # env uses retry delays of 0.2 s and a hold-off of 0.2 s: fine for tests, refused by the real minimums.
    mgr = Running(env, minimums=None).start()
    try:
        status = mgr.wait_state("ready")
        warnings = status["config_warnings"]
        assert any("manager.run.retry_delays_s must be 1 to 10 delays of at least 5 s" in w for w in warnings)
        assert any("manager.run.resume_holdoff_s must be at least 2 s" in w for w in warnings)
        assert status["retries"]["max_retries"] == 3        # the default delays (10, 30, 90 s) are used
        assert mgr.manager.logic.cfg.resume_holdoff_s == 10.0
    finally:
        mgr.stop()


# ----------------------------------------------------------------- gates, live

def test_not_ready_reasons_and_recovery(env, mgr):
    mgr.wait_state("ready")
    mgr.feed.discard("watcher")
    time.sleep(0.1)
    env.publish("watcher", {"ok": False, "tripped": True, "reason": "amplifier 56.2 C, limit 55 C"})
    status = mgr.wait_state("not_ready")
    assert status["not_ready"] == ["watcher: safety watcher: amplifier 56.2 C, limit 55 C"]
    assert status["leds"]["ready_yellow"] == "fast_blink"
    code, out = mgr.ctl("start")
    assert code == 1 and "not ready: watcher: safety watcher: amplifier 56.2 C" in out
    mgr.feed.add("watcher")
    mgr.wait_state("ready")


def test_stale_watcher_stops_the_radar_and_it_resumes_when_the_watcher_is_back(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.feed.discard("watcher")                 # the watcher has crashed: its file goes stale
    env.config["manager"]["gates"]["watcher"]["max_age_s"] = 0.3
    status = mgr.wait_state("not_ready")
    assert "safety watcher: status stale" in status["not_ready"][0] and status["run_wanted"]
    assert "safety watcher: status stale" in env.metadata()["end"]["reason"]
    mgr.feed.add("watcher")
    mgr.wait_state("running")
    assert len(env.collections()) == 2 and env.metadata()["resume"] is True
    mgr.ctl("stop", "--wait")


def test_run_is_stopped_by_lost_telemetry_and_resumes_by_itself(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.feed.clear()                            # every status file goes stale
    status = mgr.wait_state("not_ready")
    assert status["run_wanted"] and status["detail"] == "will resume when ready"
    assert env.metadata()["end"]["how"] == "stopped"
    assert "telemetry" in env.metadata()["end"]["reason"]
    mgr.feed.update({"telemetry", "watcher", "power"})
    mgr.wait_state("running")
    assert len(env.collections()) == 2 and env.metadata()["resume"] is True
    status = mgr.status()
    assert status["retries"]["interruptions"] == 1 and status["retries"]["restarts_last_hour"] == 1
    mgr.ctl("stop", "--wait")


def test_power_loss_stops_the_radar_records_safe_without_a_reading_and_resumes_when_power_is_back(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.power = {"state": "loss", "radar_stop_requested": True}
    mgr.amp_voltage, mgr.amp_current = 0.2, 0.0             # the 28 V goes with the main power
    status = mgr.wait_state("power_fail")
    assert status["leds"] == {"ready_yellow": "off", "running_green": "off"}
    assert status["idle"] and status["run_wanted"]
    end = env.metadata()["end"]
    # A clean exit: safe from that alone (the current says nothing without the 28 V rail).
    assert end["amp_safe"] is True and end["amp_safe_command_run"] is False
    assert "clean radar exit; power loss" in end["amp_safe_detail"]
    assert env.sdr_state() == "safe"            # what the power service's amp_safe hook reads: nothing to do
    time.sleep(0.8)                             # several retry periods: the manager does nothing about the SDR now
    assert env.amp_safe_calls() == 0 and mgr.status()["sdr_state"] == "safe" and mgr.status()["fault"] is None
    stop = [e for e in mgr.events("stop")][-1]
    assert stop["urgent"] is True
    mgr.power = {"state": "normal", "radar_stop_requested": False}     # power is back before the shutdown
    mgr.amp_voltage, mgr.amp_current = 28.0, 0.16
    mgr.wait_state("running")
    assert env.amp_safe_calls() == 0
    mgr.ctl("stop", "--wait")


def test_power_hook_stop_returns_when_the_radar_is_gone_and_the_run_resumes(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    t0 = time.monotonic()
    code, out = mgr.ctl("stop", "--interrupt", "--wait", "--timeout", "4")
    assert code == 0 and "stopped" in out and time.monotonic() - t0 < 1.5
    end = env.metadata()["end"] or {}
    mgr.wait_state("running")                   # by itself, after the hold-off
    assert len(env.collections()) == 2
    assert env.metadata(0)["end"]["how"] == "stopped" and "power loss" in env.metadata(0)["end"]["amp_safe_detail"]
    assert end is not None
    mgr.ctl("stop", "--wait")


def test_power_hook_stop_during_the_sdr_set_up_stays_inside_its_budget(env, mgr):
    # The radar has no SIGINT handler yet. The hook does not wait for the start line beyond
    # urgent_grace_s (0.3 s here), and the radar is gone within urgent_stop_budget_s (1.0 s here).
    mgr.wait_state("ready")
    env.behave(init_s=10.0)
    mgr.ctl("start")
    wait_for(lambda: mgr.status().get("radar", {}).get("pid"), what="radar launched")
    t0 = time.monotonic()
    code, out = mgr.ctl("stop", "--interrupt", "--wait", "--timeout", "4")
    took = time.monotonic() - t0
    assert code == 0 and 0.25 < took < 1.6, took
    wait_for(lambda: env.metadata()["end"] is not None)
    meta = env.metadata()
    assert "stopped_before_start" in meta["flags"] and meta["end"]["exit_status"] == -signal.SIGINT
    assert meta["end"]["amp_safe"] is None      # power loss: nothing checked, nothing written
    env.behave()
    mgr.ctl("stop")


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
    env.sdr(speed=480, product="")
    time.sleep(0.2)
    assert "USB 2 link (480 Mbit/s" in mgr.wait_state("not_ready")["not_ready"][0]
    env.sdr(speed=480, product="WestBridge")    # boot loader: the speed says nothing yet
    status = mgr.wait_state("ready")
    assert "firmware not loaded yet" in status["gates"]["sdr"]["note"]


def test_first_start_after_power_up_on_a_usb_2_link_is_caught_after_the_start_line(env, mgr):
    # In its boot loader the SDR passes the gate at 480 Mbit/s. Once the radar has loaded the
    # firmware the speed is read again; here the link is still USB 2.
    env.sdr(speed=480, product="WestBridge")
    mgr.wait_state("ready")
    mgr.ctl("start")
    wait_for(lambda: mgr.events("ended"))
    ended = mgr.events("ended")[0]
    assert ended["kind"] == "usb2" and "the SDR is on a USB 2 link (480 Mbit/s" in ended["failure"]
    assert not any(e["new"] == "running" for e in mgr.events("state"))
    mgr.ctl("stop")


def test_config_change_is_picked_up_by_the_gate(env, mgr):
    mgr.wait_state("ready")
    with open(env.radar_config) as f:
        text = f.read()
    with open(env.radar_config, "w") as f:
        f.write(text.replace("tx_gain: 72.0", "tx_gain: 73.0"))
    status = mgr.wait_state("not_ready")
    assert "RF0.tx_gain 73 is above the cap of 72" in status["not_ready"][0]


def test_tool_time_limits_are_checked_by_a_gate(env, mgr):
    mgr.wait_state("ready")
    env.config["manager"]["amp_safe"]["timeout_s"] = 2.0    # not longer than the tool's own --timeout 2
    status = mgr.wait_state("not_ready")
    assert "must be longer than the tool's --timeout (2 s)" in status["not_ready"][0]


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
    assert status["retries"]["restarts_last_hour"] == 2
    code, out = mgr.ctl("status")
    assert code == 0 and "fault:      start_failures" in out
    # stop does not clear it; clear-fault does, without starting.
    assert mgr.ctl("stop")[0] == 0 and mgr.state() == "fault"
    code, out = mgr.ctl("clear-fault")
    assert code == 0 and out.strip() == "fault cleared"
    status = mgr.wait_state("ready")
    assert status["last_fault"]["kind"] == "start_failures" and status["last_fault"]["cleared_by"] == "command"
    assert len(env.collections()) == 3
    env.behave()
    mgr.cycle()


def test_a_start_request_while_not_ready_does_not_clear_a_fault(env, mgr):
    mgr.wait_state("ready")
    env.behave(build="fail")
    mgr.ctl("start")
    mgr.wait_state("fault", timeout=20)
    env.sdr(present=False)
    wait_for(lambda: mgr.status()["not_ready"])
    for command in ("start", "toggle"):
        code, out = mgr.ctl(command)
        assert code == 1 and "not ready: sdr: SDR not found on USB (fault kept: 3 failed starts in a row" in out
    assert mgr.state() == "fault"
    env.sdr()
    env.behave()
    wait_for(lambda: not mgr.status()["not_ready"])
    assert mgr.ctl("toggle")[0] == 0            # ready again: the press clears the fault and starts
    mgr.wait_state("running")
    mgr.ctl("stop", "--wait")


def test_radar_crash_mid_run_recovers_by_itself(env, mgr):
    mgr.wait_state("ready")
    env.behave(radar="crash")
    mgr.ctl("start")
    mgr.wait_state("running")
    env.behave()                                # the next start behaves
    wait_for(lambda: len(env.collections()) >= 2, timeout=15)
    status = mgr.wait_state("running")
    assert status["retries"]["failed_starts"] == 1 and len(env.collections()) == 2
    assert env.metadata(0)["end"]["how"] == "failed" and env.metadata(0)["end"]["exit_status"] == 3
    # Telemetry showed shutdown current after the crash: the tool was not needed.
    assert env.amp_safe_calls() == 0 and "telemetry: amplifier current 0.16 A" in env.metadata(0)["end"]["amp_safe_detail"]
    mgr.ctl("stop", "--wait")


def test_radar_crash_without_telemetry_runs_the_tool(env):
    env.config["manager"]["gates"]["telemetry"]["require_true"] = []       # keep the gate open without the reading
    mgr = Running(env).start()
    try:
        mgr.wait_state("ready")
        env.behave(radar="crash")
        mgr.sample_age = 3600.0                 # the amplifier reading is never newer than the exit
        mgr.ctl("start")
        mgr.wait_state("running")
        env.behave()
        wait_for(lambda: len(env.collections()) >= 2, timeout=15)
        assert env.amp_safe_calls() == 1 and env.metadata(0)["end"]["amp_safe_command_run"] is True
        mgr.wait_state("running")
        mgr.ctl("stop", "--wait")
    finally:
        mgr.stop()


def test_radar_killed_from_outside_is_noticed_and_restarted(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("start")
    pid = mgr.wait_state("running")["radar"]["pid"]
    os.kill(pid, signal.SIGKILL)
    wait_for(lambda: len(env.collections()) >= 2, timeout=15)
    mgr.wait_state("running")
    assert env.metadata(0)["end"]["exit_status"] == -9
    mgr.ctl("stop", "--wait")


def test_unasked_exit_with_status_0_is_resumed_like_an_interruption(env, mgr):
    mgr.wait_state("ready")
    env.behave(radar="exit0")
    mgr.ctl("start")
    mgr.wait_state("running")
    env.behave()
    wait_for(lambda: len(env.collections()) >= 2, timeout=15)
    status = mgr.wait_state("running")
    assert env.metadata(0)["end"]["how"] == "interrupted" and env.metadata(1)["resume"] is True
    assert status["retries"]["interruptions"] == 1 and status["retries"]["failed_starts"] == 0
    mgr.ctl("stop", "--wait")


def test_restarts_per_hour_are_capped(env):
    env.config["manager"]["run"]["max_restarts_per_hour"] = 2
    mgr = Running(env).start()
    try:
        mgr.wait_state("ready")
        env.behave(radar="exit0")               # runs for a second, then exits: an interruption every time
        mgr.ctl("start")
        status = mgr.wait_state("fault", timeout=30)
        assert status["fault"]["kind"] == "restart_limit"
        assert "2 automatic restarts within an hour (limit 2)" in status["fault"]["reason"]
        assert len(env.collections()) == 3      # the first start and two restarts
    finally:
        mgr.stop()


def test_amp_unsafe_fault_has_its_own_reason_and_clears_itself_when_the_pin_is_safe_again(env, mgr):
    mgr.wait_state("ready")
    env.behave(amp_safe=3)
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.amp_current = 2.5                       # after the exit the amplifier still draws enabled current
    time.sleep(0.2)
    mgr.ctl("stop")
    status = mgr.wait_state("fault")
    assert status["fault"]["kind"] == "amp_unsafe" and "ASSUME_ENABLED" in status["fault"]["reason"]
    assert status["sdr_state"] == "unknown"
    calls = env.amp_safe_calls()
    assert calls >= 2                           # retried at once, then the fault
    # A press does not clear this fault.
    code, out = mgr.ctl("toggle")
    assert code == 1 and "it clears when the amplifier pin is confirmed safe" in out and mgr.state() == "fault"
    # With no radar and the record unknown the tool is tried again every retry_s; now it succeeds.
    env.behave()
    status = mgr.wait_state("not_ready" if False else "ready", timeout=10) if False else None
    wait_for(lambda: mgr.status()["fault"] is None, what="fault cleared")
    status = mgr.status()
    assert status["sdr_state"] == "safe" and env.amp_safe_calls() > calls
    last = status["last_fault"]
    assert last["kind"] == "amp_unsafe" and "ASSUME_ENABLED" in last["reason"]
    assert last["cleared_by"].startswith("the amplifier pin is known safe again")
    cleared = mgr.events("fault_cleared")[-1]
    assert cleared["kind"] == "amp_unsafe" and "ASSUME_ENABLED" in cleared["reason"]


def test_amp_unsafe_fault_also_clears_on_a_qualifying_shutdown_reading(env, mgr):
    mgr.wait_state("ready")
    env.behave(amp_safe=5)                      # the tool cannot run at all
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.amp_current = 2.5
    time.sleep(0.2)
    mgr.ctl("stop")
    assert mgr.wait_state("fault")["fault"]["kind"] == "amp_unsafe"
    mgr.amp_current = 0.16                      # ... and then the current shows shutdown
    wait_for(lambda: mgr.status()["fault"] is None, what="fault cleared")
    assert mgr.status()["sdr_state"] == "safe"
    assert "telemetry shows shutdown current" in mgr.status()["last_fault"]["cleared_by"]


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
    mgr.wait_sdr_safe()
    mgr.feed.discard("telemetry")
    time.sleep(0.1)
    os.unlink(os.path.join(env.run_dir, "telemetry.json"))
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
    mgr.wait_state("ready")
    assert env.metadata()["flags"] == ["no_amp_telemetry"]
    assert env.amp_safe_calls() == 0            # a clean exit is safe also without a reading
    code, out = mgr.ctl("override", "no-amp-telemetry", "off")
    assert code == 0 and out.strip() == "override cleared"
    assert mgr.wait_state("not_ready")["overrides"] == {"no_amp_telemetry": False}
    assert not os.path.exists(os.path.join(env.state_dir, "override-no-amp-telemetry"))


def test_override_does_not_open_the_watcher_gate(env, mgr):
    mgr.wait_state("ready")
    mgr.ctl("override", "no-amp-telemetry", "on")
    mgr.feed.discard("watcher")
    env.config["manager"]["gates"]["watcher"]["max_age_s"] = 0.3
    status = mgr.wait_state("not_ready")
    assert status["not_ready"][0].startswith("watcher: safety watcher: status stale")


def test_override_survives_a_restart_of_the_manager(env):
    first = Running(env).start()
    first.wait_state("ready")
    assert first.ctl("override", "no-amp-telemetry", "on")[0] == 0
    assert first.stop() == 0
    second = Running(env).start()
    try:
        assert second.wait_state("ready")["overrides"] == {"no_amp_telemetry": True}
        started = second.events("manager_started")
        assert len(started) == 2 and started[-1]["override_no_amp_telemetry"] is True
    finally:
        second.stop()


# --------------------------------------- the SDR's state with no radar running

def test_manager_start_with_no_record_and_no_telemetry_runs_the_tool_once(env):
    # No record at all, the SDR has been in use (its firmware is loaded), nothing to read the current from.
    mgr = Running(env).start(feed=("watcher", "power"))
    try:
        wait_for(lambda: mgr.events("amp_safe_done"))
        done = mgr.events("amp_safe_done")[0]
        assert done["ok"] and done["state"] == "safe" and "not known" in done["why"]
        time.sleep(1.0)                                     # several retry periods: no second run
        assert env.amp_safe_calls() == 1 and env.sdr_state() == "safe"
        assert mgr.status()["state"] == "not_ready" and mgr.status()["sdr_state"] == "safe"
    finally:
        mgr.stop()


def test_manager_start_with_the_record_unknown_and_a_good_reading_needs_no_tool(env):
    ampsafe.SdrState(env.state_dir).set("unknown", "radar launched, collection x")
    mgr = Running(env).start()
    try:
        mgr.wait_state("ready")
        status = mgr.wait_sdr_safe()
        assert status["sdr_state"] == "safe" and env.amp_safe_calls() == 0
        marked = mgr.events("sdr_state")
        assert len(marked) == 1 and marked[0]["why"] == "telemetry shows shutdown current with no radar process"
        with open(os.path.join(env.state_dir, "sdr-state")) as f:
            assert f.readline() == "safe\n"
    finally:
        mgr.stop()


def test_a_reading_that_is_not_newer_than_the_last_run_does_not_mark_the_pin_safe(env):
    # sample_uptime_s is when the Pi read the row: a row from before (or during) the last run
    # proves nothing. The tool runs instead.
    env.config["manager"]["amp_safe"]["confirm_settle_s"] = 1.0
    ampsafe.SdrState(env.state_dir).set("unknown", "radar launched, collection x")
    mgr = Running(env)
    mgr.sample_age = 3600.0                     # telemetry is "fresh", but its amplifier row is old
    mgr.start()
    try:
        wait_for(lambda: mgr.events("amp_safe_done"))
        assert not mgr.events("sdr_state") and env.amp_safe_calls() == 1
        assert "no telemetry to decide" in mgr.events("amp_safe_run")[0]["why"]
    finally:
        mgr.stop()


def test_a_reading_counts_only_some_time_after_the_manager_started(env):
    env.config["manager"]["amp_safe"].update(confirm_settle_s=1.0, retry_s=30.0)
    ampsafe.SdrState(env.state_dir).set("unknown", "x")
    env.behave(amp_safe=6)                      # the tool cannot help here: only the reading can
    mgr = Running(env).start()
    try:
        t0 = time.monotonic()
        wait_for(lambda: mgr.status().get("sdr_state") == "safe")
        assert time.monotonic() - t0 >= 0.9     # not on the first reading: one read 1 s after the start
        assert mgr.events("sdr_state")[0]["why"].startswith("telemetry shows shutdown current")
    finally:
        mgr.stop()


def test_tool_ok_with_an_unwritable_record_is_not_asked_for_again_and_the_problem_is_shown(env):
    # The wrapper returns 0 but could not write the record: without a memory of it the manager
    # would run the tool (and enable the amplifier for half a second) every retry_s for ever.
    env.behave(amp_safe="ok_unwritten")
    mgr = Running(env).start(feed=("watcher", "power"))
    try:
        wait_for(lambda: mgr.events("amp_safe_done"))
        time.sleep(1.2)                                     # four retry periods
        assert env.amp_safe_calls() == 1
        status = mgr.status()
        assert status["sdr_state"] == "safe" and "kept in memory only" in status["sdr_state_problem"]
        assert env.sdr_state() == "unknown"                 # the file itself
        code, out = mgr.ctl("status")
        assert "sdr:        PROBLEM: the record" in out
    finally:
        mgr.stop()


def test_a_tool_run_of_another_program_keeps_the_manager_from_marking_the_record_safe(env):
    import fcntl
    lock = os.path.join(env.tmp, "amp_safe_lock")
    open(lock, "w").close()
    env.config["manager"]["amp_safe"]["lock_file"] = lock
    ampsafe.SdrState(env.state_dir).set("unknown", "amp_safe: sdr_gpio_safe is starting")
    holder = os.open(lock, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    mgr = Running(env).start()                              # a good reading: shutdown current
    try:
        mgr.wait_state("ready")
        time.sleep(0.8)
        assert env.sdr_state() == "unknown" and mgr.events("sdr_state") == []
        os.close(holder)                                    # the other run is over (it failed to record)
        holder = None
        mgr.wait_sdr_safe()
        assert env.amp_safe_calls() == 0
    finally:
        if holder is not None:
            os.close(holder)
        mgr.stop()


def test_sdr_still_in_its_boot_loader_is_safe_without_the_tool(env):
    # After a power cycle a record left "unknown" by a hard power cut is stale.
    ampsafe.SdrState(env.state_dir).set("unknown", "radar launched, collection x")
    env.sdr(speed=480, product="WestBridge")
    mgr = Running(env).start(feed=("watcher", "power"))
    try:
        wait_for(lambda: mgr.events("sdr_state"))
        assert mgr.events("sdr_state")[0]["why"] == "nothing has initialised the SDR since it was powered"
        assert env.amp_safe_calls() == 0 and env.sdr_state() == "safe"
    finally:
        mgr.stop()


def test_a_tool_that_cannot_run_is_retried_without_a_fault(env):
    env.behave(amp_safe=6)                                  # BUSY: the SDR is in use by something else
    mgr = Running(env).start(feed=("watcher", "power"))
    try:
        wait_for(lambda: env.amp_safe_calls() >= 3)         # every retry_s (0.3 s here)
        assert env.sdr_state() == "unknown" and mgr.status()["fault"] is None
        assert all(not e["ok"] and e["status"] == 6 for e in mgr.events("amp_safe_done"))
        env.behave()
        wait_for(lambda: env.sdr_state() == "safe")
        calls = env.amp_safe_calls()
        time.sleep(0.8)
        assert env.amp_safe_calls() == calls                # and then it is left alone
    finally:
        mgr.stop()


def test_assume_enabled_with_no_radar_running_is_a_fault(env):
    env.behave(amp_safe=3)
    mgr = Running(env).start(feed=("watcher", "power"))
    try:
        status = mgr.wait_state("fault")
        assert status["fault"]["kind"] == "amp_unsafe" and "ASSUME_ENABLED" in status["fault"]["reason"]
        assert env.amp_safe_calls() >= 2 and status["sdr_state"] == "unknown"
        assert len(mgr.events("fault")) == 1
    finally:
        mgr.stop()


def test_enabled_current_with_no_radar_process_is_shown_and_left_to_the_watcher(env, mgr):
    mgr.wait_state("ready")
    mgr.wait_sdr_safe()
    mgr.amp_current = 2.5                                   # the amplifier is enabled, and nothing is running
    wait_for(lambda: mgr.status()["amp_enabled_while_idle"])
    time.sleep(1.0)                                         # several retry periods
    status = mgr.status()
    assert status["amp_enabled_while_idle"] == "amplifier current 2.50 A at 28.0 V"
    assert env.amp_safe_calls() == 0 and status["sdr_state"] == "safe" and status["fault"] is None
    assert "AMPLIFIER ENABLED WITH NO RADAR RUNNING" in mgr.ctl("status")[1]
    mgr.amp_current = 0.16
    wait_for(lambda: mgr.status()["amp_enabled_while_idle"] == "")


def test_enabled_current_keeps_an_unknown_record_from_being_marked_safe(env):
    ampsafe.SdrState(env.state_dir).set("unknown", "x")
    env.sdr(speed=480, product="WestBridge")    # even in the boot loader: the current comes first
    mgr = Running(env)
    mgr.amp_current = 2.5
    mgr.start()
    try:
        wait_for(lambda: mgr.status().get("amp_enabled_while_idle"))
        time.sleep(0.6)
        assert env.sdr_state() == "unknown" and not mgr.events("sdr_state") and env.amp_safe_calls() == 0
    finally:
        mgr.stop()


def test_no_tool_run_in_normal_operation(env, mgr):
    mgr.wait_state("ready")
    for _ in range(3):
        mgr.cycle()
    assert env.amp_safe_calls() == 0 and mgr.status()["sdr_state"] == "safe"
    # A radar that has to be killed, with no reading to decide: now the tool runs.
    env.config["manager"]["orca"]["stop_timeout_s"] = 0.3
    env.behave(radar="ignore_sigint")
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.sample_age = 3600.0
    mgr.ctl("stop", "--wait")
    wait_for(lambda: env.metadata()["end"] is not None)
    assert env.metadata()["end"]["killed"] is True and env.metadata()["end"]["amp_safe_command_run"] is True
    assert env.amp_safe_calls() == 1 and env.sdr_state() == "safe"


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
        mgr.feed.clear()
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
        status = mgr.wait_state("ready")
        assert status["auto"]["blocked"] and len(env.collections()) == 1
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
    out = run("stop", "--interrupt", "--wait", "--timeout", "4")       # as the power service's hook calls it
    assert out.returncode == 0 and out.stdout.split() == ["stopping", "stopped"]
    assert run("frobnicate").returncode == 2
    mgr.ctl("stop")
    wrapper = subprocess.run([os.path.join(REPO, "bin", "radarctl"), "--socket", mgr.manager.socket_path, "status"],
                             cwd=str(tmp_path), capture_output=True, text=True, timeout=30)
    assert wrapper.returncode == 0 and "state:" in wrapper.stdout


def test_stop_wait_times_out_with_an_error_inside_its_timeout(env, mgr):
    env.config["manager"]["orca"]["stop_timeout_s"] = 30.0
    mgr.wait_state("ready")
    env.behave(radar="ignore_sigint")
    mgr.ctl("start")
    mgr.wait_state("running")
    t0 = time.monotonic()
    code, out = mgr.ctl("stop", "--wait", "--timeout", "0.6")       # not urgent: the radar gets its 30 s
    assert code == 1 and "has not stopped after 0.6 s" in out and time.monotonic() - t0 < 1.5
    code, out = mgr.ctl("stop", "--interrupt", "--wait", "--timeout", "4")     # urgent: gone within the budget
    assert code == 0 and time.monotonic() - t0 < 4.0


def test_status_request_asks_for_radar_gone_now_not_from_the_last_snapshot(env, mgr):
    mgr.wait_state("ready")
    manager = mgr.manager
    stale = dict(manager._status, radar_gone=False, marker="old snapshot")

    class Frozen:                               # the service loop is busy: the snapshot stays as it is
        def __get__(self, obj, cls):
            return stale

        def __set__(self, obj, value):
            pass
    original = type(manager).__dict__.get("_status")
    type(manager)._status = Frozen()
    try:
        reply = manager._handle_request({"cmd": "status"})
        assert reply["status"]["marker"] == "old snapshot" and reply["status"]["radar_gone"] is True
        manager.runner.radar_gone = lambda: False
        assert manager._handle_request({"cmd": "status"})["status"]["radar_gone"] is False
        assert stale["radar_gone"] is False     # the snapshot itself is not changed from the socket thread
    finally:
        del manager.runner.radar_gone
        if original is None:
            del type(manager)._status
        else:
            type(manager)._status = original


def test_stop_wait_does_not_wait_for_the_tool(env, mgr):
    env.config["manager"]["amp_safe"]["timeout_s"] = 4.0
    mgr.wait_state("ready")
    env.behave(radar="ignore_sigint", amp_safe="hang")
    env.config["manager"]["orca"]["stop_timeout_s"] = 0.3
    mgr.ctl("start")
    mgr.wait_state("running")
    mgr.sample_age = 3600.0                     # no reading to decide: the tool will run, and hang
    t0 = time.monotonic()
    code, out = mgr.ctl("stop", "--wait", "--timeout", "3")
    assert code == 0 and time.monotonic() - t0 < 1.5
    status = json.loads(mgr.ctl("status", "--json")[1])     # asked now; the status file follows within a round
    assert status["radar_gone"] is True and status["idle"] is False     # the amplifier step is still going


def test_socket_is_private_and_a_second_manager_refuses_to_start(env, mgr):
    mgr.wait_state("ready")
    mode = os.stat(mgr.manager.socket_path).st_mode & 0o777
    assert mode == 0o660
    second = Manager(env.config, minimums=TEST_MINIMUMS)
    assert second.run() == 1                                # setup fails: the socket is answered
    assert mgr.ctl("status")[0] == 0                        # the first one is untouched


def test_unknown_request_is_refused(env, mgr):
    mgr.wait_state("ready")
    reply = radarctl.request(mgr.manager.socket_path, {"cmd": "self-destruct"})
    assert reply["ok"] is False and "unknown command" in reply["message"]
    reply = radarctl.request(mgr.manager.socket_path, {"cmd": "override", "name": "no-amp-telemetry", "set": "yes"})
    assert reply["ok"] is False


def test_a_silent_client_does_not_delay_the_stop_command(env, mgr):
    import socket
    mgr.wait_state("ready")
    mgr.ctl("start")
    mgr.wait_state("running")
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.connect(mgr.manager.socket_path)
    try:
        t0 = time.monotonic()
        assert mgr.ctl("stop", "--interrupt", "--wait", "--timeout", "4")[0] == 0
        assert time.monotonic() - t0 < 1.5
    finally:
        silent.close()
    mgr.ctl("stop")


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


def wait_process_state(env, state, timeout=15.0, feed=True):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if feed:
            env.all_good()
        if process_status(env).get("state") == state:
            return process_status(env)
        time.sleep(0.05)
    raise AssertionError(f"state {state!r} not reached: {process_status(env)}")


def ctl(env, *args):
    return subprocess.run([sys.executable, "-B", "-m", "radar_runner.manager.radarctl", "--socket",
                           os.path.join(env.run_dir, "manager.sock"), *args], cwd=REPO, capture_output=True,
                          text=True, timeout=30)


def gone(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


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
    assert gone(pid, 1.0)                                   # the radar is gone
    assert env.metadata()["end"]["reason"] == "the manager is stopping" and env.sdr_state() == "safe"
    r = S.read_status(os.path.join(env.run_dir, "manager.json"), 60.0)
    assert r.data["state"] == "stopped" and r.data["idle"] is True
    assert not os.path.exists(os.path.join(env.run_dir, "manager.sock"))
    assert "manager service started" in out and "radar started: pid" in out
    assert "the default is used" in out                     # the test's short delays are refused by the real minimums
    assert ctl(env, "status").returncode == 3


def test_a_killed_manager_does_not_leave_the_radar_running(env):
    proc = start_process(env)
    try:
        wait_process_state(env, "ready")
        assert ctl(env, "start").returncode == 0
        pid = wait_process_state(env, "running")["radar"]["pid"]
        proc.kill()                                         # no chance to clean up
        proc.wait(10)
        if not gone(pid):
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
        wait_process_state(env, "ready")
        deadline = time.monotonic() + 15    # the real settle time: a reading read 2 s after the manager's start
        while process_status(env).get("sdr_state") != "safe" and time.monotonic() < deadline:
            env.all_good()
            time.sleep(0.05)
        status = process_status(env)
        # It found the record unknown; a reading newer than its own start showed shutdown current.
        assert status["sdr_state"] == "safe" and "telemetry shows shutdown current" in status["sdr_state_why"]
        assert env.amp_safe_calls() == 0
        assert ctl(env, "start").returncode == 0
        wait_process_state(env, "running")
        assert ctl(env, "stop", "--wait").returncode == 0
        wait_process_state(env, "ready")
        assert process_status(env)["sdr_state"] == "safe"
    finally:
        proc2.send_signal(signal.SIGTERM)
        proc2.communicate(timeout=30)
    assert proc2.returncode == 0


def test_manager_killed_during_a_tool_run_with_no_radar(env):
    # No telemetry, no record: the manager runs the tool at its start. The tool hangs and the
    # manager is killed. The record says unknown (the tool wrote that before it started), the
    # unit's stop hook would run the tool, and the next manager tries again.
    env.config["manager"]["amp_safe"].update(timeout_s=30.0, command=env.config["manager"]["amp_safe"]["command"])
    env.behave(amp_safe="hang")
    proc = start_process(env)
    try:
        deadline = time.monotonic() + 15
        while env.amp_safe_calls() < 1 and time.monotonic() < deadline:
            env.publish("watcher", {"ok": True, "tripped": False})
            env.publish("power", {"state": "normal", "radar_stop_requested": False})
            time.sleep(0.05)
        assert env.amp_safe_calls() == 1
        time.sleep(0.3)
        assert process_status(env)["making_amp_safe"] is True and process_status(env)["idle"] is False
        proc.kill()
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
    with open(os.path.join(env.state_dir, "sdr-state")) as f:
        lines = f.read().splitlines()
    assert lines[0] == "unknown" and "amp_safe started" in lines[1]
    # The stop hook (ExecStopPost), with a stand-in for the tool: it runs it.
    tool = os.path.join(env.tmp, "hook_tool")
    with open(tool, "w") as f:
        f.write(f'#!/bin/sh\necho "$@" > {env.tmp}/hook_call\nexit 6\n')       # BUSY: the old tool still holds the SDR
    os.chmod(tool, 0o755)
    hook = subprocess.run([os.path.join(REPO, "bin", "radar-manager-stop-hook"),
                           os.path.join(env.state_dir, "sdr-state"), tool, env.sysfs], timeout=20)
    assert hook.returncode == 6 and "--state-file" in open(os.path.join(env.tmp, "hook_call")).read()
    # The next manager finds the record unknown and runs the tool again; this time it works.
    subprocess.run(["pkill", "-f", os.path.join(env.orca, "amp_safe.py")], check=False)    # the hung stand-in
    env.behave()
    proc2 = start_process(env)
    try:
        deadline = time.monotonic() + 15
        while env.sdr_state() != "safe" and time.monotonic() < deadline:
            env.publish("watcher", {"ok": True, "tripped": False})
            env.publish("power", {"state": "normal", "radar_stop_requested": False})
            time.sleep(0.05)
        assert env.sdr_state() == "safe" and env.amp_safe_calls() == 2
    finally:
        proc2.send_signal(signal.SIGTERM)
        proc2.communicate(timeout=30)
    assert proc2.returncode == 0


# ------------------------------------------------------------- the real default timings

def test_with_the_default_timings_nothing_is_shrunk(env):
    """One service run with every timing at its shipped default and the real minimums.

    The other tests shorten the delays; this one shows the behaviour a unit really has:
    a stop, the 5 s pause before the next start, a crash and its 10 s retry delay, the 2 s
    settle time of the amplifier reading, and an urgent stop inside its 3 s budget.
    """
    defaults = common_config.DEFAULTS["manager"]
    m = env.config["manager"]
    for key in ("tick_s", "gate_interval_s", "status_interval_s"):
        m[key] = defaults[key]
    m["run"] = dict(defaults["run"])
    for key in ("start_timeout_s", "stop_timeout_s", "urgent_grace_s", "urgent_stop_budget_s",
                "chirp_timeout_s", "build_timeout_s"):
        m["orca"][key] = defaults["orca"][key]
    for key in ("timeout_s", "assume_enabled_retries", "unavailable_retries", "unavailable_delay_s", "retry_s",
                "confirm_settle_s", "confirm_timeout_s", "max_loop_gap_s"):
        m["amp_safe"][key] = defaults["amp_safe"][key]
    m["amp_safe"]["command"] = m["amp_safe"]["command"][:-1] + ["30"]       # --timeout 30, as shipped
    m["amp_safe"].pop("idle_enabled_s", None)
    mgr = Running(env, minimums=None).start()               # the real minimums: nothing is refused
    try:
        mgr.wait_state("ready", timeout=15)                 # the first reading must be 2 s newer than the start
        assert "config_warnings" not in mgr.status()
        retries = mgr.status()["retries"]
        assert retries["max_restarts_per_hour"] == 12 and retries["max_starts_per_hour"] == 40

        # A run, a stop: the reading that marks the pin safe is waited for (settle 2 s), no tool.
        assert mgr.ctl("start")[0] == 0
        mgr.wait_state("running", timeout=15)
        t_stop = time.monotonic()
        assert mgr.ctl("stop", "--wait")[0] == 0
        wait_for(lambda: mgr.status().get("idle"), timeout=15)
        assert 1.5 < time.monotonic() - t_stop < 8.0        # confirm_settle_s, not the tests' zero
        t_end = time.monotonic()
        assert "tool not run: telemetry: amplifier current 0.16 A" in env.metadata()["end"]["amp_safe_detail"]

        # Start again at once: accepted, but the SDR is not started within min_start_gap_s (5 s).
        assert mgr.ctl("start")[0] == 0
        assert "start in" in mgr.wait_state("starting")["detail"]
        time.sleep(2.5)
        assert len(env.collections()) == 1
        mgr.wait_state("running", timeout=15)
        assert time.monotonic() - t_end >= 4.5

        # The radar crashes: the retry comes after retry_delays_s[0] (10 s), not sooner.
        env.behave(radar="crash")
        os.kill(mgr.status()["radar"]["pid"], signal.SIGKILL)
        wait_for(lambda: mgr.events("retry"), timeout=15)
        t_retry = time.monotonic()
        assert mgr.events("retry")[-1]["delay_s"] == 10.0
        env.behave()
        time.sleep(7.0)
        assert len(env.collections()) == 2 and mgr.state() == "starting"
        mgr.wait_state("running", timeout=15)
        assert time.monotonic() - t_retry >= 9.0
        assert mgr.status()["retries"]["restarts_last_hour"] == 1 and mgr.status()["retries"]["starts_last_hour"] == 3

        # The power service's stop hook: inside its 4 s with the default budget of 3 s.
        t0 = time.monotonic()
        code, out = mgr.ctl("stop", "--interrupt", "--wait", "--timeout", "4")
        assert code == 0 and time.monotonic() - t0 < 3.5
        assert env.sdr_state() == "safe" and env.amp_safe_calls() <= 1      # at most the one after the kill
        assert mgr.ctl("stop")[0] == 0                      # and do not resume
        wait_for(lambda: mgr.status().get("idle"), timeout=15)
        assert mgr.status()["fault"] is None
    finally:
        mgr.stop()
