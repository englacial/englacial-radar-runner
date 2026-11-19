"""Stand-in for tools/sdr_gpio_safe/amp_safe. Exit 0 is the only success.

Control file key "amp_safe": an exit status (e.g. 3 = not verified), or "hang" (sleep 60 s).
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
if os.environ.get("FAKE_AMP_SAFE_LOG"):
    with open(os.environ["FAKE_AMP_SAFE_LOG"], "a") as f:
        f.write(f"{time.time():.3f} {' '.join(sys.argv[1:])}\n")
behaviour = control.get("amp_safe", 0)
if behaviour == "hang":
    time.sleep(60)
print("amp_safe: GPIO_0 high, verified" if behaviour == 0 else f"amp_safe: failing with status {behaviour}")
sys.exit(int(behaviour))
