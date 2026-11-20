#!/usr/bin/python3
"""radarctl: start, stop and inspect the radar from the command line.

    radarctl status [--json]
    radarctl start                  start a collection (a manual run, like the button)
    radarctl stop [--wait]          stop it; --wait returns as soon as the radar process is gone
    radarctl stop --interrupt --wait --timeout 4
                                    for the power service's stop hook: stop now, urgently, and
                                    let the run resume by itself if power comes back
    radarctl toggle                 exactly what a button press does
    radarctl clear-fault            leave the fault state without starting
    radarctl override no-amp-telemetry on|off
                                    allow running without amplifier telemetry; kept across reboots

Exit status: 0 done, 1 refused or failed (the reason is printed), 2 usage,
3 the radar manager is not running.

Standard library only, and no import from this repository, so that it runs
with /usr/bin/python3 from any directory (the power service calls it).
The socket is /run/radar-runner/user/manager.sock unless --socket or the
environment variable RADARCTL_SOCKET names another.
"""
import argparse
import json
import os
import socket
import sys
import time

DEFAULT_SOCKET = "/run/radar-runner/user/manager.sock"
NOT_RUNNING = 3


class NotRunning(Exception):
    pass


def request(path, payload, timeout_s=2.0):
    """Send one request to the manager and return its reply (a dict)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        try:
            sock.connect(path)
        except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError):
            raise NotRunning(f"the radar manager is not running (no answer at {path})") from None
        except PermissionError:
            raise NotRunning(f"no permission to use {path} (run as user englacial or root)") from None
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while b"\n" not in data:
            part = sock.recv(65536)
            if not part:
                break
            data += part
        if not data:
            raise NotRunning("the radar manager closed the connection without an answer")
        return json.loads(data)
    except socket.timeout:
        raise NotRunning(f"the radar manager did not answer within {timeout_s:g} s") from None
    except (OSError, ValueError) as e:
        raise NotRunning(f"no usable answer from the radar manager: {e}") from None
    finally:
        sock.close()


def format_status(s):
    """The status as a few lines of text."""
    lines = [f"state:      {s.get('state')}" + (f"  ({s['detail']})" if s.get("detail") else "")]
    if s.get("fault"):
        lines.append(f"fault:      {s['fault'].get('kind')}: {s['fault'].get('reason')}")
    last = s.get("last_fault")
    if last and not s.get("fault"):
        lines.append(f"last fault: {last.get('kind')}: {last.get('reason')} (cleared by: {last.get('cleared_by')})")
    for reason in s.get("not_ready") or []:
        lines.append(f"not ready:  {reason}")
    if s.get("run_wanted"):
        lines.append(f"run wanted: by {s.get('trigger')}, for {s.get('run_wanted_for_s')} s")
    radar = s.get("radar") or {}
    if radar.get("active"):
        lines.append(f"collection: {radar.get('collection')}  (pid {radar.get('pid')}, "
                     f"{radar.get('chunks_closed')} data files closed)")
    if radar.get("turn_on_check"):
        lines.append(f"turn-on:    {radar['turn_on_check']}")
    if s.get("last_end"):
        lines.append(f"last run:   {s['last_end']}")
    check = s.get("turn_on_check") or {}
    if check.get("enabled"):
        mode = "blocking" if check.get("blocks") else "record only" + (
            "" if check.get("verified") else ", expected peak not set")
        last = check.get("last") or {}
        lines.append(f"turn-on:    check {mode}; last: "
                     + (f"{last.get('text')} ({last.get('collection')})" if last else "none since the manager started"))
    r = s.get("retries") or {}
    if r.get("failed_starts") or r.get("interruptions"):
        lines.append(f"retries:    {r.get('failed_starts')} failed starts (limit {r.get('max_retries')}), "
                     f"{r.get('interruptions')} interruptions (limit {r.get('max_resumes')})")
    if (s.get("overrides") or {}).get("no_amp_telemetry"):
        lines.append("override:   RUNNING WITHOUT AMPLIFIER TELEMETRY IS ALLOWED")
    if s.get("sdr_state") == "unknown":
        lines.append(f"sdr:        amplifier pin NOT known to be safe ({s.get('sdr_state_why')})")
    if s.get("sdr_state_problem"):
        lines.append(f"sdr:        PROBLEM: {s['sdr_state_problem']}")
    if s.get("amp_enabled_while_idle"):
        lines.append(f"sdr:        AMPLIFIER ENABLED WITH NO RADAR RUNNING ({s['amp_enabled_while_idle']}); "
                     "the safety watcher acts on it")
    if s.get("making_amp_safe"):
        lines.append("sdr:        the make-amplifier-safe tool is running")
    auto = s.get("auto") or {}
    if auto.get("enabled"):
        motion = s.get("motion") or {}
        lines.append(f"auto:       {auto.get('source')} says {auto.get('state')}"
                     + (f" ({motion['reason']})" if motion.get("reason") else "")
                     + (", blocked until stationary" if auto.get("blocked") else ""))
        if motion.get("counting") or motion.get("problem"):
            lines.append(f"            newest fix: {motion.get('problem') or motion.get('counting')}")
    lines.append("gates:")
    for name, gate in (s.get("gates") or {}).items():
        text = "ok" if gate.get("ok") else f"NOT OK: {gate.get('reason')}"
        lines.append(f"  {name:10s}{text}" + (f"  [{gate['note']}]" if gate.get("note") else ""))
    leds = s.get("leds") or {}
    lines.append(f"leds:       READY (yellow) {leds.get('ready_yellow')}, RUNNING (green) {leds.get('running_green')}")
    for warning in s.get("config_warnings") or []:
        lines.append(f"config:     {warning}")
    return "\n".join(lines)


def wait_gone(path, deadline, runs_ended=None, poll_s=0.05, monotonic=time.monotonic, sleep=time.sleep):
    """Wait until no radar process exists (and none is about to start). Returns (done, status).

    The amplifier step that follows a radar exit is not waited for. `runs_ended`
    is the manager's count of ended runs when the stop was accepted: a higher
    count also means "stopped", even if the run has been resumed since.
    `deadline` is on the monotonic clock; no request waits past it.
    """
    status = {}
    while True:
        left = deadline - monotonic()
        if left <= 0:
            return False, status
        status = request(path, {"cmd": "status"}, timeout_s=max(0.2, min(2.0, left))).get("status") or {}
        if status.get("radar_gone") or (runs_ended is not None and status.get("runs_ended", 0) > runs_ended):
            return True, status
        sleep(min(poll_s, max(0.0, deadline - monotonic())))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="radarctl", description="Control the radar manager.")
    parser.add_argument("--socket", default=os.environ.get("RADARCTL_SOCKET", DEFAULT_SOCKET),
                        help="the manager's socket (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="show the state, the gates and the current collection")
    p.add_argument("--json", action="store_true", help="print the full status as JSON")
    sub.add_parser("start", help="start a collection")
    p = sub.add_parser("stop", help="stop the radar")
    p.add_argument("--wait", action="store_true", help="return only when the radar process is gone")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="the whole command takes at most this long, in seconds (default: %(default)s)")
    p.add_argument("--interrupt", action="store_true",
                   help="keep the run wanted: it resumes by itself when every gate is good again")
    sub.add_parser("toggle", help="the same as one press of the button")
    sub.add_parser("clear-fault", help="leave the fault state without starting")
    p = sub.add_parser("override", help="set or clear a user override")
    p.add_argument("name", choices=["no-amp-telemetry"])
    p.add_argument("value", choices=["on", "off"])
    args = parser.parse_args(argv)

    payload = {"cmd": args.command}
    if args.command == "stop" and args.interrupt:
        payload["interrupt"] = True
    if args.command == "override":
        payload.update(name=args.name, set=args.value == "on")
    deadline = time.monotonic() + (args.timeout if args.command == "stop" else 10.0)
    try:
        reply = request(args.socket, payload, timeout_s=max(0.2, min(2.0, deadline - time.monotonic()))
                        if args.command == "stop" else 10.0)
        if args.command == "status":
            status = reply.get("status") or {}
            print(json.dumps(status, indent=1) if args.json else format_status(status))
            return 0 if reply.get("ok") else 1
        print(reply.get("message", ""))
        if not reply.get("ok"):
            return 1
        if args.command == "stop" and args.wait:
            done, status = wait_gone(args.socket, deadline, (reply.get("status") or {}).get("runs_ended"))
            if not done:
                print(f"radarctl: the radar has not stopped after {args.timeout:g} s (state {status.get('state')})",
                      file=sys.stderr)
                return 1
            print("stopped")
        return 0
    except NotRunning as e:
        print(f"radarctl: {e}", file=sys.stderr)
        return NOT_RUNNING


if __name__ == "__main__":
    sys.exit(main())
