"""Tests of tools/sdr_gpio_safe: command line, exit codes, the write/verify logic and the time limit.

See conftest.py for why none of this can touch the SDR.
"""
import fcntl
import os
import subprocess
import time

import pytest

from tool_helpers import run

OK, NOT_FOUND, ASSUME_ENABLED, TIMEOUT, BUSY, AMBIGUOUS, USAGE = 0, 2, 3, 4, 6, 7, 64
NOT_INITIALISED = "SDR not initialised by this run, pin not changed by it; amplifier state unknown"
ASSUME = "did not verify it high again: assume the amplifier is ENABLED"

# Each of these must be rejected before the SDR is looked for. The pin, its level, the
# bank and the settle time are fixed: options for them could report OK with the
# amplifier enabled.
BAD_COMMAND_LINES = [
    ["--bogus"], ["stray"], ["--high"], ["--low"],
    ["--pin", "0"], ["--pin", "5"], ["--bank", "FP0"], ["--bank", "FP1"],
    ["--shutdown-level", "low"], ["--shutdown-level", "high"], ["--settle-ms", "0"], ["--settle-ms", "1500"],
    ["--timeout"], ["--timeout", "0"], ["--timeout", "0.5"], ["--timeout", "301"], ["--timeout", "nan"],
    ["--timeout", "inf"], ["--timeout", "1e2"], ["--timeout", "5s"], ["--timeout", "-1"], ["--timeout", ""],
    ["--timeout=5"], ["--args"], ["--args", "a=b", "stray"],
]


def one_line(p):
    lines = p.stdout.splitlines()
    assert len(lines) == 1 and p.stdout.endswith("\n"), p.stdout
    assert lines[0].startswith("sdr_gpio_safe: ")
    return lines[0]


def usb_dir(tmp_path, *vendors):
    """A stand-in for /sys/bus/usb/devices with one device per vendor ID."""
    d = tmp_path / "usb"
    for i, vendor in enumerate(vendors):
        (d / f"4-{i + 1}").mkdir(parents=True)
        (d / f"4-{i + 1}" / "idVendor").write_text(vendor + "\n")
    (d / "usb4").mkdir(parents=True, exist_ok=True)       # an entry without idVendor
    return d


# --- command line ---------------------------------------------------------------

def test_help_does_not_look_for_the_sdr(fake_tool):
    for argv in (["--help"], ["-h"], ["--args", "x", "--help"]):
        p = run([fake_tool] + argv, fake="hang=find", timeout=5)   # would hang if it looked
        assert p.returncode == 0
        assert "Exit codes" in p.stdout and "ASSUME_ENABLED" in p.stdout


@pytest.mark.parametrize("argv", BAD_COMMAND_LINES, ids=" ".join)
def test_usage_error_does_not_touch_the_device(fake_tool, tmp_path, argv):
    log = tmp_path / "log"
    p = run([fake_tool] + argv, fake=f"hang=find,log={log}", timeout=5)   # would hang if it looked
    assert p.returncode == USAGE
    line = one_line(p)
    assert line.startswith("sdr_gpio_safe: USAGE: ") and line.endswith("SDR not touched")
    assert not log.exists()


def test_args_option_is_accepted(fake_tool):
    assert run([fake_tool, "--args", "type=b200", "--timeout", "5"]).returncode == OK


# --- writing and verifying --------------------------------------------------------

def test_success_writes_manual_high_output_in_order(fake_tool, tmp_path):
    log = tmp_path / "log"
    p = run([fake_tool], fake=f"log={log}")
    assert p.returncode == OK
    line = one_line(p)
    assert line.startswith("sdr_gpio_safe: OK: FP0 GPIO_0 is a manual output commanded high") and "1 write," in line
    # CTRL 0 = manual, OUT 1 = high (after CTRL), DDR 1 = output; then two readbacks, no write between
    assert log.read_text().splitlines() == ["CTRL 0 1", "OUT 1 1", "DDR 1 1", "READBACK 1", "READBACK 1"]


def test_settle_time_of_the_real_build(fake_tool_as_built):
    t = time.monotonic()
    p = run([fake_tool_as_built])
    assert p.returncode == OK and time.monotonic() - t >= 1.5
    assert "read back high twice 1500 ms apart" in one_line(p)


@pytest.mark.parametrize("fake, writes", [("lost=OUT:3", 4), ("lost=CTRL:2", 3), ("lost=DDR:2", 1),
                                          ("write_throws=4", 5), ("read_throws=2", 3), ("reverts=1", 2)])
def test_writes_are_repeated_until_the_pin_reads_high(fake_tool, tmp_path, fake, writes):
    log = tmp_path / "log"
    p = run([fake_tool], fake=f"{fake},log={log}")
    assert p.returncode == OK and f"{writes} write" in one_line(p)
    out_writes = [l for l in log.read_text().splitlines() if l.startswith("OUT")]
    assert out_writes and set(out_writes) == {"OUT 1 1"}        # only ever the shutdown level


def test_hang_when_closing_cannot_delay_the_result(fake_tool):
    t = time.monotonic()
    p = run([fake_tool], fake="hang=close", timeout=5)          # the device is never closed
    assert p.returncode == OK and time.monotonic() - t < 1.0


def test_blocked_output_cannot_delay_the_exit(fake_tool):
    """Standard output is a full pipe that nobody reads: the line is dropped, the exit code stands."""
    r, w = os.pipe()
    try:
        os.set_blocking(w, False)
        try:
            while True:
                os.write(w, b"x" * 65536)
        except BlockingIOError:
            pass
        os.set_blocking(w, True)
        t = time.monotonic()
        p = subprocess.run([str(fake_tool)], stdout=w, stdin=subprocess.DEVNULL, timeout=10)
        assert p.returncode == OK and time.monotonic() - t < 2.0
        assert not fcntl.fcntl(w, fcntl.F_GETFL) & os.O_NONBLOCK     # the tool left the pipe as it was
    finally:
        os.close(r)
        os.close(w)


# --- results that mean: SDR not initialised by this run ---------------------------------

def check_not_initialised(p, code, name, log):
    assert p.returncode == code
    line = one_line(p)
    assert line.startswith(f"sdr_gpio_safe: {name}: ") and NOT_INITIALISED in line and "ENABLED" not in line
    assert not log.exists()              # never opened, nothing written
    return line


def test_not_found(fake_tool, tmp_path):
    log = tmp_path / "log"
    check_not_initialised(run([fake_tool], fake=f"found=0,log={log}"), NOT_FOUND, "NOT_FOUND", log)
    other = usb_dir(tmp_path, "1546", "1a86")
    check_not_initialised(run([fake_tool], fake=f"found=0,sysfs={other},log={log}"), NOT_FOUND, "NOT_FOUND", log)
    check_not_initialised(run([fake_tool], fake=f"found=0,sysfs={tmp_path}/missing,log={log}"),
                          NOT_FOUND, "NOT_FOUND", log)


def test_sdr_on_usb_but_not_usable_is_busy(fake_tool, tmp_path):
    log = tmp_path / "log"
    usb = usb_dir(tmp_path, "1546", "2500")
    line = check_not_initialised(run([fake_tool], fake=f"found=0,sysfs={usb},log={log}"), BUSY, "BUSY", log)
    assert "an SDR is connected (USB 4-2)" in line and "in use by another process" in line


def test_failed_search_is_busy_or_not_found_and_one_line(fake_tool, tmp_path):
    log = tmp_path / "log"                                # the stand-in's error text has a newline
    line = check_not_initialised(run([fake_tool], fake=f"find_throws=1,log={log}"), NOT_FOUND, "NOT_FOUND", log)
    assert "the search failed: fake: cannot enumerate" in line
    usb = usb_dir(tmp_path, "2500")
    check_not_initialised(run([fake_tool], fake=f"find_throws=1,sysfs={usb},log={log}"), BUSY, "BUSY", log)


def test_two_sdrs_are_not_opened(fake_tool, tmp_path):
    log = tmp_path / "log"
    line = check_not_initialised(run([fake_tool], fake=f"found=2,log={log}"), AMBIGUOUS, "AMBIGUOUS", log)
    assert "2 SDRs match" in line


def test_time_limit_before_initialisation(fake_tool, tmp_path):
    log = tmp_path / "log"
    t = time.monotonic()
    p = run([fake_tool, "--timeout", "1"], fake=f"hang=find,log={log}", timeout=10)
    assert 1.0 <= time.monotonic() - t < 2.0
    line = check_not_initialised(p, TIMEOUT, "TIMEOUT", log)
    assert "time limit of 1.0 s reached while looking for the SDR" in line


# --- every failure after the initialisation began: assume the amplifier is enabled ---------

@pytest.mark.parametrize("fake, argv, text", [
    ("open_throws=1", [], "initialisation of type=fake,serial=TEST failed: fake: init failed"),
    ("hang=open", ["--timeout", "1"], "time limit of 1.0 s reached while initialising the SDR"),
    ("hang=write", ["--timeout", "1"], "time limit of 1.0 s reached while writing the pin"),
    ("hang=read", ["--timeout", "1"], "time limit of 1.0 s reached while writing the pin"),
    ("stuck=1", ["--timeout", "1"], "time limit of 1.0 s reached while writing the pin"),
    ("stuck=1", [], "FP0 GPIO_0 never read high (30 writes, 1 round)"),
    ("lost=CTRL:999", [], "FP0 GPIO_0 never read high (30 writes, 1 round)"),
    ("lost=OUT:999", [], "FP0 GPIO_0 never read high (30 writes, 1 round)"),
    ("write_throws=999", [], "never read high (30 writes, 1 round); last error: fake: write failed"),
    ("read_throws=999", [], "never read high (30 writes, 1 round); last error: fake: readback failed"),
    ("reverts=9", [], "FP0 GPIO_0 went back to low after reading high (3 writes, 3 rounds)"),
], ids=lambda v: " ".join(v) if isinstance(v, list) else None)
def test_failure_after_initialisation_began_is_assume_enabled(fake_tool, fake, argv, text):
    t = time.monotonic()
    p = run([fake_tool] + argv, fake=fake, timeout=15)
    assert p.returncode == ASSUME_ENABLED
    line = one_line(p)
    assert line.startswith("sdr_gpio_safe: ASSUME_ENABLED: ") and text in line and ASSUME in line
    assert time.monotonic() - t < 6      # bounded also without the time limit


# --- the real binary --------------------------------------------------------------------

def test_real_binary_help(fake_tool, real_tool):
    expected = run([fake_tool, "--help"], fake="hang=find", timeout=5)
    assert expected.returncode == 0                      # the same source answers --help without a device
    p = run([real_tool, "--help"], timeout=10)
    assert p.returncode == 0 and p.stdout == expected.stdout


@pytest.mark.parametrize("argv", [["--bogus"], ["--pin", "0"], ["--shutdown-level", "low"]], ids=" ".join)
def test_real_binary_usage_error(fake_tool, real_tool, argv):
    check = run([fake_tool] + argv, fake="hang=find", timeout=5)
    assert check.returncode == USAGE                     # only then is the real binary started
    p = run([real_tool] + argv, timeout=10)
    assert p.returncode == USAGE and p.stdout == check.stdout
