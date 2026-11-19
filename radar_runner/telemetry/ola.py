"""The OpenLog Artemis (OLA): one serial connection, kept open, never blocking.

The OLA streams CSV rows (about 10 per second) with a header line that names
the columns. Its ADS1015 ADC on the amplifier control board has to be set to
three differential readings against the amplifier's ground on AIN3:

    A0-A3  amplifier temperature     A1-A3  amplifier current     A2-A3  28 V rail

The OLA has no SD card, so it forgets this at every reset, and opening the
serial port resets it. This module therefore opens the port once, drives the
OLA's menus to set the ADC (as radar-testbench/pa_bringup/ola_setup.py does,
reading the menu numbers from the menus themselves), and then only reads.

    ola = Ola(config["telemetry"]["ola"])
    rows = ola.poll()        # call a few times per second; returns the rows read since the last call

`poll` never waits: the menu dialogue advances one step per call, so that the
service loop keeps publishing and pinging its watchdog while it runs (about 12 s).

How old is a row? Every poll reads the port until it is empty (at most
MAX_POLL_BYTES per poll). A row read in a poll that emptied the port arrived
after the previous such poll: within `window_s`. So after a stall of the loop
the backlog comes out in one poll, the newest row is taken as "now" and the
earlier ones are dated back by the OLA's own clock (`rtcTime`), or evenly over
the window if that is not usable. Each row carries the result as `age_s`. If a
poll hits the byte limit before the port is empty, `drained` is false, the rows
are held back and come out with the poll that does empty it: nothing is dated
"now" that may be old.

What it copes with. "Reopen" means close and open the port, which resets the OLA.

* port missing: tried every `reopen_s`, and at once when the device is plugged in
* the OLA resetting by itself (its start-up banner is seen): set up again on
  the same connection
* no valid row for `silent_s` (silence, garbage, or rows that no longer fit the
  header because a new header was lost): reopen
* no header after opening: the menu dialogue is run anyway after
  `boot_timeout_s`; it ends by printing the header
* the OLA not answering its menus: tried again in place after `retry_s`. Rows
  that arrive meanwhile are still returned (and logged), without amplifier values
* partial and garbled lines: dropped and counted

Resets are not repeated in a loop: the wait before a reopen and before another
try at the menus doubles each time, up to `reopen_max_s` and `retry_max_s` (an
hour), and goes back to the start only after `stable_s` of good rows.
`retry_state` says what is waited for.

Rows are parsed by column name, so added or reordered columns change nothing.
"""
import logging
import math
import os
import re
import time

log = logging.getLogger("telemetry.ola")

WANT = {"Log A0": False, "Log A1": False, "Log A2": False, "Log A3": False, "Log A0-A1": False,
        "Log A0-A3": True, "Log A1-A3": True, "Log A2-A3": True}
AMP_COLUMNS = ("A0A3mV", "A1A3mV", "A2A3mV")
AMP_VALUES = ("amp_temp_c", "amp_current_a", "amp_voltage_v")

HEADER = re.compile(r"(rtcDate,[^\r\n]*)\r?\n")
ROW = re.compile(r"\d\d/\d\d/\d{4},")
MAIN_MENU = re.compile(r"Menu: Main Menu[\s\S]*x\) Return to logging")
DEVICE_MENU = re.compile(r"Menu: Configure Attached Devices[\s\S]*x\) Exit")
ADC_MENU = re.compile(r"Menu: Configure ADS1015 ADC[\s\S]*x\) Exit")

BANNER = re.compile(r"Artemis OpenLog v")      # printed once when the OLA starts

CLOSED, BOOTING, CONFIGURING, STREAMING = "closed", "booting", "configuring", "streaming"
MAX_LINE = 4096
MAX_TEXT = 32768
MAX_POLL_BYTES = 262144         # read per poll at most; 150 s of rows, more than the tty can hold back
MAX_HELD_ROWS = 6000            # rows kept while the port has not been read empty
REPLUG_S = 10.0                 # a device gone for this long and back was replugged by a person


class OlaError(RuntimeError):
    pass


def _why(e):
    """A short reason from a port error (pyserial wraps the system's text in a longer one)."""
    return os.strerror(e.errno) if isinstance(getattr(e, "errno", None), int) else str(e)


def open_serial(port, baud):
    """Open the port without read timeout. Opening it resets the OLA (DTR)."""
    import serial       # here, so that tests and tools without a port do not need pyserial
    ser = serial.Serial(port, baud, timeout=0, write_timeout=1.0)
    try:
        import fcntl
        import termios
        # A second program that opens the port (a bench tool) now gets "device busy"
        # instead of silently taking half of the data. Root is not stopped by this.
        fcntl.ioctl(ser.fileno(), termios.TIOCEXCL)
    except (OSError, AttributeError, ValueError):
        pass
    return ser


def amp_values(cfg, a0a3_mv, a1a3_mv, a2a3_mv):
    """Convert the three ADC readings (mV at the ADC) to temperature, current and voltage.

    The scaling comes from the control board and the bench (pa_run.py):
    temperature  amplifier pin 3 gives 10 mV/C, through a 1:2 divider
    current      amplifier pin 2 gives 50 mV per 100 mA, through a 1:2 divider; the ADC then read
                 0.66-0.68 of the clamp meter on the supply wire, so the result is scaled by 1.5
                 (pa_run.py ADC_CURRENT_CAL; not re-checked with the 28 V on)
    voltage      28 V rail through 100k/10k, a factor of 11
    """
    return (a0a3_mv * cfg["temp_divider"] / cfg["temp_mv_per_c"],
            a1a3_mv * cfg["current_divider"] / cfg["current_mv_per_a"] * cfg["current_cal"],
            a2a3_mv * cfg["voltage_ratio"] / 1000.0)


def unique(names):
    """Column names made unique: a second `degC` becomes `degC_2`."""
    seen, out = {}, []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return tuple(out)


def _option(menu, label_regex):
    m = re.search(rf"^\s*(\d+)\)\s*{label_regex}", menu, re.M)
    if not m:
        raise OlaError(f"menu has no option matching {label_regex!r}")
    return m.group(1)


def _states(menu):
    return {m.group(2).strip(): m.group(3) == "Enabled"
            for m in re.finditer(r"^\s*(\d+)\)\s*([^:\n]+):\s*(Enabled|Disabled)", menu, re.M)}


def _clock_seconds(text):
    """The OLA's rtcTime ("02:18:25.74") as seconds of its day, or None."""
    try:
        h, m, sec = text.split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except ValueError:
        return None


class Ola:
    def __init__(self, cfg, *, open_port=open_serial, exists=os.path.exists, monotonic=time.monotonic):
        self.cfg = cfg
        self._open_port = open_port
        self._exists = exists
        self._monotonic = monotonic
        self.state = CLOSED
        self.columns = None             # unique names of the current header, or None before the first
        self.configured = False         # the header has the three differential columns
        self.error = ""                 # why the OLA is not delivering amplifier readings
        self.rows = 0
        self.bad_lines = 0
        self.dropped_rows = 0           # held back too long while the port could not be read empty
        self.opens = 0                  # each one resets the OLA
        self.configures = 0             # completed menu dialogues
        self.restarts = 0               # start-up banners seen on an open connection
        self.drained = True             # the last poll read the port empty
        self.window_s = 0.0             # the rows of the last poll arrived within this time before it
        self._port = None
        self._buf = b""
        self._text = ""                 # what arrived since the last menu command
        self._index = {}
        self._dialogue = None           # generator while the menus are driven
        self._wait = None               # (regex, deadline, what) the dialogue is waiting for
        self._quiet_polls = 0
        self._held = []                 # rows read but not yet returned
        self._drained_at = None         # when the port was last read empty
        self._last_row_time = None      # the time given to the last row returned
        self._last_valid_row_at = 0.0
        self._header_seen = False       # since the port was opened or the OLA restarted
        self._boot_at = 0.0
        self._next_open = 0.0
        self._next_configure = None
        self.port_present = True        # the device file exists (looked at while the port is closed)
        self._missing_since = None
        # Back-off: waits that double with every failure and start again only after stable_s of good rows.
        self._reopen_s = cfg["reopen_s"]
        self._retry_s = cfg["retry_s"]
        self._failures = 0
        self._good_since = None

    # ---- connection

    def close(self):
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None
        self._dialogue = self._wait = None
        self._buf, self._text, self._held = b"", "", []
        self.state, self.configured, self.columns = CLOSED, False, None
        self._good_since = None

    def _lost(self, why, now):
        """Give the connection up. The next open, which resets the OLA, waits longer each time."""
        self._failures += 1
        self._next_open = now + self._reopen_s
        log.warning("OLA: %s; reopening the port in %.0f s (failure %d in a row)", why, self._reopen_s, self._failures)
        self._reopen_s = min(self._reopen_s * 2, self.cfg["reopen_max_s"])
        self.error = why
        self.close()

    def _open(self, now):
        try:
            self._port = self._open_port(self.cfg["port"], self.cfg["baud"])
        except Exception as e:      # pyserial raises SerialException (an OSError) or ValueError
            why = f"cannot open the port: {_why(e)}"
            if self.error != why:
                log.warning("OLA: %s (%s)", why, self.cfg["port"])
            self.error = why
            self._port = None
            # Nothing was reset by a failed open: keep looking at the short interval.
            self._next_open = max(self._next_open, now + self.cfg["reopen_s"])
            return
        self.opens += 1
        self.state, self.error = BOOTING, "starting after the port was opened"
        self._boot_at = self._drained_at = self._last_valid_row_at = now
        self._header_seen = False
        self._next_configure = None
        self._last_row_time = None
        log.info("OLA: port open (%s); waiting for it to start", self.cfg["port"])

    def _write(self, data):
        self._text = ""
        self._quiet_polls = 0
        self._port.write(data)

    def retry_state(self, now=None):
        """What is being waited for after a failure: for the status file."""
        now = self._monotonic() if now is None else now
        waiting, in_s = None, None
        if self._port is None and self._failures:
            waiting, in_s = "reopening the port (resets the OLA)", max(self._next_open - now, 0.0)
        elif self._port is not None and self._dialogue is None and not self.configured \
                and self._next_configure is not None:
            waiting, in_s = "setting the ADS1015 again", max(self._next_configure - now, 0.0)
        return {"failures_in_a_row": self._failures, "waiting_for": waiting,
                "next_attempt_in_s": None if in_s is None else round(in_s, 1),
                "reopen_wait_s": self._reopen_s, "configure_wait_s": self._retry_s}

    # ---- the menu dialogue

    def _dialogue_steps(self):
        """The steps of ola_setup.configure(). Yields what to wait for; is sent the text that arrived."""
        timeout = self.cfg["menu_timeout_s"]
        self._write(b"\r")
        menu = yield MAIN_MENU, timeout, "the main menu"
        self._write(_option(menu, r"Detect / Configure Attached Devices").encode())
        menu = yield DEVICE_MENU, timeout, "the device menu"
        self._write(_option(menu, r"ADS1015 ADC \(0x[0-9A-Fa-f]{2}\)").encode() + b"\r")
        menu = yield ADC_MENU, timeout, "the ADS1015 menu"
        for _ in range(12):         # each change reprints the menu
            states = _states(menu)
            todo = [k for k, v in WANT.items() if states.get(k) is not None and states[k] != v]
            if not states.get("Gain x1", False):
                todo.append("Gain x1")
            if not todo:
                break
            log.info("OLA: changing %s", todo[0])
            self._write(_option(menu, re.escape(todo[0])).encode() + b"\r")
            menu = yield ADC_MENU, timeout, "the ADS1015 menu"
        else:
            raise OlaError("ADS1015 settings did not converge")
        self._write(b"x")
        yield DEVICE_MENU, timeout, "the device menu"
        self._write(b"x")
        yield MAIN_MENU, timeout, "the main menu"
        self._write(b"h")           # prints the header and returns to logging
        yield HEADER, timeout, "the header"

    def _start_configure(self, now):
        self.state = CONFIGURING
        self.error = "setting the ADS1015"
        self._held = []
        self._dialogue = self._dialogue_steps()
        self._advance(None, now)

    def _advance(self, text, now):
        try:
            regex, timeout, what = self._dialogue.send(text)
            self._wait = (regex, now + timeout, what)
        except StopIteration:
            self._dialogue = self._wait = None
            self.configures += 1
            self.state = STREAMING
            self._last_valid_row_at = self._drained_at = now
            if self.configured:
                self.error = ""
                self._good_since = now
                log.info("OLA: configured, columns %s", ",".join(self.columns))
            else:
                self._configure_failed(f"columns after configuring lack {'/'.join(AMP_COLUMNS)}", now)
        except Exception as e:      # OlaError, a port error while writing, or a bug in the dialogue
            self._dialogue = self._wait = None
            if isinstance(e, OlaError):
                self._configure_failed(str(e), now)
            else:
                self._lost(f"port error: {_why(e)}", now)

    def _configure_failed(self, why, now):
        self._dialogue = self._wait = None
        self.state = STREAMING          # rows, if any arrive, are still read
        self.configured = False
        self._good_since = None
        self._failures += 1
        self.error = f"not configured: {why}"
        self._next_configure = now + self._retry_s
        self._last_valid_row_at = now   # if no row comes either, the port is reopened silent_s from now
        log.warning("OLA: %s; next try in %.0f s (failure %d in a row)", self.error, self._retry_s, self._failures)
        self._retry_s = min(self._retry_s * 2, self.cfg["retry_max_s"])

    def _restarted(self, now):
        """The OLA printed its start-up banner on an open connection: it has reset and lost its settings."""
        self.restarts += 1
        log.warning("OLA: it restarted by itself; setting it up again")
        self._dialogue = self._wait = None
        self._held = []
        self.columns, self.configured, self._good_since = None, False, None
        self.state, self.error = BOOTING, "restarted by itself; starting"
        self._boot_at = now
        self._header_seen = False
        self._next_configure = None

    # ---- reading

    def _set_header(self, line):
        self.columns = unique(line.strip().strip(",").split(","))
        self._index = {name: i for i, name in enumerate(self.columns)}
        self.configured = all(c in self._index for c in AMP_COLUMNS)
        self._header_seen = True

    def _parse_row(self, line):
        fields = line.split(",")
        if fields and fields[-1] == "":
            fields.pop()                # every line ends with a comma
        if len(fields) != len(self.columns):
            return None                 # cut off, two lines run together, or rows of another header
        row = {"columns": self.columns, "fields": fields, "amp": None, "age_s": 0.0}
        if self.configured:
            try:
                raw = [float(fields[self._index[c]]) for c in AMP_COLUMNS]
            except ValueError:
                return None
            if not all(math.isfinite(v) for v in raw):
                return None
            row["amp"] = dict(zip(AMP_VALUES, amp_values(self.cfg, *raw)))
        clock = self._index.get("rtcTime")
        row["clock"] = _clock_seconds(fields[clock]) if clock is not None else None
        return row

    def _read(self, now):
        """Read the port until it is empty (or the byte limit) and parse what came. Sets `drained`."""
        chunks, total = [], 0
        self.drained = False
        try:
            while total < MAX_POLL_BYTES:
                data = self._port.read(65536)       # one read returns what the tty hands out, about 4 kB
                if not data:
                    self.drained = True
                    break
                chunks.append(data)
                total += len(data)
        except Exception as e:      # unplugged: pyserial raises SerialException or OSError
            self._lost(f"port lost: {_why(e)}", now)
            return
        if not chunks:
            self._quiet_polls += 1
            return
        data = b"".join(chunks)
        self._quiet_polls = 0
        if self._wait is not None:
            self._text = (self._text + data.decode("ascii", "replace"))[-MAX_TEXT:]
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        if len(self._buf) > MAX_LINE:
            self._buf = b""
            self.bad_lines += 1
        for raw in lines:
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("rtcDate,"):
                self._set_header(line)
            elif ROW.match(line):
                if self.state != STREAMING or self._dialogue is not None or self.columns is None:
                    continue            # rows before and during the menus are not used
                row = self._parse_row(line)
                if row is None:
                    self.bad_lines += 1
                else:
                    self._held.append(row)
                    self._last_valid_row_at = now
            elif BANNER.match(line):
                if self.state != BOOTING or self._header_seen:
                    self._restarted(now)
            elif line and self.state == STREAMING and self._dialogue is None:
                self.bad_lines += 1     # in the other states these are banners and menus
        if len(self._held) > MAX_HELD_ROWS:
            self.dropped_rows += len(self._held) - MAX_HELD_ROWS
            del self._held[:-MAX_HELD_ROWS]

    def _release(self, now):
        """Return the held rows with their ages, once the port has been read empty."""
        if not self.drained:
            return []                   # the newest row is not known to be recent: wait for the rest
        window = max(now - self._drained_at, 0.0) if self._drained_at is not None else 0.0
        self._drained_at = now
        rows, self._held = self._held, []
        if not rows:
            return rows
        self.window_s = window
        newest = rows[-1]["clock"]
        floor = self._last_row_time if self._last_row_time is not None else now - window
        for i, row in enumerate(rows):
            behind = None
            if newest is not None and row["clock"] is not None:
                behind = (newest - row["clock"]) % 86400.0      # by the OLA's own clock
                if behind > window + 2.0:
                    behind = None                               # that clock jumped: not usable
            if behind is None:
                behind = (len(rows) - 1 - i) * window / len(rows)   # evenly over the window
            floor = max(floor, now - behind)        # never before the row returned before it
            row["age_s"] = max(now - floor, 0.0)
        self._last_row_time = now
        self.rows += len(rows)
        return rows

    def poll(self, now=None):
        """Advance the connection one step. Returns the rows read: dicts with columns, fields, amp, age_s."""
        now = self._monotonic() if now is None else now
        if self._port is None:
            present = self._exists(self.cfg["port"])
            # Plugged in again: open at once, the OLA has just started anyway. Not for a device that
            # drops off USB and returns within seconds again and again: that one waits like any failure.
            appeared = present and self._missing_since is not None and (
                self._failures <= 1 or now - self._missing_since >= REPLUG_S)
            if not present and self._missing_since is None:
                self._missing_since = now
            elif present:
                self._missing_since = None
            self.port_present = present
            if appeared or now >= self._next_open:
                self._open(now)
            return []
        self._read(now)
        if self._port is None:
            return []
        rows = self._release(now)

        if self.state == BOOTING:
            # Ready when it has printed its header. If none comes (the open did not reset it, or the
            # line was lost), the menus are driven anyway: the dialogue ends by printing the header.
            if self._header_seen or now - self._boot_at > self.cfg["boot_timeout_s"]:
                self._start_configure(now)
        elif self._wait is not None:
            regex, deadline, what = self._wait
            # A menu is complete when it matches and nothing more arrives; rows follow the header at once.
            if regex.search(self._text) and (regex is HEADER or self._quiet_polls >= 1):
                self._advance(self._text, now)
            elif now > deadline:
                self._configure_failed(f"the OLA did not answer (waiting for {what})", now)
        elif now - self._last_valid_row_at > self.cfg["silent_s"]:
            # Silence, garbage, or rows that do not fit the header any more: start again from a reset.
            self._lost(f"no valid row for {now - self._last_valid_row_at:.0f} s", now)
        elif not self.configured and (self._next_configure is None or now >= self._next_configure):
            # A header without the differential columns, or an earlier try at the menus failed.
            log.info("OLA: not set up for %s; driving the menus", "/".join(AMP_COLUMNS))
            self._start_configure(now)
        elif self.configured and self._failures and self._good_since is not None \
                and now - self._good_since >= self.cfg["stable_s"]:
            log.info("OLA: good rows for %.0f s; the waits after a failure start short again", self.cfg["stable_s"])
            self._failures = 0
            self._reopen_s, self._retry_s = self.cfg["reopen_s"], self.cfg["retry_s"]
        return rows
