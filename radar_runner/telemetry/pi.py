"""The Pi's own sensors, read from /sys and /proc without root and without starting a program.

Checked on this Pi 5 (Ubuntu 24.04, kernel 6.8.0-1068-raspi) as user englacial:

    soc_temp_c       /sys/class/thermal/thermal_zone0/temp
    throttled        /sys/devices/platform/soc/soc:firmware/get_throttled, the value of
                     `vcgencmd get_throttled` as hex. Bits now / since boot:
                     0 / 16 under-voltage, 1 / 17 frequency capped, 2 / 18 throttled, 3 / 19 soft temperature limit
    load1, load5     /proc/loadavg
    mem_avail_mb     MemAvailable in /proc/meminfo
    sd_free_gb       free space on the SD card (the file system of /)
    nvme_free_gb     free space on the data drive; empty when it is not mounted
    nvme0_temp_c,    hwmon devices named "nvme" (the drives' composite temperature), by controller name.
    nvme1_temp_c     Which controller holds the data drive can differ between boots
    fan_rpm, fan_pwm hwmon device "pwmfan" (pwm 0-255)
    rp1_temp_c       hwmon device "rp1_adc" (the Pi 5's I/O chip)
    clock_synced     1 when the kernel says a time daemon is keeping the clock (adjtimex), else 0

A value that cannot be read is None (an empty field in the log); nothing raises.

Reading a drive's temperature asks the drive itself, and the drive can take
its time: on the bench a read took up to 1.2 s now and then (2026-11-19).
`BackgroundReader` therefore does the reading in a thread of its own, and the
service loop only picks up finished samples: a slow drive delays the Pi
readings and nothing else.
"""
import collections
import ctypes
import os
import threading
import time

COLUMNS = ("soc_temp_c", "throttled", "load1", "load5", "mem_avail_mb", "sd_free_gb", "nvme_free_gb",
           "nvme0_temp_c", "nvme1_temp_c", "fan_rpm", "fan_pwm", "rp1_temp_c", "clock_synced")
THROTTLE_BITS = ((0, "under_voltage"), (1, "frequency_capped"), (2, "throttled"), (3, "soft_temp_limit"))
STA_UNSYNC = 0x0040


def throttle_flags(value):
    """Names of the conditions in a get_throttled value: (now, since boot)."""
    return ([name for bit, name in THROTTLE_BITS if value >> bit & 1],
            [name for bit, name in THROTTLE_BITS if value >> (bit + 16) & 1])


class _Timex(ctypes.Structure):
    # struct timex of 64-bit Linux; only `status` is used, the rest is padding to the right size.
    _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("rest", ctypes.c_byte * 208)]


def clock_synced():
    """True if the kernel's clock is marked synchronised (set by chrony or systemd-timesyncd), None if unknown."""
    try:
        timex = _Timex()            # modes = 0: read only
        if ctypes.CDLL(None, use_errno=True).adjtimex(ctypes.byref(timex)) < 0:
            return None
        return not timex.status & STA_UNSYNC
    except (OSError, AttributeError):
        return None


class PiSensors:
    def __init__(self, *, root="/", data_mount="/mnt/nvme", clock_synced=clock_synced):
        self.root = root
        self.data_mount = data_mount
        self._clock_synced = clock_synced
        self._hwmon = None
        self._reads = 0

    def _text(self, path):
        try:
            with open(os.path.join(self.root, path.lstrip("/"))) as f:
                return f.read(4096).strip()
        except (OSError, UnicodeDecodeError):
            return None

    def _scaled(self, path, scale):
        try:
            return round(int(self._text(path)) / scale, 2)
        except (TypeError, ValueError):
            return None

    def _find_hwmon(self):
        """Map hwmon devices by name; nvme devices by controller ("nvme0"). Numbers change between boots."""
        found = {}
        base = "sys/class/hwmon"
        try:
            entries = sorted(os.listdir(os.path.join(self.root, base)))
        except OSError:
            entries = []
        for entry in entries:
            name = self._text(f"{base}/{entry}/name")
            if name == "nvme":
                try:
                    name = os.path.basename(os.readlink(os.path.join(self.root, base, entry, "device")))
                except OSError:
                    continue
            if name:
                found.setdefault(name, f"{base}/{entry}")
        return found

    def _free_gb(self, path, must_be_mount=False):
        try:
            if must_be_mount and not os.path.ismount(path):
                return None
            st = os.statvfs(path)
            return round(st.f_bavail * st.f_frsize / 1e9, 2)
        except OSError:
            return None

    def read(self):
        """One sample: a dict with every name in COLUMNS."""
        if self._hwmon is None or self._reads % 60 == 0:    # a drive can appear or go
            self._hwmon = self._find_hwmon()
        self._reads += 1
        hw = self._hwmon
        values = dict.fromkeys(COLUMNS)
        values["soc_temp_c"] = self._scaled("sys/class/thermal/thermal_zone0/temp", 1000)
        throttled = self._text("sys/devices/platform/soc/soc:firmware/get_throttled")
        try:
            values["throttled"] = "0x%x" % int(throttled, 16)
        except (TypeError, ValueError):
            pass
        try:
            load = self._text("proc/loadavg").split()
            values["load1"], values["load5"] = float(load[0]), float(load[1])
        except (AttributeError, IndexError, ValueError):
            pass
        for line in (self._text("proc/meminfo") or "").split("\n"):
            if line.startswith("MemAvailable:"):
                try:
                    values["mem_avail_mb"] = int(line.split()[1]) // 1024
                except (IndexError, ValueError):
                    pass
        values["sd_free_gb"] = self._free_gb("/")
        values["nvme_free_gb"] = self._free_gb(self.data_mount, must_be_mount=True) if self.data_mount else None
        for name in ("nvme0", "nvme1"):
            if name in hw:
                values[name + "_temp_c"] = self._scaled(hw[name] + "/temp1_input", 1000)
        if "pwmfan" in hw:
            values["fan_rpm"] = self._scaled(hw["pwmfan"] + "/fan1_input", 1)
            values["fan_pwm"] = self._scaled(hw["pwmfan"] + "/pwm1", 1)
        if "rp1_adc" in hw:
            values["rp1_temp_c"] = self._scaled(hw["rp1_adc"] + "/temp1_input", 1000)
        synced = self._clock_synced()
        values["clock_synced"] = None if synced is None else int(synced)
        if values["soc_temp_c"] is None:
            self._hwmon = None          # look for the devices again next time
        return values


class BackgroundReader:
    """Calls `read` every `interval_s` in its own thread and keeps the results for `take`."""

    def __init__(self, read, interval_s, *, wall=time.time, uptime=None):
        self._read = read
        self._interval_s = interval_s
        self._wall = wall
        self._uptime = uptime if uptime is not None else (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self._samples = collections.deque(maxlen=8)     # appending and popping are safe between threads
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pi-sensors", daemon=True)
        self._thread.start()

    def _run(self):
        next_read = time.monotonic()
        while not self._stop.is_set():
            wall, uptime = self._wall(), self._uptime()     # when the read began
            try:
                result = self._read()
            except Exception as e:      # handed to the loop, which reports it as this source's error
                result = e
            self._samples.append((wall, uptime, result))
            next_read = max(next_read + self._interval_s, time.monotonic())
            self._stop.wait(next_read - time.monotonic())

    def take(self):
        """The samples finished since the last call: a list of (wall, uptime, values or exception)."""
        samples = []
        while self._samples:
            samples.append(self._samples.popleft())
        return samples

    def close(self):
        self._stop.set()
