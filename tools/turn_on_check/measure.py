#!/usr/bin/env python3
"""Turn-on check: the direct-signal peak in a short collection (radar_runner/manager/README.md, "Turn-on check").

    PY=~/miniforge3/envs/orca/bin/python
    $PY tools/turn_on_check/measure.py DIR [--leakage SAMPLE] [--direct SAMPLE] [--margin-db 6]
        DIR is a collection, or the turn_on_check directory of one. Prints the peaks and the
        settings (manager.turn_on_check.expected) that would make this the expected result.
    $PY tools/turn_on_check/measure.py --input analysis_input.json --output analysis.json
        what the radar manager runs after each low-power run (the input is written by turnon.py)

Reads only, and needs numpy. The profile is the transmitted chirp correlated with each
record, the magnitudes averaged over the records, in dB relative to the chirp: a record that
is the chirp at amplitude a gives 20 log10(a) at its delay (as tools/transit_check).
"""
import argparse
import glob
import json
import math
import os
import re
import sys

import numpy as np

C = 299792458.0
MAX_RECORDS = 50            # records read by hand from a collection: enough for the profile
PEAKS_DB = 50.0             # local maxima listed: down to this far below the strongest
LEAKAGE_WITHIN = 40         # by hand: the leakage peak is looked for at most this many samples before the direct


def data_files(directory, name):
    files = glob.glob(os.path.join(directory, name + ".*"))
    return sorted((f for f in files if f.rsplit(".", 1)[1].isdigit()), key=lambda f: int(f.rsplit(".", 1)[1]))


def load_records(files, record_samples, max_records=None):
    """The complete records in the data files, in order, as an array (records, samples)."""
    parts, left = [], max_records
    for path in files:
        count = -1 if left is None else left * record_samples
        x = np.fromfile(path, dtype=np.complex64, count=count)
        parts.append(x)
        if left is not None:
            left -= len(x) // record_samples
            if left <= 0:
                break
    x = np.concatenate(parts) if parts else np.zeros(0, np.complex64)
    n = len(x) // record_samples
    return x[: n * record_samples].reshape(n, record_samples)


def profile_db(records, chirp):
    norm = float(np.sum(np.abs(chirp) ** 2))
    if norm <= 0:
        raise ValueError("the chirp file holds no signal")
    mean = np.mean([np.abs(np.correlate(r, chirp, mode="valid")) for r in records], axis=0)
    return 20 * np.log10(mean / norm + 1e-12)


def refine(p, i):
    """Peak position to a fraction of a sample (a parabola through three points)."""
    if 0 < i < len(p) - 1:
        a, b, c = p[i - 1], p[i], p[i + 1]
        d = a - 2 * b + c
        if d < 0:
            return i + 0.5 * (a - c) / d
    return float(i)


def local_maxima(p):
    i = np.arange(1, len(p) - 1)
    return i[(p[i] > p[i - 1]) & (p[i] >= p[i + 1])]


def peak(p, i):
    return {"index": int(i), "sample": round(float(refine(p, i)), 2), "level_db": round(float(p[i]), 2)}


def peak_near(p, centre, tol):
    """The strongest local maximum within `tol` samples of `centre`, and what the window holds."""
    lo, hi = max(0, math.ceil(centre - tol)), min(len(p) - 1, math.floor(centre + tol))
    out = {"expected": round(float(centre), 2), "tolerance": tol, "found": False}
    if lo > hi:
        return out
    top = lo + int(np.argmax(p[lo:hi + 1]))
    out["window_max"] = peak(p, top)
    found = [i for i in local_maxima(p) if lo - 1 <= i <= hi + 1 and abs(refine(p, i) - centre) <= tol]
    if found:
        best = max(found, key=lambda i: p[i])
        out.update(found=True, **peak(p, best))
    return out


def measure(records, chirp, records_expected=None, clip_fs=0.9, expected=None):
    """Everything the check judges, from the records. `expected`: leakage_sample, delay_samples,
    tolerance_samples, min_separation_samples, or None (nothing is searched for)."""
    out = {"records": int(len(records)), "records_expected": records_expected,
           "record_samples": int(records.shape[1]), "chirp_samples": int(len(chirp))}
    if not len(records):
        out["error"] = "no complete record in the data"
        return out
    if records.shape[1] < len(chirp) + 3:
        out["error"] = "the records are shorter than the chirp"
        return out
    magnitude = np.abs(records)
    out["raw_max_fs"] = round(float(magnitude.max()), 4)
    out["clip_fs"] = clip_fs
    out["clipped_samples"] = int(np.count_nonzero(magnitude >= clip_fs))
    p = profile_db(records, chirp)
    out["noise_floor_db"] = round(float(np.median(p)), 1)
    top = int(np.argmax(p))
    out["strongest"] = peak(p, top)
    maxima = [i for i in local_maxima(p) if p[i] >= p[top] - PEAKS_DB]
    maxima = sorted(sorted(maxima, key=lambda i: -p[i])[:8])
    out["peaks"] = [peak(p, i) for i in maxima]
    out["mode"] = "none"
    if expected:
        tol = float(expected["tolerance_samples"])
        leak_at, delay = float(expected["leakage_sample"]), float(expected["delay_samples"])
        if delay < float(expected["min_separation_samples"]):
            out["mode"] = "combined"            # the two peaks form one: its level is judged
            out["direct"] = peak_near(p, leak_at + delay, tol)
        else:
            out["mode"] = "two_peak"
            leak = peak_near(p, leak_at, tol)
            out["leakage"] = leak
            base = leak["sample"] if leak["found"] else leak_at
            out["direct"] = peak_near(p, base + delay, tol)
    return out


# ----------------------------------------------------------------------- manager

def run_input(path_in, path_out):
    with open(path_in) as f:
        spec = json.load(f)
    chirp = np.fromfile(spec["chirp"], dtype=np.complex64)
    records = load_records(spec["data_files"], int(spec["record_samples"]))
    out = measure(records, chirp, spec.get("records_expected"), float(spec.get("clip_fs", 0.9)),
                  spec.get("expected"))
    tmp = path_out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path_out)
    return 0 if "error" not in out else 3


# ------------------------------------------------------------------------ by hand

def _first(pattern, text):
    m = re.search(pattern, text, re.M)
    return float(m.group(1)) if m else None


def describe(directory):
    """(data files, record samples, tx_gain, sample rate, note) of a collection or a turn_on_check directory."""
    directory = directory.rstrip("/")
    spec_path = os.path.join(directory, "analysis_input.json")
    config = ""
    try:
        with open(os.path.join(directory, "config.yaml")) as f:
            config = f.read()
    except OSError:
        pass
    rate = _first(r"^\s*rx_rate:\s*([0-9.eE+-]+)", config)         # RF0 comes first
    if os.path.exists(spec_path):
        with open(spec_path) as f:
            spec = json.load(f)
        return (data_files(directory, "low_power.bin"), int(spec["record_samples"]), spec.get("tx_gain"),
                rate, "the low-power run of a turn-on check")
    with open(os.path.join(directory, "collection.json")) as f:
        meta = json.load(f)
    return (data_files(directory, meta["data"]["file"]), meta["data"]["record_bytes"] // 8,
            _first(r"^\s*tx_gain:\s*([0-9.]+)", config), rate, "a collection")


def by_hand(args):
    files, samples, gain, rate, what = describe(args.directory)
    if not files:
        sys.exit(f"no data files in {args.directory}")
    chirp = np.fromfile(os.path.join(args.directory, "chirp.bin"), dtype=np.complex64)
    records = load_records(files, samples, MAX_RECORDS)
    m = measure(records, chirp, clip_fs=args.clip_fs)
    if "error" in m:
        sys.exit(m["error"])
    print(f"{args.directory}: {what}, tx_gain {gain}, {m['records']} records of {samples} samples")
    print(f"raw maximum {m['raw_max_fs']:.3f} of full scale; noise floor (median) {m['noise_floor_db']:.1f} dB")
    print("peaks (sample, dB relative to the chirp):")
    for pk in m["peaks"]:
        print(f"  {pk['sample']:8.2f}  {pk['level_db']:7.1f}")
    s = m["strongest"]
    direct = s["sample"] if args.direct is None else args.direct
    if args.leakage is not None:
        leakage = args.leakage
    else:
        # The leakage inside the SDR arrives first: the strongest peak shortly before the direct one.
        earlier = [pk for pk in m["peaks"] if direct - LEAKAGE_WITHIN <= pk["sample"] <= direct - args.min_separation
                   and pk["level_db"] >= s["level_db"] - args.leakage_db]
        leakage = max(earlier, key=lambda pk: pk["level_db"])["sample"] if earlier else direct
    delay = max(0.0, direct - leakage)
    if delay < args.min_separation:
        leakage, delay = direct, 0.0
    expected = {"leakage_sample": leakage, "delay_samples": delay, "tolerance_samples": args.tolerance,
                "min_separation_samples": args.min_separation}
    m = measure(records, chirp, clip_fs=args.clip_fs, expected=expected)
    d = m["direct"]
    if not d["found"]:
        sys.exit(f"no peak at sample {d['expected']} (+/- {args.tolerance}): give --direct and --leakage")
    if m["mode"] == "two_peak":
        lk = m["leakage"]
        print(f"leakage peak at {lk['sample']} ({lk['level_db']:.1f} dB)" if lk["found"] else
              f"no leakage peak at {leakage}: {direct} is taken as it is")
        print(f"direct peak at {d['sample']} ({d['level_db']:.1f} dB), {delay:.2f} samples after the leakage"
              + (f" (about {delay * C / rate:.1f} m of path in air)" if rate else ""))
    else:
        print(f"one peak at {d['sample']} ({d['level_db']:.1f} dB): the leakage and the direct signal cannot be "
              f"told apart (closer than {args.min_separation:g} samples): the level of the combined peak is judged")
    if gain is None:
        sys.exit("tx_gain not found in config.yaml")
    print("\nSettings for /etc/radar-runner/local.yaml, if this collection is verified as correct:\n")
    print("manager:\n  turn_on_check:\n    expected:\n      verified: true")
    print(f"      leakage_sample: {leakage if m['mode'] == 'combined' else m['leakage'].get('sample', leakage)}")
    print(f"      delay_samples: {round(delay, 2)}")
    print(f"      tolerance_samples: {args.tolerance:g}")
    print(f"      level_tx_gain: {gain:g}")
    print(f"      min_level_db: {round(d['level_db'] - args.margin_db, 1)}"
          f"      # {d['level_db']:.1f} dB measured, less a margin of {args.margin_db:g} dB")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("directory", nargs="?", help="a collection, or its turn_on_check directory")
    parser.add_argument("--input", help="(the manager) the analysis input file")
    parser.add_argument("--output", help="(the manager) where the result goes")
    parser.add_argument("--leakage", type=float, help="sample of the leakage peak (default: the earliest peak)")
    parser.add_argument("--direct", type=float, help="sample of the direct peak (default: the strongest)")
    parser.add_argument("--tolerance", type=float, default=1.5, help="samples (default %(default)s)")
    parser.add_argument("--min-separation", type=float, default=4.0,
                        help="closer peaks are judged as one (default %(default)s samples)")
    parser.add_argument("--leakage-db", type=float, default=40.0,
                        help="a leakage peak may be this far below the direct one (default %(default)s dB)")
    parser.add_argument("--margin-db", type=float, default=6.0,
                        help="minimum level = measured level less this (default %(default)s dB)")
    parser.add_argument("--clip-fs", type=float, default=0.9)
    args = parser.parse_args(argv)
    if args.input:
        if not args.output:
            parser.error("--input needs --output")
        return run_input(args.input, args.output)
    if not args.directory:
        parser.error("give a directory, or --input and --output")
    return by_hand(args)


if __name__ == "__main__":
    sys.exit(main())
