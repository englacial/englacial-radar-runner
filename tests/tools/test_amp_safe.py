"""Tests of tools/sdr_gpio_safe/amp_safe, the wrapper the services call.

The wrapper only ever runs build/sdr_gpio_safe next to itself, so each test works on a
copy of the script in a temporary directory with a stand-in there: a small script, or
the build of sdr_gpio_safe with the stand-in device. Nothing can touch the SDR.
"""
import os
import shutil
import signal
import stat
import subprocess
import time

import pytest

from tool_helpers import AMP_SAFE, run

OK, NOT_FOUND, ASSUME_ENABLED, TIMEOUT, FAILED, BUSY, AMBIGUOUS, USAGE = 0, 2, 3, 4, 5, 6, 7, 64
NAMES = {OK: "OK", NOT_FOUND: "NOT_FOUND", ASSUME_ENABLED: "ASSUME_ENABLED", TIMEOUT: "TIMEOUT",
         BUSY: "BUSY", AMBIGUOUS: "AMBIGUOUS", USAGE: "USAGE"}
T = ["--timeout", "10"]


@pytest.fixture
def amp_safe(tmp_path):
    """A copy of the wrapper; its tool is tmp_path/build/sdr_gpio_safe."""
    (tmp_path / "build").mkdir()
    return shutil.copy2(AMP_SAFE, tmp_path / "amp_safe")


def stand_in(tmp_path, body):
    """Install a shell script as the tool. It records that it was started, and with what."""
    path = tmp_path / "build" / "new"
    path.write_text(f"#!/bin/sh\necho \"$$ $*\" >> {tmp_path}/started\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    path.rename(tmp_path / "build" / "sdr_gpio_safe")      # a new file, as after a rebuild


def started(tmp_path):
    f = tmp_path / "started"
    return f.read_text().splitlines() if f.exists() else []


def one_line(p):
    lines = p.stdout.splitlines()
    assert len(lines) == 1 and p.stdout.endswith("\n"), p.stdout
    assert lines[0].startswith("amp_safe: ")
    return lines[0]


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# --- passing the result through -------------------------------------------------------

@pytest.mark.parametrize("code", [OK, NOT_FOUND, ASSUME_ENABLED, TIMEOUT, BUSY, AMBIGUOUS, USAGE])
def test_exit_code_and_line_are_passed_through(amp_safe, tmp_path, code):
    stand_in(tmp_path, f"echo '[INFO] [UHD] noise' >&2\necho 'sdr_gpio_safe: {NAMES[code]}: text'\nexit {code}")
    p = run([amp_safe] + T)
    assert p.returncode == code
    line = one_line(p)
    assert line.startswith(f"amp_safe: {NAMES[code]} (exit {code}, ")
    assert line.endswith(f"sdr_gpio_safe: {NAMES[code]}: text")


def test_errors_of_the_tool_are_added_to_the_line(amp_safe, tmp_path):
    stand_in(tmp_path, "echo '[INFO] [B200] detail' >&2; echo '[ERROR] [USB] broke' >&2\n"
                       "echo 'sdr_gpio_safe: ASSUME_ENABLED: text'; exit 3")
    line = one_line(run([amp_safe] + T))
    assert line.endswith("sdr_gpio_safe: ASSUME_ENABLED: text; stderr: [ERROR] [USB] broke")


@pytest.mark.parametrize("body", [
    "exit 0",                                             # no result line
    "echo 'sdr_gpio_safe: NOT_FOUND: text'; exit 0",      # exit code and line disagree
    "echo 'sdr_gpio_safe: OK: text'; exit 2",
    "echo 'Usage: sdr_gpio_safe [options]'; exit 0",      # help text
    "echo 'sdr_gpio_safe: OK: text'; exit 1",             # unknown exit code
    "echo 'sdr_gpio_safe: FAILED: text'; exit 5",         # not a code of the tool
    "exit 2",                                             # known code, no line
    "exit 127",
    "kill -SEGV $$",
], ids=["silent", "wrong-line", "wrong-code", "help", "exit-1", "exit-5", "no-line", "exit-127", "signal"])
def test_tool_that_ran_without_a_valid_result_is_assume_enabled(amp_safe, tmp_path, body):
    stand_in(tmp_path, body)
    p = run([amp_safe] + T)
    assert p.returncode == ASSUME_ENABLED
    line = one_line(p)
    assert line.startswith("amp_safe: ASSUME_ENABLED (exit 3, ") and "assume the amplifier is ENABLED" in line


def test_tool_that_cannot_be_started_is_failed(amp_safe, tmp_path):
    p = run([amp_safe] + T)                               # no tool there
    assert p.returncode == FAILED and "sdr_gpio_safe not started" in one_line(p)
    (tmp_path / "build" / "sdr_gpio_safe").write_text("#!/bin/sh\nexit 0\n")     # not executable
    p = run([amp_safe] + T)
    assert p.returncode == FAILED and "sdr_gpio_safe not started" in one_line(p)


def test_output_is_one_line_whatever_the_tool_prints(amp_safe, tmp_path):
    stand_in(tmp_path, "printf 'a\\nb\\n\\nsdr_gpio_safe: NOT_FOUND: x\\ty\\n\\n'; printf 'e1\\ne2\\n' >&2; exit 2")
    p = run([amp_safe] + T)
    assert p.returncode == NOT_FOUND
    assert one_line(p).endswith("sdr_gpio_safe: NOT_FOUND: x y; stderr: e1 / e2")


# --- arguments ----------------------------------------------------------------------------

def test_tool_gets_a_shorter_time_limit_and_nothing_else(amp_safe, tmp_path):
    stand_in(tmp_path, "echo 'sdr_gpio_safe: OK: text'")
    assert run([amp_safe] + T).returncode == OK
    argv = started(tmp_path)[0].split()[1:]
    assert argv[0] == "--timeout" and len(argv) == 2
    assert 8.5 < float(argv[1]) <= 9.0          # 1 s less than the wrapper's own


def test_args_are_passed_on(amp_safe, tmp_path):
    stand_in(tmp_path, "echo 'sdr_gpio_safe: OK: text'")
    assert run([amp_safe] + T + ["--args", "type=b200"]).returncode == OK
    argv = started(tmp_path)[0].split()[1:]
    assert argv[0] == "--timeout" and argv[2:] == ["--args", "type=b200"]


@pytest.mark.parametrize("argv", [
    [], ["--args", "type=b200"],                                             # --timeout is required
    ["--timeout", "2"], ["--timeout", "301"], ["--timeout", "nan"], ["--timeout", "x"], ["--timeout"],
    T + ["--", "--pin", "3"], T + ["--pin", "3"], T + ["--shutdown-level", "low"], T + ["--settle-ms", "0"],
    T + ["--bank", "FP1"], T + ["--tool", "/bin/true"], T + ["--", "--help"], T + ["--help"], ["-h"],
    T + ["stray"], T + ["--", "--timeout", "100"], T + ["--time", "5"],
], ids=" ".join)
def test_usage_error_starts_nothing(amp_safe, tmp_path, argv):
    stand_in(tmp_path, "echo 'sdr_gpio_safe: OK: text'")
    p = run([amp_safe] + argv)
    assert p.returncode == USAGE and one_line(p).startswith("amp_safe: USAGE (exit 64, ")
    assert started(tmp_path) == []


# --- time limit -----------------------------------------------------------------------------

def test_hanging_tool_is_killed_in_time_and_assumed_enabled(amp_safe, tmp_path):
    stand_in(tmp_path, "trap '' TERM INT HUP\nexec python3 -c 'import signal, time\n"
                       "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)'")
    t = time.monotonic()
    p = run([amp_safe, "--timeout", "3"], timeout=10)
    took = time.monotonic() - t
    assert p.returncode == ASSUME_ENABLED and 2.4 <= took < 3.1       # killed 0.5 s before the limit
    line = one_line(p)
    assert "did not end by itself and was killed" in line and "assume the amplifier is ENABLED" in line
    assert gone(int(started(tmp_path)[0].split()[0]))


def test_tool_that_survives_the_kill_is_left_behind_in_time(amp_safe, tmp_path):
    """A process that keeps the tool's output open after the kill stands in for an unkillable tool."""
    stand_in(tmp_path, f"sleep 60 &\necho $! > {tmp_path}/child\necho 'sdr_gpio_safe: OK: early'\nsleep 60")
    t = time.monotonic()
    try:
        p = run([amp_safe, "--timeout", "3"], timeout=10)
        took = time.monotonic() - t
        assert p.returncode == ASSUME_ENABLED and took < 3.1
        line = one_line(p)
        assert "left behind" in line and "its last line: sdr_gpio_safe: OK: early" in line
    finally:
        os.kill(int((tmp_path / "child").read_text()), signal.SIGKILL)


# --- one at a time ------------------------------------------------------------------------------

def test_two_calls_run_one_after_the_other(amp_safe, tmp_path):
    stand_in(tmp_path, f"date +%s.%N >> {tmp_path}/times; sleep 0.7; date +%s.%N >> {tmp_path}/times\n"
                       "echo 'sdr_gpio_safe: OK: text'")
    calls = [subprocess.Popen([str(amp_safe)] + T, stdout=subprocess.PIPE) for _ in range(2)]
    assert [c.wait(timeout=20) for c in calls] == [OK, OK]
    for c in calls:
        c.stdout.close()
    start1, end1, start2, end2 = [float(x) for x in (tmp_path / "times").read_text().split()]
    assert end1 <= start2                       # the file reads start, end, start, end: no overlap


def test_call_gives_up_when_another_keeps_running_also_after_a_rebuild(amp_safe, tmp_path):
    stand_in(tmp_path, "sleep 5; echo 'sdr_gpio_safe: OK: text'")
    first = subprocess.Popen([str(amp_safe)] + T, stdout=subprocess.DEVNULL)
    try:
        while not started(tmp_path):
            time.sleep(0.02)
        stand_in(tmp_path, "echo 'sdr_gpio_safe: OK: text'")      # the binary is replaced meanwhile
        t = time.monotonic()
        p = run([amp_safe, "--timeout", "3"], timeout=10)
        assert p.returncode == TIMEOUT and time.monotonic() - t < 3.1
        line = one_line(p)
        assert "another sdr_gpio_safe was still running" in line and "sdr_gpio_safe not started" in line
        assert len(started(tmp_path)) == 1      # the second call started nothing
    finally:
        assert first.wait(timeout=20) == OK


# --- with the stand-in build of the tool ----------------------------------------------------------

def test_with_the_tool_itself(amp_safe, tmp_path, fake_tool):
    shutil.copy2(fake_tool, tmp_path / "build" / "sdr_gpio_safe")
    p = run([amp_safe] + T)
    assert p.returncode == OK and "sdr_gpio_safe: OK: FP0 GPIO_0 is a manual output commanded high" in one_line(p)
    assert run([amp_safe] + T, fake="found=0").returncode == NOT_FOUND
    (tmp_path / "usb" / "4-1").mkdir(parents=True)
    (tmp_path / "usb" / "4-1" / "idVendor").write_text("2500\n")
    assert run([amp_safe] + T, fake=f"found=0,sysfs={tmp_path}/usb").returncode == BUSY
    assert run([amp_safe] + T, fake="found=2").returncode == AMBIGUOUS
    assert run([amp_safe] + T, fake="open_throws=1").returncode == ASSUME_ENABLED


def test_time_limit_of_the_tool_comes_first(amp_safe, tmp_path, fake_tool):
    shutil.copy2(fake_tool, tmp_path / "build" / "sdr_gpio_safe")
    t = time.monotonic()
    p = run([amp_safe, "--timeout", "3"], fake="hang=open", timeout=10)
    assert p.returncode == ASSUME_ENABLED and 1.9 <= time.monotonic() - t < 2.5      # the tool's limit is 1 s shorter
    assert "sdr_gpio_safe: ASSUME_ENABLED: time limit of 2.0 s reached while initialising the SDR" in one_line(p)
    p = run([amp_safe, "--timeout", "3"], fake="hang=find", timeout=10)
    assert p.returncode == TIMEOUT
    assert "sdr_gpio_safe: TIMEOUT: time limit of 2.0 s reached while looking for the SDR" in one_line(p)
