"""The integrated manager against the real status files of the installed services, read only:

    /run/radar-runner/user/telemetry.json   the telemetry service (amplifier readings)
    /run/radar-runner/power.json            the power service

Everything that could touch hardware is a stand-in: ORCA and the `radar` binary, the reset
tool, the SDR's sysfs entry, the watcher's status file, the front panel (gpiozero mock pins).
Nothing is written outside the test's own directory. The real settings are used for
everything that concerns those two files (ages, field names, settle time).

Skipped where the two files are not there and fresh (another machine, services stopped).
"""
import json
import os
import threading
import time

import pytest

from radar_runner.common import config as common_config
from radar_runner.common import status as S
from radar_runner.manager import ampsafe, gates as G, radarctl
from radar_runner.manager.panel import Panel
from radar_runner.manager.service import Manager

DEFAULTS = common_config.DEFAULTS
TELEMETRY = S.status_path("telemetry", DEFAULTS["paths"]["run_dir"])
POWER = DEFAULTS["paths"]["power_status_file"]
TEST_MINIMUMS = {"retry_delay_s": 0.0, "resume_holdoff_s": 0.0, "stable_run_s": 0.0, "min_start_gap_s": 0.0}


def live():
    t = S.read_status(TELEMETRY, DEFAULTS["manager"]["gates"]["telemetry"]["max_age_s"])
    p = S.read_status(POWER, DEFAULTS["manager"]["gates"]["power"]["max_age_s"])
    return t, p


_t, _p = live()
pytestmark = pytest.mark.skipif(not (_t.ok and _p.ok),
                                reason=f"no fresh live status files: telemetry {_t.state}, power {_p.state}")


def use_live_files(env):
    """The stand-in configuration, with the real telemetry and power files and the real settings for them."""
    real = DEFAULTS["manager"]
    env.config["paths"]["power_status_file"] = POWER
    m = env.config["manager"]
    m["gates"]["telemetry"] = dict(real["gates"]["telemetry"], status_file=TELEMETRY)
    m["gates"]["power"] = dict(real["gates"]["power"])
    for key in ("current_field", "voltage_field", "sample_uptime_field", "enabled_current_a", "min_voltage_v",
                "confirm_settle_s", "confirm_timeout_s", "loop_gap_field", "max_loop_gap_s"):
        m["amp_safe"][key] = real["amp_safe"][key]
    return env


class LiveManager:
    """The manager in a thread; only the watcher's status file is a stand-in that has to be fed."""

    def __init__(self, env, panel=None):
        self.env = env
        self.manager = Manager(env.config, panel=panel, minimums=TEST_MINIMUMS)
        self._stop = threading.Event()
        self._feeder = threading.Thread(target=self._feed, daemon=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.result = None

    def _feed(self):
        while not self._stop.is_set():
            self.env.publish("watcher", {"ok": True, "tripped": False, "reason": ""})
            time.sleep(0.2)

    def _run(self):
        self.result = self.manager.run()

    def start(self):
        self.env.publish("watcher", {"ok": True, "tripped": False, "reason": ""})
        self._feeder.start()
        self._thread.start()
        return self

    def status(self):
        r = S.read_status(os.path.join(self.env.run_dir, "manager.json"), 5.0)
        return r.data if r.ok else {}

    def wait(self, predicate, timeout=30.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status and predicate(status):
                return status
            time.sleep(0.05)
        raise AssertionError(f"{what} not reached: {json.dumps(self.status(), indent=1)[:2500]}")

    def wait_state(self, state, timeout=30.0):
        return self.wait(lambda s: s.get("state") == state, timeout, f"state {state}")

    def wait_ready(self, timeout=30.0):
        """Ready, and the SDR record safe (after a run that takes one reading read 2 s after the exit)."""
        return self.wait(lambda s: s.get("state") == "ready" and s.get("sdr_state") == "safe", timeout, "ready")

    def ctl(self, *args):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = radarctl.main(["--socket", self.manager.socket_path, *args])
        return code, out.getvalue()

    def events(self, name):
        with open(os.path.join(self.env.state_dir, "manager-events.jsonl")) as f:
            return [e for e in map(json.loads, f) if e["event"] == name]

    def stop(self):
        self.manager.request_stop()
        self._thread.join(60)
        self._stop.set()
        assert not self._thread.is_alive()
        return self.result


@pytest.fixture
def live_mgr(env):
    use_live_files(env)
    from gpiozero.pins.mock import MockFactory, MockPWMPin
    factory = MockFactory(pin_class=MockPWMPin)
    holder = []
    cfg = dict(env.config["manager"]["panel"], enabled=True, kernel_debounce_s=0.0, press_min_s=0.05,
               press_gap_s=0.2)
    panel = Panel(cfg, lambda: holder[0].press(), pin_factory=factory)
    mgr = LiveManager(env, panel=panel)
    holder.append(mgr.manager)
    mgr.factory, mgr.panel = factory, panel
    mgr.start()
    yield mgr
    if mgr._thread.is_alive():
        mgr.stop()


def press(factory, hold_s=0.1):
    pin = factory.pin(4)
    pin.drive_low()
    time.sleep(hold_s)
    pin.drive_high()


def amplifier_now():
    """(True shutdown / False enabled / None, text, data) from the live telemetry file with the real settings."""
    config = {"paths": DEFAULTS["paths"], "manager": DEFAULTS["manager"]}
    state, text = ampsafe.AmpTelemetry(config).now(0.0)
    return state, text


# ----------------------------------------------- the interfaces, field by field

def test_live_telemetry_file_has_every_field_the_manager_reads():
    r = S.read_status(TELEMETRY, 3.0)
    assert r.ok, r.error
    d = r.data
    amp, loop = d["amp"], d["loop"]
    assert type(amp["fresh"]) is bool and isinstance(amp["reason"], str)
    for field in ("current_a", "voltage_v", "sample_uptime_s"):
        assert amp[field] is None or type(amp[field]) in (int, float), field
    assert type(loop["last_gap_s"]) in (int, float) and type(loop["recent_longest_gap_s"]) in (int, float)
    # The names in the settings resolve in the live file.
    cfg = DEFAULTS["manager"]
    for dotted in (cfg["gates"]["telemetry"]["require_true"] + [cfg["gates"]["telemetry"]["reason_field"]]
                   + [cfg["amp_safe"][k] for k in ("current_field", "voltage_field", "sample_uptime_field",
                                                   "loop_gap_field")]):
        G._lookup(d, dotted)                    # raises KeyError if the name is wrong
    # The sample time is on the same clock as ours, and recent.
    if amp["fresh"]:
        assert 0 <= time.clock_gettime(time.CLOCK_BOOTTIME) - amp["sample_uptime_s"] < 5


def test_live_power_file_has_every_field_the_manager_reads():
    r = S.read_status(POWER, 5.0)
    assert r.ok, r.error
    assert isinstance(r.data["state"], str) and type(r.data["radar_stop_requested"]) is bool


def test_gates_read_the_live_files(env):
    use_live_files(env)
    env.publish("watcher", {"ok": True, "tripped": False, "reason": ""})
    results = G.evaluate(G.build(env.config, G.Overrides(env.state_dir)))
    t, p = live()
    assert (results["telemetry"].run == "") is (t.data["amp"]["fresh"] is True)
    assert (results["power"].run == "") is (p.data["state"] == "normal" and p.data["radar_stop_requested"] is False)
    assert results["watcher"].run == ""


def test_amplifier_reading_from_the_live_file():
    state, text = amplifier_now()
    t, _ = live()
    amp = t.data["amp"]
    if not amp["fresh"]:
        assert state is None
    elif amp["voltage_v"] < 20.0:
        assert state is None and "28 V rail reads" in text
    else:
        assert state is (amp["current_a"] < 0.5) or state is None      # None only if the loop has just stalled
        assert f"amplifier current {amp['current_a']:.2f} A"[:22] in text or "stalled" in text


# ------------------------------------------------------------------- end to end

def needs_good_live_readings():
    t, p = live()
    if not (t.ok and t.data["amp"]["fresh"] is True):
        pytest.skip("the live amplifier telemetry is not fresh: the telemetry gate is closed")
    if not (p.ok and p.data["state"] == "normal" and p.data["radar_stop_requested"] is False):
        pytest.skip(f"the live power service is not in its normal state: {p.data.get('state') if p.ok else p.state}")


def test_start_run_stop_with_the_button_against_the_live_files(env, live_mgr):
    needs_good_live_readings()
    status = live_mgr.wait_state("ready")
    assert all(g["ok"] for g in status["gates"].values()), status["gates"]
    assert live_mgr.panel.shown == ("on", "off")
    press(live_mgr.factory)
    status = live_mgr.wait_state("running")
    assert status["trigger"] == "button" and live_mgr.panel.shown == ("off", "on")
    time.sleep(0.5)
    press(live_mgr.factory)
    live_mgr.wait_state("ready")
    meta = env.metadata()
    end = meta["end"]
    assert end["how"] == "stopped" and end["exit_status"] == 0
    state, _ = amplifier_now()
    if state is True:
        # The real amplifier is in shutdown with its 28 V on: the reading, taken from the live
        # file at least 2 s after the stand-in radar's exit, is what confirmed the pin safe.
        assert end["amp_safe"] is True and end["amp_safe_command_run"] is False
        assert end["amp_safe_detail"].startswith("tool not run: telemetry: amplifier current ")
        assert end["uptime_s"] - meta["recording_started"]["uptime_s"] >= 2.0
    elif state is None:
        # No usable reading (28 V off, or not fresh): a clean exit is safe by its history,
        # or, with the rail reading absent, nothing is checked at all.
        assert end["amp_safe_command_run"] is False
    assert env.amp_safe_calls() == 0
    assert live_mgr.stop() == 0


def test_fault_and_recovery_against_the_live_files(env, live_mgr):
    needs_good_live_readings()
    live_mgr.wait_state("ready")
    # A radar that cannot start: retried (0.2 s and 0.3 s here), then a fault.
    env.behave(radar="fail_start")
    assert live_mgr.ctl("start")[0] == 0
    status = live_mgr.wait_state("fault", timeout=60)
    assert status["fault"]["kind"] == "start_failures" and len(env.collections()) == 3
    # A press while a gate is bad changes nothing ...
    env.sdr(present=False)
    live_mgr.wait(lambda s: s["not_ready"], what="not ready")
    press(live_mgr.factory)
    time.sleep(0.5)
    assert live_mgr.status()["state"] == "fault"
    # ... and when all is good again the press clears the fault and starts.
    env.sdr()
    env.behave()
    live_mgr.wait(lambda s: not s["not_ready"], what="gates good")
    press(live_mgr.factory)
    live_mgr.wait_state("running")
    # A crash mid-run is retried by itself.
    env.behave(radar="crash")
    assert live_mgr.ctl("stop", "--wait")[0] == 0
    live_mgr.wait_state("ready")
    live_mgr.ctl("start")
    live_mgr.wait_state("running")
    env.behave()
    live_mgr.wait(lambda s: s["state"] == "running" and s["retries"]["failed_starts"] == 1, 60, "restart")
    assert live_mgr.ctl("stop", "--wait")[0] == 0
    live_mgr.wait_state("ready")
    state, _ = amplifier_now()
    if state is True:                            # after each unclean exit the live reading showed shutdown
        assert env.amp_safe_calls() == 0
    assert live_mgr.stop() == 0


def test_interrupt_as_the_power_hook_calls_it_against_the_live_files(env, live_mgr):
    needs_good_live_readings()
    live_mgr.wait_state("ready")
    live_mgr.ctl("start")
    live_mgr.wait_state("running")
    t0 = time.monotonic()
    code, out = live_mgr.ctl("stop", "--interrupt", "--wait", "--timeout", "4")
    assert code == 0 and time.monotonic() - t0 < 1.5
    live_mgr.wait_state("running", timeout=60)  # the live power file says "normal": the run resumes
    assert len(env.collections()) == 2 and env.metadata()["resume"] is True
    assert "power loss" in env.metadata(0)["end"]["amp_safe_detail"]       # nothing was checked at that stop
    live_mgr.ctl("stop", "--wait")
    live_mgr.wait_state("ready")


def test_fifty_cycles_against_the_live_files(env, live_mgr):
    needs_good_live_readings()
    live_mgr.wait_state("ready")
    for i in range(50):
        if i % 2:
            press(live_mgr.factory)
        else:
            assert live_mgr.ctl("start")[0] == 0
        live_mgr.wait_state("running")
        if i % 2:
            press(live_mgr.factory)
        else:
            assert live_mgr.ctl("stop", "--wait")[0] == 0
        live_mgr.wait_state("ready")
    names = env.collections()
    assert len(names) == 50 and names == sorted(names)
    assert all(env.metadata(i)["end"]["how"] == "stopped" for i in range(50))
    assert all(env.metadata(i)["end"]["amp_safe"] in (True, None) for i in range(50))
    assert not live_mgr.events("fault") and env.amp_safe_calls() == 0
    status = live_mgr.status()
    assert status["retries"]["restarts_last_hour"] == 0 and status["fault"] is None
    button = status["panel"]["button"]
    assert button["presses"] == 50 and sum(button["ignored"].values()) == 0
    assert live_mgr.stop() == 0
