"""The turn-on check's settings, low-power config and judgement (radar_runner/manager/turnon.py). No processes."""
import copy
import os

import pytest

from radar_runner.common import config as manager_config
from radar_runner.manager import limits, turnon
from radar_runner.manager import logic as L
from radar_runner.manager.radarctl import format_status

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "fixtures", "orca_configs", "run300_d05_soak.yaml")


def config(**changes):
    c = copy.deepcopy(manager_config.DEFAULTS)
    t = c["manager"]["turn_on_check"]
    for key, value in changes.items():
        if key in t["expected"]:
            t["expected"][key] = value
        else:
            t[key] = value
    return c


VERIFIED = dict(verified=True, leakage_sample=78.4, delay_samples=0.0, level_tx_gain=70.0, min_level_db=-12.0)


def settings(**changes):
    s, warnings = turnon.Settings.from_config(config(**changes))
    assert warnings == []
    return s


# ---------------------------------------------------------------------- settings

def test_defaults_are_off_record_only_and_without_warnings():
    s = settings()
    assert not s.enabled and not s.blocking and not s.verified and not s.blocks and s.expected() is None
    assert s.max_tx_gain == 40.0 and s.pulses == 15000 and s.analysis_command[-1].endswith("measure.py")


def test_blocking_needs_verified_expected_values():
    s, warnings = turnon.Settings.from_config(config(enabled=True, blocking=True))
    assert s.blocking and not s.blocks and "only records until they are set" in warnings[0]
    s = settings(enabled=True, blocking=True, **VERIFIED)
    assert s.blocks and s.expected() == {"leakage_sample": 78.4, "delay_samples": 0.0, "tolerance_samples": 1.5,
                                          "min_separation_samples": 4.0}


@pytest.mark.parametrize("bad", [dict(leakage_sample=0.0), dict(level_tx_gain=0.0), dict(min_level_db=0.0),
                                 dict(delay_samples=-1.0)])
def test_verified_with_unusable_expected_values_only_records(bad):
    s, warnings = turnon.Settings.from_config(config(blocking=True, **dict(VERIFIED, **bad)))
    assert not s.verified and not s.blocks and "marked verified but needs" in warnings[0]


@pytest.mark.parametrize("name,value", [("max_tx_gain", -1.0), ("pulses", 0), ("analysis_command", []),
                                        ("analysis_timeout_s", 0.5), ("clip_fs", 0.0), ("min_current_readings", 0),
                                        ("tolerance_samples", 0.0), ("current_max_a", 0.15)])
def test_a_setting_that_cannot_work_is_refused_and_the_default_used(name, value):
    s, warnings = turnon.Settings.from_config(config(**{name: value}))
    default = settings()
    assert getattr(s, name) == getattr(default, name) and name in warnings[0]


def test_a_tolerance_that_reaches_the_leakage_is_refused_in_two_peak_mode():
    # With tolerance 5 and delay 4 the leakage peak itself lies within the direct peak's window.
    for tolerance in (5.0, 4.0):
        s, warnings = turnon.Settings.from_config(config(**dict(VERIFIED, delay_samples=4.0,
                                                                tolerance_samples=tolerance)))
        assert not s.verified and "tolerance_samples must be below delay_samples" in warnings[0]
    assert settings(**dict(VERIFIED, delay_samples=4.0, tolerance_samples=1.5)).verified
    assert settings(**dict(VERIFIED, delay_samples=2.0, tolerance_samples=3.0)).verified    # combined: no matter


def test_a_low_power_cap_not_below_the_radars_cap_is_a_warning():
    c = config()
    c["manager"]["limits"]["max_tx_gain"] = 40.0
    _, warnings = turnon.Settings.from_config(c)
    assert "max_tx_gain (40) is not below manager.limits.max_tx_gain (40)" in warnings[0]


def test_limits_that_cross_are_refused_together():
    s, warnings = turnon.Settings.from_config(config(idle_current_min_a=0.3, idle_current_max_a=0.2))
    assert (s.idle_current_min_a, s.idle_current_max_a) == (0.10, 0.20) and "must be below" in warnings[0]


def test_logic_counts_two_sdr_starts_per_start_with_the_check():
    m = config(enabled=True)["manager"]
    cfg, _ = L.LogicConfig.from_config(m)
    assert cfg.sdr_starts_per_run == 2
    assert L.LogicConfig.from_config(config()["manager"])[0].sdr_starts_per_run == 1
    logic = L.ManagerLogic(L.LogicConfig(max_starts_per_hour=5, sdr_starts_per_run=2,
                                         min_start_gap_s=0.0, retry_delays_s=(5.0,)))
    good = {"gate": L.GateResult()}
    logic.gates(0.0, good)
    for i in range(2):
        logic.start(i * 10.0, L.COMMAND)
        logic.started(i * 10.0 + 1)
        logic.stop(i * 10.0 + 2, L.COMMAND)
        logic.ended(i * 10.0 + 3, {"requested": True, "amp_safe": True})
    assert len(logic.starts) == 4
    logic.start(30.0, L.COMMAND)        # a fifth and sixth SDR start would pass the limit of 5
    assert logic.fault["kind"] == "start_limit"


# ------------------------------------------------------------- low-power config

def collection_config():
    """The bench config as the runner derives it for a collection."""
    with open(BENCH) as f:
        requested = limits.load(f.read())
    return limits.derive(requested, "../nvme/c/b00001_s001_x", 1500000)


def test_low_power_config_caps_the_gain_and_counts_whole_records():
    derived = collection_config()
    lp, facts = turnon.low_power_config(derived, 40.0, 8000, "../nvme/c/b00001_s001_x/turn_on_check")
    assert lp["RF0"]["tx_gain"] == 40.0 and facts["requested_tx_gain"] == 72.0
    assert lp["CHIRP"]["num_pulses"] == 8000 == lp["FILES"]["max_chirps_per_file"] and facts["records"] == 8
    assert lp["FILES"]["save_loc"] == turnon.DATA_FILE and lp["FILES"]["output_dir"].endswith("turn_on_check")
    assert turnon.differences(derived, lp) == turnon.CHANGED
    assert facts["time_offset_s"] == 1.0 and facts["pri_s"] == 0.0002
    # A pulse count that is not a whole number of records is rounded up; a lower requested gain is kept.
    derived["RF0"]["tx_gain"] = 30.0
    lp, facts = turnon.low_power_config(derived, 40.0, 1500, "x")
    assert lp["RF0"]["tx_gain"] == 30.0 and facts["pulses"] == 2000 and facts["records"] == 2
    # The waveform is the collection's: chirp, rates, frequency, durations, presums, dithering.
    for section in ("GENERATE", "DEVICE", "GPIO", "RF1"):
        assert lp[section] == derived[section]


def test_low_power_config_passes_the_same_limits(tmp_path):
    derived = collection_config()
    lp, _ = turnon.low_power_config(derived, 40.0, 8000, "../nvme/c/b00001_s001_x/turn_on_check")
    lim = dict(manager_config.DEFAULTS["manager"]["limits"], max_tx_gain=72.0)
    assert limits.check(lp, lim) == []


def test_differences_finds_any_other_change():
    derived = collection_config()
    lp, _ = turnon.low_power_config(derived, 40.0, 8000, "x")
    lp["CHIRP"]["tx_duration"] = 2e-5
    lp["EXTRA"] = 1
    assert turnon.differences(derived, lp) - turnon.CHANGED == {("CHIRP", "tx_duration"), ("EXTRA", "")}


def test_low_power_config_needs_the_numbers():
    derived = collection_config()
    del derived["CHIRP"]["pulse_rep_int"]
    with pytest.raises(ValueError):
        turnon.low_power_config(derived, 40.0, 8000, "x")


# ------------------------------------------------------------------------ judgement

FACTS = {"tx_gain": 40.0, "requested_tx_gain": 70.0, "pulses": 8000, "records": 8, "time_offset_s": 1.0,
         "pri_s": 0.0002}
IDLE = ({"uptime": 100.0, "current_a": 0.156, "voltage_v": 28.0, "temp_c": 27.6, "case_temp_c": 19.2}, "")


def during(*currents):
    return [{"uptime": 101.5 + 0.5 * i, "current_a": c, "voltage_v": 28.0} for i, c in enumerate(currents)]


def measurement(level=-36.0, sample=78.4, found=True, raw=0.016, records=8, mode="combined"):
    peak = {"index": int(sample), "sample": sample, "level_db": level}
    out = {"records": records, "records_expected": 8, "record_samples": 2352, "raw_max_fs": raw, "clip_fs": 0.9,
           "clipped_samples": 0, "noise_floor_db": -105.0, "strongest": peak, "peaks": [peak], "mode": mode}
    out["direct"] = dict(peak, expected=78.4, tolerance=1.5, found=found,
                         window_max={"index": 76, "sample": 76.0, "level_db": -70.0})
    return out


def judged(s=None, before=IDLE, override=False, readings=None, m=None, error=""):
    s = s or settings(**VERIFIED)
    return turnon.judge(s, FACTS, before, override, during(0.288, 0.264, 0.3) if readings is None else readings,
                        measurement() if m is None and not error else m, error)


def criterion(result, name):
    return next(c for c in result["criteria"] if c["name"] == name)


def failing(result):
    return [c["name"] for c in result["criteria"] if c["ok"] is False]


def test_a_good_check_passes_with_the_level_scaled_to_the_reference_gain():
    r = judged()
    assert r["passed"] and not r["blocked"] and failing(r) == [] and r["mode"] == "combined"
    assert "-6.0 dB at tx_gain 70" in criterion(r, "signal")["text"]      # -36 at 40 is -6 at 70
    assert r["measured"]["current_max_a"] == 0.3 and r["measured"]["raw_max_at_requested_fs"] == 0.506
    assert turnon.summary_text(r).startswith("passed: combined peak at sample 78.4")


def test_not_verified_judges_everything_but_the_signal():
    r = judged(s=settings())
    assert r["passed"] and criterion(r, "signal")["ok"] is None and r["mode"] == "none"
    assert "not judged" in criterion(r, "signal")["text"] and "-6.0 dB at tx_gain 70" in criterion(r, "signal")["text"]
    r = judged(s=settings(), readings=during(0.156, 0.156))
    assert not r["passed"] and not r["blocked"] and failing(r) == ["amplifier_during"]
    assert turnon.summary_text(r).startswith("FAILED (record only")


@pytest.mark.parametrize("change,name,words", [
    (dict(voltage_v=25.0), "amplifier_before", "28 V rail at 25.0 V"),
    (dict(current_a=0.39), "amplifier_before", "idle current 0.390 A"),
    (dict(voltage_v=30.6), "amplifier_before", "28 V rail at 30.6 V"),
    (dict(temp_c=55.0), "amplifier_before", "amplifier temperature 55.0 C"),
    (dict(case_temp_c=56.0), "amplifier_before", "case temperature 56.0 C"),
    (dict(temp_c=None), "amplifier_before", "no amplifier temperature"),
])
def test_amplifier_before_the_run(change, name, words):
    r = judged(s=settings(blocking=True, **VERIFIED), before=(dict(IDLE[0], **change), ""))
    assert failing(r)[0] == name and words in r["reason"] and r["blocked"] and r["retry"]
    assert turnon.summary_text(r).startswith("FAILED, the start is tried again")


@pytest.mark.parametrize("change", [dict(case_temp_c=-45.0), dict(temp_c=-41.0, case_temp_c=-48.0),
                                    dict(temp_c=54.0, case_temp_c=54.5), dict(voltage_v=26.0)])
def test_amplifier_before_is_never_stricter_than_the_safety_watcher(change):
    # The watcher stops at 55 C and outside 26-30.5 V, and has no cold limit: a cold start is no fault.
    assert judged(s=settings(blocking=True, **VERIFIED), before=(dict(IDLE[0], **change), ""))["passed"]


def test_the_watchers_limits_are_used():
    c = config(**VERIFIED)
    c["watcher"].update(temp_trip_c=45.0, voltage_min_v=27.0)
    s, _ = turnon.Settings.from_config(c)
    assert failing(turnon.judge(s, FACTS, (dict(IDLE[0], temp_c=46.0), ""), False, during(0.3, 0.3),
                                measurement())) == ["amplifier_before"]
    assert failing(turnon.judge(s, FACTS, (dict(IDLE[0], voltage_v=26.5), ""), False, during(0.3, 0.3),
                                measurement())) == ["amplifier_before"]


def test_only_the_signal_clipping_and_data_raise_the_fault():
    s = settings(blocking=True, **VERIFIED)
    assert judged(s=s, readings=during(0.156, 0.16))["retry"]               # amplifier not enabled: retried
    for r in (judged(s=s, m=measurement(found=False)), judged(s=s, m=measurement(raw=0.95)),
              judged(s=s, m=measurement(records=4))):
        assert r["blocked"] and not r["retry"]
        assert turnon.summary_text(r).startswith("FAILED, the radar stays off")
    assert judged(s=s, error="no result within 30 s")["retry"]


def test_no_reading_before_fails_unless_the_override_is_set():
    r = judged(before=(None, "status missing"))
    assert "amplifier_before" in failing(r) and "status missing" in r["reason"]
    r = judged(before=(None, "status missing"), readings=[], override=True)
    assert r["passed"] and criterion(r, "amplifier_before")["ok"] is None
    assert criterion(r, "amplifier_during")["ok"] is None


def test_amplifier_current_during_the_pulses():
    assert failing(judged(readings=during(0.156, 0.168))) == ["amplifier_during"]     # no rise: not enabled
    assert "did not rise" in judged(readings=during(0.156, 0.168))["reason"]
    assert failing(judged(readings=during(0.3, 0.9))) == ["amplifier_during"]         # far too much
    r = judged(readings=during(0.3))                                                   # too few readings
    assert r["passed"] and criterion(r, "amplifier_during")["ok"] is None
    assert "only 1 amplifier readings during the pulses" in r["warnings"][0]
    assert "; warning: only 1 amplifier readings" in turnon.summary_text(r)
    assert judged(readings=during(0.156, 0.288))["passed"]     # a reading between pulses does not matter


def test_data_clipping_and_analysis():
    assert failing(judged(m=measurement(records=4))) == ["data"]
    r = judged(m=measurement(raw=0.95))
    assert failing(r) == ["clipping"] and "clipping" in r["reason"]
    r = judged(error="turn-on check analysis: no result within 30 s")
    assert failing(r) == ["analysis"] and "no result within 30 s" in r["reason"]
    r = judged(m={"records": 0, "records_expected": 8, "error": "no complete record in the data"})
    assert failing(r) == ["analysis"] and "no complete record" in r["reason"]


def test_a_measurement_that_is_not_a_number_is_a_failed_analysis():
    r = judged(m=measurement(raw=float("nan")))
    assert failing(r) == ["analysis"] and "not a finite number" in r["reason"]
    r = judged(m=measurement(level=float("nan")))
    assert failing(r) == ["analysis"]


def test_a_low_power_run_at_the_requested_gain_is_noted():
    r = turnon.judge(settings(**VERIFIED), dict(FACTS, requested_tx_gain=40.0), IDLE, False,
                     during(0.288, 0.3), measurement())
    assert any("at the requested tx_gain 40: it is not at low power" in w for w in r["warnings"])


def test_clipping_at_the_requested_power_is_a_warning():
    r = judged(m=measurement(raw=0.03))         # 0.03 at tx_gain 40 is about 0.95 at 70
    assert r["passed"] and "would clip" in r["warnings"][0]


def test_pulses_too_short_for_the_readings_are_a_warning():
    s = settings(**VERIFIED)
    short = dict(FACTS, pulses=4000, telemetry_interval_s=0.5)         # 0.8 s
    r = turnon.judge(s, short, IDLE, False, during(0.288, 0.264), measurement())
    assert r["passed"] and "too short for 2 amplifier readings at one every 0.5 s" in r["warnings"][0]
    assert "at least 1.4 s" in r["warnings"][0]
    assert judged()["warnings"] == []                                   # 1.6 s is enough


def test_signal_missing_or_too_weak():
    r = judged(m=measurement(found=False))
    assert failing(r) == ["signal"] and "no combined peak at sample 78.4 +/- 1.5" in r["reason"]
    assert "antenna or cable disconnected?" in r["reason"]
    r = judged(m=measurement(level=-50.0))     # -20 at 70
    assert failing(r) == ["signal"] and "-20.0 dB at tx_gain 70 below the minimum of -12" in r["reason"]
    r = judged(m=measurement(level=-41.9))     # -11.9 at 70: just above
    assert r["passed"]


def test_two_peak_mode_is_named():
    s = settings(**dict(VERIFIED, leakage_sample=75.0, delay_samples=15.2))
    r = judged(s=s, m=measurement(sample=90.2, mode="two_peak"))
    assert r["mode"] == "two_peak" and "direct peak at sample 90.2" in criterion(r, "signal")["text"]


# ------------------------------------------------------------------------- radarctl

def test_radarctl_status_shows_the_check():
    s = {"state": "fault", "fault": {"kind": "turn_on_check", "reason": "turn-on check failed: no peak"},
         "radar": {"active": True, "collection": "b1", "pid": 5, "chunks_closed": 0,
                   "turn_on_check": "low-power radar running"},
         "turn_on_check": {"enabled": True, "blocks": True, "verified": True,
                           "last": {"text": "FAILED, the radar stays off: no peak", "collection": "b00001_s001_x"}}}
    text = format_status(s)
    assert "turn-on:    low-power radar running" in text
    assert "turn-on:    check blocking; last: FAILED, the radar stays off: no peak (b00001_s001_x)" in text
    s["turn_on_check"] = {"enabled": True, "blocks": False, "verified": False, "last": None}
    assert "check record only, expected peak not set; last: none since the manager started" in format_status(s)
    s["turn_on_check"] = {"enabled": False}
    assert "turn-on:    check" not in format_status(s)
