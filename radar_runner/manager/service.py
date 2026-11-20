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

REPLY_TIMEOUT_S = 1.5       # shorter than radarctl waits for an answer (2 s)


class Manager(Service):
    """Radar manager: starts and stops the radar on triggers, behind safety gates."""
    name = "manager"

    def __init__(self, config, *, panel=None, runner=None, gate_list=None, minimums=None, **kw):
        super().__init__(config, **kw)
        self.cfg = config["manager"]
        self.interval_s = max(0.01, self.cfg["tick_s"])
        self.max_failures = 100                 # at 10 rounds a second: about 10 s of errors
        self._events_in = queue.Queue()
        self._given = {"panel": panel, "runner": runner, "gates": gate_list}
        logic_cfg, refused = L.LogicConfig.from_config(self.cfg, minimums)
        self.config_warnings += refused
        self.logic = L.ManagerLogic(logic_cfg)
        self.overrides = gates.Overrides(config["paths"]["state_dir"])
        self.sdr_state = ampsafe.SdrState(config["paths"]["state_dir"],
                                          ampsafe.tool_lock_path(self.cfg["amp_safe"]))
        self.amp_telemetry = ampsafe.AmpTelemetry(config)
        self.sdr_gate = gates.SdrGate(self.cfg["gates"]["sdr"])
        self.enabled_idle = ""                  # telemetry shows enabled current with no radar: the text
        self._next_tool = 0.0
        self.socket_path = os.path.join(config["paths"]["run_dir"], control.SOCKET_NAME)
        self.event_log = control.EventLog(
            os.path.join(config["paths"]["state_dir"], "manager-events.jsonl")
            if self.cfg["events"]["file"] in ("-", "") else self.cfg["events"]["file"],
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
        # One record object for both: what the runner learns (the tool said OK
        # but the record was not written) the idle rule here must know.
        self.runner = self._given["runner"] or Runner(self.config, self._from_runner, sdr_state=self.sdr_state)
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
                      "sdr_state": self.sdr_state.get(), "sdr_state_why": self.sdr_state.why(),
                      "override_no_amp_telemetry": self.overrides.is_set()})
        if self.overrides.is_set():
            self.log.warning("OVERRIDE is set: the radar may run without amplifier telemetry")

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
            # radar_gone is asked for now, not taken from the last snapshot:
            # `radarctl stop --wait` must not wait for the loop's next round.
            return {"ok": True, "message": "", "status": dict(self._status, radar_gone=self.runner.radar_gone())}
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
            self._keep_sdr_safe(now)
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
        self.runner.hurry = self.logic.power_fail or self.logic.closing

    def _sdr_presence(self):
        found = self.sdr_gate.find()
        if not found:
            return ampsafe.ABSENT
        boot = self.cfg["gates"]["sdr"]["bootloader_product"]
        return ampsafe.BOOTLOADER if all(product == boot for _, _, product in found) else ampsafe.LOADED

    def _keep_sdr_safe(self, now):
        """With no radar process: mark the SDR safe, or run the tool, as ampsafe.idle_need says.

        This covers a manager that starts with the record `unknown` or with no
        record, and a tool run that did not succeed (tried again every
        retry_s). The tool runs on the runner's thread. Enabled current while
        the record says `safe` is only shown: the safety watcher acts on it.
        """
        cfg = self.cfg["amp_safe"]
        if self.logic.phase != L.IDLE or self.logic.closing or self.runner.busy:
            self.enabled_idle = ""
            return
        if self.logic.power_fail or self.amp_telemetry.rail_absent():
            # Power is going or the 28 V rail is off: the amplifier cannot be enabled, the
            # current says nothing, and the power service has its own hook. Leave the record.
            self.enabled_idle = ""
            return
        # Shutdown current counts only in a reading newer than the last radar or tool run.
        reading, text = self.amp_telemetry.now(self.runner.last_activity_uptime)
        self.enabled_idle = text if reading is False else ""        # True, PENDING and None are not "enabled"
        action, why = ampsafe.idle_need(self.sdr_state.get(), self._sdr_presence(), reading)
        if action == "mark_safe":
            done, error = self.sdr_state.mark_safe(why)     # not done: a tool run is in progress elsewhere
            if done:
                self._record({"event": "sdr_state", "state": ampsafe.SAFE, "why": why, "error": error})
        elif action == "run" and now >= self._next_tool:
            self._next_tool = now + cfg["retry_s"]
            if self.runner.make_safe(why):
                self._record({"event": "amp_safe_run", "why": why})
        # An amp_unsafe fault is over when the record says safe again: a later run
        # of the tool that succeeded, or a reading that qualified above.
        if (self.logic.fault or {}).get("kind") == L.AMP_UNSAFE and self.sdr_state.known_safe:
            self.logic.amp_safe_again(now, self.sdr_state.why())

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
        elif name == "safe_done":
            self._record({"event": "amp_safe_done", **data})
            if not data.get("ok"):
                self.log.error("the amplifier was not made safe: %s", data.get("detail"))
                if data.get("status") == ampsafe.ASSUME_ENABLED:
                    self.logic.amp_unsafe(now, data.get("detail", ""))

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
                self.runner.stop(action.data.get("reason", ""), urgent=bool(action.data.get("urgent")),
                                 power=bool(action.data.get("power")))
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
            "idle": self.logic.phase == L.IDLE and not radar.get("active") and not self.runner.busy,
            # True when no radar process exists and none is about to be started: `radarctl stop --wait`
            "radar_gone": self.runner.radar_gone(),
            "radar": radar,
            "overrides": self.overrides.snapshot(),
            "sdr_state": self.sdr_state.get(),              # "safe" or "unknown" (ampsafe.py)
            "sdr_state_why": self.sdr_state.why(),
            # "" or why the record could not be written; "safe" is then only in this manager's memory
            "sdr_state_problem": self.sdr_state.problem,
            "making_amp_safe": bool(getattr(self.runner, "making_safe", False)),
            # "" or the reading: enabled current with no radar process. The safety watcher acts on it.
            "amp_enabled_while_idle": self.enabled_idle,
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
        limit = (orca["urgent_stop_budget_s"] + amp["confirm_timeout_s"]
                 + amp["timeout_s"] * (1 + max(0, amp["assume_enabled_retries"])) + 15.0)
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
        # The radar is stopped by now: wait (at most 1 s) for the last events to reach the drive.
        if not self.event_log.close(1.0):
            self.log.warning("the last events may not have reached the drive: %s",
                             self.event_log.sync_error or "the sync did not finish within 1 s")
        if self.server is not None:
            self.server.close()
        self.panel.close()
        final = self._snapshot()
        final["state"] = "stopped"
        self.publish(final)
        if not done or (self.logic.fault or {}).get("kind") == "amp_unsafe":
            raise RuntimeError("the amplifier was not confirmed safe at exit")     # exit status 1
