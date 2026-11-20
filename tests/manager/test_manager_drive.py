"""Simulated drives through gpsd: the motion trigger end to end, with the stand-in radar.

    gpsfake -> a private gpsd on a free port -> the telemetry service's gpsd client and its status
    entry for the GPS (telemetry/gps.py, telemetry/service.py) -> telemetry.json -> the manager's
    motion trigger -> logic.auto -> the stand-in radar

The receiver is a log of synthetic u-blox NAV-PVT messages at 1 Hz (tests/gpsraw/ubx_fakes.py),
replayed ten times faster than real time. The trigger counts in receiver time, so its
settings here are only shortened to keep the tests short, and max_gap_s is raised because at this
speed the manager sees every second or third fix. `stale_s` is Pi time, so a loss of fix
of 30 s here lasts 3 s and the trigger's answer holds through it (in real time it would say
"unknown", which changes nothing either: test_manager_motion.py). gpsfake runs its own gpsd with its own control
socket; nothing here touches the live gpsd or the receiver.
"""
import os
import shutil
import signal
import socket
import subprocess
import threading
import time

import pytest
from test_manager_service import Running

from radar_runner.common import status as S
from radar_runner.telemetry.gps import Gps
from radar_runner.telemetry.service import Telemetry
from tests.gpsraw import ubx_fakes as F

CYCLE_S = 0.05                  # gpsfake's pause between two packets; two packets per fix
START = 1795136400.0            # receiver time of the first fix
LEAD_S = 40                     # parked before every drive: gpsd, telemetry and the manager come up meanwhile
MOTION = {"start_speed_mps": 2.0, "start_s": 10.0, "stop_speed_mps": 0.5, "stop_s": 20.0, "max_gap_s": 5.0}

pytestmark = pytest.mark.skipif(not shutil.which("gpsfake"), reason="gpsfake is not installed")


@pytest.fixture(autouse=True)
def short_stop(monkeypatch):
    from radar_runner.manager import motion
    monkeypatch.setitem(motion.MINIMUMS, "stop_s", 1.0)       # 20 s here instead of at least 30


class _Quiet:
    def write(self, *args):
        pass

    def status(self):
        return {}

    def close(self):
        pass


class Receiver:
    """gpsfake replaying a drive, and the telemetry service's view of it (its `sources.gps` entry)."""

    def __init__(self, env, segments):
        log = os.path.join(env.tmp, "drive.ubx")
        with open(log, "wb") as f:
            f.write(F.drive(parked(LEAD_S) + segments, start_unix=START - LEAD_S))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.proc = subprocess.Popen(["gpsfake", "-t", "-1", "-q", "-P", str(port), "-c", str(CYCLE_S), log],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        cfg = dict(env.config["telemetry"]["gps"], port=port, interval_s=0.01, reconnect_s=0.2,
                   receiver_fix_command="")
        config = dict(env.config, telemetry=dict(env.config["telemetry"], gps=cfg))
        self.telemetry = Telemetry(config, gps=Gps(cfg), ola=_Quiet(), pi=_Quiet(), store=_Quiet(), status=_Quiet())
        self.entry = None
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        tel = self.telemetry
        while not self._stop.is_set():
            mono, up = time.monotonic(), S.boottime_s()
            tel._step_gps(mono, time.time(), up)
            entry = tel._gps_status(up, mono)
            with self.lock:
                self.entry = entry
            time.sleep(0.005)

    def gps(self):
        with self.lock:
            return self.entry

    def fix_time(self):
        """Receiver time of the newest fix, in seconds from the start of the drive (after the lead)."""
        from radar_runner.manager.motion import fix_time
        entry = self.gps() or {}
        t = fix_time((entry.get("values") or {}).get("gps_time"))
        return None if t is None else t - START

    def wait_until(self, t, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            now = self.fix_time()
            if now is not None and now >= t:
                return
            time.sleep(0.01)
        raise AssertionError(f"the drive did not reach {t} s (at {self.fix_time()})")

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self.telemetry.gps.close()
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except OSError:
            pass
        self.proc.wait(10)


class Drive(Running):
    """The manager with the motion trigger on, and telemetry.json carrying the receiver's fixes."""

    def __init__(self, env, segments):
        env.config["manager"]["triggers"]["gps"] = True
        env.config["manager"]["motion"].update(MOTION)
        self.receiver = Receiver(env, segments)
        super().__init__(env)

    def publish(self):
        feed = self.feed
        self.feed = feed - {"telemetry"}        # the telemetry file is written here, once, with the GPS in it
        try:
            super().publish()
        finally:
            self.feed = feed
        if "telemetry" in feed:
            up = time.clock_gettime(time.CLOCK_BOOTTIME)
            self.env.publish("telemetry", {"state": "ok",
                                           "amp": {"fresh": True, "reason": "", "current_a": self.amp_current,
                                                   "voltage_v": self.amp_voltage, "sample_uptime_s": up},
                                           "loop": {"last_gap_s": 0.25, "recent_longest_gap_s": 0.25},
                                           "sources": {"gps": self.receiver.gps() or {}}})

    def auto_states(self):
        return [e["state"] for e in self.events("auto")]

    def stop(self):
        try:
            return super().stop()
        finally:
            self.receiver.close()


def parked(seconds, hacc=0.8):
    return [(seconds, 0.03, True, hacc)]


def driving(seconds, speed=4.0):
    return [(seconds, speed, True, 0.8)]


def test_a_drive_starts_and_stops_a_collection(env):
    d = Drive(env, parked(40) + driving(60) + parked(45)).start()
    try:
        d.receiver.wait_until(39)
        assert d.status()["motion"]["state"] == "stationary" and d.state() == "ready"
        status = d.wait_state("running", timeout=20)
        assert status["trigger"] == "gps" and env.metadata()["trigger"] == "gps"
        assert d.receiver.fix_time() < 100                 # started while driving
        d.wait_state("ready", timeout=30)
        assert d.receiver.fix_time() >= 120                # 20 s after the stop
        assert len(env.collections()) == 1 and env.metadata()["end"]["reason"] == "gps: stationary"
        assert d.auto_states()[:2] == ["stationary", "moving"] and d.auto_states()[-1] in ("stationary", "unknown")
    finally:
        d.stop()


def test_loss_of_fix_mid_drive_changes_nothing(env):
    segments = parked(30) + driving(30) + [(30, 4.0, False, 0.8)] + driving(20) + [(30, 0.0, False, 0.8)] + parked(40)
    d = Drive(env, segments).start()
    try:
        d.wait_state("running", timeout=30)
        d.receiver.wait_until(70)                                 # in the middle of the loss of fix
        status = d.status()
        assert status["state"] == "running" and status["auto"]["state"] in ("moving", "unknown")
        assert status["motion"]["problem"] == "fix mode 1"
        d.receiver.wait_until(125)                                # parked, but without a fix
        assert d.state() == "running"
        d.wait_state("ready", timeout=30)
        assert d.receiver.fix_time() >= 160               # the stop counted from good fixes only
        assert len(env.collections()) == 1 and env.metadata()["end"]["reason"] == "gps: stationary"
    finally:
        d.stop()


def test_a_button_stop_mid_drive_holds_until_the_truck_has_stopped(env):
    d = Drive(env, parked(30) + driving(90) + parked(30) + driving(30)).start()
    try:
        d.wait_state("running", timeout=30)
        assert d.ctl("stop", "--wait")[0] == 0                    # the same as a button press
        status = d.wait_state("ready")
        assert status["auto"]["blocked"]
        d.receiver.wait_until(110)                                # still driving: no restart
        assert d.state() == "ready" and len(env.collections()) == 1
        assert env.metadata()["end"]["reason"] == "stop by command"
        d.receiver.wait_until(145)                                # stopped long enough: unblocked
        assert not d.status()["auto"]["blocked"]
        d.wait_state("running", timeout=30)                       # and the next drive starts again
        assert len(env.collections()) == 2 and env.metadata()["trigger"] == "gps"
    finally:
        d.stop()


def test_creeping_below_the_threshold_does_not_start(env):
    d = Drive(env, parked(30) + driving(90, speed=1.8) + parked(5)).start()
    try:
        d.receiver.wait_until(118)
        assert d.state() == "ready" and env.collections() == []
        assert d.status()["auto"]["state"] == "stationary" and "moving" not in d.auto_states()
    finally:
        d.stop()


def test_gps_noise_while_parked_does_not_start(env):
    """Like the bench's indoor antenna: bursts above 2 m/s for up to 9 s, poor fixes, a 10 m/s spike."""
    noise = []
    for _ in range(4):
        noise += parked(15) + [(9, 3.5, True, 4.5), (2, 0.4, True, 4.0), (1, 10.0, True, 6.0), (6, 2.6, True, 3.0)]
        noise += [(8, 6.0, True, 7.5)]                            # a long burst with a poor fix
    d = Drive(env, parked(25) + noise + parked(5)).start()
    try:
        d.receiver.wait_until(25 + 4 * 41 + 3)
        assert d.state() == "ready" and env.collections() == []
        assert "moving" not in d.auto_states()
    finally:
        d.stop()
