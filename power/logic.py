"""Decision logic of the power service: a pure state machine.

No I/O and no clock in here. The caller passes timestamped samples to
`PowerLogic.step()` and completion events to `PowerLogic.done()`; both return a
list of `Action`s for the caller to carry out. All times are seconds on one
monotonic clock.

Two signals are tracked, "current" (INA219 capacitor current) and "gpio"
(GPIO 26). Each has a debounced state:

    unknown     valid readings are arriving but nothing is confirmed yet
    present     main power present
    loss        main power lost
    unreadable  no valid reading for `unreadable_after_s`
    faulty      said "loss" for longer than the capacitor can carry the Pi
                while the Pi kept running; treated like unreadable

Phases:

    normal      no healthy signal says loss
    loss        at least one does: the radar is asked to stop. The ride-through
                timer runs only while the agreement rule holds without a break
    committing  shutdown committed; the steps below run in order, no abort
    shutdown    the power-off or reboot command has been issued
"""
from dataclasses import dataclass

PRESENT, LOSS, UNREADABLE, FAULTY, UNKNOWN = "present", "loss", "unreadable", "faulty", "unknown"
NORMAL, LOSS_PHASE, COMMITTING, SHUTDOWN = "normal", "loss", "committing", "shutdown"

INFO, WARNING, ERROR = 6, 4, 3  # syslog levels

COMMIT_STEPS = ("stop_radar", "amp_safe", "sync", "record", "decide")
HOOK_MARGIN_S = 1.0      # the logic gives up on a hook this long after its own timeout
RECORD_TIMEOUT_S = 3.0   # writing the guard state
DECIDE_EXTRA_S = 1.0     # longest wait, beyond restore_confirm_s, for a restore in progress
REFIRE_S = 10.0          # repeat the last-resort power command this often
LOW_VOLTAGE_ALONE_S = 2.0  # capacitor discharging below the floor for this long commits by itself


@dataclass(frozen=True)
class Action:
    """Something the caller must do.

    kind        name                 data
    "log"       -                    (level, message)
    "hook"      "stop_radar" |       -          report with done("hook:<name>", ok)
                "amp_safe"
    "sync"      -                    -          report with done("sync", ok)
    "record"    -                    guard dict report with done("record", ok)
    "save_guard" -                   guard dict (not awaited)
    "power"     "poweroff"|"reboot"  level 0-1  report with done("power", ok)
    """
    kind: str
    name: str = ""
    data: object = None


class SignalTracker:
    """Debounces the raw samples of one signal."""

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.state = UNKNOWN
        self.state_since = None
        self.raw = None           # last sample: PRESENT, LOSS or None (read error)
        self.cand = None          # value of the current run of valid samples
        self.cand_since = None
        self.cand_n = 0
        self.err_since = None     # start of the current run of read errors
        self.errors = 0

    @property
    def restoring(self):
        """Not confirmed present, but the latest valid sample says present."""
        return self.cand == PRESENT and self.state in (LOSS, UNKNOWN)

    @property
    def healthy(self):
        return self.state not in (UNREADABLE, FAULTY)

    def _set(self, t, state):
        old, self.state, self.state_since = self.state, state, t
        return (old, state)

    def update(self, t, raw):
        """Feed one sample. Returns a list of (old_state, new_state) changes."""
        cfg = self.cfg
        changes = []
        if self.state_since is None:
            self.state_since = t
        self.raw = raw
        if raw is None:
            self.errors += 1
            if self.err_since is None:
                self.err_since = t
            if self.state == FAULTY:
                self.cand = None      # recovery needs uninterrupted "present"
            elif self.state != UNREADABLE and t - self.err_since >= cfg.unreadable_after_s:
                self.cand = None
                changes.append(self._set(t, UNREADABLE))
        else:
            self.err_since = None
            if raw != self.cand:
                self.cand, self.cand_since, self.cand_n = raw, t, 1
            else:
                self.cand_n += 1
            held = t - self.cand_since
            if self.state == FAULTY:
                if self.cand == PRESENT and held >= cfg.faulty_clear_s:
                    changes.append(self._set(t, PRESENT))
            else:
                if self.state == UNREADABLE:
                    changes.append(self._set(t, UNKNOWN))
                if self.cand == LOSS and self.state != LOSS and self.cand_n >= cfg.loss_confirm_samples:
                    changes.append(self._set(t, LOSS))
                elif (self.cand == PRESENT and self.state != PRESENT and self.cand_n >= 2
                      and held >= cfg.restore_confirm_s):
                    changes.append(self._set(t, PRESENT))
        if self.state == LOSS and t - self.state_since >= cfg.faulty_after_s:
            changes.append(self._set(t, FAULTY))
        return changes


class PowerLogic:
    def __init__(self, cfg, guard_count=0, passive=False):
        self.cfg = cfg
        self.current = SignalTracker("current", cfg)
        self.gpio = SignalTracker("gpio", cfg)
        self.signals = (self.current, self.gpio)

        self.phase = NORMAL
        self.loss_since = None
        self.agree_since = None           # since when the agreement rule has held without a break
        self.discharge_low_since = None   # since when the capacitor has discharged below the floor
        self.stop_requested = False
        self.commit_ready = False      # commit conditions hold right now (loss phase)
        self._would_logged = False
        self.low_v_n = 0
        self.low_voltage = False
        self.disagree_since = None

        self.armed = False
        self.mode_reasons = None       # None until the first step
        self.guard_count = guard_count
        self.passive = bool(passive) or guard_count >= cfg.max_early_shutdowns
        self._guard_dirty = self.passive != bool(passive)
        self._count_reset_done = False
        self.both_present_since = None

        self.hooks = {"stop_radar": None, "amp_safe": None}  # None | "running" | "ok" | "failed"
        self.hook_started = {}
        self.step_name = None
        self.step_deadline = None
        self.commit_t = None
        self.commit_why = None
        self.power_kind = None
        self.power_level = None
        self.power_t = None
        self.power_fired_t = None

        self.warnings = {}
        self.t = None
        self.uptime_s = None
        self.current_a = None
        self.voltage_v = None
        self.gpio_value = None

    # ------------------------------------------------------------------ inputs

    def step(self, t, current_a, voltage_v, gpio, uptime_s, disable_file=False, guard_ok=True):
        """Feed one sample. A value of None means that reading failed."""
        out = []
        cfg = self.cfg
        self.t, self.uptime_s = t, uptime_s
        self.current_a, self.voltage_v, self.gpio_value = current_a, voltage_v, gpio

        raw_i = None if current_a is None else (LOSS if current_a < cfg.loss_current_a else PRESENT)
        raw_g = None if gpio is None else (PRESENT if gpio else LOSS)
        for sig, raw in ((self.current, raw_i), (self.gpio, raw_g)):
            for old, new in sig.update(t, raw):
                level = INFO if new in (PRESENT, UNKNOWN) else WARNING
                self._log(out, level, f"signal {sig.name}: {old} -> {new} ({self._readings()})")

        if voltage_v is None:
            self.low_v_n = 0
        elif voltage_v < cfg.low_voltage_v:
            self.low_v_n += 1
        else:
            self.low_v_n = 0
        self.low_voltage = self.low_v_n >= cfg.loss_confirm_samples
        # Confirmed discharge with the capacitor below the floor. A read error
        # leaves the timer alone; a present sample or a higher voltage clears it.
        if self.current.state != LOSS:
            self.discharge_low_since = None
        elif current_a is not None:
            if raw_i == LOSS and voltage_v < cfg.low_voltage_v:
                if self.discharge_low_since is None:
                    self.discharge_low_since = t
            else:
                self.discharge_low_since = None

        self._update_mode(disable_file, guard_ok, out)
        self._guard_timers(t, uptime_s, out)

        if self.phase in (NORMAL, LOSS_PHASE):
            self._evaluate(t, out)
        elif self.phase == COMMITTING:
            self._advance_commit(t, out)
        else:
            self._escalate(t, out)

        self._update_warnings(t, guard_ok, out)
        return out

    def done(self, t, what, ok):
        """Report that an awaited action finished ("hook:<name>", "sync", "record", "power")."""
        out = []
        if what.startswith("hook:"):
            name = what[5:]
            if self.hooks.get(name) != "running":
                return out
            self.hooks[name] = "ok" if ok else "failed"
            if not ok:
                self._log(out, WARNING, f"hook {name} failed or timed out: continuing without it")
            if self.phase == COMMITTING and self.step_name == name:
                self._next_step(t, out)
            elif self.phase == NORMAL:
                self.hooks[name] = None
        elif what in ("sync", "record"):
            if self.phase == COMMITTING and self.step_name == what:
                if not ok:
                    self._log(out, ERROR, f"{what} failed: continuing with the shutdown")
                self._next_step(t, out)
        elif what == "power":
            if not ok and self.phase == SHUTDOWN:
                self._log(out, ERROR, f"{self.power_kind} command (level {self.power_level}) failed")
                if self.power_level == 0:
                    self._fire(t, 1, out)
        return out

    # ------------------------------------------------------------- mode, guard

    def _update_mode(self, disable_file, guard_ok, out):
        reasons = []
        if not self.cfg.armed:
            reasons.append("config")
        if disable_file:
            reasons.append("disable_file")
        if self.passive:
            reasons.append("boot_loop_guard")
        if not guard_ok:
            reasons.append("guard_state_unwritable")
        if reasons != self.mode_reasons:
            self.mode_reasons = reasons
            self.armed = not reasons
            if self.armed:
                self._log(out, INFO, "mode: ARMED (will stop the radar and power off on power loss)")
            else:
                self._log(out, WARNING if reasons != ["config"] else INFO,
                          f"mode: {self.mode} ({', '.join(reasons)}): decisions are logged, "
                          "no hooks run, no power-off")

    @property
    def mode(self):
        if self.armed:
            return "armed"
        return "passive" if self.passive else "monitor"

    def guard_data(self):
        return {"count": self.guard_count, "passive": self.passive}

    def _guard_timers(self, t, uptime_s, out):
        if self.phase in (COMMITTING, SHUTDOWN):
            return
        cfg = self.cfg
        if self._guard_dirty:
            self._guard_dirty = False
            out.append(Action("save_guard", data=self.guard_data()))
        if not self._count_reset_done and uptime_s >= cfg.early_boot_s:
            self._count_reset_done = True
            if self.guard_count:
                self._log(out, INFO, f"boot passed {cfg.early_boot_s:.0f} s without a shutdown by this "
                          f"service: early-shutdown count reset ({self.guard_count} -> 0)")
                self.guard_count = 0
                out.append(Action("save_guard", data=self.guard_data()))
        if all(s.state == PRESENT for s in self.signals):
            if self.both_present_since is None:
                self.both_present_since = t
        else:
            self.both_present_since = None
        if (self.passive and self.both_present_since is not None
                and t - self.both_present_since >= cfg.passive_rearm_s):
            self.passive = False
            self.guard_count = 0
            self._log(out, WARNING, f"boot-loop guard: both signals healthy and present for "
                      f"{cfg.passive_rearm_s:.0f} s: passive mode cleared")
            out.append(Action("save_guard", data=self.guard_data()))

    # ------------------------------------------------------ normal / loss phase

    def _agreement(self, losses):
        """Return (basis, None) if the agreement rule holds for this sample, else (None, why_not).

        It holds if both signals say loss, or one does and the other is unreadable
        or faulty, and no signal that says loss has just produced a present sample.
        """
        if len(losses) == 2:
            basis = "both signals say loss"
        else:
            lost = losses[0]
            other = self.gpio if lost is self.current else self.current
            if other.state in (UNREADABLE, FAULTY):
                if self.cfg.single_signal_action != "shutdown":
                    return None, f"only {lost.name} says loss ({other.name} {other.state}), " \
                                 "single_signal_action = quiesce"
                basis = f"{lost.name} says loss, {other.name} is {other.state}"
            elif other.state == PRESENT:
                return None, f"signals disagree ({lost.name} loss, {other.name} present)"
            else:
                return None, f"{other.name} not determined yet"
        if any(s.restoring for s in losses):
            return None, "a present sample interrupted the loss"
        return basis, None

    def _evaluate(self, t, out):
        cfg = self.cfg
        current_valid = self.current_a is not None
        losses = [s for s in self.signals if s.state == LOSS]
        if self.phase == NORMAL:
            if not losses:
                return
            self.phase = LOSS_PHASE
            self.loss_since = t
            self.agree_since = None
            self.stop_requested = True
            self._would_logged = False
            names = " and ".join(s.name for s in losses)
            self._log(out, WARNING, f"POWER LOSS indicated by {names}: radar stop requested "
                      f"({self._readings()})")
        elif not losses:
            self.phase = NORMAL
            self.stop_requested = False
            self.commit_ready = False
            self.agree_since = None
            if self.hooks["stop_radar"] != "running":
                self.hooks["stop_radar"] = None
            if all(s.state == PRESENT for s in self.signals):
                why = "power is back"
            else:
                why = "no healthy signal says loss any more"
            self._log(out, INFO, f"back to normal after {t - self.loss_since:.1f} s: {why}; radar "
                      f"stop request cleared ({self._readings()})")
            return

        # Stop the radar once per loss, as soon as the service is armed (normally at once).
        if self.armed and self.hooks["stop_radar"] is None:
            self._start_hook("stop_radar", t, out)

        # The ride-through is timed on the agreement itself: the timer starts when
        # the rule first holds and starts again after any sample for which it does not.
        basis, _ = self._agreement(losses)
        if basis is None:
            self.agree_since = None
        elif self.agree_since is None:
            self.agree_since = t
        floor = cfg.low_voltage_v
        if basis and t - self.agree_since >= cfg.ride_through_s:
            trigger = f"loss agreed for {t - self.agree_since:.1f} s; {basis}"
        elif basis and self.low_voltage:
            trigger = f"capacitor at {self.voltage_v:.2f} V, below {floor:.2f} V; {basis}"
        elif (self.discharge_low_since is not None and current_valid
              and t - self.discharge_low_since >= LOW_VOLTAGE_ALONE_S):
            # Cannot happen with main power present, so GPIO is not needed. Without
            # this, a GPIO line stuck high would turn every power-off into a hard cut.
            trigger = (f"capacitor discharging below floor ({self.voltage_v:.2f} V < {floor:.2f} V for "
                       f"{t - self.discharge_low_since:.1f} s); gpio says {self.gpio.state}")
        else:
            trigger = None
        self.commit_ready = trigger is not None
        if not self.commit_ready:
            return
        if self.armed:
            self._log(out, WARNING, f"COMMIT to shutdown: {trigger} ({self._readings()})")
            self.phase = COMMITTING
            self.commit_t = t
            self.commit_why = trigger
            self._enter_step("stop_radar", t, out)
        elif not self._would_logged:
            self._would_logged = True
            self._log(out, WARNING, f"WOULD COMMIT to shutdown now: {trigger} "
                      f"({self._readings()}). Not armed ({', '.join(self.mode_reasons)}): "
                      "no hooks run, no power-off")

    # -------------------------------------------------------------- committing

    def _start_hook(self, name, t, out):
        self.hooks[name] = "running"
        self.hook_started[name] = t
        out.append(Action("hook", name=name))

    def _enter_step(self, name, t, out):
        cfg = self.cfg
        self.step_name = name
        if name in ("stop_radar", "amp_safe"):
            if self.hooks[name] == "ok":
                return self._next_step(t, out)   # stop_radar already ran when the loss began
            if self.hooks[name] != "running":
                self._start_hook(name, t, out)
            timeout = getattr(cfg, name + "_timeout_s")
            self.step_deadline = self.hook_started[name] + timeout + HOOK_MARGIN_S
        elif name == "sync":
            self.step_deadline = t + cfg.sync_timeout_s
            out.append(Action("sync"))
        elif name == "record":
            early = self.uptime_s <= cfg.early_boot_s
            self.guard_count = self.guard_count + 1 if early else 0
            data = {"count": self.guard_count,
                    "passive": self.guard_count >= cfg.max_early_shutdowns,
                    "last_shutdown": {"uptime_s": round(self.uptime_s, 1), "early": early,
                                      "why": self.commit_why}}
            if early:
                self._log(out, WARNING, f"shutdown within {cfg.early_boot_s:.0f} s of boot: "
                          f"early-shutdown count {self.guard_count} of {cfg.max_early_shutdowns}")
            self.step_deadline = t + RECORD_TIMEOUT_S
            out.append(Action("record", data=data))
        else:  # decide: made in step(), on a sample taken after the steps above finished
            self.step_deadline = t + cfg.restore_confirm_s + DECIDE_EXTRA_S

    def _next_step(self, t, out):
        i = COMMIT_STEPS.index(self.step_name)
        self._enter_step(COMMIT_STEPS[i + 1], t, out)

    def _advance_commit(self, t, out):
        if self.step_name == "decide":
            self._decide(t, out)
        elif t >= self.step_deadline:
            self._log(out, ERROR, f"shutdown step {self.step_name} did not finish in time: continuing")
            if self.step_name in self.hooks:
                self.hooks[self.step_name] = "failed"
            self._next_step(t, out)

    def _decide(self, t, out):
        """Final power check: reboot if every healthy signal is confirmed present."""
        # A restore still being confirmed is given time to finish, so that power
        # returning during the sequence leads to a reboot, not a Pi left off.
        if any(s.restoring for s in self.signals) and t < self.step_deadline:
            return
        healthy = [s for s in self.signals if s.healthy]
        if healthy and all(s.state == PRESENT for s in healthy):
            self.power_kind = "reboot"
            self._log(out, WARNING, f"main power is back ({self._readings()}): REBOOT instead of power-off")
        else:
            self.power_kind = "poweroff"
            self._log(out, WARNING, f"main power not confirmed back ({self._readings()}): POWER OFF")
        self.phase = SHUTDOWN
        self.step_name = None
        self.power_t = t
        self._fire(t, 0, out)

    def _fire(self, t, level, out):
        self.power_level = level
        self.power_fired_t = t
        if level:
            self._log(out, ERROR, f"still running {t - self.power_t:.0f} s after the "
                      f"{self.power_kind} command: last resort")
        out.append(Action("power", name=self.power_kind, data=level))

    def _escalate(self, t, out):
        if self.power_level == 0:
            if t - self.power_t >= self.cfg.last_resort_after_s:
                self._fire(t, 1, out)
        elif t - self.power_fired_t >= REFIRE_S:
            self._fire(t, 1, out)

    # ---------------------------------------------------------------- warnings

    def _update_warnings(self, t, guard_ok, out):
        cfg = self.cfg
        w = {}
        for s in self.signals:
            if s.state == UNREADABLE:
                w[f"{s.name}_unreadable"] = f"{s.name} signal unreadable"
            elif s.state == FAULTY:
                w[f"{s.name}_faulty"] = (f"{s.name} signal reported loss for more than "
                                         f"{cfg.faulty_after_s:.0f} s while the Pi kept running: ignored "
                                         f"until it reads present for {cfg.faulty_clear_s:.0f} s")
        if not any(s.healthy for s in self.signals):
            w["no_power_monitoring"] = ("both power signals unreadable or faulty: a power loss "
                                        "cannot be detected and no action will be taken")
        states = {s.state for s in self.signals}
        if states == {LOSS, PRESENT}:
            if self.disagree_since is None:
                self.disagree_since = t
            if t - self.disagree_since >= cfg.disagree_warn_s:
                w["signals_disagree"] = (f"signals disagree (current {self.current.state}, gpio "
                                         f"{self.gpio.state}): radar stopped, no shutdown")
        else:
            self.disagree_since = None
        if self.phase == LOSS_PHASE and not self.commit_ready and cfg.single_signal_action != "shutdown":
            losses = [s for s in self.signals if s.state == LOSS]
            if len(losses) == 1 and not all(s.healthy for s in self.signals):
                w["single_signal_quiesce"] = (f"only {losses[0].name} says loss and the other signal is "
                                              "not usable: radar stopped, no shutdown")
        if self.passive:
            w["boot_loop_guard"] = (f"boot-loop guard: {cfg.max_early_shutdowns} early shutdowns in a "
                                    "row, shutdown authority suspended (passive)")
        if not guard_ok:
            w["guard_state_unwritable"] = "guard state cannot be written: shutdown authority suspended"
        for code in w.keys() - self.warnings.keys():
            self._log(out, WARNING, f"warning: {w[code]} ({self._readings()})")
        for code in self.warnings.keys() - w.keys():
            self._log(out, INFO, f"warning cleared: {code}")
        self.warnings = w

    # ------------------------------------------------------------------ output

    def _log(self, out, level, message):
        out.append(Action("log", data=(level, message)))

    def _readings(self):
        i = "unreadable" if self.current_a is None else f"{self.current_a:+.2f} A"
        v = "unreadable" if self.voltage_v is None else f"{self.voltage_v:.2f} V"
        g = "unreadable" if self.gpio_value is None else str(self.gpio_value)
        return f"cap {v}, current {i}, gpio {g}"

    @property
    def state(self):
        if self.phase == LOSS_PHASE and self.commit_ready and not self.armed:
            return "would_shutdown"
        return self.phase

    def snapshot(self):
        """State for the status file (plain JSON types)."""
        t = self.t

        def sig(s, value):
            return {"state": s.state,
                    "state_age_s": None if s.state_since is None else round(t - s.state_since, 1),
                    "value": value, "sample": s.raw if s.raw is not None else "error",
                    "read_errors": s.errors}

        if self.phase != NORMAL:
            present = False
        elif any(s.state == PRESENT for s in self.signals) and not any(
                s.state == UNKNOWN for s in self.signals):
            present = True
        else:
            present = None
        return {
            "state": self.state,
            "mode": self.mode,
            "armed": self.armed,
            "mode_reasons": list(self.mode_reasons or ()),
            "power_present": present,
            "radar_stop_requested": self.stop_requested,
            "loss_duration_s": None if self.phase == NORMAL else round(t - self.loss_since, 1),
            "agreement_s": None if self.agree_since is None else round(t - self.agree_since, 1),
            "commit_step": self.step_name,
            "power_action": self.power_kind,
            "signals": {"current": sig(self.current, self.current_a),
                        "gpio": sig(self.gpio, self.gpio_value)},
            "capacitor_v": self.voltage_v,
            "current_a": self.current_a,
            "low_voltage": self.low_voltage,
            "warnings": dict(sorted(self.warnings.items())),
            "guard": {"early_shutdowns": self.guard_count,
                      "max_early_shutdowns": self.cfg.max_early_shutdowns,
                      "passive": self.passive,
                      "in_early_boot_window": self.uptime_s <= self.cfg.early_boot_s},
        }
