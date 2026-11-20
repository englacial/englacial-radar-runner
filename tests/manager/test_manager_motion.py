"""The GPS motion trigger (manager/motion.py) with injected fixes, in the shape of telemetry.json."""
import time

import pytest

from radar_runner.common import status as S
from radar_runner.manager import motion as M
from radar_runner.manager import radarctl

T0 = 1795136400.0       # receiver time of the first fix: 2026-11-20T01:00:00Z


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(round(t % 1 * 1000)) % 1000:03d}Z"


def status(t, speed, mode=3, eph=1.0, health="ok", age=0.3):
    """What the trigger reads: the telemetry status file with the gps source as telemetry/service.py writes it."""
    values = {"gps_time": iso(t), "mode": mode, "speed_mps": speed, "eph_m": eph, "lat": -79.5, "lon": -112.0}
    return S.StatusRead(S.OK, {"sources": {"gps": {"health": health, "reason": "", "new_fix_age_s": age,
                                                   "values": values}}}, 0.2)


class Drive:
    """Fixes once a second of receiver time; the trigger looks twice per fix, as the manager does."""

    def __init__(self, cfg=None):
        self.motion = M.Motion(cfg or M.MotionConfig())
        self.t = T0             # receiver time
        self.now = 1000.0       # the Pi's monotonic clock
        self.states = []

    def fixes(self, seconds, speed, **kw):
        for _ in range(int(seconds)):
            for _ in range(2):
                self.states.append(self.motion.update(self.now, status(self.t, speed, **kw)))
                self.now += 0.5
            self.t += 1.0
        return self.motion.state

    def nothing(self, seconds, read=None):
        """No new fix: the same file again (gps stale) or nothing readable."""
        for _ in range(int(2 * seconds)):
            self.states.append(self.motion.update(self.now, read))
            self.now += 0.5
        return self.motion.state

    def changes(self):
        out = []
        for s in self.states:
            if not out or out[-1] != s:
                out.append(s)
        return out


def test_defaults_match_the_config_file():
    from radar_runner.common import config as C
    cfg, warnings = M.MotionConfig.from_config(C.DEFAULTS["manager"]["motion"])
    assert cfg == M.MotionConfig() and warnings == []


def test_a_drive_is_moving_after_start_s_and_stationary_after_stop_s():
    d = Drive()
    assert d.fixes(150, 0.02) == M.STATIONARY                # parked at the start
    d.fixes(20, 4.0)
    assert d.motion.state == M.STATIONARY                    # 19 s of receiver time: not yet
    assert d.fixes(2, 4.0) == M.MOVING
    assert d.fixes(600, 4.0) == M.MOVING
    assert d.fixes(119, 0.0) == M.MOVING                     # stopped: 118 s counted
    assert d.fixes(3, 0.0) == M.STATIONARY
    assert d.changes() == [M.UNKNOWN, M.STATIONARY, M.MOVING, M.STATIONARY]


def test_at_boot_it_says_unknown_until_a_full_count():
    d = Drive()
    assert d.fixes(20, 5.0) == M.UNKNOWN
    assert "5.0 m/s for 19 of 20 s" in d.motion.reason
    assert d.fixes(1, 5.0) == M.MOVING


def test_parked_noise_like_the_bench_never_says_moving():
    """Indoors on 2026-11-19/20: stretches of up to 11 s above 2 m/s, up to 10 m/s for a moment."""
    d = Drive()
    d.fixes(130, 0.05)
    for _ in range(30):
        d.fixes(11, 6.6)
        d.fixes(1, 1.2)
        d.fixes(9, 3.0, eph=4.9)
        d.fixes(2, 0.3)
        d.fixes(1, 10.2)
        d.fixes(30, 0.1)
    assert M.MOVING not in d.states


def test_poor_fixes_do_not_count_and_never_change_a_decision_by_themselves():
    d = Drive()
    d.fixes(25, 4.0)
    assert d.motion.state == M.MOVING
    for kw in ({"mode": 2}, {"eph": 7.5}, {"health": "stale"}, {"mode": 1, "speed": None}):
        speed = kw.pop("speed", 0.0)
        assert d.fixes(3, speed, **kw) == M.MOVING           # shorter than stale_s: the decision holds
        d.fixes(2, 4.0)
    # A truck that parks while the fix is poor: the stop is only counted from good fixes.
    assert d.fixes(300, 0.0, eph=8.0) == M.UNKNOWN
    assert d.fixes(119, 0.0) == M.UNKNOWN
    assert d.fixes(2, 0.0) == M.STATIONARY


def test_loss_of_fix_or_stale_data_is_unknown_and_restarts_the_counts():
    d = Drive()
    d.fixes(25, 4.0)
    assert d.nothing(3) == M.MOVING                          # less than stale_s without a new fix
    assert d.nothing(2) == M.UNKNOWN
    assert "nothing that counts for 5 s" in d.motion.reason
    d.fixes(19, 4.0)
    assert d.motion.state == M.UNKNOWN                       # a full count again
    d.fixes(2, 4.0)
    assert d.motion.state == M.MOVING
    assert d.nothing(10, read=S.StatusRead(S.STALE, None, 12.0, "12.0 s old")) == M.UNKNOWN
    assert d.motion.problem == "telemetry status: 12.0 s old"


def test_a_fix_that_arrives_old_does_not_count():
    d = Drive()
    d.fixes(30, 4.0, age=6.0)
    assert d.motion.state == M.UNKNOWN and d.motion.problem == "fix 6 s old"


def test_a_gap_between_fixes_restarts_the_count():
    d = Drive()
    d.fixes(15, 4.0)
    d.t += 4.0                                              # 5 s of receiver time between two fixes
    d.fixes(15, 4.0)
    assert d.motion.state == M.UNKNOWN
    d.fixes(6, 4.0)
    assert d.motion.state == M.MOVING


def test_creeping_between_the_thresholds_keeps_the_last_decision():
    d = Drive()
    d.fixes(130, 0.0)
    assert d.fixes(600, 1.5) == M.STATIONARY                 # creeping after a stop: no start
    d.fixes(25, 3.0)
    assert d.fixes(600, 1.0) == M.MOVING                     # slow after driving: no stop
    for _ in range(20):                                     # stop-and-go: never stationary for long enough
        d.fixes(60, 0.0)
        d.fixes(5, 1.0)
    assert d.motion.state == M.MOVING


def test_settings_below_their_limits_are_refused():
    cfg, warnings = M.MotionConfig.from_config({"start_speed_mps": 0.3, "start_s": 1.0, "stop_s": 5.0,
                                                "stop_speed_mps": 0.2, "min_mode": 1, "max_eph_m": 0.0,
                                                "max_gap_s": 0.1, "stale_s": 0.5})
    assert cfg == M.MotionConfig(stop_speed_mps=0.2)
    assert len(warnings) == 7 and all("the default is used" in w for w in warnings)
    cfg, warnings = M.MotionConfig.from_config(dict(vars(M.MotionConfig()), stop_speed_mps=3.0))
    assert cfg == M.MotionConfig() and warnings == ["manager.motion.stop_speed_mps must be at least 0 and below "
                                                    "start_speed_mps: the default is used"]
    cfg, _ = M.MotionConfig.from_config(dict(vars(M.MotionConfig()), stop_s=1.0), minimums={"stop_s": 0.5})
    assert cfg.stop_s == 1.0                                # tests may lower the limits


@pytest.mark.parametrize("gap, why", [
    (600.0, "must be below the smaller of start_s and stop_s"), (20.0, "must be below the smaller of start_s and stop_s"),
    (6.0, "must be at most stale_s")])
def test_a_gap_limit_that_would_bridge_a_count_is_refused(gap, why):
    cfg, warnings = M.MotionConfig.from_config(dict(vars(M.MotionConfig()), max_gap_s=gap))
    assert cfg.max_gap_s == 3.0 and len(warnings) == 1 and why in warnings[0]


def test_the_counts_start_again_when_the_answer_becomes_unknown():
    d = Drive()
    d.fixes(19, 4.0)                                        # 18 s of receiver time counted
    stale = status(d.t - 1, 4.0)                            # the same fix again and again: nothing new
    for _ in range(12):
        d.states.append(d.motion.update(d.now, stale))
        d.now += 0.5
    assert d.motion.state == M.UNKNOWN
    d.t += 1.0                                              # the next fix, 2 s after the last one
    assert d.fixes(1, 4.0) == M.UNKNOWN                     # not "moving" on the strength of the old count
    assert d.fixes(20, 4.0) == M.MOVING


@pytest.mark.parametrize("data", [{"sources": []}, {"sources": {"gps": "x"}}, {"sources": {"gps": {"values": 5}}},
                                  {"sources": {"gps": {"health": "ok", "values": []}}}, []])
def test_a_status_of_the_wrong_shape_is_no_fix(data):
    d = Drive()
    d.fixes(25, 4.0)
    odd = S.StatusRead(S.OK, data, 0.2)
    for _ in range(12):
        d.motion.update(d.now, odd)
        d.now += 0.5
    assert d.motion.state == M.UNKNOWN


@pytest.mark.parametrize("text, expected", [
    ("2026-11-20T01:00:00.000Z", T0), ("2026-11-20T01:00:00Z", T0), ("2026-11-20T01:00:00.25Z", T0 + 0.25),
    ("", None), (None, None), ("2026-11-20 01:00:00", None), ("garbage Z", None)])
def test_fix_time(text, expected):
    assert M.fix_time(text) == expected


# ------------------------------------------------------------------ in the manager

def test_disabled_nothing_of_it_runs_and_the_status_says_so(env):
    from test_manager_service import Running
    mgr = Running(env)
    assert mgr.manager.motion is None
    mgr.start()
    try:
        status = mgr.wait_state("ready")
    finally:
        mgr.stop()
    assert status["motion"] == {"enabled": False} and status["auto"]["enabled"] is False
    assert "auto:" not in radarctl.format_status(status)


def test_enabled_the_status_shows_what_it_says_and_why(env):
    from test_manager_service import Running
    env.config["manager"]["triggers"]["gps"] = True
    env.config["manager"]["motion"]["start_s"] = 1.0           # refused: below 5 s
    mgr = Running(env).start()
    try:
        status = mgr.wait_state("ready")
    finally:
        mgr.stop()
    motion = status["motion"]
    assert motion["enabled"] is True and motion["state"] == "unknown" and motion["settings"]["start_s"] == 20.0
    assert motion["reason"] == "gps: not in the status"         # the stand-in telemetry file has no GPS
    assert "manager.motion.start_s must be at least 5: the default is used" in status["config_warnings"]
    assert "auto:       gps says unknown (gps: not in the status)" in radarctl.format_status(status)


def test_status_text_shows_the_newest_fix():
    text = radarctl.format_status({"auto": {"enabled": True, "source": "gps", "state": "stationary", "blocked": True},
                                   "motion": {"enabled": True, "state": "stationary", "reason": "at most 0.5 m/s for 120 s",
                                              "problem": "", "counting": "4.0 m/s for 7 of 20 s"}})
    assert "gps says stationary (at most 0.5 m/s for 120 s), blocked until stationary" in text
    assert "newest fix: 4.0 m/s for 7 of 20 s" in text
