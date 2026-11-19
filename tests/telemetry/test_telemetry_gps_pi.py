"""The gpsd client and the Pi sensors (radar_runner/telemetry/gps.py, pi.py)."""
import copy
import json
import os
import socket
import threading

from telemetry_fakes import TPV, GpsdServer, make_pi_tree

from radar_runner.common import config as C
from radar_runner.telemetry import gps as G
from radar_runner.telemetry import pi as P

CFG = C.DEFAULTS["telemetry"]["gps"]


class Bench:
    def __init__(self, **overrides):
        self.t = 0.0
        self.server = GpsdServer()
        self.gps = G.Gps(dict(copy.deepcopy(CFG), **overrides), connect=self.server, monotonic=lambda: self.t)
        self.samples = []

    def run(self, seconds):
        for _ in range(int(seconds / 0.25)):
            self.t += 0.25
            sample = self.gps.poll()
            if sample is not None:
                self.samples.append(sample)

    @property
    def conn(self):
        return self.server.connections[-1]


def test_one_sample_per_second_with_the_fix_fields():
    b = Bench()
    b.run(10)
    assert b.conn.sent[0] == b'?WATCH={"enable":true};\n'          # no "json": gpsd streams nothing by itself
    assert set(b.conn.sent[1:]) == {b"?POLL;\n"} and 9 <= len(b.conn.sent) - 1 <= 11
    assert 9 <= len(b.samples) <= 10 and b.gps.error == "" and b.gps.connects == 1
    s = b.samples[-1]
    assert list(s) == list(G.COLUMNS)
    assert (s["mode"], s["lat"], s["lon"], s["alt_hae_m"], s["alt_msl_m"]) == (3, 40.0, -120.0, -21.628, 7.888)
    assert (s["speed_mps"], s["track_deg"], s["climb_mps"]) == (0.082, 190.989, -0.42)
    assert (s["eph_m"], s["epv_m"], s["ept_s"], s["hdop"], s["pdop"], s["vdop"]) == (1.56, 2.105, 0.005, 0.96, 1.63, 1.31)
    assert s["sats_used"] is None and s["receivers"] == 1 and s["new_fix"] is True
    assert s["gps_time"].startswith("2026-11-19T01:")
    assert [x["gps_time"] for x in b.samples] == sorted({x["gps_time"] for x in b.samples})    # each one new


def test_satellite_counts_when_gpsd_includes_them():
    b = Bench()
    b.server.prepare = lambda conn: conn.sky.update(uSat=20, nSat=47)
    b.run(3)
    assert (b.samples[-1]["sats_used"], b.samples[-1]["sats_seen"]) == (20, 47)


def test_gpsd_not_there_at_start_then_appearing():
    b = Bench()
    b.server.down = True
    b.run(12)
    assert b.samples == [] and "cannot connect to gpsd: Connection refused" in b.gps.error
    assert b.gps.connects == 0
    b.server.down = False
    b.run(7)
    assert b.samples and b.gps.error == "" and b.gps.connects == 1


def test_gpsd_restarting_is_followed():
    b = Bench()
    b.run(5)
    b.conn.eof = True                       # gpsd exits
    b.server.down = True
    b.run(3)
    assert "gpsd closed the connection" in b.gps.error and b.gps.receivers is None
    b.run(5)
    assert "cannot connect" in b.gps.error
    b.server.down = False
    n = len(b.samples)
    b.run(7)
    assert len(b.samples) > n and b.gps.connects == 2 and b.gps.error == ""
    assert b.server.connections[0].closed


def test_gpsd_that_stops_answering_is_reconnected():
    b = Bench()
    b.run(5)
    b.conn.answer = False
    b.run(9)
    assert b.gps.connects == 1
    b.run(8)
    assert b.gps.connects == 2 and b.samples[-1]["new_fix"]


def test_send_failure_reconnects():
    b = Bench()
    b.run(3)
    b.conn.closed = True
    b.run(1.5)
    assert "connection lost" in b.gps.error
    b.run(6)
    assert b.gps.connects == 2


def test_fix_that_stops_moving_is_not_new():
    b = Bench()
    b.run(5)
    assert b.gps.no_data_s(b.t) < 1.5
    b.conn.advance = False                  # gpsd keeps answering with its last fix
    n = len(b.samples)
    b.run(10)
    stale = b.samples[n + 1:]
    assert stale and all(s["new_fix"] is False and s["lat"] == 40.0 for s in stale)
    assert b.t - b.gps.last_new_fix_at >= 9
    assert 9 <= b.gps.no_data_s(b.t) <= 11  # present, and nothing new from it for this long
    b.run(25)
    assert 34 <= b.gps.no_data_s(b.t) <= 36
    b.conn.advance = True
    b.run(2)
    assert b.gps.no_data_s(b.t) < 1.5 and b.samples[-1]["new_fix"]


def test_receiver_without_time_and_receiver_unplugged():
    b = Bench()
    b.server.prepare = lambda conn: conn.tpv.update(mode=0, time=None, lat=None, lon=None)
    b.run(5)
    assert b.samples[-1]["mode"] == 0 and b.samples[-1]["new_fix"] is False and b.gps.last_new_fix_at is None
    b.run(30)
    assert b.gps.no_data_s(b.t) > 30        # counted from when gpsd first had the receiver
    b.conn.active = 0                       # unplugged: gpsd has no receiver
    b.run(3)
    assert b.samples[-1]["receivers"] == 0 and b.samples[-1]["mode"] is None and b.gps.no_data_s(b.t) == 0.0
    b.conn.active = 1
    b.run(3)
    assert 0 < b.gps.no_data_s(b.t) <= 3.5  # back: counted from now


def test_garbage_from_gpsd_is_ignored():
    b = Bench()
    b.run(2)
    b.conn._out += b'{"class":"POLL","tpv":[{"mode":"x"\r\n{"class":"POLL"}\r\nnot json\r\n{"class":"POLL","tpv":7,"sky":null}\r\n'
    n = len(b.samples)
    b.run(5)
    assert len(b.samples) >= n + 4 and b.gps.error == ""
    b.conn._out += b"x" * (G.MAX_BUFFER + 10)
    b.run(3)
    assert len(b.gps._buf) < G.MAX_BUFFER


def test_the_client_never_sends_anything_but_watch_and_poll_and_starts_no_program():
    b = Bench()
    b.server.prepare = lambda conn: conn.tpv.update(mode=0, time=None)      # a receiver that delivers nothing
    b.run(120)
    sent = {data for conn in b.server.connections for data in conn.sent}
    assert sent == {b'?WATCH={"enable":true};\n', b"?POLL;\n"}
    source = open(G.__file__).read()
    assert "subprocess" not in source and "hexdata" not in source and "?DEVICE" not in source


def test_real_socket_to_a_local_server():
    """The default connection code, against a tiny gpsd stand-in on a real TCP socket."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    seen = []

    def serve():
        conn, _ = server.accept()
        conn.sendall(b'{"class":"VERSION","release":"3.25"}\r\n')
        f = conn.makefile("rb")
        for line in f:
            seen.append(line)
            if line.startswith(b"?POLL"):
                reply = {"class": "POLL", "active": 1, "tpv": [TPV], "sky": []}
                conn.sendall(json.dumps(reply, separators=(",", ":")).encode() + b"\r\n")     # as gpsd writes it
                break
        conn.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    gps = G.Gps(dict(CFG, port=port))
    sample = None
    for _ in range(200):
        sample = gps.poll()
        if sample:
            break
        threading.Event().wait(0.01)
    thread.join(5)
    server.close()
    gps.close()
    assert sample and sample["lat"] == 40.0 and seen[0].startswith(b"?WATCH")
    refused = G.Gps(dict(CFG, port=port))                               # nothing listens there now
    assert refused.poll() is None and "cannot connect to gpsd" in refused.error


# ---- Pi sensors

def test_pi_sensors_from_a_fake_tree(tmp_path):
    root = make_pi_tree(str(tmp_path / "root"))
    (tmp_path / "mnt").mkdir()
    pi = P.PiSensors(root=root, data_mount=str(tmp_path / "mnt"), clock_synced=lambda: True)
    v = pi.read()
    assert list(v) == list(P.COLUMNS)
    assert v["soc_temp_c"] == 50.15 and v["throttled"] == "0x50005"
    assert (v["load1"], v["load5"], v["mem_avail_mb"]) == (0.32, 0.38, 15027)
    # hwmon1 is controller nvme1 and hwmon2 is nvme0 in the fake tree: found by controller, not by number.
    assert (v["nvme1_temp_c"], v["nvme0_temp_c"]) == (39.85, 53.85)
    assert (v["fan_rpm"], v["fan_pwm"], v["rp1_temp_c"]) == (2933.0, 75.0, 49.08)
    assert v["sd_free_gb"] > 0 and v["nvme_free_gb"] is None            # the data directory is not a mount point
    assert v["clock_synced"] == 1
    assert P.throttle_flags(0x50005) == (["under_voltage", "throttled"], ["under_voltage", "throttled"])
    assert P.throttle_flags(0x80000) == ([], ["soft_temp_limit"]) and P.throttle_flags(0) == ([], [])


def test_pi_sensors_that_are_missing_give_empty_values(tmp_path):
    root = make_pi_tree(str(tmp_path / "root"), nvme=(), fan=False, soc=None, throttled="not hex\n")
    os.remove(os.path.join(root, "proc/meminfo"))
    with open(os.path.join(root, "proc/loadavg"), "w") as f:
        f.write("garbage")
    v = P.PiSensors(root=root, data_mount="", clock_synced=lambda: None).read()
    for name in ("soc_temp_c", "throttled", "load1", "mem_avail_mb", "nvme0_temp_c", "nvme1_temp_c", "fan_rpm",
                 "nvme_free_gb", "clock_synced"):
        assert v[name] is None, name
    assert v["rp1_temp_c"] == 49.08
    empty = P.PiSensors(root=str(tmp_path / "nothing"), clock_synced=lambda: False).read()
    assert empty["soc_temp_c"] is None and empty["clock_synced"] == 0


def test_pi_sensors_follow_a_drive_that_appears(tmp_path):
    root = make_pi_tree(str(tmp_path / "root"), nvme=())
    pi = P.PiSensors(root=root, data_mount="", clock_synced=lambda: True)
    assert pi.read()["nvme0_temp_c"] is None
    os.makedirs(os.path.join(root, "sys/class/hwmon/hwmon7"))
    os.makedirs(os.path.join(root, "sys/devices/nvme0"))
    os.symlink(os.path.join(root, "sys/devices/nvme0"), os.path.join(root, "sys/class/hwmon/hwmon7/device"))
    for name, text in (("name", "nvme\n"), ("temp1_input", "41000\n")):
        with open(os.path.join(root, "sys/class/hwmon/hwmon7", name), "w") as f:
            f.write(text)
    values = [pi.read()["nvme0_temp_c"] for _ in range(61)]
    assert values[-1] == 41.0


def test_pi_sensors_on_this_machine():
    v = P.PiSensors().read()
    assert list(v) == list(P.COLUMNS)
    assert isinstance(v["load1"], float) and v["mem_avail_mb"] > 0 and v["sd_free_gb"] > 0
    assert P.clock_synced() in (True, False, None)
    if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
        assert 0 < v["soc_temp_c"] < 110


def test_background_reader_keeps_a_slow_read_away_from_the_caller():
    import time
    slow = threading.Event()
    reads = []

    def read():
        reads.append(time.monotonic())
        if len(reads) == 3:
            slow.wait(5)                    # a drive that takes its time
        if len(reads) == 5:
            raise OSError(5, "Input/output error")
        return {"n": len(reads)}
    reader = P.BackgroundReader(read, 0.05, uptime=time.monotonic)
    got, deadline = [], time.monotonic() + 5
    while len(got) < 2 and time.monotonic() < deadline:
        got += reader.take()
        time.sleep(0.01)
    assert [values for _, _, values in got[:2]] == [{"n": 1}, {"n": 2}]
    assert 0.03 < got[1][1] - got[0][1] < 0.5                   # at the interval, stamped when the read began
    started = time.monotonic()
    for _ in range(50):                                         # the third read hangs: take() never waits
        assert reader.take() == []
        time.sleep(0.005)
    assert time.monotonic() - started < 1.0
    slow.set()
    more, deadline = [], time.monotonic() + 5
    while len(more) < 3 and time.monotonic() < deadline:
        more += reader.take()
        time.sleep(0.01)
    assert more[0][2] == {"n": 3} and more[1][2] == {"n": 4}
    assert isinstance(more[2][2], OSError)                      # an error is handed over, and reading goes on
    reader.close()
    reader._thread.join(2)
    assert not reader._thread.is_alive()
