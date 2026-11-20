"""The turn-on check: a short low-power run before every radar start (README, "Turn-on check").

    settings, warnings = Settings.from_config(config)
    check = TurnOnCheck(config, settings)      # None when settings.enabled is false
    runner = Runner(config, post, turn_on_check=check); check.runner = runner

The runner calls `check(run)` after the build and before the radar is launched,
on its own thread, with the SDR's record already set to unknown. It returns
(passed, text). The sequence:

  1. the low-power config: the collection's config as written and checked, with
     RF0.tx_gain capped at `max_tx_gain`, a fixed pulse count, and its own
     output directory and data file name. Nothing else differs (checked), so the
     waveform and the delays are those of the collection. It must pass the same
     limits;
  2. an amplifier reading before the run: voltage, idle current, temperatures;
  3. the low-power radar (Runner.check_radar), with the amplifier current read
     from the telemetry status while its pulses go out;
  4. the analysis (tools/turn_on_check/measure.py, with ORCA's Python and numpy,
     in a process of its own, with a time limit): the profile, the peaks,
     clipping;
  5. the judgement (`judge`, no I/O), written to turn_on_check/result.json and
     collection.json, and posted as the event "turn_on_check".

What follows from the outcome:
  * passed, or failed while not blocking ("record only"): (True, text); the
    radar is launched at the requested power. A collection after a failed
    check carries the flag `turn_on_check_failed`;
  * failed while blocking, on the signal, clipping or data: (False, text): the
    runner records the fatal failure "turn_on_check", the manager's fault;
  * failed while blocking on the amplifier readings only, or the analysis
    failed: a failed start (non-fatal "turn_on_check"), retried;
  * the low-power radar did not run properly (did not start, timed out, exited
    with an error, was killed, the SDR on USB 2): that failure is recorded as the
    run's own (run.fail), exactly as it would be for the radar at power: a
    failed start, retried; the rate warning stays fatal;
  * the amplifier pin not confirmed safe after the low-power radar: as after
    any radar (amp_safe False: the fault `amp_unsafe`);
  * a stop at any point: (True, ...) after the step in progress; the runner
    then does not launch the radar (it checks the stop under its lock);
  * the check's directory cannot be written: a failed start ("directory").

Blocking needs `expected.verified`: until a user has verified a first start and
set the expected peak, the check only records (a warning says so if `blocking`
is set without it).

The data of the low-power run stays in <collection>/turn_on_check/ (config.yaml,
chirp.bin, low_power.bin.0, the radar's logs, analysis input, output and log,
result.json): a subdirectory, and a data file name that is not the collection's,
so it is never taken for science data.
"""
import copy
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass

from radar_runner.common.config import DEFAULTS
from radar_runner.common.status import boottime_s, read_status

from radar_runner.watcher.service import checked as watcher_settings

from . import collection as coll
from . import gates
from . import limits

log = logging.getLogger("manager.turnon")

SUBDIR = "turn_on_check"
DATA_FILE = "low_power.bin"
INPUT_FILE, OUTPUT_FILE, ANALYSIS_LOG, RESULT_FILE = ("analysis_input.json", "analysis.json", "analysis.log",
                                                      "result.json")
FLAG, WARNING_FLAG = "turn_on_check_failed", "turn_on_check_warning"
# The only settings in which the low-power config may differ from the collection's.
CHANGED = {("RF0", "tx_gain"), ("CHIRP", "num_pulses"), ("FILES", "output_dir"), ("FILES", "save_loc"),
           ("FILES", "max_chirps_per_file")}
# Amplifier readings during the pulses: after the radar's start line and CHIRP.time_offset, from
# WINDOW_START_S later (the start line is seen up to 0.1 s late, and a row reaches the Pi after the
# ADC measured it) to WINDOW_END_S before the exit.
WINDOW_START_S, WINDOW_END_S = 0.25, 0.15
# The low-power radar must have exited this long after CHIRP.time_offset and twice the time of its
# pulses, counted from its start line (lost pulses are sent again: a third lost still fits).
RUN_MARGIN_S = 5.0
TEMP_FIELDS = ("amp.temp_c", "amp.case_temp_c")
FAULT_CRITERIA = ("signal", "clipping", "data")     # a blocking failure of these is the fault turn_on_check


def _number(value):
    """A finite number from a config value (ORCA's files may hold "56e6" as text), or None."""
    try:
        out = float(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None
    return out if out is not None and math.isfinite(out) else None


# ---------------------------------------------------------------------- settings

@dataclass
class Settings:
    """manager.turn_on_check, checked (config.py has the defaults and what each one means)."""
    enabled: bool
    blocking: bool                  # as set; `blocks` says whether it has effect
    max_tx_gain: float
    pulses: int
    analysis_command: list
    analysis_timeout_s: float
    voltage_min_v: float            # the safety watcher's limits: never stricter than it
    voltage_max_v: float
    temp_trip_c: float
    idle_current_min_a: float
    idle_current_max_a: float
    current_rise_min_a: float
    current_max_a: float
    min_current_readings: int
    clip_fs: float
    min_separation_samples: float
    tolerance_samples: float
    verified: bool = False          # expected.verified, and the expected values usable
    leakage_sample: float = 0.0
    delay_samples: float = 0.0
    level_tx_gain: float = 0.0
    min_level_db: float = 0.0

    @property
    def blocks(self):
        """A failed check keeps the radar off."""
        return self.blocking and self.verified

    @classmethod
    def from_config(cls, config):
        """(Settings, warnings) from the whole config. A setting that cannot work is refused: the default is used."""
        c = config["manager"]["turn_on_check"]
        e = c["expected"]
        base = DEFAULTS["manager"]["turn_on_check"]
        warnings = []
        out = {}

        def take(name, ok, why, source=c):
            value = source[name]
            if ok(value):
                out[name] = value
            else:
                where = "expected." if source is e else ""
                out[name] = (base["expected"] if source is e else base)[name]
                warnings.append(f"manager.turn_on_check.{where}{name} {why}: the default is used")

        take("max_tx_gain", lambda v: 0 <= v <= 90, "must be between 0 and 90")
        take("pulses", lambda v: 1 <= v <= 200000, "must be between 1 and 200000")
        take("analysis_command", lambda v: bool(v) and all(isinstance(a, str) and a for a in v),
             "must be a command (a list of words)")
        take("analysis_timeout_s", lambda v: 1 <= v <= 600, "must be between 1 and 600 s")
        take("clip_fs", lambda v: 0 < v <= 2, "must be above 0 and at most 2")
        take("min_separation_samples", lambda v: 0 <= v <= 100, "must be between 0 and 100")
        take("min_current_readings", lambda v: 1 <= v <= 50, "must be between 1 and 50")
        take("current_rise_min_a", lambda v: 0 <= v <= 5, "must be between 0 and 5 A")
        for name in ("enabled", "blocking"):
            out[name] = c[name]
        w, _ = watcher_settings(config["watcher"])         # as the watcher itself uses them
        out.update(voltage_min_v=w["voltage_min_v"], voltage_max_v=w["voltage_max_v"], temp_trip_c=w["temp_trip_c"])
        for low, high in (("idle_current_min_a", "idle_current_max_a"),):
            if c[low] < c[high]:
                out[low], out[high] = c[low], c[high]
            else:
                out[low], out[high] = base[low], base[high]
                warnings.append(f"manager.turn_on_check.{low} must be below {high}: the defaults are used")
        take("current_max_a", lambda v: v > out["idle_current_max_a"], "must be above idle_current_max_a")
        take("tolerance_samples", lambda v: 0 < v <= 50, "must be above 0 and at most 50", e)
        cap = config["manager"]["limits"]["max_tx_gain"]
        if out["enabled"] and out["max_tx_gain"] >= cap:
            warnings.append(f"manager.turn_on_check.max_tx_gain ({out['max_tx_gain']:g}) is not below "
                            f"manager.limits.max_tx_gain ({cap:g}): the low-power run may be at full power")
        if e["verified"]:
            two_peak = e["delay_samples"] >= out["min_separation_samples"]
            problems = [why for ok, why in ((e["leakage_sample"] >= 1, "leakage_sample at least 1"),
                                            (e["delay_samples"] >= 0, "delay_samples not negative"),
                                            (e["level_tx_gain"] > 0, "level_tx_gain above 0"),
                                            (e["min_level_db"] < 0, "min_level_db below 0"),
                                            # else the leakage peak lies in the direct peak's window
                                            (not two_peak or out["tolerance_samples"] < e["delay_samples"],
                                             "tolerance_samples must be below delay_samples")) if not ok]
            if problems:
                warnings.append("manager.turn_on_check.expected is marked verified but needs "
                                + ", ".join(problems) + ": the check only records")
            else:
                out.update(verified=True, leakage_sample=e["leakage_sample"], delay_samples=e["delay_samples"],
                           level_tx_gain=e["level_tx_gain"], min_level_db=e["min_level_db"])
        if out["enabled"] and out["blocking"] and not out.get("verified"):
            warnings.append("manager.turn_on_check.blocking needs the expected values (expected.verified): "
                            "the check only records until they are set")
        return cls(**out), warnings

    def expected(self):
        """The input of the peak search, or None before the expected values are set."""
        if not self.verified:
            return None
        return {"leakage_sample": self.leakage_sample, "delay_samples": self.delay_samples,
                "tolerance_samples": self.tolerance_samples, "min_separation_samples": self.min_separation_samples}

    def public(self):
        return {"enabled": self.enabled, "blocking": self.blocking, "verified": self.verified,
                "blocks": self.blocks, "max_tx_gain": self.max_tx_gain, "pulses": self.pulses}


# ------------------------------------------------------------- low-power config

def flatten(config):
    out = {}
    for section, values in config.items():
        if isinstance(values, dict):
            out.update(((section, key), value) for key, value in values.items())
        else:
            out[(section, "")] = values
    return out


def low_power_config(derived, max_tx_gain, pulses, output_dir):
    """The low-power config from a collection's derived config. Returns (config, facts); raises ValueError.

    facts: tx_gain, requested_tx_gain, pulses (a whole number of records), records,
    time_offset_s, pri_s.
    """
    out = copy.deepcopy(derived)
    try:
        requested = _number(out["RF0"]["tx_gain"])
        presums = _number(out["CHIRP"].get("num_presums", 1))
        pri = _number(out["CHIRP"]["pulse_rep_int"])
        offset = _number(out["CHIRP"].get("time_offset", 0))
    except (KeyError, TypeError, AttributeError):
        raise ValueError("the collection config lacks RF0.tx_gain or CHIRP.pulse_rep_int") from None
    if requested is None or presums is None or presums < 1 or pri is None or pri <= 0 or offset is None:
        raise ValueError("RF0.tx_gain, CHIRP.num_presums, CHIRP.pulse_rep_int or CHIRP.time_offset is not usable")
    presums = int(presums)
    records = max(1, -(-int(pulses) // presums))
    gain = min(requested, float(max_tx_gain))
    out["RF0"]["tx_gain"] = gain
    out["CHIRP"]["num_pulses"] = records * presums
    out["FILES"]["output_dir"] = output_dir
    out["FILES"]["save_loc"] = DATA_FILE
    out["FILES"]["max_chirps_per_file"] = records * presums
    return out, {"tx_gain": gain, "requested_tx_gain": requested, "pulses": records * presums, "records": records,
                 "time_offset_s": offset, "pri_s": pri}


def data_files(directory):
    """low_power.bin.0, .1, ... in order (the radar opens an empty next file after its last record)."""
    try:
        names = [n for n in os.listdir(directory) if n.startswith(DATA_FILE + ".") and n.split(".")[-1].isdigit()]
    except OSError:
        return []
    return [os.path.join(directory, n) for n in sorted(names, key=lambda n: int(n.split(".")[-1]))]


def differences(a, b):
    """The (section, key) pairs in which two configs differ."""
    fa, fb, missing = flatten(a), flatten(b), object()
    return {k for k in set(fa) | set(fb) if fa.get(k, missing) != fb.get(k, missing)}


# ---------------------------------------------------------------- amplifier readings

class AmpReadings:
    """The latest amplifier reading in the telemetry service's status file."""

    def __init__(self, config, read=read_status):
        m = config["manager"]
        gate_cfg, amp = m["gates"]["telemetry"], m["amp_safe"]
        self.gate = gates.StatusFileGate("telemetry", gates.status_file(gate_cfg, "telemetry",
                                                                          config["paths"]["run_dir"]),
                                         gate_cfg, "amplifier telemetry", read)
        self._read = read
        self.fields = {"uptime": amp["sample_uptime_field"], "current_a": amp["current_field"],
                       "voltage_v": amp["voltage_field"], "temp_c": TEMP_FIELDS[0], "case_temp_c": TEMP_FIELDS[1]}

    def latest(self):
        """(reading, problem): reading is {uptime, current_a, voltage_v, temp_c, case_temp_c} or None."""
        problem = self.gate.problem()
        if problem:
            return None, problem
        r = self._read(self.gate.path, self.gate.cfg["max_age_s"])
        if not r.ok:
            return None, f"status {r.state}"
        out = {}
        for name, dotted in self.fields.items():
            try:
                value = gates._lookup(r.data, dotted)
            except KeyError:
                value = None
            out[name] = value if type(value) in (int, float) and math.isfinite(value) else None
        if out["uptime"] is None or out["current_a"] is None or out["voltage_v"] is None:
            return None, "the amplifier reading is incomplete"
        return out, ""


class _Sampler(threading.Thread):
    """Collects the distinct amplifier readings while the low-power radar runs, and when it printed its start line."""

    def __init__(self, readings, run, uptime, interval_s=0.1):
        super().__init__(daemon=True, name="turn-on-check-telemetry")
        self.readings, self.run_, self.uptime, self.interval_s = readings, run, uptime, interval_s
        self.items = {}
        self.start_uptime = None
        self.halt = threading.Event()

    def run(self):
        while True:
            check = self.run_.check_run
            if self.start_uptime is None and check is not None and check.started.is_set():
                self.start_uptime = self.uptime()
            try:
                reading, _ = self.readings.latest()
            except Exception:
                reading = None
            if reading is not None:
                self.items[reading["uptime"]] = reading
            if self.halt.wait(self.interval_s):
                return


# ------------------------------------------------------------------------ judgement

def _scaled(level, at_gain, to_gain):
    """A level measured at one tx_gain, as at another: the drive is linear in dB with tx_gain."""
    return level + (to_gain - at_gain)


def judge_before(s, before, override):
    """The criterion `amplifier_before`, from (reading, problem) taken just before the low-power run."""
    reading, problem = before
    if reading is None:
        return {"name": "amplifier_before", "ok": None if override else False,
                "text": f"no amplifier reading before the run ({problem})"
                        + ("; not judged: override set" if override else "")}
    idle, wrong = reading["current_a"], []
    if not s.voltage_min_v <= reading["voltage_v"] <= s.voltage_max_v:
        wrong.append(f"28 V rail at {reading['voltage_v']:.1f} V (allowed {s.voltage_min_v:g}-{s.voltage_max_v:g})")
    if not s.idle_current_min_a <= idle <= s.idle_current_max_a:
        wrong.append(f"idle current {idle:.3f} A (allowed {s.idle_current_min_a:g}-{s.idle_current_max_a:g})")
    for key, what in (("temp_c", "amplifier"), ("case_temp_c", "case")):
        value = reading.get(key)
        if value is None:
            if key == "temp_c":
                wrong.append("no amplifier temperature")
        elif value >= s.temp_trip_c:
            wrong.append(f"{what} temperature {value:.1f} C (the safety watcher stops at {s.temp_trip_c:g} C)")
    return {"name": "amplifier_before", "ok": not wrong, "text": "; ".join(wrong) or
            f"{reading['voltage_v']:.1f} V, {idle:.3f} A idle, {reading.get('temp_c')} C"}


def judge(s, facts, before, override, during, measurement, analysis_error=""):
    """The criteria and the outcome. No I/O.

    s            Settings
    facts        low_power_config's facts
    before       (reading, problem) before the run
    override     the user override "run without amplifier telemetry" is set
    during       [reading] with an uptime inside the pulses
    measurement  measure.py's output (dict), or None with `analysis_error`
    Each criterion: {name, ok: True / False / None (not judged), text}.
    """
    criteria, warnings = [], []

    def add(name, ok, text):
        criteria.append({"name": name, "ok": ok, "text": text})

    gain, requested = facts["tx_gain"], facts["requested_tx_gain"]
    interval = facts.get("telemetry_interval_s", 0.5)
    pulses_s = facts["pulses"] * facts["pri_s"]
    if pulses_s - WINDOW_START_S - WINDOW_END_S < s.min_current_readings * interval:
        warnings.append(f"the low-power pulses last {pulses_s:.1f} s: too short for {s.min_current_readings} "
                        f"amplifier readings at one every {interval:g} s (they need at least "
                        f"{s.min_current_readings * interval + WINDOW_START_S + WINDOW_END_S:.1f} s: "
                        "raise manager.turn_on_check.pulses)")
    reading = before[0]
    idle = reading["current_a"] if reading is not None else None
    criteria.append(judge_before(s, before, override))
    currents = [r["current_a"] for r in during]
    if idle is None:
        add("amplifier_during", None, "not judged: no idle reading to compare with")
    elif len(currents) < s.min_current_readings:
        # Telemetry's freshness is a gate of its own: a reading too few is no reason to keep the radar off.
        why = (f"only {len(currents)} amplifier readings during the pulses (at least {s.min_current_readings} "
               "needed): not judged")
        add("amplifier_during", None, why)
        warnings.append(why)
    else:
        top = max(currents)
        wrong = []
        if top < idle + s.current_rise_min_a:
            wrong.append(f"amplifier current did not rise: at most {top:.3f} A against {idle:.3f} A idle "
                         f"(at least +{s.current_rise_min_a:g} A expected): is the amplifier enabled?")
        if top > s.current_max_a:
            wrong.append(f"amplifier current {top:.3f} A, above {s.current_max_a:g} A")
        add("amplifier_during", not wrong, "; ".join(wrong) or
            f"up to {top:.3f} A in {len(currents)} readings ({idle:.3f} A idle)")

    m = measurement or {}
    if measurement is not None and not analysis_error and "error" not in m and not _finite(m):
        analysis_error = "a measured value is not a finite number"
    if gain >= requested:
        warnings.append(f"the check ran at the requested tx_gain {requested:g}: it is not at low power")
    if analysis_error or "error" in m:
        add("analysis", False, f"the analysis failed: {analysis_error or m['error']}")
    elif measurement is not None:
        records, wanted = m.get("records", 0), m.get("records_expected") or facts["records"]
        add("data", records >= wanted, f"{records} records" + ("" if records >= wanted else f" of {wanted}"))
        raw = m["raw_max_fs"]
        clipped = not raw < s.clip_fs
        add("clipping", not clipped, f"raw maximum {raw:.3f} of full scale" +
            (f", at or above {s.clip_fs:g}: clipping" if clipped else ""))
        strongest = m["strongest"]
        if not s.verified:
            add("signal", None, f"strongest peak at sample {strongest['sample']:g}, {strongest['level_db']:.1f} dB "
                f"at tx_gain {gain:g} ({_scaled(strongest['level_db'], gain, requested):.1f} dB at tx_gain "
                f"{requested:g}); not judged: the expected peak is not set")
        else:
            d = m.get("direct") or {}
            what = "combined peak" if m.get("mode") == "combined" else "direct peak"
            where = f"sample {d.get('expected', 0):g} +/- {s.tolerance_samples:g}"
            if not d.get("found"):
                near = d.get("window_max")
                add("signal", False, f"no {what} at {where}" + (
                    f" (the highest value there is {_scaled(near['level_db'], gain, s.level_tx_gain):.1f} dB "
                    f"at tx_gain {s.level_tx_gain:g}, at sample {near['index']}, on the slope of another peak)"
                    if near else "") + ": antenna or cable disconnected?")
            else:
                level = _scaled(d["level_db"], gain, s.level_tx_gain)
                ok = level >= s.min_level_db
                add("signal", ok, f"{what} at sample {d['sample']:g}, {level:.1f} dB at tx_gain {s.level_tx_gain:g} "
                    + (f"(at least {s.min_level_db:g})" if ok else
                       f"below the minimum of {s.min_level_db:g} dB: antenna or cable disconnected or damaged?"))
    passed = all(c["ok"] is not False for c in criteria)
    # Only what the antenna, the cable and the receiver show raises the fault; a reading of the
    # amplifier out of its window, or a failed analysis, makes the start a failed one, retried.
    fault = any(c["ok"] is False and c["name"] in FAULT_CRITERIA for c in criteria)
    out = {"passed": passed, "blocking": s.blocks, "blocked": s.blocks and not passed,
           "retry": s.blocks and not passed and not fault,
           "mode": m.get("mode", "none") if s.verified else "none", "criteria": criteria}
    measured = {"idle_current_a": idle, "current_readings": len(currents),
                "current_max_a": max(currents) if currents else None}
    if reading is not None:
        measured.update(voltage_v=reading["voltage_v"], temp_c=reading.get("temp_c"),
                        case_temp_c=reading.get("case_temp_c"))
    if measurement is not None and "error" not in m:
        measured.update(records=m["records"], raw_max_fs=m["raw_max_fs"],
                        raw_max_at_requested_fs=round(m["raw_max_fs"] * 10 ** ((requested - gain) / 20), 3),
                        noise_floor_db=m["noise_floor_db"], strongest_sample=m["strongest"]["sample"],
                        strongest_level_db=m["strongest"]["level_db"])
        for name in ("leakage", "direct"):
            peak = m.get(name)
            if isinstance(peak, dict) and peak.get("found"):
                measured[f"{name}_sample"], measured[f"{name}_level_db"] = peak["sample"], peak["level_db"]
        if measured["raw_max_at_requested_fs"] >= s.clip_fs:
            warnings.append(f"at tx_gain {requested:g} the raw maximum would be about "
                            f"{measured['raw_max_at_requested_fs']:.2f} of full scale: the receiver would clip")
    out["measured"] = measured
    out["warnings"] = warnings
    out["reason"] = "; ".join(c["text"] for c in criteria if c["ok"] is False)
    return out


def _finite(m):
    """The numbers that are judged are all finite (a NaN would pass every comparison)."""
    values = [m.get("raw_max_fs"), (m.get("strongest") or {}).get("level_db")]
    for name in ("direct", "leakage"):
        peak = m.get(name)
        if isinstance(peak, dict) and peak.get("found"):
            values += [peak.get("level_db"), peak.get("sample")]
    return all(type(v) in (int, float) and math.isfinite(v) for v in values)


def summary_text(result):
    """One line for the status, the journal and the alerts."""
    if result.get("stopped"):
        return f"stopped before it was judged ({result['stopped']})"
    warnings = "".join(f"; warning: {w}" for w in result.get("warnings") or [])
    if result["passed"]:
        signal = next((c for c in result["criteria"] if c["name"] == "signal"), None)
        return "passed" + (f": {signal['text']}" if signal else "") + warnings
    if result.get("retry"):
        what = "analysis" if any(c["name"] == "analysis" and c["ok"] is False for c in result["criteria"]) \
            else "amplifier readings"
        return f"FAILED ({what}): a failed start: " + result["reason"] + warnings
    return (
            "FAILED, the radar stays off: " if result["blocked"] else
            "FAILED (record only, the radar starts anyway): ") + result["reason"] + warnings


# ------------------------------------------------------------------------ the check

class TurnOnCheck:
    def __init__(self, config, settings, readings=None, uptime=boottime_s, wall=time.time):
        self.config = config
        self.cfg = config["manager"]
        self.s = settings
        self.readings = readings or AmpReadings(config)
        self.overrides = gates.Overrides(config["paths"]["state_dir"])
        self._uptime, self._wall = uptime, wall
        self.runner = None          # set once the runner exists

    def __call__(self, run):
        started = time.monotonic()
        result = {"collection": run.name, "directory": SUBDIR, "started": coll.stamp(self._wall),
                  "settings": self.s.public()}
        try:
            passed, text = self._check(run, result)
        finally:
            check = run.check_run
            if check is not None and check.amp_safe is False and run.amp_safe is not False:
                # On every path, also an internal error: the run reports what the amplifier step found.
                run.amp_safe, run.amp_safe_ran = False, check.amp_safe_ran
                run.amp_safe_detail = f"after the turn-on check's radar: {check.amp_safe_detail}"
            result["duration_s"] = round(time.monotonic() - started, 2)
            self._record(run, result)
        return passed, text

    def _check(self, run, result):
        s, runner = self.s, self.runner
        orca = os.path.realpath(self.cfg["orca"]["dir"])
        root = os.path.realpath(self.cfg["gates"]["data"]["root"])
        directory = os.path.join(run.directory, SUBDIR)
        config_path = os.path.join(directory, "config.yaml")

        # 1. The low-power config, from the collection's config as written and checked.
        try:
            derived = limits.load_file(os.path.join(run.directory, "config.yaml"))
            lp, facts = low_power_config(derived, s.max_tx_gain, s.pulses, os.path.relpath(directory, orca))
        except (limits.ConfigError, ValueError) as e:
            return self._fail(run, result, "config", f"turn-on check: no low-power config: {e}", fatal=True)
        facts["telemetry_interval_s"] = self.config["telemetry"]["status_interval_s"]
        result["low_power"] = facts
        problems = limits.check(lp, self.cfg["limits"]) + limits.check_output(lp, orca, root)
        extra = differences(derived, lp) - CHANGED
        if extra:
            problems.append("the low-power config differs from the collection's in more than the gain, the pulse "
                            "count and the output: " + ", ".join(".".join(k) for k in sorted(extra)))
        if not facts["tx_gain"] <= min(facts["requested_tx_gain"], s.max_tx_gain):
            problems.append("the low-power tx_gain is above its cap")
        if problems:
            return self._fail(run, result, "config", "turn-on check: low-power config refused: "
                              + "; ".join(problems), fatal=True)
        try:
            os.mkdir(directory, 0o755)
            with open(os.path.join(run.directory, limits.CHIRP_FILE), "rb") as f:
                coll.write_durable(os.path.join(directory, limits.CHIRP_FILE), f.read())
            coll.write_durable(config_path, limits.dump(lp))
            written = limits.load_file(config_path)
        except (OSError, limits.ConfigError) as e:
            return self._fail(run, result, "directory", "turn-on check: cannot write its files in "
                              f"{directory}: {getattr(e, 'strerror', None) or e}")
        if written != lp:
            return self._fail(run, result, "config", "turn-on check: the low-power config does not read back "
                              "as it was derived", fatal=True)
        if run.stop.is_set():
            return self._stopped(run, result)

        # 2. Before the run, 3. the run, with the readings during its pulses.
        override = self.overrides.is_set()
        before = self.readings.latest()
        first = judge_before(s, before, override)
        if s.blocks and first["ok"] is False:
            # Nothing is transmitted when the amplifier's readings are already out of their window.
            result.update(passed=False, blocking=True, blocked=True, retry=True, mode="none", criteria=[first],
                          reason=first["text"], warnings=[], measured={},
                          readings={"before": before[0], "before_problem": before[1], "override": override})
            runner.add_flag(FLAG)
            run.fail("turn_on_check", f"turn-on check failed (a failed start; no low-power run): {first['text']} "
                     f"(collection {run.name})")
            return False, first["text"]
        sampler = _Sampler(self.readings, run, self._uptime)
        sampler.start()
        pulses_s = facts["pulses"] * facts["pri_s"]
        try:
            check = runner.check_radar(run, directory, config_path,
                                       max_run_s=facts["time_offset_s"] + 2 * pulses_s + RUN_MARGIN_S)
        finally:
            sampler.halt.set()
            sampler.join(2.0)
        exit_uptime = check.exit_uptime if check.exit_uptime is not None else runner.last_activity_uptime
        result["radar"] = {k: v for k, v in check.info().items() if k in (
            "launched", "started", "exit_status", "killed", "failure", "kind", "signalled_before_start",
            "amp_safe", "amp_safe_command_run", "amp_safe_detail")}
        if check.amp_safe is False:
            run.amp_safe, run.amp_safe_detail = False, f"after the turn-on check's radar: {check.amp_safe_detail}"
            run.amp_safe_ran = check.amp_safe_ran
        if run.stop.is_set():
            return self._stopped(run, result)
        if check.failure or check.amp_safe is False:
            text = ("turn-on check, low-power radar: "
                    + (check.failure or f"the amplifier pin is not confirmed safe ({check.amp_safe_detail})"))
            result["failure"] = text
            run.fail(check.kind or "amp_unsafe", text, check.fatal)
            return False, text
        if sampler.start_uptime is None:
            sampler.start_uptime = exit_uptime - pulses_s - facts["time_offset_s"]      # cannot happen: started
        window = (sampler.start_uptime + facts["time_offset_s"] + WINDOW_START_S, exit_uptime - WINDOW_END_S)
        during = sorted((r for r in sampler.items.values() if window[0] <= r["uptime"] <= window[1]),
                        key=lambda r: r["uptime"])
        result["readings"] = {"before": before[0], "before_problem": before[1], "window_uptime": window,
                              "during": during, "override": override}

        # 4. The analysis, in a process of its own (numpy).
        spec = {"data_files": data_files(directory),
                "chirp": os.path.join(directory, limits.CHIRP_FILE),
                "record_samples": limits.burst(lp)["rx_samples"], "records_expected": facts["records"],
                "clip_fs": s.clip_fs, "expected": s.expected(), "tx_gain": facts["tx_gain"],
                "requested_tx_gain": facts["requested_tx_gain"]}
        output = os.path.join(directory, OUTPUT_FILE)
        measurement, error = None, ""
        try:
            coll.write_durable(os.path.join(directory, INPUT_FILE), json.dumps(spec, indent=1) + "\n")
        except OSError as e:
            error = f"cannot write its input: {e.strerror}"
        if not error:
            argv = list(s.analysis_command) + ["--input", os.path.join(directory, INPUT_FILE), "--output", output]
            ok, text, status = runner._command(run.stop, argv, None, s.analysis_timeout_s,
                                               "turn-on check analysis", os.path.join(directory, ANALYSIS_LOG))
            if run.stop.is_set():
                return self._stopped(run, result)
            try:
                with open(output) as f:
                    measurement = json.load(f)
                if not isinstance(measurement, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                measurement = None
                error = text or f"no usable result in {OUTPUT_FILE} ({getattr(e, 'strerror', None) or e})"
            if not ok and measurement is not None and "error" not in measurement:
                measurement, error = None, text
        if error and not os.path.isdir(directory):
            return self._fail(run, result, "directory", f"turn-on check: its directory {directory} is gone")
        result["measurement"] = measurement

        # 5. The judgement.
        result.update(judge(s, facts, before, override, during, measurement, error))
        text = summary_text(result)
        if result["warnings"]:
            runner.add_flag(WARNING_FLAG)
        if result["passed"]:
            return True, text
        runner.add_flag(FLAG)
        if result["retry"]:
            run.fail("turn_on_check", f"turn-on check failed (a failed start): {result['reason']} "
                     f"(collection {run.name})")
            return False, result["reason"]
        if result["blocked"]:
            return False, f"{result['reason']} (collection {run.name}; the next button press or trigger tries again)"
        return True, text

    def _fail(self, run, result, kind, text, fatal=False):
        result["failure"] = text
        run.fail(kind, text, fatal)
        return False, text

    def _stopped(self, run, result):
        result["stopped"] = run.stop_reason or "stop requested"
        return True, "stopped"

    def _record(self, run, result):
        """result.json, collection.json and the event, whatever happened. Never raises."""
        try:
            if "passed" not in result:          # not judged: stopped, or the run failed before
                result.update(passed=None, blocking=self.s.blocks, blocked=False, criteria=[], mode="none",
                              reason=result.get("failure", ""))
            result["text"] = result.get("failure") or summary_text(result)
            summary = {k: result.get(k) for k in ("collection", "passed", "blocking", "blocked", "text")}
            summary.update({k: result[k] for k in ("mode", "reason", "measured", "warnings", "stopped", "failure",
                                                   "duration_s") if result.get(k) not in (None, "")})
            summary["criteria"] = [{"name": c["name"], "ok": c["ok"]} for c in result.get("criteria", [])]
            summary["low_power"] = {k: v for k, v in (result.get("low_power") or {}).items()
                                    if k in ("tx_gain", "requested_tx_gain", "pulses")}
            summary["time_utc"] = result["started"]["time_utc"]
            directory = os.path.join(run.directory, SUBDIR)
            if os.path.isdir(directory):
                try:
                    coll.write_durable(os.path.join(directory, RESULT_FILE),
                                       json.dumps(result, indent=1, default=str) + "\n")
                except OSError as e:
                    log.warning("cannot write the turn-on check's result: %s", e.strerror)
            if run.meta is not None:
                run.meta.data["turn_on_check"] = summary
                run.meta.write()
        except Exception:
            log.exception("internal error while recording the turn-on check's result")
            summary = {"collection": run.name, "passed": None, "text": "internal error while recording the result"}
        if self.runner is not None:
            self.runner.post("turn_on_check", summary)
