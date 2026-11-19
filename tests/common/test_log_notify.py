"""Logging and systemd notification helpers (radar_runner/common/log.py, sdnotify.py)."""
import io
import logging
import os
import socket
import sys
import uuid
import warnings

import pytest

from radar_runner.common.log import setup_logging
from radar_runner.common.sdnotify import Notifier


@pytest.fixture
def clean_logging():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, sys.excepthook)
    yield
    root.handlers[:], root.level, sys.excepthook = saved[0], saved[1], saved[2]
    logging.captureWarnings(False)


def lines(stream):
    return stream.getvalue().splitlines()


def test_journal_priorities(clean_logging):
    out = io.StringIO()
    assert setup_logging("debug", stream=out, environ={"JOURNAL_STREAM": "8:12345"}) == ""
    log = logging.getLogger("telemetry")
    log.debug("d")
    log.info("i %d", 5)
    log.warning("w")
    log.error("e")
    log.critical("c")
    assert lines(out) == ["<7>telemetry: d", "<6>telemetry: i 5", "<4>telemetry: w",
                          "<3>telemetry: e", "<2>telemetry: c"]


def test_every_line_of_a_traceback_has_the_priority(clean_logging):
    out = io.StringIO()
    setup_logging("info", stream=out, environ={"JOURNAL_STREAM": "8:1"})
    try:
        raise ValueError("first line\nsecond line")
    except ValueError:
        logging.getLogger("x").exception("it failed")
    got = lines(out)
    assert got[0] == "<3>x: it failed" and len(got) > 4
    assert all(line.startswith("<3>") for line in got)
    assert got[-1] == "<3>second line"


def test_level_filter_and_unknown_level(clean_logging):
    out = io.StringIO()
    assert setup_logging("warning", stream=out, environ={"JOURNAL_STREAM": "1:1"}) == ""
    logging.getLogger("x").info("hidden")
    logging.getLogger("x").warning("shown")
    assert lines(out) == ["<4>x: shown"]
    assert "unknown logging level" in setup_logging("loud", stream=out, environ={})
    assert logging.getLogger().level == logging.INFO


def test_plain_output_outside_systemd(clean_logging):
    out = io.StringIO()
    setup_logging("info", stream=out, environ={})
    logging.getLogger("x").warning("hello")
    (line,) = lines(out)
    assert line.endswith(" WARNING x: hello") and not line.startswith("<")


def test_calling_again_does_not_duplicate_output(clean_logging):
    out = io.StringIO()
    for _ in range(3):
        setup_logging("info", stream=out, environ={"JOURNAL_STREAM": "1:1"})
    logging.getLogger("x").info("once")
    assert lines(out) == ["<6>x: once"]


def test_library_warnings_are_logged(clean_logging):
    out = io.StringIO()
    setup_logging("info", stream=out, environ={"JOURNAL_STREAM": "1:1"})
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("pin factory fallback")
    assert any(line.startswith("<4>py.warnings: ") and "pin factory fallback" in line for line in lines(out))


def test_uncaught_exception_is_logged_as_critical(clean_logging):
    out = io.StringIO()
    setup_logging("info", stream=out, environ={"JOURNAL_STREAM": "1:1"})
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    got = lines(out)
    assert got[0] == "<2>root: uncaught exception" and got[-1] == "<2>RuntimeError: boom"


def test_broken_stdout_does_not_raise(clean_logging, capsys):
    class Broken:
        def write(self, text):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    setup_logging("info", stream=Broken(), environ={"JOURNAL_STREAM": "1:1"})
    logging.getLogger("x").error("lost")
    assert capsys.readouterr().err == ""


# ---- sd_notify

@pytest.fixture
def notify_socket():
    """A datagram socket standing in for systemd's. Abstract, so no path-length limit."""
    name = "@radar-runner-test-" + uuid.uuid4().hex
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind("\0" + name[1:])
    sock.settimeout(5)
    yield name, sock
    sock.close()


def received(sock):
    got = []
    sock.setblocking(False)
    try:
        while True:
            got.append(sock.recv(4096).decode())
    except BlockingIOError:
        return got


def test_no_socket_means_no_op():
    n = Notifier({})
    assert not n.enabled and n.watchdog_s is None and n.ping_interval == 1.0
    assert n.ready() is False and n.send("X=1") is False
    n.watchdog()
    n.stopping()
    assert not Notifier({"NOTIFY_SOCKET": "relative/path"}).enabled


def test_messages(notify_socket):
    name, sock = notify_socket
    n = Notifier({"NOTIFY_SOCKET": name})
    assert n.enabled and n.ready() and n.status("two\nlines") and n.stopping()
    assert received(sock) == ["READY=1", "STATUS=two lines", "STOPPING=1"]


def test_path_socket(tmp_path):
    path = str(tmp_path / "n.sock")
    if len(path) > 100:
        pytest.skip("the temporary directory's path is too long for a socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    try:
        assert Notifier({"NOTIFY_SOCKET": path}).ready()
        assert received(sock) == ["READY=1"]
    finally:
        sock.close()


def test_watchdog_interval_and_rate(notify_socket):
    name, sock = notify_socket
    now = [100.0]
    n = Notifier({"NOTIFY_SOCKET": name, "WATCHDOG_USEC": "30000000"}, monotonic=lambda: now[0])
    assert n.watchdog_s == 30.0 and n.ping_interval == 1.0       # at least once a second
    n.watchdog()
    n.watchdog()
    now[0] += 0.9
    n.watchdog()
    assert received(sock) == ["WATCHDOG=1"]
    now[0] += 0.1
    n.watchdog()
    assert received(sock) == ["WATCHDOG=1"]
    n = Notifier({"NOTIFY_SOCKET": name, "WATCHDOG_USEC": "1500000"})
    assert n.ping_interval == pytest.approx(0.5)                 # three pings per period


def test_watchdog_meant_for_another_process_or_malformed():
    assert Notifier({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "1"}).watchdog_s is None
    assert Notifier({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": str(os.getpid())}).watchdog_s == 30.0
    for bad in ("", "abc", "-5", "0"):
        n = Notifier({"WATCHDOG_USEC": bad})
        assert n.watchdog_s is None and n.ping_interval == 1.0


def test_send_failure_is_not_raised():
    n = Notifier({"NOTIFY_SOCKET": "@radar-runner-test-nobody-" + uuid.uuid4().hex})
    assert n.enabled and n.ready() is False
    n.watchdog()
