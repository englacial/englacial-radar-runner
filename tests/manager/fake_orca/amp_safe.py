"""Stand-in for tools/sdr_gpio_safe/amp_safe. Opens no device.

    amp_safe.py --timeout N [--state-file PATH]

Like the real wrapper: --timeout is mandatory (64 without it); exit 0 is the only
success; with --state-file it writes "unknown" to that file before it starts and
"safe" after an OK (first line the word, free text after; temp file, fsync, rename).

Control file key "amp_safe": an exit status (3 = ASSUME_ENABLED, 2 = NOT_FOUND, 6 = BUSY,
4 = TIMEOUT, ...), "hang" (sleep 60 s after writing "unknown"), "lock" (another caller holds
the tool: exit 4 without starting it), or a list of these used one per call, the last one for
every later call (e.g. [2, 2, 0]). As with the real wrapper, a call that never starts the tool
(lock, 5 FAILED, 8 STATE_FILE, 64 USAGE) leaves the record alone.
Appends one line per call to the file named by FAKE_AMP_SAFE_LOG, if set.
"""
import json
import os
import sys
import time

control = {}
if os.environ.get("FAKE_ORCA_CONTROL"):
    try:
        with open(os.environ["FAKE_ORCA_CONTROL"]) as f:
            control = json.load(f)
    except OSError:
        pass

args = sys.argv[1:]
state_file = args[args.index("--state-file") + 1] if "--state-file" in args else None
calls = 0
if os.environ.get("FAKE_AMP_SAFE_LOG"):
    try:
        with open(os.environ["FAKE_AMP_SAFE_LOG"]) as f:
            calls = len(f.readlines())
    except OSError:
        pass
    with open(os.environ["FAKE_AMP_SAFE_LOG"], "a") as f:
        f.write(f"{time.time():.3f} {' '.join(args)}\n")
if "--timeout" not in args:
    print("usage: amp_safe --timeout N [--state-file PATH]")
    sys.exit(64)


def record(state, why):
    if state_file is None:
        return
    tmp = state_file + ".tool.tmp"
    with open(tmp, "w") as f:
        f.write(f"{state}\n{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {why}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, state_file)


behaviour = control.get("amp_safe", 0)
if isinstance(behaviour, list):
    behaviour = behaviour[min(calls, len(behaviour) - 1)] if behaviour else 0
if behaviour == "lock":
    print("amp_safe: TIMEOUT (exit 4): another run is still going")
    sys.exit(4)
if behaviour in (5, 8, 64):
    print(f"amp_safe: failing with status {behaviour} before the tool was started")
    sys.exit(behaviour)
record("unknown", "amp_safe started")
if behaviour == "hang":
    time.sleep(60)
if behaviour == 0:
    record("safe", "amp_safe: GPIO_0 high, verified")
    print("amp_safe: GPIO_0 high, verified")
else:
    print(f"amp_safe: failing with status {behaviour}")
sys.exit(int(behaviour))
