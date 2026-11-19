"""The ORCA runner (radar_runner/manager/runner.py) against the stand-in ORCA tree.

Real processes and real time, with a stand-in `radar` that copies the path
rules, the burst arithmetic, the late SIGINT handler and the output lines of
ORCA's sdr/main.cpp, and a stand-in for the make-amplifier-safe tool. No SDR is
opened and the real tool is never run.
"""
import glob
import hashlib
import json
import os
import signal
import sys
import time

import pytest
import yaml

from radar_runner.manager import ampsafe
from radar_runner.manager import limits
from radar_runner.manager.runner import OutputLog, Runner

REQUEST = {"trigger": "button", "attempt": 1, "resume": False}


class Telemetry:
    """Stand-in for ampsafe.AmpTelemetry: the reading after an exit, and whether the 28 V rail is there."""

    def __init__(self, state=None, text="no fresh amplifier reading after the exit", rail_absent=False):
        self.state, self.text, self.absent = state, text, rail_absent
        self.confirms = 0

    def confirm_shutdown(self, exit_uptime):
        self.confirms += 1
        return self.state, self.text

    def rail_absent(self):
        return self.absent

    def now(self, after_uptime):
        return self.state, self.text


@pytest.fixture
def runner(env, events):
    r = Runner(env.config, events, amp_telemetry=Telemetry())
    yield r
    r.shutdown(10.0)


def run_and_stop(env, events, runner, record_s=0.3, **behaviour):
    env.behave(**behaviour)
    runner.start(REQUEST)
    events.wait("started")
    time.sleep(record_s)
    runner.stop("test stop")
    return events.wait("ended")


def chunks_of(directory):
    return sorted((f for f in os.listdir(directory) if f.startswith("rx_samps.bin.")),
                  key=lambda f: int(f.rsplit(".", 1)[1]))


def stdout_text(directory):
    parts = sorted((f for f in os.listdir(directory) if f.startswith("uhd_stdout.log")),
                   key=lambda f: int(f.rsplit(".", 1)[1]) if f[-1].isdigit() else 0)
    return "".join(open(os.path.join(directory, p)).read() for p in parts)


# ------------------------------------------------------------------ a normal run

def test_a_run_writes_everything_into_its_own_directory(env, events, runner):
    info = run_and_stop(env, events, runner, record_s=0.5)
    assert events.names() == ["collection", "started", "ended"]
    assert info["requested"] and info["failure"] == "" and info["exit_status"] == 0
    assert info["started"] and info["launched"] and not info["killed"] and not info["signalled_before_start"]
    # A clean exit leaves the pin safe: the tool is not run.
    assert info["amp_safe"] is True and info["amp_safe_command_run"] is False and env.amp_safe_calls() == 0

    name = events.wait("collection")["name"]
    d = os.path.join(env.root, name)
    files = sorted(os.listdir(d))
    # Data in chunks, written by the radar itself, in the collection directory.
    chunks = chunks_of(d)
    assert len(chunks) >= 3 and chunks[0] == "rx_samps.bin.0"
    for expected in ("chirp.bin", "collection.json", "config.yaml", "config_requested.yaml", "gps_log.txt",
                     "prepare.log", "uhd_stdout.log", "uhd_stderr.log"):
        assert expected in files
    # Nothing was written into the ORCA tree or anywhere else.
    assert not glob.glob(os.path.join(env.orca, "**", "rx_samps*"), recursive=True)
    assert not glob.glob(os.path.join(env.orca, "**", "chirp.bin"), recursive=True)
    assert os.listdir(env.root) == [name]
    # Every closed chunk holds whole records of the size the metadata gives.
    meta = env.metadata()
    record = meta["data"]["record_bytes"]
    assert record == 2352 * 8
    for chunk in chunks[:-1]:
        assert os.path.getsize(os.path.join(d, chunk)) == 2 * record      # chunk_max_s = 2 records
    assert info["chunks_closed"] == len(chunks)


def test_record_size_in_the_metadata_is_what_the_radar_writes(env, events, runner):
    # The radar sizes a record from RF1.rx_rate (as ORCA does), and so must the metadata.
    run_and_stop(env, events, runner, record_s=0.3)
    d = env.collection_dir()
    assert "INFO: Number of RX samples: 2352" in stdout_text(d) and "INFO: Number of TX samples: 728" in stdout_text(d)
    assert os.path.getsize(os.path.join(d, "rx_samps.bin.0")) % env.metadata()["data"]["record_bytes"] == 0


def test_collection_name_has_boot_number_sequence_and_utc_time(env, events, runner):
    for count in (1, 2):
        runner.start(REQUEST)
        events.wait("started", count=count)
        runner.stop("next")
        events.wait("ended", count=count)
    first, second = env.collections()
    assert first[:11] == "b00001_s001" and second[:11] == "b00001_s002"
    stamp = first.split("_")[2]
    assert len(stamp) == 16 and stamp.endswith("Z") and stamp[8] == "T"
    assert abs(time.mktime(time.strptime(stamp, "%Y%m%dT%H%M%SZ")) - time.mktime(time.gmtime())) < 60


def test_a_lost_boot_counter_does_not_reuse_numbers(env, events):
    os.mkdir(os.path.join(env.root, "b00041_s007_20260101T000000Z"))
    r = Runner(env.config, events, amp_telemetry=Telemetry())
    r.start(REQUEST)
    events.wait("started")
    r.shutdown(10)
    assert env.collections()[-1].startswith("b00042_s001_")


def test_the_boot_number_comes_from_one_provider(env, events):
    r = Runner(env.config, events, amp_telemetry=Telemetry(), boot_number=lambda state_dir, floor=0: (77, ""))
    r.start(REQUEST)
    events.wait("started")
    r.shutdown(10)
    assert env.collections()[-1].startswith("b00077_s001_")


def test_derived_config_is_what_the_radar_read_and_passes_the_limits(env, events, runner):
    run_and_stop(env, events, runner, record_s=0.1)
    d = env.collection_dir()
    with open(os.path.join(d, "config.yaml")) as f:
        text = f.read()
    derived = limits.load(text)
    assert limits.check(derived, env.config["manager"]["limits"]) == []
    assert limits.check_output(derived, env.orca, env.root) == []
    # Relative to the ORCA checkout: the only form that the unchanged radar and chirp generator both accept.
    out = derived["FILES"]["output_dir"]
    assert not os.path.isabs(out)
    assert os.path.realpath(os.path.join(env.orca, out)) == os.path.realpath(d)
    assert derived["FILES"]["max_chirps_per_file"] == 2000          # 0.4 s / (200 us x 1000 presums) records
    assert derived["RF0"]["tx_gain"] == 72.0 and derived["GPIO"]["pwr_amp_pin"] == 2
    assert derived["RF1"]["tx_rate"] == derived["RF0"]["tx_rate"] == derived["GENERATE"]["sample_rate"] == 56e6
    assert "&" not in text and "*" not in text                      # plain YAML, no anchors
    assert yaml.safe_load(text)["CHIRP"]["tx_duration"] == pytest.approx(1.3e-5)
    # The requested file is kept byte for byte.
    with open(os.path.join(d, "config_requested.yaml"), "rb") as a, open(env.radar_config, "rb") as b:
        assert a.read() == b.read()
    # The radar's own first line shows the path it was given.
    first = stdout_text(d).splitlines()[0]
    assert "Reading from config file: ../../" in first and first.rstrip().endswith("config.yaml")


def test_output_log_has_run_py_format_and_the_protocol_lines(env, events, runner):
    run_and_stop(env, events, runner)
    lines = stdout_text(env.collection_dir()).splitlines()
    for line in lines:
        stamp, sep, _ = line.partition("] \t")
        assert sep and stamp[0] == "[" and abs(float(stamp[1:]) - time.time()) < 60
    text = "\n".join(lines)
    for marker in ("[VERSION] 0.0.2", "[OPEN FILE] ", "[START] Beginning main loop", "[CLOSE FILE] ",
                   "[RX] Reached stop signal handling", "[TX] Done.", "[RX] Total pulses written"):
        assert marker in text


def test_uhd_messages_go_to_their_own_file(env, events, runner):
    run_and_stop(env, events, runner, record_s=0.1, radar="stderr")
    with open(os.path.join(env.collection_dir(), "uhd_stderr.log")) as f:
        text = f.read()
    assert "[INFO] [B200] Operating over USB 3." in text and "something UHD says on stderr" in text and "LLLL" in text
    assert "LLLL" not in stdout_text(env.collection_dir()) and "Operating over USB" not in stdout_text(env.collection_dir())
    assert env.metadata()["end"]["stderr_log"]["bytes"] > 0


def test_metadata_is_valid_from_the_start_and_completed_at_the_end(env, events, runner):
    runner.start(REQUEST, flags=["no_amp_telemetry"])
    events.wait("collection")
    events.wait("started")
    time.sleep(0.2)
    meta = env.metadata()                       # as a reader finds it after a hard power cut
    assert meta["end"] is None and meta["recording_started"]["uptime_s"] > 0
    assert meta["trigger"] == "button" and meta["flags"] == ["no_amp_telemetry"]
    assert meta["boot"] == 1 and meta["sequence"] == 1 and meta["name"] == env.collections()[0]
    assert meta["usb_speed_mbps"] == 5000
    runner.add_flag("something_later")
    runner.stop("done")
    events.wait("ended")
    meta = env.metadata()
    end = meta["end"]
    assert end["how"] == "stopped" and end["reason"] == "done" and end["exit_status"] == 0
    assert end["killed"] is False and end["amp_safe"] is True and end["amp_safe_command_run"] is False
    assert end["data_files_closed"] >= 1
    assert meta["flags"] == ["no_amp_telemetry", "something_later"]
    assert end["uptime_s"] >= meta["recording_started"]["uptime_s"] >= meta["created"]["uptime_s"]


def test_metadata_reports_the_size_of_the_output_log(env, events, runner):
    run_and_stop(env, events, runner, record_s=0.2)
    d = env.collection_dir()
    log = env.metadata()["end"]["log"]
    parts = [f for f in os.listdir(d) if f.startswith("uhd_stdout.log")]
    assert log["bytes"] == sum(os.path.getsize(os.path.join(d, p)) for p in parts) > 0
    assert log["lines"] == len(stdout_text(d).splitlines()) and log["parts"] == len(parts) and log["error"] == ""


def test_metadata_records_the_orca_working_tree_and_the_radar_binary(env, events, runner):
    import subprocess
    git = lambda *args: subprocess.run(["git", "-C", env.orca, *args], check=True, capture_output=True)
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "stand-in")
    env.config["manager"]["orca"]["radar_command"] = ["radar.py", "{config_rel}"]     # hash the "binary" itself
    env.config["manager"]["orca"]["launch_prefix"] = ["/usr/bin/setpriv", "--pdeathsig", "KILL", sys.executable]
    run_and_stop(env, events, runner, record_s=0.1)
    meta = env.metadata()
    assert meta["orca"]["dirty"] is False and meta["orca"]["changed"] == [] and len(meta["orca"]["commit"]) == 40
    binary = os.path.join(env.orca, "sdr", "build", "radar.py")
    with open(binary, "rb") as f:
        assert meta["radar_binary"] == {"path": os.path.realpath(binary) if False else meta["radar_binary"]["path"],
                                        "sha256": hashlib.sha256(f.read()).hexdigest()}
    assert meta["radar_binary"]["path"].endswith("sdr/build/radar.py")
    # An uncommitted change (ORCA has an approved one right now) is recorded with the next collection.
    with open(binary, "a") as f:
        f.write("# local change\n")
    run_and_stop(env, events, runner, record_s=0.1)
    events.wait("ended", count=2)
    meta = env.metadata()
    assert meta["orca"]["dirty"] is True and meta["orca"]["changed"] == ["sdr/build/radar.py"]
    with open(binary, "rb") as f:
        assert meta["radar_binary"]["sha256"] == hashlib.sha256(f.read()).hexdigest()


def test_a_radar_with_a_pulse_count_finishes_by_itself(env, events, runner):
    with open(env.radar_config) as f:
        text = f.read().replace("num_pulses: -1", "num_pulses: 3000")
    with open(env.radar_config, "w") as f:
        f.write(text)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert not info["requested"] and info["failure"] == "" and info["interrupted"] == ""
    assert info["exit_status"] == 0 and info["started"]
    assert env.metadata()["end"]["how"] == "finished" and env.amp_safe_calls() == 0


def test_a_radar_that_runs_until_stopped_and_exits_with_status_0_unasked_is_interrupted(env, events, runner):
    env.behave(radar="exit0")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert not info["requested"] and info["failure"] == "" and info["exit_status"] == 0
    assert info["interrupted"] == "the radar exited with status 0 without being asked to (num_pulses is -1)"
    assert env.metadata()["end"]["how"] == "interrupted"
    assert info["amp_safe"] is True and env.amp_safe_calls() == 0      # it was a clean exit all the same


# ------------------------------------------------------------- failures of the start

def test_config_that_fails_the_limits_is_fatal_and_starts_nothing(env, events, runner):
    env.config["manager"]["limits"]["max_tx_gain"] = 43.0
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["fatal"] and info["kind"] == "config" and "RF0.tx_gain 72 is above the cap of 43" in info["failure"]
    assert not info["launched"] and info["amp_safe"] is None
    assert env.collections() == [] and env.amp_safe_calls() == 0


def test_config_with_rf1_rates_that_differ_is_refused_at_the_start(env, events, runner):
    with open(env.radar_config) as f:
        text = f.read()
    rf1 = text.index("RF1:")
    text = text[:rf1] + text[rf1:].replace("tx_rate: 56000000.0", "tx_rate: 224000000.0", 1)
    with open(env.radar_config, "w") as f:
        f.write(text)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["fatal"] and "RF1.tx_rate (2.24e+08) differs from RF0's" in info["failure"] and not info["launched"]


def test_missing_config_is_fatal(env, events, runner):
    os.unlink(env.radar_config)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["fatal"] and info["kind"] == "config" and not info["launched"]


@pytest.mark.parametrize("behaviour, kind, text", [
    ({"chirp": "fail"}, "chirp", "chirp generation failed (status 1"),
    ({"chirp": "nofile"}, "chirp", "wrote no chirp file"),
    ({"build": "fail"}, "build", "build failed (status 2"),
])
def test_failed_chirp_or_build_is_a_failed_start_without_a_radar(env, events, runner, behaviour, kind, text):
    env.behave(**behaviour)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == kind and text in info["failure"] and not info["fatal"]
    assert not info["launched"] and info["amp_safe"] is None and env.amp_safe_calls() == 0
    meta = env.metadata()
    assert meta["end"]["how"] == "failed" and meta["recording_started"] is None
    assert not os.path.exists(os.path.join(env.state_dir, "sdr-state"))     # the SDR was never touched


def test_slow_chirp_generation_times_out(env, events, runner):
    env.config["manager"]["orca"]["chirp_timeout_s"] = 0.3
    env.behave(chirp="slow")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "chirp" and "no result within 0.3 s" in info["failure"]


def test_radar_that_fails_to_start_is_not_known_safe_and_the_tool_runs(env, events, runner):
    env.behave(radar="fail_start")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "exit" and "status 1 before it started recording" in info["failure"]
    assert info["launched"] and not info["started"] and "started" not in events.names()
    assert any("No devices found" in line for line in info["output_tail"])
    assert info["amp_safe"] is True and info["amp_safe_command_run"] is True and env.amp_safe_calls() == 1
    assert env.sdr_state() == "safe"


def test_missing_radar_program(env, events, runner):
    env.config["manager"]["orca"]["radar_command"] = ["./no-such-radar", "{config_rel}"]
    env.config["manager"]["orca"]["launch_prefix"] = []
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "launch" and "is not there after the build" in info["failure"]
    assert not info["launched"] and env.amp_safe_calls() == 0
    assert not os.path.exists(os.path.join(env.state_dir, "sdr-state"))


def test_launch_prefix_that_cannot_be_run_puts_the_record_back(env, events, runner):
    ampsafe.SdrState(env.state_dir).set("safe", "before")
    env.config["manager"]["orca"]["launch_prefix"] = ["/no/such/setpriv"]
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "launch" and not info["launched"]
    assert env.sdr_state() == "safe"            # nothing touched the SDR


# ------------------------------------------------------------- the turn-on check hook

def test_turn_on_check_hook_can_refuse_and_the_record_says_unknown_from_the_hook_on(env, events):
    seen = []

    def hook(run):
        seen.append(env.sdr_state())            # what the record says while the hook runs
        return False, "no direct signal"
    ampsafe.SdrState(env.state_dir).set("safe", "before")
    r = Runner(env.config, events, turn_on_check=hook, amp_telemetry=Telemetry())
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["fatal"] and info["kind"] == "turn_on_check" and "no direct signal" in info["failure"]
    assert not info["launched"] and env.amp_safe_calls() == 0
    assert seen == ["unknown"] and env.sdr_state() == "unknown"     # the hook ran a radar of its own


def test_turn_on_check_hook_that_passes(env, events):
    r = Runner(env.config, events, turn_on_check=lambda run: (True, ""), amp_telemetry=Telemetry())
    info = run_and_stop(env, events, r, record_s=0.1)
    assert info["failure"] == "" and env.sdr_state() == "safe"


# --------------------------------------------------- stopping in each phase of a start

def test_stop_during_chirp_generation_aborts_at_once_without_a_radar(env, events, runner):
    env.behave(chirp="slow")
    runner.start(REQUEST)
    events.wait("collection")
    time.sleep(0.3)
    assert runner.radar_gone() is False         # a start is under way: a radar may still come
    t0 = time.monotonic()
    runner.stop("changed my mind")
    assert runner.radar_gone() is True          # none will be launched now
    info = events.wait("ended")
    assert time.monotonic() - t0 < 2.0
    assert info["requested"] and info["failure"] == "" and not info["launched"] and info["amp_safe"] is None
    assert env.metadata()["end"]["how"] == "not_started" and env.amp_safe_calls() == 0
    assert not os.path.exists(os.path.join(env.state_dir, "sdr-state"))


def test_stop_during_the_sdr_set_up_is_held_until_the_start_line(env, events, runner):
    # ORCA has no SIGINT handler until its set-up is over: a signal now would kill it half way.
    env.behave(init_s=1.0)
    runner.start(REQUEST)
    events.wait("collection")
    time.sleep(0.6)                             # the radar is in its set-up
    assert runner.snapshot()["pid"] and not runner.snapshot()["recording"]
    t0 = time.monotonic()
    runner.stop("button")
    assert runner.radar_gone() is False
    info = events.wait("ended")
    assert 0.2 < time.monotonic() - t0 < 3.0    # it waited for the start line, then stopped cleanly
    assert info["exit_status"] == 0 and info["started"] and not info["signalled_before_start"]
    assert not info["killed"] and info["failure"] == ""
    assert "started" not in events.names()      # the run was never reported as running
    assert "[RX] Reached stop signal handling" in stdout_text(env.collection_dir())
    assert info["amp_safe"] is True and env.amp_safe_calls() == 0 and env.sdr_state() == "safe"


def test_urgent_stop_during_the_sdr_set_up_waits_only_the_grace_time_and_the_pin_is_not_trusted(env, events):
    # Power loss or the manager's exit: 0.3 s of grace here, then the signal, which kills the radar
    # in mid set-up. Telemetry showing shutdown current does not make that safe.
    telemetry = Telemetry(True, "amplifier current 0.16 A at 28.0 V")
    r = Runner(env.config, events, amp_telemetry=telemetry)
    env.behave(init_s=5.0)
    r.start(REQUEST)
    events.wait("collection")
    time.sleep(0.5)
    t0 = time.monotonic()
    r.stop("the manager is stopping", urgent=True)
    info = events.wait("ended")
    assert 0.25 < time.monotonic() - t0 < 2.5
    assert info["signalled_before_start"] and info["exit_status"] == -signal.SIGINT and not info["started"]
    assert info["amp_safe_command_run"] is True and env.amp_safe_calls() == 1 and telemetry.confirms == 0
    assert "in the middle of the SDR set-up" in info["amp_safe_detail"]
    assert "stopped_before_start" in env.metadata()["flags"]
    r.shutdown(5)


def test_urgent_stop_during_the_set_up_still_waits_for_a_start_line_that_comes_in_time(env, events, runner):
    env.config["manager"]["orca"]["urgent_grace_s"] = 2.0
    env.config["manager"]["orca"]["urgent_stop_budget_s"] = 4.0
    env.behave(init_s=0.5)
    runner.start(REQUEST)
    events.wait("collection")
    while not runner.snapshot().get("pid"):
        time.sleep(0.01)
    runner.stop("the manager is stopping", urgent=True)     # the start line comes within the grace time
    info = events.wait("ended")
    assert info["exit_status"] == 0 and not info["signalled_before_start"] and env.amp_safe_calls() == 0


def test_start_timeout_during_the_set_up_kills_the_radar_and_the_tool_runs(env, events, runner):
    env.config["manager"]["orca"]["start_timeout_s"] = 0.6
    env.behave(init_s=5.0)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "start_timeout" and "did not report [START] within 0.6 s" in info["failure"]
    assert info["signalled_before_start"] and info["exit_status"] == -signal.SIGINT
    assert info["amp_safe_command_run"] is True and env.amp_safe_calls() == 1 and info["amp_safe"] is True


def test_radar_that_never_prints_its_start_line(env, events, runner):
    env.config["manager"]["orca"]["start_timeout_s"] = 0.6
    env.behave(radar="no_start")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "start_timeout" and not info["started"] and not info["killed"]
    assert info["signalled_before_start"] and env.amp_safe_calls() == 1    # it cannot be told how far it got


def test_stop_after_the_start_line(env, events, runner):
    info = run_and_stop(env, events, runner, record_s=0.2)
    assert info["exit_status"] == 0 and not info["signalled_before_start"]


def test_nothing_touches_the_sdr_after_a_stop_between_build_and_launch(env, events):
    def hook(run):
        run.stop.set()                          # as if the stop arrived right before the launch
        return True, ""
    r = Runner(env.config, events, turn_on_check=hook, amp_telemetry=Telemetry())
    r.start(REQUEST)
    info = events.wait("ended")
    assert not info["launched"] and info["amp_safe"] is None and r.radar_gone()


# ------------------------------------------------------------------ USB speed

def test_usb_2_link_is_found_from_sysfs_after_the_start_line(env, events, runner):
    # Before its firmware is loaded the SDR is a USB 2 device and the gate lets it through;
    # the speed can be judged only once the radar has initialised it.
    env.sdr(speed=480, product="")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "usb2" and not info["fatal"]
    assert "the SDR is on a USB 2 link (480 Mbit/s at 4-1); the radar needs USB 3" in info["failure"]
    assert "started" not in events.names() and info["exit_status"] == 0     # stopped cleanly, never "running"
    assert env.metadata()["end"]["how"] == "failed"


def test_usb_3_speed_is_recorded(env, events, runner):
    run_and_stop(env, events, runner, record_s=0.1)
    assert env.metadata()["usb_speed_mbps"] == 5000


def test_usb_speed_that_cannot_be_read_does_not_stop_the_run(env, events, runner):
    env.sdr(present=False)
    runner.start(REQUEST)
    events.wait("started", timeout=15)
    runner.stop("x")
    info = events.wait("ended")
    assert info["failure"] == "" and "usb_speed_unknown" in env.metadata()["flags"]


def test_usb_check_can_be_switched_off(env, events, runner):
    env.config["manager"]["orca"]["require_usb3"] = False
    env.sdr(speed=480, product="")
    info = run_and_stop(env, events, runner, record_s=0.1)
    assert info["failure"] == ""


# --------------------------------------------------------------- failures of the run

def test_radar_that_exits_with_an_error_mid_run(env, events, runner):
    env.behave(radar="crash")
    runner.start(REQUEST)
    events.wait("started")
    info = events.wait("ended")
    assert not info["requested"] and info["kind"] == "exit" and info["failure"] == "the radar exited with status 3"
    assert env.amp_safe_calls() == 1 and env.metadata()["end"]["how"] == "failed"


def test_radar_that_ignores_sigint_is_killed_as_a_last_resort(env, events, runner, caplog):
    env.config["manager"]["orca"]["stop_timeout_s"] = 0.4
    t0 = time.monotonic()
    info = run_and_stop(env, events, runner, record_s=0.3, radar="ignore_sigint")
    assert info["killed"] and info["exit_status"] == -signal.SIGKILL and info["requested"]
    assert info["failure"] == ""                # a stop that needed SIGKILL is not a failed run
    assert "LAST RESORT" in caplog.text and "SIGKILL" in caplog.text
    meta = env.metadata()
    assert meta["end"]["killed"] is True and "killed" in meta["flags"]
    assert env.amp_safe_calls() == 1            # killed: the pin is not known safe
    # The data written before the kill is whole records.
    d = env.collection_dir()
    chunks = chunks_of(d)
    assert chunks and all(os.path.getsize(os.path.join(d, c)) % meta["data"]["record_bytes"] == 0 for c in chunks[:-1])
    assert time.monotonic() - t0 < 8


def test_urgent_stop_has_the_radar_gone_within_its_budget_whatever_the_radar_does(env, events, runner):
    env.config["manager"]["orca"]["stop_timeout_s"] = 30.0          # the normal allowance is long here
    env.behave(radar="ignore_sigint")
    runner.start(REQUEST)
    events.wait("started")
    t0 = time.monotonic()
    runner.stop("power", urgent=True)
    while not runner.radar_gone():
        assert time.monotonic() - t0 < 3.0
        time.sleep(0.01)
    assert 0.8 < time.monotonic() - t0 < 1.6    # urgent_stop_budget_s = 1.0 in these tests
    assert events.wait("ended")["killed"]


def test_a_stop_can_become_urgent_while_it_waits(env, events, runner):
    env.config["manager"]["orca"]["stop_timeout_s"] = 30.0
    env.behave(radar="ignore_sigint")
    runner.start(REQUEST)
    events.wait("started")
    runner.stop("button")
    time.sleep(0.3)
    assert not runner.radar_gone()
    t0 = time.monotonic()
    runner.stop("power loss", urgent=True, power=True)
    info = events.wait("ended")
    assert time.monotonic() - t0 < 2.0 and info["killed"] and info["stop_reason"] == "button"


def test_radar_that_exits_slowly_but_in_time_is_not_killed(env, events, runner):
    env.config["manager"]["orca"]["stop_timeout_s"] = 4.0
    info = run_and_stop(env, events, runner, record_s=0.2, radar="slow_exit")
    assert not info["killed"] and info["exit_status"] == 0


def test_flood_of_output_is_all_kept_and_never_blocks_the_radar(env, events, runner):
    # The per-pulse lines are needed to process the data: nothing is capped or filtered.
    env.config["manager"]["data"]["log_part_mb"] = 0.05
    info = run_and_stop(env, events, runner, record_s=1.5, radar="flood")
    assert info["exit_status"] == 0 and not info["killed"]      # it could still stop cleanly: never blocked
    d = env.collection_dir()
    parts = [f for f in os.listdir(d) if f.startswith("uhd_stdout.log")]
    assert len(parts) >= 5 and "uhd_stdout.log" in parts and "uhd_stdout.log.1" in parts
    text = stdout_text(d)
    errors = [line for line in text.splitlines() if "[ERROR] (Chirp " in line]
    offsets = [line for line in text.splitlines() if "time_offset increased by" in line]
    assert len(errors) > 5000 and len(errors) == 200 * len(offsets)         # every line the radar printed
    numbers = [int(line.split("(Chirp ")[1].split(")")[0]) for line in errors]
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)    # none missing in between
    log = env.metadata()["end"]["log"]
    assert log["lines"] == len(text.splitlines()) and log["parts"] == len(parts)
    assert "log_truncated" not in env.metadata()["flags"]
    assert env.metadata()["end"]["data_files_closed"] >= 5      # and the radar kept writing data all the while


# ---------------------------------------------------------------------------- stop

def test_only_one_run_at_a_time(env, events, runner):
    runner.start(REQUEST)
    with pytest.raises(RuntimeError):
        runner.start(REQUEST)
    assert runner.busy
    assert runner.shutdown(10.0) and not runner.busy


def test_shutdown_stops_the_radar(env, events, runner):
    runner.start(REQUEST)
    events.wait("started")
    pid = runner.snapshot()["pid"]
    assert runner.shutdown(10.0)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert env.metadata()["end"]["reason"] == "the manager is stopping" and env.sdr_state() == "safe"


def test_snapshot_and_radar_gone_through_a_run(env, events, runner):
    assert runner.snapshot() == {"active": False} and runner.radar_gone() is True
    runner.start(REQUEST)
    events.wait("started")
    time.sleep(0.3)
    snap = runner.snapshot()
    assert snap["active"] and snap["recording"] and snap["pid"] > 0 and snap["collection"] == env.collections()[0]
    assert snap["writing"].startswith("rx_samps.bin.") and snap["log"]["bytes"] > 0 and snap["usb_speed_mbps"] == 5000
    assert runner.radar_gone() is False
    runner.stop("x")
    events.wait("ended")
    assert runner.radar_gone() is True and runner.snapshot() == {"active": False}


def test_radar_gone_is_true_while_the_tool_still_runs_after_the_exit(env, events, runner):
    # `radarctl stop --wait` returns when the radar process is gone, not when the amplifier step is over.
    env.config["manager"]["amp_safe"]["timeout_s"] = 3.0
    env.behave(radar="crash", amp_safe="hang")
    runner.start(REQUEST)
    events.wait("started")
    deadline = time.monotonic() + 5
    while not runner.radar_gone():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert runner.busy and "ended" not in events.names()    # the tool is still running
    assert runner.snapshot()["pid"] is None
    events.wait("ended", timeout=15)


# ------------------------------------------------- after the exit: is the pin safe?

def test_record_is_unknown_while_the_radar_runs_and_safe_after_a_clean_exit(env, events, runner):
    path = os.path.join(env.state_dir, "sdr-state")
    assert not os.path.exists(path) and env.sdr_state() == "unknown"       # no record = unknown
    runner.start(REQUEST)
    events.wait("started")
    with open(path) as f:
        lines = f.read().splitlines()
    assert lines[0] == "unknown" and "radar launched, collection b00001_s001_" in lines[1]
    runner.stop("x")
    info = events.wait("ended")
    assert env.sdr_state() == "safe" and info["amp_safe_detail"].startswith("tool not run: clean radar exit")
    with open(path) as f:
        assert f.readline() == "safe\n"                     # what the unit's stop hook tests


@pytest.mark.parametrize("state, text", [(True, "amplifier current 0.16 A at 28.0 V"),
                                         (None, "no fresh amplifier reading after the exit")])
def test_tool_does_not_run_after_a_clean_exit(env, events, state, text):
    r = Runner(env.config, events, amp_telemetry=Telemetry(state, text))
    info = run_and_stop(env, events, r, record_s=0.1)
    assert info["amp_safe"] is True and info["amp_safe_command_run"] is False and env.amp_safe_calls() == 0
    assert info["amp_safe_detail"].startswith("tool not run: ") and text in info["amp_safe_detail"]
    assert env.sdr_state() == "safe" and env.metadata()["end"]["amp_safe_command_run"] is False
    r.shutdown(5)


def test_tool_runs_if_telemetry_shows_enabled_current_after_a_clean_exit(env, events):
    r = Runner(env.config, events, amp_telemetry=Telemetry(False, "amplifier current 2.50 A at 27.5 V"))
    info = run_and_stop(env, events, r, record_s=0.1)
    assert info["amp_safe_command_run"] is True and env.amp_safe_calls() == 1 and info["amp_safe"] is True
    assert "telemetry shows the amplifier enabled" in info["amp_safe_detail"] and env.sdr_state() == "safe"
    r.shutdown(5)


@pytest.mark.parametrize("behaviour", ["crash", "fail_start", "ignore_sigint"])
@pytest.mark.parametrize("state, runs", [(None, 1), (False, 1), (True, 0)])
def test_after_an_unclean_exit_the_tool_runs_unless_a_reading_shows_shutdown(env, events, behaviour, state, runs):
    env.config["manager"]["orca"]["stop_timeout_s"] = 0.4
    r = Runner(env.config, events, amp_telemetry=Telemetry(state, "reading"))
    env.behave(radar=behaviour)
    r.start(REQUEST)
    if behaviour == "ignore_sigint":
        events.wait("started")
        r.stop("x")
    info = events.wait("ended")
    assert env.amp_safe_calls() == runs and info["amp_safe_command_run"] is bool(runs)
    assert info["amp_safe"] is True and env.sdr_state() == "safe"
    r.shutdown(5)


def test_the_tool_is_called_with_the_state_file_and_keeps_the_record_itself(env, events, runner):
    env.behave(radar="crash")
    runner.start(REQUEST)
    events.wait("ended")
    with open(env.amp_safe_log) as f:
        call = f.read().split(None, 1)[1].strip()
    assert call == f"--timeout 2 --state-file {os.path.join(env.state_dir, 'sdr-state')}"
    with open(os.path.join(env.state_dir, "sdr-state")) as f:
        lines = f.read().splitlines()
    assert lines[0] == "safe" and "amp_safe: GPIO_0 high, verified" in lines[1]     # written by the tool, not by us
    with open(os.path.join(env.collection_dir(), "amp_safe.log")) as f:
        assert "make amplifier safe" in f.read()


@pytest.mark.parametrize("status, name", [(5, "FAILED"), (7, "AMBIGUOUS"), (8, "STATE_FILE"), (64, "USAGE")])
def test_a_failed_tool_after_an_unclean_exit_is_not_safe(env, events, runner, status, name):
    env.behave(radar="crash", amp_safe=status)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["amp_safe"] is False and env.amp_safe_calls() == 1
    assert f"tool run 1 time: status {status} ({name})" in info["amp_safe_detail"]
    assert env.metadata()["end"]["amp_safe"] is False and env.sdr_state() == "unknown"


def test_assume_enabled_is_retried_at_once_and_then_reported(env, events, runner, caplog):
    # Exit 3: the tool itself initialised the SDR and could not verify the pin.
    env.behave(radar="crash", amp_safe=3)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert env.amp_safe_calls() == 2                        # assume_enabled_retries = 1 in these tests
    assert info["amp_safe"] is False and "tool run 2 times: status 3 (ASSUME_ENABLED)" in info["amp_safe_detail"]
    assert env.sdr_state() == "unknown" and "URGENT" in caplog.text


def test_assume_enabled_then_ok_is_safe(env, events, runner):
    env.behave(radar="crash", amp_safe=[3, 0])
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["amp_safe"] is True and env.amp_safe_calls() == 2 and env.sdr_state() == "safe"


@pytest.mark.parametrize("first, name", [(2, "NOT_FOUND"), (6, "BUSY"), (4, "TIMEOUT"), ("lock", "TIMEOUT")])
def test_sdr_not_reachable_right_after_a_kill_is_tried_again_after_a_delay(env, events, runner, first, name,
                                                                          caplog):
    # After a radar was killed in its set-up the SDR may be re-enumerating; "lock" is another
    # caller holding the tool. One more try after unavailable_delay_s (0.1 s here), then it is a failure.
    env.config["manager"]["orca"]["start_timeout_s"] = 0.5
    env.behave(init_s=5.0, amp_safe=[first, 0])
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["signalled_before_start"] and env.amp_safe_calls() == 2
    assert info["amp_safe"] is True and "tool run 2 times: status 0 (OK)" in info["amp_safe_detail"]
    assert f"status 4 (TIMEOUT)" in caplog.text if name == "TIMEOUT" else name in caplog.text
    assert env.sdr_state() == "safe"


def test_sdr_that_stays_unreachable_becomes_a_failure_after_the_retries(env, events, runner):
    env.behave(radar="crash", amp_safe=2)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert env.amp_safe_calls() == 2            # unavailable_retries = 1 in these tests
    assert info["amp_safe"] is False and "tool run 2 times: status 2 (NOT_FOUND)" in info["amp_safe_detail"]


def test_another_caller_holding_the_tool_leaves_the_record_as_it_was(env, events, runner):
    env.behave(radar="crash", amp_safe="lock")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["amp_safe"] is False and "status 4 (TIMEOUT)" in info["amp_safe_detail"]
    with open(os.path.join(env.state_dir, "sdr-state")) as f:
        assert "radar launched" in f.read()     # still our "unknown": the tool never started


def test_delayed_retries_end_when_the_stop_becomes_urgent(env, events, runner):
    # The manager is asked to exit while the tool is being retried after a crash.
    env.config["manager"]["amp_safe"].update(unavailable_retries=50, unavailable_delay_s=0.2)
    env.behave(radar="crash", amp_safe=2)
    runner.start(REQUEST)
    events.wait("started")
    deadline = time.monotonic() + 10
    while env.amp_safe_calls() < 2:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    runner.stop("the manager is stopping", urgent=True)
    info = events.wait("ended", timeout=5)
    assert info["amp_safe"] is False and env.amp_safe_calls() < 10      # not all 50


def test_no_delayed_retries_when_the_stop_was_urgent(env, events, runner):
    env.config["manager"]["orca"]["stop_timeout_s"] = 0.3
    env.behave(radar="ignore_sigint", amp_safe=[2, 0])
    runner.start(REQUEST)
    events.wait("started")
    runner.stop("the manager is stopping", urgent=True)
    info = events.wait("ended")
    assert env.amp_safe_calls() == 1 and info["amp_safe"] is False      # the unit's stop hook takes over


def test_hanging_tool_is_killed_at_the_managers_limit(env, events, runner):
    env.config["manager"]["amp_safe"]["timeout_s"] = 0.5
    env.behave(radar="crash", amp_safe="hang")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["amp_safe"] is False and "no result within 0.5 s" in info["amp_safe_detail"]
    assert env.sdr_state() == "unknown"                     # the tool wrote "unknown" before it started


def test_tool_that_is_not_configured(env, events, runner):
    env.config["manager"]["amp_safe"]["command"] = []
    env.behave(radar="crash")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["amp_safe"] is False and "not configured" in info["amp_safe_detail"]


# ----------------------------------------------------------------------- power loss

def test_stop_from_the_power_service_checks_nothing_and_leaves_the_record(env, events):
    telemetry = Telemetry(True, "amplifier current 0.16 A at 28.0 V")
    r = Runner(env.config, events, amp_telemetry=telemetry)
    r.start(REQUEST)
    events.wait("started")
    t0 = time.monotonic()
    r.stop("power service: state loss", urgent=True, power=True)
    info = events.wait("ended")
    assert time.monotonic() - t0 < 1.5
    assert info["amp_safe"] is None and info["amp_safe_command_run"] is False
    assert "power loss (the stop came from the power service)" in info["amp_safe_detail"]
    assert telemetry.confirms == 0 and env.amp_safe_calls() == 0
    with open(os.path.join(env.state_dir, "sdr-state")) as f:
        assert f.readline() == "unknown\n"      # as it was: a later check decides
    assert env.metadata()["end"]["amp_safe"] is None
    r.shutdown(5)


def test_28_v_rail_absent_at_the_exit_is_treated_as_a_power_loss(env, events):
    telemetry = Telemetry(None, "28 V rail reads 0.3 V", rail_absent=True)
    r = Runner(env.config, events, amp_telemetry=telemetry)
    env.behave(radar="crash")                   # even after an unclean exit
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["amp_safe"] is None and "power loss (the 28 V rail reads absent)" in info["amp_safe_detail"]
    assert telemetry.confirms == 0 and env.amp_safe_calls() == 0 and env.sdr_state() == "unknown"
    r.shutdown(5)


# -------------------------------------------------- the tool outside a radar run

def test_make_safe_runs_the_tool_and_the_tool_records_the_result(env, events, runner):
    assert runner.make_safe("the state is not known") is True
    done = events.wait("safe_done")
    assert done["ok"] and done["state"] == "safe" and done["status"] == 0 and done["why"] == "the state is not known"
    assert env.amp_safe_calls() == 1 and env.sdr_state() == "safe" and not runner.busy
    assert os.path.exists(os.path.join(env.state_dir, "amp_safe.log"))     # its output, outside any collection


def test_make_safe_is_refused_while_a_radar_runs(env, events, runner):
    runner.start(REQUEST)
    events.wait("started")
    assert runner.make_safe("x") is False                   # never while the radar process is alive
    runner.stop("x")
    events.wait("ended")
    assert env.amp_safe_calls() == 0 and "safe_done" not in events.names()


def test_a_start_asked_for_while_the_tool_runs_waits_for_it(env, events, runner):
    env.config["manager"]["amp_safe"]["timeout_s"] = 1.0
    env.behave(amp_safe="hang")
    assert runner.make_safe("x") and runner.making_safe and runner.busy
    assert runner.make_safe("again") is False               # one at a time
    env.behave()
    runner.start(REQUEST)
    events.wait("started")
    assert events.names().index("safe_done") < events.names().index("collection")
    assert runner.shutdown(10.0)


def test_make_safe_results(env, events, runner):
    env.behave(amp_safe=3)
    runner.make_safe("x")
    done = events.wait("safe_done")
    assert not done["ok"] and done["status"] == 3 and done["state"] == "unknown" and env.amp_safe_calls() == 2
    env.behave(amp_safe=6)                                  # BUSY: no delayed retries outside a radar run
    runner.make_safe("x")
    done = events.wait("safe_done", count=2)
    assert not done["ok"] and done["status"] == 6 and env.sdr_state() == "unknown" and env.amp_safe_calls() == 3
    env.behave()
    runner.make_safe("x")
    assert events.wait("safe_done", count=3)["ok"] and env.sdr_state() == "safe"
    env.behave(amp_safe=6)      # BUSY after "safe": the wrapper wrote "unknown" before it started the tool
    runner.make_safe("x")
    done = events.wait("safe_done", count=4)
    assert not done["ok"] and done["state"] == "unknown" and env.sdr_state() == "unknown"


def test_time_of_the_last_activity_follows_radar_and_tool_runs(env, events, runner):
    start = runner.last_activity_uptime
    run_and_stop(env, events, runner, record_s=0.1)
    after_radar = runner.last_activity_uptime
    assert after_radar > start
    time.sleep(0.05)
    runner.make_safe("x")
    events.wait("safe_done")
    assert runner.last_activity_uptime > after_radar


# ---------------------------------------------------------------------- output log

def test_output_log_parts_and_nothing_dropped(tmp_path):
    out = OutputLog(str(tmp_path), "x.log", part_bytes=4096, wall=lambda: 1000.5)
    for i in range(400):
        out.write(f"[ERROR] (Chirp {i}) Receiver error: ERROR_CODE_LATE_COMMAND")
    out.write("[CLOSE FILE] a/b/rx_samps.bin.3")
    out.close()
    names = sorted(os.listdir(tmp_path), key=lambda n: int(n.rsplit(".", 1)[1]) if n[-1].isdigit() else 0)
    assert names[0] == "x.log" and names[1] == "x.log.1" and len(names) >= 5
    text = "".join((tmp_path / n).read_text() for n in names)
    assert text.count("[ERROR] (Chirp ") == 400 and out.lines == 401 and out.bytes == len(text)
    assert text.startswith("[1000.500] \t[ERROR] (Chirp 0)") and text.endswith("[CLOSE FILE] a/b/rx_samps.bin.3\n")
    assert all(os.path.getsize(tmp_path / n) <= 4096 + 100 for n in names)


def test_output_log_survives_a_write_error(tmp_path):
    out = OutputLog(str(tmp_path / "missing"), "x.log", 4096)
    out.write("line")
    out.close()
    assert out.error and out.snapshot()["bytes"] == 0


# ------------------------------------------------------------------- robustness

def test_tool_still_runs_if_the_data_drive_has_gone(env, events, runner):
    import shutil
    env.behave(radar="crash")
    runner.start(REQUEST)
    events.wait("started")
    shutil.rmtree(env.collection_dir())         # as if the drive dropped out mid-run
    info = events.wait("ended")
    assert info["failure"] and info["amp_safe"] is True and env.amp_safe_calls() == 1   # no log file, but it ran
    assert env.sdr_state() == "safe"


def test_an_internal_error_ends_the_run_and_the_runner_lives_on(env, events):
    calls = []

    def hook(run):
        calls.append(1)
        if len(calls) == 1:
            raise ZeroDivisionError("bug in a hook")
        return True, ""
    r = Runner(env.config, events, turn_on_check=hook, amp_telemetry=Telemetry())
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "internal" and "ZeroDivisionError" in info["failure"] and not info["launched"]
    r.start(REQUEST)                            # the same thread serves the next start
    events.wait("started")
    assert r.shutdown(10.0)
    assert events.wait("ended", count=2)["failure"] == ""


def test_stand_in_radar_has_no_sigint_handler_during_its_set_up(env, tmp_path):
    # The property of ORCA that the hold-until-start rule exists for, checked on the stand-in itself.
    import subprocess
    env.behave(init_s=5.0)
    config = os.path.join(env.orca, "config.yaml")
    with open(config, "w") as f:
        json.dump({"DEVICE": {"device_args": ""}}, f)
    proc = subprocess.Popen([sys.executable, "radar.py", "config.yaml"], cwd=os.path.join(env.orca, "sdr", "build"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ, "FAKE_ORCA_CONTROL": env.control})
    time.sleep(1.0)
    proc.send_signal(signal.SIGINT)
    assert proc.wait(5) == -signal.SIGINT       # killed by the signal, not a clean exit
