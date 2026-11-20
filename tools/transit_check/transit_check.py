#!/usr/bin/env python3
"""Loopback reference check: the numbers that describe one collection made into the bench
loopback, to compare a run after transport with the reference made before it.

    ~/miniforge3/envs/orca/bin/python tools/transit_check/transit_check.py COLLECTION_DIR [REFERENCE.json]
    ... --save REFERENCE.json     write this collection's numbers as the reference

Reads only. The level is the pulse-compressed peak of each record (1000 pulses) against the
transmitted chirp, in dB; the shape is the mean compressed profile around the peak.
"""
import glob, json, os, re, sys
import numpy as np

def measure(d):
    d = d.rstrip("/")
    chirp = np.fromfile(d + "/chirp.bin", dtype=np.complex64)
    meta = json.load(open(d + "/collection.json"))
    nbytes = meta["data"]["record_bytes"]; nsamp = nbytes // 8
    files = sorted(glob.glob(d + "/rx_samps.bin.*"), key=lambda p: int(p.rsplit(".", 1)[1]))
    x = np.concatenate([np.fromfile(f, dtype=np.complex64) for f in files])
    x = x[: len(x) // nsamp * nsamp].reshape(-1, nsamp)
    n = np.sum(np.abs(chirp) ** 2)
    prof = np.array([np.abs(np.correlate(r, chirp, mode="valid")) for r in x])
    mean_db = 20 * np.log10(prof.mean(axis=0) / n + 1e-12)
    peak = int(np.argmax(mean_db))
    level = 20 * np.log10(prof[:, peak] / n + 1e-12)
    spec = np.fft.fftshift(np.abs(np.fft.fft(x.mean(axis=0))))
    freq = np.fft.fftshift(np.fft.fftfreq(nsamp, 1 / 56e6)) / 1e6
    band = freq[spec > spec.max() * 0.1]
    out = {"collection": os.path.basename(d), "records": int(len(x)),
           "peak_sample": peak, "peak_level_db": round(float(level.mean()), 2),
           "peak_level_std_db": round(float(level.std()), 3),
           "peak_level_first_last_db": [round(float(level[:10].mean()), 2), round(float(level[-10:].mean()), 2)],
           "shape_db_peak_minus6_to_plus6": [round(float(v), 1) for v in mean_db[peak - 6: peak + 7]],
           "noise_floor_median_db": round(float(np.median(mean_db)), 1),
           "raw_max_fs": round(float(np.abs(x).max()), 3),
           "spectrum_band_mhz": [round(float(band.min()), 1), round(float(band.max()), 1)]}
    log = open(d + "/uhd_stdout.log", errors="replace").read()
    m = re.findall(r"Total pulses (written|attempted): (\d+)", log)
    c = {k: int(v) for k, v in m}
    if "written" in c and "attempted" in c and c["attempted"]:
        out["pulses_written"], out["pulses_attempted"] = c["written"], c["attempted"]
        out["pulses_lost_pct"] = round(100 * (1 - c["written"] / c["attempted"]), 2)
    cfg = open(d + "/config_requested.yaml").read()
    g = re.search(r"^\s*tx_gain:\s*([\d.]+)", cfg, re.M)
    out["tx_gain"] = float(g.group(1)) if g else None
    return out

def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    now = measure(argv[0])
    for k, v in now.items():
        print(f"{k:32s} {v}")
    if len(argv) == 3 and argv[1] == "--save":
        json.dump(now, open(argv[2], "w"), indent=1); print("saved as the reference:", argv[2])
    elif len(argv) == 2:
        ref = json.load(open(argv[1]))
        print(f"\nagainst the reference {ref['collection']} (tx_gain {ref.get('tx_gain')}):")
        print(f"  peak level       {now['peak_level_db'] - ref['peak_level_db']:+.2f} dB")
        print(f"  peak sample      {now['peak_sample'] - ref['peak_sample']:+d}")
        d = [a - b for a, b in zip(now["shape_db_peak_minus6_to_plus6"], ref["shape_db_peak_minus6_to_plus6"])]
        print(f"  shape, peak-6..+6, relative to the peak change: {[round(v - d[6], 1) for v in d]}")
        print(f"  noise floor      {now['noise_floor_median_db'] - ref['noise_floor_median_db']:+.1f} dB")
        if "pulses_lost_pct" in now and "pulses_lost_pct" in ref:
            print(f"  pulses lost      {now['pulses_lost_pct']:.2f} % (reference {ref['pulses_lost_pct']:.2f} %)")

if __name__ == "__main__":
    main(sys.argv[1:])
