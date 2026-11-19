"""alertctl: look at the alerts service and send a test alert through it.

    alertctl status [--json]        the service's status: the link to ntfy, the queue, active conditions
    alertctl queue                  the alerts waiting to be sent, in sending order
    alertctl test [--priority P] [--text TEXT] [--wait S]
                                    put a test alert in the service's inbox; the service queues and
                                    sends it like any other alert. P: urgent, high, default (the
                                    default) or low. --wait: wait up to S seconds until it is sent

Exit status: 0 done, 1 failed or not sent within --wait, 2 usage, 3 the alerts service is not running.

It reads the config for the paths only, never the secrets file, and writes only
into the service's inbox. Run as user englacial.
"""
import argparse
import json
import os
import secrets
import sys
import time

from ..common import config as config_module
from ..common import status as status_files
from . import logic as L
from .queue import DiskQueue

NOT_RUNNING = 3


def _paths(config):
    a, state_dir = config["alerts"], config["paths"]["state_dir"]
    return {"queue": os.path.join(state_dir, a["queue_dir"]), "inbox": os.path.join(state_dir, a["inbox_dir"]),
            "status": status_files.status_path("alerts", config["paths"]["run_dir"])}


def _read(path):
    return status_files.read_status(path, 10.0)


def format_status(s, read):
    lines = [f"service:    {'running' if read.ok else read.state + (': ' + read.error if read.error else '')}"]
    ntfy, link = s.get("ntfy") or {}, s.get("link") or {}
    lines.append(f"ntfy:       {'configured' if ntfy.get('configured') else 'NOT CONFIGURED: ' + str(ntfy.get('problem'))}")
    lines.append(f"link:       {link.get('state')}" + (f", {link.get('failures_in_a_row')} failures in a row, next try in "
                                                       f"{link.get('next_attempt_in_s')} s" if link.get("failures_in_a_row") else ""))
    if link.get("last_result"):
        lines.append(f"last send:  {link.get('last_result')}")
    q = s.get("queue") or {}
    by = ", ".join(f"{n} {p}" for p, n in (q.get("by_priority") or {}).items())
    lines.append(f"queue:      {q.get('length')} waiting" + (f" ({by})" if by else "")
                 + f"; at most {q.get('max')}; dropped {q.get('dropped')}" + (f"; ERROR {q['error']}" if q.get("error") else ""))
    c = s.get("counters") or {}
    lines.append(f"since start: {c.get('queued')} queued, {c.get('sent')} sent, {c.get('held_back')} held back, "
                 f"{c.get('rejected')} refused by the server")
    lines.append(f"boot:       {s.get('boot')}; summary {'sent to the queue' if s.get('summary_sent') else 'not yet'}")
    for name, cond in sorted((s.get("conditions") or {}).items()):
        lines.append(f"condition:  {name}: {cond.get('title')} ({'alerted' if cond.get('raised') else 'pending'})")
    for name, e in sorted((s.get("events") or {}).items()):
        lines.append(f"events:     {name}: {e.get('lines')} lines, {e.get('skipped')} skipped"
                     + (f", {e['lost']} rotations missed" if e.get("lost") else "") + (f"; {e['error']}" if e.get("error") else ""))
    for part, error in sorted((s.get("errors") or {}).items()):
        lines.append(f"ERROR:      {part}: {error}")
    for r in (s.get("recent") or [])[-5:]:
        lines.append(f"recent:     {r.get('id')} {r.get('priority')}: {r.get('title')}: {r.get('result')}")
    return "\n".join(lines)


def cmd_status(paths, args):
    read = _read(paths["status"])
    if read.data is None:
        print(f"the alerts service has not written {paths['status']} ({read.state}: {read.error})")
        return NOT_RUNNING
    if args.json:
        print(json.dumps(read.data, indent=1))
    else:
        print(format_status(read.data, read))
    return 0 if read.ok else NOT_RUNNING


def cmd_queue(paths, args):
    q = DiskQueue(paths["queue"], 1)
    names = q.names()
    if not names:
        print(f"no alerts waiting in {paths['queue']}" + (f" ({q.error})" if q.error else ""))
        return 0
    for priority, _, name in names:
        try:
            with open(os.path.join(paths["queue"], name), "rb") as f:
                title = json.loads(f.read(16384)).get("title", "?")
        except (OSError, ValueError, AttributeError):
            title = "(unreadable)"
        print(f"{name}  {L.PRIORITY_NAMES.get(priority, priority):8} {title}")
    return 0


def cmd_test(paths, args):
    test_id = secrets.token_hex(4)
    request = {"id": test_id, "priority": args.priority, "text": args.text or ""}
    name = f"{time.time_ns()}-{os.getpid()}.json"
    tmp = os.path.join(paths["inbox"], "." + name + ".tmp")
    try:
        os.makedirs(paths["inbox"], exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(request, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(paths["inbox"], name))
    except OSError as e:
        print(f"cannot write to {paths['inbox']}: {e.strerror or e} (run as user englacial)")
        return 1
    read = _read(paths["status"])
    print(f"test alert {test_id} ({args.priority}) is in the inbox")
    if not read.ok:
        print("the alerts service is not running: the alert is sent when it starts")
        return NOT_RUNNING
    if not args.wait:
        return 0
    deadline = time.monotonic() + args.wait
    last = ""
    while time.monotonic() < deadline:
        read = _read(paths["status"])
        entry = next((r for r in (read.data or {}).get("recent") or [] if r.get("test_id") == test_id), None)
        if entry is not None and entry.get("result") != last:
            last = entry.get("result")
            print(f"{entry.get('id')}: {last}")
            if last == "sent":
                return 0
        time.sleep(0.5)
    link = ((read.data or {}).get("link") or {})
    print(f"not sent within {args.wait:g} s; link {link.get('state')}: {link.get('last_result')}")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="alertctl", description=__doc__.split("\n\n")[0])
    parser.add_argument("--local-config", default=config_module.LOCAL_FILE, help="per-unit config file")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    sub.add_parser("queue")
    p = sub.add_parser("test")
    p.add_argument("--priority", choices=[L.PRIORITY_NAMES[x] for x in sorted(L.PRIORITY_NAMES, reverse=True)],
                   default="default")
    p.add_argument("--text", default="")
    p.add_argument("--wait", type=float, default=0.0, metavar="S")
    args = parser.parse_args(argv)
    config, _ = config_module.load(local_file=args.local_config, secrets_file=None)
    paths = _paths(config)
    return {"status": cmd_status, "queue": cmd_queue, "test": cmd_test}[args.command](paths, args)


if __name__ == "__main__":
    sys.exit(main())
