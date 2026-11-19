"""Stand-ins for the telemetry service's devices: an OLA on a serial port, gpsd, the Pi's /sys and /proc."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "data", "ola_session.jsonl")

DEFAULT_HEADER = ("rtcDate,rtcTime,aX,aY,aZ,gX,gY,gZ,mX,mY,mZ,imu_degC,gps_Date,gps_Time,gps_Lat,gps_Long,gps_Alt,"
                  "gps_SIV,gps_FixType,gps_GroundSpeed,gps_Heading,gps_pDOP,degC,A0mV,A1mV,A2mV,A3mV,output_Hz,")
CONFIGURED_HEADER = DEFAULT_HEADER.replace("A0mV,A1mV,A2mV,A3mV", "A0A3mV,A1A3mV,A2A3mV")


def configured_row(a0a3=140.0, a1a3=26.0, a2a3=2542.0, tmp117=20.1641, n=0):
    """One row; `n` counts tenths of a second on the OLA's own clock."""
    clock = 3 * 3600 + n // 10
    return (f"01/01/2000,{clock // 3600:02d}:{clock % 3600 // 60:02d}:{clock % 60:02d}.{n % 10}4,"
            f"-31.25,-9.28,-1020.02,-0.53,2.55,-0.91,7.95,72.0,-107.25,23.24,"
            f"11/19/2026,01:03:43.300,400000000,-1200000000,-28759,20,3,78,19098896,151,{tmp117},"
            f"{a0a3},{a1a3},{a2a3},9.5,\r\n")


def load_session(path=SESSION):
    """The recorded OLA session as a list of ("rx", bytes) and ("tx", bytes)."""
    script = []
    with open(path) as f:
        for line in f:
            entry = json.loads(line)
            for kind in ("rx", "tx"):
                if kind in entry:
                    script.append((kind, entry[kind].encode("latin-1")))
    return script


class FakeSerial:
    """A serial port that replays a script.

    Each read returns the next recorded chunk, as many per round as the bench has released
    (`release`), then nothing: like a tty, where a read returns what has arrived and no more.
    A "tx" entry holds the replay until the code under test has written those bytes, as the OLA
    waits for a key.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.written = b""
        self.all_written = []
        self.closed = False
        self.fail_read = None       # an exception to raise from read, as when the adapter is unplugged
        self.silent = False         # the OLA sends nothing and ignores what is written
        self.available = 0          # chunks that have "arrived" and can be read now
        self.reads = 0

    def release(self, chunks=1):
        """Called by the bench once per round: this many chunks have arrived since the last round."""
        self.available = chunks

    def feed(self, *chunks):
        self.script.extend(("rx", c if isinstance(c, bytes) else c.encode()) for c in chunks)

    def expect(self, data):
        self.script.append(("tx", data))

    def write(self, data):
        self.all_written.append(bytes(data))
        if not self.silent:
            self.written += data
        return len(data)

    def read(self, size=1):
        self.reads += 1
        if self.fail_read is not None:
            raise self.fail_read
        if self.silent:
            return b""
        if self.script and self.script[0][0] == "tx":
            want = self.script[0][1]
            if self.written.startswith(want):       # the key was pressed: the OLA answers next round
                self.written = self.written[len(want):]
                self.script.pop(0)
                self.available = 0
            return b""
        if not self.script or self.available <= 0:
            return b""
        self.available -= 1
        return self.script.pop(0)[1]

    def close(self):
        self.closed = True


class Ports:
    """Stands in for opening the serial port: hands out the fake ports in turn, or fails like a missing device."""

    def __init__(self, *ports):
        self.ports = list(ports)
        self.opened = []
        self.missing = False

    def __call__(self, port, baud):
        if self.missing or not self.ports:
            raise FileNotFoundError(2, "No such file or directory", port)
        self.opened.append(self.ports.pop(0))
        return self.opened[-1]

    def exists(self, path):
        return not self.missing

    def release(self, chunks=1):
        for port in self.opened:
            port.release(chunks)


TPV = {"class": "TPV", "device": "/dev/ttyACM0", "mode": 3, "time": "2026-11-19T01:05:40.600Z", "leapseconds": 18,
       "ept": 0.005, "lat": 40.0, "lon": -120.0, "altHAE": -21.628, "altMSL": 7.888, "alt": 7.888, "epx": 5.891,
       "epy": 13.38, "epv": 2.105, "track": 190.989, "speed": 0.082, "climb": -0.42, "eps": 267.61, "eph": 1.56,
       "sep": 33.756}
SKY = {"class": "SKY", "device": "/dev/ttyACM0", "time": "2026-11-19T01:05:40.600Z", "hdop": 0.96, "pdop": 1.63,
       "vdop": 1.31}


class FakeGpsd:
    """One client connection to a stand-in gpsd: answers ?WATCH and ?POLL as gpsd 3.25 does."""

    def __init__(self):
        self.tpv = dict(TPV)
        self.sky = dict(SKY)
        self.active = 1
        self.second = 0
        self.advance = True         # the fix time moves on between polls
        self.answer = True
        self.closed = False
        self.eof = False
        self.sent = []
        self._out = b'{"class":"VERSION","release":"3.25","rev":"3.25","proto_major":3,"proto_minor":15}\r\n'

    def sendall(self, data):
        if self.closed:
            raise BrokenPipeError(32, "Broken pipe")
        self.sent.append(data)
        if not self.answer:
            return
        if data.startswith(b"?WATCH"):
            self._out += (b'{"class":"DEVICES","devices":[]}\r\n'
                          b'{"class":"WATCH","enable":true,"json":false,"nmea":false,"raw":0}\r\n')
        elif data.startswith(b"?POLL"):
            if self.advance:
                self.second += 1
            tpvs, skies = [], []
            if self.active:
                tpv = dict(self.tpv)
                if tpv.get("time"):
                    tpv["time"] = "2026-11-19T01:%02d:%02d.000Z" % (self.second // 60, self.second % 60)
                tpvs, skies = [tpv], [self.sky]
            reply = {"class": "POLL", "time": "2026-11-19T01:05:40.761Z", "active": self.active,
                     "tpv": tpvs, "gst": [], "sky": skies}
            self._out += json.dumps(reply, separators=(",", ":")).encode() + b"\r\n"

    def recv(self, size):
        if self.eof:
            return b""
        if not self._out:
            raise BlockingIOError(11, "Resource temporarily unavailable")
        # In two parts, to exercise lines that arrive in pieces.
        cut = max(1, len(self._out) // 2) if len(self._out) > 40 else len(self._out)
        data, self._out = self._out[:cut], self._out[cut:]
        return data

    def close(self):
        self.closed = True


class GpsdServer:
    """Stands in for connecting to gpsd: a new FakeGpsd per connection, or "connection refused"."""

    def __init__(self):
        self.down = False
        self.connections = []
        self.prepare = None         # called with each new connection

    def __call__(self, host, port):
        if self.down:
            raise ConnectionRefusedError(111, "Connection refused")
        conn = FakeGpsd()
        if self.prepare:
            self.prepare(conn)
        self.connections.append(conn)
        return conn


def make_pi_tree(root, *, nvme=("nvme1", "nvme0"), fan=True, soc="50150\n", throttled="50005\n"):
    """A small /sys and /proc with what telemetry/pi.py reads. hwmon numbers are deliberately shuffled."""
    def put(path, text):
        path = os.path.join(root, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    if soc is not None:
        put("sys/class/thermal/thermal_zone0/temp", soc)
    if throttled is not None:
        put("sys/devices/platform/soc/soc:firmware/get_throttled", throttled)
    put("proc/loadavg", "0.32 0.38 0.23 1/345 12345\n")
    put("proc/meminfo", "MemTotal:       16608872 kB\nMemFree:        12 kB\nMemAvailable:   15387648 kB\n")
    put("sys/class/hwmon/hwmon0/name", "cpu_thermal\n")
    for i, controller in enumerate(nvme):
        put(f"sys/class/hwmon/hwmon{i + 1}/name", "nvme\n")
        put(f"sys/class/hwmon/hwmon{i + 1}/temp1_input", f"{39850 + 14000 * i}\n")
        os.makedirs(os.path.join(root, "sys/devices", controller), exist_ok=True)
        os.symlink(os.path.join(root, "sys/devices", controller), os.path.join(root, f"sys/class/hwmon/hwmon{i + 1}/device"))
    put("sys/class/hwmon/hwmon3/name", "rp1_adc\n")
    put("sys/class/hwmon/hwmon3/temp1_input", "49080\n")
    if fan:
        put("sys/class/hwmon/hwmon4/name", "pwmfan\n")
        put("sys/class/hwmon/hwmon4/fan1_input", "2933\n")
        put("sys/class/hwmon/hwmon4/pwm1", "75\n")
    return root
