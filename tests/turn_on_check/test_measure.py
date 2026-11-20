"""tools/turn_on_check/measure.py on recorded bench records (fixtures/README.md). Needs numpy:

    ~/miniforge3/envs/orca/bin/python -B -m pytest -q -p no:cacheprovider tests/turn_on_check

Skipped in an environment without numpy (the manager's own).
"""
import importlib.util
import json
import os
import shutil

import pytest

np = pytest.importorskip("numpy")

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
TOOL = os.path.join(HERE, "..", "..", "tools", "turn_on_check", "measure.py")
spec = importlib.util.spec_from_file_location("measure", TOOL)
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)

SAMPLES = 2352
COMBINED = {"leakage_sample": 78.4, "delay_samples": 0.0, "tolerance_samples": 1.5, "min_separation_samples": 4.0}
TWO_PEAK = {"leakage_sample": 75.0, "delay_samples": 15.2, "tolerance_samples": 1.5, "min_separation_samples": 4.0}


def records(name):
    return measure.load_records([os.path.join(FIX, name)], SAMPLES)


def chirp():
    return np.fromfile(os.path.join(FIX, "chirp.bin"), dtype=np.complex64)


def test_portable_loopback_one_combined_peak():
    # docs/transit-reference: peak at sample 78, -5.98 dB, raw maximum 0.515.
    m = measure.measure(records("loopback_30ft_tx70.bin"), chirp(), 2, expected=COMBINED)
    assert m["records"] == 2 and m["mode"] == "combined" and m["clipped_samples"] == 0
    assert m["raw_max_fs"] == pytest.approx(0.52, abs=0.02)
    d = m["direct"]
    assert d["found"] and d["index"] == 78 and d["sample"] == pytest.approx(78.4, abs=0.1)
    assert d["level_db"] == pytest.approx(-5.95, abs=0.2)
    assert m["strongest"]["index"] == 78 and m["noise_floor_db"] < -85


def test_fifty_metre_loopback_two_peaks():
    # The earlier 50 m cable: leakage at 75 (-35 dB), loopback 15 samples later (-10.6 dB).
    m = measure.measure(records("loopback_50m_tx70.bin"), chirp(), 2, expected=TWO_PEAK)
    assert m["mode"] == "two_peak"
    assert m["leakage"]["found"] and m["leakage"]["sample"] == pytest.approx(75.0, abs=0.2)
    assert m["leakage"]["level_db"] == pytest.approx(-35.0, abs=0.5)
    assert m["direct"]["found"] and m["direct"]["sample"] == pytest.approx(90.2, abs=0.2)
    assert m["direct"]["level_db"] == pytest.approx(-10.6, abs=0.3)


def test_no_signal_has_no_peak_where_one_is_expected():
    m = measure.measure(records("no_signal_tx0.bin"), chirp(), 2, expected=COMBINED)
    d = m["direct"]
    assert not d["found"] or d["level_db"] < -90


def test_amplifier_off_only_the_leakage_fails_by_position_and_by_level():
    # The 28 V gone after a power cut, the SDR still running: only the leakage, 3 samples early.
    x = records("amplifier_off_tx70.bin")
    d = measure.measure(x, chirp(), 2, expected=COMBINED)["direct"]
    assert not d["found"]                                   # nothing at the bench's 78.4 +/- 1.5
    wide = dict(COMBINED, tolerance_samples=4.0)            # a tolerance wide enough to reach the leakage
    d = measure.measure(x, chirp(), 2, expected=wide)["direct"]
    assert d["found"] and d["sample"] == pytest.approx(75.07, abs=0.1)
    assert d["level_db"] == pytest.approx(-33.5, abs=0.3)   # 27.6 dB under the bench's -5.9
    assert d["level_db"] < -5.9 - 15                        # below a minimum set with a 15 dB margin


def test_wrong_delay_finds_no_direct_peak():
    # The portable loopback judged with the 50 m cable's delay: nothing at 90.
    m = measure.measure(records("loopback_30ft_tx70.bin"), chirp(), 2, expected=TWO_PEAK)
    assert not m["direct"]["found"] or m["direct"]["level_db"] < -50
    assert m["direct"]["window_max"]["level_db"] < -50


def test_level_follows_tx_gain_one_db_per_step():
    lo = measure.measure(records("loopback_50m_tx45.bin"), chirp(), 1, expected=TWO_PEAK)["direct"]["level_db"]
    hi = measure.measure(records("loopback_50m_tx72.bin"), chirp(), 1, expected=TWO_PEAK)["direct"]["level_db"]
    assert hi - lo == pytest.approx(27.0, abs=0.5)


def test_clipping_is_counted():
    x = records("loopback_30ft_tx70.bin") * 2.0      # 1.04 of full scale at the top
    m = measure.measure(x, chirp(), 2, clip_fs=0.9)
    assert m["raw_max_fs"] > 0.9 and m["clipped_samples"] > 0 and m["mode"] == "none"


def test_empty_or_short_data_is_an_error():
    assert "no complete record" in measure.measure(np.zeros((0, SAMPLES), np.complex64), chirp())["error"]
    assert "shorter than the chirp" in measure.measure(np.zeros((1, 100), np.complex64), chirp())["error"]


def test_the_managers_call(tmp_path):
    data = tmp_path / "low_power.bin.0"
    shutil.copy(os.path.join(FIX, "loopback_30ft_tx70.bin"), data)
    with open(data, "ab") as f:
        f.write(b"\0" * 100)                        # a cut-off record at the end is left out
    spec_in = {"data_files": [str(data)], "chirp": os.path.join(FIX, "chirp.bin"), "record_samples": SAMPLES,
               "records_expected": 2, "clip_fs": 0.9, "expected": COMBINED}
    (tmp_path / "in.json").write_text(json.dumps(spec_in))
    assert measure.main(["--input", str(tmp_path / "in.json"), "--output", str(tmp_path / "out.json")]) == 0
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["records"] == 2 and out["direct"]["found"]
    data.write_bytes(b"")
    assert measure.main(["--input", str(tmp_path / "in.json"), "--output", str(tmp_path / "out.json")]) == 3
    assert "error" in json.loads((tmp_path / "out.json").read_text())


def collection(tmp_path, name, gain):
    d = tmp_path / "b00001_s001_20261120T000000Z"
    d.mkdir()
    shutil.copy(os.path.join(FIX, "chirp.bin"), d / "chirp.bin")
    shutil.copy(os.path.join(FIX, name), d / "rx_samps.bin.0")
    (d / "collection.json").write_text(json.dumps({"data": {"file": "rx_samps.bin", "record_bytes": SAMPLES * 8}}))
    (d / "config.yaml").write_text(f"RF0:\n  rx_rate: 56000000.0\n  tx_gain: {gain}\nRF1:\n  tx_gain: 10\n")
    return d


def test_by_hand_on_the_portable_loopback_prints_the_settings(tmp_path, capsys):
    d = collection(tmp_path, "loopback_30ft_tx70.bin", 70.0)
    assert measure.main([str(d)]) == 0
    out = capsys.readouterr().out
    assert "one peak at 78.4" in out and "cannot be told apart" in out and "WARNING" not in out
    assert "      verified: true\n      leakage_sample: 78.4\n      delay_samples: 0.0\n" in out
    assert "      level_tx_gain: 70\n      min_level_db: -18.0" in out


def test_by_hand_with_more_than_one_strong_peak_the_user_must_choose(tmp_path, capsys):
    # If the leakage were the strongest peak, taking the strongest as the direct one would judge the leakage.
    d = collection(tmp_path, "loopback_50m_tx70.bin", 70.0)
    assert measure.main([str(d)]) == 0
    out = capsys.readouterr().out
    assert "WARNING: more than one peak within 40 dB of the strongest" in out
    assert "74.98" in out and "90.16" in out and "--direct" in out
    assert "verified: true" not in out and "      verified: false" in out


def test_by_hand_finds_the_leakage_before_the_direct_peak(tmp_path, capsys):
    d = collection(tmp_path, "loopback_50m_tx70.bin", 70.0)
    assert measure.main([str(d), "--direct", "90.16"]) == 0
    out = capsys.readouterr().out
    assert "WARNING" not in out and "      verified: true" in out
    assert "leakage peak at 74.98" in out and "15.18 samples after the leakage (about 81.3 m of path in air)" in out
    assert "      delay_samples: 15.18" in out and "      min_level_db: -22.6" in out
