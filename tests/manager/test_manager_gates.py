"""The gates (radar_runner/manager/gates.py) and the amp-safe decision (ampsafe.py)."""
import os
import time
from collections import namedtuple

import pytest

from radar_runner.common import config as common_config
from radar_runner.common import status as S
from radar_runner.manager import ampsafe, gates as G
from radar_runner.manager.logic import GateResult

INSTALLED = "/home/englacial/englacial-radar-runner"

MANAGER = common_config.DEFAULTS["manager"]


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


def test_amp_safe_gate_checks_the_time_limits(env):
    # The tool's own --timeout is mandatory, and the manager must not kill the tool before it:
    # a kill inside the tool's initialisation leaves the amplifier enabled.
    cfg = env.config["manager"]["amp_safe"]
    tool = cfg["command"][:2]
    cfg["command"] = tool
    assert "needs --timeout N" in check(env, "amp_safe").start
    cfg["command"] = tool + ["--timeout", "soon"]
    assert "needs --timeout N" in check(env, "amp_safe").start
    cfg["command"] = tool + ["--timeout", "30"]
    cfg["timeout_s"] = 30.0
    assert check(env, "amp_safe").start == ("manager.amp_safe.timeout_s (30 s) must be longer than the tool's "
                                            "--timeout (30 s)")
    cfg["timeout_s"] = 40.0
    assert check(env, "amp_safe") == GateResult()


def test_sdr_identity_as_measured_on_this_hardware(env):
    # Before the firmware load: 2500:0023 "WestBridge" at 480. After: still 2500:0023, at 5000,
    # with another (here empty) product text. The product ID does not change.
    env.sdr(speed=480, product="WestBridge", pid="0023")
    assert "firmware not loaded yet" in check(env, "sdr").note
    env.sdr(speed=5000, product="", pid="0023")
    assert check(env, "sdr") == GateResult(note="USB 4-1, 5000 Mbit/s")
    env.sdr(speed=480, product="", pid="0023")              # firmware loaded, but on a USB 2 link
    assert check(env, "sdr").start == "SDR on a USB 2 link (480 Mbit/s at 4-1)"


# ------------------------------------------- amp-safe: the three decisions (pure functions)

CLEAN = {"reached_running": True, "killed": False, "exit_status": 0, "failure": ""}
KILLED = {**CLEAN, "killed": True, "exit_status": -9}
EARLY = {**CLEAN, "reached_running": False, "exit_status": -2, "signalled_before_start": True}
NO_READING = lambda: (None, "no fresh amplifier reading after the exit")       # noqa: E731
SHUTDOWN = lambda: (True, "amplifier current 0.16 A at 28.0 V")                # noqa: E731
ENABLED = lambda: (False, "amplifier current 2.50 A at 27.5 V")                # noqa: E731


def never():
    raise AssertionError("telemetry must not be asked")


@pytest.mark.parametrize("change, why", [
    ({"killed": True}, "the radar was killed"),
    ({"reached_running": False}, "the radar never reached its running state"),
    ({"exit_status": 1}, "the radar exited with status 1"),
    ({"exit_status": -9}, "the radar exited with status -9"),
    ({"exit_status": None}, "the radar exited with status None"),
    ({"failure": "the SDR is on a USB 2 link"}, "failure: the SDR is on a USB 2 link"),
    ({"signalled_before_start": True}, "the radar was signalled before its start line, in the middle of the SDR set-up"),
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
def test_a_qualifying_reading_decides_whatever_the_history(facts):
    assert ampsafe.known_safe_after_exit(facts, SHUTDOWN) == (True, "telemetry: amplifier current 0.16 A at 28.0 V")
    known, why = ampsafe.known_safe_after_exit(facts, ENABLED)
    assert not known and "telemetry shows the amplifier enabled" in why


def test_a_radar_signalled_before_its_start_line_is_never_trusted():
    # It has no SIGINT handler until then: it died in the middle of the SDR set-up.
    known, why = ampsafe.known_safe_after_exit(EARLY, never)
    assert not known and "in the middle of the SDR set-up" in why


def test_after_exit_actions():
    assert ampsafe.after_exit(CLEAN, NO_READING, "")[0] == "safe"
    assert ampsafe.after_exit(CLEAN, SHUTDOWN, "")[0] == "safe"
    assert ampsafe.after_exit(CLEAN, ENABLED, "")[0] == "run"
    assert ampsafe.after_exit(KILLED, NO_READING, "")[0] == "run"
    assert ampsafe.after_exit(KILLED, SHUTDOWN, "")[0] == "safe"
    assert ampsafe.after_exit(EARLY, never, "")[0] == "run"


@pytest.mark.parametrize("facts", [KILLED, EARLY, dict(CLEAN, exit_status=1), dict(CLEAN, reached_running=False)])
def test_after_exit_on_a_power_loss_after_an_unclean_exit_checks_nothing_and_writes_nothing(facts):
    action, why = ampsafe.after_exit(facts, never, "the stop came from the power service")
    assert action == "leave" and "power loss (the stop came from the power service)" in why


def test_after_exit_on_a_power_loss_a_clean_exit_is_safe_from_the_facts_alone():
    # Reached running, SIGINT, status 0: no telemetry is needed (and none is waited for).
    action, why = ampsafe.after_exit(CLEAN, never, "the stop came from the power service")
    assert action == "safe" and "clean radar exit" in why and "no reading waited for" in why
    assert ampsafe.after_exit(CLEAN, never, "the 28 V rail reads absent")[0] == "safe"


def test_exit_codes_of_the_tool_are_named():
    assert ampsafe.EXIT_MEANING == {0: "OK", 2: "NOT_FOUND", 3: "ASSUME_ENABLED", 4: "TIMEOUT", 5: "FAILED",
                                    6: "BUSY", 7: "AMBIGUOUS", 8: "STATE_FILE", 64: "USAGE"}
    assert ampsafe.RETRY_LATER == (2, 6, 4)
    assert ampsafe.describe(6) == "status 6 (BUSY)" and ampsafe.describe(None) == "not started"
    assert ampsafe.describe(8) == "status 8 (STATE_FILE)"
    assert "killed" in ampsafe.describe(ampsafe.KILLED_BY_CALLER) and "not a status of the tool" in ampsafe.describe(1)


def test_idle_sdr_still_in_its_boot_loader_is_safe_without_the_tool():
    # Nothing has initialised it since it was powered: also clears a record left by a hard power cut.
    assert ampsafe.idle_need("unknown", "bootloader", None) == (
        "mark_safe", "nothing has initialised the SDR since it was powered")
    assert ampsafe.idle_need("unknown", "bootloader", True)[0] == "mark_safe"
    assert ampsafe.idle_need("safe", "bootloader", None)[0] == "none"


def test_idle_unknown_state_runs_the_tool_unless_a_reading_decides():
    # Manager start with the record "unknown", or with no record at all, after the SDR has been in use.
    action, why = ampsafe.idle_need("unknown", "loaded", None)
    assert action == "run" and "not known" in why
    assert ampsafe.idle_need("unknown", "loaded", True) == (
        "mark_safe", "telemetry shows shutdown current with no radar process")
    assert ampsafe.idle_need("unknown", "loaded", False)[0] == "run"
    assert ampsafe.idle_need("safe", "loaded", None)[0] == "none"
    assert ampsafe.idle_need("safe", "loaded", True)[0] == "none"


def test_idle_enabled_current_is_never_marked_safe_and_is_left_to_the_watcher():
    # The safety watcher owns "enabled current with no radar process: run the tool".
    action, why = ampsafe.idle_need("safe", "loaded", False)
    assert action == "none" and "left to the safety watcher" in why
    # Enabled current wins over the boot-loader rule: no "safe" is written while it is seen.
    assert ampsafe.idle_need("unknown", "bootloader", False)[0] == "none"
    assert ampsafe.idle_need("safe", "bootloader", False)[0] == "none"


def test_idle_waits_for_a_reading_that_is_due_before_it_runs_the_tool():
    # Right after a run (or the manager's start) the reading is not yet old enough to count.
    action, why = ampsafe.idle_need("unknown", "loaded", ampsafe.PENDING)
    assert action == "none" and "waiting for a reading" in why
    assert ampsafe.idle_need("safe", "loaded", ampsafe.PENDING)[0] == "none"


def test_idle_without_an_sdr_nothing_is_done():
    for state in ("safe", "unknown"):
        for reading in (None, True, False):
            assert ampsafe.idle_need(state, "absent", reading)[0] == "none"


# ------------------------------------------------- the reading from the telemetry file

def amp_telemetry(env, **kw):
    return ampsafe.AmpTelemetry(env.config, **kw)


def now_uptime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def publish_amp(env, current, voltage=28.0, age=0.0, fresh=True, gap=0.25):
    up = now_uptime() - age
    data = {"amp": {"fresh": fresh, "current_a": current, "voltage_v": voltage, "sample_uptime_s": up}}
    if gap is not None:
        data["loop"] = {"last_gap_s": gap, "recent_longest_gap_s": gap}
    env.publish("telemetry", data)
    return up


def test_field_names_match_the_telemetry_service():
    # As radar_runner/telemetry/service.py publishes them (see also the live-file test in test_manager_live.py).
    cfg = MANAGER["amp_safe"]
    assert (cfg["current_field"], cfg["voltage_field"], cfg["sample_uptime_field"], cfg["loop_gap_field"]) == (
        "amp.current_a", "amp.voltage_v", "amp.sample_uptime_s", "loop.last_gap_s")
    from radar_runner.telemetry import service as telemetry_service
    import inspect
    source = inspect.getsource(telemetry_service)
    for name in ('"fresh"', '"reason"', '"current_a"', '"voltage_v"', '"sample_uptime_s"', '"last_gap_s"'):
        assert name in source, name


def test_confirm_shutdown_from_the_telemetry_file(env):
    exit_uptime = now_uptime() - 1.0
    publish_amp(env, 0.156)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime) == (True, "amplifier current 0.16 A at 28.0 V")
    publish_amp(env, 2.47, 27.5)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime) == (False, "amplifier current 2.47 A at 27.5 V")
    publish_amp(env, 0.5)                                   # at the threshold counts as enabled
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is False


def test_confirm_needs_a_reading_taken_after_the_exit(env):
    publish_amp(env, 0.156, age=2.0)                        # read before the radar exited
    exit_uptime = now_uptime()
    state, text = amp_telemetry(env).confirm_shutdown(exit_uptime)
    assert state is None and text == "no fresh amplifier reading after the exit"


def test_sample_lag_a_row_read_just_after_the_exit_was_measured_before_it(env):
    # sample_uptime_s is when the row reached the Pi; the ADC measured it earlier. With the
    # default 2 s, a row read 1.5 s after the exit does not count, one read 2.1 s after does.
    env.config["manager"]["amp_safe"].update(confirm_settle_s=2.0, confirm_timeout_s=0.3)
    exit_uptime = now_uptime() - 1.5
    publish_amp(env, 0.156)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None
    # Without waiting: pending while a reading that counts is still due (confirm_timeout_s more) ...
    state, text = amp_telemetry(env).now(exit_uptime)
    assert state == ampsafe.PENDING and "waiting for a reading taken 2 s after the last run ended" in text
    # ... and unusable when that time has passed and the rows are still not newer (sample time stuck).
    late = amp_telemetry(env, uptime=lambda: exit_uptime + 2.0 + 0.3 + 0.01)
    state, text = late.now(exit_uptime)
    assert state is None and "not read 2 s after the last run ended" in text
    exit_uptime = now_uptime() - 2.1
    publish_amp(env, 0.156)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is True
    assert amp_telemetry(env).now(exit_uptime)[0] is True


def test_enabled_current_needs_no_settle_time(env):
    env.config["manager"]["amp_safe"]["confirm_settle_s"] = 2.0
    publish_amp(env, 2.5)
    assert amp_telemetry(env).now(now_uptime())[0] is False


def test_confirm_waits_for_the_reading(env):
    exit_uptime = now_uptime()
    env.config["manager"]["amp_safe"]["confirm_timeout_s"] = 3.0
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        publish_amp(env, 0.16)                              # the telemetry service's next sample
    assert amp_telemetry(env, sleep=sleep).confirm_shutdown(exit_uptime)[0] is True and len(sleeps) == 1


def test_a_reading_from_a_stalled_telemetry_loop_is_no_proof_of_shutdown(env):
    # After a stall the rows that piled up come out together: their times say little.
    exit_uptime = now_uptime() - 1.0
    publish_amp(env, 0.156, gap=3.5)
    state, text = amp_telemetry(env).confirm_shutdown(exit_uptime)
    assert state is None and "the telemetry loop has just stalled" in text
    publish_amp(env, 0.156, gap=None)                       # a file without the loop field: no reading
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None
    assert amp_telemetry(env).now(exit_uptime - 10.0)[0] is None
    publish_amp(env, 2.5, gap=3.5)                          # enabled current is believed all the same
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is False
    publish_amp(env, 0.156, gap=0.26)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is True


def test_one_stall_means_wait_for_the_next_reading_not_no_reading(env):
    clock = [100.0]
    amp = amp_telemetry(env, monotonic=lambda: clock[0])
    after = now_uptime() - 10.0                             # the last run ended long ago
    publish_amp(env, 0.156, gap=3.5)
    state, text = amp.now(after)
    assert state == ampsafe.PENDING and "waiting for the next reading" in text
    assert ampsafe.idle_need(ampsafe.UNKNOWN, ampsafe.LOADED, state)[0] == "none"      # the tool does not run
    clock[0] += 0.5
    publish_amp(env, 0.156, gap=0.25)                       # the next round is even again
    assert amp.now(after)[0] is True
    # A loop that stays stalled is "no reading" after confirm_timeout_s (0.5 s here) ...
    publish_amp(env, 0.156, gap=3.5)
    assert amp.now(after)[0] == ampsafe.PENDING
    clock[0] += 0.6
    assert amp.now(after)[0] is None
    # ... and enabled current is believed at once, stall or not.
    publish_amp(env, 2.5, gap=3.5)
    assert amp.now(after)[0] is False


def test_the_wait_for_a_reading_ends_when_the_stop_becomes_urgent(env):
    sleeps = []
    amp = amp_telemetry(env, sleep=sleeps.append)
    publish_amp(env, 0.156, age=5.0)                        # only a reading from before the exit
    state, text = amp.confirm_shutdown(now_uptime(), urgent=lambda: len(sleeps) >= 1)
    assert state is None and "cut short" in text and len(sleeps) == 1


def test_confirm_says_nothing_without_the_28_v_rail_or_fresh_telemetry(env):
    exit_uptime = now_uptime() - 1.0
    publish_amp(env, 0.0, voltage=0.3)                      # power loss: zero current means nothing
    state, text = amp_telemetry(env).confirm_shutdown(exit_uptime)
    assert state is None and "28 V rail reads 0.3 V" in text
    assert amp_telemetry(env).rail_absent() is True
    publish_amp(env, 0.16, fresh=False)
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None
    assert amp_telemetry(env).rail_absent() is False        # not fresh: says nothing either way
    env.publish("telemetry", {"amp": {"fresh": True, "current_a": None, "voltage_v": 28.0, "sample_uptime_s": 1e12},
                              "loop": {"last_gap_s": 0.25}})
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None
    os.unlink(os.path.join(env.run_dir, "telemetry.json"))
    assert amp_telemetry(env).confirm_shutdown(exit_uptime)[0] is None
    assert amp_telemetry(env).now(exit_uptime) == (None, "no fresh amplifier reading")
    assert amp_telemetry(env).rail_absent() is False


def test_sdr_state_record(env):
    state = ampsafe.SdrState(env.state_dir)
    assert state.get() == "unknown" and not state.known_safe and state.why() == "no record"    # no file
    assert state.set("unknown", "radar launched, collection x") == "" and state.get() == "unknown"
    assert state.set("safe", "clean radar exit") == "" and state.known_safe
    assert ampsafe.SdrState(env.state_dir).get() == "safe"  # a file: any other program can read it
    with open(os.path.join(env.state_dir, "sdr-state")) as f:
        lines = f.read().splitlines()
    assert lines[0] == "safe" and lines[1].endswith(" clean radar exit") and "clean radar exit" in state.why()
    assert os.listdir(env.state_dir) == ["sdr-state"]       # replaced by rename: no temporary file left
    # Anything that is not exactly "safe" on the first line is unknown.
    for content in ("", "SAFE\n", "safe-ish\n", "unknown\n", "\x00\x00", " safe\nsafe\n"):
        with open(os.path.join(env.state_dir, "sdr-state"), "w") as f:
            f.write(content)
        assert state.get() == ("safe" if content.strip().split("\n")[0] == "safe" else "unknown")
    assert "no-dir" in ampsafe.SdrState(os.path.join(env.tmp, "no-dir")).set("safe")


def test_the_lock_probe_sees_a_running_wrapper_and_nothing_else(env):
    import fcntl
    lock = os.path.join(env.tmp, "amp_safe_script")
    with open(lock, "w") as f:
        f.write("#!/bin/sh\n")
    assert ampsafe.tool_running(lock) is False
    assert ampsafe.tool_running(os.path.join(env.tmp, "missing")) is False      # no tool: none can be running
    holder = os.open(lock, os.O_RDONLY)                     # as the real wrapper: flock on its own script
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert ampsafe.tool_running(lock) is True
        assert ampsafe.tool_running(lock) is True           # the probe takes nothing itself
    finally:
        os.close(holder)
    assert ampsafe.tool_running(lock) is False
    # The lock file is the first word of the command unless a setting names another.
    cfg = common_config.DEFAULTS["manager"]["amp_safe"]
    assert ampsafe.tool_lock_path(cfg) == cfg["command"][0] and cfg["command"][0].endswith("sdr_gpio_safe/amp_safe")
    assert ampsafe.tool_lock_path(dict(cfg, lock_file="/x/y")) == "/x/y"
    # The real wrapper locks itself (read from its source; it is never run here).
    with open(cfg["command"][0].replace(INSTALLED, common_config.REPO)) as f:
        source = f.read()
    assert "SCRIPT = os.path.realpath(__file__)" in source and "fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)" in source


def test_the_record_is_not_marked_safe_while_another_run_of_the_tool_is_in_progress(env):
    import fcntl
    lock = os.path.join(env.tmp, "amp_safe_script")
    open(lock, "w").close()
    state = ampsafe.SdrState(env.state_dir, lock)
    state.set("unknown", "amp_safe: sdr_gpio_safe is starting")         # what the other wrapper wrote
    holder = os.open(lock, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        done, text = state.mark_safe("telemetry shows shutdown current")
        assert not done and "in progress" in text and state.get() == "unknown"
    finally:
        os.close(holder)
    assert state.mark_safe("telemetry shows shutdown current") == (True, "") and state.get() == "safe"


def test_safe_is_remembered_in_memory_when_the_record_cannot_be_written(env):
    state = ampsafe.SdrState(env.state_dir)
    state.set("unknown", "radar launched")
    os.chmod(env.state_dir, 0o500)                          # the state directory is not writable
    try:
        done, error = state.mark_safe("clean radar exit")
        assert done and "Permission denied" in error
        assert state.get() == "safe" and state.known_safe   # for this manager ...
        assert "could not be written" in state.problem and "kept in memory only" in state.problem
        assert "the file says: " in state.why()
        assert ampsafe.SdrState(env.state_dir).get() == "unknown"      # ... not for another program
        assert ampsafe.idle_need(state.get(), ampsafe.LOADED, None)[0] == "none"    # the tool is not asked for again
    finally:
        os.chmod(env.state_dir, 0o700)
    # Anybody writing the record ends the memory: the file decides again.
    ampsafe.SdrState(env.state_dir).set("unknown", "amp_safe: sdr_gpio_safe is starting")
    assert state.get() == "unknown" and state.problem == ""
    state.remember_safe("x")
    state.set("unknown", "radar launched")                  # and so does this manager's own "unknown"
    assert state.get() == "unknown"
    state.remember_safe("x")
    assert state.set("safe", "ok") == "" and state.problem == "" and state.get() == "safe"


def test_sdr_state_is_replaced_never_edited_in_place(env):
    state = ampsafe.SdrState(env.state_dir)
    state.set("unknown", "x")
    before = os.stat(state.path).st_ino
    state.set("safe", "y")
    assert os.stat(state.path).st_ino != before             # a new file was renamed over the old one


def test_record_written_by_the_real_wrapper_format_is_read(env):
    # tools/sdr_gpio_safe/amp_safe writes "safe" or "unknown" on the first line and free text after.
    path = os.path.join(env.state_dir, "sdr-state")
    with open(path, "w") as f:
        f.write("safe\namp_safe OK (exit 0, 2.71 s) at 2026-11-19T08:00:00Z\n")
    assert ampsafe.SdrState(env.state_dir).known_safe
    with open(path, "w") as f:
        f.write("unknown\namp_safe: tool starting at 2026-11-19T08:00:00Z\n")
    assert not ampsafe.SdrState(env.state_dir).known_safe


# --------------------------------------------------------------- the stop hook script

def stop_hook(env, tool_status, product=None, options=()):
    """Run bin/radar-manager-stop-hook with a stand-in tool. Returns (exit status, calls of the tool)."""
    import subprocess
    from radar_runner.common import config as common_config
    tool = os.path.join(env.tmp, "amp_safe")
    calls = os.path.join(env.tmp, "hook_calls")
    with open(tool, "w") as f:
        f.write(f'#!/bin/sh\necho "$@" >> {calls}\nexit {tool_status}\n')
    os.chmod(tool, 0o755)
    if product is not None:
        env.sdr(speed=480 if product == "WestBridge" else 5000, product=product)
    hook = os.path.join(common_config.REPO, "bin", "radar-manager-stop-hook")
    result = subprocess.run([hook, *options, os.path.join(env.state_dir, "sdr-state"), tool, env.sysfs], timeout=20)
    try:
        with open(calls) as f:
            return result.returncode, f.read().splitlines()
    except OSError:
        return result.returncode, []


def test_stop_hook_does_nothing_when_the_record_says_safe(env):
    ampsafe.SdrState(env.state_dir).set("safe", "clean radar exit")
    assert stop_hook(env, 0) == (0, [])


def test_stop_hook_runs_the_tool_with_the_state_file_when_the_record_is_not_safe(env):
    state = ampsafe.SdrState(env.state_dir)
    state.set("unknown", "radar launched, collection x")    # as a killed manager leaves it
    code, calls = stop_hook(env, 0)
    assert code == 0 and calls == [f"--timeout 30 --state-file {state.path}"]
    code, calls = stop_hook(env, 3)                         # the tool's status is passed on
    assert code == 3 and len(calls) == 2


def test_stop_hook_as_the_power_service_hook_passes_its_timeout_and_runs_only_when_needed(env):
    state = ampsafe.SdrState(env.state_dir)
    state.set("unknown", "radar launched, collection x")
    code, calls = stop_hook(env, 0, options=("--timeout", "10"))
    assert code == 0 and calls == [f"--timeout 10 --state-file {state.path}"]
    state.set("safe", "clean radar exit; power loss")      # what the manager records after a clean stop
    assert stop_hook(env, 0, options=("--timeout", "10"))[1] == calls           # no further call
    state.set("unknown", "radar launched, collection y")
    assert stop_hook(env, 0, product="WestBridge", options=("--timeout", "10"))[1] == calls


def test_stop_hook_with_no_record_runs_the_tool(env):
    assert stop_hook(env, 6)[0] == 6                        # no record = unknown


def test_stop_hook_leaves_an_sdr_in_its_boot_loader_alone(env):
    # Nothing has initialised it since power-up: a run of the tool would be the first enable.
    ampsafe.SdrState(env.state_dir).set("unknown", "left by a hard power cut")
    assert stop_hook(env, 0, product="WestBridge") == (0, [])
    assert stop_hook(env, 0, product="")[1] != []           # firmware loaded: the tool runs
