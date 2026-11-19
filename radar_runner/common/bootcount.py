"""Boot counter: a number that goes up by one at every boot of this Pi.

The Pi's clock has no battery, so a time in a file or directory name is not
unique and not ordered: two boots can start at the same restored time. Names
that must stay unique and ordered carry the boot counter instead, for example
`b00042` in the sensor log names (and, later, in the collection names).

    count, problem = boot_count(config["paths"]["state_dir"])

The counter is kept in <state_dir>/boot_count.json with the kernel's ID of the
boot that it was last raised in. The first caller in a new boot raises it; every
later caller in the same boot, in any service, gets the same number.

`floor` is for a counter that was lost or rolled back (a new or cloned SD card
with the old data drive): pass the highest number already used in the names on
the data drive, and a new boot gets at least one more than that.

Nothing here raises. If the file cannot be read or written, the count is still
returned (0 if nothing better is known) together with the reason, and the
caller must then make its names unique in another way as well (the sensor
logger never reuses a file name that exists).
"""
import fcntl
import json
import os

from .status import current_boot_id

FILE_NAME = "boot_count.json"


def _read(path):
    """Return (count, boot_id) from the file, or (None, "") if there is no usable file."""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read(4096))
        count = data["count"]
        if type(count) is not int or count < 0:
            return None, ""
        return count, str(data.get("boot_id", ""))
    except (OSError, ValueError, KeyError, TypeError):
        return None, ""


def _write(path, count, boot_id):
    """Replace the file atomically and make it durable: a power cut leaves the old or the new file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, (json.dumps({"count": count, "boot_id": boot_id}) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def boot_count(state_dir, *, floor=0, boot_id=None):
    """Return (count, problem). `problem` is "" unless the counter could not be kept on disk."""
    boot_id = current_boot_id() if boot_id is None else boot_id
    path = os.path.join(state_dir, FILE_NAME)
    known = 0
    try:
        # One writer at a time: several services start together at boot.
        lock = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        count, file_boot = _read(path)
        if count is not None and boot_id and file_boot == boot_id:
            return count, ""
        return max(count or 0, floor), f"cannot keep the boot counter in {state_dir}: {e.strerror}"
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        count, file_boot = _read(path)
        known = count or 0
        if count is not None and boot_id and file_boot == boot_id:
            return count, ""                    # already raised in this boot
        new = max(known, floor) + 1
        _write(path, new, boot_id)
        if not boot_id:
            return new, "the kernel boot ID is not readable: the counter goes up at every start"
        return new, ""
    except OSError as e:
        return max(known, floor) + 1, f"cannot keep the boot counter in {state_dir}: {e.strerror}"
    finally:
        os.close(lock)      # also releases the lock


def raise_boot_count(state_dir, at_least, *, boot_id=None):
    """Raise this boot's number to `at_least` (never lower it). Returns (count, problem).

    For the case that the number turned out to be in use already: the sensor logger found a
    directory of another boot under it, on a drive that was not there when the counter was read.
    """
    boot_id = current_boot_id() if boot_id is None else boot_id
    path = os.path.join(state_dir, FILE_NAME)
    try:
        lock = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        return at_least, f"cannot keep the boot counter in {state_dir}: {e.strerror}"
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        count, _ = _read(path)
        new = max(count or 0, at_least)
        if new != count:
            _write(path, new, boot_id)
        return new, ""
    except OSError as e:
        return at_least, f"cannot keep the boot counter in {state_dir}: {e.strerror}"
    finally:
        os.close(lock)
