"""Status files: atomic writing, and reading with an age (radar_runner/common/status.py)."""
import json
import os
import threading

import pytest

from power import service as power_service
from power.guard import Guard
from radar_runner.common import config as C
from radar_runner.common import status as S
from tests.power.helpers import FakeActuator, FakeClock, ListLog, make_config

POWER_STATUS = S.POWER_STATUS_FILE


class Clock:
    """A wall clock and a boot-time clock that the test moves by hand."""

    def __init__(self, wall=1_800_000_000.0, up=500.0):
        self.wall_s, self.up_s = wall, up

    def wall(self):
        return self.wall_s

    def uptime(self):
        return self.up_s

    def advance(self, s):
        self.wall_s += s
        self.up_s += s


def writer(path, clock, boot_id="boot-a"):
    return S.StatusWriter(str(path), wall=clock.wall, uptime=clock.uptime, boot_id=boot_id)


def read(path, clock, max_age_s=5.0, boot_id="boot-a"):
    return S.read_status(str(path), max_age_s, wall=clock.wall, uptime=clock.uptime, boot_id=boot_id)


def test_status_path():
    assert S.status_path("telemetry") == "/run/radar-runner/user/telemetry.json"
    assert S.POWER_STATUS_FILE == "/run/radar-runner/power.json"      # root's; not under "user"
    assert C.DEFAULTS["paths"]["run_dir"] == S.DEFAULT_RUN_DIR
    assert C.DEFAULTS["paths"]["power_status_file"] == S.POWER_STATUS_FILE
    assert S.status_path("power", "/tmp/x") == "/tmp/x/power.json"


def test_write_then_read(tmp_path):
    clock, path = Clock(), tmp_path / "a.json"
    assert writer(path, clock).write({"state": "ok", "value": 1.5}) == ""
    r = read(path, clock)
    assert r.ok and r.state == S.OK and r.age_s == 0.0 and r.error == ""
    assert r.data["state"] == "ok" and r.data["value"] == 1.5
    assert r.data["time"] == clock.wall_s and r.data["uptime_s"] == clock.up_s
    assert r.data["time_utc"] == "2027-01-15T08:00:00Z"
    assert r.data["boot_id"] == "boot-a" and r.data["pid"] == os.getpid()
    assert path.read_text().endswith("}\n")
    assert os.listdir(tmp_path) == ["a.json"]            # no temporary file left behind


def test_writer_fields_override_the_service(tmp_path):
    clock, path = Clock(), tmp_path / "a.json"
    writer(path, clock).write({"time": 1, "uptime_s": 2, "pid": 3, "boot_id": "x"})
    data = json.loads(path.read_text())
    assert (data["time"], data["uptime_s"], data["pid"], data["boot_id"]) == (
        clock.wall_s, clock.up_s, os.getpid(), "boot-a")


def test_age_and_stale(tmp_path):
    clock, path = Clock(), tmp_path / "a.json"
    writer(path, clock).write({"n": 1})
    clock.advance(4.9)
    r = read(path, clock)
    assert r.ok and r.age_s == pytest.approx(4.9)
    clock.advance(0.2)
    r = read(path, clock)
    assert r.state == S.STALE and not r.ok and r.age_s == pytest.approx(5.1)
    assert r.data == json.loads(path.read_text())        # still returned, for display
    assert "5.1 s old" in r.error
    assert read(path, clock, max_age_s=10).ok            # the caller sets the limit


def test_a_clock_step_does_not_change_the_age(tmp_path):
    # The wall clock is set from GPS or the network while services run.
    clock, path = Clock(wall=1000.0), tmp_path / "a.json"
    writer(path, clock).write({})
    clock.wall_s = 1_800_000_000.0       # stepped forward by decades
    clock.up_s += 1.0
    r = read(path, clock)
    assert r.ok and r.age_s == pytest.approx(1.0)
    clock.wall_s = 5.0                   # and backward
    assert read(path, clock).ok


def test_file_from_another_boot_is_stale(tmp_path):
    clock, path = Clock(), tmp_path / "a.json"
    writer(path, clock, boot_id="boot-a").write({})
    r = read(path, clock, boot_id="boot-b")              # same uptime, by coincidence
    assert r.state == S.STALE and r.age_s is None and "another boot" in r.error


def test_timestamp_in_the_future_is_stale(tmp_path):
    clock, path = Clock(), tmp_path / "a.json"
    writer(path, clock).write({})
    clock.up_s -= 0.5                                    # within rounding: fine, age 0
    r = read(path, clock)
    assert r.ok and r.age_s == 0.0
    clock.up_s -= 10
    r = read(path, clock)
    assert r.state == S.STALE and "future" in r.error


def test_wall_time_is_used_without_uptime(tmp_path):
    clock, path = Clock(), tmp_path / "a.json"
    path.write_text(json.dumps({"time": clock.wall_s - 3}))
    r = read(path, clock)
    assert r.ok and r.age_s == pytest.approx(3.0)
    assert read(path, clock, max_age_s=2).state == S.STALE


def test_missing(tmp_path):
    r = read(tmp_path / "none.json", Clock())
    assert r.state == S.MISSING and not r.ok and r.data is None and r.age_s is None
    assert read(tmp_path / "no-dir" / "x.json", Clock()).state == S.MISSING


@pytest.mark.parametrize("content", [
    b"", b"{", b'{"time": 1', b"[1, 2]", b'"text"', b"null", b"\xff\xfe", b"{'a': 1}",
    b'{"state": "ok"}',                      # no timestamp
    b'{"time": "yesterday"}', b'{"time": null}', b'{"uptime_s": true}', b'{"time": NaN}',
    b'{"time": Infinity}',
    b"[" * 100000,                           # nesting too deep to parse
])
def test_corrupt(tmp_path, content):
    path = tmp_path / "a.json"
    path.write_bytes(content)
    r = read(path, Clock())
    assert r.state == S.CORRUPT and not r.ok and r.error


def test_too_large_is_corrupt(tmp_path):
    path = tmp_path / "a.json"
    path.write_bytes(b'{"time": 1, "pad": "' + b"x" * S.MAX_BYTES + b'"}')
    assert read(path, Clock()).state == S.CORRUPT


def test_unreadable_is_corrupt(tmp_path):
    assert read(tmp_path, Clock()).state == S.CORRUPT    # a directory
    if os.geteuid() != 0:
        path = tmp_path / "a.json"
        path.write_text("{}")
        path.chmod(0)
        r = read(path, Clock())
        assert r.state == S.CORRUPT and "cannot read" in r.error


def test_temporary_file_is_new_each_time_and_exclusive(tmp_path, monkeypatch):
    clock, path = Clock(), tmp_path / "a.json"
    w = writer(path, clock)
    used = []
    real_replace = os.replace
    monkeypatch.setattr(S.os, "replace", lambda src, dst: (used.append(src), real_replace(src, dst))[1])
    # Files, even a symlink, at the old fixed name must not matter or be followed.
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    os.symlink(victim, str(path) + ".tmp")
    assert w.write({"n": 1}) == "" and w.write({"n": 2}) == ""
    assert victim.read_text() == "untouched"
    assert len(set(used)) == 2 and all(os.path.dirname(u) == str(tmp_path) for u in used)
    assert all(os.path.basename(u).startswith(".a.json.") and u.endswith(".tmp") for u in used)
    assert path.stat().st_mode & 0o777 == 0o644
    assert read(path, clock).data["n"] == 2


def test_short_writes_are_completed(tmp_path, monkeypatch):
    clock, path = Clock(), tmp_path / "a.json"
    real_write = os.write
    monkeypatch.setattr(S.os, "write", lambda fd, data: real_write(fd, bytes(data[:7])))
    assert writer(path, clock).write({"blob": "x" * 5000}) == ""
    monkeypatch.undo()
    assert read(path, clock).data["blob"] == "x" * 5000


def test_temporary_file_is_removed_when_a_write_fails(tmp_path, monkeypatch):
    clock = Clock()
    (tmp_path / "is-a-dir.json").mkdir()
    w = writer(tmp_path / "is-a-dir.json", clock)          # the rename cannot succeed
    assert w.write({"n": 1}) != ""
    assert os.listdir(tmp_path) == ["is-a-dir.json"]
    w = writer(tmp_path / "a.json", clock)

    def disk_full(fd, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(S.os, "write", disk_full)
    assert "No space left" in w.write({"n": 1})
    monkeypatch.undo()
    assert os.listdir(tmp_path) == ["is-a-dir.json"]


def test_leftovers_of_a_killed_writer_are_removed_at_start(tmp_path):
    mine = tmp_path / ".a.json.abc123.tmp"
    other = tmp_path / ".b.json.abc123.tmp"
    mine.write_text("{")
    other.write_text("{")
    writer(tmp_path / "a.json", Clock())
    assert not mine.exists() and other.exists()            # only this service's own


def test_write_failure_is_reported_not_raised(tmp_path):
    clock = Clock()
    w = writer(tmp_path / "no-dir" / "a.json", clock)
    assert "FileNotFoundError" in w.write({}) and w.error
    w = writer(tmp_path / "a.json", clock)
    assert "ValueError" in w.write({"x": float("nan")})          # not valid JSON
    assert "TypeError" in w.write({"x": object()})
    assert not (tmp_path / "a.json").exists()
    assert w.write({"x": 1}) == "" and w.error == ""
    assert "TypeError" in w.write(None)
    assert read(tmp_path / "a.json", clock).data["x"] == 1       # the last good file stays


def test_reader_never_sees_a_partial_file(tmp_path):
    path = tmp_path / "a.json"
    w = S.StatusWriter(str(path))
    w.write({"blob": "x" * 20000})
    stop = threading.Event()

    def write_loop():
        while not stop.is_set():
            w.write({"blob": "x" * 20000})

    thread = threading.Thread(target=write_loop)
    thread.start()
    try:
        results = [S.read_status(str(path), 60.0) for _ in range(2000)]
    finally:
        stop.set()
        thread.join()
    assert {r.state for r in results} == {S.OK}
    assert all(len(r.data["blob"]) == 20000 for r in results)


def test_real_clocks(tmp_path):
    path = str(tmp_path / "a.json")
    assert S.StatusWriter(path).write({"state": "ok"}) == ""
    r = S.read_status(path, 5.0)
    assert r.ok and 0 <= r.age_s < 2 and len(r.data["boot_id"]) == 36


# ---- the power service's file (format: power/service.py, Service.publish)

def power_service_file(tmp_path, clock):
    """Have the real power service code, on fake hardware, write its status file."""
    cfg = make_config(status_file=str(tmp_path / "run" / "power.json"),
                      state_dir=str(tmp_path / "state"),
                      disable_file=str(tmp_path / "power-disarm"))
    service = power_service.Service(cfg, lambda: (0.02, 5.19), lambda: 1, FakeActuator(),
                                    Guard(cfg.state_dir), clock=clock, log=ListLog())
    for _ in range(10):                      # 2 s: long enough to confirm "present"
        service.tick()
        clock.sleep(0.2)
    return cfg.status_file


def test_reads_the_power_service_file(tmp_path):
    clock = FakeClock(uptime0=1000.0)
    path = power_service_file(tmp_path, clock)
    uptime_now = [clock.uptime()]
    r = S.read_status(path, 5.0, wall=clock.wall, uptime=lambda: uptime_now[0])
    assert r.ok and r.age_s <= 0.5
    assert r.data["version"] == 1 and r.data["state"] == "normal"
    assert r.data["power_present"] is True and r.data["radar_stop_requested"] is False
    assert set(r.data["signals"]) == {"current", "gpio"}
    uptime_now[0] += 6.0                     # the power service stops writing
    r = S.read_status(path, 5.0, wall=clock.wall, uptime=lambda: uptime_now[0])
    assert r.state == S.STALE and r.data["state"] == "normal"


@pytest.mark.skipif(not os.path.exists(POWER_STATUS), reason="the power service is not running here")
def test_reads_the_live_power_service_file():
    r = S.read_status(POWER_STATUS, 5.0)
    assert r.state in (S.OK, S.STALE), r.error      # stale if the service is stopped
    assert r.age_s is not None and r.age_s >= 0
    for key in ("state", "mode", "power_present", "radar_stop_requested", "signals", "uptime_s"):
        assert key in r.data
