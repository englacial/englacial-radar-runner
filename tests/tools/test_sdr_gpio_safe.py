"""Tests of tools/sdr_gpio_safe: command line, exit codes, the write/verify logic and the time limit.

See conftest.py for why none of this can touch the SDR.
"""
import time

import pytest

from tool_helpers import run

OK, NOT_FOUND, UNVERIFIED, TIMEOUT, USAGE = 0, 2, 3, 4, 64
FAST = ["--settle-ms", "50"]

# Each of these must be rejected before the SDR is looked for.
BAD_COMMAND_LINES = [
    ["--bogus"], ["stray"], ["--high"], ["--low"], ["--pin"], ["--pin", "0", "stray"],
    ["--pin", "-1"], ["--pin", "32"], ["--pin", "1.5"], ["--pin", "0x1"], ["--pin", ""], ["--pin", "one"],
    ["--shutdown-level"], ["--shutdown-level", "HIGH"], ["--shutdown-level", "1"], ["--shutdown-level", "enable"],
    ["--settle-ms", "-5"], ["--settle-ms", "10001"], ["--settle-ms", "1.5"],
    ["--timeout", "0"], ["--timeout", "0.5"], ["--timeout", "301"], ["--timeout", "nan"], ["--timeout", "inf"],
    ["--timeout", "1e2"], ["--timeout", "5s"], ["--bank", ""], ["--args"], ["--pin=0"],
]


def one_line(p):
    lines = p.stdout.splitlines()
    assert len(lines) == 1 and p.stdout.endswith("\n"), p.stdout
    assert lines[0].startswith("sdr_gpio_safe: ")
    return lines[0]


def writes(log):
    return [tuple(line.split()) for line in log.read_text().splitlines() if not line.startswith("READBACK")]


# --- command line ---------------------------------------------------------------

def test_help_does_not_look_for_the_sdr(fake_tool):
    for argv in (["--help"], ["-h"], ["--pin", "3", "--help"]):
        p = run([fake_tool] + argv, fake="hang=find", timeout=5)   # would hang if it looked
        assert p.returncode == 0
        assert "Exit codes:" in p.stdout and "--shutdown-level" in p.stdout


@pytest.mark.parametrize("argv", BAD_COMMAND_LINES, ids=" ".join)
def test_usage_error(fake_tool, argv):
    p = run([fake_tool] + argv, fake="hang=find", timeout=5)       # would hang if it looked
    assert p.returncode == USAGE
    assert one_line(p).startswith("sdr_gpio_safe: USAGE: ")


def test_no_option_drives_the_enabled_level(fake_tool, tmp_path):
    """Whatever the options, OUT is only ever written with the shutdown level."""
    for i, (argv, value) in enumerate([([], "1"), (["--pin", "3"], "8"), (["--shutdown-level", "high"], "1"),
                                       (["--shutdown-level", "low"], "0"),
                                       (["--pin", "5", "--shutdown-level", "low"], "0")]):
        log = tmp_path / f"log{i}"
        run([fake_tool] + FAST + argv, fake=f"log={log},lost_writes=2")
        out_writes = [w for w in writes(log) if w[0] == "OUT"]
        assert out_writes and all(w[1] == value for w in out_writes)


# --- writing and verifying --------------------------------------------------------

def test_success_writes_manual_output_shutdown_level_in_order(fake_tool, tmp_path):
    log = tmp_path / "log"
    p = run([fake_tool] + FAST, fake=f"log={log}")
    assert p.returncode == OK
    line = one_line(p)
    assert line.startswith("sdr_gpio_safe: OK: FP0 pin 0 reads high") and "1 write," in line
    # CTRL 0 = manual, DDR 1 = output, OUT 1 = high; OUT after CTRL; then two readbacks, no write between
    assert log.read_text().splitlines() == ["CTRL 0 1", "DDR 1 1", "OUT 1 1", "READBACK 1", "READBACK 1"]


def test_pin_and_bank_options(fake_tool, tmp_path):
    log = tmp_path / "log"
    p = run([fake_tool] + FAST + ["--pin", "3", "--bank", "FP1", "--args", "serial=X"], fake=f"log={log}")
    assert p.returncode == OK and "FP1 pin 3 reads high" in one_line(p)
    assert writes(log) == [("CTRL", "0", "8"), ("DDR", "8", "8"), ("OUT", "8", "8")]


def test_settle_time_is_waited(fake_tool):
    t = time.monotonic()
    assert run([fake_tool, "--settle-ms", "700"]).returncode == OK
    assert time.monotonic() - t >= 0.7


def test_writes_that_do_not_stick_are_repeated(fake_tool):
    p = run([fake_tool] + FAST, fake="lost_writes=3")
    assert p.returncode == OK and "4 writes," in one_line(p)


def test_failing_writes_are_repeated(fake_tool):
    p = run([fake_tool] + FAST, fake="write_throws=4")
    assert p.returncode == OK


def test_pin_that_goes_back_is_written_again(fake_tool):
    p = run([fake_tool] + FAST, fake="reverts=1")
    assert p.returncode == OK and "2 writes," in one_line(p)


def test_pin_that_keeps_going_back_is_unverified(fake_tool):
    p = run([fake_tool] + FAST, fake="reverts=9")
    assert p.returncode == UNVERIFIED
    assert "went back to low" in one_line(p) and "3 rounds" in one_line(p)


def test_pin_that_never_follows_is_unverified(fake_tool):
    t = time.monotonic()
    p = run([fake_tool] + FAST, fake="stuck=1")
    assert p.returncode == UNVERIFIED
    assert "never read high (30 writes, 1 round)" in one_line(p)
    assert time.monotonic() - t < 6      # bounded: 30 attempts


# --- finding and opening ------------------------------------------------------------

def test_not_found(fake_tool, tmp_path):
    log = tmp_path / "log"
    p = run([fake_tool] + FAST, fake=f"found=0,log={log}")
    assert p.returncode == NOT_FOUND and one_line(p).startswith("sdr_gpio_safe: NOT_FOUND: ")
    assert not log.exists()              # never opened, nothing written


def test_search_failure_is_not_found_and_one_line(fake_tool):
    p = run([fake_tool] + FAST, fake="find_throws=1")     # the stand-in's error text has a newline
    assert p.returncode == NOT_FOUND and "fake: cannot enumerate" in one_line(p)


def test_two_sdrs_are_not_opened(fake_tool, tmp_path):
    log = tmp_path / "log"
    p = run([fake_tool] + FAST, fake=f"found=2,log={log}")
    assert p.returncode == UNVERIFIED and "2 SDRs match" in one_line(p)
    assert not log.exists()


def test_failed_initialisation_is_unverified(fake_tool):
    p = run([fake_tool] + FAST, fake="open_throws=1")
    assert p.returncode == UNVERIFIED and "not initialised: fake: init failed" in one_line(p)


# --- time limit -----------------------------------------------------------------------

@pytest.mark.parametrize("step, stage", [("find", "looking for the SDR"), ("open", "initialising the SDR")])
def test_call_that_never_returns_ends_at_the_time_limit(fake_tool, step, stage):
    t = time.monotonic()
    p = run([fake_tool, "--timeout", "1"], fake=f"hang={step}", timeout=10)
    took = time.monotonic() - t
    assert p.returncode == TIMEOUT and 1.0 <= took < 2.0
    assert one_line(p) == f"sdr_gpio_safe: TIMEOUT: no result within 1.0 s, stuck while {stage}; pin state unknown"


def test_time_limit_also_covers_the_retries(fake_tool):
    p = run([fake_tool, "--timeout", "1"], fake="stuck=1", timeout=10)
    assert p.returncode == TIMEOUT and "stuck while writing the pin" in one_line(p)


# --- the real binary --------------------------------------------------------------------

def test_real_binary_help(fake_tool, real_tool):
    expected = run([fake_tool, "--help"], fake="hang=find", timeout=5)
    assert expected.returncode == 0                      # the same source answers --help without a device
    p = run([real_tool, "--help"], timeout=10)
    assert p.returncode == 0 and p.stdout == expected.stdout


@pytest.mark.parametrize("argv", [["--bogus"], ["--pin", "32"], ["--high"]], ids=" ".join)
def test_real_binary_usage_error(fake_tool, real_tool, argv):
    check = run([fake_tool] + argv, fake="hang=find", timeout=5)
    assert check.returncode == USAGE                     # only then is the real binary started
    p = run([real_tool] + argv, timeout=10)
    assert p.returncode == USAGE and p.stdout == check.stdout
