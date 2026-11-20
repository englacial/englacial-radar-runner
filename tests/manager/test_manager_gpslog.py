"""The raw GPS log of a collection: started with the radar, stopped with it, recorded in collection.json.

gpsd is a stand-in of its raw pass-through on a free port (tests/gpsraw/ubx_fakes.py).
"""
import os
import subprocess
import sys
import time

import pytest
from test_manager_runner import REQUEST, Telemetry

from radar_runner.gpsraw import ubx
from radar_runner.manager import gpslog
from radar_runner.manager.runner import Runner
from tests.gpsraw import ubx_fakes as F

T0 = 1795136400.0


def receiver_output(seconds):
    return b"".join(F.nav_pvt(T0 + i, -79.5, -112.0, 3.0) + F.rawx(T0 + i) + F.sfrbx() + F.nav_eoe(T0 + i)
                    for i in range(seconds))


@pytest.fixture
def gpsd(env):
    server = F.RawGpsd([receiver_output(30)], chunk=300)
    env.config["manager"]["gps_log"]["enabled"] = True
    env.config["telemetry"]["gps"]["port"] = server.port
    yield server
    server.close()


@pytest.fixture
def runner(env, events):
    r = Runner(env.config, events, amp_telemetry=Telemetry())
    yield r
    r.shutdown(10.0)


def record(env, events, runner, seconds=0.6):
    runner.start(REQUEST)
    events.wait("started")
    time.sleep(seconds)
    asked = time.monotonic()
    runner.stop("test stop")
    while not runner.radar_gone():
        time.sleep(0.005)
    gone = time.monotonic() - asked
    info = events.wait("ended")
    return info, gone


def test_a_collection_has_the_raw_log_and_its_summary(env, events, runner, gpsd):
    info, gone = record(env, events, runner)
    assert info["failure"] == "" and info["amp_safe"] is True
    meta = env.metadata()
    raw = meta["gps_raw"]
    assert raw["file"] == "gps_raw.ubx" and raw["error"] == "" and raw["no_raw_data"] is False
    assert raw["rawx_epochs"] == 30 and raw["carrier_phase_valid"] == 240 and raw["rawx_every"] == 10
    assert raw["messages"] == {"NAV-PVT": 30, "RXM-RAWX": 30, "RXM-SFRBX": 30}      # NAV-EOE is not kept
    assert raw["first_rawx"]["utc"] == "2026-11-20T01:00:00.000Z" and raw["gpsd_connects"] == 1
    path = os.path.join(env.collection_dir(), "gps_raw.ubx")
    assert raw["bytes"] == os.path.getsize(path)
    assert ubx.scan(path)["rawx_epochs"] == 30
    assert "no_raw_gps" not in meta["flags"]
    assert gpsd.watches == [b'?WATCH={"enable":true,"raw":2};\n']
    assert gone < 1.0                       # the radar's stop does not wait for the logger


def test_no_gpsd_is_flagged_and_changes_nothing_else(env, events, runner):
    env.config["manager"]["gps_log"]["enabled"] = True          # and nothing listens on the port
    info, _ = record(env, events, runner, seconds=0.3)
    assert info["failure"] == "" and info["amp_safe"] is True and env.metadata()["end"]["how"] == "stopped"
    raw = env.metadata()["gps_raw"]
    assert raw["no_raw_data"] is True and raw["bytes"] == 0 and "cannot connect to gpsd" in raw["error"]
    assert "no_raw_gps" in env.metadata()["flags"]
    assert os.path.exists(os.path.join(env.collection_dir(), gpslog.LOG_NAME))


def test_a_receiver_without_raw_messages_is_flagged(env, events, runner):
    server = F.RawGpsd([b"".join(F.nav_pvt(T0 + i, -79.5, -112.0, 0.0) + F.nav_eoe(T0 + i) for i in range(10))])
    env.config["manager"]["gps_log"]["enabled"] = True
    env.config["telemetry"]["gps"]["port"] = server.port
    try:
        record(env, events, runner, seconds=0.3)
    finally:
        server.close()
    raw = env.metadata()["gps_raw"]
    assert raw["no_raw_data"] is True and raw["rawx_epochs"] == 0 and raw["messages"] == {"NAV-PVT": 10}
    assert "no_raw_gps" in env.metadata()["flags"]


def test_disabled_there_is_no_raw_log(env, events, runner):
    record(env, events, runner, seconds=0.2)
    assert "gps_raw" not in env.metadata() and "gps_raw.ubx" not in os.listdir(env.collection_dir())


def test_a_logger_that_does_not_exit_is_killed_after_the_wait(tmp_path):
    cfg = {"file": "gps_raw.ubx", "messages": [], "fsync_interval_s": 1.0, "stop_wait_s": 0.3, "rawx_every": 10}
    log = gpslog.GpsLog(cfg, {"host": "127.0.0.1", "port": 1}, str(tmp_path))
    log.proc = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, "
                                 "signal.SIG_IGN); print('', flush=True); time.sleep(30)"],
                                stdout=subprocess.PIPE, start_new_session=True)
    log.proc.stdout.readline()               # it is running and ignores SIGTERM
    started = time.monotonic()
    out = log.finish()
    assert time.monotonic() - started < 1.5 and log.proc.returncode is not None
    assert out["no_raw_data"] is True and "did not exit within 0.3 s: killed" in out["error"]


def test_settings_that_cannot_work_are_reported_and_nothing_is_started(tmp_path):
    cfg = {"file": "../x.ubx", "messages": ["RXM-RAWX", "NAV-NOPE"], "fsync_interval_s": 1.0, "stop_wait_s": 0.3,
           "rawx_every": -1}
    log = gpslog.GpsLog(cfg, {"host": "127.0.0.1", "port": 1}, str(tmp_path))
    log.start()
    assert log.proc is None
    out = log.finish()
    assert "NAV-NOPE" in out["error"] and "plain file name" in out["error"] and "rawx_every" in out["error"]
