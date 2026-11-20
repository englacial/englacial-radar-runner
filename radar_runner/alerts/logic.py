"""What the alerts service decides, without I/O and without a clock.

Times passed in are seconds since boot (CLOCK_BOOTTIME), never the wall clock,
so a clock step (the Pi has no clock battery) changes no decision here. State
that is kept across a restart carries the boot ID and is used only in the same boot.

    scrub(text, secrets)            remove positions and secrets from text for ntfy
    watcher_alert, manager_alert    an event of the safety watcher or the manager -> Alert or None
    condition_states(...)           the status files -> the conditions that may become alerts
    Conditions                      a condition becomes an alert after lasting long enough
    DiskLevels                      one alert per free-space level crossed
    Limiter                         the storm limit
    boot_summary(...)               the text of the summary at each boot
"""
import re
from dataclasses import dataclass, field

URGENT, HIGH, DEFAULT, LOW = 5, 4, 3, 2         # ntfy priorities (5 = max: breaks through on the phone)
PRIORITY_NAMES = {URGENT: "urgent", HIGH: "high", DEFAULT: "default", LOW: "low"}
BY_NAME = {name: p for p, name in PRIORITY_NAMES.items()}
SEVERITY = {"urgent": URGENT, "warning": DEFAULT, "info": LOW}      # the watcher's event severities


@dataclass
class Alert:
    key: str                    # the kind of alert, for the storm limit
    priority: int
    title: str
    message: str
    limited: bool = True        # False: never held back (boot summary, test alerts)
    clears: str = ""            # the key of the alert that this one says is over
    tags: list = field(default_factory=list)


# ---------------------------------------------------------------- what may leave the Pi

REMOVED = "[removed]"
_NUMBER = r"[-+]?\d+(?:\.\d+)?"
# The second line of defence (positions are never read from named fields): narrow enough that
# readings, sizes, counts, durations and PIDs pass. Hemisphere letters only in capitals.
_POSITION_PATTERNS = [
    # Degrees (not degrees Celsius), with minutes and seconds.
    re.compile(r"[-+]?\d+(?:\.\d+)?\s*°(?!\s*[CF]\b)(?:\s*\d+(?:\.\d+)?\s*['′])?(?:\s*\d+(?:\.\d+)?\s*[\"″])?"
               r"\s*[NSEW]?"),
    # 77.85S 166.67E, 77.85 S, 166.67 E; S 77.85 E 166.67
    re.compile(r"(?<![\w.])\d{1,3}\.\d+\s?[NS]\b[\s,;/]*\d{1,3}\.\d+\s?[EW]\b"),
    re.compile(r"\b[NS]\s?\d{1,3}\.\d+[\s,;/]+[EW]\s?\d{1,3}\.\d+"),
    # One of them alone, with three or more decimals (100 m): 77.846S, S77.846
    re.compile(r"(?<![\w.])\d{1,3}\.\d{3,}\s?[NSEW]\b"),
    re.compile(r"\b[NSEW]\s?\d{1,3}\.\d{3,}(?!\d)"),
    # Two numbers with three or more decimals side by side, one of them with two or three digits
    # before the point: a latitude and a longitude.
    re.compile(r"(?<![\w.])[-+]?(?:\d{2,3}\.\d{3,}\s*[,;/ ]\s*[-+]?\d{1,3}|\d{1,3}\.\d{3,}\s*[,;/ ]\s*[-+]?\d{2,3})"
               r"\.\d{3,}(?!\d)"),
    # A number, or two, after a word that names a position or a coordinate (lat_deg=, pos=(...), ...).
    re.compile(r"(?i)((?<![a-z])(?:gps_)?(?:lat|lats|latitude|lon|lng|gps_long|longitude|pos|position|positions|"
               r"coord|coords|coordinate|coordinates|northing|easting|geo)(?:_[a-z0-9]+)?(?![a-z])\W{0,4}?)"
               + _NUMBER + r"(?:\s*[,;/ ]\s*" + _NUMBER + ")?"),
    # A number of degrees with four or more decimals (11 m), and the OLA's degrees x 1e7 (8 to 10 digits).
    re.compile(r"(?<![\w.])[-+]?[1-9]\d{0,2}\.\d{4,}(?!\d)"),
    re.compile(r"(?<![\w.])[-+]?\d{8,10}(?:\.\d+)?(?![\w.])"),
]
_SECRET_PATTERNS = [
    re.compile(r"\btk_[A-Za-z0-9]{6,}"),                                   # ntfy access tokens
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(token|password|passwd|secret|credentials?|api[_-]?key)(\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),          # user:password@ in a URL
]
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def scrub(text, secrets=()):
    """`text` with anything that could be a position, and every secret, replaced by [removed]."""
    text = _CONTROL.sub(" ", str(text))
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4 and secret != "-":
            text = text.replace(secret, REMOVED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + m.group(2) if m.lastindex == 2 else "") + REMOVED, text)
    for pattern in _POSITION_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + REMOVED, text)
    return text


def clip(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:max(limit - 3, 0)] + "..."


# ---------------------------------------------------------------- events

WATCHER_CONDITIONS = {"amp_temp": "amplifier temperature", "case_temp": "amplifier case temperature",
                      "voltage": "28 V supply", "amp_enabled": "amplifier enabled with no radar",
                      "pin": "amplifier pin not known safe"}
WATCHER_HEADLINES = {
    "stuck": "AMPLIFIER STUCK ENABLED",
    "blocked": "AMPLIFIER ENABLED, RESET TOOL CANNOT RUN",
    "radar_stop": "safety watcher stopped the radar",
    "radar_kill": "radar did not stop: killed",
    "signal_failed": "safety watcher cannot signal the radar",
    "reset_tool_result": "SDR GPIO reset tool did not succeed",
    "stall": "safety watcher stalled",
    "warning": "safety watcher warning",
    "warning_cleared": "safety watcher warning over",
}
# Events that are never alerts by themselves. "not_ok" is the radar being kept off, which happens at
# every start for a few seconds: it is the condition `watcher_not_ok` when it lasts.
WATCHER_QUIET = {"not_ok", "ok", "start", "stop", "unpowered", "powered", "reset_tool", "radar_stopped"}


def _text(event, name="text"):
    value = event.get(name)
    return value if isinstance(value, str) else ""


def watcher_alert(event, previous=None, power_loss=False):
    """The alert for one event of the safety watcher's log, or None.

    `previous` is the event before it in the log (None if not known); `power_loss` is true when the
    power service reports a loss of main power right now.
    """
    name, severity = event.get("event"), event.get("severity")
    condition = event.get("condition") if isinstance(event.get("condition"), str) else ""
    what = event.get("warning") if isinstance(event.get("warning"), str) else condition
    key = f"watcher.{name}.{what}"
    if name == "start" and _restarted(event, previous, "stop"):
        return Alert("watcher.restart", DEFAULT, "safety watcher restarted",
                     "The safety watcher started again without having stopped cleanly (crash, hang or kill). "
                     + _text(event))
    if name in WATCHER_QUIET:
        return None
    if name == "trip":
        if condition == "voltage" and power_loss:
            return None         # the 28 V falls with main power: part of every shutdown
        label = WATCHER_CONDITIONS.get(condition, condition or "limit")
        priority = SEVERITY.get(severity, DEFAULT)
        return Alert(key, priority, ("URGENT: " if priority == URGENT else "") + f"safety trip: {label}", _text(event))
    if name == "clear":
        label = WATCHER_CONDITIONS.get(condition, condition or "limit")
        return Alert(key, LOW, f"safety trip over: {label}", _text(event), clears=f"watcher.trip.{condition}")
    if name == "warning_cleared":
        return Alert(key, LOW, WATCHER_HEADLINES[name], _text(event), clears=f"watcher.warning.{what}")
    if severity not in ("warning", "urgent"):
        return None
    priority = SEVERITY[severity]
    headline = WATCHER_HEADLINES.get(name, f"safety watcher: {name}")
    return Alert(key, priority, ("URGENT: " if priority == URGENT and not headline.isupper() else "") + headline,
                 _text(event))


def _restarted(event, previous, clean_stop):
    """True if this start follows, in the same boot, something other than a clean stop."""
    if not isinstance(previous, dict) or previous.get("event") == clean_stop:
        return False
    up, before = event.get("uptime_s"), previous.get("uptime_s")
    return isinstance(up, (int, float)) and isinstance(before, (int, float)) and up >= before


_ERRORS = re.compile(r"\[RX\] Error count: (\d+)")
_ATTEMPTED = re.compile(r"\[RX\] Total pulses attempted: (\d+)")


def error_rate(event):
    """(errors, pulses attempted) from ORCA's last lines in an `ended` event, or None."""
    tail = event.get("output_tail")
    if not isinstance(tail, list):
        return None
    text = "\n".join(line for line in tail if isinstance(line, str))
    errors, attempted = _ERRORS.search(text), _ATTEMPTED.search(text)
    if not errors or not attempted or int(attempted.group(1)) == 0:
        return None
    return int(errors.group(1)), int(attempted.group(1))


def _count(n):
    """A count in words: never seven digits in a row, which look like a position to the filter."""
    if n >= 1_000_000:
        return f"{n / 1e6:.1f} million"
    if n >= 10_000:
        return f"{n / 1e3:.0f} thousand"
    return str(n)


def manager_alert(event, previous=None, error_rate_warn=None):
    """The alert for one event of the radar manager's log, or None.

    `error_rate_warn`: a run whose ORCA error count is above this fraction of the pulses attempted
    is an alert (the late-command rate; ORCA prints both at its exit).
    """
    name = event.get("event")
    kind = event.get("kind") if isinstance(event.get("kind"), str) else ""
    if name == "ended" and not _text(event, "failure") and error_rate_warn is not None:
        counts = error_rate(event)
        if counts and counts[0] / counts[1] > error_rate_warn:
            return Alert("manager.error_rate", DEFAULT, "radar error rate high",
                         f"The last run had {_count(counts[0])} errors in {_count(counts[1])} pulses "
                         f"({100 * counts[0] / counts[1]:.1f} %, warning above {100 * error_rate_warn:g} %): "
                         "late commands from a busy Pi? The data is still recorded."
                         + (f" Collection {_text(event, 'collection')}." if _text(event, "collection") else ""))
    if name == "fault":
        return Alert(f"manager.fault.{kind}", HIGH, f"radar FAULT: {kind}",
                     f"The radar manager is in fault ({kind}): {_text(event, 'reason')}. "
                     "The radar stays off until the fault is cleared (radar_runner/manager/README.md, \"Faults\").")
    if name == "fault_cleared":
        return Alert(f"manager.fault_cleared.{kind}", LOW, f"radar fault cleared: {kind}",
                     f"Fault {kind} cleared by {_text(event, 'by') or 'unknown'}.", clears=f"manager.fault.{kind}")
    if name == "ended" and _text(event, "failure"):
        return Alert("manager.ended", LOW, "radar run failed",
                     f"A radar run ended with a failure: {_text(event, 'failure')}"
                     + (f" (collection {_text(event, 'collection')})" if _text(event, "collection") else ""))
    if name == "manager_started" and _restarted(event, previous, "manager_stopped"):
        return Alert("manager.restart", DEFAULT, "radar manager restarted",
                     "The radar manager started again without having stopped cleanly (crash, watchdog or kill). "
                     "A run that was going is not resumed by a restart.")
    return None


# ---------------------------------------------------------------- conditions from the status files

# The title by telemetry's `sources.gps.no_position` cause, else by its health. Health and reason only,
# never a reading.
GPS_TITLES = {"gpsd_unidentified": "GPS receiver sends no position (not identified by gpsd)",
              "no_position_messages": "GPS receiver sends no position messages",
              "no_data": "GPS: no data from the receiver",
              "no_fix": "GPS: no fix (antenna or sky?)",
              "missing": "GPS: no receiver, or gpsd not answering",
              "stale": "GPS: no data from the receiver",
              "error": "GPS receiver not working"}
POWER_HIGH = ("no_power_monitoring", "boot_loop_guard", "guard_state_unwritable")
MAX_POWER_WARNINGS = 20     # the power service has seven; more is a damaged file
MAX_KINDS = 200             # kinds of alert followed by the storm limit


@dataclass
class State:
    """One condition as read now. active None: the source cannot say; nothing changes."""
    active: object
    priority: int = DEFAULT
    title: str = ""
    text: str = ""
    delay_s: float = 0.0


def _get(data, *path):
    for name in path:
        if not isinstance(data, dict):
            return None
        data = data.get(name)
    return data


def _num(value):
    return value if type(value) in (int, float) else None


def condition_states(reads, cfg, tracked=()):
    """The conditions as the status files show them now: {name: State}.

    `reads` maps telemetry, watcher, manager and power to a status_files.StatusRead; `cfg` is the
    `alerts.conditions` section plus `sd_free_gb_min`; `tracked` the names already followed (so
    that a power warning that has gone away is seen as over).
    """
    out = {}
    stale = cfg["status_stale_s"]
    tel, wat, man, pwr = (reads.get(n) for n in ("telemetry", "watcher", "manager", "power"))

    def status_down(name, read, priority, label, consequence):
        if read is None:
            return
        if read.ok:
            out[name] = State(False)
            return
        stopped = isinstance(read.data, dict) and (read.data.get("running") is False
                                                     or read.data.get("state") == "stopped")
        how = "was stopped" if stopped else f"is not updating its status ({read.state}: {read.error})"
        out[name] = State(True, priority, f"{label} not running", f"The {label} {how}. {consequence}", stale)

    status_down("telemetry_status", tel, DEFAULT, "telemetry service",
                "No sensor logs and no amplifier readings: the radar cannot run.")
    status_down("watcher_status", wat, HIGH, "safety watcher",
                "Nothing watches the amplifier; the manager does not start the radar.")
    status_down("power_status", pwr, HIGH, "power service",
                "A loss of main power will not shut the Pi down cleanly.")
    status_down("manager_status", man, DEFAULT, "radar manager", "The radar is not run.")

    if wat is not None and wat.ok:
        ok = wat.data.get("ok")
        out["watcher_not_ok"] = State(ok is False, DEFAULT, "radar kept off by the safety watcher",
                                      f"The safety watcher has not let the radar run for over "
                                      f"{cfg['watcher_not_ok_s']:g} s: {wat.data.get('reason')}",
                                      cfg["watcher_not_ok_s"])
    if man is not None and man.ok:
        reasons = man.data.get("not_ready") if isinstance(man.data.get("not_ready"), list) else []
        out["not_ready"] = State(man.data.get("state") == "not_ready", DEFAULT, "radar not ready",
                                 "The radar manager has been not ready for over "
                                 f"{cfg['not_ready_s']:g} s: " + ("; ".join(str(r) for r in reasons) or "no reason given"),
                                 cfg["not_ready_s"])

    if tel is not None and tel.ok:
        d = tel.data
        gps = _get(d, "sources", "gps")
        if not isinstance(gps, dict):
            out["gps"] = State(True, DEFAULT, "GPS: telemetry has no GPS source",
                               "The telemetry status has no GPS source at all (its version or settings?). "
                               "The radar still runs; its data lacks positions.", cfg["gps_s"])
        else:
            problem = gps.get("health") != "ok" or bool(gps.get("reason"))
            # Looked up only as texts: a list there would be an unhashable key.
            titles = [GPS_TITLES.get(gps.get(k)) for k in ("no_position", "health") if isinstance(gps.get(k), str)]
            out["gps"] = State(problem, DEFAULT, next((t for t in titles if t), "GPS: no fix"),
                               f"GPS {gps.get('health')}: {gps.get('reason') or 'no fix'}. The radar still runs; "
                               "its data lacks positions.", cfg["gps_s"])
        logs = d.get("logging") if isinstance(d.get("logging"), dict) else {}
        bad = logs.get("location") != "primary" or logs.get("writer", "running") != "running"
        out["logging"] = State(bad, DEFAULT, "sensor logs not on the data drive",
                               f"Sensor logs: location {logs.get('location')}, writer {logs.get('writer')}"
                               + (f" ({logs.get('reason')})" if logs.get("reason") else "")
                               + ". Is the data drive there and writable?", cfg["logging_fallback_s"])
        pi = _get(d, "sources", "pi") if isinstance(_get(d, "sources", "pi"), dict) else {}
        values = pi.get("values") if isinstance(pi.get("values"), dict) else {}
        soc = _num(values.get("soc_temp_c"))
        throttled = pi.get("throttled_now") if isinstance(pi.get("throttled_now"), list) else []
        hot = bool(throttled) or (soc is not None and soc >= cfg["pi_temp_c"])
        out["pi_hot"] = State(hot, DEFAULT, "Pi hot or throttling",
                              f"Pi SoC {soc} C" + (f", now: {', '.join(map(str, throttled))}" if throttled else "")
                              + ". A hot Pi raises the radar's late-command rate.", cfg["pi_hot_s"])
        sd = _num(values.get("sd_free_gb"))
        if sd is not None:
            out["sd_low"] = State(sd < cfg["sd_free_gb_min"], DEFAULT, "SD card nearly full",
                                  f"{sd:.1f} GB free on the SD card (journal, state, fallback logs).", 60.0)

    if pwr is not None:
        warnings = pwr.data.get("warnings") if pwr.ok and isinstance(pwr.data.get("warnings"), dict) else None
        codes = set(sorted(map(str, warnings or ()))[:MAX_POWER_WARNINGS])
        codes |= {n[len("power."):] for n in tracked if n.startswith("power.")}
        for code in sorted(codes):
            if warnings is None:
                out[f"power.{code}"] = State(None)
                continue
            out[f"power.{code}"] = State(code in warnings, HIGH if code in POWER_HIGH else DEFAULT,
                                         f"power service: {code.replace('_', ' ')}", str(warnings.get(code, "")),
                                         cfg["power_warning_s"])
    return out


class Conditions:
    """A condition is an alert once it has lasted its delay, and "over" once gone for `clear_s`."""

    def __init__(self, clear_s, restored=None):
        self.clear_s = clear_s
        # name -> {"since", "raised", "gone_since", "title"}
        self.entries = dict(restored) if isinstance(restored, dict) else {}

    def update(self, now, states):
        alerts = []
        for name, s in states.items():
            e = self.entries.get(name)
            if s.active is None:
                continue
            if s.active:
                if e is None:
                    e = self.entries[name] = {"since": now, "raised": False, "gone_since": None, "title": s.title}
                e["gone_since"] = None
                if not e["raised"] and now - e["since"] >= s.delay_s:
                    e["raised"], e["title"] = True, s.title
                    alerts.append(Alert(f"cond.{name}", s.priority, s.title, s.text))
            elif e is not None:
                if not e["raised"]:
                    del self.entries[name]
                    continue
                if e["gone_since"] is None:
                    e["gone_since"] = now
                if now - e["gone_since"] >= self.clear_s:
                    del self.entries[name]
                    alerts.append(Alert(f"cond.{name}.over", LOW, f"over: {e['title']}",
                                        f"No longer: {e['title']} (for {self.clear_s:g} s).", clears=f"cond.{name}"))
        return alerts

    def active(self):
        return {name: e for name, e in self.entries.items() if e["raised"]}


class DiskLevels:
    """One alert when the free space on the data drive falls below each level; a level alerts again
    only after the free space has been `rearm_fraction` above it. Kept across reboots."""

    def __init__(self, levels, rearm_fraction, restored=None):
        self.levels = sorted({float(x) for x in levels if type(x) in (int, float) and x > 0}, reverse=True)
        self.rearm = rearm_fraction
        self.crossed = {float(x) for x in restored if type(x) in (int, float)} if isinstance(restored, list) else set()

    def update(self, free_gb):
        if _num(free_gb) is None or not self.levels:
            return []
        for level in list(self.crossed):
            if free_gb > level * (1 + self.rearm):
                self.crossed.discard(level)
        new = [level for level in self.levels if free_gb < level and level not in self.crossed]
        if not new:
            return []
        self.crossed.update(new)
        lowest = min(new)
        priority = HIGH if lowest == self.levels[-1] else DEFAULT
        return [Alert(f"disk.{lowest:g}", priority, f"data drive below {lowest:g} GB free",
                      f"{free_gb:.1f} GB free on the data drive. The radar manager stops recording at its "
                      "reserve; data is never deleted automatically.")]


class Limiter:
    """The storm limit.

    Per kind (Alert.key): at most `burst` alerts in any `window_s`. The ones held back are counted;
    the next one let through says how many came before it, and if none comes, a note says so once
    the window allows it. All kinds together: at most `max_per_hour` in any hour, urgent ones not
    counted and never held back by this second limit. An alert saying that something is over
    (`clears`) goes out only if the alert it clears went out, and does not count.
    Times are seconds since boot; a state restored from another boot keeps only the counts.
    """

    def __init__(self, burst, window_s, max_per_hour, restored=None, same_boot=False):
        self.burst, self.window_s, self.max_per_hour = max(1, burst), window_s, max(1, max_per_hour)
        self.kinds = {}         # key -> {"times": [...], "held": n, "priority": p, "title": str, "text": str}
        self.recent = []        # times of everything counted against max_per_hour
        self.passed = {}        # key -> whether its last alert went out
        self.held_total = 0
        if isinstance(restored, dict):
            def times(value):
                return [t for t in value if type(t) in (int, float)] if same_boot and isinstance(value, list) else []

            def count(value):
                return value if type(value) is int and value >= 0 else 0
            for key, k in list((restored.get("kinds") or {}).items())[:MAX_KINDS]:
                if isinstance(k, dict):
                    self.kinds[str(key)] = {"times": times(k.get("times")), "held": count(k.get("held")),
                                            "priority": min(max(count(k.get("priority")), LOW), URGENT),
                                            "title": str(k.get("title") or key), "text": str(k.get("text") or "")}
            self.recent = times(restored.get("recent"))
            # Kept across a reboot: a trip that ends in the next boot still gets its "over".
            if isinstance(restored.get("passed"), dict):
                self.passed = {str(k): bool(v) for k, v in restored["passed"].items()}
            self.held_total = count(restored.get("held_total"))

    def snapshot(self):
        return {"kinds": self.kinds, "recent": self.recent, "passed": self.passed, "held_total": self.held_total}

    def _prune(self, now):
        self.recent = [t for t in self.recent if 0 <= now - t < 3600.0]
        for key in list(self.kinds):
            k = self.kinds[key]
            k["times"] = [t for t in k["times"] if 0 <= now - t < self.window_s]
            if not k["times"] and not k["held"]:
                del self.kinds[key]

    def admit(self, now, alert):
        """True if `alert` may be queued now. May add a line to its message about held-back ones."""
        if not alert.limited:
            return True
        if alert.clears:
            return self.passed.get(alert.clears, False)
        self._prune(now)
        if alert.key not in self.kinds and len(self.kinds) >= MAX_KINDS:
            alert.key = "other"         # odd input must not grow the state without bound
        if len(self.passed) > 4 * MAX_KINDS:
            self.passed = {key: v for key, v in self.passed.items() if key in self.kinds}
        k = self.kinds.setdefault(alert.key, {"times": [], "held": 0, "priority": LOW, "title": "", "text": ""})
        over_global = alert.priority < URGENT and len(self.recent) >= self.max_per_hour
        if len(k["times"]) >= self.burst or over_global:
            k["held"] += 1
            k["priority"] = max(k["priority"], alert.priority) if k["held"] > 1 else alert.priority
            k["title"], k["text"] = clip(alert.title, 200), clip(alert.message, 500)
            self.held_total += 1
            self.passed[alert.key] = False
            return False
        k["times"].append(now)
        if alert.priority < URGENT:
            self.recent.append(now)         # urgent ones neither count nor are held back by the overall limit
        if k["held"]:
            alert.message += f"\n({k['held']} more like this held back before this one.)"
            k["held"] = 0
        self.passed[alert.key] = True
        return True

    def notes(self, now):
        """Notes for kinds whose held-back alerts were not followed by one that went out."""
        self._prune(now)
        out = []
        for key, k in self.kinds.items():
            if k["held"] and len(k["times"]) < self.burst and (k["priority"] == URGENT
                                                                or len(self.recent) < self.max_per_hour):
                out.append(Alert(key, k["priority"], f"{k['held']} more: {k['title']}",
                                 f"{k['held']} more alerts of this kind were held back (storm limit). "
                                 f"The last one: {k['text']}", limited=False))
                k["times"].append(now)
                if k["priority"] < URGENT:
                    self.recent.append(now)
                k["held"] = 0
                self.passed[key] = True
        return out


# ---------------------------------------------------------------- the boot summary

def _duration(seconds):
    if _num(seconds) is None:
        return "?"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


def _gb(value):
    return f"{value:.1f} GB" if _num(value) is not None else "unknown"


def boot_summary(f):
    """The summary alert. `f` is a dict of facts gathered by the service (see Alerts._summary_facts)."""
    lines = [f["unsettled"]] if f.get("unsettled") else []
    boot = f.get("boot")
    clock = {1: "clock set", 0: "CLOCK NOT SET"}.get(f.get("clock_synced"), "clock state unknown")
    lines.append(f"Boot {boot if boot is not None else '?'}, up {_duration(f.get('uptime_s'))}, {clock}.")

    prev = f.get("previous")
    if prev is None:
        lines.append("Previous boot: no record (first start of the alerts service, or its state was lost).")
    else:
        gap = ""
        if type(boot) is int and type(prev.get("boot")) is int and boot - prev["boot"] > 1:
            gap = f" (boots {prev['boot'] + 1}-{boot - 1}: no record)"
        if prev.get("power_shutdown"):
            how = f"shut down by the power service ({prev['power_shutdown']})"
        elif prev.get("clean_stop"):
            how = "the alerts service was stopped cleanly (a shutdown, a reboot or by hand)"
        else:
            how = ("ended without a clean stop (power cut, hang and watchdog, or crash); "
                   f"last sign of life at {_duration(prev.get('last_uptime_s'))}")
        number = prev.get("boot") if prev.get("boot") is not None else "?"
        lines.append(f"Previous boot {number}: up at least {_duration(prev.get('last_uptime_s'))}; {how}{gap}.")
        counts = {k: v for k, v in (prev.get("alerts") or {}).items() if str(k).isdigit()}
        if counts:
            lines.append("Alerts in that boot: " + ", ".join(
                f"{counts[p]} {PRIORITY_NAMES.get(int(p), p)}" for p in sorted(counts, key=int, reverse=True)) + ".")

    disk = f.get("disk") or {}
    lines.append(f"Data drive: {_gb(disk.get('nvme_free_gb'))} free. SD card: {_gb(disk.get('sd_free_gb'))} free.")

    col = f.get("collections")
    if col is None:
        lines.append("Last collection: not looked at.")
    elif col.get("error"):
        lines.append(f"Last collection: {col['error']}.")
    elif not col.get("last"):
        lines.append(f"Last collection: none ({col.get('count', 0)} on the drive).")
    else:
        last = col["last"]
        lines.append(f"Last collection: {last.get('name')}, {last.get('how')}"
                     + (f" ({last['reason']})" if last.get("reason") else "")
                     + (f", recorded {_duration(last['recorded_s'])}" if last.get("recorded_s") is not None else "")
                     + f", {last.get('data_gb', 0):.2f} GB of data; {col.get('count')} collections on the drive.")

    for line in f.get("services") or []:
        lines.append(line)
    active = f.get("active") or []
    lines.append("Active alerts: " + ("; ".join(active) if active else "none") + ".")
    q = f.get("queue") or {}
    lines.append(f"Alert queue: {q.get('length', 0)} waiting, {q.get('dropped', 0)} dropped (queue full), "
                 f"{q.get('held', 0)} held back (storm limit) so far.")
    return Alert("summary", DEFAULT, f"boot {boot if boot is not None else '?'} summary", "\n".join(lines),
                 limited=False, tags=["summary"])
