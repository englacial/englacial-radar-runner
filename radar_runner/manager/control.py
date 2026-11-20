"""The manager's command socket and its event log.

Socket: a Unix stream socket, <run_dir>/manager.sock, mode 0660 (user
englacial and root). One request per connection: one line of JSON in, one line
of JSON out.

    {"cmd": "status"}
    {"cmd": "start"} / {"cmd": "toggle"} / {"cmd": "clear-fault"}
    {"cmd": "stop"} or {"cmd": "stop", "interrupt": true}
    {"cmd": "override", "name": "no-amp-telemetry", "set": true}

Every reply has "ok" (true/false) and "message", and "status": the same
content as the status file, taken after the command was applied.

Event log: <state_dir>/manager-events.jsonl, one JSON object per line, appended.
"""
import json
import logging
import os
import socket
import threading
import time

from radar_runner.common.status import boottime_s

log = logging.getLogger("manager.control")

SOCKET_NAME = "manager.sock"
MAX_REQUEST = 1 << 16
MAX_CLIENTS = 8             # served at the same time
CLIENT_TIMEOUT_S = 2.0      # a client has this long to send its request


class ControlServer(threading.Thread):
    """Accepts connections and passes each request to `handler(request) -> reply` (both dicts)."""

    def __init__(self, path, handler):
        super().__init__(daemon=True, name="manager-control")
        self.path = path
        self.handler = handler
        self._closing = threading.Event()
        self._sock = None
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)

    def open(self):
        """Bind the socket. Raises OSError, also if another manager already answers on it."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(self.path)
        except OSError:
            pass                    # nobody there: a file left by an earlier run, or nothing
        else:
            raise OSError(f"another radar manager is already answering on {self.path}")
        finally:
            probe.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        old = os.umask(0o117)       # the socket is created 0660
        try:
            sock.bind(self.path)
        finally:
            os.umask(old)
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        self.start()

    def run(self):
        while not self._closing.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._closing.is_set():
                    return
                time.sleep(0.2)
                continue
            # One short-lived thread per client: a client that is slow or sends nothing
            # holds up nobody else (the power service's stop hook is a client).
            if not self._slots.acquire(blocking=False):
                conn.close()                    # too many at once: the client sees "no answer"
                continue
            threading.Thread(target=self._client, args=(conn,), daemon=True, name="manager-client").start()

    def _client(self, conn):
        try:
            self._serve(conn)
        except Exception:
            log.exception("error while serving a command")
        finally:
            conn.close()
            self._slots.release()

    def _serve(self, conn):
        conn.settimeout(CLIENT_TIMEOUT_S)
        deadline = time.monotonic() + CLIENT_TIMEOUT_S     # for the whole request, not per byte
        data = b""
        try:
            while b"\n" not in data and len(data) < MAX_REQUEST:
                if time.monotonic() >= deadline:
                    raise ValueError("request not complete in time")
                part = conn.recv(4096)
                if not part:
                    break
                data += part
            request = json.loads(data.split(b"\n", 1)[0])
            if not isinstance(request, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            reply = {"ok": False, "message": "request not understood"}
        else:
            reply = self.handler(request)
        try:
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass

    def close(self):
        self._closing.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            try:
                os.unlink(self.path)
            except OSError:
                pass


class EventLog:
    """Append-only log, one JSON object per line. Never raises; the last error is in `error`.

    Each line carries `time`, `time_utc`, `uptime_s` and the event's own fields.
    When the file passes `max_bytes` it is renamed to <file>.1 (replacing the
    one before) and a new file is started.

    Each line is written at once and then synced to the drive by a thread of
    its own, as the safety watcher's log is: without the sync a power cut loses
    the last events (the alerts service then misses them), and syncing in the
    loop would let a slow card hold up an urgent stop, whose event is written
    just before the stop is carried out.
    """

    def __init__(self, path, max_bytes, wall=time.time, uptime=boottime_s):
        self.path, self.max_bytes = path, int(max_bytes)
        self._wall, self._uptime = wall, uptime
        self.error = ""
        self.written = 0
        self.synced = 0         # rounds of the syncing thread
        self.sync_error = ""
        self._asked = 0         # sync requests made, and the number that a finished round covered
        self._covered = 0
        self._sync_done = threading.Condition()
        self._sync_wanted = threading.Event()
        self._rotated = False
        self._syncer = None

    def _sync_loop(self):
        while True:
            self._sync_wanted.wait()
            self._sync_wanted.clear()
            asked = self._asked
            paths = [self.path]
            if self._rotated:
                self._rotated = False
                paths.insert(0, self.path + ".1")
            error = ""
            for path in paths:
                try:
                    fd = os.open(path, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError as e:
                    error = f"{type(e).__name__}: {e.strerror or e}"
            self.sync_error = error
            self.synced += 1
            with self._sync_done:
                self._covered = asked
                self._sync_done.notify_all()

    def _want_sync(self):
        try:
            self._asked += 1
            if self._syncer is None:
                self._syncer = threading.Thread(target=self._sync_loop, daemon=True, name="manager-events-sync")
                self._syncer.start()
            self._sync_wanted.set()
        except Exception as e:      # no thread to be had: the lines are written, only not synced yet
            self._syncer = None
            self.sync_error = f"{type(e).__name__}: {e}"

    def close(self, timeout=1.0):
        """Wait, for at most `timeout`, until every line written so far is synced. For the end of
        the manager, after the radar is stopped: the last events (`ended`, `manager_stopped`) are the
        ones a power cut would otherwise take. Returns True if they are synced."""
        if self._syncer is None:
            return self._asked == 0
        with self._sync_done:
            return self._sync_done.wait_for(lambda: self._covered >= self._asked, timeout)

    def write(self, event):
        try:
            now = self._wall()
            line = json.dumps({"time": round(now, 3),
                               "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                               "uptime_s": round(self._uptime(), 3), **event}, default=str) + "\n"
            try:
                if os.path.getsize(self.path) + len(line) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                    self._rotated = True
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode())     # one write per line: lines never interleave or split
            finally:
                os.close(fd)
            self.written += 1
            self.error = ""
            self._want_sync()
        except (OSError, ValueError, TypeError) as e:
            self.error = f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"
        return self.error
