"""Making the amplifier safe: when the reset tool runs, and the record of the SDR's state.

The tool (stage 3) is `tools/sdr_gpio_safe/amp_safe --timeout N --state-file PATH`.
It drives the SDR's amplifier-enable pin to the shutdown level and verifies it.
It must never be called while a radar process is alive, and it serialises
concurrent callers. Every run of it initialises UHD, which itself enables the
amplifier for about 0.3-0.5 s, so **it runs only when the pin is not known to
be safe** (decided by the user, 2026-11-19).

Exit status of the tool
    0   OK: the only safe result
    3   ASSUME_ENABLED: this run initialised the SDR and did not verify the pin.
        Urgent: run again at once, then a fault
    2 NOT_FOUND, 6 BUSY (SDR in use), 4 TIMEOUT (e.g. another caller holds its
        lock): the SDR may be coming back after a kill: tried again a few
        times with a delay before it counts as a failure
    7 AMBIGUOUS, 5 FAILED, 8 STATE_FILE (the record could not be written; the
        tool was not started), 64 USAGE: a failure
    After every result but 0 the record says `unknown`, also if it said `safe`
    before the call: the wrapper writes `unknown` before it starts the tool.

The record: <state_dir>/sdr-state. Its first line is the word `safe` or
`unknown`; the rest is free text for people. A missing or unreadable file means
unknown. Who writes it:
  * the tool's wrapper (--state-file): `unknown` before it starts the tool,
    `safe` after an OK. The manager does not write around a tool run;
  * the manager: `unknown` before the turn-on-check hook and before every
    radar launch; `safe` when it finds the pin safe without the tool (below).
    It never writes `safe` while a run of the tool by any program is in
    progress (it probes the wrapper's lock first): that run has written
    `unknown` and may yet be killed in the middle of the initialisation.
If the record cannot be written (the tool said OK, or the manager found the
pin safe), this manager remembers `safe` in memory, for as long as the file
stays as it is, so that the tool is not run again and again; the status shows
the problem. Other programs still read `unknown`.
Other programs can read it:

    [ "$(head -n1 /var/lib/radar-runner/user/sdr-state 2>/dev/null)" = safe ]

Known safe without the tool means one of:
  * the last thing that initialised the SDR was the radar (with the amplifier
    pin configured: the config limits enforce that), it reached its running
    state and exited cleanly;
  * nothing has initialised the SDR since it was powered (boot loader);
  * a fresh amplifier reading with the 28 V rail present, taken with no radar
    process and at least `confirm_settle_s` after the last radar or tool run
    ended, shows shutdown current. Where there is such a reading it decides,
    in both directions, whatever the history says: with one exception, a
    radar that was signalled before its start line (it has no SIGINT handler
    until then, so it died in the middle of the SDR set-up) is never trusted.
A reading counts only while the telemetry service's loop runs evenly
(`loop.last_gap_s` at most `max_loop_gap_s`): after a stall the rows that piled
up come out together and their times say little. A stall means "wait for the
next reading" (for at most `confirm_timeout_s`), not "no reading".

Enabled current seen with no radar process is the safety watcher's business:
it owns the rule "amplifier draws enabled current with no radar: run the tool".
The manager never marks the pin safe while it sees such current, shows it in
its status, and otherwise leaves it alone.

On a power loss (the stop came from the power service, or the 28 V rail reads
absent) no reading is waited for and the tool is not run: the current says
nothing without the rail, and the time belongs to the shutdown. If the radar
exited cleanly, `safe` is recorded from that alone (one write, before `radarctl
stop --wait` returns), so that the power service's hook need not run the tool;
otherwise the record is left as it is and that hook runs it.

Urgency: when a power loss or the manager's own exit begins while the manager
waits for a reading or runs the tool, it stops waiting, finishes the attempt
in progress (the tool is never killed early) and makes no further attempt.

The decisions are the pure functions `known_safe_after_exit`, `after_exit` and
`idle_need`; runner.py and service.py only carry them out.
"""
import fcntl
import os
import time

from radar_runner.common.status import boottime_s, read_status

from .gates import StatusFileGate, _lookup, status_file

SAFE, UNKNOWN = "safe", "unknown"
STATE_FILE = "sdr-state"
OK, ASSUME_ENABLED = 0, 3
KILLED_BY_CALLER = -1       # not a status of the tool: the caller killed it (its own, longer limit passed)
EXIT_MEANING = {0: "OK", 2: "NOT_FOUND", 3: "ASSUME_ENABLED", 4: "TIMEOUT", 5: "FAILED", 6: "BUSY",
                7: "AMBIGUOUS", 8: "STATE_FILE", 64: "USAGE"}
RETRY_LATER = (2, 6, 4)     # NOT_FOUND, BUSY, TIMEOUT: worth another try after a delay
ABSENT, BOOTLOADER, LOADED = "absent", "bootloader", "loaded"       # the SDR, as sysfs shows it
PENDING = "pending"         # a reading: shutdown current, but not yet read long enough after the last run


def unclean(facts):
    """Why this radar exit was not clean, or "".

    facts: reached_running, killed, exit_status, failure, signalled_before_start.
    """
    if facts.get("signalled_before_start"):
        return "the radar was signalled before its start line, in the middle of the SDR set-up"
    if facts.get("killed"):
        return "the radar was killed"
    if not facts.get("reached_running"):
        return "the radar never reached its running state"
    if facts.get("exit_status") != 0:
        return f"the radar exited with status {facts.get('exit_status')}"
    if facts.get("failure"):
        return f"failure: {facts['failure']}"
    return ""


def known_safe_after_exit(facts, read):
    """Is the pin known to be safe after this radar exit? Returns (known_safe, why).

    `read()` gives (state, text) from the amplifier telemetry sampled after
    the exit: True = shutdown current, False = enabled current, None = no
    usable reading. It is not called when the answer does not depend on it.
    """
    if facts.get("signalled_before_start"):
        return False, unclean(facts)
    state, text = read()
    if state is True:
        return True, f"telemetry: {text}"
    if state is False:
        return False, f"telemetry shows the amplifier enabled: {text}"
    reason = unclean(facts)
    if reason:
        return False, f"{reason}; no telemetry to decide ({text})"
    return True, f"clean radar exit; no telemetry to check ({text})"


def after_exit(facts, read, power_lost):
    """What to do after a radar exit. Returns (action, why).

    "leave"  power loss after an exit that was not clean: check nothing, write nothing
    "safe"   the pin is known to be safe: record it
    "run"    run the tool

    On a power loss `read` is not called.
    """
    if power_lost:
        reason = unclean(facts)
        if reason:
            return "leave", f"power loss ({power_lost}) and {reason}: nothing checked, the record is left as it is"
        return "safe", f"clean radar exit; power loss ({power_lost}): no reading waited for"
    known, why = known_safe_after_exit(facts, read)
    return ("safe" if known else "run"), why


def idle_need(state, sdr, reading):
    """With no radar process: what to do about the SDR's state. Returns (action, why).

    action: "none", "mark_safe" or "run" (the tool).
    state     SAFE or UNKNOWN: the record
    sdr       ABSENT, BOOTLOADER or LOADED
    reading   True = shutdown current in a reading new enough to count,
              False = enabled current, None = no usable reading,
              PENDING = shutdown current, and a reading that counts is due shortly

    The tool runs only if the record is not `safe` and the SDR has been
    initialised since it was powered. Enabled current while the record says
    `safe` is left to the safety watcher.
    """
    if sdr == ABSENT:
        return "none", "the SDR is not on USB: nothing can be done or checked"
    if reading is False:
        if state != SAFE and sdr == LOADED:
            return "run", "the state of the SDR is not known and telemetry shows the amplifier enabled"
        return "none", "enabled current with no radar process: left to the safety watcher"
    if sdr == BOOTLOADER:
        if state != SAFE:
            return "mark_safe", "nothing has initialised the SDR since it was powered"
        return "none", ""
    if reading is True:
        if state != SAFE:
            return "mark_safe", "telemetry shows shutdown current with no radar process"
        return "none", ""
    if reading == PENDING:
        return "none", "waiting for a reading taken after the last run"
    if state != SAFE:
        return "run", "the state of the SDR is not known and there is no telemetry to decide"
    return "none", ""


def describe(status):
    """Text for an exit status of the tool (None = it could not be started)."""
    if status is None:
        return "not started"
    if status == KILLED_BY_CALLER:
        return "no result within the caller's limit, killed"
    return f"status {status} ({EXIT_MEANING.get(status, 'not a status of the tool')})"


def tool_lock_path(amp_safe_cfg):
    """The file the tool's wrapper locks while it runs: the wrapper script itself."""
    given = amp_safe_cfg.get("lock_file", "-")
    if given not in ("-", ""):
        return given
    command = amp_safe_cfg["command"]
    return str(command[0]) if command else ""


def tool_running(lock_path):
    """True if some program's run of the tool holds the wrapper's lock right now. Never blocks.

    A shared lock is asked for: it fails only against a running wrapper (which
    holds the exclusive lock), not against another probe. A file that cannot
    be opened means that no tool can be running.
    """
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except (OSError, TypeError, ValueError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def tool_timeout(command):
    """The value after --timeout in the tool's command line, or None."""
    args = [str(a) for a in command]
    try:
        return float(args[args.index("--timeout") + 1])
    except (ValueError, IndexError):
        return None


class AmpTelemetry:
    """The amplifier current from the telemetry service's status file."""

    def __init__(self, config, read=read_status, uptime=boottime_s, monotonic=time.monotonic,
                 sleep=time.sleep):
        m = config["manager"]
        gate_cfg = m["gates"]["telemetry"]
        self.cfg = m["amp_safe"]
        self._gate = StatusFileGate("telemetry", status_file(gate_cfg, "telemetry", config["paths"]["run_dir"]),
                                    gate_cfg, "amplifier telemetry", read)
        self._read, self._uptime, self._monotonic, self._sleep = read, uptime, monotonic, sleep
        self._stalled_since = None      # monotonic time since which every reading came from a stalled loop

    def _sample(self):
        """(sample uptime, current, voltage, loop even) if the telemetry is fresh and complete, else None.

        `loop even` is False if the telemetry service's loop has just stalled,
        None if it does not say: the reading is then no proof of shutdown.
        """
        if self._gate.problem():
            return None
        r = self._read(self._gate.path, self._gate.cfg["max_age_s"])
        if not r.ok:
            return None
        try:
            values = [_lookup(r.data, self.cfg[key]) for key in
                      ("sample_uptime_field", "current_field", "voltage_field")]
        except KeyError:
            return None
        if not all(type(v) in (int, float) for v in values):
            return None
        try:
            gap = _lookup(r.data, self.cfg["loop_gap_field"])
        except KeyError:
            gap = None
        even = (0 <= gap <= self.cfg["max_loop_gap_s"]) if type(gap) in (int, float) else None
        return values + [even]

    def rail_absent(self):
        """True if the latest fresh reading shows the 28 V rail off (a power loss)."""
        sample = self._sample()
        return sample is not None and sample[2] < self.cfg["min_voltage_v"]

    def _judge(self, sample):
        """(True shutdown / False enabled / None says nothing, text) for a sample that is new enough."""
        _, current, voltage, even = sample
        if voltage < self.cfg["min_voltage_v"]:
            return None, f"28 V rail reads {voltage:.1f} V: the current says nothing"
        text = f"amplifier current {current:.2f} A at {voltage:.1f} V"
        if current >= self.cfg["enabled_current_a"]:
            return False, text
        if even is None:
            return None, f"{text}, but the telemetry service does not report its loop"
        if not even:
            return None, f"{text}, but the telemetry loop has just stalled"
        return True, text

    def now(self, after_uptime):
        """The latest reading, without waiting: (True / False / PENDING / None, text).

        True = shutdown current, False = enabled current, None = unusable.
        Shutdown current counts only in a sample read at least `confirm_settle_s`
        after `after_uptime` (the end of the last radar or tool run, or the
        manager's start). The sample's time is the earliest moment the row can
        have reached the Pi; the ADC measured it before that, so an older row
        may show the state before the run ended. While such a sample can
        still be expected (`confirm_timeout_s` more), the answer is PENDING.
        Shutdown current read while the telemetry loop was stalled is PENDING
        too, until the stall has lasted `confirm_timeout_s`.
        """
        sample = self._sample()
        if sample is None:
            return None, "no fresh amplifier reading"
        state, text = self._judge(sample)
        if state is None and sample[3] is False and sample[2] >= self.cfg["min_voltage_v"]:
            if self._stalled_since is None:
                self._stalled_since = self._monotonic()
            if self._monotonic() - self._stalled_since < self.cfg["confirm_timeout_s"]:
                return PENDING, f"{text}; waiting for the next reading"
            return None, text
        self._stalled_since = None
        settle = self.cfg["confirm_settle_s"]
        if state is True and sample[0] < after_uptime + settle:
            if self._uptime() < after_uptime + settle + self.cfg["confirm_timeout_s"]:
                return PENDING, f"{text}; waiting for a reading taken {settle:g} s after the last run ended"
            return None, f"{text}, but not read {settle:g} s after the last run ended"
        return state, text

    def confirm_shutdown(self, exit_uptime, urgent=None):
        """Wait for a reading taken after a radar exit. Returns (True/False/None, text).

        `urgent()` turning true (a power loss, the manager's exit) ends the wait.
        """
        cfg = self.cfg
        deadline = self._monotonic() + cfg["confirm_timeout_s"]
        while True:
            if urgent is not None and urgent():
                return None, "the wait for a reading was cut short: the stop became urgent"
            sample = self._sample()
            if sample is not None and sample[0] >= exit_uptime + cfg["confirm_settle_s"]:
                state, text = self._judge(sample)
                if state is not None or self._monotonic() >= deadline:
                    return state, text
            if self._monotonic() >= deadline:
                return None, "no fresh amplifier reading after the exit"
            self._sleep(0.2)


class SdrState:
    """<state_dir>/sdr-state: is the SDR's amplifier pin known to be safe? Missing or damaged = unknown.

    `lock_path` is the file that the tool's wrapper locks (`tool_lock_path`).
    `problem` is "" or why the record could not be brought up to date; the
    state is then remembered here (`remember_safe`) while the file stays as it is.
    """

    def __init__(self, state_dir, lock_path=""):
        self.path = os.path.join(state_dir, STATE_FILE)
        self.lock_path = lock_path
        self.problem = ""
        self._remembered = None     # identity of the file when `safe` was remembered in memory

    def _identity(self):
        try:
            st = os.stat(self.path)
            return (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            return ("missing",)

    def _file_state(self):
        try:
            with open(self.path, "rb") as f:
                first = f.readline(64).strip()
        except OSError:
            return UNKNOWN
        return SAFE if first == b"safe" else UNKNOWN

    def get(self):
        if self._file_state() == SAFE:
            self._remembered, self.problem = None, ""
            return SAFE
        if self._remembered is not None:
            if self._remembered == self._identity():
                return SAFE
            self._remembered, self.problem = None, ""       # somebody wrote the record since
        return UNKNOWN

    def remember_safe(self, problem):
        """The pin is safe but the record could not say so: keep it in memory while the file stays as it is."""
        self._remembered = self._identity()
        self.problem = f"the record {self.path} could not be written ({problem}); `safe` is kept in memory only"

    def mark_safe(self, why):
        """Record `safe`, unless a run of the tool is in progress. Returns (done, text).

        Not done: a tool run (of any program) holds the wrapper's lock; it
        has written `unknown` and the outcome is its to record. Done with a
        text: the file could not be written and `safe` is remembered.
        """
        if self.lock_path and tool_running(self.lock_path):
            return False, "a run of the make-amplifier-safe tool is in progress: the record is left to it"
        error = self.set(SAFE, why)
        if error:
            self.remember_safe(error)
        return True, error

    @property
    def known_safe(self):
        return self.get() == SAFE

    def why(self):
        """The text recorded with the state, for the status."""
        try:
            with open(self.path, "rb") as f:
                f.readline(64)
                text = f.read(300).decode("utf-8", "replace").strip()
        except OSError:
            text = "no record"
        return f"{self.problem}; the file says: {text}" if self._remembered is not None else text

    def set(self, state, why=""):
        """Record `safe` or `unknown`, durably, before returning. Returns "" or the error text."""
        tmp = self.path + ".tmp"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._remembered = None
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, f"{SAFE if state == SAFE else UNKNOWN}\n{stamp} {why}\n".encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            fd = os.open(os.path.dirname(self.path) or ".", os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self.problem = ""
            return ""
        except OSError as e:
            return f"{self.path}: {e.strerror}"
