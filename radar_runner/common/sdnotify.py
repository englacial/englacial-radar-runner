"""systemd service notification (sd_notify(3)) without libsystemd.

For units with `Type=notify` and `WatchdogSec=`: the service says READY=1 once
it is up and WATCHDOG=1 regularly; if the pings stop, systemd kills and
restarts it. Outside systemd (no NOTIFY_SOCKET) every call does nothing.
"""
import os
import socket
import time


class Notifier:
    def __init__(self, environ=os.environ, monotonic=time.monotonic):
        self._monotonic = monotonic
        self._sock = None
        self._addr = None
        self._last_ping = None
        path = environ.get("NOTIFY_SOCKET", "")
        if path[:1] in ("/", "@"):
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
                self._sock.setblocking(False)
                self._addr = "\0" + path[1:] if path[0] == "@" else path
            except OSError:
                self._sock = None
        # systemd's watchdog period, if it is meant for this process.
        try:
            usec = int(environ.get("WATCHDOG_USEC", "0"))
            pid = int(environ.get("WATCHDOG_PID", os.getpid()))
        except ValueError:
            usec, pid = 0, 0
        self.watchdog_s = usec / 1e6 if usec > 0 and pid == os.getpid() else None
        # Ping three times per period, and at least once a second.
        self.ping_interval = min(1.0, self.watchdog_s / 3) if self.watchdog_s else 1.0

    @property
    def enabled(self):
        return self._sock is not None

    def send(self, text):
        """Send one notification. Returns False if there is no socket or it failed."""
        if self._sock is None:
            return False
        try:
            self._sock.sendto(text.encode(), self._addr)
            return True
        except OSError:
            return False

    def ready(self):
        return self.send("READY=1")

    def stopping(self):
        return self.send("STOPPING=1")

    def status(self, text):
        """One line shown by `systemctl status`."""
        return self.send("STATUS=" + text.replace("\n", " "))

    def watchdog(self):
        """Ping the watchdog if a ping is due. Call it often from the main loop."""
        now = self._monotonic()
        if self._last_ping is None or now - self._last_ping >= self.ping_interval:
            self._last_ping = now
            self.send("WATCHDOG=1")
