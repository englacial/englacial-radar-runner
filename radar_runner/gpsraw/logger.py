"""The raw GPS logger: a gpsd client that appends the receiver's UBX messages to a file.

    python -m radar_runner.gpsraw log --out <collection>/gps_raw.ubx [--keep RXM-RAWX,RXM-SFRBX,...]
                                      [--parent-pid PID]

The radar manager starts one per collection, alongside the radar, and stops it
with SIGTERM when the radar is gone. It uses gpsd's raw pass-through
(`?WATCH={"enable":true,"raw":2}`): gpsd sends every packet it reads from the
receiver, byte for byte, and nothing is ever sent to the receiver. gpsd stays
the receiver's only owner.

* What is written is whole UBX messages only, in the order received, with
  nothing added: the file is a plain u-blox log that RTKLIB's convbin and
  other tools read. gpsd's own JSON lines and any NMEA are left out, and with
  --keep only the listed messages are written. A message cut off by a lost
  connection is dropped (counted in `skipped_bytes`), never written.
* Each batch of messages is one write(2) to a file opened for appending. A
  thread forces the file to the drive every --fsync-s seconds (at least 1 s),
  so that reading gpsd never waits for the drive. After a hard power cut the
  file holds whole messages up to the last sync, then possibly more and
  possibly the start of one cut off; readers skip a cut-off last message
  (ubx.scan counts it).
* gpsd restarting, or the receiver unplugged (systemd then stops gpsd), ends
  the connection: the logger waits --reconnect-s seconds, connects again and
  carries on in the same file. The longest time without a message, counted
  from the start and to the end of the run as well, is reported.
* A failure inside the loop (a write error, a broken log, anything not
  foreseen) is counted and reported, and the loop carries on after a pause: one
  bad moment must not cost the rest of a long run's raw data.
* Its own messages go to standard error, each text at most once a minute, so
  that its log stays small whatever gpsd does.
* It exits on SIGTERM or SIGINT, when its parent process is gone (the kernel's
  parent-death signal, and a look at the parent between reads; --parent-pid
  names the parent it was started by), and after --max-s seconds if given. At
  its exit it prints one JSON line on standard output: what was written (see
  `summary`).

Light on purpose: it blocks in recv(2), and after each read pauses 0.1 s so
that the packets of an epoch come in one read (about ten wake-ups a second
instead of one per packet); it does no checksum arithmetic (gpsd checked every
packet) and reads only the header of each message, plus one byte per
measurement of RXM-RAWX for the carrier-phase count.
"""
import ctypes
import json
import os
import signal
import socket
import sys
import threading
import time

from . import ubx

RECV_BYTES = 1 << 16
MIN_FSYNC_S = 1.0
SAY_EVERY_S = 60.0          # the same text is written to the log at most this often


class _Stop(Exception):
    pass


class RawLogger:
    def __init__(self, path, *, host="127.0.0.1", port=2947, keep=(), fsync_s=10.0, reconnect_s=1.0,
                 batch_s=0.1, max_s=0.0, connect=socket.create_connection, monotonic=time.monotonic, wall=time.time,
                 uptime=lambda: time.clock_gettime(time.CLOCK_BOOTTIME), parent=None, err=sys.stderr):
        self.path = path
        self.host, self.port = host, port
        self.keep = {ubx.IDS[k] for k in keep} if keep else None
        self.fsync_s = max(fsync_s, MIN_FSYNC_S) if fsync_s == fsync_s else MIN_FSYNC_S
        self.reconnect_s, self.batch_s, self.max_s = max(reconnect_s, 0.05), batch_s, max_s
        self.pause_after_error_s = 1.0
        self._connect, self._mono, self._wall, self._uptime = connect, monotonic, wall, uptime
        self._parent = parent
        self._err = err
        self.stopping = False
        self.waiting = False        # in recv, connect or a pause: a stop may interrupt it
        self.bytes = 0
        self.messages = {}
        self.rawx_epochs = self.measurements = self.cp_valid = 0
        self.first_rawx = self.last_rawx = None
        self.connects = self.disconnects = 0
        self.loop_errors = 0
        self.longest_gap_s = 0.0
        self.last_message_at = None
        self._started_at = None
        self.problems = {}          # kind -> the last text of that kind, for the summary
        self.skipped = 0            # bytes of earlier connections that were not part of a whole message
        self._framer = ubx.Framer()
        self._fd = None
        self._sock = None
        self._dirty = threading.Event()
        self._sync_lock = threading.Lock()
        self._said = {}             # text -> when it was last written to the log

    def _say(self, text):
        now = self._mono()
        last = self._said.get(text)
        if last is not None and now - last < SAY_EVERY_S:
            return
        self._said[text] = now
        if len(self._said) > 100:
            self._said = {t: w for t, w in self._said.items() if now - w < SAY_EVERY_S}
        try:
            print(f"gps raw log: {text}", file=self._err, flush=True)
        except (OSError, ValueError):
            self.problems["log"] = "cannot write the logger's own log"

    def _problem(self, kind, text):
        self.problems[kind] = text
        self._say(text)

    # ---- the file

    def _write(self, data):
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(self._fd, view):]
            self.bytes += len(data)
            self._dirty.set()
        except OSError as e:
            self._problem("write", f"write: {e.strerror}")

    def _sync(self):
        with self._sync_lock:
            if self._fd is not None and self._dirty.is_set():
                self._dirty.clear()
                try:
                    os.fsync(self._fd)
                except OSError as e:
                    self._problem("fsync", f"fsync: {e.strerror}")

    def _syncer(self):
        while not self.stopping:
            time.sleep(1.0)
            if not self.stopping and self._mono() - self._last_sync >= self.fsync_s:
                self._last_sync = self._mono()
                self._sync()

    # ---- messages

    def _take(self, data, now):
        out = []
        for cls, msg_id, message in self._framer.feed(data):
            if self.keep is not None and (cls, msg_id) not in self.keep:
                continue
            out.append(message)
            key = ubx.name(cls, msg_id)
            self.messages[key] = self.messages.get(key, 0) + 1
            if (cls, msg_id) == ubx.RAWX:
                info = ubx.rawx_summary(message[6:-2])
                if info is not None:
                    self.rawx_epochs += 1
                    self.measurements += info[3]
                    self.cp_valid += info[4]
                    self.last_rawx = (info, self._wall(), self._uptime())
                    if self.first_rawx is None:
                        self.first_rawx = self.last_rawx
        if out:
            since = self.last_message_at if self.last_message_at is not None else self._started_at
            if since is not None:
                self.longest_gap_s = max(self.longest_gap_s, now - since)
            self.last_message_at = now
            self._write(b"".join(out))

    def _new_connection(self):
        """A message cut off by the lost connection is never completed: drop it, count it."""
        self.skipped += self._framer.skipped + self._framer.pending
        self._framer = ubx.Framer()

    # ---- the loop

    def _alive(self):
        return self._parent is None or os.getppid() == self._parent

    def _pause(self, seconds):
        self.waiting = True
        try:
            if not self.stopping:
                time.sleep(seconds)
        finally:
            self.waiting = False

    def run(self):
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self._last_sync = self._started_at = self._mono()
        syncer = threading.Thread(target=self._syncer, daemon=True, name="gps-raw-sync")
        syncer.start()
        try:
            while not self.stopping:
                now = self._mono()
                if (self.max_s and now - self._started_at >= self.max_s) or not self._alive():
                    break
                try:
                    self._step()
                except _Stop:
                    raise
                except Exception as e:      # a bad moment must not end the log
                    self.loop_errors += 1
                    self._problem("loop", f"error in the loop: {type(e).__name__}: {e}")
                    self.waiting = False
                    self._pause(self.pause_after_error_s)
        except _Stop:
            pass
        finally:
            self.stopping = True
            self.waiting = False
            if self._sock is not None:
                self._sock.close()
                self._sock = None
            self._sync()
            os.close(self._fd)
            self._fd = None
        return self.summary()

    def _step(self):
        """One read, or one connection attempt."""
        if self._sock is None:
            self._sock = self._open()
            if self._sock is None:
                self._pause(self.reconnect_s)
                return
        self.waiting = True
        try:
            if self.stopping:
                return
            data = self._sock.recv(RECV_BYTES)
        except socket.timeout:
            return
        except OSError as e:
            data = b""
            self._problem("gpsd", f"gpsd connection: {e.strerror or e}")
        finally:
            self.waiting = False
        if not data:
            self.disconnects += 1
            self._say("gpsd closed the connection; connecting again")
            self._sock.close()
            self._sock = None
            self._new_connection()
            self._pause(self.reconnect_s)
            return
        self._take(data, self._mono())
        if self.batch_s:
            self._pause(self.batch_s)       # let packets gather: fewer wake-ups

    def _open(self):
        self.waiting = True
        try:
            sock = self._connect((self.host, self.port), timeout=1.0)
            sock.settimeout(1.0)
            sock.sendall(b'?WATCH={"enable":true,"raw":2};\n')
        except OSError as e:
            self._problem("gpsd", f"cannot connect to gpsd at {self.host}:{self.port}: {e.strerror or e}")
            return None
        finally:
            self.waiting = False
        self.connects += 1
        self._say(f"connected to gpsd at {self.host}:{self.port}")
        return sock

    def summary(self):
        def epoch(entry):
            if entry is None:
                return None
            (tow, week, leap, _, _), wall, up = entry
            return dict(ubx.epoch_time(week, tow, leap), received_utc=ubx.utc_text(wall),
                        received_uptime_s=round(up, 3))
        gap = self.longest_gap_s
        if self._started_at is not None:
            since = self.last_message_at if self.last_message_at is not None else self._started_at
            gap = max(gap, self._mono() - since)
        return {"file": os.path.basename(self.path), "bytes": self.bytes, "messages": dict(self.messages),
                "rawx_epochs": self.rawx_epochs, "measurements": self.measurements,
                "carrier_phase_valid": self.cp_valid, "first_rawx": epoch(self.first_rawx),
                "last_rawx": epoch(self.last_rawx), "gpsd_connects": self.connects,
                "gpsd_disconnects": self.disconnects, "longest_gap_s": round(gap, 1),
                "skipped_bytes": self.skipped + self._framer.skipped, "loop_errors": self.loop_errors,
                "error": "; ".join(self.problems.values())}


def _die_with_parent():
    """Ask the kernel for SIGTERM when the parent exits (Linux prctl PR_SET_PDEATHSIG)."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def main(args):
    keep = [k for k in args.keep.split(",") if k] if args.keep else []
    unknown = [k for k in keep if k not in ubx.IDS]
    if unknown:
        print(f"gps raw log: unknown message names: {', '.join(unknown)}", file=sys.stderr)
        return 2
    _die_with_parent()
    # After the parent-death request: a parent that is gone already is seen here.
    parent = args.parent_pid if args.parent_pid else os.getppid()
    logger = RawLogger(args.out, host=args.host, port=args.port, keep=keep, fsync_s=args.fsync_s,
                       reconnect_s=args.reconnect_s, max_s=args.max_s, parent=parent)

    def stop(signum, frame):
        logger.stopping = True
        if logger.waiting:          # never in the middle of a write
            raise _Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        os.nice(5)
    except OSError:
        pass
    try:
        result = logger.run()
    except _Stop:
        result = logger.summary()
    except OSError as e:
        result = dict(logger.summary(), error=f"cannot open {args.out}: {e.strerror}")
    try:
        print(json.dumps(result), flush=True)
    except (OSError, ValueError):
        pass
    return 0 if not result["error"] or result["bytes"] else 1
