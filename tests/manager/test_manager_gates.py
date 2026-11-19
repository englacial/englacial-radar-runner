"""The gates (radar_runner/manager/gates.py) and the amp-safe decision (ampsafe.py)."""
import os
import time
from collections import namedtuple

import pytest

from radar_runner.common import status as S
from radar_runner.manager import ampsafe, gates as G
from radar_runner.manager.logic import GateResult


def names(env):
    return [g.name for g in G.build(env.config, G.Overrides(env.state_dir))]


def check(env, name):
    gate = next(g for g in G.build(env.config, G.Overrides(env.state_dir)) if g.name == name)
    return gate.check()


def test_all_gates_good_with_every_stand_in(env):
    env.all_good()
    results = G.evaluate(G.build(env.config, G.Overrides(env.state_dir)))
    assert list(results) == ["telemetry", "watcher", "power", "data", "sdr", "config", "orca", "amp_safe"]
    assert all(not (r.start or r.run) for r in results.values()), results


def test_a_gate_that_raises_fails_closed():
    class Broken:
        name = "broken"

        def check(self):
            raise ZeroDivisionError("oops")
    r = G.evaluate([Broken()])["broken"]
    assert "internal error" in r.start and r.run == r.start


# ----------------------------------------------------------------- telemetry

def test_telemetry_missing_stale_or_not_fresh(env):
    r = check(env, "telemetry")
    assert r.start == r.run == "amplifier telemetry: status missing (no such file)"
    env.publish("telemetry", {"amp": {"fresh": False, "reason": "no row for 3.4 s"}})
    assert check(env, "telemetry").run == "amplifier telemetry: no row for 3.4 s"
    env.publish("telemetry", {"amp": {"fresh": True, "reason": ""}})
    assert check(env, "telemetry") == GateResult()
    # The format is not final: a missing field, or one that is not true/false, fails closed.
    env.publish("telemetry", {"state": "ok"})
    assert check(env, "telemetry").run == "amplifier telemetry: amp.fresh missing"
    env.publish("telemetry", {"amp": {"fresh": "yes"}})
    assert "amp.fresh is not true/false" in check(env, "telemetry").run
    env.publish("telemetry", {"amp": "broken"})
    assert "amp.fresh missing" in check(env, "telemetry").run


def test_telemetry_goes_stale(env):
    env.publish("telemetry", {"amp": {"fresh": True}})
    env.config["manager"]["gates"]["telemetry"]["max_age_s"] = 0.1
    time.sleep(0.25)
    r = check(env, "telemetry")
    assert r.run.startswith("amplifier telemetry: status stale (")


def test_telemetry_file_from_another_boot_or_damaged(env):
    path = os.path.join(env.run_dir, "telemetry.json")
    S.StatusWriter(path, boot_id="another-boot").write({"amp": {"fresh": True}})
    assert "written during another boot" in check(env, "telemetry").run
    with open(path, "w") as f:
        f.write("{not json")
    assert "status corrupt" in check(env, "telemetry").run


def test_field_names_and_path_are_settings(env):
    cfg = env.config["manager"]["gates"]["telemetry"]
    cfg.update(status_file=os.path.join(env.tmp, "other.json"), require_true=["pa.ok", "pa.powered"],
               require_false=["pa.stale"], reason_field="pa.why")
    S.StatusWriter(cfg["status_file"]).write({"pa": {"ok": True, "powered": True, "stale": False}})
    assert check(env, "telemetry") == GateResult()
    S.StatusWriter(cfg["status_file"]).write({"pa": {"ok": True, "powered": False, "stale": True, "why": ""}})
    assert check(env, "telemetry").run == "amplifier telemetry: pa.powered is false, pa.stale is true"


def test_telemetry_override(env):
    overrides = G.Overrides(env.state_dir)
    gate = next(g for g in G.build(env.config, overrides) if g.name == "telemetry")
    assert gate.check().run and not overrides.is_set() and overrides.snapshot() == {"no_amp_telemetry": False}
    assert overrides.set(True, "by a test") == ""
    r = gate.check()
    assert r.start == r.run == "" and r.kind == "override"
    assert "OVERRIDE, running without amplifier telemetry (amplifier telemetry: status missing" in r.note
    # It persists as a file: a new object (a restart, a reboot) sees it.
    assert G.Overrides(env.state_dir).is_set()
    assert os.path.exists(os.path.join(env.state_dir, "override-no-amp-telemetry"))
    # With good telemetry the override changes nothing and is not reported as used.
    env.publish("telemetry", {"amp": {"fresh": True}})
    assert gate.check() == GateResult()
    assert overrides.set(False) == "" and not overrides.is_set()
    assert overrides.set(False) == ""                       # clearing twice is fine


def test_override_cannot_be_written(env):
    overrides = G.Overrides(os.path.join(env.tmp, "no-such-dir"))
    assert "no-such-dir" in overrides.set(True) and not overrides.is_set()


# ------------------------------------------------------------------- watcher

def test_watcher_missing_tripped_or_good(env):
    assert check(env, "watcher").run == "safety watcher: status missing (no such file)"
    env.publish("watcher", {"ok": True, "tripped": False, "reason": ""})
    assert check(env, "watcher") == GateResult()
    env.publish("watcher", {"ok": False, "tripped": True, "reason": "amplifier 56.2 C, limit 55 C"})
    r = check(env, "watcher")
    assert r.start == r.run == "safety watcher: amplifier 56.2 C, limit 55 C"
    env.publish("watcher", {"ok": True, "tripped": True})
    assert check(env, "watcher").run == "safety watcher: tripped is true"


def test_watcher_has_no_override(env):
    G.Overrides(env.state_dir).set(True)
    assert check(env, "watcher").run != ""


# --------------------------------------------------------------------- power

def test_power_gate(env):
    r = check(env, "power")
    assert r.run == "power service: status missing (no such file)" and r.kind == ""
    env.publish("power", {"state": "normal", "radar_stop_requested": False})
    assert check(env, "power") == GateResult()
    env.publish("power", {"state": "loss", "radar_stop_requested": True})
    r = check(env, "power")
    assert r.start == r.run == "power service: state loss, radar stop requested" and r.kind == "power_fail"
    for state in ("would_shutdown", "committing", "shutdown"):
        env.publish("power", {"state": state, "radar_stop_requested": True})
        assert check(env, "power").kind == "power_fail"
    env.publish("power", {"state": "normal", "radar_stop_requested": True})
    assert check(env, "power").run == "power service: state normal, radar stop requested"
    env.publish("power", {"state": "normal"})               # a file without the field: fail closed
    assert check(env, "power").run != ""


def test_power_gate_reads_the_real_format(env):
    # As power/service.py writes it: time and uptime_s, no boot_id.
    import json
    with open(env.power_file, "w") as f:
        json.dump({"version": 1, "time": time.time(), "uptime_s": time.clock_gettime(time.CLOCK_BOOTTIME),
                   "state": "normal", "radar_stop_requested": False}, f)
    assert check(env, "power") == GateResult()
    with open(env.power_file, "w") as f:
        json.dump({"time": time.time(), "uptime_s": time.clock_gettime(time.CLOCK_BOOTTIME) - 30,
                   "state": "normal", "radar_stop_requested": False}, f)
    assert "status stale" in check(env, "power").run


# ---------------------------------------------------------------------- data

Stat = namedtuple("Stat", "f_bavail f_frsize f_flag")


def data_gate(env, free_gb=100.0, mounted=True, flag=0, **cfg):
    settings = dict(env.config["manager"]["gates"]["data"], start_min_free_gb=20.0, stop_min_free_gb=5.0, **cfg)
    return G.DataGate(settings, ismount=lambda path: mounted,
                      statvfs=lambda path: Stat(int(free_gb * 1e9 / 4096), 4096, flag))


def test_data_gate_free_space_thresholds(env):
    r = data_gate(env, 100.0).check()
    assert r.start == r.run == "" and r.note == "100.0 GB free"
    r = data_gate(env, 19.9).check()
    assert r.run == "" and r.start == "data drive nearly full: 19.9 GB free, no start below 20 GB"
    r = data_gate(env, 4.9).check()
    assert r.start == r.run == "data drive full: 4.9 GB free, stop below 5 GB"


def test_data_gate_not_mounted_never_falls_back_to_the_sd_card(env):
    mount = os.path.join(env.tmp, "nvme")
    r = data_gate(env, mounted=False, mount_point=mount).check()
    assert r.start == r.run == f"data drive not mounted at {mount}"
    assert data_gate(env, mounted=True, mount_point=mount).check().run == ""
    # A data root outside the drive is refused even when the drive is mounted.
    r = data_gate(env, mounted=True, mount_point=mount, root=os.path.join(env.tmp, "elsewhere")).check()
    assert "is not on the data drive" in r.run and not os.path.exists(os.path.join(env.tmp, "elsewhere"))


def test_data_gate_with_the_real_mount_check(env):
    cfg = dict(env.config["manager"]["gates"]["data"], mount_point=os.path.join(env.tmp, "nvme"))
    assert G.DataGate(cfg).check().run.startswith("data drive not mounted at")      # a plain directory


def test_data_gate_read_only_or_unwritable(env):
    assert "not writable" in data_gate(env, flag=os.ST_RDONLY).check().run
    os.chmod(env.root, 0o555)
    try:
        if os.geteuid() != 0:
            assert "not writable" in data_gate(env).check().run
    finally:
        os.chmod(env.root, 0o755)


def test_data_gate_creates_the_root_on_the_drive(env):
    root = os.path.join(env.tmp, "nvme", "radar-runner", "new-root")
    assert data_gate(env, root=root).check().run == "" and os.path.isdir(root)


# ----------------------------------------------------------------------- SDR

def test_sdr_gate_is_start_only_and_reads_sysfs(env):
    r = check(env, "sdr")
    assert r == GateResult(note="USB 4-1, 5000 Mbit/s")
    env.sdr(present=False)
    assert check(env, "sdr") == GateResult(start="SDR not found on USB")           # run stays empty
    env.sdr(speed=480, product="USRP B200")
    assert check(env, "sdr") == GateResult(start="SDR on a USB 2 link (480 Mbit/s at 4-1)")
    env.sdr(speed=10000)
    assert check(env, "sdr").start == ""


def test_sdr_in_its_boot_loader_passes_because_its_speed_says_nothing(env):
    # As this Pi shows it before the first UHD start after power-up: 2500:0023 "WestBridge" at 480.
    env.sdr(speed=480, product="WestBridge")
    r = check(env, "sdr")
    assert r.start == "" and "firmware not loaded yet" in r.note


def test_sdr_gate_ignores_other_devices_and_wants_exactly_one(env):
    other = os.path.join(env.sysfs, "2-2")
    os.makedirs(other)
    for name, value in (("idVendor", "1546"), ("idProduct", "01a9"), ("speed", "12")):
        with open(os.path.join(other, name), "w") as f:
            f.write(value)
    os.makedirs(os.path.join(env.sysfs, "usb1"))            # an entry without ID files
    assert check(env, "sdr").start == ""
    second = os.path.join(env.sysfs, "5-1")
    os.makedirs(second)
    for name, value in (("idVendor", "2500"), ("idProduct", "0022"), ("speed", "5000")):
        with open(os.path.join(second, name), "w") as f:
            f.write(value)
    assert check(env, "sdr").start == "2 SDRs found on USB; expected one"
    env.config["manager"]["gates"]["sdr"]["sysfs_dir"] = os.path.join(env.tmp, "nope")
    assert check(env, "sdr").start == "SDR not found on USB"


# ------------------------------------------------------- config, ORCA, amp-safe

def test_config_gate(env):
    assert check(env, "config") == GateResult()
    env.config["manager"]["limits"]["max_tx_gain"] = 43.0
    r = check(env, "config")
    assert r.run == "" and r.start == f"radar config {env.radar_config}: RF0.tx_gain 72 is above the cap of 43"
    os.unlink(env.radar_config)
    assert "No such file" in check(env, "config").start


def test_config_gate_rereads_only_when_the_file_changes(env, monkeypatch):
    from radar_runner.manager import limits
    gate = G.ConfigGate(env.radar_config, env.config["manager"]["limits"])
    calls = []
    real = limits.load_file
    monkeypatch.setattr(limits, "load_file", lambda path: calls.append(path) or real(path))
    for _ in range(5):
        assert gate.check().start == ""
    assert len(calls) == 1
    with open(env.radar_config, "a") as f:
        f.write("\nGPIO: {pwr_amp_pin: \"-1\"}\n")          # a second GPIO section
    r = gate.check()
    assert len(calls) == 2 and "appears twice" in r.start
    assert "(and" not in r.start


def test_config_gate_names_how_many_more_problems(env):
    with open(env.radar_config) as f:
        text = f.read()
    with open(env.radar_config, "w") as f:
        f.write(text.replace("tx_gain: 72.0", "tx_gain: 99").replace('pwr_amp_pin: "2"', 'pwr_amp_pin: "-1"'))
    assert check(env, "config").start.endswith("(and 1 more)")


def test_orca_gate(env):
    assert check(env, "orca") == GateResult()
    env.config["manager"]["orca"]["radar_cwd"] = "sdr"
    assert "two levels below" in check(env, "orca").start
    env.config["manager"]["orca"]["dir"] = os.path.join(env.tmp, "no-orca")
    assert "not found" in check(env, "orca").start


def test_amp_safe_gate_refuses_to_run_without_the_command(env):
    assert check(env, "amp_safe") == GateResult()
    env.config["manager"]["amp_safe"]["command"] = []
    assert "not configured" in check(env, "amp_safe").start
    env.config["manager"]["amp_safe"]["command"] = [os.path.join(env.tmp, "amp_safe"), "--timeout", "20"]
    assert "is not an executable file" in check(env, "amp_safe").start


def test_default_config_is_not_ready_until_the_tool_and_the_radar_config_exist():
    from radar_runner.manager.config import MANAGER_DEFAULTS
    assert MANAGER_DEFAULTS["amp_safe"]["command"][0].endswith("tools/sdr_gpio_safe/amp_safe")
    assert MANAGER_DEFAULTS["amp_safe"]["policy"] == "when_needed"
    assert MANAGER_DEFAULTS["limits"]["max_tx_gain"] == 43.0        # the conservative default


# ------------------------------------------- amp-safe: the three decisions (pure functions)

CLEAN = {"reached_running": True, "killed": False, "exit_status": 0, "failure": ""}
KILLED = {**CLEAN, "killed": True, "exit_status": -9}
NO_READING = (None, "no fresh amplifier reading after the exit")
SHUTDOWN = (True, "amplifier current 0.16 A at 28.0 V")
ENABLED = (False, "amplifier current 2.50 A at 27.5 V")


def never():
    raise AssertionError("telemetry must not be asked")


@pytest.mark.parametrize("change, why", [
    ({"killed": True}, "the radar was killed"),
    ({"reached_running": False}, "the radar never reached its running state"),
    ({"exit_status": 1}, "the radar exited with status 1"),
    ({"exit_status": -9}, "the radar exited with status -9"),
    ({"exit_status": None}, "the radar exited with status None"),
    ({"failure": "the radar reports the SDR on a USB 2 link"}, "failure: the radar reports the SDR on a USB 2 link"),
])
def test_what_counts_as_an_unclean_radar_exit(change, why):
    assert ampsafe.unclean({**CLEAN, **change}) == why
    assert ampsafe.unclean(CLEAN) == ""


def test_known_safe_after_a_clean_exit_without_telemetry():
    known, why = ampsafe.known_safe_after_exit(CLEAN, NO_READING)
    assert known and why.startswith("clean radar exit; no telemetry to check")


def test_not_known_safe_after_an_unclean_exit_without_telemetry():
    known, why = ampsafe.known_safe_after_exit(KILLED, NO_READING)
    assert not known and why.startswith("the radar was killed; no telemetry to decide")


@pytest.mark.parametrize("facts", [CLEAN, KILLED, {**CLEAN, "reached_running": False}, {**CLEAN, "exit_status": 3}])
def test_a_fresh_reading_with_the_28_v_present_decides_whatever_the_history(facts):
    assert ampsafe.known_safe_after_exit(facts, SHUTDOWN) == (True, "telemetry: amplifier current 0.16 A at 28.0 V")
    known, why = ampsafe.known_safe_after_exit(facts, ENABLED)
    assert not known and "telemetry shows the amplifier enabled" in why


def test_decide_when_needed_runs_the_tool_only_when_not_known_safe():
    assert ampsafe.decide("when_needed", CLEAN, lambda: NO_READING)[:2] == (False, True)
    assert ampsafe.decide("when_needed", CLEAN, lambda: SHUTDOWN)[:2] == (False, True)
    assert ampsafe.decide("when_needed", CLEAN, lambda: ENABLED)[:2] == (True, False)
    assert ampsafe.decide("when_needed", KILLED, lambda: NO_READING)[:2] == (True, False)
    assert ampsafe.decide("when_needed", KILLED, lambda: SHUTDOWN)[:2] == (False, True)
    assert ampsafe.decide("when_needed", KILLED, lambda: ENABLED)[:2] == (True, False)


def test_decide_always_runs_the_tool_and_judges_by_the_history_alone():
    run, known, why = ampsafe.decide("always", CLEAN, never)
    assert run and known and why.startswith("policy always; clean radar exit")
    run, known, why = ampsafe.decide("always", KILLED, never)
    assert run and not known
    run, known, why = ampsafe.decide("sometimes", CLEAN, never)
    assert run and "unknown policy 'sometimes', treated as always" in why


@pytest.mark.parametrize("status, known_before, expected", [
    (0, False, ("safe", True)), (0, True, ("safe", True)),
    (3, True, ("unknown", False)), (3, False, ("unknown", False)),         # ASSUME_ENABLED: never safe
    (ampsafe.KILLED_BY_CALLER, True, ("unknown", False)),                   # killed half way
    (2, True, ("safe", True)), (2, False, ("unknown", False)),              # NOT_FOUND: nothing changed
    (6, True, ("safe", True)), (6, False, ("unknown", False)),              # BUSY
    (7, False, ("unknown", False)), (4, False, ("unknown", False)), (5, True, ("safe", True)),
    (64, False, ("unknown", False)), (None, True, ("safe", True)), (None, False, ("unknown", False)),
    (99, False, ("unknown", False)),
])
def test_the_record_after_a_run_of_the_tool(status, known_before, expected):
    assert ampsafe.after_tool(status, known_before) == expected


def test_exit_codes_of_the_tool_are_named():
    assert ampsafe.EXIT_MEANING == {0: "OK", 2: "NOT_FOUND", 3: "ASSUME_ENABLED", 4: "TIMEOUT", 5: "FAILED",
                                    6: "BUSY", 7: "AMBIGUOUS", 64: "USAGE"}
    assert ampsafe.describe(6) == "status 6 (BUSY)" and ampsafe.describe(None) == "not started"
    assert "killed" in ampsafe.describe(ampsafe.KILLED_BY_CALLER) and "not a status of the tool" in ampsafe.describe(1)


IDLE = dict(enabled_for_s=0.0, enabled_limit_s=5.0)


def test_idle_sdr_still_in_its_boot_loader_is_safe_without_the_tool():
    # Nothing has initialised it since it was powered: also clears a record left by a hard power cut.
    assert ampsafe.idle_need("unknown", "bootloader", None, **IDLE) == (
        "mark_safe", "nothing has initialised the SDR since it was powered")
    assert ampsafe.idle_need("safe", "bootloader", None, **IDLE)[0] == "none"


def test_idle_unknown_state_runs_the_tool_unless_telemetry_decides():
    # Manager start with the record "unknown", or with no record at all, after the SDR has been in use.
    action, why = ampsafe.idle_need("unknown", "loaded", None, **IDLE)
    assert action == "run" and "not known" in why
    assert ampsafe.idle_need("unknown", "loaded", True, **IDLE) == (
        "mark_safe", "telemetry shows shutdown current with no radar process")
    assert ampsafe.idle_need("safe", "loaded", None, **IDLE)[0] == "none"
    assert ampsafe.idle_need("safe", "loaded", True, **IDLE)[0] == "none"


def test_idle_enabled_current_runs_the_tool_only_when_it_lasts():
    # One reading is not enough: every start of the tool or of the radar enables the amplifier briefly.
    for state in ("safe", "unknown"):
        assert ampsafe.idle_need(state, "loaded", False, enabled_for_s=4.9, enabled_limit_s=5.0)[0] == "none"
        action, why = ampsafe.idle_need(state, "loaded", False, enabled_for_s=5.0, enabled_limit_s=5.0)
        assert action == "run" and "enabled for 5 s with no radar process" in why


def test_idle_without_an_sdr_nothing_is_done():
    for state in ("safe", "unknown"):
        for reading in (None, True, False):
            assert ampsafe.idle_need(state, "absent", reading, enabled_for_s=60.0, enabled_limit_s=5.0)[0] == "none"


def amp_telemetry(env, **kw):
    return ampsafe.AmpTelemetry(env.config, **kw)


def publish_amp(env, current, voltage=28.0, age=0.0, fresh=True):
    up = time.clock_gettime(time.CLOCK_BOOTTIME) - age
    env.publish("telemetry", {"amp": {"fresh": fresh, "current_a": current, "voltage_v": voltage,
                                      "sample_uptime_s": up}})
    return up


def test_confirm_shutdown_from_the_telemetry_file(env):
    exit_uptime = time.clock_gettime(time.CLOCK_BOOTTIME) - 1.0
    publish_amp(env, 0.156)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime) == (True, "amplifier current 0.16 A at 28.0 V")
    publish_amp(env, 2.47, 27.5)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime) == (False, "amplifier current 2.47 A at 27.5 V")
    publish_amp(env, 0.5)                                   # at the threshold counts as enabled
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is False


def test_confirm_needs_a_reading_taken_after_the_exit(env):
    publish_amp(env, 0.156, age=2.0)                        # taken before the radar exited
    exit_uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
    state, text = amp_telemetry(env).confirm_shutdown(exit_uptime)
    assert state is None and text == "no fresh amplifier reading after the exit"
    env.config["manager"]["amp_safe"]["confirm_settle_s"] = 1.0
    publish_amp(env, 0.156)                                 # newer than the exit, but not by the settle time
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None


def test_confirm_waits_for_the_reading(env):
    exit_uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
    env.config["manager"]["amp_safe"]["confirm_timeout_s"] = 3.0
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        publish_amp(env, 0.16)                              # the telemetry service's next sample
    assert amp_telemetry(env, sleep=sleep).confirm_shutdown(exit_uptime)[0] is True and len(sleeps) == 1


def test_confirm_says_nothing_without_the_28_v_rail_or_fresh_telemetry(env):
    exit_uptime = time.clock_gettime(time.CLOCK_BOOTTIME) - 1.0
    publish_amp(env, 0.0, voltage=0.3)                      # power loss: zero current means nothing
    state, text = amp_telemetry(env).confirm_shutdown(exit_uptime)
    assert state is None and "28 V rail reads 0.3 V" in text
    publish_amp(env, 0.16, fresh=False)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None
    env.publish("telemetry", {"amp": {"fresh": True, "current_a": None, "voltage_v": 28.0, "sample_uptime_s": 1e12}})
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None
    os.unlink(os.path.join(env.run_dir, "telemetry.json"))
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None


def test_latest_reading_without_waiting(env):
    assert amp_telemetry(env).now() == (None, "no fresh amplifier reading")
    publish_amp(env, 0.156)
    assert amp_telemetry(env).now() == (True, "amplifier current 0.16 A at 28.0 V")
    publish_amp(env, 2.6, 27.4)
    assert amp_telemetry(env).now() == (False, "amplifier current 2.60 A at 27.4 V")
    publish_amp(env, 0.0, 0.2)
    assert amp_telemetry(env).now()[0] is None


def test_sdr_state_record(env):
    state = ampsafe.SdrState(env.state_dir)
    assert state.get() == "unknown" and not state.known_safe and state.why() == "no record"    # no file
    assert state.set("unknown", "radar launched, collection x") == "" and state.get() == "unknown"
    assert state.set("safe", "clean radar exit") == "" and state.known_safe
    assert ampsafe.SdrState(env.state_dir).get() == "safe"  # a file: any other program can read it
    with open(os.path.join(env.state_dir, "sdr-state")) as f:
        lines = f.read().splitlines()
    assert lines[0] == "safe" and lines[1].endswith(" clean radar exit") and "clean radar exit" in state.why()
    assert os.listdir(env.state_dir) == ["sdr-state"]       # replaced atomically: no temporary file left
    # Anything that is not exactly "safe" on the first line is unknown.
    for content in ("", "SAFE\n", "safe-ish\n", "unknown\n", "\x00\x00", " safe\nsafe\n"):
        with open(os.path.join(env.state_dir, "sdr-state"), "w") as f:
            f.write(content)
        assert state.get() == ("safe" if content.strip().split("\n")[0] == "safe" else "unknown")
    assert "no-dir" in ampsafe.SdrState(os.path.join(env.tmp, "no-dir")).set("safe")


def stop_hook(env, tool_status):
    """Run bin/radar-manager-stop-hook with a stand-in tool. Returns (exit status, calls of the tool)."""
    import subprocess
    from radar_runner.common import config as common_config
    tool = os.path.join(env.tmp, "amp_safe")
    calls = os.path.join(env.tmp, "hook_calls")
    with open(tool, "w") as f:
        f.write(f'#!/bin/sh\necho "$@" >> {calls}\nexit {tool_status}\n')
    os.chmod(tool, 0o755)
    hook = os.path.join(common_config.REPO, "bin", "radar-manager-stop-hook")
    result = subprocess.run([hook, os.path.join(env.state_dir, "sdr-state"), tool], timeout=20)
    try:
        with open(calls) as f:
            return result.returncode, f.read().splitlines()
    except OSError:
        return result.returncode, []


def test_stop_hook_of_the_unit_runs_the_tool_only_when_the_state_is_not_safe(env):
    state = ampsafe.SdrState(env.state_dir)
    state.set("safe", "clean radar exit")
    assert stop_hook(env, 0) == (0, [])                     # known safe: nothing is done
    state.set("unknown", "radar launched, collection x")    # as a killed manager leaves it
    assert stop_hook(env, 0) == (0, ["--timeout 14"])
    assert state.get() == "safe" and "stop hook" in state.why()


def test_stop_hook_with_no_record_or_a_failing_tool(env):
    state = ampsafe.SdrState(env.state_dir)
    assert stop_hook(env, 3) == (3, ["--timeout 14"])       # no record = unknown; the tool's status is passed on
    assert state.get() == "unknown" and not os.path.exists(state.path)
    state.set("unknown", "x")
    assert stop_hook(env, 6)[0] == 6 and state.get() == "unknown"
