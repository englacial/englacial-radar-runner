"""Collection directories: names, the metadata file, and durable small writes.

One directory per collection under the data root:

    b00042_s003_20261119T015500Z/
        collection.json         what, when, why, and how it ended (below)
        config.yaml             the config the radar was given (derived by the manager)
        config_requested.yaml   the requested config, byte for byte
        chirp.bin               the transmitted pulse
        rx_samps.bin.0, .1 ...  the data, in chunks, written by the radar itself
        uhd_stdout.log, .1 ...  the radar's output, each line as "[unix time] \\t<line>"
        uhd_stderr.log          UHD's own messages
        prepare.log             chirp generation and build output
        amp_safe.log            output of the "make the amplifier safe" command
        gps_log.txt             created (empty) by the radar

The name carries the boot counter and a sequence number within the boot, so
names are unique and sort in the order of recording even when the clock is
wrong; the UTC time is for people.

collection.json is written when the directory is created, with "end": null,
and rewritten when the radar has started and when the collection is over. A
file that still says "end": null belongs to a collection that was cut off
(power cut, crash): its data is valid up to the last complete record.
"""
import json
import os
import re
import time

from radar_runner.common.status import boottime_s, current_boot_id

NAME_RE = re.compile(r"^b(\d{5,})_s(\d{3,})_")
META_FILE = "collection.json"


# --------------------------------------------------------------------- boot number

def _placeholder_boot_count(state_dir, floor=0, boot_id=None):
    """PLACEHOLDER, to be deleted at merge: the shared helper is radar_runner.common.bootcount.

    Same file and format (<state_dir>/boot_count.json: {"count", "boot_id"}),
    without the locking, so the two agree if both are used during the change-over.
    """
    boot_id = current_boot_id() if boot_id is None else boot_id
    path = os.path.join(state_dir, "boot_count.json")
    count, file_boot = 0, ""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read(4096))
        if type(data["count"]) is int and data["count"] >= 0:
            count, file_boot = data["count"], str(data.get("boot_id", ""))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if count and boot_id and file_boot == boot_id:
        return count, ""
    new = max(count, floor) + 1
    try:
        write_durable(path, json.dumps({"count": new, "boot_id": boot_id}) + "\n")
    except OSError as e:
        return new, f"cannot keep the boot counter in {state_dir}: {e.strerror}"
    return new, ""


def boot_number(state_dir, floor=0):
    """Return (boot counter, problem text). The one place the manager gets its boot number from."""
    try:
        from radar_runner.common.bootcount import boot_count    # the shared helper (arrives at merge)
    except ImportError:
        return _placeholder_boot_count(state_dir, floor)
    return boot_count(state_dir, floor=floor)


# -------------------------------------------------------------------------- names

def existing(root):
    """Return [(boot, sequence, name)] of the collection directories in `root`."""
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        m = NAME_RE.match(name)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), name))
    return out


def create(root, state_dir, wall=time.time, boot_number=boot_number):
    """Create the next collection directory. Returns (path, boot, sequence, problem text).

    Raises OSError if the directory cannot be created.
    """
    found = existing(root)
    # A boot counter that was lost (new SD card, old data drive) must not reuse numbers.
    boot, problem = boot_number(state_dir, floor=max((b for b, _, _ in found), default=0))
    seq = max((s for b, s, _ in found if b == boot), default=0) + 1
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(wall()))
    for _ in range(1000):
        path = os.path.join(root, f"b{boot:05d}_s{seq:03d}_{stamp}")
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            seq += 1
            continue
        fsync_dir(root)
        return path, boot, seq, problem
    raise FileExistsError(f"no free collection name in {root}")


# ------------------------------------------------------------------ durable writes

def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_durable(path, text):
    """Replace `path` atomically and sync it: a power cut leaves the old or the new file."""
    data = text.encode() if isinstance(text, str) else text
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(os.path.dirname(path) or ".")


def stamp(wall=time.time, uptime=boottime_s):
    now = wall()
    return {"time": round(now, 3), "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "uptime_s": round(uptime(), 3)}


class Metadata:
    """collection.json: written at the start, rewritten at each later step."""

    def __init__(self, directory, data):
        self.path = os.path.join(directory, META_FILE)
        self.data = dict(data)
        self.data.setdefault("format", 1)
        self.data.setdefault("flags", [])
        self.data.setdefault("recording_started", None)
        self.data.setdefault("end", None)
        self.error = ""

    def add_flag(self, flag):
        if flag not in self.data["flags"]:
            self.data["flags"].append(flag)

    def write(self):
        """Returns "" or the error text; never raises."""
        try:
            write_durable(self.path, json.dumps(self.data, indent=1) + "\n")
            self.error = ""
        except (OSError, ValueError, TypeError) as e:
            self.error = f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"
        return self.error


def git_commit(directory):
    """Commit of a checkout, read from .git without running git. "" if unknown."""
    try:
        git = os.path.join(directory, ".git")
        with open(os.path.join(git, "HEAD")) as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head[:40]
        ref = head[5:]
        try:
            with open(os.path.join(git, ref)) as f:
                return f.read().strip()[:40]
        except OSError:
            with open(os.path.join(git, "packed-refs")) as f:
                for line in f:
                    if line.strip().endswith(" " + ref):
                        return line.split()[0][:40]
    except OSError:
        pass
    return ""
