"""Shared test helpers: a simulator that drives the pure logic, and fakes for the service."""
import re
import threading
from pathlib import Path

from power import logic as L
from power.config import Config

LOG_DIR = Path.home() / "ups-bringup" / "logs"


def make_config(**overrides):
    cfg = Config()
    for k, v in overrides.items():
        assert hasattr(cfg, k), k
        setattr(cfg, k, v)
    return cfg


class Outage:
    """Signal model of the UPS as measured on the bench (UPS_BRINGUP.md).

    Power is lost at t0 and returns at t1 (None = never). The current reverses
    at once; GPIO 26 follows `gpio_lag` later; both show a restore at once.
    """

    def __init__(self, t0, t1=None, gpio_lag=1.5, load_a=-1.0, v0=5.19, dv_dt=-0.02):
        self.t0, self.t1, self.gpio_lag = t0, t1, gpio_lag
        self.load_a, self.v0, self.dv_dt = load_a, v0, dv_dt

    def lost(self, t):
        return t >= self.t0 and (self.t1 is None or t < self.t1)

    def current(self, t):
        return self.load_a if self.lost(t) else 0.02

    def voltage(self, t):
        if t < self.t0:
            return self.v0
        end = t if self.lost(t) else self.t1
        return self.v0 + self.dv_dt * (end - self.t0)

    def gpio(self, t):
        return 0 if self.lost(t) and t >= self.t0 + self.gpio_lag else 1


class Sim:
    """Drives PowerLogic the way the service does, in simulated time.

    Hooks, sync and the guard write are completed after configurable delays;
    a delay of None means the job never finishes (hang).
    """

    def __init__(self, cfg=None, source=None, dt=0.2, uptime0=1000.0, guard_count=0, passive=False,
                 **cfg_overrides):
        self.cfg = cfg or make_config(armed=True, **cfg_overrides)
        self.logic = L.PowerLogic(self.cfg, guard_count=guard_count, passive=passive)
        self.dt = dt
        self.t = 0.0
        self.n = 0
        self.uptime0 = uptime0
        self.source = source
        self.current = lambda t: 0.02
        self.voltage = lambda t: 5.19
        self.gpio = lambda t: 1
        if source is not None:
            self.current, self.voltage, self.gpio = source.current, source.voltage, source.gpio
        self.disable_file = lambda t: False
        self.guard_ok = lambda t: True
        self.delay = {"hook:stop_radar": 0.0, "hook:amp_safe": 0.0, "sync": 0.05, "record": 0.02,
                      "power": None}
        self.ok = {"hook:stop_radar": True, "hook:amp_safe": True, "sync": True, "record": True,
                   "power": True}
        self.survive = False   # True: keep simulating after a successful power command
        self.alive = True
        self.pending = []      # (due, what, ok)
        self.actions = []      # (t, Action), logs excluded
        self.logs = []         # (t, level, message)
        self.states = []       # (t, state) on change

    # -- running

    def _handle(self, t, actions):
        for a in actions:
            if a.kind == "log":
                self.logs.append((t, *a.data))
                continue
            self.actions.append((t, a))
            what = {"hook": "hook:" + a.name, "sync": "sync", "record": "record",
                    "power": "power"}.get(a.kind)
            if what is None:
                continue
            if what == "power":
                if not self.ok["power"]:
                    self.pending.append((t, "power", False))
                elif not self.survive:
                    self.alive = False     # the Pi goes down
            elif self.delay[what] is not None:
                self.pending.append((t + self.delay[what], what, self.ok[what]))

    def _deliver(self, t):
        for _ in range(20):
            due = [p for p in self.pending if p[0] <= t + 1e-9]
            if not due:
                return
            for p in due:
                self.pending.remove(p)
                self._handle(t, self.logic.done(t, p[1], p[2]))

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end - 1e-9 and self.alive:
            t = self.t
            self._deliver(t)
            v = self.voltage(t)
            i = self.current(t)
            self._handle(t, self.logic.step(t, i, None if i is None else v, self.gpio(t),
                                            self.uptime0 + t, disable_file=self.disable_file(t),
                                            guard_ok=self.guard_ok(t)))
            self._deliver(t)
            if not self.states or self.states[-1][1] != self.logic.state:
                self.states.append((t, self.logic.state))
            self.n += 1
            self.t = round(self.n * self.dt, 6)
        return self

    # -- inspection

    def kinds(self):
        return [(a.kind, a.name) if a.name else (a.kind,) for _, a in self.actions]

    def find(self, kind, name=None):
        return [(t, a) for t, a in self.actions if a.kind == kind and (name is None or a.name == name)]

    def power(self):
        """(time, kind, level) of every power command."""
        return [(t, a.name, a.data) for t, a in self.find("power")]

    def state_names(self):
        return [s for _, s in self.states]

    def logged(self, pattern):
        return [(t, m) for t, _, m in self.logs if re.search(pattern, m)]


# ----------------------------------------------------------- recorded bench logs

_LINE = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3})\s+cap=([\d.]+) V\s+(?:shunt=\S+ mV\s+)?"
                   r"I=([+-][\d.]+) A\s+GPIO26=([01])")


def load_recording(name):
    """Parse a ups_check.py / ups_load_test.py log into [(t, current_a, voltage_v, gpio)].

    t is seconds from the first sample. The loggers took the seconds and the
    milliseconds from two separate clock reads, so a sample taken across a
    second boundary can be stamped one second early; those are corrected.
    """
    rows = []
    prev = None
    for line in (LOG_DIR / name).read_text().splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        h, mi, s, ms, v, i, g = m.groups()
        t = int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000
        if prev is not None and t < prev:
            t += 1.0
        assert prev is None or 0 < t - prev < 0.5, (name, line)
        prev = t
        rows.append((t, float(i), float(v), int(g)))
    t0 = rows[0][0]
    return [(round(t - t0, 3), i, v, g) for t, i, v, g in rows]


class Replay(Sim):
    """Feeds a recording through the logic at the recorded sample times."""

    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.rows = rows

    def run(self):
        for t, i, v, g in self.rows:
            if not self.alive:
                break
            self._deliver(t)
            self._handle(t, self.logic.step(t, i, v, g, self.uptime0 + t))
            self._deliver(t)
            if not self.states or self.states[-1][1] != self.logic.state:
                self.states.append((t, self.logic.state))
            self.t = t
        return self


# ------------------------------------------------------------- service fakes

class FakeClock:
    """Simulated time for the service loop: sleep() just advances it."""

    def __init__(self, uptime0=1000.0):
        self.t = 0.0
        self.uptime0 = uptime0
        self.wall0 = 1795000000.0

    def monotonic(self):
        return self.t

    def wall(self):
        return self.wall0 + self.t

    def uptime(self):
        return self.uptime0 + self.t

    def sleep(self, s):
        # Let the service's worker threads (sync, guard write) finish in real time
        # first, so that simulated time cannot run past their timeouts.
        for th in threading.enumerate():
            if th.name in ("sync", "record"):
                th.join()
        self.t += max(s, 0.0)


class FakeJob:
    what = "power"
    proc = None

    def __init__(self, result):
        self.result = result

    def poll(self, now):
        return self.result


class FakeActuator:
    """Records the power commands. ok=None means the command hangs."""

    def __init__(self, ok=True):
        self.fired = []
        self.ok = ok

    def start(self, kind, level, now):
        self.fired.append((kind, level))
        return FakeJob(self.ok)


class ListLog:
    def __init__(self):
        self.lines = []

    def __call__(self, level, message):
        self.lines.append((level, message))

    def text(self):
        return "\n".join(m for _, m in self.lines)
