"""The "make the amplifier safe" step after a radar exit: whether to run the tool, and the SDR-state flag.

The tool (stage 3: `tools/sdr_gpio_safe/amp_safe --timeout N`) drives the
SDR's amplifier-enable pin to the shutdown level and verifies it. Exit status
0 is the only success. It may only be called when no radar process exists, and
it serialises concurrent callers itself.

Every run of the tool initialises UHD, and that initialisation enables the
amplifier for about 0.3-0.5 s before the tool's write lands. After a clean
radar exit the pin is already safe, so an unconditional run costs one
unnecessary enable per stop. Which is better is NOT DECIDED; the choice is the
setting `manager.amp_safe.policy`, and `decide` below is the one place that
implements it:

    always        run the tool after every radar exit (the plan as written; the default)
    when_needed   run it unless the exit was clean AND fresh amplifier telemetry,
                  sampled after the exit with the 28 V rail present, shows
                  shutdown current

"Clean" means: the radar reached its running state, was not killed, and
exited with status 0 without a failure being recorded.

The flag file <state_dir>/sdr-state-unknown exists while the SDR's GPIO state
is not known to be safe: it is created before every radar launch and removed
after a successful run of the tool or a clean exit confirmed by telemetry.
Anything that later has to decide whether to run the tool (the manager unit's
stop hook, a power-loss hook) can test for the file.
"""
import os
import time

from radar_runner.common.status import boottime_s, read_status, status_path

from .gates import StatusFileGate, _lookup

ALWAYS, WHEN_NEEDED = "always", "when_needed"
FLAG_FILE = "sdr-state-unknown"
EXIT_MEANING = {2: "SDR not found", 3: "not verified", 4: "timeout", 5: "failed", 64: "usage error"}


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


def decide(policy, facts, confirm):
    """Should the amp-safe command run now? Returns (run, why).

    `confirm()` is called only if everything else allows skipping. It returns
    (state, text): state True = telemetry shows the amplifier in shutdown,
    False = it shows enabled current, None = no usable telemetry.
    Anything but an explicit, confirmed clean exit under `when_needed` runs the tool.
    """
    if policy != WHEN_NEEDED:
        return True, "policy always" if policy == ALWAYS else f"unknown policy {policy!r}: treated as always"
    reason = unclean(facts)
    if reason:
        return True, reason
    state, text = confirm()
    if state is True:
        return False, f"clean exit confirmed by telemetry ({text})"
    if state is False:
        return True, f"telemetry shows the amplifier enabled after the exit ({text})"
    return True, f"clean exit not confirmed by telemetry ({text})"


class AmpTelemetry:
    """Reads the amplifier current from the telemetry service's status file after a radar exit."""

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

    def confirm_shutdown(self, exit_uptime):
        """Wait for a sample taken after the exit. Returns (True/False/None, text)."""
        cfg = self.cfg
        deadline = self._monotonic() + cfg["confirm_timeout_s"]
        while True:
            sample = self._sample()
            if sample is not None and sample[0] >= exit_uptime + cfg["confirm_settle_s"]:
                _, current, voltage = sample
                if voltage < cfg["min_voltage_v"]:
                    return None, f"28 V rail reads {voltage:.1f} V: the current says nothing"
                return current < cfg["enabled_current_a"], f"amplifier current {current:.2f} A at {voltage:.1f} V"
            if self._monotonic() >= deadline:
                return None, "no fresh amplifier reading after the exit"
            self._sleep(0.2)


class SdrStateFlag:
    """<state_dir>/sdr-state-unknown: exists while the SDR GPIO state is not known to be safe."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, FLAG_FILE)

    def _sync_dir(self):
        fd = os.open(os.path.dirname(self.path) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def is_set(self):
        return os.path.lexists(self.path)

    def set(self, note=""):
        """Returns "" or the error text. Durable before it returns."""
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, (note + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            self._sync_dir()
            return ""
        except OSError as e:
            return f"{self.path}: {e.strerror}"

    def clear(self):
        try:
            os.unlink(self.path)
            self._sync_dir()
        except FileNotFoundError:
            pass
        except OSError as e:
            return f"{self.path}: {e.strerror}"
        return ""
