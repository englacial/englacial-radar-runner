"""GPS position from gpsd.

gpsd owns the receiver. This module is an ordinary gpsd client over its JSON
socket, standard library only, and never waits: `poll` is called a few times
per second from the service loop. It only ever asks gpsd for data: nothing
here sends a command to the receiver, and nothing here starts another program.

`Gps` asks gpsd for its latest fix once per `interval_s` with `?POLL;`. It
does not use gpsd's streaming mode: this receiver runs at 10 Hz with all
satellite messages, and gpsd would format and send about 150 kB of JSON per
second to this client. A POLL reply is one line of about 1 kB. The price: the
satellite counts are only in a reply now and then, so `sats_used` is mostly
empty here; the OLA logs the same receiver's count (`gps_SIV`) in every row.

gpsd keeps answering with its last fix after the receiver has stopped, so a
sample is only "new" when the fix time has moved on since the last sample.
`no_data_s` says for how long gpsd has had a receiver that delivers no new fix
time. That is what a choked receiver output looked like on 2026-11-16 (the
receiver's transmit buffer overflowed); `setup/check.py --receiver` reads the
receiver's side of it.

`no_position_s` says for how long gpsd has had a receiver without a new 2D or
3D fix. On 2026-11-19, after a power cut, gpsd delivered fix times with mode 0
for 77 minutes while the receiver had a fix: it had not identified the receiver
(empty `subtype` in its DEVICES report), and the receiver sent it no position
message (setup/gps_receiver.py has what gpsd does and what is inferred). gpsd
clears the fix at every cycle, so mode 0 with fix times means no position
message arrives, and mode 1 a position message without a fix. While there is no
position, or the last report did not show an identified receiver, `?DEVICES;`
is asked every DEVICES_EVERY_S, and `identified` gives the answer. Both
questions only read gpsd's state.
"""
import json
import logging
import socket
import time

log = logging.getLogger("telemetry.gps")

COLUMNS = ("gps_time", "new_fix", "mode", "lat", "lon", "alt_hae_m", "alt_msl_m", "speed_mps", "track_deg",
           "climb_mps", "eph_m", "epv_m", "eps_mps", "ept_s", "sep_m", "hdop", "pdop", "vdop",
           "sats_used", "sats_seen", "receivers")
_TPV = (("gps_time", "time"), ("mode", "mode"), ("lat", "lat"), ("lon", "lon"), ("alt_hae_m", "altHAE"),
        ("alt_msl_m", "altMSL"), ("speed_mps", "speed"), ("track_deg", "track"), ("climb_mps", "climb"),
        ("eph_m", "eph"), ("epv_m", "epv"), ("eps_mps", "eps"), ("ept_s", "ept"), ("sep_m", "sep"))
_SKY = (("hdop", "hdop"), ("pdop", "pdop"), ("vdop", "vdop"), ("sats_used", "uSat"), ("sats_seen", "nSat"))
MAX_BUFFER = 1 << 18
DEVICES_EVERY_S = 10.0


def connect_tcp(host, port):
    sock = socket.create_connection((host, port), timeout=1.0)      # local: answers or is refused at once
    sock.setblocking(False)
    return sock


class Gps:
    def __init__(self, cfg, *, connect=connect_tcp, monotonic=time.monotonic):
        self.cfg = cfg
        self._connect = connect
        self._monotonic = monotonic
        self.error = "not connected to gpsd yet"
        self.connects = 0
        self.receivers = None           # receivers gpsd has, from the last reply
        self.last_reply_at = None
        self.last_new_fix_at = None
        self.last_position_at = None    # the last new fix with mode 2 or 3
        # The mode of the last sample with a new fix time. A reply between gpsd clearing its fix and
        # the next message has no time and mode 0: that says nothing about the receiver.
        self.new_fix_mode = None
        self.devices = None             # gpsd's devices from its last DEVICES report
        self._sock = None
        self._buf = b""
        self._next_connect = 0.0
        self._next_poll = 0.0
        self._next_devices = 0.0
        self._last_fix_time = None
        self._receiver_since = None     # since when gpsd has had a receiver

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._buf = None, b""

    def _lost(self, why, now):
        if self.error != why:
            log.warning("gpsd: %s", why)
        self.error = why
        self.receivers = self._receiver_since = self.devices = None
        self.close()
        self._next_connect = now + self.cfg["reconnect_s"]

    def _send(self, data, now):
        try:
            self._sock.sendall(data)
            return True
        except OSError as e:
            self._lost(f"connection lost: {e.strerror or e}", now)
            return False

    def _sample(self, reply, now):
        """One row from a POLL reply. With several receivers, the one with the best fix."""
        tpvs = [t for t in reply.get("tpv") or [] if isinstance(t, dict)]
        skies = [s for s in reply.get("sky") or [] if isinstance(s, dict)]
        sample = dict.fromkeys(COLUMNS)
        self.receivers = sample["receivers"] = reply.get("active") if isinstance(reply.get("active"), int) else len(tpvs)
        if not self.receivers:
            self._receiver_since = None
        elif self._receiver_since is None:
            self._receiver_since = now
        tpv = max(tpvs, key=lambda t: t.get("mode") if isinstance(t.get("mode"), int) else 0, default={})
        for name, key in _TPV:
            sample[name] = tpv.get(key)
        sky = next((s for s in skies if s.get("device") == tpv.get("device")), {})
        for name, key in _SKY:
            sample[name] = sky.get(key)
        fix_time = sample["gps_time"]
        sample["new_fix"] = bool(fix_time) and fix_time != self._last_fix_time
        if sample["new_fix"]:
            self._last_fix_time = fix_time
            self.last_new_fix_at = now
            self.new_fix_mode = sample["mode"]
            if isinstance(sample["mode"], int) and sample["mode"] >= 2:
                self.last_position_at = now
        return sample

    @property
    def identified(self):
        """True if gpsd has identified every receiver it reported (a non-empty subtype), False if not,
        None if it has reported none or not been asked."""
        if not self.devices:
            return None
        # `subtype` comes from the version reply; "Unknown" is gpsd's placeholder for older receivers.
        return all(d.get("subtype1") or d.get("subtype") not in (None, "", "Unknown") for d in self.devices)

    def _devices(self, reply):
        devices = reply.get("devices")
        if isinstance(devices, list):
            self.devices = [d for d in devices if isinstance(d, dict)]

    def poll(self, now=None):
        """Advance the connection one step. Returns a sample (dict by COLUMNS) when gpsd answered, else None."""
        now = self._monotonic() if now is None else now
        if self._sock is None:
            if now < self._next_connect:
                return None
            self._next_connect = now + self.cfg["reconnect_s"]
            try:
                self._sock = self._connect(self.cfg["host"], self.cfg["port"])
            except OSError as e:
                why = f"cannot connect to gpsd: {e.strerror or e}"
                if self.error != why:
                    log.warning("%s (%s:%s)", why, self.cfg["host"], self.cfg["port"])
                self.error = why
                self._sock = None
                return None
            self.connects += 1
            self.error = "connected, no answer yet"
            self.last_reply_at = now        # the answer timeout counts from here
            self._next_poll = now
            self._next_devices = now + DEVICES_EVERY_S     # the reply to WATCH carries a DEVICES report
            log.info("gpsd: connected (%s:%s)", self.cfg["host"], self.cfg["port"])
            # Watch without "json": gpsd opens the receiver for us but sends nothing by itself.
            if not self._send(b'?WATCH={"enable":true};\n', now):
                return None
        if now >= self._next_poll:
            self._next_poll = max(self._next_poll + self.cfg["interval_s"], now)
            if not self._send(b"?POLL;\n", now):
                return None
        no_position = self.no_position_s(now) > self.cfg["stale_after_s"]
        if now >= self._next_devices and (self.identified is not True or no_position):
            self._next_devices = now + DEVICES_EVERY_S
            if not self._send(b"?DEVICES;\n", now):
                return None
        try:
            data = self._sock.recv(65536)
            if not data:
                self._lost("gpsd closed the connection", now)
                return None
            self._buf += data
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as e:
            self._lost(f"connection lost: {e.strerror or e}", now)
            return None
        sample = None
        if b"\n" in self._buf:
            *lines, self._buf = self._buf.split(b"\n")
            for line in lines:
                if line.startswith(b'{"class":"DEVICES"'):
                    try:
                        self._devices(json.loads(line))
                    except (ValueError, TypeError, AttributeError):
                        pass
                    continue
                if not line.startswith(b'{"class":"POLL"'):
                    continue                # VERSION, WATCH, DEVICE, ERROR
                try:
                    reply = json.loads(line)
                    sample = self._sample(reply, now)
                    self.last_reply_at = now
                    self.error = ""
                except (ValueError, TypeError, AttributeError):
                    pass
        if len(self._buf) > MAX_BUFFER:
            self._buf = b""
        if now - self.last_reply_at > self.cfg["answer_timeout_s"]:
            self._lost(f"gpsd has not answered for {now - self.last_reply_at:.0f} s; reconnecting", now)
        return sample

    def no_data_s(self, now):
        """For how long gpsd has had a receiver without a new fix time from it; 0.0 if that is not the case."""
        if self._sock is None or not self.receivers or self._receiver_since is None:
            return 0.0
        return max(now - max(self._receiver_since, self.last_new_fix_at or 0.0), 0.0)

    def no_position_s(self, now):
        """For how long gpsd has had a receiver without a new 2D or 3D fix from it; 0.0 if that is not the case."""
        if self._sock is None or not self.receivers or self._receiver_since is None:
            return 0.0
        return max(now - max(self._receiver_since, self.last_position_at or 0.0), 0.0)
