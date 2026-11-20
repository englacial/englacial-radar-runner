"""The GPS motion trigger: says "moving", "stationary" or "unknown" for logic.auto().

It reads the fixes that the telemetry service publishes (telemetry.json,
`sources.gps`: about one per second from gpsd) and opens no connection of its
own. Only fixes that count are used: the telemetry source healthy, the fix
new (its receiver time has moved on), at most `stale_s` old, with fix mode at
least `min_mode`, a horizontal error estimate (gpsd's `eph`, from the
receiver's hAcc) at most `max_eph_m`, and a speed.

    moving       every counting fix for `start_s` at `start_speed_mps` or faster
    stationary   every counting fix for `stop_s` at `stop_speed_mps` or slower
    unknown      no counting fix for `stale_s` (fix lost, poor fix, stale data, gpsd or
                 telemetry down), and at start-up

Durations are measured in receiver time between fixes, so a stall of this
loop or of the telemetry service cannot stretch or shorten them. A fix that
does not count, a speed between the two thresholds, or two counting fixes more
than `max_gap_s` apart start both counts again. "moving" and "stationary"
hold until the other one is reached or the fixes stop counting for `stale_s`;
so a truck creeping between the two speeds keeps whatever was said last, and
after "unknown" the full time is needed again. What logic.py does with the
three answers (an automatic run is stopped only on "stationary", "unknown"
never changes the radar's state, the manual rules) is in logic.py.
"""
import calendar
import time
from dataclasses import dataclass, fields

MOVING, STATIONARY, UNKNOWN = "moving", "stationary", "unknown"

# The smallest values that the settings may have: below them parked GPS noise or a
# flapping trigger could start and stop the radar (each start enables the amplifier
# for about half a second). A refused value is replaced by the default.
MINIMUMS = {"start_speed_mps": 1.0, "start_s": 5.0, "stop_s": 30.0, "max_gap_s": 1.0, "stale_s": 2.0}


@dataclass(frozen=True)
class MotionConfig:
    start_speed_mps: float = 2.0
    start_s: float = 20.0
    stop_speed_mps: float = 0.5
    stop_s: float = 120.0
    min_mode: int = 3
    max_eph_m: float = 5.0
    max_gap_s: float = 3.0
    stale_s: float = 5.0

    @classmethod
    def from_config(cls, motion_cfg, minimums=None):
        """Return (config, warnings). A value that is refused is replaced by its default."""
        low = dict(MINIMUMS, **(minimums or {}))
        default = cls()
        values = {f.name: motion_cfg.get(f.name, getattr(default, f.name)) for f in fields(cls)}
        warnings = []

        def refuse(name, why):
            warnings.append(f"manager.motion.{name} {why}: the default is used")
            values[name] = getattr(default, name)

        for name, floor in low.items():
            if name in values and values[name] < floor:
                refuse(name, f"must be at least {floor:g}")
        if values["min_mode"] not in (2, 3):
            refuse("min_mode", "must be 2 or 3")
        if values["max_eph_m"] <= 0:
            refuse("max_eph_m", "must be above 0")
        if not 0 <= values["stop_speed_mps"] < values["start_speed_mps"]:
            refuse("stop_speed_mps", "must be at least 0 and below start_speed_mps")
        return cls(**values), warnings


def fix_time(text):
    """Receiver time of a fix ("2026-11-20T06:47:55.900Z") as seconds, or None."""
    if not isinstance(text, str) or not text.endswith("Z"):
        return None
    try:
        whole, _, frac = text[:-1].partition(".")
        t = calendar.timegm(time.strptime(whole, "%Y-%m-%dT%H:%M:%S"))
        return t + (float("0." + frac) if frac else 0.0)
    except (ValueError, OverflowError):
        return None


class Motion:
    """Feed it the telemetry status at each look (`update`); it answers what to report."""

    def __init__(self, cfg=None, label="gps"):
        self.cfg = cfg or MotionConfig()
        self.label = label
        self.state = UNKNOWN
        self.reason = "no fix seen yet"
        self.fast_since = None      # receiver time of the first fix of the current stretch at speed
        self.still_since = None     # ... at a standstill
        self.last_fix = None        # receiver time of the last counting fix
        self.last_counted_at = None  # Pi time (monotonic) at which that fix was taken
        self.last_seen = None       # receiver time of the last fix looked at, counting or not
        self.speed = None
        self.problem = ""           # why the newest fix does not count, or ""
        self.changes = 0

    def _restart(self):
        self.fast_since = self.still_since = None

    def update(self, now, read):
        """`read`: a common.status read of telemetry.json (or None). Returns the state to report."""
        self.problem = self._look(now, read)
        if self.state == UNKNOWN:
            self.reason = self.problem or "counting: " + self._counting()
        elif self.last_counted_at is None or now - self.last_counted_at > self.cfg.stale_s:
            self._set(UNKNOWN, f"nothing that counts for {self.cfg.stale_s:g} s: {self.problem or 'no new fix'}")
        return self.state

    def _look(self, now, read):
        """Count the newest fix, if there is one; returns "" or why it does not count."""
        cfg = self.cfg
        if read is None or not getattr(read, "ok", False):
            self._restart()
            return "telemetry status: " + (getattr(read, "error", "") or "not read")
        gps = ((read.data.get("sources") or {}).get("gps")) or {}
        values = gps.get("values") or {}
        t = fix_time(values.get("gps_time"))
        if gps.get("health") != "ok" or t is None:
            self._restart()
            return f"gps: {gps.get('health', 'not in the status')}" + (f" ({gps['reason']})" if gps.get("reason") else "")
        if t == self.last_seen:                         # the same fix as at the last look
            return self.problem
        self.last_seen = t
        age = gps.get("new_fix_age_s")
        age = (age if isinstance(age, (int, float)) else float("inf")) + (read.age_s or 0.0)
        mode, eph, speed = values.get("mode"), values.get("eph_m"), values.get("speed_mps")
        problem = ""
        if age > cfg.stale_s:
            problem = f"fix {age:.0f} s old"
        elif not isinstance(mode, int) or mode < cfg.min_mode:
            problem = f"fix mode {mode}"
        elif not isinstance(eph, (int, float)) or eph > cfg.max_eph_m:
            problem = f"horizontal error {eph} m"
        elif not isinstance(speed, (int, float)):
            problem = "no speed"
        if problem:
            self._restart()
            return problem
        if self.last_fix is not None and not 0 < t - self.last_fix <= cfg.max_gap_s:
            self._restart()                             # a gap in the fixes (or time went back)
        self.last_fix, self.last_counted_at, self.speed = t, now, float(speed)
        if speed >= cfg.start_speed_mps:
            self.still_since = None
            self.fast_since = t if self.fast_since is None else self.fast_since
            if t - self.fast_since >= cfg.start_s and self.state != MOVING:
                self._set(MOVING, f"{speed:.1f} m/s, at least {cfg.start_speed_mps:g} m/s for {cfg.start_s:g} s")
        elif speed <= cfg.stop_speed_mps:
            self.fast_since = None
            self.still_since = t if self.still_since is None else self.still_since
            if t - self.still_since >= cfg.stop_s and self.state != STATIONARY:
                self._set(STATIONARY, f"at most {cfg.stop_speed_mps:g} m/s for {cfg.stop_s:g} s")
        else:
            self._restart()                             # between the thresholds: neither count runs
        return ""

    def _counting(self):
        t = self.last_fix
        if self.fast_since is not None:
            return f"{self.speed:.1f} m/s for {t - self.fast_since:.0f} of {self.cfg.start_s:g} s"
        if self.still_since is not None:
            return f"{self.speed:.1f} m/s for {t - self.still_since:.0f} of {self.cfg.stop_s:g} s"
        return "" if self.speed is None else f"{self.speed:.1f} m/s, between the two thresholds"

    def _set(self, state, reason):
        self.state, self.reason = state, reason
        self.changes += 1

    def snapshot(self):
        return {"state": self.state, "reason": self.reason, "problem": self.problem,
                "speed_mps": None if self.speed is None else round(self.speed, 2),
                "counting": "" if self.problem else self._counting(), "changes": self.changes,
                "settings": {f.name: getattr(self.cfg, f.name) for f in fields(self.cfg)}}
