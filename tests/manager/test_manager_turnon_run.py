"""The turn-on check run by the runner and the service, against the stand-in ORCA tree.

Real processes and real time: the stand-in radar for both runs, a stand-in analysis
(fake_orca/fake_measure.py), the stand-in reset tool, and a stand-in telemetry status
whose amplifier current rises while the stand-in radar sends pulses. One test runs the
real analysis (tools/turn_on_check/measure.py, with ORCA's Python) on recorded loopback
records. No SDR is opened.
"""
import json
import os
import sys
import threading
import time

import pytest
import yaml

from radar_runner.common import config as common_config
from radar_runner.manager import turnon
from radar_runner.manager.runner import Runner

from test_manager_runner import REQUEST, Telemetry
from test_manager_service import Running

HERE = os.path.dirname(os.path.abspath(__file__))
RECORDS = os.path.join(HERE, "..", "turn_on_check", "fixtures")
ORCA_PYTHON = "/home/englacial/miniforge3/envs/orca/bin/python"
MEASURE = os.path.join(common_config.REPO, "tools", "turn_on_check", "measure.py")
VERIFIED = dict(verified=True, leakage_sample=78.4, delay_samples=0.0, level_tx_gain=70.0, min_level_db=-12.0)


class Feed(threading.Thread):
    """The telemetry status: 0.288 A while the stand-in radar sends pulses (its tx_file exists), else 0.156 A."""

    def __init__(self, env, tx_file, interval_s=0.05):
        super().__init__(daemon=True)
        self.env, self.tx_file, self.halt = env, tx_file, threading.Event()
        self.interval_s = interval_s
        self.idle, self.on = 0.156, 0.288

    def run(self):
        while not self.halt.is_set():
            current = self.on if os.path.exists(self.tx_file) else self.idle
            up = time.clock_gettime(time.CLOCK_BOOTTIME)
            self.env.publish("telemetry", {"state": "ok", "amp": {
                "fresh": True, "reason": "", "current_a": current, "voltage_v": 28.0, "sample_uptime_s": up,
                "temp_c": 27.6, "case_temp_c": 19.2}, "loop": {"last_gap_s": 0.25, "recent_longest_gap_s": 0.25}})
            time.sleep(self.interval_s)


def setup_check(env, real=False, time_offset=0, **changes):
    """Enable the check in env's config; the stand-in radar at real speed. Returns the tx_file."""
    t = env.config["manager"]["turn_on_check"]
    t.update(enabled=True, pulses=8000, analysis_timeout_s=5.0,      # 1.6 s of pulses
             analysis_command=[ORCA_PYTHON, "-I", MEASURE] if real else
             [sys.executable, os.path.join(env.orca, "fake_measure.py")])
    for key, value in changes.items():
        (t["expected"] if key in t["expected"] else t)[key] = value
    m = env.config["manager"]
    m["orca"]["env"] = [e for e in m["orca"]["env"] if not e.startswith("FAKE_RADAR_SPEEDUP")]
    with open(env.radar_config) as f:
        radar = yaml.safe_load(f)
    radar["CHIRP"]["time_offset"] = time_offset
    with open(env.radar_config, "w") as f:
        yaml.safe_dump(radar, f)
    return os.path.join(env.tmp, "transmitting")


@pytest.fixture
def check_env(env):
    feeds = []

    def make(events, behave=None, telemetry=None, feed=True, real=False, time_offset=0, feed_s=0.05, **changes):
        tx_file = setup_check(env, real=real, time_offset=time_offset, **changes)
        env.behave(tx_file=tx_file, pulse_delay_s=time_offset, **(behave or {}))
        if feed:
            f = Feed(env, tx_file, feed_s)
            f.start()
            feeds.append(f)
        s, warnings = turnon.Settings.from_config(env.config)
        assert warnings == [] or changes.get("blocking")
        check = turnon.TurnOnCheck(env.config, s)
        r = Runner(env.config, events, turn_on_check=check, amp_telemetry=telemetry or Telemetry())
        check.runner = r
        runners.append(r)
        return r
    runners = []
    yield make
    for r in runners:
        r.shutdown(10.0)
    for f in feeds:
        f.halt.set()


def check_dir(env):
    return os.path.join(env.collection_dir(), turnon.SUBDIR)


def result(env):
    with open(os.path.join(check_dir(env), turnon.RESULT_FILE)) as f:
        return json.load(f)


def data_in_root(env):
    return [f for f in os.listdir(env.collection_dir()) if f.startswith("rx_samps.bin")]


# ------------------------------------------------------------------ a check that passes

def test_the_check_runs_before_the_radar_and_is_kept_with_the_collection(env, events, check_env):
    r = check_env(events)
    r.start(REQUEST)
    events.wait("started")
    time.sleep(0.2)
    r.stop("test stop")
    info = events.wait("ended")
    assert events.names() == ["collection", "turn_on_check", "started", "ended"]
    assert info["failure"] == "" and info["launched"] and info["amp_safe"] is True and env.amp_safe_calls() == 0
    summary = events.wait("turn_on_check")
    assert summary["passed"] is True and summary["blocking"] is False and summary["blocked"] is False
    assert summary["text"].startswith("passed: strongest peak at sample 78.4, -36.0 dB at tx_gain 40 (-4.0 dB at")
    assert summary["text"].endswith("not judged: the expected peak is not set")
    assert summary["low_power"] == {"tx_gain": 40.0, "requested_tx_gain": 72.0, "pulses": 8000}
    assert [c["ok"] for c in summary["criteria"]] == [True, True, True, True, None]
    # The low-power data is in its own directory, under a name of its own.
    d = check_dir(env)
    names = set(os.listdir(d))
    assert {"config.yaml", "chirp.bin", "low_power.bin.0", "uhd_stdout.log", "uhd_stderr.log", "gps_log.txt",
            "analysis_input.json", "analysis.json", "analysis.log", "result.json"} <= names
    assert os.path.getsize(os.path.join(d, "low_power.bin.0")) == 8 * 2352 * 8
    with open(os.path.join(d, "config.yaml")) as f:
        lp = yaml.safe_load(f)
    with open(os.path.join(env.collection_dir(), "config.yaml")) as f:
        full = yaml.safe_load(f)
    assert lp["RF0"]["tx_gain"] == 40.0 and full["RF0"]["tx_gain"] == 72.0 and lp["CHIRP"]["num_pulses"] == 8000
    assert lp["GENERATE"] == full["GENERATE"] and lp["CHIRP"]["tx_duration"] == full["CHIRP"]["tx_duration"]
    with open(os.path.join(d, "chirp.bin"), "rb") as a, open(os.path.join(env.collection_dir(), "chirp.bin"), "rb") as b:
        assert a.read() == b.read()
    # The collection itself: the data at full power in its root, the check's summary in collection.json.
    assert data_in_root(env) and "low_power.bin.0" not in os.listdir(env.collection_dir())
    meta = env.metadata()
    assert meta["turn_on_check"]["passed"] is True and "turn_on_check_failed" not in meta["flags"]
    res = result(env)
    assert res["radar"]["exit_status"] == 0 and res["radar"]["amp_safe"] is True
    assert len(res["readings"]["during"]) >= 4 and res["measured"]["current_max_a"] == 0.288
    assert res["measured"]["idle_current_a"] == 0.156 and env.sdr_state() == "safe"


def test_without_the_check_nothing_of_it_happens(env, events, runner):
    env.behave()
    runner.start(REQUEST)
    events.wait("started")
    runner.stop("test stop")
    events.wait("ended")
    assert events.names() == ["collection", "started", "ended"]
    assert not os.path.exists(check_dir(env)) and "turn_on_check" not in env.metadata()


@pytest.fixture
def runner(env, events):
    r = Runner(env.config, events, amp_telemetry=Telemetry())
    yield r
    r.shutdown(10.0)


# ------------------------------------------------------------------ a check that fails

def test_a_blocking_check_that_fails_keeps_the_radar_off(env, events, check_env):
    r = check_env(events, behave={"analysis": "absent"}, blocking=True, **VERIFIED)
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "turn_on_check" and info["fatal"] and not info["launched"]
    assert info["failure"].startswith("turn-on check failed: no combined peak at sample 78.4 +/- 1.5")
    assert "the next button press or trigger tries again" in info["failure"]
    assert "started" not in events.names() and data_in_root(env) == []
    summary = events.wait("turn_on_check")
    assert summary["passed"] is False and summary["blocked"] is True
    assert env.metadata()["end"]["how"] == "failed" and env.sdr_state() == "safe" and env.amp_safe_calls() == 0


def test_a_check_that_fails_while_only_recording_lets_the_radar_start_and_flags_the_collection(env, events, check_env):
    r = check_env(events, behave={"analysis": "absent"}, **VERIFIED)
    r.start(REQUEST)
    events.wait("started")
    r.stop("test stop")
    info = events.wait("ended")
    assert info["failure"] == ""
    summary = events.wait("turn_on_check")
    assert summary["passed"] is False and summary["blocked"] is False
    assert summary["text"].startswith("FAILED (record only, the radar starts anyway): no combined peak")
    assert "turn_on_check_failed" in env.metadata()["flags"]


def test_amplifier_current_that_does_not_rise_fails_the_check(env, events, check_env):
    r = check_env(events, blocking=True, **VERIFIED)
    env.behave(tx_file=os.path.join(env.tmp, "elsewhere"))     # the stand-in telemetry never sees the pulses
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "turn_on_check" and "amplifier current did not rise" in info["failure"]
    # An amplifier matter before the antenna is judged: a failed start, retried, not the latched fault.
    assert not info["fatal"] and "the start is tried again" in events.wait("turn_on_check")["text"]


def test_the_default_pulse_count_gives_readings_at_the_real_telemetry_rate(env, events, check_env):
    # As on the unit: CHIRP.time_offset before the first pulse, one telemetry status every 0.5 s.
    pulses = common_config.DEFAULTS["manager"]["turn_on_check"]["pulses"]
    r = check_env(events, blocking=True, time_offset=0.5, feed_s=0.5, pulses=pulses, **VERIFIED)
    r.start(REQUEST)
    events.wait("started", timeout=15.0)
    r.stop("test stop")
    events.wait("ended")
    summary = events.wait("turn_on_check")
    assert summary["passed"] is True and [c["ok"] for c in summary["criteria"]][:2] == [True, True]
    assert summary["measured"]["current_readings"] >= 4 and summary["measured"]["current_max_a"] == 0.288


def test_too_few_readings_during_short_pulses_is_not_judged_and_does_not_block(env, events, check_env):
    # 0.8 s of pulses after a 0.5 s time_offset, telemetry at 2 Hz: at most one reading in the window.
    r = check_env(events, blocking=True, time_offset=0.5, feed_s=0.5, pulses=4000, **VERIFIED)
    r.start(REQUEST)
    events.wait("started", timeout=15.0)
    r.stop("test stop")
    assert events.wait("ended")["failure"] == ""
    summary = events.wait("turn_on_check")
    during = next(c for c in summary["criteria"] if c["name"] == "amplifier_during")
    assert summary["passed"] is True and during["ok"] is None
    assert any("amplifier readings during the pulses" in w for w in summary["warnings"])
    assert any("too short for 2 amplifier readings" in w for w in summary["warnings"])
    assert "warning:" in summary["text"]


def test_the_readings_window_ends_at_the_check_radars_own_exit(env, events, check_env):
    # A reading after the exit says "enabled": the reset tool runs (1 s) after the low-power radar. The
    # readings in that time (idle current, no pulses) must not count as "during the pulses".
    env.config["manager"]["amp_safe"]["confirm_settle_s"] = 0.0
    r = check_env(events, telemetry=Telemetry(state=False, text="amplifier current 2.5 A"),
                  behave={"amp_safe_s": 1.0})
    r.start(REQUEST)
    events.wait("started", timeout=15.0)
    r.stop("test stop")
    events.wait("ended", timeout=15.0)
    res = result(env)
    assert res["radar"]["amp_safe_command_run"] is True
    assert res["readings"]["during"] and all(x["current_a"] == 0.288 for x in res["readings"]["during"])


def test_the_amplifier_step_of_the_check_radar_reaches_the_run_after_an_internal_error(env, events, check_env):
    r = check_env(events, behave={"radar_check": "fail_start", "amp_safe": 5})
    real = r._radar

    def radar_then_bug(run, argv, cwd):
        real(run, argv, cwd)
        if run.parent is not None:
            raise ZeroDivisionError("bug after the check radar")
    r._radar = radar_then_bug
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "internal" and info["amp_safe"] is False and not info["launched"]


def test_amplifier_telemetry_missing_with_the_override_is_not_judged(env, events, check_env):
    r = check_env(events, feed=False, **VERIFIED)
    with open(os.path.join(env.state_dir, "override-no-amp-telemetry"), "w") as f:
        f.write("test\n")
    r.start(REQUEST)
    events.wait("started")
    r.stop("test stop")
    events.wait("ended")
    summary = events.wait("turn_on_check")
    assert summary["passed"] is True
    assert [c["ok"] for c in summary["criteria"]][:2] == [None, None]


# ------------------------------------------------------- the low-power radar misbehaves

@pytest.mark.parametrize("mode,kind,pulses", [("fail_start", "exit", 4000),
                                              ("crash", "exit", 20000),      # it crashes 1 s after the start line
                                              ("no_start", "start_timeout", 4000)])
def test_a_low_power_radar_that_fails_is_a_failed_start_and_the_amplifier_step_runs(env, events, check_env,
                                                                                     mode, kind, pulses):
    r = check_env(events, behave={"radar_check": mode}, blocking=True, pulses=pulses, **VERIFIED)
    r.start(REQUEST)
    info = events.wait("ended", timeout=15.0)
    assert info["kind"] == kind and not info["fatal"] and not info["launched"]     # retried, as any failed start
    assert info["failure"].startswith("turn-on check, low-power radar: ")
    assert env.amp_safe_calls() >= 1                  # not a clean exit: the tool ran, as after any radar
    assert data_in_root(env) == [] and result(env)["radar"]["amp_safe_command_run"] is True


def test_a_low_power_radar_that_does_not_finish_is_stopped(env, events, check_env, monkeypatch):
    monkeypatch.setattr(turnon, "RUN_MARGIN_S", 0.5)
    r = check_env(events, behave={"radar_check": "hang"})
    t0 = time.monotonic()
    r.start(REQUEST)
    info = events.wait("ended", timeout=15.0)
    assert info["kind"] == "check_timeout" and "had not finished" in info["failure"] and not info["launched"]
    assert time.monotonic() - t0 < 6.0


def test_the_amplifier_not_confirmed_safe_after_the_low_power_radar_is_amp_unsafe(env, events, check_env):
    r = check_env(events, behave={"radar_check": "fail_start", "amp_safe": 5})
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["amp_safe"] is False and "after the turn-on check's radar" in info["amp_safe_detail"]
    assert not info["launched"]


@pytest.mark.parametrize("mode,words,crit", [("no_data", "no complete record", "analysis"),
                                             ("short_data", "4 records of 8", "data")])
def test_too_little_data_fails_the_check(env, events, check_env, mode, words, crit):
    r = check_env(events, behave={"radar_check": mode, "analysis": "count_data"}, blocking=True, **VERIFIED)
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "turn_on_check" and words in info["failure"] and not info["launched"]
    assert [c["name"] for c in events.wait("turn_on_check")["criteria"] if c["ok"] is False] == [crit]


@pytest.mark.parametrize("mode,words", [("fail", "turn-on check analysis failed (status 1"),
                                        ("garbage", "no usable result"),
                                        ("slow", "no result within 1 s")])
def test_an_analysis_that_fails_fails_the_check(env, events, check_env, mode, words):
    r = check_env(events, behave={"analysis": mode}, blocking=True, analysis_timeout_s=1.0, **VERIFIED)
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "turn_on_check" and words in info["failure"] and not info["launched"]


def test_an_analysis_that_fails_while_only_recording_lets_the_radar_start(env, events, check_env):
    r = check_env(events, behave={"analysis": "fail"})
    r.start(REQUEST)
    events.wait("started")
    r.stop("test stop")
    assert events.wait("ended")["failure"] == ""
    assert events.wait("turn_on_check")["passed"] is False


def test_the_data_drive_gone_during_the_check_is_a_failed_start(env, events, check_env):
    r = check_env(events, behave={"analysis": "slow"}, analysis_timeout_s=1.5)
    r.start(REQUEST)
    deadline = time.monotonic() + 10
    while not os.path.exists(os.path.join(env.root, env.collections()[-1] if env.collections() else "x",
                                          turnon.SUBDIR, turnon.INPUT_FILE)):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    import shutil
    shutil.rmtree(env.collection_dir())
    info = events.wait("ended")
    assert info["kind"] == "directory" and not info["fatal"] and not info["launched"]


# ------------------------------------------------------------------------- stopping

def wait_for_check_radar(r, started=False, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = r._current
        check = run.check_run if run is not None else None
        if check is not None and check.launched and (not started or check.started.is_set()):
            return check
        time.sleep(0.01)
    raise AssertionError("the low-power radar did not start")


def test_a_stop_during_the_check_radars_set_up_is_held_then_nothing_more_starts(env, events, check_env):
    r = check_env(events, behave={"init_s": 0.6})
    r.start(REQUEST)
    check = wait_for_check_radar(r)
    assert r.radar_gone() is False and r.snapshot()["turn_on_check"] == "low-power radar running"
    assert r.snapshot()["pid"] == check.proc.pid
    r.stop("stop by button")
    info = events.wait("ended")
    assert info["requested"] and info["failure"] == "" and not info["launched"] and r.radar_gone()
    assert not check.signalled_before_start and check.exit_status == 0 and env.sdr_state() == "safe"
    assert "started" not in events.names() and data_in_root(env) == []
    assert events.wait("turn_on_check")["stopped"] == "stop by button"


def test_a_stop_during_the_pulses_stops_the_check_radar_at_once(env, events, check_env):
    r = check_env(events, pulses=50000)         # 10 s of pulses
    r.start(REQUEST)
    check = wait_for_check_radar(r, started=True)
    t0 = time.monotonic()
    r.stop("stop by command")
    deadline = time.monotonic() + 3
    while not r.radar_gone():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert time.monotonic() - t0 < 1.0
    info = events.wait("ended")
    assert not info["launched"] and check.exit_status == 0 and "started" not in events.names()


def test_a_power_loss_during_the_check_radar_has_it_gone_in_the_urgent_budget(env, events, check_env):
    r = check_env(events, behave={"init_s": 5.0})
    r.start(REQUEST)
    wait_for_check_radar(r)
    t0 = time.monotonic()
    r.stop("power service", urgent=True, power=True)
    deadline = time.monotonic() + 3
    while not r.radar_gone():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    budget = env.config["manager"]["orca"]["urgent_stop_budget_s"]
    assert time.monotonic() - t0 <= budget + 0.3
    info = events.wait("ended")
    assert not info["launched"] and "started" not in events.names()
    # Signalled in its set-up: not known safe, and on a power loss the tool is left to the power service.
    assert result(env)["radar"]["signalled_before_start"] and env.amp_safe_calls() == 0


def test_a_power_loss_between_the_two_runs_starts_nothing(env, events, check_env):
    r = check_env(events, behave={"analysis": "slow"}, analysis_timeout_s=20.0)
    r.start(REQUEST)
    deadline = time.monotonic() + 10
    while not os.path.exists(os.path.join(env.root, env.collections()[-1] if env.collections() else "x",
                                          turnon.SUBDIR, turnon.INPUT_FILE)):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert env.sdr_state() == "safe"            # the low-power radar exited cleanly, its amplifier step is done
    time.sleep(0.3)                             # the analysis is running
    t0 = time.monotonic()
    r.stop("power service", urgent=True, power=True)
    assert r.radar_gone()
    info = events.wait("ended")
    assert time.monotonic() - t0 < 1.0 and not info["launched"] and "started" not in events.names()
    assert events.wait("turn_on_check")["stopped"] == "power service"


# ------------------------------------------------------------- the real analysis

@pytest.mark.skipif(not os.path.exists(ORCA_PYTHON), reason="no ORCA Python environment (numpy)")
@pytest.mark.parametrize("records,gain,passed", [("loopback_30ft_tx70.bin", 70.0, True),
                                                 ("no_signal_tx0.bin", 0.0, False)])
def test_the_real_analysis_on_recorded_loopback_records(env, events, check_env, records, gain, passed):
    behave = {"records_from": os.path.join(RECORDS, records), "records_gain": gain,
              "chirp_from": os.path.join(RECORDS, "chirp.bin")}
    r = check_env(events, behave=behave, real=True, blocking=True, **VERIFIED)
    r.start(REQUEST)
    if passed:
        events.wait("started")
        r.stop("test stop")
    info = events.wait("ended")
    summary = events.wait("turn_on_check")
    assert summary["passed"] is passed and (info["kind"] == "turn_on_check") is not passed
    if passed:
        # Recorded at tx_gain 70 (-5.9 dB), sent by the stand-in at the check's tx_gain 40: 30 dB lower.
        assert summary["measured"]["direct_level_db"] == pytest.approx(-35.9, abs=0.3)
        assert summary["measured"]["direct_sample"] == pytest.approx(78.4, abs=0.2)
    else:
        assert "no combined peak at sample 78.4" in info["failure"] or "below the minimum" in info["failure"]


# ------------------------------------------------------------------------ the service

class CheckRunning(Running):
    """The service with the check, the telemetry status from Feed (temperatures, current during pulses)."""

    def __init__(self, env, tx_file):
        super().__init__(env)
        self.feed_thread = Feed(env, tx_file)

    def publish(self):
        env = self.env
        env.publish("watcher", {"ok": True, "tripped": False, "reason": ""})
        env.publish("power", self.power)

    def start(self, feed=()):
        self.feed_thread.start()
        time.sleep(0.1)
        return super().start(feed=("watcher", "power"))

    def stop(self):
        out = super().stop()
        self.feed_thread.halt.set()
        return out


def test_the_service_shows_a_blocking_failure_as_a_fault_and_tries_again_at_the_next_press(env):
    tx_file = setup_check(env, blocking=True, **VERIFIED)
    env.behave(tx_file=tx_file, analysis="absent")
    mgr = CheckRunning(env, tx_file).start()
    try:
        mgr.wait_state("ready")
        assert mgr.ctl("start")[0] == 0
        status = mgr.wait_state("fault")
        assert status["fault"]["kind"] == "turn_on_check" and "no combined peak" in status["fault"]["reason"]
        assert status["leds"] == {"ready_yellow": "fast_blink", "running_green": "off"}
        last = status["turn_on_check"]["last"]
        assert last["blocked"] is True and status["turn_on_check"]["blocks"] is True
        assert status["retries"]["starts_last_hour"] == 2            # two SDR starts are counted
        assert mgr.events("turn_on_check")[0]["passed"] is False
        _, text = mgr.ctl("status")
        assert "turn-on:    check blocking; last: FAILED, the radar stays off" in text
        # The next press clears the fault and tries again; with a good check the radar runs.
        env.behave(tx_file=tx_file)
        mgr.manager.press()
        mgr.wait_state("running", timeout=15.0)
        assert mgr.status()["turn_on_check"]["last"]["passed"] is True and len(env.collections()) == 2
        assert mgr.ctl("stop", "--wait")[0] == 0
        mgr.wait_state("ready")
    finally:
        mgr.stop()
