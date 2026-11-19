"""Stand-in for "cmake .. && make". Control file key "build": "fail" (exit 2)."""
import json
import os
import sys

control = {}
if os.environ.get("FAKE_ORCA_CONTROL"):
    try:
        with open(os.environ["FAKE_ORCA_CONTROL"]) as f:
            control = json.load(f)
    except OSError:
        pass
if control.get("build") == "fail":
    print("make: *** [radar] Error 1")
    sys.exit(2)
print("[100%] Built target radar")
