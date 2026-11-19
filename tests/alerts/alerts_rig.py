"""A test rig for the alerts service: its own directories, the other services' files written by hand,
a simulated uptime and wall clock, and the stand-in ntfy server. Nothing here touches the installed
services, their files or any host but 127.0.0.1."""
import copy
import json
import os

from radar_runner.alerts.service import Alerts
from radar_runner.common import config as C
from radar_runner.common import status as S
from radar_runner.common.sdnotify import Notifier

TOPIC = "radar-test-topic-q7"
TOKEN = "tk_alertstesttoken0123456789"
BOOT_A, BOOT_B = "11111111-aaaa-4aaa-8aaa-000000000001", "22222222-bbbb-4bbb-8bbb-000000000002"
LAT, LON = "-77.8463", "166.6683"
POSITION_STRINGS = ["77.8463", "166.6683", "778463", "1666683", "77.84", "166.66"]


class Clock:
    def __init__(self, uptime=1000.0, wall=1795120000.0):
        self.up, self.wall_s = uptime, wall

    def uptime(self):
        return self.up

    def wall(self):
        return self.wall_s

    def advance(self, seconds):
        self.up += seconds
        self.wall_s += seconds


def telemetry_data(**extra):
    d = {"state": "ok", "problems": [], "boot_count": 42, "boot_count_problem": "",
         "amp": {"fresh": True, "temp_c": 30.0, "current_a": 0.156, "voltage_v": 27.9},
         "sources": {
             "ola": {"health": "ok", "reason": "", "values": {"gps_Lat": 778463000.0 * -1, "gps_Long": 1666683000.0}},
             "gps": {"health": "ok", "reason": "", "values": {"lat": float(LAT), "lon": float(LON), "mode": 3}},
             "pi": {"health": "ok", "reason": "", "values": {"soc_temp_c": 55.0, "sd_free_gb": 97.0,
                                                             "nvme_free_gb": 900.0, "clock_synced": 1},
                    "throttled_now": [], "throttled_since_boot": []},
             "ups": {"health": "ok", "reason": "", "values": {}}},
         "clock": {"steps": 0, "synced": 1},
         "logging": {"location": "primary", "writer": "running", "reason": ""}}
    d.update(extra)
    return d


class Rig:
    def __init__(self, tmp_path, server_url, boot_id=BOOT_A, **alerts_changes):
        self.tmp = tmp_path
        self.run_dir, self.state_dir = tmp_path / "run", tmp_path / "state"
        self.run_dir.mkdir(exist_ok=True)
        self.state_dir.mkdir(exist_ok=True)
        self.collections = tmp_path / "collections"
        self.collections.mkdir(exist_ok=True)
        self.clock = Clock()
        self.boot_id = boot_id
        self.server_url = server_url
        self.changes = alerts_changes
        self.watcher_events = self.state_dir / "watcher-events.jsonl"
        self.manager_events = self.state_dir / "manager-events.jsonl"
        self.power = {"version": 1, "state": "normal", "mode": "armed", "power_present": True,
                      "radar_stop_requested": False, "warnings": {},
                      "guard": {"early_shutdowns": 0, "passive": False, "last_shutdown": None}}
        self.telemetry = telemetry_data()
        self.watcher = {"ok": True, "tripped": False, "reason": "", "running": True}
        self.manager = {"state": "ready", "not_ready": [], "fault": None, "last_fault": None}
        self.service = None

    def config(self):
        cfg = copy.deepcopy(C.DEFAULTS)
        cfg["paths"].update(run_dir=str(self.run_dir), state_dir=str(self.state_dir),
                            power_status_file=str(self.run_dir / "power.json"))
        cfg["manager"]["gates"]["data"]["root"] = str(self.collections)
        a = cfg["alerts"]
        a["unit_name"] = "test-unit"
        a["summary_after_s"] = 0.0
        a["ntfy"].update(server=self.server_url, topic=TOPIC, token=TOKEN, timeout_s=2.0, retry_s=1.0,
                         retry_max_s=8.0)
        for key, value in self.changes.items():
            section, _, name = key.rpartition(".")
            (a[section] if section else a)[name] = value
        return cfg

    # ---- the other services' files

    def write_status(self):
        for name, data, path in (("telemetry", self.telemetry, self.run_dir / "telemetry.json"),
                                 ("watcher", self.watcher, self.run_dir / "watcher.json"),
                                 ("manager", self.manager, self.run_dir / "manager.json"),
                                 ("power", self.power, self.run_dir / "power.json")):
            if data is None:
                continue
            S.StatusWriter(str(path), wall=self.clock.wall, uptime=self.clock.uptime,
                           boot_id=self.boot_id).write(data)

    def event(self, path, **fields):
        line = {"time": round(self.clock.wall(), 3), "time_utc": "2026-11-19T20:00:00Z",
                "uptime_s": round(self.clock.up, 3), **fields}
        with open(path, "a") as f:
            f.write(json.dumps(line) + "\n")

    def watcher_event(self, **fields):
        self.event(self.watcher_events, **fields)

    def manager_event(self, **fields):
        self.event(self.manager_events, **fields)

    # ---- the service, in this process

    def start(self):
        self.write_status()
        for path in (self.watcher_events, self.manager_events):
            path.touch()
        self.service = Alerts(self.config(), wall=self.clock.wall, uptime=self.clock.uptime, boot_id=self.boot_id,
                              notifier=Notifier(environ={}))
        self.service.setup()
        return self.service

    def tick(self, n=1, dt=1.0):
        data = None
        for _ in range(n):
            self.clock.advance(dt)
            self.write_status()
            data = self.service.tick()
            self.service.publish(data)
        return data

    def stop(self):
        self.service.cleanup()
        self.service = None

    def reboot(self, boot_id=BOOT_B, uptime=20.0):
        """A new boot: uptime starts again, the run directory is empty, the state directory stays."""
        if self.service is not None:
            self.stop()
        self.boot_id = boot_id
        self.clock.up = uptime
        for name in os.listdir(self.run_dir):
            os.unlink(self.run_dir / name)
        return self.start()

    def queued(self):
        return sorted(os.listdir(self.state_dir / "alerts-queue"))

    def state(self):
        return json.loads((self.state_dir / "alerts-state.json").read_text())
