"""Status files: how the services share their latest state.

Each service publishes one small JSON file. Only that service writes it.
Everything else reads it and must treat an old file as "no information".

    /run/radar-runner/power.json            the power service (runs as root)
    /run/radar-runner/user/<service>.json   every other service (run as englacial)

/run/radar-runner belongs to root and only root writes in it; the `user`
directory inside belongs to englacial and root never writes there. Neither
side can therefore get in the way of the other's files.

The writer adds these fields to whatever the service publishes:

    time, time_utc   wall clock (Unix seconds, and ISO text for people)
    uptime_s         seconds since boot (CLOCK_BOOTTIME)
    boot_id          the kernel's ID of this boot
    pid              the writing process

The age of a file is taken from `uptime_s`, not from the wall clock: this Pi
has no clock battery, so the wall clock is stepped (by minutes or years) when
GPS or the network sets it, and a wall-clock age would make every status look
stale, or fresh, at that moment. A file without `uptime_s` falls back to `time`.
The power service's file (power/service.py) has `time` and `uptime_s` and no
`boot_id`; it is read the same way.
"""
import glob
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass

DEFAULT_RUN_DIR = "/run/radar-runner/user"                 # services run as englacial
POWER_STATUS_FILE = "/run/radar-runner/power.json"         # written by the power service (root)

OK = "ok"              # readable and no older than the caller allows
STALE = "stale"        # readable but too old, from another boot, or from the future
MISSING = "missing"    # no such file
CORRUPT = "corrupt"    # unreadable, not JSON, not an object, or no usable timestamp

FUTURE_TOLERANCE_S = 1.0   # a timestamp this far ahead of now is rounding; further is stale
MAX_BYTES = 1 << 20


def status_path(service, run_dir=DEFAULT_RUN_DIR):
    return os.path.join(run_dir, service + ".json")


def boottime_s():
    """Seconds since boot, including time suspended. Not affected by clock changes."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def current_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_all(fd, data):
    """os.write may write only a part; keep going until everything is written."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class StatusWriter:
    """Writes one status file atomically: readers see the old or the new file, never a part.

    Each write goes to a new temporary file in the same directory (created
    exclusively, under a name nobody can predict) and is then renamed over the
    status file. `write` never raises. It returns "" or the error text, and
    keeps the last result in `error`, so that a service can log a failure
    once, not every time.
    """

    def __init__(self, path, *, wall=time.time, uptime=boottime_s, boot_id=None):
        self.path = path
        self.error = ""
        self._dir = os.path.dirname(path) or "."
        self._tmp_prefix = "." + os.path.basename(path) + "."
        self._wall = wall
        self._uptime = uptime
        self._boot_id = current_boot_id() if boot_id is None else boot_id
        # Temporary files left by an earlier run of this service that was killed mid-write.
        for stale in glob.glob(os.path.join(glob.escape(self._dir), glob.escape(self._tmp_prefix) + "*.tmp")):
            try:
                os.unlink(stale)
            except OSError:
                pass

    def write(self, data):
        tmp = None
        try:
            wall = self._wall()
            body = dict(data)
            body.update({"time": round(wall, 3),
                         "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall)),
                         "uptime_s": round(self._uptime(), 3),
                         "boot_id": self._boot_id,
                         "pid": os.getpid()})
            text = json.dumps(body, indent=1, allow_nan=False) + "\n"
            fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=self._tmp_prefix, suffix=".tmp")
            try:
                _write_all(fd, text.encode())
                os.fchmod(fd, 0o644)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            tmp = None
            self.error = ""
        except (OSError, ValueError, TypeError) as e:
            # Without the temporary file's name, so that a repeated failure gives the same text.
            self.error = f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return self.error


@dataclass
class StatusRead:
    state: str            # OK, STALE, MISSING or CORRUPT
    data: dict = None     # the file's content when it could be parsed (also when stale)
    age_s: float = None   # None when unknown
    error: str = ""       # why, for anything but OK

    @property
    def ok(self):
        return self.state == OK


def _number(value):
    return (type(value) in (int, float)) and math.isfinite(value)


def read_status(path, max_age_s, *, wall=time.time, uptime=boottime_s, boot_id=None):
    """Read a status file. Never raises.

    The result is OK only if the file could be parsed and is at most
    `max_age_s` old. Callers decide with `.ok`; `.data` is also returned for a
    stale file so that it can be shown, never to be acted on.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return StatusRead(MISSING, error="no such file")
    except OSError as e:
        return StatusRead(CORRUPT, error=f"cannot read: {e}")
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("file too large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (ValueError, RecursionError) as e:
        return StatusRead(CORRUPT, error=f"cannot parse: {e}")

    if _number(data.get("uptime_s")):
        age = uptime() - data["uptime_s"]
        this_boot = current_boot_id() if boot_id is None else boot_id
        file_boot = data.get("boot_id")
        if file_boot and this_boot and file_boot != this_boot:
            return StatusRead(STALE, data, None, "written during another boot")
    elif _number(data.get("time")):
        age = wall() - data["time"]
    else:
        return StatusRead(CORRUPT, data, None, "no uptime_s or time field")

    if age < -FUTURE_TOLERANCE_S:
        return StatusRead(STALE, data, round(age, 3), "timestamp is in the future")
    age = round(max(age, 0.0), 3)
    if age > max_age_s:
        return StatusRead(STALE, data, age, f"{age:.1f} s old, limit {max_age_s:g} s")
    return StatusRead(OK, data, age)
