"""The ORCA runner (radar_runner/manager/runner.py) against the stand-in ORCA tree.

Real processes and real time, with a stand-in `radar` that copies the path
rules and the stdout lines of ORCA's sdr/main.cpp. No SDR is opened.
"""
import glob
import json
import os
import signal
import time

import pytest
import yaml

from radar_runner.manager import ampsafe
from radar_runner.manager import limits
from radar_runner.manager.runner import OutputLog, Runner

REQUEST = {"trigger": "button", "attempt": 1, "resume": False}


@pytest.fixture
def runner(env, events):
    r = Runner(env.config, events)
    yield r
    r.shutdown(10.0)


def run_and_stop(env, events, runner, record_s=0.5, **behaviour):
    env.behave(**behaviour)
    runner.start(REQUEST)
    events.wait("started")
    time.sleep(record_s)
    runner.stop("test stop")
    return events.wait("ended")


# ------------------------------------------------------------------ a normal run

def test_a_run_writes_everything_into_its_own_directory(env, events, runner):
    info = run_and_stop(env, events, runner)
    assert events.names() == ["collection", "started", "ended"]
    assert info["requested"] and info["failure"] == "" and info["exit_status"] == 0
    assert info["started"] and info["launched"] and not info["killed"]
    assert info["amp_safe"] is True and env.amp_safe_calls() == 1

    name = events.wait("collection")["name"]
    d = os.path.join(env.root, name)
    files = sorted(os.listdir(d))
    # Data in chunks, written by the radar itself, in the collection directory.
    chunks = sorted((f for f in files if f.startswith("rx_samps.bin.")), key=lambda f: int(f.rsplit(".", 1)[1]))
    assert len(chunks) >= 3 and chunks[0] == "rx_samps.bin.0"
    for expected in ("chirp.bin", "collection.json", "config.yaml", "config_requested.yaml", "gps_log.txt",
                     "prepare.log", "uhd_stdout.log", "amp_safe.log"):
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
    r = Runner(env.config, events)
    r.start(REQUEST)
    events.wait("started")
    r.shutdown(10)
    assert env.collections()[-1].startswith("b00042_s001_")


def test_the_boot_number_comes_from_one_provider(env, events):
    r = Runner(env.config, events, boot_number=lambda state_dir, floor=0: (77, ""))
    r.start(REQUEST)
    events.wait("started")
    r.shutdown(10)
    assert env.collections()[-1].startswith("b00077_s001_")


def test_derived_config_is_what_the_radar_read_and_passes_the_limits(env, events, runner):
    run_and_stop(env, events, runner, record_s=0.1)
    d = env.collection_dir()
    with open(os.path.join(d, "config.yaml")) as f:
        derived = limits.load(f.read())
    assert limits.check(derived, env.config["manager"]["limits"]) == []
    assert limits.check_output(derived, env.orca, env.root) == []
    # Relative to the ORCA checkout: the only form that the unchanged radar and chirp generator both accept.
    out = derived["FILES"]["output_dir"]
    assert not os.path.isabs(out)
    assert os.path.realpath(os.path.join(env.orca, out)) == os.path.realpath(d)
    assert derived["FILES"]["max_chirps_per_file"] == 2000          # 0.4 s / (200 us x 1000 presums) records
    assert derived["RF0"]["tx_gain"] == 72.0 and derived["GPIO"]["pwr_amp_pin"] == 2
    # The requested file is kept byte for byte.
    with open(os.path.join(d, "config_requested.yaml"), "rb") as a, open(env.radar_config, "rb") as b:
        assert a.read() == b.read()
    # The radar's own first line shows the path it was given.
    with open(os.path.join(d, "uhd_stdout.log")) as f:
        first = f.readline()
    assert "Reading from config file: ../../" in first and first.rstrip().endswith("config.yaml")


def test_output_log_has_run_py_format_and_the_protocol_lines(env, events, runner):
    run_and_stop(env, events, runner)
    with open(os.path.join(env.collection_dir(), "uhd_stdout.log")) as f:
        lines = f.read().splitlines()
    for line in lines:
        stamp, sep, _ = line.partition("] \t")
        assert sep and stamp[0] == "[" and abs(float(stamp[1:]) - time.time()) < 60
    text = "\n".join(lines)
    for marker in ("[VERSION] 0.0.2", "[OPEN FILE] ", "[START] Beginning main loop", "[CLOSE FILE] ",
                   "[RX] Reached stop signal handling", "[TX] Done.", "[RX] Total pulses written"):
        assert marker in text


def test_metadata_is_valid_from_the_start_and_completed_at_the_end(env, events, runner):
    runner.start(REQUEST, flags=["no_amp_telemetry"])
    events.wait("collection")
    events.wait("started")
    time.sleep(0.2)
    meta = env.metadata()                       # as a reader finds it after a hard power cut
    assert meta["end"] is None and meta["recording_started"]["uptime_s"] > 0
    assert meta["trigger"] == "button" and meta["flags"] == ["no_amp_telemetry"]
    assert meta["boot"] == 1 and meta["sequence"] == 1 and meta["name"] == env.collections()[0]
    runner.add_flag("something_later")
    runner.stop("done")
    events.wait("ended")
    meta = env.metadata()
    end = meta["end"]
    assert end["how"] == "stopped" and end["reason"] == "done" and end["exit_status"] == 0
    assert end["killed"] is False and end["amp_safe"] is True and end["amp_safe_command_run"] is True
    assert end["data_files_closed"] >= 1 and end["log"]["bytes"] > 0
    assert meta["flags"] == ["no_amp_telemetry", "something_later"]
    assert end["uptime_s"] >= meta["recording_started"]["uptime_s"] >= meta["created"]["uptime_s"]


def test_a_radar_with_a_pulse_count_finishes_by_itself(env, events, runner):
    with open(env.radar_config) as f:
        text = f.read().replace("num_pulses: -1", "num_pulses: 3000")
    with open(env.radar_config, "w") as f:
        f.write(text)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert not info["requested"] and info["failure"] == "" and info["exit_status"] == 0 and info["started"]
    assert env.metadata()["end"]["how"] == "finished"
    assert env.amp_safe_calls() == 1


# ------------------------------------------------------------- failures of the start

def test_config_that_fails_the_limits_is_fatal_and_starts_nothing(env, events, runner):
    env.config["manager"]["limits"]["max_tx_gain"] = 43.0
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["fatal"] and info["kind"] == "config" and "RF0.tx_gain 72 is above the cap of 43" in info["failure"]
    assert not info["launched"] and info["amp_safe"] is None
    assert env.collections() == [] and env.amp_safe_calls() == 0


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


def test_radar_that_fails_to_start(env, events, runner):
    env.behave(radar="fail_start")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "exit" and "status 1 before it started recording" in info["failure"]
    assert info["launched"] and not info["started"] and "started" not in events.names()
    assert any("No devices found" in line for line in info["output_tail"])
    assert info["amp_safe"] is True and env.amp_safe_calls() == 1       # a radar process existed


def test_radar_that_never_prints_its_start_line(env, events, runner):
    env.config["manager"]["orca"]["start_timeout_s"] = 0.5
    env.behave(radar="no_start")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "start_timeout" and "did not report [START] within 0.5 s" in info["failure"]
    assert not info["started"] and not info["killed"] and env.amp_safe_calls() == 1


def test_radar_on_a_usb_2_link_is_a_failed_start(env, events, runner):
    env.behave(radar="usb2")
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "usb2" and "USB 2" in info["failure"] and env.amp_safe_calls() == 1


def test_missing_radar_program(env, events, runner):
    env.config["manager"]["orca"]["radar_command"] = ["./no-such-radar", "{config_rel}"]
    env.config["manager"]["orca"]["launch_prefix"] = []
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "launch" and not info["launched"] and env.amp_safe_calls() == 0
    assert not os.path.exists(os.path.join(env.state_dir, "sdr-state"))


def test_turn_on_check_hook_can_refuse(env, events):
    r = Runner(env.config, events, turn_on_check=lambda run: (False, "no direct signal"))
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["fatal"] and info["kind"] == "turn_on_check" and "no direct signal" in info["failure"]
    assert not info["launched"] and env.amp_safe_calls() == 0


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
    assert env.amp_safe_calls() == 1
    # The data written before the kill is whole records.
    d = env.collection_dir()
    chunks = sorted((f for f in os.listdir(d) if f.startswith("rx_samps.bin.")),
                    key=lambda f: int(f.rsplit(".", 1)[1]))
    assert chunks and all(os.path.getsize(os.path.join(d, c)) % meta["data"]["record_bytes"] == 0 for c in chunks[:-1])
    assert time.monotonic() - t0 < 8


def test_radar_that_exits_slowly_but_in_time_is_not_killed(env, events, runner):
    env.config["manager"]["orca"]["stop_timeout_s"] = 4.0
    info = run_and_stop(env, events, runner, record_s=0.2, radar="slow_exit")
    assert not info["killed"] and info["exit_status"] == 0


def test_flood_of_output_is_capped_and_never_blocks_the_radar(env, events, runner):
    env.config["manager"]["data"].update(log_part_mb=0.05, log_max_mb=0.2)
    info = run_and_stop(env, events, runner, record_s=1.5, radar="flood")
    assert info["exit_status"] == 0 and not info["killed"]      # it could still stop cleanly: never blocked
    d = env.collection_dir()
    parts = sorted(f for f in os.listdir(d) if f.startswith("uhd_stdout.log"))
    assert len(parts) >= 4 and parts[0] == "uhd_stdout.log" and "uhd_stdout.log.1" in parts
    total = sum(os.path.getsize(os.path.join(d, p)) for p in parts)
    assert 200_000 <= total < 300_000
    meta = env.metadata()
    assert meta["end"]["log"]["lines_dropped"] > 1000 and "log_truncated" in meta["flags"]
    # The lines that matter are kept after the cap: the file reports and the exit.
    with open(os.path.join(d, parts[-1])) as f:
        tail = f.read()
    assert "[CLOSE FILE]" in tail and "[RX] Total pulses written" in tail and "lines were not logged" in tail
    # ... and the radar kept writing data all the while.
    assert meta["end"]["data_files_closed"] >= 5


def test_stderr_goes_to_its_own_file(env, events, runner):
    run_and_stop(env, events, runner, record_s=0.1, radar="stderr")
    with open(os.path.join(env.collection_dir(), "uhd_stderr.log")) as f:
        text = f.read()
    assert "something UHD says on stderr" in text and "LLLL" in text
    with open(os.path.join(env.collection_dir(), "uhd_stdout.log")) as f:
        assert "LLLL" not in f.read()


# ---------------------------------------------------------------------------- stop

def test_stop_during_the_start_sequence_aborts_it_without_a_radar(env, events, runner):
    env.behave(chirp="slow")
    runner.start(REQUEST)
    events.wait("collection")
    time.sleep(0.3)
    t0 = time.monotonic()
    runner.stop("changed my mind")
    info = events.wait("ended")
    assert time.monotonic() - t0 < 2.0
    assert info["requested"] and info["failure"] == "" and not info["launched"] and info["amp_safe"] is None
    assert env.metadata()["end"]["how"] == "not_started" and env.amp_safe_calls() == 0


def test_stop_before_the_start_line_stops_the_radar(env, events, runner):
    env.behave(radar="no_start")
    runner.start(REQUEST)
    events.wait("collection")
    time.sleep(1.0)                             # the radar is up, but has not started recording
    runner.stop("early stop")
    info = events.wait("ended")
    assert info["requested"] and info["launched"] and not info["started"] and info["failure"] == ""
    assert env.amp_safe_calls() == 1


def test_only_one_run_at_a_time(env, events, runner):
    runner.start(REQUEST)
    with pytest.raises(RuntimeError):
        runner.start(REQUEST)
    assert runner.busy
    assert runner.shutdown(10.0) and not runner.busy


def test_shutdown_stops_the_radar_and_makes_the_amplifier_safe(env, events, runner):
    runner.start(REQUEST)
    events.wait("started")
    pid = runner.snapshot()["pid"]
    assert runner.shutdown(10.0)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert env.amp_safe_calls() == 1 and env.metadata()["end"]["reason"] == "the manager is stopping"


def test_snapshot_while_running(env, events, runner):
    assert runner.snapshot() == {"active": False}
    runner.start(REQUEST)
    events.wait("started")
    time.sleep(0.3)
    snap = runner.snapshot()
    assert snap["active"] and snap["recording"] and snap["pid"] > 0 and snap["collection"] == env.collections()[0]
    assert snap["writing"].startswith("rx_samps.bin.") and snap["log"]["bytes"] > 0


# ------------------------------------------------------------- amplifier made safe

@pytest.mark.parametrize("status, name", [(2, "NOT_FOUND"), (5, "FAILED"), (6, "BUSY"), (7, "AMBIGUOUS"), (64, "USAGE")])
def test_tool_failure_that_changed_nothing_after_a_clean_exit_leaves_the_pin_known_safe(env, events, runner,
                                                                                        status, name):
    # Policy "always": the tool runs although a clean radar exit left the pin safe. These
    # exit codes mean "state unknown and unchanged by the call", so what was known still holds.
    info = run_and_stop(env, events, runner, record_s=0.1, amp_safe=status)
    assert info["amp_safe"] is True and info["amp_safe_command_run"] is True and env.amp_safe_calls() == 1
    assert f"status {status} ({name})" in info["amp_safe_detail"]
    assert "known safe before and is unchanged" in info["amp_safe_detail"]
    assert env.sdr_state() == "safe"


@pytest.mark.parametrize("status", [2, 5, 6])
def test_tool_failure_after_an_unclean_exit_is_not_safe(env, events, runner, status):
    env.behave(radar="crash", amp_safe=status)
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["amp_safe"] is False and "not known safe before" in info["amp_safe_detail"]
    assert env.metadata()["end"]["amp_safe"] is False and env.sdr_state() == "unknown"


def test_assume_enabled_is_retried_at_once_and_then_reported(env, events, runner, caplog):
    # Exit 3: the tool itself initialised the SDR and could not verify the pin.
    info = run_and_stop(env, events, runner, record_s=0.1, amp_safe=3)
    assert env.amp_safe_calls() == 2                        # assume_enabled_retries = 1 in these tests
    assert info["amp_safe"] is False and "status 3 (ASSUME_ENABLED) in each of 2 attempts" in info["amp_safe_detail"]
    assert env.sdr_state() == "unknown" and "URGENT" in caplog.text
    assert env.metadata()["end"]["amp_safe"] is False


def test_hanging_amp_safe_command_times_out(env, events, runner):
    env.config["manager"]["amp_safe"]["timeout_s"] = 0.5
    info = run_and_stop(env, events, runner, record_s=0.1, amp_safe="hang")
    assert info["amp_safe"] is False and "no result within 0.5 s" in info["amp_safe_detail"]
    assert env.sdr_state() == "unknown"                     # killed half way: it may have initialised the SDR


def test_sdr_state_is_unknown_while_the_radar_runs_and_safe_after(env, events, runner):
    path = os.path.join(env.state_dir, "sdr-state")
    assert not os.path.exists(path) and env.sdr_state() == "unknown"       # no record = unknown
    runner.start(REQUEST)
    events.wait("started")
    with open(path) as f:
        lines = f.read().splitlines()
    assert lines[0] == "unknown" and "radar launched, collection b00001_s001_" in lines[1]
    runner.stop("x")
    events.wait("ended")
    assert env.sdr_state() == "safe"
    with open(path) as f:
        assert f.readline() == "safe\n"                     # what the unit's stop hook tests


def confirm(state, text="telemetry"):
    class Fake:
        calls = 0

        def confirm_shutdown(self, exit_uptime):
            Fake.calls += 1
            return state, text
    return Fake()


def test_default_policy_is_when_needed():
    from radar_runner.manager.config import MANAGER_DEFAULTS
    assert MANAGER_DEFAULTS["amp_safe"]["policy"] == "when_needed"
    assert MANAGER_DEFAULTS["amp_safe"]["command"][1:] == ["--timeout", "14"]
    assert MANAGER_DEFAULTS["amp_safe"]["timeout_s"] > 14


@pytest.mark.parametrize("state, text", [(True, "amplifier current 0.16 A at 28.0 V"),
                                         (None, "no fresh amplifier reading after the exit")])
def test_when_needed_does_not_run_the_tool_after_a_clean_exit(env, events, state, text):
    # Known safe: the radar, with the amplifier pin configured, exited cleanly; telemetry agrees or is absent.
    env.config["manager"]["amp_safe"]["policy"] = "when_needed"
    r = Runner(env.config, events, amp_telemetry=confirm(state, text))
    info = run_and_stop(env, events, r, record_s=0.1)
    assert info["amp_safe"] is True and info["amp_safe_command_run"] is False and env.amp_safe_calls() == 0
    assert info["amp_safe_detail"].startswith("command not run: ") and text in info["amp_safe_detail"]
    assert env.sdr_state() == "safe" and env.metadata()["end"]["amp_safe_command_run"] is False


def test_when_needed_runs_the_tool_if_telemetry_shows_enabled_current_after_a_clean_exit(env, events):
    env.config["manager"]["amp_safe"]["policy"] = "when_needed"
    r = Runner(env.config, events, amp_telemetry=confirm(False, "amplifier current 2.50 A at 27.5 V"))
    info = run_and_stop(env, events, r, record_s=0.1)
    assert info["amp_safe_command_run"] is True and env.amp_safe_calls() == 1 and info["amp_safe"] is True
    assert "telemetry shows the amplifier enabled" in info["amp_safe_detail"] and env.sdr_state() == "safe"


@pytest.mark.parametrize("behaviour", ["crash", "fail_start", "ignore_sigint"])
@pytest.mark.parametrize("state, runs", [(None, 1), (False, 1), (True, 0)])
def test_when_needed_after_an_unclean_exit(env, events, behaviour, state, runs):
    # Killed, crashed, or never running: the tool runs, unless a fresh reading
    # with the 28 V present shows shutdown current: then the current decides.
    env.config["manager"]["amp_safe"]["policy"] = "when_needed"
    env.config["manager"]["orca"]["stop_timeout_s"] = 0.4
    r = Runner(env.config, events, amp_telemetry=confirm(state))
    env.behave(radar=behaviour)
    r.start(REQUEST)
    if behaviour == "ignore_sigint":
        events.wait("started")
        r.stop("x")
    info = events.wait("ended")
    assert env.amp_safe_calls() == runs and info["amp_safe_command_run"] is bool(runs)
    assert info["amp_safe"] is True and env.sdr_state() == "safe"


# -------------------------------------------------- the tool outside a radar run

def test_make_safe_runs_the_tool_and_records_the_result(env, events, runner):
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
    assert env.amp_safe_calls() == 1 and "safe_done" not in events.names()


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


def test_make_safe_assume_enabled_and_unchanged_failures(env, events, runner):
    env.behave(amp_safe=3)
    runner.make_safe("x")
    done = events.wait("safe_done")
    assert not done["ok"] and done["status"] == 3 and done["state"] == "unknown" and env.amp_safe_calls() == 2
    env.behave(amp_safe=6)                                  # BUSY: nothing changed, still unknown
    runner.make_safe("x")
    done = events.wait("safe_done", count=2)
    assert not done["ok"] and done["status"] == 6 and env.sdr_state() == "unknown" and env.amp_safe_calls() == 3
    env.behave()
    runner.make_safe("x")
    assert events.wait("safe_done", count=3)["ok"] and env.sdr_state() == "safe"
    env.behave(amp_safe=6)                                  # BUSY again: what was known still holds
    runner.make_safe("x")
    done = events.wait("safe_done", count=4)
    assert done["ok"] and done["state"] == "safe" and env.sdr_state() == "safe"


# ---------------------------------------------------------------------- output log

def test_output_log_parts_and_cap(tmp_path):
    out = OutputLog(str(tmp_path), "x.log", part_bytes=4096, max_bytes=10000, wall=lambda: 1000.5)
    for i in range(400):
        out.write(f"[ERROR] (Chirp {i}) Receiver error: ERROR_CODE_LATE_COMMAND")
    out.write("[CLOSE FILE] a/b/rx_samps.bin.3")
    out.close()
    names = sorted(os.listdir(tmp_path))
    assert names == ["x.log", "x.log.1", "x.log.2"]
    assert out.dropped > 100 and out.bytes < 10000 + 400
    last = (tmp_path / "x.log.2").read_text()
    assert "[1000.500] \t[CLOSE FILE] a/b/rx_samps.bin.3\n" in last
    assert f"{out.dropped} lines were not logged" in last
    assert (tmp_path / "x.log").read_text().startswith("[1000.500] \t[ERROR] (Chirp 0)")


def test_output_log_survives_a_write_error(tmp_path):
    out = OutputLog(str(tmp_path / "missing"), "x.log", 4096, 10000)
    out.write("line")
    out.close()
    assert out.error and out.snapshot()["bytes"] == 0


def test_derived_config_is_plain_yaml_without_anchors(env, events, runner):
    run_and_stop(env, events, runner, record_s=0.05)
    with open(os.path.join(env.collection_dir(), "config.yaml")) as f:
        text = f.read()
    assert "&" not in text and "*" not in text
    assert yaml.safe_load(text)["CHIRP"]["tx_duration"] == pytest.approx(1.3e-5)
    json.dumps(yaml.safe_load(text))


# ------------------------------------------------------------------- robustness

def test_amplifier_is_made_safe_even_if_the_data_drive_has_gone(env, events, runner):
    import shutil
    runner.start(REQUEST)
    events.wait("started")
    time.sleep(0.2)
    shutil.rmtree(env.collection_dir())         # as if the drive dropped out mid-run
    runner.stop("data drive gone")
    info = events.wait("ended")
    assert info["amp_safe"] is True and env.amp_safe_calls() == 1       # no log file, but it ran
    assert env.sdr_state() == "safe"


def test_an_internal_error_ends_the_run_and_the_runner_lives_on(env, events):
    calls = []

    def hook(run):
        calls.append(1)
        if len(calls) == 1:
            raise ZeroDivisionError("bug in a hook")
        return True, ""
    r = Runner(env.config, events, turn_on_check=hook)
    r.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "internal" and "ZeroDivisionError" in info["failure"] and not info["launched"]
    r.start(REQUEST)                            # the same thread serves the next start
    events.wait("started")
    assert r.shutdown(10.0)
    assert events.wait("ended", count=2)["failure"] == ""


def test_radar_program_missing_after_the_build_is_a_failed_start(env, events, runner):
    os.unlink(os.path.join(env.orca, "sdr", "build", "radar.py"))
    env.config["manager"]["orca"]["radar_command"] = ["radar.py", "{config_rel}"]
    runner.start(REQUEST)
    info = events.wait("ended")
    assert info["kind"] == "launch" and "is not there after the build" in info["failure"]
    assert not info["launched"] and env.amp_safe_calls() == 0
