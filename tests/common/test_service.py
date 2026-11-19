"""The service loop (radar_runner/common/service.py), in simulated time and as a real process."""
import copy
import io
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

import pytest

from radar_runner.common import config as C
from radar_runner.common import status as S
from radar_runner.common.service import Service

REPO = C.REPO


class FakeNotifier:
    ping_interval = 1.0

    def __init__(self):
        self.sent = []

    def ready(self):
        self.sent.append("READY")

    def stopping(self):
        self.sent.append("STOPPING")

    def watchdog(self):
        self.sent.append("WATCHDOG")


class Sim:
    """Simulated time: waiting on the stop event advances the clock."""

    def __init__(self):
        self.t = 0.0
        self.waits = []


def make(service_class, tmp_path, **attrs):
    """Build a service whose clock is simulated and whose status goes to tmp_path."""
    config = copy.deepcopy(C.DEFAULTS)
    config["paths"]["run_dir"] = str(tmp_path)
    sim = Sim()
    service = service_class(config, notifier=FakeNotifier(), monotonic=lambda: sim.t,
                            config_warnings=attrs.pop("config_warnings", ()))
    for k, v in attrs.items():
        setattr(service, k, v)

    def wait(timeout):
        sim.waits.append(timeout)
        sim.t += timeout
        return service._stop.is_set()
    service._stop.wait = wait
    service.sim = sim
    return service


class Counter(Service):
    name = "counter"
    stop_after = 3

    def setup(self):
        self.count = 0
        self.tick_times = []
        self.cleaned = False

    def tick(self):
        self.count += 1
        self.tick_times.append(self.sim.t)
        if self.count >= self.stop_after:
            self.request_stop()
        return {"state": "running", "count": self.count}

    def cleanup(self):
        self.cleaned = True


@pytest.fixture
def log_text():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield stream
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_runs_ticks_at_the_interval_and_publishes(tmp_path):
    service = make(Counter, tmp_path)
    assert service.run() == 0
    assert service.tick_times == [0.0, 1.0, 2.0]
    assert service.cleaned
    data = json.loads((tmp_path / "counter.json").read_text())
    assert data["state"] == "running" and data["count"] == 3 and "uptime_s" in data
    assert "config_warnings" not in data
    sent = service.notifier.sent
    assert sent[0] == "READY" and sent[-1] == "STOPPING" and sent.count("WATCHDOG") >= 3


def test_status_is_readable_with_the_shared_reader(tmp_path):
    service = make(Counter, tmp_path, stop_after=1)
    service.run()
    r = S.read_status(S.status_path("counter", str(tmp_path)), 5.0)
    assert r.ok and r.data["count"] == 1


def test_watchdog_is_pinged_during_a_long_interval(tmp_path):
    service = make(Counter, tmp_path, interval_s=10.0, stop_after=2)
    service.run()
    assert service.tick_times == [0.0, 10.0]
    assert max(service.sim.waits) <= 1.0                       # never sleeps past a ping
    assert service.notifier.sent.count("WATCHDOG") >= 10


def test_slow_tick_keeps_a_gap(tmp_path):
    class Slow(Counter):
        def tick(self):
            data = super().tick()
            self.sim.t += 2.5                                  # longer than the interval
            return data

    service = make(Slow, tmp_path)
    service.run()
    # The next round starts a quarter interval after a late one ends, not at once.
    assert service.tick_times == [0.0, 2.75, 5.5]


def test_config_warnings_are_logged_and_published(tmp_path, log_text):
    service = make(Counter, tmp_path, stop_after=1, config_warnings=["x.yaml: unknown setting a: ignored"])
    service.run()
    assert "config: x.yaml: unknown setting a: ignored" in log_text.getvalue()
    data = json.loads((tmp_path / "counter.json").read_text())
    assert data["config_warnings"] == ["x.yaml: unknown setting a: ignored"]


def test_failing_tick_publishes_nothing_then_recovers(tmp_path, log_text):
    class Flaky(Counter):
        stop_after = 5

        def tick(self):
            data = super().tick()
            if self.count in (2, 3):
                raise OSError("sensor gone")
            return data

    service = make(Flaky, tmp_path)
    published = []
    service.status.write = lambda data: published.append(data["count"]) or ""
    assert service.run() == 0
    assert published == [1, 4, 5]
    assert log_text.getvalue().count("OSError: sensor gone") == 2


def test_repeated_failure_exits_with_status_1(tmp_path, log_text):
    class Broken(Service):
        name = "broken"
        rounds = 0
        cleaned = False

        def tick(self):
            self.rounds += 1
            raise RuntimeError("always")

        def cleanup(self):
            self.cleaned = True

    service = make(Broken, tmp_path, max_failures=4)
    assert service.run() == 1
    assert service.rounds == 4 and service.cleaned
    assert "giving up" in log_text.getvalue()
    assert not (tmp_path / "broken.json").exists()


def test_failures_must_be_in_a_row(tmp_path):
    class Alternating(Counter):
        stop_after = 40

        def tick(self):
            data = super().tick()
            if self.count % 2:
                raise RuntimeError("every other round")
            return data

    service = make(Alternating, tmp_path, max_failures=3)
    assert service.run() == 0 and service.count == 40


def test_setup_failure_exits_before_ready(tmp_path, log_text):
    class NoDevice(Counter):
        def setup(self):
            raise OSError("no such device")

    service = make(NoDevice, tmp_path)
    assert service.run() == 1
    assert service.notifier.sent == []
    assert "setup failed" in log_text.getvalue()


def test_cleanup_failure_is_reported(tmp_path, log_text):
    class BadCleanup(Counter):
        def cleanup(self):
            raise OSError("close failed")

    assert make(BadCleanup, tmp_path, stop_after=1).run() == 1
    assert "cleanup failed" in log_text.getvalue()


def test_tick_may_publish_nothing_or_something_unusable(tmp_path, log_text):
    class Quiet(Counter):
        def tick(self):
            super().tick()
            return None

    service = make(Quiet, tmp_path)
    assert service.run() == 0 and not (tmp_path / "counter.json").exists()

    class Wrong(Counter):
        def tick(self):
            super().tick()
            return 5

    assert make(Wrong, tmp_path, max_failures=2).run() == 1      # counted as failed rounds


def test_status_write_failure_is_logged_once_and_does_not_stop_the_loop(tmp_path, log_text):
    service = make(Counter, tmp_path / "missing-dir", stop_after=5)
    assert service.run() == 0 and service.count == 5
    assert log_text.getvalue().count("cannot write status file") == 1


def test_status_write_recovery_is_logged(tmp_path, log_text):
    class MakesDir(Counter):
        stop_after = 4

        def tick(self):
            if self.count == 2:
                os.mkdir(self.config["paths"]["run_dir"])
            return super().tick()

    service = make(MakesDir, tmp_path / "late-dir")
    assert service.run() == 0
    text = log_text.getvalue()
    assert text.count("cannot write status file") == 1 and text.count("is being written") == 1


def test_stop_request_ends_a_wait_at_once(tmp_path):
    service = make(Counter, tmp_path, interval_s=100.0, stop_after=1)
    service.run()
    assert service.sim.waits == [] and service.tick_times == [0.0]


# ---- as a real process, with a stand-in for systemd's notification socket

def run_example(tmp_path, local_yaml, extra_env=None):
    name = "@radar-runner-test-" + uuid.uuid4().hex
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind("\0" + name[1:])
    sock.settimeout(10)
    local = tmp_path / "local.yaml"
    local.write_text(local_yaml)
    env = {"PATH": os.environ["PATH"], "NOTIFY_SOCKET": name, "WATCHDOG_USEC": "600000",
           "JOURNAL_STREAM": "8:1"}
    env.update(extra_env or {})
    proc = subprocess.Popen(
        [sys.executable, "-B", "-u", "-m", "radar_runner.example",
         "--local-config", str(local), "--secrets", str(tmp_path / "no-secrets.yaml")],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc, sock


def test_example_service_process(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    proc, sock = run_example(tmp_path, f"paths: {{run_dir: {run_dir}}}\nbogus: 1\n")
    try:
        assert sock.recv(4096) == b"READY=1"
        pings = [sock.recv(4096) for _ in range(3)]              # 0.2 s apart
        assert pings == [b"WATCHDOG=1"] * 3
        deadline = time.monotonic() + 10
        while True:
            r = S.read_status(str(run_dir / "example.json"), 5.0)
            if r.ok or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        assert r.ok and r.data["state"] == "running" and r.data["count"] >= 1
        assert r.data["pid"] == proc.pid
        assert any("unknown setting bogus" in w for w in r.data["config_warnings"])
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    finally:
        proc.kill()
        sock.close()
    assert proc.returncode == 0
    assert "<6>example: example service started" in out
    assert "<4>example: config: " in out and "unknown setting bogus" in out
    assert "<6>example: example service stopping" in out
    assert "Traceback" not in out


def test_example_service_stops_on_sigint_and_reports_stopping(tmp_path):
    proc, sock = run_example(tmp_path, f"paths: {{run_dir: {tmp_path}}}\n")
    try:
        assert sock.recv(4096) == b"READY=1"
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        assert time.monotonic() - started < 2                    # does not wait out the interval
        sock.setblocking(False)
        messages = []
        try:
            while True:
                messages.append(sock.recv(4096))
        except BlockingIOError:
            pass
        assert messages[-1] == b"STOPPING=1"
    finally:
        proc.kill()
        proc.communicate()
        sock.close()
    assert proc.returncode == 0
