"""Gates: the requirements to start the radar and to keep it running.

Each gate is an object with a `name` and a `check()` that returns a
`GateResult` (logic.py): `start` says why a start is refused, `run` why a
running radar must stop; both empty means good. A gate that raises counts as
failed (fail closed). Gates only read: status files, sysfs, the file system.

    telemetry   amplifier telemetry is fresh (telemetry service's status file),
                or the user override "run without amplifier telemetry" is set
    watcher     the safety watcher's status is fresh and not tripped
    power       the power service's status is fresh, normal, and asks for no stop
    data        the data drive is mounted, writable, and has free space
    sdr         the SDR is on USB at USB 3 speed (start only)
    config      the requested radar config passes the limits (start only)
    orca        the ORCA checkout is in place (start only)
    amp_safe    the "make the amplifier safe" command is configured (start only)

The telemetry and watcher gates read status files whose formats are not
final: the path and the field names are settings (`manager.gates.*`).
"""
import os

from radar_runner.common.status import read_status, status_path

from . import limits as limits_module
from .logic import GateResult

OVERRIDE_NO_AMP_TELEMETRY = "override-no-amp-telemetry"     # flag file in the state directory


# ---------------------------------------------------------------------- override

class Overrides:
    """User overrides that persist across reboots: one flag file each in the state directory.

    The file's existence is the override, so a damaged file cannot clear it.
    Other services (the safety watcher) may test for the same file.
    """

    def __init__(self, state_dir):
        self.state_dir = state_dir

    def path(self, name=OVERRIDE_NO_AMP_TELEMETRY):
        return os.path.join(self.state_dir, name)

    def is_set(self, name=OVERRIDE_NO_AMP_TELEMETRY):
        try:
            return os.path.lexists(self.path(name))
        except OSError:
            return False

    def set(self, value, note="", name=OVERRIDE_NO_AMP_TELEMETRY):
        """Set or clear. Returns "" or the error text."""
        path = self.path(name)
        try:
            if value:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                try:
                    os.write(fd, (note + "\n").encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
            else:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            dir_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return ""
        except OSError as e:
            return f"{path}: {e.strerror}"

    def snapshot(self):
        return {"no_amp_telemetry": self.is_set()}


# ------------------------------------------------------------------- status files

def _lookup(data, dotted):
    """Value at a dotted path such as "amp.fresh"; KeyError if absent."""
    for part in dotted.split("."):
        if not isinstance(data, dict):
            raise KeyError(dotted)
        data = data[part]
    return data


class StatusFileGate:
    """A service's status file must be fresh, and some of its fields true or false.

    Fails closed: a missing, old or damaged file, a missing field and a field
    that is not exactly true/false all count as "not good".
    """

    def __init__(self, name, path, cfg, what, read=read_status):
        self.name = name
        self.path = path
        self.cfg = cfg
        self.what = what            # for the reason text, e.g. "amplifier telemetry"
        self._read = read

    def problem(self):
        r = self._read(self.path, self.cfg["max_age_s"])
        if not r.ok:
            return f"{self.what}: status {r.state} ({r.error})"
        failed = []
        for wanted, fields in ((True, self.cfg["require_true"]), (False, self.cfg["require_false"])):
            for dotted in fields:
                try:
                    value = _lookup(r.data, str(dotted))
                except KeyError:
                    failed.append(f"{dotted} missing")
                    continue
                if value is not wanted:
                    failed.append(f"{dotted} is {str(value).lower() if isinstance(value, bool) else 'not true/false'}")
        if not failed:
            return ""
        detail = ""
        if self.cfg["reason_field"]:
            try:
                detail = str(_lookup(r.data, self.cfg["reason_field"]) or "")[:120]
            except KeyError:
                pass
        return f"{self.what}: " + (detail or ", ".join(failed))

    def check(self):
        problem = self.problem()
        return GateResult(start=problem, run=problem)


class TelemetryGate(StatusFileGate):
    """Amplifier telemetry, with the user override "run without amplifier telemetry"."""

    def __init__(self, path, cfg, overrides, read=read_status):
        super().__init__("telemetry", path, cfg, "amplifier telemetry", read)
        self.overrides = overrides

    def check(self):
        problem = self.problem()
        if problem and self.overrides.is_set():
            return GateResult(note=f"OVERRIDE, running without amplifier telemetry ({problem})", kind="override")
        return GateResult(start=problem, run=problem)


class PowerGate:
    """The power service: stale, not normal, or a stop request means stop now and do not start."""
    name = "power"

    def __init__(self, path, max_age_s, read=read_status):
        self.path = path
        self.max_age_s = max_age_s
        self._read = read

    def check(self):
        r = self._read(self.path, self.max_age_s)
        if not r.ok:
            problem = f"power service: status {r.state} ({r.error})"
            return GateResult(start=problem, run=problem)
        state = r.data.get("state")
        stop = r.data.get("radar_stop_requested")
        if stop is not False or state != "normal":
            problem = f"power service: state {state}" + (", radar stop requested" if stop is not False else "")
            return GateResult(start=problem, run=problem, kind="power_fail")
        return GateResult()


# --------------------------------------------------------------------- data drive

class DataGate:
    """The data drive: mounted (never the SD card), writable, with free space."""
    name = "data"

    def __init__(self, cfg, ismount=os.path.ismount, statvfs=os.statvfs):
        self.cfg = cfg
        self._ismount = ismount
        self._statvfs = statvfs

    def check(self):
        cfg = self.cfg
        mount, root = cfg["mount_point"], cfg["root"]
        if mount:       # "" (only possible in tests: a file cannot set it) skips the mount check
            if not self._ismount(mount):
                problem = f"data drive not mounted at {mount}"
                return GateResult(start=problem, run=problem)
            real_root, real_mount = os.path.realpath(root), os.path.realpath(mount)
            if os.path.commonpath([real_root, real_mount]) != real_mount:
                problem = f"data root {root} is not on the data drive {mount}"
                return GateResult(start=problem, run=problem)
        try:
            os.makedirs(root, exist_ok=True)
            st = self._statvfs(root)
        except OSError as e:
            problem = f"data root {root}: {e.strerror}"
            return GateResult(start=problem, run=problem)
        if st.f_flag & os.ST_RDONLY or not os.access(root, os.W_OK | os.X_OK):
            problem = f"data root {root} is not writable"
            return GateResult(start=problem, run=problem)
        free_gb = st.f_bavail * st.f_frsize / 1e9
        note = f"{free_gb:.1f} GB free"
        if free_gb < cfg["stop_min_free_gb"]:
            problem = f"data drive full: {free_gb:.1f} GB free, stop below {cfg['stop_min_free_gb']:g} GB"
            return GateResult(start=problem, run=problem, note=note)
        if free_gb < cfg["start_min_free_gb"]:
            return GateResult(start=f"data drive nearly full: {free_gb:.1f} GB free, "
                                    f"no start below {cfg['start_min_free_gb']:g} GB", note=note)
        return GateResult(note=note)


# ---------------------------------------------------------------------------- SDR

def _read_attr(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


class SdrGate:
    """The SDR is on USB, at USB 3 speed. Read from sysfs; the device is never opened.

    Start only: while the radar starts, the SDR leaves the bus for a moment to
    load its firmware, and a running radar reports a lost SDR by itself.
    """
    name = "sdr"

    def __init__(self, cfg):
        self.cfg = cfg

    def find(self):
        """Return [(sysfs name, speed in Mbit/s, product text)] of every matching device."""
        cfg = self.cfg
        found = []
        try:
            names = sorted(os.listdir(cfg["sysfs_dir"]))
        except OSError:
            return found
        products = [str(p).lower() for p in cfg["usb_products"]]
        for name in names:
            base = os.path.join(cfg["sysfs_dir"], name)
            if _read_attr(os.path.join(base, "idVendor")).lower() != cfg["usb_vendor"].lower():
                continue
            if _read_attr(os.path.join(base, "idProduct")).lower() not in products:
                continue
            try:
                speed = int(float(_read_attr(os.path.join(base, "speed"))))
            except ValueError:
                speed = 0
            found.append((name, speed, _read_attr(os.path.join(base, "product"))))
        return found

    def check(self):
        cfg = self.cfg
        found = self.find()
        if not found:
            return GateResult(start="SDR not found on USB")
        if len(found) > 1:
            return GateResult(start=f"{len(found)} SDRs found on USB; expected one")
        name, speed, product = found[0]
        if speed >= cfg["min_speed_mbps"]:
            return GateResult(note=f"USB {name}, {speed} Mbit/s")
        if product == cfg["bootloader_product"]:
            return GateResult(note=f"USB {name}: firmware not loaded yet, link speed known after the first start")
        return GateResult(start=f"SDR on a USB 2 link ({speed} Mbit/s at {name})")


# ------------------------------------------------------------- config, ORCA, tool

class ConfigGate:
    """The requested radar config passes the limits. Re-read only when the file changes."""
    name = "config"

    def __init__(self, path, limits):
        self.path = path
        self.limits = limits
        self._key = None
        self._problem = ""

    def check(self):
        try:
            st = os.stat(self.path)
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError as e:
            self._key = None
            return GateResult(start=f"radar config {self.path}: {e.strerror}")
        if key != self._key:
            try:
                problems = limits_module.check(limits_module.load_file(self.path), self.limits)
            except limits_module.ConfigError as e:
                problems = [str(e)]
            self._problem = ""
            if problems:
                more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
                self._problem = f"radar config {self.path}: {problems[0]}{more}"
            self._key = key
        return GateResult(start=self._problem)


class OrcaGate:
    """The ORCA checkout and the directories the commands run in exist."""
    name = "orca"

    def __init__(self, cfg):
        self.cfg = cfg

    def check(self):
        cfg = self.cfg
        for sub in ("chirp_cwd", "build_cwd", "radar_cwd"):
            path = os.path.join(cfg["dir"], cfg[sub])
            if not os.path.isdir(path):
                return GateResult(start=f"ORCA directory {path} not found")
        if len(os.path.normpath(cfg["radar_cwd"]).split(os.sep)) != 2:
            return GateResult(start="orca.radar_cwd must be two levels below orca.dir (the radar resolves "
                                    "its paths from ../../)")
        return GateResult()


class AmpSafeGate:
    """The "make the amplifier safe" command is configured: without it the radar is never started."""
    name = "amp_safe"

    def __init__(self, cfg):
        self.cfg = cfg

    def check(self):
        command = self.cfg["command"]
        if not command or not all(isinstance(a, str) and a for a in command):
            return GateResult(start="the make-amplifier-safe command is not configured (manager.amp_safe.command)")
        if not (os.path.isfile(command[0]) and os.access(command[0], os.X_OK)):
            return GateResult(start=f"the make-amplifier-safe command {command[0]} is not an executable file")
        return GateResult()


# ----------------------------------------------------------------------- assembly

def build(config, overrides):
    """The gates for a loaded config (the whole config, with the `manager` section)."""
    m = config["manager"]
    g = m["gates"]
    run_dir = config["paths"]["run_dir"]
    return [
        TelemetryGate(g["telemetry"]["status_file"] or status_path("telemetry", run_dir), g["telemetry"], overrides),
        StatusFileGate("watcher", g["watcher"]["status_file"] or status_path("watcher", run_dir), g["watcher"],
                       "safety watcher"),
        PowerGate(config["paths"]["power_status_file"], g["power"]["max_age_s"]),
        DataGate(g["data"]),
        SdrGate(g["sdr"]),
        ConfigGate(m["radar_config"], m["limits"]),
        OrcaGate(m["orca"]),
        AmpSafeGate(m["amp_safe"]),
    ]


def evaluate(gates):
    """Check every gate. Returns {name: GateResult} in order. Never raises."""
    results = {}
    for gate in gates:
        try:
            results[gate.name] = gate.check()
        except Exception as e:      # fail closed
            problem = f"{gate.name} gate: internal error ({type(e).__name__}: {e})"
            results[gate.name] = GateResult(start=problem, run=problem)
    return results
