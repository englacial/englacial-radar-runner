"""The command socket, the event log, collection names and the manager's settings."""
import json
import os
import socket
import stat

import pytest
import yaml

from radar_runner.common import config as common_config
from radar_runner.manager import collection as coll
from radar_runner.manager import control, radarctl


# -------------------------------------------------------------------- socket

@pytest.fixture
def server(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return {"ok": request.get("cmd") != "bad", "message": f"did {request.get('cmd')}",
                "status": {"state": "ready", "idle": True, "radar_gone": True, "runs_ended": 0}}
    srv = control.ControlServer(str(tmp_path / "manager.sock"), handler)
    srv.open()
    srv.seen = seen
    yield srv
    srv.close()


def test_request_and_reply(server):
    reply = radarctl.request(server.path, {"cmd": "start"})
    assert reply == {"ok": True, "message": "did start",
                     "status": {"state": "ready", "idle": True, "radar_gone": True, "runs_ended": 0}}
    assert server.seen == [{"cmd": "start"}]
    assert stat.S_IMODE(os.stat(server.path).st_mode) == 0o660


def test_garbage_does_not_reach_the_handler_or_stop_the_server(server):
    for payload in (b"not json\n", b"[1, 2]\n", b"\n", b"x" * 100000, b""):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(server.path)
        try:
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            data = s.recv(65536)
        except OSError:
            data = b""
        finally:
            s.close()
        if data:
            assert json.loads(data) == {"ok": False, "message": "request not understood"}
    assert server.seen == []
    assert radarctl.request(server.path, {"cmd": "status"})["ok"]


def test_handler_error_does_not_stop_the_server(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True, "message": ""}
    srv = control.ControlServer(str(tmp_path / "m.sock"), handler)
    srv.open()
    try:
        with pytest.raises(radarctl.NotRunning):
            radarctl.request(srv.path, {"cmd": "x"})
        assert radarctl.request(srv.path, {"cmd": "y"})["ok"]
    finally:
        srv.close()


def test_stale_socket_file_is_replaced_and_removed_at_close(tmp_path):
    path = str(tmp_path / "manager.sock")
    old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old.bind(path)
    old.close()                                             # left behind by a killed manager
    srv = control.ControlServer(path, lambda r: {"ok": True, "message": ""})
    srv.open()
    assert radarctl.request(path, {"cmd": "status"})["ok"]
    srv.close()
    assert not os.path.exists(path)


def test_radarctl_exit_codes_and_output(server, capsys):
    assert radarctl.main(["--socket", server.path, "start"]) == 0
    assert capsys.readouterr().out == "did start\n"
    assert radarctl.main(["--socket", server.path, "stop", "--wait"]) == 0
    assert capsys.readouterr().out == "did stop\nstopped\n"         # the stand-in says radar_gone at once
    assert radarctl.main(["--socket", server.path, "stop", "--interrupt"]) == 0
    assert radarctl.main(["--socket", server.path, "override", "no-amp-telemetry", "on"]) == 0
    assert radarctl.main(["--socket", server.path, "clear-fault"]) == 0
    assert radarctl.main(["--socket", server.path, "toggle"]) == 0
    assert server.seen == [{"cmd": "start"}, {"cmd": "stop"}, {"cmd": "status"}, {"cmd": "stop", "interrupt": True},
                           {"cmd": "override", "name": "no-amp-telemetry", "set": True},
                           {"cmd": "clear-fault"}, {"cmd": "toggle"}]
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        radarctl.main(["--socket", server.path, "override", "everything", "on"])
    assert e.value.code == 2


def test_radarctl_refused_command_exits_1(tmp_path, capsys):
    srv = control.ControlServer(str(tmp_path / "m.sock"), lambda r: {"ok": False, "message": "not ready: sdr: gone"})
    srv.open()
    try:
        assert radarctl.main(["--socket", srv.path, "start"]) == 1
        assert capsys.readouterr().out == "not ready: sdr: gone\n"
    finally:
        srv.close()


def test_status_text():
    text = radarctl.format_status({
        "state": "fault", "detail": "", "fault": {"kind": "amp_unsafe", "reason": "not verified"},
        "not_ready": ["sdr: SDR not found on USB"], "run_wanted": False, "last_end": "stopped (manual: stop by button)",
        "retries": {"failed_starts": 2, "max_retries": 3, "interruptions": 0, "max_resumes": 5},
        "overrides": {"no_amp_telemetry": True}, "sdr_state": "unknown",
        "sdr_state_why": "radar launched, collection x", "making_amp_safe": True,
        "auto": {"enabled": True, "source": "gps", "state": "moving", "blocked": True},
        "gates": {"telemetry": {"ok": True, "reason": "", "note": "OVERRIDE"},
                  "sdr": {"ok": False, "reason": "SDR not found on USB"}},
        "leds": {"ready_yellow": "fast_blink", "running_green": "off"},
        "config_warnings": ["x.yaml: unknown setting a: ignored"]})
    for expected in ("state:      fault", "fault:      amp_unsafe: not verified", "not ready:  sdr: SDR not found on USB",
                     "last run:   stopped (manual: stop by button)", "2 failed starts (limit 3)",
                     "RUNNING WITHOUT AMPLIFIER TELEMETRY IS ALLOWED", "amplifier pin NOT known to be safe (radar launched, collection x)",
                     "the make-amplifier-safe tool is running",
                     "gps says moving, blocked until stationary", "  telemetry ok  [OVERRIDE]",
                     "  sdr       NOT OK: SDR not found on USB", "READY (yellow) fast_blink, RUNNING (green) off",
                     "config:     x.yaml: unknown setting a: ignored"):
        assert expected in text
    assert radarctl.format_status({})                       # an empty status does not raise
    cleared = radarctl.format_status({"state": "ready", "fault": None, "last_fault": {
        "kind": "amp_unsafe", "reason": "tool run 3 times: status 3 (ASSUME_ENABLED)",
        "cleared_by": "the amplifier pin is known safe again: amp_safe OK"}})
    assert ("last fault: amp_unsafe: tool run 3 times: status 3 (ASSUME_ENABLED) "
            "(cleared by: the amplifier pin is known safe again: amp_safe OK)") in cleared


def test_radarctl_uses_only_the_standard_library():
    import ast
    with open(radarctl.__file__) as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports: it is run as a plain script"
            imported.add(node.module.split(".")[0])
    assert imported <= {"argparse", "json", "os", "socket", "sys", "time"}


# ------------------------------------------------------------------ event log

def test_event_log_appends_one_json_object_per_line(tmp_path):
    path = str(tmp_path / "events.jsonl")
    log = control.EventLog(path, 1e6, wall=lambda: 1795000000.25, uptime=lambda: 42.5)
    assert log.write({"event": "state", "old": "ready", "new": "starting"}) == ""
    assert log.write({"event": "fault", "kind": "amp_unsafe", "reason": "x"}) == ""
    with open(path) as f:
        lines = [json.loads(line) for line in f]
    assert lines[0] == {"time": 1795000000.25, "time_utc": "2026-11-18T11:06:40Z", "uptime_s": 42.5,
                        "event": "state", "old": "ready", "new": "starting"}
    assert lines[1]["kind"] == "amp_unsafe" and log.written == 2
    control.EventLog(path, 1e6).write({"event": "again"})   # a new run appends
    with open(path) as f:
        assert len(f.readlines()) == 3


def test_event_log_is_limited_in_size(tmp_path):
    path = str(tmp_path / "events.jsonl")
    log = control.EventLog(path, 2000)
    for i in range(100):
        log.write({"event": "x", "i": i, "text": "y" * 50})
    assert os.path.getsize(path) <= 2000 and os.path.getsize(path + ".1") <= 2000
    with open(path) as f:
        last = [json.loads(line) for line in f]
    assert last[-1]["i"] == 99
    assert sorted(os.listdir(tmp_path)) == ["events.jsonl", "events.jsonl.1"]


def test_event_log_never_raises(tmp_path):
    log = control.EventLog(str(tmp_path / "no-dir" / "events.jsonl"), 1e6)
    assert "No such file" in log.write({"event": "x"}) and log.error
    ok = control.EventLog(str(tmp_path / "e.jsonl"), 1e6)
    assert ok.write({"event": "x", "odd": object()}) == ""  # anything is written as text


# ----------------------------------------------------------- collection names

def test_collection_names_are_unique_and_ordered_with_a_wrong_clock(tmp_path):
    root, state = tmp_path / "root", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    boot = [3]
    provider = lambda state_dir, floor=0: (boot[0], "")
    names = []
    for wall in (1795000000, 1795000000, 1700000000):       # same second twice, then the clock jumps back
        path, b, seq, problem = coll.create(str(root), str(state), wall=lambda: wall, boot_number=provider)
        names.append(os.path.basename(path))
        assert b == 3 and problem == ""
    assert names == ["b00003_s001_20261118T110640Z", "b00003_s002_20261118T110640Z", "b00003_s003_20231114T221320Z"]
    boot[0] = 4                                             # next boot, clock still wrong
    path, b, seq, _ = coll.create(str(root), str(state), wall=lambda: 946684800, boot_number=provider)
    names.append(os.path.basename(path))
    assert names[-1] == "b00004_s001_20000101T000000Z"
    assert names == sorted(names)                           # the order of recording, whatever the clock said


def test_boot_number_comes_from_the_shared_counter(tmp_path):
    from radar_runner.common import bootcount
    assert coll.boot_count is bootcount.boot_count
    root, state = tmp_path / "root", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    (root / "b00041_s007_20260101T000000Z").mkdir()         # an old drive in a system with a new SD card
    path, boot, seq, problem = coll.create(str(root), str(state))
    assert boot == 42 and seq == 1 and problem == ""        # the floor: one more than the highest name
    assert bootcount.boot_count(str(state)) == (42, "")     # and the telemetry service gets the same number
    path, boot, seq, _ = coll.create(str(root), str(state))
    assert (boot, seq) == (42, 2)


def test_git_dirty_and_binary_hash(tmp_path):
    import hashlib
    import subprocess
    assert coll.git_dirty(str(tmp_path)) == (None, [])      # not a checkout
    run = lambda *args: subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    run("init", "-q")
    (tmp_path / "main.cpp").write_text("int main() {}\n")
    run("add", "main.cpp")
    run("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "x")
    assert coll.git_dirty(str(tmp_path)) == (False, [])
    (tmp_path / "untracked.txt").write_text("notes\n")       # untracked files do not count
    assert coll.git_dirty(str(tmp_path)) == (False, [])
    (tmp_path / "main.cpp").write_text("int main() { return 1; }\n")
    assert coll.git_dirty(str(tmp_path)) == (True, ["main.cpp"])
    assert coll.sha256_file(str(tmp_path / "main.cpp")) == hashlib.sha256(b"int main() { return 1; }\n").hexdigest()
    assert coll.sha256_file(str(tmp_path / "missing")) == ""


def test_metadata_file_is_replaced_atomically(tmp_path):
    meta = coll.Metadata(str(tmp_path), {"name": "x", "flags": ["a"]})
    assert meta.write() == ""
    meta.add_flag("b")
    meta.add_flag("a")
    meta.data["end"] = {"how": "stopped"}
    assert meta.write() == ""
    with open(tmp_path / "collection.json") as f:
        data = json.load(f)
    assert data == {"name": "x", "flags": ["a", "b"], "format": 1, "recording_started": None, "end": {"how": "stopped"}}
    assert os.listdir(tmp_path) == ["collection.json"]      # no temporary file left
    bad = coll.Metadata(str(tmp_path / "gone"), {})
    assert bad.write() != ""                                # reported, not raised


def test_git_commit_is_read_without_running_git(tmp_path):
    assert coll.git_commit(str(tmp_path)) == ""
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("6fd9448aaaabbbbccccddddeeeeffff000011112\n")
    assert coll.git_commit(str(tmp_path)) == "6fd9448aaaabbbbccccddddeeeeffff000011112"
    (git / "HEAD").write_text("0123456789012345678901234567890123456789\n")
    assert coll.git_commit(str(tmp_path)) == "0123456789012345678901234567890123456789"


# --------------------------------------------------------------------- config

def test_manager_section_is_in_the_shared_config_and_its_documented_file():
    assert "manager" in common_config.DEFAULTS
    with open(common_config.REPO_FILE) as f:
        assert yaml.safe_load(f)["manager"] == common_config.DEFAULTS["manager"]


def test_manager_settings_are_layered_like_the_shared_ones(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("manager:\n  limits:\n    max_tx_gain: 52\n    amp_pin: two\n  panel:\n    breathing: blink\n"
                     "  nonsense: 1\n")
    config, warnings = common_config.load(local_file=str(local), secrets_file=None)
    m = config["manager"]
    assert m["limits"]["max_tx_gain"] == 52.0 and m["panel"]["breathing"] == "blink"
    assert m["limits"]["amp_pin"] == 2                      # a value of the wrong type keeps the default
    assert any("manager.limits.amp_pin must be a whole number" in w for w in warnings)
    assert any("unknown setting manager.nonsense" in w for w in warnings)


def test_defaults_that_the_measurements_on_this_hardware_set():
    m = common_config.DEFAULTS["manager"]
    assert m["limits"]["max_tx_gain"] == 43.0               # the conservative gain cap
    # The tool: first contact after power-up took 16.7 s; the manager's limit is above the tool's own.
    assert m["amp_safe"]["command"][0].endswith("tools/sdr_gpio_safe/amp_safe")
    assert m["amp_safe"]["command"][1:] == ["--timeout", "30"] and m["amp_safe"]["timeout_s"] == 40.0
    assert "policy" not in m["amp_safe"]
    # The radar's first start loads firmware and FPGA (14 s) before its own set-up.
    assert m["orca"]["start_timeout_s"] >= 45.0
    assert m["gates"]["sdr"]["bootloader_product"] == "WestBridge" and m["gates"]["sdr"]["min_speed_mbps"] == 5000
    assert m["gates"]["watcher"] == {"status_file": "-", "max_age_s": 5.0, "require_true": ["ok"],
                                     "require_false": ["tripped"], "reason_field": "reason"}
    assert m["gates"]["telemetry"]["require_true"] == ["amp.fresh"]
    assert "log_max_mb" not in m["data"]                    # the radar's output is never cut


# --------------------------------------------------- slow and broken clients

def test_a_silent_client_does_not_hold_up_another(server):
    import time
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.connect(server.path)                             # connects and sends nothing
    half = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    half.connect(server.path)
    half.sendall(b'{"cmd": "sta')                           # ... or half a request
    try:
        t0 = time.monotonic()
        assert radarctl.request(server.path, {"cmd": "stop"})["ok"]
        assert time.monotonic() - t0 < 0.5                  # answered at once, not after their timeouts
    finally:
        silent.close()
        half.close()


def test_a_slow_client_is_dropped_after_its_time(server, monkeypatch):
    import time
    monkeypatch.setattr(control, "CLIENT_TIMEOUT_S", 0.3)
    slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    slow.settimeout(5)
    slow.connect(server.path)
    t0 = time.monotonic()
    for _ in range(20):                                     # a byte now and then, never a whole request
        try:
            slow.sendall(b" ")
        except OSError:
            break
        time.sleep(0.05)
    data = slow.recv(4096)
    assert time.monotonic() - t0 < 3.0
    assert data == b"" or json.loads(data) == {"ok": False, "message": "request not understood"}
    slow.close()
    assert server.seen == []


def test_more_clients_than_slots_are_turned_away_not_queued(server, monkeypatch):
    held = []
    for _ in range(control.MAX_CLIENTS):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(server.path)
        held.append(c)
    import time
    time.sleep(0.3)                                         # all slots are taken by silent clients
    with pytest.raises(radarctl.NotRunning):
        radarctl.request(server.path, {"cmd": "status"}, timeout_s=1.0)
    for c in held:
        c.close()
    time.sleep(0.3)
    assert radarctl.request(server.path, {"cmd": "status"})["ok"]


def test_radarctl_stop_wait_respects_its_timeout(tmp_path, capsys):
    import time
    srv = control.ControlServer(str(tmp_path / "m.sock"), lambda r: {
        "ok": True, "message": "stopping", "status": {"state": "stopping", "radar_gone": False, "runs_ended": 3}})
    srv.open()
    try:
        t0 = time.monotonic()
        assert radarctl.main(["--socket", srv.path, "stop", "--interrupt", "--wait", "--timeout", "0.6"]) == 1
        assert 0.5 <= time.monotonic() - t0 < 1.2
        assert "has not stopped after 0.6 s" in capsys.readouterr().err
    finally:
        srv.close()


def test_radarctl_stop_wait_sees_a_stop_that_was_followed_by_a_restart(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        ended = 3 if len(calls) == 1 else 4                 # the run ended and a new one has begun
        return {"ok": True, "message": "stopping", "status": {"radar_gone": False, "runs_ended": ended}}
    srv = control.ControlServer(str(tmp_path / "m.sock"), handler)
    srv.open()
    try:
        assert radarctl.main(["--socket", srv.path, "stop", "--wait", "--timeout", "5"]) == 0
    finally:
        srv.close()
