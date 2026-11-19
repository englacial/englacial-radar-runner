"""The safety watcher service (radar_runner/watcher/service.py): with real files, a stand-in reset tool and
stand-in radar processes, first in simulated time, then as a real process.

No test runs the real amp_safe or sdr_gpio_safe, opens a device, or signals a process it did not start.
"""
import copy
import fcntl
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

    def __init__(self, tmp_path, up=UP0, **changes):
        self.tmp = tmp_path
        for name in ("run", "state"):
            (tmp_path / name).mkdir(exist_ok=True)
        self.up = up
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
        self.radar_age = 1000.0     # the stand-in radars count as long past their set-up
        self.watcher.radar.with_ages = lambda now: tuple((pid, self.radar_age) for pid in self.watcher.radar.pids())
        self.watcher.sdr_state = lambda: self.sdr
        (tmp_path / "state" / "sdr-state").write_text("safe\nwritten by the test\n")
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
    sdr = "ready"

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
        made[-1].make = make
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
    assert d["conditions"]["telemetry"]["state"] == "ok"
    assert d["conditions"]["pin"] == {"state": "ok", "since_uptime_s": None, "record_safe": True,
                                      "signalled_by_watcher": False, "sdr": "unknown"}
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
    # The status says "tripped" before the tool starts.
    assert [e["event"] for e in r.events] == ["start", "trip", "not_ok", "reset_tool", "reset_tool_result", "clear", "ok"]
    r.run(120.0)
    assert len(r.tool.calls) == 1



def test_when_the_state_file_cannot_be_written_the_tool_runs_without_it(rig):
    # A card that has gone read-only: amp_safe then refuses to start the tool (exit 8).
    r = rig()
    r.tool.plan(codes=[8, 0])
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(14.0)
    first, second = r.tool.calls
    assert "--state-file" in first["argv"] and second["argv"] == ["--timeout", "30"]
    assert r.status().data["ok"] is True
    assert "without --state-file" in r.named("reset_tool_result")[0]["text"]


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
                                        "stop_since_uptime_s": None, "setting_up": [], "exe": str(r.radars.exe)}
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
    r.run(2.5)
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
    assert d["problems"][0] == {"condition": "watcher", "text": "the safety watcher is not running", "trip": False}
    assert d["tripped"] is True and d["problems"][1]["condition"] == "amp_enabled"     # what it knew last stays visible
    assert proc.poll() is None                      # left to finish: a kill would leave the amplifier enabled
    assert gone(proc) == 0 and r.tool.succeeded
    assert "amp_safe: OK" in (r.tmp / "run" / "watcher-reset-tool.out").read_text()    # its output had somewhere to go
    assert [e["event"] for e in r.events][-2:] == ["reset_tool_left", "stop"]


# ---- settings

def test_settings_that_cannot_work_revert_to_the_defaults_with_a_warning():
    cfg = copy.deepcopy(C.DEFAULTS["watcher"])
    assert W.checked(cfg) == (cfg, [])
    bad = copy.deepcopy(cfg)
    bad.update(trip_samples=1, temp_trip_c=40.0, voltage_min_v=31.0, loop_interval_s=30.0, clear_hold_s=10.0,
               radar_setup_s=-1.0)
    bad["reset"].update(timeout_s=1.0, stuck_retry_s=0.0, blind_delay_s=0.0)
    fixed, warnings = W.checked(bad)
    assert fixed == dict(cfg, clear_hold_s=10.0)    # what can work is kept
    assert len(warnings) == 8 and all(w.endswith("defaults used") for w in warnings)
    assert any(w.startswith("watcher.reset.timeout_s, watcher.reset.kill_margin_s: must be") for w in warnings)


def test_bad_settings_are_published_as_config_warnings(rig):
    r = rig(trip_samples=0)
    assert r.watcher.cfg["trip_samples"] == 3
    r.run(3.0)
    assert r.status().data["config_warnings"] == [
        "watcher.trip_samples, watcher.trip_window: must be at least 2 readings, and a window no smaller: defaults used"]


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
  radar_setup_s: 0.5
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


# ---- the reset tool's results, through the stand-in

def test_a_tool_that_ran_and_did_not_help_three_times_means_stuck_with_one_run_a_minute(rig):
    r = rig()
    r.tool.plan(codes=[3])
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(20.0)
    d = r.status().data
    assert len(r.tool.calls) == 3
    assert d["ok"] is False and d["tripped"] is True and d["conditions"]["amp_enabled"]["state"] == "stuck"
    assert d["reason"].startswith("AMPLIFIER STUCK ENABLED") and "last: ASSUME_ENABLED (exit 3)" in d["reason"]
    stuck, = r.named("stuck")
    assert stuck["severity"] == "urgent" and stuck["current_a"] == ENABLED_A
    r.run(125.0)
    assert len(r.tool.calls) == 5                   # one a minute
    r.tool.plan(codes=[0])                          # the next run succeeds and the current falls
    r.run(62.0)
    assert len(r.tool.calls) == 6 and r.status().data["ok"] is True
    assert "no longer stuck enabled" in r.named("clear")[0]["text"]


@pytest.mark.parametrize("code, name", [(2, "NOT_FOUND"), (4, "TIMEOUT"), (5, "FAILED"), (6, "BUSY"), (7, "AMBIGUOUS"),
                                        (8, "STATE_FILE"), (64, "USAGE")])
def test_a_tool_that_cannot_run_is_never_called_stuck_and_is_urgent_after_thirty_seconds(rig, code, name):
    r = rig()
    r.tool.plan(codes=[code])
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(30.0)
    d = r.status().data
    assert d["conditions"]["amp_enabled"]["state"] == "resetting" and r.named("blocked") == []
    r.run(15.0)
    d = r.status().data
    assert d["ok"] is False and d["tripped"] is True and d["conditions"]["amp_enabled"]["state"] == "blocked"
    assert d["conditions"]["amp_enabled"]["tool_ran"] == 0 and r.named("stuck") == []
    assert d["reason"].startswith("AMPLIFIER ENABLED with no radar process and the reset tool cannot run")
    assert (d["reset_tool"]["last"]["code"], d["reset_tool"]["last"]["name"]) == (code, name)
    blocked, = r.named("blocked")
    assert blocked["severity"] == "urgent" and f"{name} (exit {code})" in blocked["text"]
    r.tool.plan(codes=[0])
    r.run(20.0)
    assert r.status().data["ok"] is True


def test_a_tool_that_cannot_be_started_is_reported_as_failed(rig):
    r = rig(command="/nonexistent/amp_safe")
    r.run(3.0)
    assert r.status().data["reset_tool"]["runnable"] is False
    r.amp["current_a"] = ENABLED_A
    r.run(45.0)
    d = r.status().data
    assert d["conditions"]["amp_enabled"]["state"] == "blocked" and d["reset_tool"]["last"]["name"] == "FAILED"
    assert "cannot start /nonexistent/amp_safe: No such file or directory" in d["reset_tool"]["last"]["text"]


def test_while_another_amp_safe_holds_the_lock_the_watcher_does_not_start_its_own(rig):
    r = rig()
    r.run(3.0)
    holder = os.open(str(r.tool.path), os.O_RDONLY)     # as a running amp_safe holds its own script
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert W.lock_held(str(r.tool.path))
        r.amp["current_a"] = ENABLED_A
        r.run(20.0)
        assert r.tool.calls == [] and r.status().data["tripped"] is True
    finally:
        os.close(holder)
    assert not W.lock_held(str(r.tool.path)) and not W.lock_held("/nonexistent/amp_safe")
    r.run(1.0)
    assert len(r.tool.calls) == 1


# ---- no telemetry, and the pin not known to be safe

def test_without_telemetry_a_record_that_does_not_say_safe_gets_one_run_of_the_tool(rig):
    r = rig()
    r.run(3.0)
    (r.tmp / "state" / "sdr-state").write_text("unknown\namp_safe: sdr_gpio_safe is starting\n")
    r.feed = False
    r.run(19.0)
    assert r.tool.calls == [] and r.status().data["tripped"] is False
    r.run(3.0)
    d = r.status().data
    assert len(r.tool.calls) == 1 and d["conditions"]["pin"]["record_safe"] is False
    assert d["conditions"]["pin"]["sdr"] == "ready"
    assert r.named("trip")[0]["condition"] == "pin" and r.named("trip")[0]["severity"] == "urgent"
    # The stand-in said OK; the real amp_safe would have written "safe" itself.
    (r.tmp / "state" / "sdr-state").write_text("safe\n")
    r.run(600.0)
    d = r.status().data
    assert len(r.tool.calls) == 1 and d["tripped"] is False and d["ok"] is False        # telemetry is still lost


@pytest.mark.parametrize("sdr", ["bootloader", "absent"])
def test_without_telemetry_the_tool_is_not_run_on_an_sdr_that_was_never_started_or_is_not_there(rig, sdr):
    r = rig()
    r.sdr = sdr
    (r.tmp / "state" / "sdr-state").unlink()        # no record at all: not known safe
    r.feed = False
    r.run(600.0)
    assert r.tool.calls == [] and r.status().data["tripped"] is False
    assert r.status().data["conditions"]["pin"]["sdr"] == sdr


def test_the_record_and_the_sdr_are_read_without_touching_anything(tmp_path):
    record = tmp_path / "sdr-state"
    assert not W.record_says_safe(str(record))
    for text, safe in (("safe\nwhy\n", True), ("safe", True), ("unknown\n", False), ("safely\n", False),
                       (" safe\n", False), ("", False), ("SAFE\n", False)):
        record.write_text(text)
        assert W.record_says_safe(str(record)) is safe, text
    usb = tmp_path / "usb"
    assert W.sdr_state(str(usb)) == "absent"
    for name, vendor, product in (("1-1", "1a86", "USB Serial"), ("2-1", "2500", "WestBridge "), ("usb1", "1d6b", None)):
        (usb / name).mkdir(parents=True)
        (usb / name / "idVendor").write_text(vendor + "\n")
        if product:
            (usb / name / "product").write_text(product + "\n")
    assert W.sdr_state(str(usb)) == "bootloader"
    (usb / "2-1" / "product").write_text("USRP B200\n")
    assert W.sdr_state(str(usb)) == "ready"
    assert W.sdr_state() in ("ready", "bootloader", "absent")       # the real one: only looked at


# ---- what a restart must not lose

def test_a_restarted_watcher_knows_its_trips_again_and_a_file_from_another_boot_is_ignored(rig):
    r = rig()
    r.run(3.0)
    r.amp["temp_c"] = 57.0
    r.run(3.0)
    r.amp["temp_c"] = 53.0
    r.run(3.0)
    kept = json.loads((r.tmp / "run" / "watcher-state.json").read_text())
    assert kept["state"] == {"trips": {"amp_temp": True, "case_temp": False, "voltage": False}, "enabled": "idle",
                             "signalled": False, "record_unwritable": False}
    again = Rig(r.tmp, up=r.up)                     # the watcher is killed and started again
    again.amp = dict(r.amp)
    again.run(4.0)
    d = again.status().data
    assert d["ok"] is False and d["tripped"] is True and d["conditions"]["amp_temp"]["state"] == "trip"
    assert "restored" in again.named("start")[-1]["text"]
    # The same file with another boot's ID says nothing about now.
    kept["boot_id"] = BOOT
    (r.tmp / "run" / "watcher-state.json").write_text(json.dumps(kept))
    other = Rig(r.tmp, up=r.up)
    other.run(3.0)
    assert other.status().data["ok"] is True
    for junk in ("", "{", "[1]", '{"state": 5, "uptime_s": 1}'):
        (r.tmp / "run" / "watcher-state.json").write_text(junk)
        Rig(r.tmp, up=r.up).run(3.0)


def test_the_record_unwritable_flag_survives_a_restart(rig):
    r = rig()
    r.tool.plan(codes=[8, 0])
    r.run(3.0)
    r.amp["current_a"] = ENABLED_A
    r.run(7.0)
    assert json.loads((r.tmp / "run" / "watcher-state.json").read_text())["state"]["record_unwritable"] is True
    again = Rig(r.tmp, up=r.up)
    assert again.watcher._record_unwritable is True


# ---- which program is the radar

def test_the_radar_binary_comes_from_the_managers_settings_unless_set_explicitly():
    config = copy.deepcopy(C.DEFAULTS)
    assert config["watcher"]["radar_exe"] == "manager"
    config["manager"] = {"orca": {"dir": "/opt/orca", "radar_cwd": "sdr/build", "radar_command": ["./radar", "{config_rel}"]}}
    assert W.radar_exe(config) == "/opt/orca/sdr/build/radar"
    config["watcher"]["radar_exe"] = "/somewhere/else/radar"
    assert W.radar_exe(config) == "/somewhere/else/radar"
    # The defaults as they are: the manager's ORCA checkout.
    orca = C.DEFAULTS["manager"]["orca"]
    assert W.radar_exe(copy.deepcopy(C.DEFAULTS)) == "/home/englacial/orca/sdr/build/radar"
    # The allowance outlasts the time in which the manager itself holds a stop for ORCA's start line.
    assert C.DEFAULTS["watcher"]["radar_setup_s"] > orca["start_timeout_s"] + orca["stop_timeout_s"]
    # ... and the files that both use are the same files.
    from radar_runner.manager import ampsafe, gates
    assert C.DEFAULTS["watcher"]["reset"]["state_file"] == ampsafe.STATE_FILE
    assert C.DEFAULTS["watcher"]["override_file"] == gates.OVERRIDE_NO_AMP_TELEMETRY
    watcher_gate = C.DEFAULTS["manager"]["gates"]["watcher"]
    assert (watcher_gate["require_true"], watcher_gate["require_false"], watcher_gate["reason_field"],
            watcher_gate["max_age_s"]) == (["ok"], ["tripped"], "reason", 5.0)
    # Without the manager's settings at all: the usual place.
    config = copy.deepcopy(C.DEFAULTS)
    config.pop("manager", None)
    assert W.radar_exe(config) == "/home/englacial/orca/sdr/build/radar"
    config["manager"] = {"orca": {"dir": 5}}
    assert W.radar_exe(config) == "/home/englacial/orca/sdr/build/radar"


def test_the_age_of_a_radar_process_is_taken_from_proc(tmp_path):
    radars = FakeRadar(tmp_path / "build")
    proc = radars.start()
    try:
        time.sleep(0.3)
        (pid, age), = RadarProcesses(str(radars.exe)).with_ages(S.boottime_s())
        assert pid == proc.pid and 0.2 <= age <= 5.0
    finally:
        radars.stop_all()


def test_a_stand_in_radar_that_is_still_setting_up_is_not_signalled(rig):
    r = rig()
    r.radar_age = 3.0
    radar = r.radars.start()
    r.run(3.0)
    r.feed = False
    r.run(60.0)
    d = r.status().data
    assert radar.poll() is None and d["ok"] is False and d["radar"]["setting_up"] == [radar.pid]
    assert d["counters"]["radar_sigint"] == 0 and [w["warning"] for w in d["warnings"]] == ["radar_setting_up"]
    r.radar_age = 66.0
    r.run(1.0)
    assert gone(radar) == -signal.SIGINT
    assert r.status().data["conditions"]["pin"]["signalled_by_watcher"] is True
