"""Stand-in for ORCA's preprocessing/generate_chirp.py.

Path rule copied from the real one: the chirp goes to
    os.path.join(<checkout>, FILES.output_dir, GENERATE.out_file)
where <checkout> is the parent of this script's directory.

Control file key "chirp": "fail" (exit 1), "slow" (sleep 30 s first), "nofile" (write nothing);
"chirp_from": copy this file as the chirp (otherwise zeros of the right length).
"""
import json
import os
import sys
import time

import yaml

control = {}
if os.environ.get("FAKE_ORCA_CONTROL"):
    try:
        with open(os.environ["FAKE_ORCA_CONTROL"]) as f:
            control = json.load(f)
    except OSError:
        pass
if control.get("chirp") == "fail":
    print("Error occured when generating chirp.")
    sys.exit(1)
if control.get("chirp") == "slow":
    time.sleep(30)
if control.get("chirp") == "nofile":
    sys.exit(0)

with open(sys.argv[1]) as f:
    config = yaml.safe_load(f)
script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
output_dir = os.path.join(script_dir, config["FILES"].get("output_dir", "data"))
os.makedirs(output_dir, exist_ok=True)
filename = os.path.join(output_dir, config["GENERATE"]["out_file"])
samples = int(float(config["GENERATE"]["sample_rate"]) * float(config["GENERATE"]["pulse_length"]))
with open(filename, "wb") as f:
    if control.get("chirp_from"):           # a real chirp, for the turn-on check's analysis
        with open(control["chirp_from"], "rb") as real:
            f.write(real.read())
    else:
        f.write(b"\x00" * (8 * max(1, samples)))
print("--- Converting Chirp to File ---")
print("\tChirp successfully stored in %s" % filename)
