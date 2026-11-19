"""Shared pieces of the safety watcher's tests: simulated telemetry, a stand-in reset tool, stand-in radars.

Nothing here touches the SDR, the OLA, the installed services or any process that the tests did not start.
"""
import copy
import json
import shutil
import signal
import subprocess

from radar_runner.common import config as C
from radar_runner.watcher import logic as L

GOOD = {"temp_c": 27.2, "case_temp_c": 19.3, "current_a": 0.156, "voltage_v": 27.98}     # the bench, in shutdown
ENABLED_A = 2.58


def settings(**changes):
    cfg = copy.deepcopy(C.DEFAULTS["watcher"])
    for key, value in changes.items():
        if key in cfg["reset"]:
            cfg["reset"][key] = value
        else:
            assert key in cfg, key
            cfg[key] = value
    return cfg


class Sim:
    """The logic in simulated time: four rounds a second, a new amplifier reading every 0.5 s."""

    def __init__(self, **changes):
        self.cfg = settings(**changes)
        self.t = 100.0
        self.logic = L.WatcherLogic(self.cfg, self.t)
        self.amp = dict(GOOD)
        self.telemetry = True           # readings arrive
        self.reason = "amplifier readings not fresh: the OLA is not set up for the amplifier readings (configuring)"
        self.radar = ()
        self.override = False
        self.power_lost = False
        self.pi = {"soc_temp_c": 46.0, "throttled": ()}
        self.gap = 0.25
        self.events = []
        self.signals = []               # (time, signal, pids)
        self.tool_starts = []           # times at which the logic asked for the reset tool
        self.statuses = []              # (time, ok, tripped, reason) at every round
        self._sample = None
        self._pi_sample = None
        self._rounds = 0

    def round(self, dt=0.25):
        self.t += dt
        self._rounds += 1
        if self._rounds % 2 == 1 or dt > 0.25:
            self._sample = L.Sample(self.t - 0.25, **self.amp)
        if self._rounds % 8 == 1:
            self._pi_sample = L.PiSample(self.t - 0.5, self.pi["soc_temp_c"], tuple(self.pi["throttled"]))
        inp = L.Inputs(sample=self._sample if self.telemetry else None,
                       telemetry_reason="" if self.telemetry else self.reason,
                       radar_pids=tuple(self.radar), override=self.override, power_lost=self.power_lost,
                       pi=self._pi_sample if self.telemetry else None,
                       telemetry_gap_s=self.gap if self.telemetry else None)
        self.carry_out(self.logic.step(self.t, inp))
        self.statuses.append((self.t, self.logic.ok, self.logic.tripped, self.logic.reason))

    def carry_out(self, actions):
        for action in actions:
            if action.kind == "event":
                self.events.append(dict(action.data, t=self.t))
            elif action.kind == "signal":
                self.signals.append((self.t, action.data[0], action.data[1]))
            elif action.kind == "run_tool":
                self.tool_starts.append(self.t)

    def run(self, seconds):
        for _ in range(round(seconds / 0.25)):
            self.round()
        return self

    def one_reading(self, **values):
        """Exactly one reading with these values, then the normal ones again."""
        while self._rounds % 2 == 1:        # up to the round before a new reading
            self.round()
        before = dict(self.amp)
        self.amp.update(values)
        self.round()
        self.amp = before
        self.round()

    def tool_done(self, code, text="stand-in", took=3.5):
        """The reset tool ends `took` seconds after it was started, with this exit code."""
        assert self.logic.tool_running
        self.run(took)
        self.carry_out(self.logic.tool_done(self.t, code, text, took))

    def named(self, name, **match):
        return [e for e in self.events if e["event"] == name and all(e.get(k) == v for k, v in match.items())]

    def clear(self):
        self.events, self.signals, self.tool_starts, self.statuses = [], [], [], []

    @property
    def ok(self):
        return self.logic.ok

    @property
    def tripped(self):
        return self.logic.tripped

    def state(self, condition):
        return self.logic.snapshot(self.t, self.override)["conditions"][condition]["state"]


def telemetry_status(up, amp=None, *, fresh=True, reason="", pi_temp=46.0, throttled=(), gap=0.25, age=0.25):
    """What the telemetry service publishes, cut down to what the watcher reads."""
    values = dict(GOOD, **(amp or {}))
    return {"state": "ok",
            "amp": {"fresh": fresh, "reason": reason, "fresh_limit_s": 2.0, "age_s": age,
                    "sample_uptime_s": round(up - age, 3), **values},
            "sources": {"pi": {"health": "ok", "sample_uptime_s": round(up - 1.0, 3),
                               "values": {"soc_temp_c": pi_temp}, "throttled_now": list(throttled)}},
            "loop": {"last_gap_s": 0.25, "recent_longest_gap_s": gap}}


# ---- stand-ins that run as real processes

TOOL = '''#!/usr/bin/python3 -B
"""Stand-in for tools/sdr_gpio_safe/amp_safe: records the call and ends as the test asks. Touches nothing."""
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
plan = {"codes": [0], "sleep": 0.0}
try:
    with open(os.path.join(here, "plan.json")) as f:
        plan.update(json.load(f))
except OSError:
    pass
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "time": time.time(), "pid": os.getpid(),
                        "radar_alive": os.path.exists(os.path.join(here, "radar-alive"))}) + "\\n")
with open(os.path.join(here, "calls.jsonl")) as f:
    n = sum(1 for _ in f)
code = plan["codes"][min(n, len(plan["codes"])) - 1]
time.sleep(plan["sleep"])
if code == 0:
    with open(os.path.join(here, "succeeded"), "w") as f:
        f.write("ok")
names = {0: "OK", 2: "NOT_FOUND", 3: "ASSUME_ENABLED", 4: "TIMEOUT", 5: "FAILED", 6: "BUSY", 7: "AMBIGUOUS",
         8: "STATE_FILE", 64: "USAGE"}
print(f"amp_safe: {names.get(code, '?')} (exit {code}, 0.00 s): stand-in", flush=True)
sys.exit(code)
'''


class FakeTool:
    """A stand-in amp_safe in its own directory. `plan(codes=[6, 0])` sets the exit code of each call in turn."""

    def __init__(self, directory):
        self.dir = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "amp_safe"
        self.path.write_text(TOOL)
        self.path.chmod(0o755)

    def plan(self, codes=(0,), sleep=0.0):
        (self.dir / "plan.json").write_text(json.dumps({"codes": list(codes), "sleep": sleep}))

    @property
    def calls(self):
        try:
            return [json.loads(line) for line in (self.dir / "calls.jsonl").read_text().splitlines()]
        except FileNotFoundError:
            return []

    @property
    def succeeded(self):
        return (self.dir / "succeeded").exists()


class FakeRadar:
    """Stand-in radar processes: copies of `sleep` under the name `radar`, as test_pa_guard.py does."""

    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.exe = directory / "radar"
        shutil.copy(shutil.which("sleep"), self.exe)
        self.procs = []

    def start(self, ignore_sigint=False):
        # An ignored signal stays ignored across exec: a radar that does not react to SIGINT.
        ignore = (lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)) if ignore_sigint else None
        proc = subprocess.Popen([str(self.exe), "300"], preexec_fn=ignore)
        self.procs.append(proc)
        return proc

    def stop_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)
