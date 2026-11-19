#!/usr/bin/python3
"""Stand-in for the system commands that setup/setup.sh calls, for test_setup_script.py.

Linked under the names systemctl, snap, tailscale, ... in a directory that the
test puts first in PATH. Its picture of the system is a JSON file ($FAKE_STATE):
queries are answered from it, and commands that would change the real system
change it instead and are recorded in its "log".
"""
import json
import os
import sys

tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
state_path = os.environ["FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)
destdir = os.environ.get("DESTDIR", "")


def done(code=0, text=None, record=False):
    if text is not None:
        print(text)
    if record:
        state["log"].append(" ".join([tool] + args))
    with open(state_path, "w") as f:
        json.dump(state, f, indent=1)
    sys.exit(code)


if state.get("fail") and state["fail"] in " ".join([tool] + args):
    print(f"{tool}: simulated failure", file=sys.stderr)
    done(1, record=True)

if tool == "id":
    done(0, "1000")

if tool == "systemctl":
    verb = next(a for a in args if not a.startswith("-"))
    unit = args[-1]
    if verb == "list-unit-files":
        done(0, f"{unit} enabled enabled" if unit in state["units"] else None)
    if verb == "is-enabled":
        if unit in state["masked"]:
            done(1, "masked")
        if unit in state["enabled"]:
            done(0, "enabled")
        done(1, "disabled" if unit in state["units"] else "not-found")
    if verb == "is-active":
        done(0 if unit in state["active"] else 3, "active" if unit in state["active"] else "inactive")
    if verb == "show":
        done(0, state["watchdog"])
    if verb == "mask":
        state["masked"].append(unit)
        state["active"] = [u for u in state["active"] if u != unit]
        done(0, record=True)
    if verb == "daemon-reload":
        if os.path.exists(destdir + "/etc/systemd/system.conf.d/radar-runner-watchdog.conf"):
            state["watchdog"] = "30s"
        done(0, record=True)
    if verb == "enable":
        state["units"].append(unit)
        state["enabled"].append(unit)
        state["active"].append(unit)
        done(0, record=True)
    done(1, record=True)        # anything else (restart, stop, ...) is not expected

if tool == "busctl":
    done(0, 's "ignore"' if args[-1] == "HandlePowerKey" else f"t {state['inhibit_usec']}")

if tool == "dpkg-query":
    package = args[-1]
    if package not in state["installed"]:
        done(1)
    sys.stdout.write("installed" if "Status" in args[1] else "1.0")
    done(0)

if tool == "apt-mark":
    if args[0] == "showhold":
        done(0, "\n".join(state["held"]) or None)
    state["held"].append(args[-1])
    done(0, record=True)

if tool == "snap":
    if args[0] == "get":
        print("error: access denied (try with sudo)", file=sys.stderr)
        done(1)
    if "--time" in args:
        done(0, "timer: 00:00~24:00/4\nlast: today at 19:27 UTC\n"
             + (f"hold: {state['snap_hold']}\n" if state["snap_hold"] else "") + "next: tomorrow at 04:22 UTC")
    state["snap_hold"] = "forever"
    done(0, record=True)

if tool == "tailscale":
    if args[:2] == ["debug", "prefs"]:
        if state["tailscale_auto_update"] is None:      # tailscaled not running
            print("failed to connect to local tailscaled", file=sys.stderr)
            done(1)
        done(0, json.dumps({"ControlURL": "https://example.invalid", "WantRunning": True,
                            "AutoUpdate": {"Check": True, "Apply": state["tailscale_auto_update"]},
                            "Hostname": ""}, indent="\t"))
    if args == ["set", "--auto-update=false"]:
        if not state.get("tailscale_set_ignored"):
            state["tailscale_auto_update"] = False
        done(0, record=True)
    done(1, record=True)        # nothing else about Tailscale may be touched

if tool == "systemd-tmpfiles":
    with open(args[-1]) as f:
        for line in f:
            fields = line.split()
            if len(fields) >= 3 and fields[0] == "d":
                os.makedirs(destdir + fields[1], exist_ok=True)
                os.chmod(destdir + fields[1], int(fields[2], 8))
    done(0, record=True)

if tool == "findmnt":
    if "--verify" in args:
        done(0 if state["fstab_valid"] else 1)
    done(0 if state["nvme_device"] else 1, state["nvme_device"] or None)

if tool == "lsblk":
    if args[0] == "-rno":
        done(0, "\n".join(state["labels"]))
    done(0, state["nvme_uuid"] if "UUID" in args else state["nvme_label"])

print(f"fake_system_tool: no stand-in for {tool}", file=sys.stderr)
done(127, record=True)
