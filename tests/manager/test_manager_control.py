"""The command socket, the event log, collection names and the manager's settings."""
import json
import os
import socket
import stat

import pytest
import yaml

from radar_runner.common import config as common_config
from radar_runner.manager import collection as coll
from radar_runner.manager import config as manager_config
from radar_runner.manager import control, radarctl


# -------------------------------------------------------------------- socket

@pytest.fixture
def server(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return {"ok": request.get("cmd") != "bad", "message": f"did {request.get('cmd')}",
                "status": {"state": "ready", "idle": True, "runs_ended": 0}}
    srv = control.ControlServer(str(tmp_path / "manager.sock"), handler)
    srv.open()
    srv.seen = seen
    yield srv
    srv.close()


def test_request_and_reply(server):
    reply = radarctl.request(server.path, {"cmd": "start"})
    assert reply == {"ok": True, "message": "did start", "status": {"state": "ready", "idle": True, "runs_ended": 0}}
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
    assert capsys.readouterr().out == "did stop\nstopped\n"
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
        "overrides": {"no_amp_telemetry": True}, "sdr_state_unknown": True,
        "auto": {"enabled": True, "source": "gps", "state": "moving", "blocked": True},
        "gates": {"telemetry": {"ok": True, "reason": "", "note": "OVERRIDE"},
                  "sdr": {"ok": False, "reason": "SDR not found on USB"}},
        "leds": {"ready_yellow": "fast_blink", "running_green": "off"},
        "config_warnings": ["x.yaml: unknown setting a: ignored"]})
    for expected in ("state:      fault", "fault:      amp_unsafe: not verified", "not ready:  sdr: SDR not found on USB",
                     "last run:   stopped (manual: stop by button)", "2 failed starts (limit 3)",
                     "RUNNING WITHOUT AMPLIFIER TELEMETRY IS ALLOWED", "GPIO state not known to be safe",
                     "gps says moving, blocked until stationary", "  telemetry ok  [OVERRIDE]",
                     "  sdr       NOT OK: SDR not found on USB", "READY (yellow) fast_blink, RUNNING (green) off",
                     "config:     x.yaml: unknown setting a: ignored"):
        assert expected in text
    assert radarctl.format_status({})                       # an empty status does not raise


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


def test_placeholder_boot_counter_counts_boots_not_starts(tmp_path):
    state = str(tmp_path)
    assert coll._placeholder_boot_count(state, boot_id="boot-a") == (1, "")
    assert coll._placeholder_boot_count(state, boot_id="boot-a") == (1, "")     # same boot: same number
    assert coll._placeholder_boot_count(state, boot_id="boot-b") == (2, "")
    assert coll._placeholder_boot_count(state, floor=41, boot_id="boot-c") == (42, "")
    with open(os.path.join(state, "boot_count.json")) as f:
        assert json.load(f) == {"count": 42, "boot_id": "boot-c"}               # the shared helper's format
    count, problem = coll._placeholder_boot_count(os.path.join(state, "missing"), boot_id="x")
    assert count == 1 and "cannot keep the boot counter" in problem


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

HERE = os.path.dirname(os.path.abspath(__file__))
SECTION_FILE = os.path.join(common_config.REPO, "docs", "merge", "manager-config-section.yaml")


def test_config_section_for_the_merge_matches_the_built_in_defaults():
    # docs/merge/manager-config-section.yaml is what goes into config/radar-runner.yaml.
    with open(SECTION_FILE) as f:
        assert yaml.safe_load(f) == {"manager": manager_config.MANAGER_DEFAULTS}


def test_manager_settings_are_layered_like_the_shared_ones(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("manager:\n  limits:\n    max_tx_gain: 52\n    amp_pin: two\n  panel:\n    breathing: blink\n"
                     "  amp_safe:\n    policy: when_needed\n  nonsense: 1\n")
    config, warnings = manager_config.load(local_file=str(local), secrets_file=None)
    m = config["manager"]
    assert m["limits"]["max_tx_gain"] == 52.0 and m["panel"]["breathing"] == "blink"
    assert m["amp_safe"]["policy"] == "when_needed"
    assert m["limits"]["amp_pin"] == 2                      # a value of the wrong type keeps the default
    assert any("manager.limits.amp_pin must be a whole number" in w for w in warnings)
    assert any("unknown setting manager.nonsense" in w for w in warnings)
    assert config["paths"]["run_dir"] == "/run/radar-runner/user"


def test_defaults_do_not_share_state():
    a, b = manager_config.defaults(), manager_config.defaults()
    a["manager"]["limits"]["max_tx_gain"] = 99.0
    assert b["manager"]["limits"]["max_tx_gain"] == 43.0
    assert manager_config.MANAGER_DEFAULTS["limits"]["max_tx_gain"] == 43.0
