"""The queue of alerts waiting to be sent: one small JSON file per alert, in one directory.

    p5-000000000042.json        priority 5 (urgent), sequence number 42

Each file is written to a temporary name, synced, renamed into place and the
directory synced, so a power cut at any instant leaves the old set of files or
the new one, never a half-written alert under a queue name. A file that cannot
be read as an alert anyway (a damaged card) is deleted and counted. Files are
taken highest priority first, then oldest first; an alert is deleted only after
ntfy has accepted it, so a restart or power cut in the middle of sending repeats
at most the one alert that was being sent.

The queue holds at most `max_items`. When it is full, a new alert pushes out
the oldest alert of the lowest priority present, but only if that priority is
not higher than its own: an urgent alert is never dropped to make room for a
lower one; a new alert that is lower than everything queued is itself dropped.
Nothing here raises except where noted.
"""
import json
import os
import re

NAME_RE = re.compile(r"^p([1-5])-(\d{12})\.json$")
MAX_FILE_BYTES = 16384


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_durably(path, data):
    """Replace `path` with `data` (bytes) so that a power cut leaves the old or the new file. Raises OSError."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


class DiskQueue:
    def __init__(self, directory, max_items):
        self.dir = directory
        self.max_items = max(1, int(max_items))
        self.error = ""
        self.dropped = 0        # pushed out, or refused, because the queue was full
        self.damaged = 0        # files that were not a readable alert
        self.load_error = ""    # why the last load found no alert in a file that is there
        self.seq = 0

    def setup(self):
        """Create the directory, remove temporary files of a writer that was cut off, find the next number."""
        try:
            os.makedirs(self.dir, mode=0o755, exist_ok=True)
            for name in os.listdir(self.dir):
                if name.startswith(".") and name.endswith(".tmp"):
                    os.unlink(os.path.join(self.dir, name))
            self.error = ""
        except OSError as e:
            self.error = f"{self.dir}: {e.strerror or e}"
        self.seq = max([seq for _, seq, _ in self.names()] + [self.seq])

    def names(self):
        """[(priority, seq, name)] of the queued files, in sending order; [] if the directory cannot be listed."""
        return self.listing() or []

    def listing(self):
        """As names(), but None if the directory cannot be listed: not to be taken for an empty queue."""
        try:
            listing = os.listdir(self.dir)
        except OSError as e:
            self.error = f"{self.dir}: {e.strerror or e}"
            return None
        found = []
        for name in listing:
            m = NAME_RE.match(name)
            if m:
                found.append((int(m.group(1)), int(m.group(2)), name))
        found.sort(key=lambda x: (-x[0], x[1]))
        return found

    def counts(self):
        by = {}
        for priority, _, _ in self.names():
            by[priority] = by.get(priority, 0) + 1
        return by

    def add(self, item, priority):
        """Queue one alert (a dict). Returns the file name, or "" if it was dropped or could not be written."""
        present = self.names()
        while len(present) >= self.max_items:
            lowest = min(p for p, _, _ in present)
            if lowest > priority:
                self.dropped += 1
                return ""
            victim = min((x for x in present if x[0] == lowest), key=lambda x: x[1])
            self.remove(victim[2])
            self.dropped += 1
            present.remove(victim)
        self.seq += 1
        name = f"p{priority}-{self.seq:012d}.json"
        body = dict(item, id=name[:-5], priority=priority)
        try:
            write_durably(os.path.join(self.dir, name), (json.dumps(body) + "\n").encode())
            self.error = ""
            return name
        except (OSError, TypeError, ValueError) as e:
            self.error = f"cannot queue an alert in {self.dir}: {getattr(e, 'strerror', None) or e}"
            return ""

    def load(self, name):
        """The alert in one file, or None (a damaged file is then deleted and counted)."""
        path = os.path.join(self.dir, name)
        self.load_error = ""
        try:
            with open(path, "rb") as f:
                raw = f.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as e:
            self.load_error = self.error = f"{path}: {e.strerror or e}"
            return None
        try:
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("too large")
            item = json.loads(raw)
            if not (isinstance(item, dict) and isinstance(item.get("title"), str)
                    and isinstance(item.get("message"), str)):
                raise ValueError("not an alert")
        except (ValueError, RecursionError):
            self.damaged += 1
            self.remove(name)
            return None
        item["priority"] = int(NAME_RE.match(name).group(1))
        return item

    def remove(self, name):
        """True if the file is gone (or was never there)."""
        try:
            os.unlink(os.path.join(self.dir, name))
        except FileNotFoundError:
            return True
        except OSError as e:
            self.error = f"cannot delete {name}: {e.strerror or e}"
            return False
        try:
            _fsync_dir(self.dir)
        except OSError:
            pass
        return True
