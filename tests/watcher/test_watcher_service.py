"""The safety watcher service (radar_runner/watcher/service.py): with real files, a stand-in reset tool and
stand-in radar processes, first in simulated time, then as a real process.

No test runs the real amp_safe or sdr_gpio_safe, opens a device, or signals a process it did not start.
"""
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest
from watcher_fakes import ENABLED_A, GOOD, FakeRadar, FakeTool, telemetry_status

from radar_runner.common import config as C
from radar_runner.common import status as S
from radar_runner.watcher import service as W
from radar_runner.watcher.service import EventLog, RadarProcesses, Watcher

REPO = C.REPO
WALL0 = 1795046400.0
UP0 = 500.0
BOOT = "11111111-2222-3333-4444-555555555555"


class Notifier:
    ping_interval = 1.0

    def ready(self):
        pass

    def stopping(self):
        pass

    def watchdog(self):
        pass


class Rig:
    """A watcher on simulated clocks, with real files under tmp_path, a stand-in tool and stand-in radars."""

    def __init__(self, tmp_path, **changes):
        self.tmp = tmp_path
        for name in ("run", "state"):
            (tmp_path / name).mkdir()
        self.up = UP0
        self.tool = FakeTool(tmp_path / "tool")
        self.radars = FakeRadar(tmp_path / "orca")
        self.config = copy.deepcopy(C.DEFAULTS)
        self.config["paths"].update(run_dir=str(tmp_path / "run"), state_dir=str(tmp_path / "state"),
                                    power_status_file=str(tmp_path / "power.json"))
        w = self.config["watcher"]
        w["radar_exe"] = str(self.radars.exe)
        w["reset"]["command"] = str(self.tool.path)
        for key, value in changes.items():
            (w["reset"] if key in w["reset"] else w)[key] = value
        self.amp = dict(GOOD)
        self.feed = True            # the telemetry service is writing its status file
        self.follow_tool = True     # the amplifier goes back to shutdown when the stand-in tool reports OK
        self.telemetry = S.StatusWriter(str(tmp_path / "run" / "telemetry.json"), wall=self.wall,
                                        uptime=lambda: self.up, boot_id=S.current_boot_id())
        self.watcher = Watcher(self.config, notifier=Notifier(), wall=self.wall, uptime=lambda: self.up,
                               monotonic=lambda: self.up,
                               events=EventLog(str(tmp_path / "state" / "watcher-events.jsonl"), 5e6, wall=self.wall,
                                               uptime=lambda: self.up, threaded=False),
                               status=S.StatusWriter(str(tmp_path / "run" / "watcher.json"), wall=self.wall,
                                                     uptime=lambda: self.up, boot_id=S.current_boot_id()))
        self.watcher.setup()
        self._rounds = 0

    def wall(self):
        return WALL0 + self.up

    def round(self, dt=0.25):
        self.up += dt
        self._rounds += 1
        if self.follow_tool and self.tool.succeeded:
            self.amp["current_a"] = GOOD["current_a"]
        if self.feed and self._rounds % 2 == 1:
            self.telemetry.write(telemetry_status(self.up, self.amp))
        tool = self.watcher._tool
        if tool is not None and self.tool_ends:        # let the stand-in tool finish (real time) before the round
            tool["proc"].wait(20)
        data = self.watcher.tick()
        if data is not None:
            self.watcher.publish(data)
        return data

    tool_ends = True

    def run(self, seconds):
        data = None
        for _ in range(round(seconds / 0.25)):
            data = self.round()
        return data

    def status(self, max_age=5.0):
        return S.read_status(str(self.tmp / "run" / "watcher.json"), max_age, wall=self.wall, uptime=lambda: self.up)

    @property
    def events(self):
        path = self.tmp / "state" / "watcher-events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def named(self, name):
        return [e for e in self.events if e["event"] == name]

    def close(self):
        self.radars.stop_all()
        tool = self.watcher._tool
        if tool is not None and tool["proc"].poll() is None:
            tool["proc"].kill()         # a stand-in started by this test
            tool["proc"].wait(10)


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(**changes):
        made.append(Rig(tmp_path, **changes))
        return made[-1]
    yield make
    for r in made:
        r.close()


def gone(proc, timeout=10.0):
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        return None


# ---- inputs and the status file

def test_good_telemetry_gives_an_ok_status_with_the_shared_fields(rig):
    r = rig()
    assert r.run(1.0) is None and r.status().state == S.MISSING        # nothing is published before it can judge
    r.run(1.0)
    read = r.status()
    assert read.ok and read.data["ok"] is True and read.data["tripped"] is False and read.data["reason"] == ""
    d = read.data
    assert d["running"] is True and d["pid"] == os.getpid() and d["uptime_s"] == r.up
    assert d["amp"] == dict(GOOD, sample_uptime_s=pytest.approx(r.up - 0.5, abs=0.3))
    assert d["conditions"]["telemetry"]["state"] == "ok" and d["pi"]["soc_temp_c"] == 46.0
    assert d["override"] == {"no_amp_telemetry": False, "file": str(r.tmp / "state" / "override-no-amp-telemetry")}
    assert d["reset_tool"] == {"running": False, "last": None, "command": str(r.tool.path),
                               "state_file": str(r.tmp / "state" / "sdr-state"), "runnable": True}
    assert d["events"]["file"].endswith("watcher-events.jsonl") and d["events"]["error"] == ""
    assert d["radar"]["exe"] == str(r.radars.exe) and d["radar"]["running"] is False
    assert [e["event"] for e in r.events] == ["start"]


def test_the_status_is_rewritten_every_round_and_goes_stale_when_the_loop_stalls(rig):
    r = rig()
    r.run(3.0)
    first = r.status().data["uptime_s"]
    r.round()
    assert r.status().data["uptime_s"] == first + 0.25
    # The loop hangs: nothing rewrites the file, and a reader's own age limit catches it.
    r.up += 5.5
    read = r.status(max_age=5.0)
    assert read.state == S.STALE and not read.ok and read.data["ok"] is True     # the content must not be acted on
    # An error inside a round: nothing is published for that round either.
    r.watcher.radar.pids = lambda: 1 / 0
    with pytest.raises(ZeroDivisionError):
        r.watcher.tick()
    assert r.status().state == S.STALE


def test_a_missing_telemetry_file_is_not_ok_and_says_why(rig):
    r = rig()
    r.feed = False
    r.run(4.0)
    d = r.status().data
    assert d["ok"] is False and d["tripped"] is False
    assert d["reason"].endswith("telemetry status file missing: no such file")
    assert d["conditions"]["telemetry"]["state"] == "lost" and r.tool.calls == []


@pytest.mark.parametrize("content", [b"", b'{"state": "ok", "amp": {"fresh": tr', b"\x00\xff garbage", b"[1, 2, 3]",
                                     b'{"amp": {"fresh": true}}'])
def test_a_damaged_or_half_written_telemetry_file_is_treated_as_no_telemetry(rig, content):
    r = rig()
    r.run(3.0)
    assert r.status().data["ok"] is True
    r.feed = False
    path = r.tmp / "run" / "telemetry.json"
    path.write_bytes(content)
    r.run(2.5)
    assert r.status().data["ok"] is True            # not yet 3 s: one bad read changes nothing
    r.run(1.0)
    d = r.status().data
    assert d["ok"] is False and d["tripped"] is False and "telemetry status file corrupt" in d["reason"]
    r.feed = True
    r.run(2.0)
    assert r.status().data["ok"] is True
    assert [e["event"] for e in r.events] == ["start", "not_ok", "ok"]


def test_a_telemetry_file_left_from_another_boot_is_not_used(rig):
    r = rig()
    r.feed = False
    S.StatusWriter(str(r.tmp / "run" / "telemetry.json"), wall=r.wall, uptime=lambda: r.up + 4.0,
                   boot_id=BOOT).write(telemetry_status(r.up + 4.0))
    r.run(4.0)
    assert r.status().data["ok"] is False and "written during another boot" in r.status().data["reason"]


def test_telemetry_that_is_being_set_up_is_reported_with_the_telemetry_services_reason(rig):
    r = rig()
    r.feed = False
    r.telemetry.write(telemetry_status(r.up, fresh=False, reason="the OLA is not set up for the amplifier readings (configuring)"))
    for _ in range(48):                             # 12 s, the file fresh but its readings not
        r.up += 0.25
        r.telemetry.write(telemetry_status(r.up, fresh=False,
                                           reason="the OLA is not set up for the amplifier readings (configuring)"))
        data = r.watcher.tick()
        if data:
            r.watcher.publish(data)
    d = r.status().data
    assert d["ok"] is False and d["tripped"] is False and r.tool.calls == []
    assert "amplifier readings not fresh: the OLA is not set up for the amplifier readings (configuring)" in d["reason"]


def test_the_override_file_makes_missing_telemetry_ok_and_shows_in_the_status(rig):
    r = rig()
    r.feed = False
    (r.tmp / "state" / "override-no-amp-telemetry").write_text("set by the user\n")
    r.run(4.0)
    d = r.status().data
    assert d["ok"] is True and d["override"]["no_amp_telemetry"] is True
    assert d["conditions"]["telemetry"]["state"] == "lost" and d["conditions"]["telemetry"]["overridden"] is True
    assert d["warnings"][0]["warning"] == "override"
    (r.tmp / "state" / "override-no-amp-telemetry").unlink()
    r.round()
    assert r.status().data["ok"] is False


def test_a_reported_power_loss_makes_the_falling_rail_no_trip(rig):
    r = rig()
    r.run(3.0)
    power = S.StatusWriter(str(r.tmp / "power.json"), wall=r.wall, uptime=lambda: r.up, boot_id="")
    for volts in (24.0, 22.0, 21.0, 9.0, 0.3, 0.3):
        r.amp["voltage_v"] = volts
        for _ in range(4):
            power.write({"state": "loss", "power_present": False})
            r.round()
    d = r.status().data
    assert d["ok"] is False and d["tripped"] is False and d["conditions"]["voltage"]["state"] == "unpowered"
    assert r.named("trip") == [] and r.tool.calls == []


def test_an_over_temperature_trip_reaches_the_status_and_the_event_log(rig):
    r = rig()
    r.run(3.0)
    r.amp["temp_c"] = 57.5
    r.run(2.0)
    d = r.status().data
    assert d["ok"] is False and d["tripped"] is True and "reached 55 C (now 57.5 C)" in d["reason"]
    assert d["conditions"]["amp_temp"]["state"] == "trip" and d["counters"]["trips"] == 1
    trip, = r.named("trip")
    assert trip["severity"] == "urgent" and trip["condition"] == "amp_temp" and trip["temp_c"] == 57.5
    assert trip["uptime_s"] == pytest.approx(r.up - 0.75, abs=0.3) and trip["time_utc"].endswith("Z")
    r.amp["temp_c"] = 40.0
    r.run(32.0)
    assert r.status().data["ok"] is True and [e["event"] for e in r.events] == ["start", "trip", "not_ok", "warning",
                                                                               "clear", "ok", "warning_cleared"]


# ---- the reset tool, through a stand-in

def test_enabled_with_no_radar_runs_the_tool_with_the_timeout_and_the_shared_state_file(rig):
    r = rig()
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(5.0)
    assert r.tool.calls == []
    r.run(4.0)                                      # the tool runs, reports OK, the current falls, three readings
    call, = r.tool.calls
    assert call["argv"] == ["--timeout", "30", "--state-file", str(r.tmp / "state" / "sdr-state")]
    d = r.status().data
    assert d["ok"] is True and d["conditions"]["amp_enabled"]["state"] == "idle"
    last = d["reset_tool"]["last"]
    assert (last["code"], last["name"]) == (0, "OK") and "amp_safe: OK (exit 0" in last["text"]
    assert last["time_utc"].endswith("Z") and d["counters"]["tool_runs"] == 1 and d["counters"]["tool_ok"] == 1
    assert [e["event"] for e in r.events] == ["start", "trip", "reset_tool", "not_ok", "reset_tool_result", "clear", "ok"]
    r.run(120.0)
    assert len(r.tool.calls) == 1


@pytest.mark.parametrize("code, name", [(3, "ASSUME_ENABLED"), (2, "NOT_FOUND"), (4, "TIMEOUT"), (5, "FAILED"),
                                        (6, "BUSY"), (7, "AMBIGUOUS"), (8, "STATE_FILE"), (64, "USAGE")])
def test_every_failing_exit_code_is_retried_twice_then_stuck_with_one_run_a_minute(rig, code, name):
    r = rig()
    r.tool.plan(codes=[code])
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(25.0)
    d = r.status().data
    assert len(r.tool.calls) == 3
    assert d["ok"] is False and d["tripped"] is True and d["conditions"]["amp_enabled"]["state"] == "stuck"
    assert d["reason"].startswith("AMPLIFIER STUCK ENABLED") and f"last: {name} (exit {code})" in d["reason"]
    assert (d["reset_tool"]["last"]["code"], d["reset_tool"]["last"]["name"]) == (code, name)
    gaps = [b["uptime_s"] - a["uptime_s"] for a, b in zip(r.named("reset_tool"), r.named("reset_tool")[1:])]
    assert gaps == pytest.approx([2.25 if code == 3 else 5.25] * 2, abs=0.3)
    stuck, = r.named("stuck")
    assert stuck["severity"] == "urgent" and stuck["current_a"] == ENABLED_A
    r.run(125.0)
    assert len(r.tool.calls) == 5                   # one a minute
    r.tool.plan(codes=[0])                          # the next run succeeds and the current falls
    r.run(62.0)
    assert len(r.tool.calls) == 6 and r.status().data["ok"] is True
    assert "no longer stuck enabled" in r.named("clear")[0]["text"]


def test_a_tool_that_ends_with_no_valid_result_counts_as_assume_enabled(rig):
    r = rig()
    r.tool.plan(codes=[1])                          # a traceback in amp_safe, for example
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(7.0)
    last = r.status().data["reset_tool"]["last"]
    assert (last["code"], last["name"]) == (3, "ASSUME_ENABLED") and "ended with status 1, which is no result" in last["text"]


def test_the_tool_runs_even_if_its_output_file_cannot_be_made(rig):
    r = rig()
    r.watcher.tool_output_file = str(r.tmp / "no-such-directory" / "tool.out")
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(9.0)
    assert len(r.tool.calls) == 1 and r.status().data["ok"] is True
    last = r.status().data["reset_tool"]["last"]
    assert (last["code"], last["text"]) == (0, "no output")


def test_a_tool_that_cannot_be_started_is_reported_as_failed_and_the_state_becomes_stuck(rig):
    r = rig(command="/nonexistent/amp_safe")
    r.run(3.0)
    assert r.status().data["reset_tool"]["runnable"] is False
    r.amp["current_a"] = ENABLED_A
    r.run(25.0)
    d = r.status().data
    assert d["conditions"]["amp_enabled"]["state"] == "stuck" and d["reset_tool"]["last"]["name"] == "FAILED"
    assert "cannot start /nonexistent/amp_safe: No such file or directory" in d["reset_tool"]["last"]["text"]


def test_a_tool_that_does_not_return_is_given_up_on_after_its_timeout_plus_the_margin(rig):
    r = rig(timeout_s=3.0, kill_margin_s=1.0)
    r.tool.plan(codes=[0], sleep=120.0)
    r.tool_ends = False
    r.follow_tool = False
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(6.0)
    proc, started = r.watcher._tool["proc"], r.watcher._tool["started"]
    r.run(started + 3.75 - r.up)
    assert r.status().data["reset_tool"]["running"] is True and proc.poll() is None        # still waited for
    r.run(0.5)                                      # 4 s after its start: no longer
    last = r.status().data["reset_tool"]["last"]
    assert last["name"] == "ASSUME_ENABLED" and "did not return within 4 s and was killed" in last["text"]
    assert gone(proc) == -signal.SIGKILL


def test_the_tool_never_runs_while_a_stand_in_radar_is_alive(rig):
    r = rig()
    radar = r.radars.start()
    r.run(3.0)
    assert r.status().data["radar"] == {"running": True, "pids": [radar.pid], "stop": "idle",
                                        "stop_since_uptime_s": None, "exe": str(r.radars.exe)}
    r.amp["current_a"] = ENABLED_A                  # a radar that transmits: enabled current is normal
    r.run(120.0)
    assert r.tool.calls == [] and r.status().data["ok"] is True and radar.poll() is None
    # Over temperature with the radar still there: the status is not ok, and the tool still must not run.
    r.amp["temp_c"] = 58.0
    r.run(6.0)
    assert r.tool.calls == [] and radar.poll() is None
    r.run(1.0)                                      # the grace period is over: the watcher stops the radar itself
    assert gone(radar) == -signal.SIGINT
    r.run(4.5)
    assert r.tool.calls == []                       # the radar left the amplifier enabled: 5 s first
    r.run(2.0)
    assert len(r.tool.calls) == 1
    kinds = [e["event"] for e in r.events]
    assert kinds.index("radar_stop") < kinds.index("radar_stopped") < kinds.index("reset_tool")


# ---- stopping a radar directly

def test_a_stand_in_radar_that_ignores_sigint_is_killed_after_the_allowed_exit_time(rig):
    r = rig()
    stubborn = r.radars.start(ignore_sigint=True)
    polite = r.radars.start()
    bystander_exe = r.tmp / "orca" / "not-the-radar"
    shutil.copy(shutil.which("sleep"), bystander_exe)
    bystander = subprocess.Popen([str(bystander_exe), "300"])
    try:
        r.run(3.0)
        r.feed = False                              # telemetry is lost; nothing else stops the radars
        r.run(7.5)
        assert stubborn.poll() is None and polite.poll() is None and r.named("radar_stop") == []
        r.run(1.0)                                  # 3 s to "lost" plus 5 s of grace: SIGINT to both
        assert gone(polite) == -signal.SIGINT and stubborn.poll() is None
        d = r.status().data
        assert d["radar"]["stop"] == "interrupted" and d["radar"]["pids"] == [stubborn.pid]
        r.run(4.0)
        assert stubborn.poll() is None
        r.run(1.5)                                  # 5 s after the SIGINT: SIGKILL
        assert gone(stubborn) == -signal.SIGKILL
        r.run(0.5)
        d = r.status().data
        assert d["radar"]["running"] is False and d["radar"]["stop"] == "idle"
        assert d["counters"]["radar_sigint"] == 1 and d["counters"]["radar_sigkill"] == 1
        assert [e["event"] for e in r.events if e["event"].startswith("radar")] == ["radar_stop", "radar_kill",
                                                                                    "radar_stopped"]
        assert r.named("radar_stop")[0]["pids"] == sorted([stubborn.pid, polite.pid])
        assert bystander.poll() is None and r.tool.calls == []     # another program is never touched
    finally:
        bystander.kill()
        bystander.wait(10)


def test_radar_processes_are_found_by_their_executable_also_after_a_rebuild(tmp_path):
    radars = FakeRadar(tmp_path / "build")
    finder = RadarProcesses(str(radars.exe))
    assert finder.pids() == ()
    a, b = radars.start(), radars.start()
    other = subprocess.Popen([shutil.which("sleep"), "300"])
    try:
        assert finder.pids() == tuple(sorted((a.pid, b.pid)))
        # The binary is rebuilt while the old processes still run: they still count.
        os.unlink(radars.exe)
        shutil.copy(shutil.which("sleep"), radars.exe)
        assert os.readlink(f"/proc/{a.pid}/exe").endswith(" (deleted)")
        assert finder.pids() == tuple(sorted((a.pid, b.pid)))
        # A signal goes only to a process that is (still) a radar.
        assert finder.signal(other.pid, signal.SIGKILL) == "" and finder.signal(2 ** 22 + 5, signal.SIGKILL) == ""
        time.sleep(0.2)
        assert other.poll() is None
        assert finder.signal(a.pid, signal.SIGKILL) == "" and gone(a) == -signal.SIGKILL
        assert finder.pids() == (b.pid,)
    finally:
        other.kill()
        other.wait(10)
        radars.stop_all()


def test_a_radar_path_through_a_symlink_is_resolved(tmp_path):
    radars = FakeRadar(tmp_path / "real")
    os.symlink(tmp_path / "real", tmp_path / "link")
    proc = radars.start()
    try:
        assert RadarProcesses(str(tmp_path / "link" / "radar")).pids() == (proc.pid,)
        assert RadarProcesses(str(tmp_path / "nothing" / "radar")).pids() == ()
    finally:
        radars.stop_all()


# ---- stopping the watcher

def test_a_stopped_watcher_leaves_a_not_ok_status_and_does_not_kill_a_running_tool(rig):
    r = rig()
    r.tool.plan(codes=[0], sleep=1.5)
    r.tool_ends = False
    r.follow_tool = False
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(6.0)
    proc = r.watcher._tool["proc"]
    assert proc.poll() is None
    r.watcher.cleanup()
    d = r.status().data
    assert d["ok"] is False and d["reason"] == "the safety watcher is not running" and d["running"] is False
    assert proc.poll() is None                      # left to finish: a kill would leave the amplifier enabled
    assert gone(proc) == 0 and r.tool.succeeded
    assert "amp_safe: OK" in (r.tmp / "run" / "watcher-reset-tool.out").read_text()    # its output had somewhere to go
    assert [e["event"] for e in r.events][-2:] == ["reset_tool_left", "stop"]


# ---- settings

def test_settings_that_cannot_work_revert_to_the_defaults_with_a_warning():
    cfg = copy.deepcopy(C.DEFAULTS["watcher"])
    assert W.checked(cfg) == (cfg, [])
    bad = copy.deepcopy(cfg)
    bad.update(trip_samples=1, temp_trip_c=40.0, voltage_min_v=31.0, loop_interval_s=30.0, clear_hold_s=10.0)
    bad["reset"].update(timeout_s=1.0, stuck_retry_s=0.0)
    fixed, warnings = W.checked(bad)
    assert fixed == dict(cfg, clear_hold_s=10.0)    # what can work is kept
    assert len(warnings) == 6 and all(w.endswith("defaults used") for w in warnings)
    assert any(w.startswith("watcher.reset.timeout_s, watcher.reset.kill_margin_s: must be") for w in warnings)


def test_bad_settings_are_published_as_config_warnings(rig):
    r = rig(trip_samples=0)
    assert r.watcher.cfg["trip_samples"] == 3
    r.run(3.0)
    assert r.status().data["config_warnings"] == ["watcher.trip_samples: must be between 2 and 50: defaults used"]


def test_file_settings_are_names_in_the_state_and_run_directories_or_full_paths(rig, tmp_path):
    r = rig(override_file=str(tmp_path / "elsewhere" / "flag"), telemetry_status_file=str(tmp_path / "t.json"))
    assert r.watcher.override_file == str(tmp_path / "elsewhere" / "flag")
    assert r.watcher.telemetry_file == str(tmp_path / "t.json")
    assert r.watcher.state_file == str(tmp_path / "state" / "sdr-state")
    assert r.watcher.events_file == str(tmp_path / "state" / "watcher-events.jsonl")


# ---- the event log

def test_the_event_log_is_one_json_object_per_line_and_starts_a_new_file_when_full(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(str(path), 400, wall=lambda: WALL0, uptime=lambda: 12.3456, threaded=False)
    for n in range(5):
        log.write({"event": "trip", "severity": "urgent", "text": f"number {n}", "condition": "amp_temp"})
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    older = [json.loads(line) for line in (tmp_path / "events.jsonl.1").read_text().splitlines()]
    # One older file is kept; the newest events are always there and no file passes the limit.
    assert [e["text"] for e in older + lines] == ["number 2", "number 3", "number 4"]
    assert path.stat().st_size <= 400 and (tmp_path / "events.jsonl.1").stat().st_size <= 400
    assert lines[-1] == {"time": WALL0, "time_utc": "2026-11-19T00:00:00Z", "uptime_s": 12.346, "event": "trip",
                         "severity": "urgent", "text": "number 4", "condition": "amp_temp"}
    assert log.written == 5 and log.error == ""


def test_an_event_log_that_cannot_be_written_never_raises_and_says_so(tmp_path):
    log = EventLog(str(tmp_path / "no-such-directory" / "events.jsonl"), 1e6, threaded=False)
    log.write({"event": "x"})
    assert log.written == 0 and "No such file or directory" in log.error
    log.write({"event": object()})                  # not JSON: written as text, not an error
    log = EventLog(str(tmp_path / "events.jsonl"), 1e6)        # with its thread
    for n in range(20):
        log.write({"event": "n", "n": n})
    log.close()
    assert [json.loads(line)["n"] for line in (tmp_path / "events.jsonl").read_text().splitlines()] == list(range(20))


# ---- the unit file

def test_the_unit_runs_as_the_user_restarts_always_and_never_kills_the_reset_tool():
    with open(os.path.join(REPO, "systemd", "radar-watcher.service")) as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    for wanted in ("User=englacial", "Group=englacial", "Type=notify", "NotifyAccess=main", "Restart=always",
                   "RestartSec=1", "StartLimitIntervalSec=0", "WatchdogSec=15", "WatchdogSignal=SIGKILL",
                   "KillMode=process", "Nice=-5", "OOMScoreAdjust=-900", "WantedBy=multi-user.target",
                   "WorkingDirectory=/home/englacial/englacial-radar-runner",
                   "ExecStart=/home/englacial/englacial-radar-runner/.venv/bin/python -B -u -m radar_runner.watcher"):
        assert wanted in lines, wanted
    # Independent of every other service of this project: no ordering after, and no need of, any of them.
    for line in lines:
        if line.split("=")[0] in ("After", "Requires", "Wants", "BindsTo", "PartOf", "Before", "Requisite"):
            assert line == "After=local-fs.target", line
    # The watchdog must outlast a loop round by far and stay below the hardware watchdog's 30 s.
    assert C.DEFAULTS["watcher"]["loop_interval_s"] * 20 <= 15 < 30


# ---- as a real process, everything with stand-ins

class Feeder(threading.Thread):
    """Writes a telemetry status file four times a second, as the telemetry service does."""

    def __init__(self, path, tool):
        super().__init__(daemon=True)
        self.writer = S.StatusWriter(str(path))
        self.tool = tool
        self.amp = dict(GOOD)
        self.feeding = True
        self.done = threading.Event()

    def run(self):
        while not self.done.wait(0.1):
            if self.tool.succeeded:
                self.amp["current_a"] = GOOD["current_a"]
            if self.feeding:
                self.writer.write(telemetry_status(S.boottime_s(), self.amp, age=0.05))


def start_process(tmp_path, tool, radars):
    for d in ("run", "state"):
        (tmp_path / d).mkdir()
    (tmp_path / "local.yaml").write_text(f"""
paths:
  run_dir: {tmp_path}/run
  state_dir: {tmp_path}/state
  power_status_file: {tmp_path}/power.json
watcher:
  loop_interval_s: 0.1
  radar_exe: {radars.exe}
  telemetry_lost_s: 1.0
  trip_span_s: 0.2
  clear_hold_s: 1.0
  idle_enabled_s: 1.0
  stop_grace_s: 0.5
  radar_exit_s: 0.5
  reset:
    command: {tool.path}
    timeout_s: 5.0
""")
    env = dict(os.environ, PYTHONPATH=REPO)
    env.pop("NOTIFY_SOCKET", None)
    return subprocess.Popen([sys.executable, "-B", "-u", "-m", "radar_runner.watcher", "--local-config",
                             str(tmp_path / "local.yaml"), "--secrets", str(tmp_path / "none.yaml")],
                            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for_status(tmp_path, proc, condition, timeout=20):
    deadline = time.monotonic() + timeout
    read = None
    while time.monotonic() < deadline:
        read = S.read_status(str(tmp_path / "run" / "watcher.json"), 2.0)
        if read.ok and read.data["pid"] == proc.pid and condition(read.data):
            return read.data
        assert proc.poll() is None, proc.stdout.read()
        time.sleep(0.05)
    raise AssertionError(f"status never satisfied the condition: {read}")


def test_the_service_end_to_end_with_stand_ins(tmp_path):
    tool, radars = FakeTool(tmp_path / "tool"), FakeRadar(tmp_path / "orca")
    feeder = Feeder(tmp_path / "run" / "telemetry.json", tool)
    proc = start_process(tmp_path, tool, radars)
    feeder.start()
    try:
        d = wait_for_status(tmp_path, proc, lambda d: d["ok"])
        assert d["tripped"] is False and d["reason"] == "" and d["amp"]["current_a"] == 0.156

        # Over temperature with a radar that ignores SIGINT and no manager to stop it.
        radar = radars.start(ignore_sigint=True)
        wait_for_status(tmp_path, proc, lambda d: d["radar"]["pids"] == [radar.pid])
        feeder.amp["temp_c"] = 58.0
        d = wait_for_status(tmp_path, proc, lambda d: d["tripped"])
        assert d["ok"] is False and "reached 55 C" in d["reason"]
        assert gone(radar) == -signal.SIGKILL
        assert tool.calls == []
        feeder.amp["temp_c"] = 30.0
        wait_for_status(tmp_path, proc, lambda d: d["ok"] and not d["radar"]["running"])

        # Telemetry stops and comes back.
        feeder.feeding = False
        d = wait_for_status(tmp_path, proc, lambda d: not d["ok"])
        assert d["tripped"] is False and d["conditions"]["telemetry"]["state"] == "lost"
        feeder.feeding = True
        wait_for_status(tmp_path, proc, lambda d: d["ok"])

        # The amplifier left enabled with no radar: the tool runs once, with the expected command line.
        feeder.amp["current_a"] = ENABLED_A
        wait_for_status(tmp_path, proc, lambda d: d["reset_tool"]["last"] is not None)
        d = wait_for_status(tmp_path, proc, lambda d: d["ok"])
        assert d["reset_tool"]["last"]["name"] == "OK" and d["counters"]["tool_runs"] == 1
        assert [c["argv"] for c in tool.calls] == [["--timeout", "5", "--state-file", str(tmp_path / "state" / "sdr-state")]]

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(10) == 0
        final = json.loads((tmp_path / "run" / "watcher.json").read_text())
        assert final["ok"] is False and final["reason"] == "the safety watcher is not running"
        events = [json.loads(line) for line in (tmp_path / "state" / "watcher-events.jsonl").read_text().splitlines()]
        kinds = [e["event"] for e in events]
        for wanted in ("start", "trip", "radar_stop", "radar_kill", "radar_stopped", "clear", "not_ok", "ok",
                       "reset_tool", "reset_tool_result", "stop"):
            assert wanted in kinds, wanted
        assert kinds[0] == "start" and kinds[-1] == "stop"
        output = proc.stdout.read()
        assert "Traceback" not in output and "watcher service started" in output
    finally:
        feeder.done.set()
        radars.stop_all()
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_the_process_killed_outright_leaves_a_status_that_goes_stale(tmp_path):
    tool, radars = FakeTool(tmp_path / "tool"), FakeRadar(tmp_path / "orca")
    feeder = Feeder(tmp_path / "run" / "telemetry.json", tool)
    proc = start_process(tmp_path, tool, radars)
    feeder.start()
    try:
        wait_for_status(tmp_path, proc, lambda d: d["ok"])
        proc.kill()
        proc.wait(10)
        path = str(tmp_path / "run" / "watcher.json")
        assert S.read_status(path, 5.0).data["ok"] is True      # what it said last ...
        time.sleep(1.2)
        assert not S.read_status(path, 1.0).ok                  # ... which a reader with an age limit refuses
    finally:
        feeder.done.set()
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
