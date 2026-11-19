"""Persistent state of the boot-loop guard.

One small JSON file in the state directory:

    count          consecutive shutdowns started by this service within
                   `early_boot_s` of boot
    passive        true once the count reached the limit; cleared after a long
                   period of healthy power or by `python3 -m power clear-guard`
    last_shutdown  details of the last shutdown this service started

The decisions are made in logic.py; this module only reads and writes the
file. Nothing here raises: a missing or corrupt file gives the defaults, and a
failed write is reported to the caller.
"""
import json
import os
import threading

FILE_NAME = "power_guard.json"


class Guard:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, FILE_NAME)
        self.count = 0
        self.passive = False
        self.last_shutdown = None
        self._lock = threading.Lock()

    def load(self):
        """Read the file. Returns a list of problems (empty if fine or simply absent)."""
        self.count, self.passive, self.last_shutdown = 0, False, None
        try:
            with open(self.path, "rb") as f:
                raw = f.read(65536)
        except FileNotFoundError:
            return []
        except OSError as e:
            return [f"guard state {self.path} unreadable ({e}): starting from zero"]
        try:
            data = json.loads(raw)
            count, passive = data["count"], data["passive"]
            if type(count) is not int or not 0 <= count <= 1000000 or type(passive) is not bool:
                raise ValueError("bad field value")
            last = data.get("last_shutdown")
            if last is not None and not isinstance(last, dict):
                raise ValueError("bad last_shutdown")
        except Exception as e:  # any malformed content at all
            return [f"guard state {self.path} corrupt ({e!r}): starting from zero"]
        self.count, self.passive, self.last_shutdown = count, passive, last
        return []

    def update(self, data):
        """Apply a guard dict from the logic ("count", "passive", optional "last_shutdown")."""
        with self._lock:
            self.count = data["count"]
            self.passive = data["passive"]
            if "last_shutdown" in data:
                self.last_shutdown = data["last_shutdown"]

    def save(self):
        """Write the file atomically and durably. Returns "" on success, else the error text."""
        with self._lock:
            body = json.dumps({"version": 1, "count": self.count, "passive": self.passive,
                               "last_shutdown": self.last_shutdown}, indent=1) + "\n"
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path), mode=0o755, exist_ok=True)
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
                try:
                    os.write(fd, body.encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, self.path)
                dfd = os.open(os.path.dirname(self.path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError as e:
                return str(e)
            return ""

    def as_dict(self):
        return {"count": self.count, "passive": self.passive, "last_shutdown": self.last_shutdown}
