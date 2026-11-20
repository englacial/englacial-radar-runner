"""Stand-in for tools/turn_on_check/measure.py --input IN --output OUT (no numpy, no real analysis).

It writes what measure.py would write for a loopback with one peak where the input expects it
(or at sample 78.4 when nothing is expected), at "level_db" (default -36.0, tx_gain 70 less 30 dB).

Control file key "analysis": "fail" (exit 1, no output), "garbage" (output that is not JSON),
"slow" (sleep 30 s first), "error" (measure.py's own error: no complete record, exit 3),
"absent" (no peak where one is expected), "count_data" (the records found in the data file);
"level_db": the level of the peak; "raw_max_fs": the raw maximum.
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
path_in, path_out = args[args.index("--input") + 1], args[args.index("--output") + 1]
with open(path_in) as f:
    spec = json.load(f)
print("fake analysis of", spec["data_files"])
mode = control.get("analysis", "")
if mode == "fail":
    print("Traceback (most recent call last): ... ValueError")
    sys.exit(1)
if mode == "slow":
    time.sleep(30)
if mode == "garbage":
    with open(path_out, "w") as f:
        f.write("{not json")
    sys.exit(0)
records = spec["records_expected"]
if mode == "count_data":
    try:
        records = sum(os.path.getsize(p) for p in spec["data_files"]) // (8 * spec["record_samples"])
    except OSError:
        records = 0
out = {"records": records, "records_expected": spec["records_expected"], "record_samples": spec["record_samples"]}
if mode == "error" or records == 0:
    out["error"] = "no complete record in the data"
else:
    level = float(control.get("level_db", -36.0))
    expected = spec.get("expected")
    at = 78.4 if not expected else expected["leakage_sample"] + expected["delay_samples"]
    peak = {"index": int(at), "sample": at, "level_db": level}
    out.update(raw_max_fs=float(control.get("raw_max_fs", 0.016)), clip_fs=spec["clip_fs"], clipped_samples=0,
               noise_floor_db=-105.0, strongest=peak, peaks=[peak], mode="none")
    if expected:
        out["mode"] = "combined" if expected["delay_samples"] < expected["min_separation_samples"] else "two_peak"
        window = {"index": int(at) - 3, "sample": at - 3, "level_db": -70.0}
        out["direct"] = ({"expected": at, "tolerance": expected["tolerance_samples"], "found": False,
                          "window_max": window} if mode == "absent" else
                         dict(peak, expected=at, tolerance=expected["tolerance_samples"], found=True,
                              window_max=peak))
with open(path_out, "w") as f:
    json.dump(out, f)
sys.exit(3 if "error" in out else 0)
