"""Making the amplifier safe: when the reset tool runs, and the record of the SDR's state.

The tool (stage 3) is `tools/sdr_gpio_safe/amp_safe --timeout N`. It drives the
SDR's amplifier-enable pin to the shutdown level and verifies it. It must never
be called while a radar process is alive, and it serialises concurrent callers.
Every run of it initialises UHD, which itself enables the amplifier for about
0.3-0.5 s, so it is not run when it is not needed.

Exit status of the tool
    0   OK: the only safe result
    3   ASSUME_ENABLED: this run initialised the SDR and did not verify the pin.
        Urgent: retried at once, then a fault
    2 NOT_FOUND, 6 BUSY (SDR in use, e.g. by a radar), 7 AMBIGUOUS, 4 TIMEOUT,
    5 FAILED, 64 USAGE: the state is unknown and was not changed by the call

Policy (decided by the user, 2026-11-19): **the tool runs only when the pin is
not known to be safe.** Setting `manager.amp_safe.policy`:

    when_needed   (default) as above
    always        run the tool after every radar exit as well

Known safe means one of:
  * the last thing that initialised the SDR was the radar (with the amplifier
    pin configured: the config limits enforce that) and it exited cleanly;
  * the tool ran successfully since;
  * nothing has initialised the SDR since it was powered (it is still in its
    boot loader).
Where a fresh amplifier reading with the 28 V rail present is available, taken
with no radar process, the current decides instead: shutdown current is safe,
enabled current is not, whatever the history says.
Everything else is "unknown" and the tool runs: a radar that was killed,
crashed, exited non-zero or never reached its running state; a tool run that
did not succeed; no record at all (first start, or the record was lost).

The record is the file <state_dir>/sdr-state. Its first line is the word
`safe` or `unknown`; the rest is free text for people. A missing or unreadable
file means unknown. It is set to `unknown` before every radar launch. Other
programs (the manager unit's stop hook, a power-loss hook) can read it:

    [ "$(head -n1 /var/lib/radar-runner/user/sdr-state 2>/dev/null)" = safe ]

The decisions are the three pure functions `known_safe_after_exit`, `decide`
and `idle_need`; runner.py and service.py only carry them out.
"""
import os
import time

from radar_runner.common.status import boottime_s, read_status, status_path

from .gates import StatusFileGate, _lookup

ALWAYS, WHEN_NEEDED = "always", "when_needed"
SAFE, UNKNOWN = "safe", "unknown"
STATE_FILE = "sdr-state"
OK, ASSUME_ENABLED = 0, 3
KILLED_BY_CALLER = -1       # not a status of the tool: the caller killed it (its own, longer limit passed)
EXIT_MEANING = {0: "OK", 2: "NOT_FOUND", 3: "ASSUME_ENABLED", 4: "TIMEOUT", 5: "FAILED", 6: "BUSY",
                7: "AMBIGUOUS", 64: "USAGE"}
ABSENT, BOOTLOADER, LOADED = "absent", "bootloader", "loaded"       # the SDR, as sysfs shows it


def unclean(facts):
    """Why this radar exit was not clean, or "". facts: reached_running, killed, exit_status, failure."""
    if facts.get("killed"):
        return "the radar was killed"
    if not facts.get("reached_running"):
        return "the radar never reached its running state"
    if facts.get("exit_status") != 0:
        return f"the radar exited with status {facts.get('exit_status')}"
    if facts.get("failure"):
        return f"failure: {facts['failure']}"
    return ""


def known_safe_after_exit(facts, reading):
    """Is the pin known to be safe after this radar exit? Returns (known_safe, why).

    `reading` is (state, text) from the amplifier telemetry sampled after the
    exit: state True = shutdown current, False = enabled current, None = no
    usable reading (not fresh, or the 28 V rail is off).
    """
    state, text = reading
    if state is True:
        return True, f"telemetry: {text}"
    if state is False:
        return False, f"telemetry shows the amplifier enabled: {text}"
    reason = unclean(facts)
    if reason:
        return False, f"{reason}; no telemetry to decide ({text})"
    return True, f"clean radar exit; no telemetry to check ({text})"


def decide(policy, facts, read):
    """After a radar exit: should the tool run? Returns (run, known_safe, why).

    `read()` returns the reading for `known_safe_after_exit`. Under `always`
    it is not asked: the tool runs anyway, and the history alone says whether
    the pin was known safe before (which matters if the tool then fails
    without changing anything).
    """
    if policy != WHEN_NEEDED:
        known, why = known_safe_after_exit(facts, (None, "not asked"))
        label = "policy always" if policy == ALWAYS else f"unknown policy {policy!r}, treated as always"
        return True, known, f"{label}; {why}"
    known, why = known_safe_after_exit(facts, read())
    return (not known), known, why


def idle_need(state, sdr, reading, enabled_for_s, enabled_limit_s):
    """With no radar process: what to do about the SDR's state. Returns (action, why).

    action: "none", "mark_safe" or "run" (the tool).
    state            SAFE or UNKNOWN: the record
    sdr              ABSENT, BOOTLOADER or LOADED
    reading          True shutdown current / False enabled current / None no usable reading
    enabled_for_s    for how long the reading has said "enabled" without a break
    """
    if sdr == BOOTLOADER:
        if state != SAFE:
            return "mark_safe", "nothing has initialised the SDR since it was powered"
        return "none", ""
    if sdr == ABSENT:
        return "none", "the SDR is not on USB: nothing can be done or checked"
    if reading is True:
        if state != SAFE:
            return "mark_safe", "telemetry shows shutdown current with no radar process"
        return "none", ""
    if reading is False:
        # Not on one sample: every start of the tool and of the radar enables the amplifier briefly.
        if enabled_for_s >= enabled_limit_s:
            return "run", f"telemetry shows the amplifier enabled for {enabled_for_s:.0f} s with no radar process"
        return "none", ""
    if state != SAFE:
        return "run", "the state of the SDR is not known and there is no telemetry to decide"
    return "none", ""


def after_tool(status, known_before):
    """The record after a run of the tool. Returns (state, ok): ok = confirmed or still known safe."""
    if status == OK:
        return SAFE, True
    if status in (ASSUME_ENABLED, KILLED_BY_CALLER):
        return UNKNOWN, False       # the SDR may have been initialised and left unverified
    # Nothing was changed by the call (also: it could not be started): what was known before still holds.
    return (SAFE, True) if known_before else (UNKNOWN, False)


def describe(status):
    """Text for an exit status of the tool (None = it did not finish or could not be started)."""
    if status is None:
        return "not started"
    if status == KILLED_BY_CALLER:
        return "no result within the caller's limit, killed"
    return f"status {status} ({EXIT_MEANING.get(status, 'not a status of the tool')})"


class AmpTelemetry:
    """The amplifier current from the telemetry service's status file."""

    def __init__(self, config, read=read_status, uptime=boottime_s, monotonic=time.monotonic,
                 sleep=time.sleep):
        m = config["manager"]
        gate_cfg = m["gates"]["telemetry"]
        self.cfg = m["amp_safe"]
        self._gate = StatusFileGate("telemetry", gate_cfg["status_file"]
                                    or status_path("telemetry", config["paths"]["run_dir"]),
                                    gate_cfg, "amplifier telemetry", read)
        self._read, self._uptime, self._monotonic, self._sleep = read, uptime, monotonic, sleep

    def _sample(self):
        """(sample uptime, current, voltage) if the telemetry is fresh and complete, else None."""
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
        return values

    def _judge(self, sample):
        _, current, voltage = sample
        if voltage < self.cfg["min_voltage_v"]:
            return None, f"28 V rail reads {voltage:.1f} V: the current says nothing"
        return current < self.cfg["enabled_current_a"], f"amplifier current {current:.2f} A at {voltage:.1f} V"

    def now(self):
        """The latest reading: (True shutdown / False enabled / None unusable, text). Does not wait."""
        sample = self._sample()
        if sample is None:
            return None, "no fresh amplifier reading"
        return self._judge(sample)

    def confirm_shutdown(self, exit_uptime):
        """Wait for a reading taken after a radar exit. Returns (True/False/None, text)."""
        cfg = self.cfg
        deadline = self._monotonic() + cfg["confirm_timeout_s"]
        while True:
            sample = self._sample()
            if sample is not None and sample[0] >= exit_uptime + cfg["confirm_settle_s"]:
                return self._judge(sample)
            if self._monotonic() >= deadline:
                return None, "no fresh amplifier reading after the exit"
            self._sleep(0.2)


class SdrState:
    """<state_dir>/sdr-state: is the SDR's amplifier pin known to be safe? Missing or damaged = unknown."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, STATE_FILE)

    def get(self):
        try:
            with open(self.path, "rb") as f:
                first = f.readline(64).strip()
        except OSError:
            return UNKNOWN
        return SAFE if first == b"safe" else UNKNOWN

    @property
    def known_safe(self):
        return self.get() == SAFE

    def why(self):
        """The text recorded with the state, for the status."""
        try:
            with open(self.path, "rb") as f:
                f.readline(64)
                return f.read(300).decode("utf-8", "replace").strip()
        except OSError:
            return "no record"

    def set(self, state, why=""):
        """Record `safe` or `unknown`, durably, before returning. Returns "" or the error text."""
        tmp = self.path + ".tmp"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
            return ""
        except OSError as e:
            return f"{self.path}: {e.strerror}"
