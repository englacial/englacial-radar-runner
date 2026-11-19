"""Tests of tools/sdr_gpio_safe/amp_safe, the wrapper the services call.

The tool it runs is always a stand-in here: a small script, or the build of
sdr_gpio_safe with the stand-in device. Nothing can touch the SDR.
"""
import os
import signal
import stat
import subprocess
import time

import pytest

from tool_helpers import AMP_SAFE, run

OK, NOT_FOUND, UNVERIFIED, TIMEOUT, FAILED, USAGE = 0, 2, 3, 4, 5, 64
NAMES = {OK: "OK", NOT_FOUND: "NOT_FOUND", UNVERIFIED: "UNVERIFIED", TIMEOUT: "TIMEOUT", USAGE: "USAGE"}


def stand_in(tmp_path, body, name="tool"):
    """A stand-in for the sdr_gpio_safe binary: a shell script. It records that it was started."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\necho \"$$ $*\" >> {tmp_path}/started\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


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

@pytest.mark.parametrize("code", [OK, NOT_FOUND, UNVERIFIED, TIMEOUT, USAGE])
def test_exit_code_and_line_are_passed_through(tmp_path, code):
    tool = stand_in(tmp_path, f"echo '[INFO] [UHD] noise' >&2\necho 'sdr_gpio_safe: {NAMES[code]}: text'\nexit {code}")
    p = run([AMP_SAFE, "--tool", tool])
    assert p.returncode == code
    line = one_line(p)
    assert line.startswith(f"amp_safe: {NAMES[code]} (exit {code}, ")
    assert line.endswith(f"sdr_gpio_safe: {NAMES[code]}: text")


def test_errors_of_the_tool_are_added_to_the_line(tmp_path):
    tool = stand_in(tmp_path, "echo '[INFO] [B200] detail' >&2; echo '[ERROR] [USB] broke' >&2\n"
                              "echo 'sdr_gpio_safe: UNVERIFIED: text'; exit 3")
    line = one_line(run([AMP_SAFE, "--tool", tool]))
    assert line.endswith("sdr_gpio_safe: UNVERIFIED: text; stderr: [ERROR] [USB] broke")


@pytest.mark.parametrize("body", [
    "exit 0",                                             # no result line
    "echo 'sdr_gpio_safe: UNVERIFIED: text'; exit 0",     # exit code and line disagree
    "echo 'Usage: sdr_gpio_safe [options]'; exit 0",      # help text
    "echo 'sdr_gpio_safe: OK: text'; exit 1",             # unknown exit code
    "exit 3",                                             # known code, no line
    "exit 127",
    "kill -SEGV $$",
], ids=["silent", "wrong-line", "help", "exit-1", "no-line", "exit-127", "signal"])
def test_anything_else_is_a_failure_never_ok(tmp_path, body):
    p = run([AMP_SAFE, "--tool", stand_in(tmp_path, body)])
    assert p.returncode == FAILED
    assert one_line(p).startswith("amp_safe: FAILED (exit 5, ")


def test_tool_that_cannot_be_run(tmp_path):
    p = run([AMP_SAFE, "--tool", tmp_path / "missing"])
    assert p.returncode == FAILED and "cannot run" in one_line(p)
    not_executable = tmp_path / "plain"
    not_executable.write_text("#!/bin/sh\nexit 0\n")
    p = run([AMP_SAFE, "--tool", not_executable])
    assert p.returncode == FAILED and "cannot run" in one_line(p)


def test_output_is_one_line_whatever_the_tool_prints(tmp_path):
    tool = stand_in(tmp_path, "printf 'a\\nb\\n\\nsdr_gpio_safe: NOT_FOUND: x\\ty\\n\\n'; printf 'e1\\ne2\\n' >&2; exit 2")
    p = run([AMP_SAFE, "--tool", tool])
    assert p.returncode == NOT_FOUND
    assert one_line(p).endswith("sdr_gpio_safe: NOT_FOUND: x y; stderr: e1 / e2")


# --- arguments ----------------------------------------------------------------------------

def test_tool_gets_its_options_and_a_shorter_time_limit(tmp_path):
    tool = stand_in(tmp_path, "echo 'sdr_gpio_safe: OK: text'")
    assert run([AMP_SAFE, "--tool", tool, "--timeout", "10", "--", "--pin", "3", "--args", "a=b"]).returncode == OK
    argv = started(tmp_path)[0].split()[1:]
    assert argv[:4] == ["--pin", "3", "--args", "a=b"] and argv[4] == "--timeout" and len(argv) == 6
    assert 8.5 < float(argv[5]) <= 9.0          # 1 s less than the wrapper's own


@pytest.mark.parametrize("argv", [["--timeout", "2"], ["--timeout", "301"], ["--timeout", "nan"], ["--timeout", "x"],
                                  ["--bogus"], ["--", "--help"], ["--", "-h"], ["--", "--timeout", "100"], ["--tool"]],
                         ids=" ".join)
def test_usage_error_starts_nothing(tmp_path, argv):
    tool = stand_in(tmp_path, "echo 'sdr_gpio_safe: OK: text'")
    p = run([AMP_SAFE, "--tool", tool] + argv)
    assert p.returncode == USAGE and one_line(p).startswith("amp_safe: USAGE (exit 64, ")
    assert started(tmp_path) == []


# --- time limit -----------------------------------------------------------------------------

def test_hanging_tool_is_killed_in_time(tmp_path):
    tool = stand_in(tmp_path, "trap '' TERM INT HUP\nexec python3 -c 'import signal, time\n"
                              "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)'")
    t = time.monotonic()
    p = run([AMP_SAFE, "--tool", tool, "--timeout", "3"], timeout=10)
    took = time.monotonic() - t
    assert p.returncode == TIMEOUT and 2.4 <= took < 3.1       # killed 0.5 s before the limit
    assert "did not end by itself and killed" in one_line(p)
    assert gone(int(started(tmp_path)[0].split()[0]))


def test_tool_that_survives_the_kill_is_left_behind_in_time(tmp_path):
    """A process that keeps the tool's output open after the kill stands in for an unkillable tool."""
    tool = stand_in(tmp_path, f"sleep 60 &\necho $! > {tmp_path}/child\necho 'sdr_gpio_safe: OK: early'\nsleep 60")
    t = time.monotonic()
    try:
        p = run([AMP_SAFE, "--tool", tool, "--timeout", "3"], timeout=10)
        took = time.monotonic() - t
        assert p.returncode == TIMEOUT and took < 3.1
        line = one_line(p)
        assert "left behind" in line and "pin state unknown" in line and "its last line: sdr_gpio_safe: OK: early" in line
    finally:
        os.kill(int((tmp_path / "child").read_text()), signal.SIGKILL)


def test_tool_own_time_limit_comes_first(fake_tool):
    t = time.monotonic()
    p = run([AMP_SAFE, "--tool", fake_tool, "--timeout", "3"], fake="hang=open", timeout=10)
    took = time.monotonic() - t
    assert p.returncode == TIMEOUT and 1.9 <= took < 2.5       # the tool's limit is 1 s shorter
    assert one_line(p).endswith("sdr_gpio_safe: TIMEOUT: no result within 2.0 s, stuck while initialising the SDR; "
                                "pin state unknown")


# --- one at a time ------------------------------------------------------------------------------

def test_two_calls_run_one_after_the_other(tmp_path):
    tool = stand_in(tmp_path, f"date +%s.%N >> {tmp_path}/times; sleep 0.7; date +%s.%N >> {tmp_path}/times\n"
                              "echo 'sdr_gpio_safe: OK: text'")
    calls = [subprocess.Popen([str(AMP_SAFE), "--tool", str(tool)], stdout=subprocess.PIPE) for _ in range(2)]
    assert [c.wait(timeout=20) for c in calls] == [OK, OK]
    for c in calls:
        c.stdout.close()
    start1, end1, start2, end2 = [float(x) for x in (tmp_path / "times").read_text().split()]
    assert end1 <= start2                       # the file reads start, end, start, end: no overlap


def test_call_gives_up_when_another_keeps_running(tmp_path):
    tool = stand_in(tmp_path, "sleep 5; echo 'sdr_gpio_safe: OK: text'")
    first = subprocess.Popen([str(AMP_SAFE), "--tool", str(tool)], stdout=subprocess.DEVNULL)
    try:
        while not started(tmp_path):
            time.sleep(0.02)
        t = time.monotonic()
        p = run([AMP_SAFE, "--tool", tool, "--timeout", "3"], timeout=10)
        assert p.returncode == TIMEOUT and time.monotonic() - t < 3.1
        assert "another sdr_gpio_safe was still running" in one_line(p)
        assert len(started(tmp_path)) == 1      # the second call started nothing
    finally:
        assert first.wait(timeout=20) == OK


# --- with the stand-in build of the tool ----------------------------------------------------------

def test_with_the_tool_itself(fake_tool):
    p = run([AMP_SAFE, "--tool", fake_tool, "--", "--settle-ms", "50"])
    assert p.returncode == OK and "sdr_gpio_safe: OK: FP0 pin 0 reads high" in one_line(p)
    p = run([AMP_SAFE, "--tool", fake_tool, "--", "--settle-ms", "50"], fake="found=0")
    assert p.returncode == NOT_FOUND
    p = run([AMP_SAFE, "--tool", fake_tool, "--", "--settle-ms", "50"], fake="open_throws=1")
    assert p.returncode == UNVERIFIED
    p = run([AMP_SAFE, "--tool", fake_tool, "--", "--pin", "40"])
    assert p.returncode == USAGE
