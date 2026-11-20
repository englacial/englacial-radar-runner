"""The alerts service and alertctl as real processes, the unit file, and what the service may write.

Everything runs against a scratch directory and the stand-in ntfy server on 127.0.0.1."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from alerts_rig import TOKEN, TOPIC
from fake_ntfy import FakeNtfy
from radar_runner.common import config as C
from radar_runner.common import status as S

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNITS = os.path.join(REPO, "systemd")


@pytest.fixture
def server():
    with FakeNtfy() as s:
        yield s


def write_config(tmp_path, server_url, **alerts):
    for d in ("run", "state", "collections"):
        (tmp_path / d).mkdir(exist_ok=True)
    a = {"summary_after_s": 0.0, "loop_interval_s": 0.2, "unit_name": "proc-unit",
         "ntfy": {"server": server_url, "topic": TOPIC, "token": TOKEN, "timeout_s": 2.0, "retry_s": 0.5,
                  "retry_max_s": 1.0}}
    a.update(alerts)
    config = {"paths": {"run_dir": str(tmp_path / "run"), "state_dir": str(tmp_path / "state"),
                        "power_status_file": str(tmp_path / "run" / "power.json")},
              "manager": {"gates": {"data": {"root": str(tmp_path / "collections")}}},
              "alerts": a}
    (tmp_path / "local.yaml").write_text(json.dumps(config))      # JSON is YAML
    return tmp_path / "local.yaml"


def start(tmp_path, local):
    env = dict(os.environ, PYTHONPATH=REPO)
    env.pop("NOTIFY_SOCKET", None)
    return subprocess.Popen([sys.executable, "-B", "-u", "-m", "radar_runner.alerts", "--local-config", str(local),
                             "--secrets", str(tmp_path / "no-secrets.yaml")],
                            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_status(tmp_path, proc, condition, timeout=20.0):
    deadline = time.monotonic() + timeout
    read = None
    while time.monotonic() < deadline:
        read = S.read_status(str(tmp_path / "run" / "alerts.json"), 5.0)
        if read.ok and read.data.get("pid") == proc.pid and condition(read.data):
            return read.data
        assert proc.poll() is None, proc.stdout.read()
        time.sleep(0.05)
    raise AssertionError(f"status never satisfied the condition: {read}")


def append_event(path, **fields):
    with open(path, "a") as f:
        f.write(json.dumps({"time": time.time(), "time_utc": "2026-11-19T20:00:00Z",
                            "uptime_s": S.boottime_s(), **fields}) + "\n")


def test_the_service_runs_sends_and_stops_cleanly(tmp_path, server):
    local = write_config(tmp_path, server.url)
    proc = start(tmp_path, local)
    try:
        wait_status(tmp_path, proc, lambda d: d["summary_sent"] and d["counters"]["sent"] >= 1)
        append_event(tmp_path / "state" / "watcher-events.jsonl", event="stuck", severity="urgent",
                     text="AMPLIFIER STUCK ENABLED: 2.5 A")
        assert server.wait_for(2)
        assert server.bodies()[1]["priority"] == 5
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(15) == 0
        out = proc.stdout.read()
        assert "Traceback" not in out and "alerts service started" in out
        assert json.loads((tmp_path / "run" / "alerts.json").read_text())["running"] is False
        assert json.loads((tmp_path / "state" / "alerts-state.json").read_text())["clean_stop"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_killed_in_the_middle_of_a_send_loses_nothing(tmp_path, server):
    server.mode = "hang"                    # the request is in flight when the process dies
    local = write_config(tmp_path, server.url, **{"ntfy": {"server": server.url, "topic": TOPIC, "token": TOKEN,
                                                           "timeout_s": 10.0}})
    proc = start(tmp_path, local)
    try:
        deadline = time.monotonic() + 20
        while server.attempts == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.attempts == 1
        proc.kill()
        proc.wait(10)
        assert len(os.listdir(tmp_path / "state" / "alerts-queue")) == 1      # the summary, still queued
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    server.mode = "ok"
    server.release.set()
    proc = start(tmp_path, local)
    try:
        assert server.wait_for(1)
        time.sleep(1.0)
        [summary] = server.bodies()             # sent once: the request that was cut off never got a 200
        assert "summary" in summary["title"]
        assert os.listdir(tmp_path / "state" / "alerts-queue") == []
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(15)


def test_killed_service_leaves_a_status_that_goes_stale_and_nothing_else_changes(tmp_path, server):
    local = write_config(tmp_path, server.url)
    watcher_events = tmp_path / "state" / "watcher-events.jsonl"
    append_event(watcher_events, event="start", severity="info", text="started")
    before = watcher_events.read_bytes()
    proc = start(tmp_path, local)
    try:
        wait_status(tmp_path, proc, lambda d: d["summary_sent"])
        proc.kill()
        proc.wait(10)
        time.sleep(1.2)
        assert not S.read_status(str(tmp_path / "run" / "alerts.json"), 1.0).ok
        assert watcher_events.read_bytes() == before
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


# ---- what the service writes: only its own files

AUDIT = textwrap.dedent("""
    import json, os, sys
    writes = []
    def hook(event, args):
        if event == "open" and args[0] is not None and not isinstance(args[0], int):
            mode, flags = args[1] or "", args[2] or 0
            if any(c in str(mode) for c in "wax+") or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
                writes.append(("open", os.path.abspath(os.fsdecode(args[0]))))
        elif event in ("os.rename", "os.replace"):
            writes.append((event, os.path.abspath(os.fsdecode(args[0]))))
            writes.append((event, os.path.abspath(os.fsdecode(args[1]))))
        elif event in ("os.kill", "os.killpg", "signal.pthread_kill"):
            writes.append((event, repr(args)))
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.link",
                       "os.symlink", "os.utime", "shutil.rmtree") and not isinstance(args[0], int):
            writes.append((event, os.path.abspath(os.fsdecode(args[0]))))
    sys.addaudithook(hook)
    sys.path.insert(0, {tests!r})
    import pathlib
    from alerts_rig import Rig
    from fake_ntfy import FakeNtfy
    with FakeNtfy() as server:
        rig = Rig(pathlib.Path({tmp!r}), server.url, **{{"conditions.status_stale_s": 0.0}})
        rig.start()
        rig.manager_event(event="fault", kind="a", reason="r")
        rig.watcher_event(event="stuck", severity="urgent", text="STUCK")
        inbox = rig.state_dir / "alerts-inbox"
        (inbox / "1-1.json").write_text(json.dumps({{"id": "x", "priority": "low"}}))
        start = len(writes)
        service = rig.service
        for i in range(20):         # the inputs are not rewritten: they go stale, which raises conditions
            rig.clock.advance(1.0)
            service.publish(service.tick())
        service.cleanup()
        print(json.dumps({{"writes": writes[start:], "sent": len(server.received)}}))
""")


def test_every_write_of_the_service_is_inside_its_own_files(tmp_path):
    tests = os.path.dirname(os.path.abspath(__file__))
    script = AUDIT.format(tests=tests, tmp=str(tmp_path))
    r = subprocess.run([sys.executable, "-B", "-c", script], cwd=REPO, env=dict(os.environ, PYTHONPATH=REPO),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout.strip().split("\n")[-1])
    assert result["sent"] >= 3
    state, run = str(tmp_path / "state"), str(tmp_path / "run")
    own = (os.path.join(state, "alerts-queue") + "/", os.path.join(state, "alerts-inbox") + "/",
           os.path.join(state, ".alerts-state.json."), os.path.join(state, "alerts-state.json"),
           os.path.join(run, ".alerts.json."), os.path.join(run, "alerts.json"))
    assert result["writes"], "the audit saw nothing"
    for event, path in result["writes"]:
        path = path.strip("'")
        assert event not in ("os.kill", "os.killpg", "signal.pthread_kill"), (event, path)
        assert path.startswith(own) or path in (os.path.join(state, "alerts-queue"),
                                                os.path.join(state, "alerts-inbox")), (event, path)


# ---- the unit file

def unit_lines(name):
    with open(os.path.join(UNITS, name)) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def test_the_unit_is_an_observer_that_needs_nothing_and_nothing_needs():
    lines = unit_lines("radar-alerts.service")
    for wanted in ("User=englacial", "Group=englacial", "Type=notify", "NotifyAccess=main", "Restart=always",
                   "StartLimitIntervalSec=0", "WatchdogSec=60", "WatchdogSignal=SIGKILL", "Nice=10",
                   "ProtectSystem=strict", "ReadWritePaths=-/run/radar-runner/user -/var/lib/radar-runner/user",
                   "NoNewPrivileges=yes", "WantedBy=multi-user.target", "OOMScoreAdjust=500",
                   "WorkingDirectory=/home/englacial/englacial-radar-runner",
                   "ExecStart=/home/englacial/englacial-radar-runner/.venv/bin/python -B -u -m radar_runner.alerts"):
        assert wanted in lines, wanted
    for line in lines:
        key = line.split("=")[0]
        assert key not in ("Requires", "Wants", "BindsTo", "PartOf", "Requisite", "Before", "Upholds"), line
        if key == "After":
            assert line == "After=local-fs.target network.target", line     # ordering only; no network-online wait
    # No other unit of this project refers to it.
    for name in os.listdir(UNITS):
        if name != "radar-alerts.service":
            with open(os.path.join(UNITS, name)) as f:
                assert "radar-alerts" not in f.read(), name
    # The watchdog outlasts a full round of sends between two pings.
    a = C.DEFAULTS["alerts"]
    assert a["ntfy"]["timeout_s"] * 2 < 60 and a["loop_interval_s"] * 10 < 60


def test_the_secrets_belong_in_the_secrets_file_only():
    import yaml
    with open(C.REPO_FILE) as f:
        repo = yaml.safe_load(f)
    assert {k: repo["alerts"]["ntfy"][k] for k in ("server", "topic", "token")} == \
        {"server": "-", "topic": "-", "token": "-"}


def test_the_offload_section_of_the_secrets_file_is_accepted_and_not_used(tmp_path):
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("alerts:\n  ntfy: {server: 'https://ntfy.example', topic: t1, token: tk_x}\n"
                       "offload:\n  gcs: {bucket: b, credentials_file: /etc/radar-runner/gcs.json}\n")
    secrets.chmod(0o640)
    config, warnings = C.load(local_file=None, secrets_file=str(secrets))
    assert warnings == [] and "offload" not in config
    assert config["alerts"]["ntfy"]["topic"] == "t1"
    secrets.write_text("alerts:\n  ntfy: {topic: 5, colour: red}\n")
    _, warnings = C.load(local_file=None, secrets_file=str(secrets))
    assert len(warnings) == 2 and all("alerts.ntfy." in w for w in warnings)
    assert not any("tk_" in w or "t1" in w for w in warnings)


# ---- alertctl

def alertctl(tmp_path, local, *args, timeout=60):
    return subprocess.run([sys.executable, "-B", "-m", "radar_runner.alerts.ctl", "--local-config", str(local), *args],
                          cwd=REPO, env=dict(os.environ, PYTHONPATH=REPO), capture_output=True, text=True,
                          timeout=timeout)


def test_alertctl_test_goes_through_the_real_path_and_status_shows_it(tmp_path, server):
    local = write_config(tmp_path, server.url)
    r = alertctl(tmp_path, local, "status")
    assert r.returncode == 3 and "has not written" in r.stdout
    r = alertctl(tmp_path, local, "test", "--priority", "urgent")
    assert r.returncode == 3 and "does not exist" in r.stdout      # alertctl never makes the inbox
    assert not (tmp_path / "state" / "alerts-inbox").exists()
    proc = start(tmp_path, local)
    try:
        wait_status(tmp_path, proc, lambda d: d["summary_sent"])
        proc.send_signal(signal.SIGTERM)        # stopped: the request waits in the inbox
        assert proc.wait(10) == 0
        r = alertctl(tmp_path, local, "test", "--priority", "urgent")
        assert r.returncode == 3 and "is sent when it starts" in r.stdout
        assert alertctl(tmp_path, local, "status").returncode == 3
        proc = start(tmp_path, local)
        assert server.wait_for(2)           # the summary and the test alert
        r = alertctl(tmp_path, local, "test", "--priority", "high", "--text", "from the test", "--wait", "20")
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.strip().endswith(": sent")
        test = [b for b in server.bodies() if "test alert" in b["title"]]
        assert [b["priority"] for b in test] == [5, 4] and "from the test" in test[1]["message"]
        r = alertctl(tmp_path, local, "status")
        assert r.returncode == 0 and "ntfy:       configured" in r.stdout and "queue:      0 waiting" in r.stdout
        assert TOKEN not in r.stdout and TOPIC not in r.stdout
        r = alertctl(tmp_path, local, "status", "--json")
        assert json.loads(r.stdout)["counters"]["sent"] >= 2         # this process: the two test alerts
        assert "no alerts waiting" in alertctl(tmp_path, local, "queue").stdout
        server.mode, server.status = "status", 503
        r = alertctl(tmp_path, local, "test", "--wait", "3")
        assert r.returncode == 1 and "not sent within 3 s" in r.stdout
        r = alertctl(tmp_path, local, "queue")
        assert "test alert (default)" in r.stdout and "p3-" in r.stdout
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(15)


def test_alertctl_never_reads_the_secrets_file():
    with open(os.path.join(REPO, "radar_runner", "alerts", "ctl.py")) as f:
        text = f.read()
    assert "secrets_file=None" in text and "SECRETS_FILE" not in text


# ---- a stop never waits for the network (the power service forces the power-off 10 s after asking)

SLOW_DNS = textwrap.dedent("""
    import socket, sys, time
    def hung(*args, **kwargs):
        time.sleep(120)             # a DNS lookup that hangs: getaddrinfo cannot be interrupted
        raise socket.gaierror(-3, "Temporary failure in name resolution")
    socket.getaddrinfo = hung
    sys.argv = ["radar_runner.alerts"] + sys.argv[1:]
    import runpy
    runpy.run_module("radar_runner.alerts", run_name="__main__")
""")


def stop_during_send(tmp_path, proc, attempted):
    deadline = time.monotonic() + 20
    while not attempted() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert attempted(), proc.stdout.read() if proc.poll() is not None else "no send started"
    time.sleep(1.0)
    t = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    status = proc.wait(10)
    took = time.monotonic() - t
    out = proc.stdout.read()
    assert status == 0 and "Traceback" not in out, out
    state = json.loads((tmp_path / "state" / "alerts-state.json").read_text())
    assert state["clean_stop"] is True
    assert len(os.listdir(tmp_path / "state" / "alerts-queue")) == 1        # the alert stays queued
    return took


def test_a_stop_during_a_send_to_a_server_that_does_not_answer_takes_under_two_seconds(tmp_path, server):
    server.mode = "hang"
    local = write_config(tmp_path, server.url, **{"ntfy": {"server": server.url, "topic": TOPIC, "token": TOKEN,
                                                           "timeout_s": 25.0}})
    proc = start(tmp_path, local)
    try:
        took = stop_during_send(tmp_path, proc, lambda: server.attempts >= 1)
        assert took < 2.0, took
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_a_stop_during_a_hung_dns_lookup_takes_under_two_seconds(tmp_path):
    local = write_config(tmp_path, "https://ntfy.invalid", **{"ntfy": {
        "server": "https://ntfy.invalid", "topic": TOPIC, "token": TOKEN, "timeout_s": 25.0}})
    env = dict(os.environ, PYTHONPATH=REPO)
    env.pop("NOTIFY_SOCKET", None)
    proc = subprocess.Popen([sys.executable, "-B", "-u", "-c", SLOW_DNS, "--local-config", str(local),
                             "--secrets", str(tmp_path / "no-secrets.yaml")],
                            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        def attempted():
            read = S.read_status(str(tmp_path / "run" / "alerts.json"), 5.0)
            return read.ok and read.data.get("sending") is not None
        took = stop_during_send(tmp_path, proc, attempted)
        assert took < 2.0, took
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_the_unit_gives_a_stop_less_time_than_the_power_service_gives_the_power_off():
    stop = [int(line.split("=")[1]) for line in unit_lines("radar-alerts.service") if line.startswith("TimeoutStopSec=")]
    with open(os.path.join(REPO, "power", "config.py")) as f:
        assert "last_resort_after_s: float = 10.0" in f.read()
    assert stop == [5]
