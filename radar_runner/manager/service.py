"""The radar manager service: connects the state machine to the outside world.

Everything that happens elsewhere (a button press, a radarctl command, a
report of the runner, the automatic source) is put in one queue and applied by
the service loop, so the state machine is only ever touched from one thread.

    button / radarctl / runner / auto  ->  queue  ->  tick()  ->  logic  ->  runner, LEDs,
                                                                             status file, event log
"""
import logging
import os
import queue
import threading
import time

from radar_runner.common.service import Service

from . import ampsafe, control, gates, logic as L, panel as panel_module
from .runner import Runner

REPLY_TIMEOUT_S = 5.0


class Manager(Service):
    """Radar manager: starts and stops the radar on triggers, behind safety gates."""
    name = "manager"

    def __init__(self, config, *, panel=None, runner=None, gate_list=None, **kw):
        super().__init__(config, **kw)
        self.cfg = config["manager"]
        self.interval_s = max(0.01, self.cfg["tick_s"])
        self.max_failures = 100                 # at 10 rounds a second: about 10 s of errors
        self._events_in = queue.Queue()
        self._given = {"panel": panel, "runner": runner, "gates": gate_list}
        self.logic = L.ManagerLogic(L.LogicConfig.from_config(self.cfg))
        self.overrides = gates.Overrides(config["paths"]["state_dir"])
        self.sdr_flag = ampsafe.SdrStateFlag(config["paths"]["state_dir"])
        self.socket_path = os.path.join(config["paths"]["run_dir"], control.SOCKET_NAME)
        self.event_log = control.EventLog(
            self.cfg["events"]["file"] or os.path.join(config["paths"]["state_dir"], "manager-events.jsonl"),
            self.cfg["events"]["max_mb"] * 1e6)
        self._next_gates = 0.0
        self._next_status = 0.0
        self._next_panel_retry = 0.0
        self._status = {"state": L.NOT_READY, "idle": True}
        self._event_error = ""
        self.server = None

    # ---------------------------------------------------------------- setup

    def setup(self):
        self.gate_list = self._given["gates"] or gates.build(self.config, self.overrides)
        self.runner = self._given["runner"] or Runner(self.config, self._from_runner)
        if self._given["panel"] is not None:
            self.panel = self._given["panel"]
        elif self.cfg["panel"]["enabled"]:
            self.panel = panel_module.Panel(self.cfg["panel"], self.press)
        else:
            self.panel = panel_module.NoPanel()
        error = self.panel.open()
        if error:
            self.log.error("front panel not available (%s): running without button and LEDs", error)
        self.server = control.ControlServer(self.socket_path, self._handle_request)
        self.server.open()                      # an error here ends the process: systemd restarts it
        self._record({"event": "manager_started", "pid": os.getpid(),
                      "sdr_state_unknown": self.sdr_flag.is_set(),
                      "override_no_amp_telemetry": self.overrides.is_set()})
        if self.overrides.is_set():
            self.log.warning("OVERRIDE is set: the radar may run without amplifier telemetry")
        if self.sdr_flag.is_set():
            self.log.warning("the SDR GPIO state was not confirmed safe when the last radar run ended")

    # ------------------------------------------------- inputs from other threads

    def press(self):
        """A button press (from the panel's thread)."""
        self._events_in.put(("press",))

    def report_auto(self, state, label="gps"):
        """The automatic source (stage 6: the GPS motion trigger) reports "moving", "stationary" or "unknown"."""
        self._events_in.put(("auto", state, label))

    def _from_runner(self, name, data):
        self._events_in.put(("runner", name, data))

    def _handle_request(self, request):
        """Called in the socket thread: hand the request to the loop and wait for its answer."""
        if request.get("cmd") == "status":
            return {"ok": True, "message": "", "status": self._status}
        slot = {"done": threading.Event(), "reply": None}
        self._events_in.put(("request", request, slot))
        if not slot["done"].wait(REPLY_TIMEOUT_S):
            return {"ok": False, "message": "the manager did not answer in time", "status": self._status}
        return slot["reply"]

    # ------------------------------------------------------------------ loop

    def tick(self):
        now = self._monotonic()
        changed = False
        replies = []
        while True:
            try:
                item = self._events_in.get_nowait()
            except queue.Empty:
                break
            changed = True
            kind = item[0]
            if kind == "press":
                self._check_gates(now)          # decide on what is true now, not half a second ago
                ok, message = self.logic.toggle(now, L.BUTTON)
                self.log.info("button press: %s", message)
            elif kind == "auto":
                self.logic.auto(now, item[1], item[2])
            elif kind == "runner":
                self._runner_event(now, item[1], item[2])
            elif kind == "request":
                replies.append((item[2], self._request(now, item[1])))
            self._act()
        if now >= self._next_gates:
            self._check_gates(now)
        else:
            self.logic.tick(now)
        changed = self._act() or changed

        leds = L.LEDS[self.logic.state]
        if self.panel.shown != leds:
            self.panel.show(*leds)
        if self.panel.error and self.cfg["panel"]["enabled"] and now >= self._next_panel_retry:
            self._next_panel_retry = now + 30.0
            if not self.panel.snapshot().get("available"):
                self.panel.open()

        if changed or replies or now >= self._next_status:
            self._next_status = now + self.cfg["status_interval_s"]
            self._status = self._snapshot()
            for slot, reply in replies:
                reply["status"] = self._status
                slot["reply"] = reply
                slot["done"].set()
            return self._status
        return None

    def _check_gates(self, now):
        self._next_gates = now + self.cfg["gate_interval_s"]
        results = gates.evaluate(self.gate_list)
        if results.get("telemetry") is not None and results["telemetry"].kind == "override" and self.runner.busy:
            self.runner.add_flag("no_amp_telemetry")
        self.logic.gates(now, results)

    def _request(self, now, request):
        cmd = request.get("cmd")
        if cmd in ("start", "toggle", "stop"):
            self._check_gates(now)
        if cmd == "start":
            ok, message = self.logic.start(now, L.COMMAND)
        elif cmd == "toggle":
            ok, message = self.logic.toggle(now, L.COMMAND)
        elif cmd == "stop" and request.get("interrupt"):
            ok, message = self.logic.interrupt(now, L.COMMAND, "interrupted by command (power service stop hook)")
        elif cmd == "stop":
            ok, message = self.logic.stop(now, L.COMMAND)
        elif cmd == "clear-fault":
            ok, message = self.logic.clear_fault(now, L.COMMAND)
        elif cmd == "override" and request.get("name") == "no-amp-telemetry" and type(request.get("set")) is bool:
            error = self.overrides.set(request["set"], f"set with radarctl at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
            ok = not error
            message = error or ("override set: the radar may run WITHOUT amplifier telemetry"
                                if request["set"] else "override cleared")
            self._record({"event": "override", "name": "no_amp_telemetry", "set": request["set"], "error": error})
            self._check_gates(now)
        else:
            ok, message = False, f"unknown command {cmd!r}"
        return {"ok": ok, "message": message}

    def _runner_event(self, now, name, data):
        if name == "collection":
            self.logic.collection_created(now, data.get("name"))
            self._record({"event": "collection", **data})
        elif name == "started":
            self.logic.started(now)
        elif name == "ended":
            self._record({"event": "ended", **data})
            self.logic.ended(now, data)

    def _act(self):
        """Carry out what the logic decided. Returns True if there was anything."""
        actions = self.logic.drain()
        for action in actions:
            if action.kind == "start":
                flags = []
                telemetry = (self.logic.results or {}).get("telemetry")
                if telemetry is not None and telemetry.kind == "override":
                    flags.append("no_amp_telemetry")
                try:
                    self.runner.start(action.data, flags)
                except RuntimeError as e:       # cannot happen: the logic starts only after "ended"
                    self.log.error("cannot start: %s", e)
                    self._events_in.put(("runner", "ended", {"requested": False, "failure": str(e),
                                                             "kind": "internal", "amp_safe": None}))
            elif action.kind == "stop":
                self.runner.stop(action.data.get("reason", ""))
            elif action.kind == "event":
                self._record(action.data)
        return bool(actions)

    def _record(self, event):
        name = event.get("event")
        if name == "fault":
            self.log.error("FAULT (%s): %s", event.get("kind"), event.get("reason"))
        elif name == "state":
            self.log.info("state %s -> %s%s", event.get("old"), event.get("new"),
                          f" ({event['reason']})" if event.get("reason") else "")
        elif name == "gate":
            (self.log.info if event.get("ok") else self.log.warning)(
                "gate %s: %s", event.get("gate"), "ok" if event.get("ok") else event.get("reason"))
        elif name == "ended" and event.get("failure"):
            self.log.warning("run ended: %s", event.get("failure"))
        elif name != "ended":
            self.log.info("%s", {k: v for k, v in event.items()})
        error = self.event_log.write(event)
        if error != self._event_error:
            if error:
                self.log.error("cannot write the event log %s: %s", self.event_log.path, error)
            self._event_error = error

    def _snapshot(self):
        snap = self.logic.snapshot()
        radar = self.runner.snapshot()
        snap.update({
            # True when no radar process exists and the amplifier has been made safe: `radarctl stop --wait`
            "idle": self.logic.phase == L.IDLE and not radar.get("active"),
            "radar": radar,
            "overrides": self.overrides.snapshot(),
            "sdr_state_unknown": self.sdr_flag.is_set(),
            "amp_safe_policy": self.cfg["amp_safe"]["policy"],
            "panel": self.panel.snapshot(),
            "socket": self.socket_path,
            "events": {"file": self.event_log.path, "written": self.event_log.written,
                       "error": self.event_log.error},
        })
        if self.config_warnings:
            snap["config_warnings"] = list(self.config_warnings)
        return snap

    # --------------------------------------------------------------- cleanup

    def cleanup(self):
        """Stop the radar, make the amplifier safe, switch the LEDs off."""
        now = self._monotonic()
        self.logic.shutdown(now)
        self._act()
        self.panel.show(*L.LEDS[L.STOPPING] if self.runner.busy else (L.OFF, L.OFF))
        orca, amp = self.cfg["orca"], self.cfg["amp_safe"]
        limit = orca["stop_timeout_s"] + amp["timeout_s"] + amp["confirm_timeout_s"] + 15.0
        deadline = time.monotonic() + limit
        done = False
        while not done and time.monotonic() < deadline:
            self.notifier.watchdog()
            done = self.runner.shutdown(0.5)
        if not done:
            self.log.error("the radar run did not end within %.0f s of the stop request", limit)
        while True:                             # the runner's last reports, for the event log
            try:
                item = self._events_in.get_nowait()
            except queue.Empty:
                break
            if item[0] == "runner":
                self._runner_event(time.monotonic(), item[1], item[2])
            elif item[0] == "request":
                item[2]["reply"] = {"ok": False, "message": "the manager is stopping", "status": self._status}
                item[2]["done"].set()
        self._act()
        self._record({"event": "manager_stopped", "radar_stopped": done})
        if self.server is not None:
            self.server.close()
        self.panel.close()
        final = self._snapshot()
        final["state"] = "stopped"
        self.publish(final)
        if not done or (self.logic.fault or {}).get("kind") == "amp_unsafe":
            raise RuntimeError("the amplifier was not confirmed safe at exit")     # exit status 1
