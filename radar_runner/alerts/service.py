"""The alerts service: reads what the other services publish, queues alerts on disk, sends them to ntfy.

One process and one loop (radar_runner/common/service.py), once a second:

1. test alerts from alertctl (the inbox directory);
2. new lines of the safety watcher's and the manager's event logs;
3. the status files of telemetry, watcher, manager and power, as conditions;
4. the free space on the data drive, from the telemetry status;
5. the boot summary, once per boot;
6. every alert through the storm limit, the position and secret filter, into the queue;
7. the queue, highest priority first, to ntfy while the link works.

It is an observer only: it opens no device, signals no process, and writes
only its own files (its status file, its state file, its queue and inbox).
No other unit depends on it. Rules and numbers: README.md.
"""
import copy
import json
import logging
import os
import re
import socket
import threading
import time

from ..common import bootcount
from ..common import config as config_module
from ..common import status as status_files
from ..common.service import Service
from . import logic as L
from . import ntfy
from .queue import DiskQueue, write_durably
from .tail import EventTail

STATE_VERSION = 1
HEARTBEAT_S = 300.0         # the state file records that this boot is still alive this often
REJECT_LIMIT = 3            # a message refused this often by the server (400, 413, 422) is dropped
INBOX_MAX_BYTES = 16384
INBOX_PER_ROUND = 10
SCAN_TIMEOUT_S = 10.0       # the data drive must answer the summary's directory listing within this
COLLECTION_RE = re.compile(r"^b(\d{5,})_s(\d{3,})_")
RECENT = 20
SEND_ROUND_S = 0.5          # the loop waits at most this long per round for sends to finish
MAX_STALE = 2               # sends given up on but still in a DNS lookup; no new one beyond this
STALE_EXIT_S = 600.0        # at that limit this long (nothing at all can be sent): exit, systemd restarts
UNREMOVABLE_RETRY_S = 60.0
UNREADABLE_RETRY_S = 600.0


MESSAGE_MAX_BYTES = 4000    # ntfy turns a longer message into an attachment, or refuses it

# Settings that cannot work revert to their defaults, with a warning: (section, settings, test, what must hold).
_RULES = (
    ("", ("loop_interval_s",), lambda c: 0.1 <= c["loop_interval_s"] <= 10.0, "between 0.1 and 10 s"),
    ("", ("queue_max",), lambda c: 10 <= c["queue_max"] <= 10000, "between 10 and 10000"),
    ("", ("title_max_chars", "message_max_chars"),
     lambda c: 20 <= c["title_max_chars"] <= 250 and 200 <= c["message_max_chars"] <= 3000,
     "20-250 and 200-3000 characters"),
    ("ntfy", ("timeout_s",), lambda c: 2.0 <= c["timeout_s"] <= 25.0, "between 2 and 25 s (the watchdog is 60 s)"),
    ("ntfy", ("retry_s", "retry_max_s"), lambda c: 1.0 <= c["retry_s"] <= c["retry_max_s"] <= 3600.0,
     "1 s <= retry_s <= retry_max_s <= 3600 s"),
    ("ntfy", ("max_per_round",), lambda c: 1 <= c["max_per_round"] <= 20, "between 1 and 20"),
    ("storm", ("burst", "window_s", "max_per_hour"),
     lambda c: 1 <= c["burst"] <= 100 and 60.0 <= c["window_s"] <= 86400.0 and 1 <= c["max_per_hour"] <= 1000,
     "burst 1-100, window_s 60-86400 s, max_per_hour 1-1000"),
    ("events", ("max_read_kb",), lambda c: 64 <= c["max_read_kb"] <= 4096, "between 64 and 4096"),
    ("", ("error_rate_warn",), lambda c: 0.0 < c["error_rate_warn"] <= 1.0, "between 0 and 1"),
)


def checked(cfg, defaults=None):
    """The `alerts` section with impossible values replaced by the defaults: (settings, warnings)."""
    defaults = config_module.DEFAULTS["alerts"] if defaults is None else defaults
    cfg, warnings = copy.deepcopy(cfg), []
    for section, names, good, text in _RULES:
        mine, base = (cfg[section], defaults[section]) if section else (cfg, defaults)
        if not good(mine):
            for name in names:
                mine[name] = base[name]
            prefix = f"alerts.{section}." if section else "alerts."
            warnings.append(f"{', '.join(prefix + n for n in names)}: must be {text}: defaults used")
    return cfg, warnings


def _path(base, name):
    return os.path.join(base, name)         # a full path stays as it is


def scan_collections(root):
    """The newest collection under `root`: {"count", "last": {...}} (raises OSError)."""
    found = []
    for name in os.listdir(root):
        m = COLLECTION_RE.match(name)
        if m:
            found.append((int(m.group(1)), int(m.group(2)), name))
    if not found:
        return {"count": 0, "last": None}
    name = max(found)[2]
    directory = os.path.join(root, name)
    meta = {}
    try:
        with open(os.path.join(directory, "collection.json"), "rb") as f:
            meta = json.loads(f.read(65536))
    except (OSError, ValueError):
        pass
    meta = meta if isinstance(meta, dict) else {}
    end = meta.get("end") if isinstance(meta.get("end"), dict) else None
    started = meta.get("recording_started") if isinstance(meta.get("recording_started"), dict) else None
    recorded = None
    if end and started and all(type(x.get("uptime_s")) in (int, float) for x in (end, started)):
        recorded = max(end["uptime_s"] - started["uptime_s"], 0.0)
    data_file = (meta.get("data") or {}).get("file") if isinstance(meta.get("data"), dict) else None
    data_file = data_file if isinstance(data_file, str) and data_file else "rx_samps.bin"
    size = 0
    for entry in os.scandir(directory):
        if entry.name.startswith(data_file):
            try:
                size += entry.stat().st_size
            except OSError:
                pass
    how = (str(end.get("how") or "ended") if end else
           "cut off (no end recorded: power cut or crash)" if meta else "no collection.json")
    return {"count": len(found), "last": {"name": name, "how": how,
                                          "reason": str(end.get("reason") or "") if end else "",
                                          "recorded_s": recorded, "data_gb": size / 1e9}}


class Alerts(Service):
    """Alerts: queues alerts on disk and sends them to ntfy. Reads the other services' files only."""
    name = "alerts"

    def __init__(self, config, *, wall=time.time, uptime=status_files.boottime_s, boot_id=None, **kw):
        super().__init__(config, **kw)
        self.cfg, problems = checked(config["alerts"])
        self.config_warnings += problems
        cfg = self.cfg
        self.interval_s = cfg["loop_interval_s"]
        self._wall, self._uptime = wall, uptime
        self.boot_id = status_files.current_boot_id() if boot_id is None else boot_id
        paths = config["paths"]
        self.state_dir, self.run_dir = paths["state_dir"], paths["run_dir"]
        self.queue = DiskQueue(_path(self.state_dir, cfg["queue_dir"]), cfg["queue_max"])
        self.inbox_dir = _path(self.state_dir, cfg["inbox_dir"])
        self.state_path = _path(self.state_dir, cfg["state_file"])
        ev = cfg["events"]
        watcher_file = (ev["watcher_file"] if ev["watcher_file"] != "-"
                        else _path(self.state_dir, config["watcher"]["events_file"]))
        manager_file = ev["manager_file"]
        if manager_file == "-":
            manager_file = config["manager"]["events"]["file"]
            manager_file = (_path(self.state_dir, "manager-events.jsonl") if manager_file in ("-", "")
                            else manager_file)
        self.tails = {"watcher": EventTail(watcher_file, ev["max_read_kb"] * 1024),
                      "manager": EventTail(manager_file, ev["max_read_kb"] * 1024)}
        self.status_files = {"telemetry": status_files.status_path("telemetry", self.run_dir),
                             "watcher": status_files.status_path("watcher", self.run_dir),
                             "manager": status_files.status_path("manager", self.run_dir),
                             "power": paths["power_status_file"]}
        self.collections_root = config["manager"]["gates"]["data"]["root"]
        n = cfg["ntfy"]
        self.ntfy = {"server": n["server"], "topic": n["topic"], "token": n["token"]}
        # Only the token is a secret from ntfy's readers: the server and the topic are where every alert
        # goes anyway, and replacing the topic everywhere would eat a host name that contains it.
        self.secrets = [n["token"]] if n["token"] != "-" else []
        self.unit = cfg["unit_name"] if cfg["unit_name"] != "-" else socket.gethostname()
        self.cond_cfg = dict(cfg["conditions"], sd_free_gb_min=cfg["disk"]["sd_free_gb_min"])
        # Set in setup() from the state file.
        self.limiter = self.conditions = self.disk = None
        self.previous = None
        self.summary_sent = False
        self.power_last_shutdown = None
        self.boot = None
        self.boot_alerts = {}
        self.last_events = {}
        self.state_note = ""
        # The link to ntfy.
        self.next_send_at = 0.0
        self.failures = 0
        self.link = {"state": "idle", "last_ok_uptime_s": None, "last_attempt_uptime_s": None,
                     "last_result": "", "last_status": None}
        self.rejections = {}        # name -> {"count", "accepts": sends accepted before its first refusal}
        self.held = {}              # name -> uptime: refused, not tried again before then
        self.accepts = 0
        self.sending = None         # the send on its way: {"send", "name", "item", "since"}
        self.stale = []             # sends given up on whose thread still runs (a DNS lookup)
        self.unremovable = set()    # sent, but the file could not be deleted
        self.unreadable = {}        # name -> uptime of the next try
        self.inbox_stuck = set()    # inbox files that could not be deleted
        self.inbox_problem = ""
        self._removal_retry_at = 0.0
        self._stale_full_since = None
        self.exit_status = 0
        self.counters = {"queued": 0, "sent": 0, "held_back": 0, "rejected": 0, "events": 0,
                         "voltage_trips_in_power_loss": 0, "inbox": 0}
        self.recent = []
        self.errors = {}
        self._dirty = True
        self._saved_at = None
        self._state_error = ""
        self._scan = None
        self._reads = {}
        self._followed = set()      # event logs read once already by this process

    # ------------------------------------------------------------------ state

    def _load_state(self):
        try:
            with open(self.state_path, "rb") as f:
                raw = f.read(1 << 20)
        except FileNotFoundError:
            return None
        except OSError as e:
            self.state_note = f"state file unreadable ({e.strerror}): started afresh"
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                raise ValueError
            return data
        except (ValueError, RecursionError):
            self.state_note = "state file damaged: started afresh; events before this start were not looked at"
            return None

    def _save_state(self, now, clean_stop=False):
        data = {
            "version": STATE_VERSION, "boot_id": self.boot_id, "boot": self.boot, "uptime_s": round(now, 1),
            "clean_stop": clean_stop, "summary_sent": self.summary_sent, "previous": self.previous,
            "power_last_shutdown": self.power_last_shutdown,
            "alerts": self.boot_alerts, "seq": self.queue.seq,
            "tails": {name: {"position": t.position(), "last": self.last_events.get(name)}
                      for name, t in self.tails.items()},
            "limiter": self.limiter.snapshot(), "conditions": self.conditions.entries,
            "disk_levels": sorted(self.disk.crossed),
        }
        try:
            write_durably(self.state_path, (json.dumps(data) + "\n").encode())
            self._dirty, self._saved_at = False, now
            if self._state_error:
                self.log.info("state file %s is being written again", self.state_path)
            self._state_error = ""
        except (OSError, TypeError, ValueError) as e:
            error = f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"
            if error != self._state_error:
                self.log.error("cannot write the state file %s: %s", self.state_path, error)
            self._state_error = error

    def setup(self):
        now = self._uptime()
        self.queue.setup()
        # Temporary state files of a run that was cut off in the middle of a save.
        directory, base = os.path.split(self.state_path)
        try:
            for name in os.listdir(directory or "."):
                if name.startswith(f".{base}.") and name.endswith(".tmp"):
                    os.unlink(os.path.join(directory, name))
        except OSError:
            pass
        try:
            os.makedirs(self.inbox_dir, mode=0o755, exist_ok=True)
            for name in os.listdir(self.inbox_dir):
                if name.startswith(".") and name.endswith(".tmp"):      # an alertctl killed half way
                    os.unlink(os.path.join(self.inbox_dir, name))
        except OSError as e:
            self.errors["inbox"] = f"{e.strerror or e}"
        st = self._load_state()
        try:
            self._restore(st)
        except Exception as e:      # a state file that parses but makes no sense: never a reason not to run
            self.state_note = f"state file not usable ({type(e).__name__}): started afresh"
            self.previous, self.summary_sent, self.boot = None, False, None
            self.boot_alerts, self.last_events = {}, {}
            self._restore(None)
        if self.state_note:
            self.log.warning("%s", self.state_note)
        self.log.info("queue %s: %d waiting; ntfy %s", self.queue.dir, len(self.queue.names()),
                      "configured" if not self._ntfy_problem() else self._ntfy_problem())
        self._save_state(now)

    def _restore(self, st):
        storm = self.cfg["storm"]
        same = st is not None and st.get("boot_id") == self.boot_id
        if st is None:
            for tail in self.tails.values():
                tail.begin_at_end()     # history is not sent
        else:
            tails = st.get("tails") if isinstance(st.get("tails"), dict) else {}
            for name, tail in self.tails.items():
                saved = tails.get(name) if isinstance(tails.get(name), dict) else {}
                tail.restore(saved.get("position"))
                if not tail.started:
                    tail.begin_at_end()
                if isinstance(saved.get("last"), dict):
                    self.last_events[name] = saved["last"]
            if type(st.get("seq")) is int:
                self.queue.seq = max(self.queue.seq, st["seq"])
            self.power_last_shutdown = st.get("power_last_shutdown")
            boot = st.get("boot") if type(st.get("boot")) is int else None
            counts = {str(k): v for k, v in (st.get("alerts") or {}).items() if str(k).isdigit() and type(v) is int}
            if same:
                previous = st.get("previous")
                self.previous = previous if isinstance(previous, dict) else None
                self.summary_sent = st.get("summary_sent") is True
                self.boot, self.boot_alerts = boot, counts
            else:
                up = st.get("uptime_s")
                self.previous = {"boot": boot, "last_uptime_s": up if type(up) in (int, float) else None,
                                 "clean_stop": st.get("clean_stop") is True, "alerts": counts}
        conditions = st.get("conditions") if same else None
        if isinstance(conditions, dict):
            conditions = {name: e for name, e in conditions.items() if isinstance(e, dict)
                          and type(e.get("since")) in (int, float) and isinstance(e.get("raised"), bool)
                          and isinstance(e.get("title"), str)
                          and (e.get("gone_since") is None or type(e.get("gone_since")) in (int, float))}
        self.limiter = L.Limiter(storm["burst"], storm["window_s"], storm["max_per_hour"],
                                 st.get("limiter") if st else None, same_boot=same)
        self.conditions = L.Conditions(self.cond_cfg["clear_s"], conditions)
        self.disk = L.DiskLevels(self.cfg["disk"]["free_gb_levels"], self.cfg["disk"]["rearm_fraction"],
                                 st.get("disk_levels") if st else None)

    def run(self):
        status = super().run()
        return status or self.exit_status

    def cleanup(self):
        if self.limiter is None:
            return
        if self.sending is not None:
            # The power service forces the power-off 10 s after asking for it: never make it wait.
            # The alert stays queued and is sent after the next start.
            self.sending["send"].cancel()
            self.log.info("stopping: the send of %s was cut off; it stays queued", self.sending["name"])
        now = self._uptime()
        self._save_state(now, clean_stop=True)
        data = self._status(now)
        data["running"] = False
        self.publish(data)

    # ------------------------------------------------------------------ one round

    def _guarded(self, part, fn, *args):
        """Run one part of the round; a failure is logged once and published, and the rest goes on."""
        try:
            result = fn(*args)
            if part in self.errors:
                del self.errors[part]
            return result
        except Exception as e:
            text = f"{type(e).__name__}: {e}"
            if self.errors.get(part) != text:
                self.log.exception("%s failed", part)
            self.errors[part] = text
            return []

    def tick(self):
        now = self._uptime()
        self._reads = {name: status_files.read_status(path, self.config["status"]["stale_after_s"],
                                                      wall=self._wall, uptime=self._uptime, boot_id=self.boot_id)
                       for name, path in self.status_files.items()}
        if self.boot is None:
            self.boot = self._boot_number()
        alerts = []
        alerts += self._guarded("inbox", self._inbox)
        alerts += self._guarded("events", self._events)
        alerts += self._guarded("conditions", self._conditions, now)
        alerts += self._guarded("summary", self._summary, now)
        alerts += [(a, None) for a in self._guarded("storm notes", self.limiter.notes, now)]
        for alert, when in alerts:
            self._guarded("queue", self._enqueue, now, alert, when)
        if self._dirty or self._saved_at is None or now - self._saved_at >= HEARTBEAT_S:
            self._save_state(now)
        self._guarded("send", self._send)
        return self._status(self._uptime())

    def _boot_number(self):
        tel = self._reads.get("telemetry")
        if tel is not None and tel.ok and type(tel.data.get("boot_count")) is int:
            return tel.data["boot_count"]
        # Read only: the counter is raised by the services that name files with it.
        count, boot_id = bootcount._read(os.path.join(self.state_dir, bootcount.FILE_NAME))
        return count if count is not None and boot_id == self.boot_id else None

    def _power_loss(self):
        p = self._reads.get("power")
        return bool(p is not None and p.ok and (p.data.get("power_present") is False
                                                 or p.data.get("state") not in ("normal", None)
                                                 or p.data.get("radar_stop_requested") is True))

    # ---- sources

    def _inbox(self):
        out = []
        try:
            names = sorted(n for n in os.listdir(self.inbox_dir) if n.endswith(".json") and not n.startswith("."))
        except FileNotFoundError:
            return out
        self.inbox_stuck &= set(names)
        if not self.inbox_stuck:
            self.inbox_problem = ""
        for name in names[:INBOX_PER_ROUND]:
            path = os.path.join(self.inbox_dir, name)
            try:
                with open(path, "rb") as f:
                    request = json.loads(f.read(INBOX_MAX_BYTES))
            except (OSError, ValueError, RecursionError):
                request = None
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                # Queued only once taken out of the inbox: one that cannot be deleted (a directory made
                # by root) would otherwise be queued again every round. Tried again every round.
                if name not in self.inbox_stuck:
                    self.inbox_problem = f"cannot delete {path}: {e.strerror or e}; the request waits"
                    self.log.error("%s", self.inbox_problem)
                self.inbox_stuck.add(name)
                continue
            self.inbox_stuck.discard(name)
            if not isinstance(request, dict):
                continue
            self.counters["inbox"] += 1
            priority = L.BY_NAME.get(request.get("priority"), L.DEFAULT)
            test_id = str(request.get("id") or "")[:40]
            text = str(request.get("text") or "")
            alert = L.Alert("test", priority, f"test alert ({L.PRIORITY_NAMES[priority]})",
                            f"Test alert from alertctl, id {test_id}." + (f" {text}" if text else ""),
                            limited=False, tags=["test"])
            out.append((alert, {"test_id": test_id}))
        return out

    def _events(self):
        out = []
        power_loss = self._power_loss()
        for name, tail in self.tails.items():
            # The logs carry no boot ID. What was appended after this process first read a log was
            # written in this boot (the writers of the boot before are gone); what was there already
            # may be from either. Only between two such events is a start without a clean stop a restart.
            live = name in self._followed
            self._followed.add(name)
            for event in tail.read():
                self.counters["events"] += 1
                up = event.get("uptime_s")
                previous = self.last_events.get(name)
                if not (live and isinstance(previous, dict) and previous.get("boot_id") == self.boot_id):
                    previous = None
                try:
                    if name == "watcher":
                        alert = L.watcher_alert(event, previous, power_loss)
                        if (alert is None and power_loss and event.get("event") == "trip"
                                and event.get("condition") == "voltage"):
                            self.counters["voltage_trips_in_power_loss"] += 1
                    else:
                        alert = L.manager_alert(event, previous, self.cfg["error_rate_warn"])
                except Exception:
                    self.log.exception("cannot judge an event of %s; skipped", tail.path)
                    alert = None
                self.last_events[name] = {"event": str(event.get("event")), "uptime_s": up,
                                          "boot_id": self.boot_id if live else None}
                self._dirty = True
                if alert is not None:
                    out.append((alert, {"time_utc": event.get("time_utc"), "uptime_s": event.get("uptime_s")}))
        return out

    def _conditions(self, now):
        states = L.condition_states(self._reads, self.cond_cfg, self.conditions.entries)
        before = json.dumps(self.conditions.entries, sort_keys=True)
        out = [(a, None) for a in self.conditions.update(now, states)]
        tel = self._reads["telemetry"]
        if tel.ok:
            free = L._get(tel.data, "sources", "pi", "values", "nvme_free_gb")
            crossed = set(self.disk.crossed)
            out += [(a, None) for a in self.disk.update(free)]
            self._dirty |= crossed != self.disk.crossed
        self._dirty |= before != json.dumps(self.conditions.entries, sort_keys=True)
        return out

    # ---- the boot summary

    def _summary(self, now):
        if self.summary_sent or now < self.cfg["summary_after_s"]:
            return []
        if self._scan is None:
            result = {}

            def scan():
                try:
                    result["value"] = scan_collections(self.collections_root)
                except OSError as e:
                    result["value"] = {"error": f"cannot list {self.collections_root} ({e.strerror or e})"}
            thread = threading.Thread(target=scan, daemon=True, name="alerts-scan")
            self._scan = {"thread": thread, "result": result, "started": now}
            thread.start()
        scan = self._scan
        if "value" not in scan["result"] and now - scan["started"] < SCAN_TIMEOUT_S:
            return []           # wait for the drive, at most SCAN_TIMEOUT_S
        collections = scan["result"].get("value") or {
            "error": f"the data drive did not answer within {SCAN_TIMEOUT_S:g} s"}
        facts = self._summary_facts(now, collections)
        self.summary_sent = True
        self._dirty = True
        return [(L.boot_summary(facts), None)]

    def _summary_facts(self, now, collections):
        r = self._reads
        tel, wat, man, pwr = r["telemetry"], r["watcher"], r["manager"], r["power"]
        values = L._get(tel.data, "sources", "pi", "values") if tel.ok else None
        previous = dict(self.previous) if self.previous else None
        last_shutdown = L._get(pwr.data, "guard", "last_shutdown") if pwr.data else None
        if previous is not None and isinstance(last_shutdown, dict) and last_shutdown != self.power_last_shutdown:
            previous["power_shutdown"] = str(last_shutdown.get("why") or "no reason recorded")
        if isinstance(last_shutdown, dict):
            self.power_last_shutdown = last_shutdown
        services = []
        if man.ok:
            d = man.data
            line = f"Radar manager: {d.get('state')}"
            if isinstance(d.get("fault"), dict):
                line += f", FAULT {d['fault'].get('kind')}: {d['fault'].get('reason')}"
            elif isinstance(d.get("last_fault"), dict):
                line += f" (last fault {d['last_fault'].get('kind')})"
            if d.get("not_ready"):
                line += "; not ready: " + "; ".join(str(x) for x in d["not_ready"])
            services.append(line + ".")
        else:
            services.append(f"Radar manager: not running ({man.state}).")
        if wat.ok:
            services.append("Safety watcher: " + ("ok." if wat.data.get("ok") is True else
                                                   f"radar may not run: {wat.data.get('reason')}."))
        else:
            services.append(f"Safety watcher: NOT RUNNING ({wat.state}).")
        if tel.ok:
            problems = tel.data.get("problems") or []
            services.append("Telemetry: " + (tel.data.get("state") or "?")
                            + (": " + ", ".join(map(str, problems)) if problems else "") + ".")
        else:
            services.append(f"Telemetry: NOT RUNNING ({tel.state}).")
        if pwr.ok:
            d = pwr.data
            warnings = d.get("warnings") if isinstance(d.get("warnings"), dict) else {}
            guard = d.get("guard") if isinstance(d.get("guard"), dict) else {}
            services.append(f"Power service: {d.get('state')}, {d.get('mode')}"
                            + (f", early shutdowns {guard.get('early_shutdowns')}" if guard.get("early_shutdowns") else "")
                            + (f"; warnings: {', '.join(sorted(warnings))}" if warnings else "") + ".")
        else:
            services.append(f"Power service: NOT RUNNING ({pwr.state}).")
        config_warnings = sum(len(x.data.get("config_warnings") or []) for x in (tel, wat, man) if x.ok)
        config_warnings += len(self.config_warnings)
        if config_warnings:
            services.append(f"Config warnings: {config_warnings} (setup/check.py lists them).")
        active = [e["title"] for e in self.conditions.active().values()]
        if self.disk.crossed:
            active.append(f"data drive below {min(self.disk.crossed):g} GB")
        return {"boot": self.boot, "uptime_s": now,
                "clock_synced": L._get(tel.data, "clock", "synced") if tel.ok else None,
                "previous": previous,
                "disk": {"nvme_free_gb": (values or {}).get("nvme_free_gb"), "sd_free_gb": (values or {}).get("sd_free_gb")},
                "collections": collections, "services": services, "active": active,
                "queue": {"length": len(self.queue.names()), "dropped": self.queue.dropped,
                          "held": self.limiter.held_total}}

    # ---- queueing

    def _clock_set(self):
        tel = self._reads.get("telemetry")
        return L._get(tel.data, "clock", "synced") if tel is not None and tel.ok else None

    def _enqueue(self, now, alert, when):
        when = when or {}
        try:
            admitted = self.limiter.admit(now, alert)
        except Exception:
            self.log.exception("storm limit failed; the alert goes out")
            admitted = True         # better one alert too many than one lost
        if not admitted:
            if alert.clears:
                return          # its alert was held back: no "over" either
            self.counters["held_back"] += 1
            self._dirty = True
            self.log.info("held back (storm limit): %s", alert.title)
            return
        wall = when.get("time_utc") if isinstance(when.get("time_utc"), str) else None
        if wall is None:
            wall = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._wall()))
        up = when.get("uptime_s") if type(when.get("uptime_s")) in (int, float) else now
        clock = {0: ", clock not set"}.get(self._clock_set(), "")
        footer = f"\n-- {self.unit}, {wall.replace('T', ' ')}{clock}, uptime {L._duration(up)}"
        if "uptime_s" not in when and self.boot is not None:
            footer += f", boot {self.boot}"
        limit = self.cfg["message_max_chars"]
        footer = L.scrub(footer, self.secrets)
        message = L.clip(L.scrub(alert.message, self.secrets), max(limit - len(footer), 100))
        while len((message + footer).encode()) > MESSAGE_MAX_BYTES:
            message = L.clip(message, len(message) - 100)
        message += footer
        title = L.clip(L.scrub(f"{self.unit}: {alert.title}", self.secrets), self.cfg["title_max_chars"])
        item = {"title": title, "message": message, "key": alert.key,
                "tags": [L.PRIORITY_NAMES.get(alert.priority, "default")] + list(alert.tags),
                "raised_uptime_s": round(now, 1), "boot_id": self.boot_id}
        if when.get("test_id"):
            item["test_id"] = when["test_id"]
        name = self.queue.add(item, alert.priority)
        if not name:
            self.log.error("alert not queued (%s): %s", self.queue.error or "queue full", title)
            return
        self.counters["queued"] += 1
        key = str(alert.priority)
        self.boot_alerts[key] = self.boot_alerts.get(key, 0) + 1
        self._dirty = True
        self.log.log(logging.WARNING if alert.priority >= L.HIGH else logging.INFO, "queued %s (%s): %s", name,
                     L.PRIORITY_NAMES.get(alert.priority), title)
        self._remember(name, item, alert.priority, "queued")

    def _remember(self, name, item, priority, result):
        entry = next((e for e in self.recent if e["id"] == name[:-5]), None)
        if entry is None:
            entry = {"id": name[:-5], "title": item.get("title"), "priority": L.PRIORITY_NAMES.get(priority)}
            if item.get("test_id"):
                entry["test_id"] = item["test_id"]
            self.recent.append(entry)
            del self.recent[:-RECENT]
        entry.update(result=result, uptime_s=round(self._uptime(), 1))

    # ---- sending

    def _ntfy_problem(self):
        problem = ntfy.server_problem(self.ntfy["server"])
        if not problem and self.ntfy["topic"] == "-":
            problem = "no ntfy topic set (alerts.ntfy.topic in /etc/radar-runner/secrets.yaml)"
        return problem

    def _send(self):
        """Start, look at and finish sends; never waits on the network for more than SEND_ROUND_S."""
        n = self.cfg["ntfy"]
        round_end = time.monotonic() + SEND_ROUND_S
        self._reap()
        for _ in range(max(1, n["max_per_round"])):
            if self.sending is None and not self._start_next():
                return
            s = self.sending
            while not s["send"].done.is_set():
                if self._stop.is_set():
                    return
                if time.monotonic() - s["since"] > n["timeout_s"]:
                    s["send"].cancel()      # ends it at once, unless it is in a DNS lookup
                    self.stale.append(s)
                    self.sending = None
                    self._finished(s, ntfy.Result(ntfy.RETRY, text=f"no complete answer within {n['timeout_s']:g} s"))
                    return
                if time.monotonic() >= round_end:
                    return          # still on its way: looked at again next round
                s["send"].done.wait(0.05)
            self.sending = None
            if not self._finished(s, s["send"].result):
                return

    def _start_next(self):
        if self._stop.is_set() or self._uptime() < self.next_send_at or len(self.stale) >= MAX_STALE:
            return False
        now = self._uptime()            # holds and retries count in uptime, like every other wait here
        # Alerts never refused first (by priority and age); then the refused ones, the one refused
        # longest ago first: a refused alert never holds up another one.
        def order(entry):
            r = self.rejections.get(entry[2])
            return (0, 0.0) if r is None else (1, r["at"])
        for _, _, name in sorted(self.queue.names(), key=order):
            if name in self.unremovable or self.held.get(name, 0.0) > now or self.unreadable.get(name, 0.0) > now:
                continue
            item = self.queue.load(name)
            if item is None:
                if self.queue.load_error:
                    # One file that cannot be read (EIO) must not hold up the others.
                    if name not in self.unreadable:
                        self.log.error("cannot read %s: %s; the others are sent first", name, self.queue.load_error)
                    self.unreadable[name] = now + UNREADABLE_RETRY_S
                continue
            self.unreadable.pop(name, None)
            item["title"] = L.scrub(item["title"], self.secrets)
            item["message"] = L.scrub(item["message"], self.secrets)
            self.link["last_attempt_uptime_s"] = round(self._uptime(), 1)
            send = ntfy.Send(self.ntfy["server"], self.ntfy["topic"], self.ntfy["token"], item,
                             min(self.cfg["ntfy"]["timeout_s"], ntfy.READ_TIMEOUT_S)).start()
            self.sending = {"send": send, "name": name, "item": item, "since": time.monotonic()}
            return True
        if self.failures == 0 and self.link["state"] != "unconfigured":
            self.link["state"] = "idle"
        return False

    def _reap(self):
        """Sends given up on that have ended since (a DNS lookup); and files that could not be deleted."""
        now = self._uptime()
        queued = {name for _, _, name in self.queue.names()}
        for records in (self.held, self.rejections, self.unreadable):
            for name in [n for n in records if n not in queued]:
                del records[name]
        self.unremovable &= queued
        if len(self.stale) < MAX_STALE:
            self._stale_full_since = None
        elif self._stale_full_since is None:
            self._stale_full_since = now
        elif now - self._stale_full_since >= STALE_EXIT_S and not self._stop.is_set():
            # Threads stuck in DNS lookups that never return: nothing, urgent alerts included, can be
            # sent, while the watchdog is still fed. A new process starts afresh; the queue is on disk.
            self.log.error("%d sends have been stuck in DNS lookups for %.0f s: exiting so that systemd "
                           "restarts the service", len(self.stale), now - self._stale_full_since)
            self.exit_status = 1
            self.request_stop()
            return
        for s in [s for s in self.stale if s["send"].done.is_set()]:
            self.stale.remove(s)
            if s["send"].result is not None and s["send"].result.kind == ntfy.SENT:
                self.log.info("%s went through after all", s["name"])
                self._sent(s["name"], s["item"])
        now = self._uptime()
        if self.unremovable and now >= self._removal_retry_at:
            self._removal_retry_at = now + UNREMOVABLE_RETRY_S
            for name in list(self.unremovable):
                if self.queue.remove(name):
                    self.unremovable.discard(name)
            if not self.unremovable:
                self.log.info("the sent alerts that could not be deleted are deleted now")

    def _sent(self, name, item):
        self.accepts += 1
        self.counters["sent"] += 1
        self.failures = 0
        self.link.update(state="ok", last_ok_uptime_s=round(self._uptime(), 1))
        self.rejections.pop(name, None)
        self.held.pop(name, None)
        self._remember(name, item, item["priority"], "sent")
        self.log.info("sent %s: %s", name, item["title"])
        if not self.queue.remove(name):
            # Sent, but still in the queue (a card gone read-only): never sent again from here.
            if not self.unremovable:
                self.log.error("%s; sent alerts are kept out of sending until they can be deleted", self.queue.error)
            self.unremovable.add(name)
            self._removal_retry_at = self._uptime() + UNREMOVABLE_RETRY_S

    def _finished(self, s, result):
        """Act on the result of one send. True if the next alert may be sent at once."""
        n = self.cfg["ntfy"]
        name, item = s["name"], s["item"]
        now = self._uptime()
        text = L.scrub(result.text, self.secrets)
        self.link.update(last_result=text, last_status=result.status)
        if result.kind == ntfy.SENT:
            self._sent(name, item)
            return True
        if result.kind == ntfy.UNCONFIGURED:
            self.link["state"] = "unconfigured"
            self.next_send_at = now + 60.0
            return False
        if result.kind == ntfy.REJECTED:
            # A refusal counts against this alert only if another alert went through since its previous
            # refusal: refusals while the server refuses everything say nothing about this alert.
            r = self.rejections.get(name)
            server_works = r is not None and self.accepts > r["accepts"]
            if r is None:
                r = self.rejections[name] = {"count": 0, "accepts": self.accepts}
            if server_works:
                r["count"] += 1
            r["accepts"], r["at"] = self.accepts, now
            self.log.error("ntfy refused %s (%d time(s) while others went through): %s", name, r["count"], text)
            if r["count"] >= REJECT_LIMIT and server_works:
                self.queue.remove(name)
                self.rejections.pop(name, None)
                self.held.pop(name, None)
                self.counters["rejected"] += 1
                self._remember(name, item, item["priority"], f"dropped: refused by the server ({text})")
                return True
            self.held[name] = now + n["retry_max_s"]
            self._remember(name, item, item["priority"], f"refused, kept: {text}")
            if server_works:
                return True
            # Nothing has gone through since: more likely the settings (topic, server) than this alert.
        self.failures += 1
        wait = min(n["retry_max_s"], n["retry_s"] * 2 ** min(self.failures - 1, 20))
        if result.retry_after_s is not None:
            wait = max(wait, min(result.retry_after_s, 3600.0))
        self.next_send_at = now + wait
        if self.link["state"] != "failing":
            self.log.warning("cannot send to ntfy: %s; alerts stay queued", text)
        self.link["state"] = "failing"
        self._remember(name, item, item["priority"], f"waiting: {text}")
        return False

    # ---- status

    def _status(self, now):
        counts = self.queue.counts()
        problem = self._ntfy_problem()
        return {
            "running": True,
            "ntfy": {"configured": not problem, "problem": problem},
            "link": dict(self.link, failures_in_a_row=self.failures,
                         next_attempt_in_s=round(max(self.next_send_at - now, 0.0), 1) if self.failures else None),
            "sending": ({"id": self.sending["name"][:-5], "for_s": round(time.monotonic() - self.sending["since"], 1)}
                        if self.sending else None),
            "stale_sends": len(self.stale),
            "inbox": {"dir": self.inbox_dir, "problem": self.inbox_problem},
            "sent_not_deleted": sorted(n[:-5] for n in self.unremovable),
            "unreadable": sorted(n[:-5] for n in self.unreadable),
            "refused_kept": {n[:-5]: r["count"] for n, r in self.rejections.items()},
            "queue": {"dir": self.queue.dir, "length": sum(counts.values()),
                      "by_priority": {L.PRIORITY_NAMES.get(p, str(p)): c for p, c in sorted(counts.items(), reverse=True)},
                      "max": self.queue.max_items, "dropped": self.queue.dropped, "damaged": self.queue.damaged,
                      "error": self.queue.error},
            "counters": dict(self.counters, held_back_total=self.limiter.held_total if self.limiter else 0),
            "boot": self.boot,
            "summary_sent": self.summary_sent,
            "conditions": {name: {"since_uptime_s": round(e["since"], 1), "raised": e["raised"], "title": e["title"]}
                           for name, e in (self.conditions.entries.items() if self.conditions else ())},
            "disk_levels_crossed": sorted(self.disk.crossed) if self.disk else [],
            "events": {name: {"file": t.path, "position": t.position(), "lines": t.lines, "skipped": t.skipped,
                              "lost": t.lost, "error": t.error} for name, t in self.tails.items()},
            "inputs": {name: r.state for name, r in self._reads.items()},
            "recent": list(self.recent),
            "errors": dict(self.errors),
            "state_file": {"path": self.state_path, "error": self._state_error, "note": self.state_note},
        }
