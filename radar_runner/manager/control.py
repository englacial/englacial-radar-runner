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


class ControlServer(threading.Thread):
    """Accepts connections and passes each request to `handler(request) -> reply` (both dicts)."""

    def __init__(self, path, handler):
        super().__init__(daemon=True, name="manager-control")
        self.path = path
        self.handler = handler
        self._closing = threading.Event()
        self._sock = None

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
            try:
                self._serve(conn)
            except Exception:
                log.exception("error while serving a command")
            finally:
                conn.close()

    def _serve(self, conn):
        conn.settimeout(2.0)
        data = b""
        try:
            while b"\n" not in data and len(data) < MAX_REQUEST:
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
    """

    def __init__(self, path, max_bytes, wall=time.time, uptime=boottime_s):
        self.path, self.max_bytes = path, int(max_bytes)
        self._wall, self._uptime = wall, uptime
        self.error = ""
        self.written = 0

    def write(self, event):
        try:
            now = self._wall()
            line = json.dumps({"time": round(now, 3),
                               "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                               "uptime_s": round(self._uptime(), 3), **event}, default=str) + "\n"
            try:
                if os.path.getsize(self.path) + len(line) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode())     # one write per line: lines never interleave or split
            finally:
                os.close(fd)
            self.written += 1
            self.error = ""
        except (OSError, ValueError, TypeError) as e:
            self.error = f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"
        return self.error
