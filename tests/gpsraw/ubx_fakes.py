"""Synthetic u-blox messages for the tests: NAV-PVT, NAV-EOE, RXM-RAWX, RXM-SFRBX, and drives built from them.

The values are plausible, not physical: pseudoranges and carrier phases that
move steadily, enough for gpsd to report fixes and for a converter to write
observations.
"""
import math
import struct
import time

from radar_runner.gpsraw import ubx

LEAP_S = 18
C = 299792458.0
L1 = 1575.42e6


def gps_week_tow(unix):
    gps = unix - ubx.GPS_EPOCH_UNIX + LEAP_S
    return int(gps // 604800), gps % 604800


def nav_pvt(unix, lat, lon, speed, heading=90.0, fix=True, hacc_m=0.8, sacc_mps=0.1, num_sv=20):
    """NAV-PVT (92 bytes) for UTC `unix`; fix=False sends "no fix"."""
    _, tow = gps_week_tow(unix)
    itow = int(round(tow * 1000)) % (604800 * 1000)
    tm = time.gmtime(int(unix))
    nano = int(round((unix % 1) * 1e9))
    if nano >= 500_000_000:         # the receiver rounds to the nearest second and gives a negative nano
        tm = time.gmtime(int(unix) + 1)
        nano -= 1_000_000_000
    valid = 0x07                    # date, time, fully resolved
    fix_type, flags = (3, 0x01) if fix else (0, 0x00)
    vel_n = speed * math.cos(math.radians(heading))
    vel_e = speed * math.sin(math.radians(heading))
    payload = struct.pack("<IHBBBBBBIiBBBBiiiiIIiiiiiIIHBBBBBBihH",
                          itow, tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, valid,
                          20, nano, fix_type, flags, 0xEA, num_sv if fix else 0,
                          int(lon * 1e7), int(lat * 1e7), 2000_000, 1970_000,
                          int(hacc_m * 1000), int(hacc_m * 1500),
                          int(vel_n * 1000), int(vel_e * 1000), 0, int(speed * 1000), int(heading * 1e5),
                          int(sacc_mps * 1000), 50_000, 120, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert len(payload) == 92, len(payload)
    return ubx.frame(0x01, 0x07, payload)


def nav_eoe(unix):
    _, tow = gps_week_tow(unix)
    return ubx.frame(0x01, 0x61, struct.pack("<I", int(round(tow * 1000)) % (604800 * 1000)))


def rawx(unix, n_sats=8, cp_valid=True, t0=None):
    """RXM-RAWX with `n_sats` GPS L1 C/A measurements."""
    week, tow = gps_week_tow(unix)
    dt = 0.0 if t0 is None else unix - t0
    meas = b""
    for i in range(n_sats):
        rng = 20_000_000.0 + 500_000.0 * i + 300.0 * dt
        doppler = -300.0 / C * L1
        cp = rng / (C / L1)
        trk = 0x01 | (ubx.CP_VALID | 0x04 if cp_valid else 0)
        meas += struct.pack("<ddfBBBBHBBBBBB", rng, cp, doppler, 0, 2 + i, 0, 0, 60000, 45, 5, 2, 3, trk, 0)
    head = struct.pack("<dHbBBB2s", tow, week, LEAP_S, n_sats, 0x01, 1, b"\0\0")
    return ubx.frame(0x02, 0x15, head + meas)


def sfrbx(sv=2, words=10):
    payload = struct.pack("<BBBBBBBB", 0, sv, 0, 0, words, 0, 2, 0) + b"".join(
        struct.pack("<I", 0x22C00000 + k) for k in range(words))
    return ubx.frame(0x02, 0x13, payload)


def drive(segments, start_unix=1795136400.0, rate_hz=1.0, lat=-79.5, lon=-112.0, raw=False):
    """Bytes of a receiver output for a drive. segments: [(seconds, speed m/s, fix ok, hAcc m), ...]."""
    out = []
    t, east = start_unix, 0.0
    step = 1.0 / rate_hz
    for seconds, speed, fix, hacc in segments:
        for _ in range(int(round(seconds * rate_hz))):
            east += speed * step
            lon_now = lon + east / (111_320.0 * math.cos(math.radians(lat)))
            out.append(nav_pvt(t, lat, lon_now, speed, fix=fix, hacc_m=hacc))
            if raw:
                out.append(rawx(t, t0=start_unix))
                out.append(sfrbx())
            out.append(nav_eoe(t))
            t += step
    return b"".join(out)


class RawGpsd:
    """A stand-in for gpsd's raw pass-through on a real TCP port. Each connection is answered like
    gpsd 3.25 answers the WATCH, then sent the next of `streams` in pieces of `chunk` bytes; with
    `close` the connection is then closed (gpsd restarting), otherwise held open with nothing more."""

    def __init__(self, streams, chunk=37, pause_s=0.0, close=False):
        import socket
        import threading
        self.streams, self.chunk, self.pause_s, self.close_after = list(streams), chunk, pause_s, close
        self.watches = []
        self.connections = 0
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self._open = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            self._open.append(conn)
            try:
                conn.sendall(b'{"class":"VERSION","release":"3.25","rev":"3.25","proto_major":3,"proto_minor":15}\r\n')
                self.watches.append(conn.makefile("rb").readline())
                conn.sendall(b'{"class":"DEVICES","devices":[{"class":"DEVICE","path":"/dev/ttyACM0"}]}\r\n'
                             b'{"class":"WATCH","enable":true,"json":false,"nmea":false,"raw":2}\r\n')
                data = self.streams.pop(0) if self.streams else b""
                for i in range(0, len(data), self.chunk):
                    conn.sendall(data[i:i + self.chunk])
                    if self.pause_s:
                        time.sleep(self.pause_s)
                if self.close_after:
                    conn.close()
            except OSError:
                pass

    def close(self):
        import socket
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.server.close()
        for conn in self._open:
            try:
                conn.close()
            except OSError:
                pass


# ------------------------------------------------------------------ a private gpsd, by gpsfake

def alive(pid):
    """True if the process exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def children(pid):
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(p) for p in f.read().split()]
    except OSError:
        return []


def gpsfake_start(log_path, cycle_s):
    """gpsfake feeding `log_path` to a private gpsd on a free port, with gpsfake's own control socket
    (/tmp/gpsfake-<pid>.sock). Returns (process, port). The receiver is a TCP stand-in: the system's
    AppArmor profile for gpsd allows no pseudo-terminal."""
    import socket
    import subprocess
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(["gpsfake", "-t", "-1", "-q", "-P", str(port), "-c", str(cycle_s), str(log_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return proc, port
        except OSError:
            time.sleep(0.1)
    gpsfake_stop(proc)
    raise AssertionError("gpsfake's gpsd did not come up")


def gpsfake_stop(proc):
    """SIGTERM to the private gpsd, which then removes its control socket and shared memory, and to
    gpsfake; SIGKILL only as the last resort. Each by its own PID."""
    import os
    import signal
    daemons = children(proc.pid)
    for pid in daemons + [proc.pid]:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and (proc.poll() is None or any(alive(p) for p in daemons)):
        time.sleep(0.05)
    for pid in daemons + [proc.pid]:
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    proc.wait(10)
