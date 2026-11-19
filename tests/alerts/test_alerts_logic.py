"""The decisions of the alerts service (radar_runner/alerts/logic.py), in simulated time."""
import copy

import pytest

from radar_runner.alerts import logic as L
from radar_runner.common import config as C
from radar_runner.common.status import StatusRead

COND = dict(C.DEFAULTS["alerts"]["conditions"], sd_free_gb_min=C.DEFAULTS["alerts"]["disk"]["sd_free_gb_min"])


# ---- positions and secrets never leave the Pi

@pytest.mark.parametrize("text, gone", [
    ("fix at lat -77.8463 lon 166.6683", ["-77.8463", "166.6683", "77.8463"]),
    ("latitude: -77.84, longitude: 166.67", ["77.84", "166.67"]),
    ("position=-77.846,166.668 ok", ["77.846", "166.668"]),
    ("gps_Lat 378418007 gps_Long -1222910377", ["378418007", "1222910377"]),
    ("OLA row 378418007.0,-1222910377.0", ["378418007", "1222910377"]),
    ("we are at -77.8463, 166.6683 now", ["77.8463", "166.6683"]),
    ("77°50'47\"S 166°40'06\"E", ["77°50", "166°40"]),
    ("77.85S 166.67E", ["77.85", "166.67"]),
    ("37.8418011 -122.2910401", ["37.8418011", "122.2910401"]),
    ("coordinates (-77.846; 166.668)", ["77.846", "166.668"]),
    ("easting 538123 northing 1350211", ["538123", "1350211"]),
])
def test_positions_are_removed(text, gone):
    out = L.scrub(text)
    for piece in gone:
        assert piece not in out, (text, out)
    assert L.REMOVED in out


@pytest.mark.parametrize("text", [
    "amplifier temperature 56.2 C, case 41.0 C, 2.58 A, 27.9 V",
    "AMPLIFIER STUCK ENABLED: 2.51 A with no radar process after 3 runs of the reset tool",
    "Last collection: b00005_s001_20261119T185630Z, stopped (stop by command)",
    "962.8 GB free on the data drive",
    "the radar may not run: amplifier 28 V absent (1.1 V)",
    "Pi SoC 81.5 C, now: throttled",
    "uptime 109 min, boot 5",
])
def test_ordinary_alert_text_is_left_as_it_is(text):
    assert L.scrub(text) == text


def test_secrets_are_removed():
    secrets = ["https://ntfy.example.org", "radar-topic-x93", "tk_abcdefghijklmnopqrstuvwxyz123"]
    text = ("posting to https://ntfy.example.org/radar-topic-x93 with tk_abcdefghijklmnopqrstuvwxyz123; "
            "Authorization: Bearer tk_other1234567 token=hunter22 password: swordfish "
            "https://user:pw@host.example/x")
    out = L.scrub(text, secrets)
    for piece in ("ntfy.example.org", "radar-topic-x93", "tk_abc", "tk_other", "hunter22", "swordfish", "user:pw"):
        assert piece not in out, out
    assert "token=" in out and "password: " in out


def test_control_characters_are_flattened_and_long_text_is_clipped():
    assert L.scrub("a\x00b\x1bc\nd") == "a b c\nd"
    assert L.clip("x" * 50, 10) == "x" * 7 + "..." and L.clip("short", 10) == "short"


# ---- priorities of the events

def watcher(event, severity, text="t", **fields):
    return {"time_utc": "2026-11-19T20:00:00Z", "uptime_s": 500.0, "event": event, "severity": severity,
            "text": text, **fields}


def test_watcher_severities_map_to_ntfy_priorities():
    over_temp = L.watcher_alert(watcher("trip", "urgent", "amplifier temperature: reached 55 C", condition="amp_temp"))
    assert over_temp.priority == L.URGENT == 5 and "URGENT" in over_temp.title
    stuck = L.watcher_alert(watcher("stuck", "urgent", "AMPLIFIER STUCK ENABLED: 2.5 A"))
    assert stuck.priority == L.URGENT and stuck.title == "AMPLIFIER STUCK ENABLED"
    assert L.watcher_alert(watcher("blocked", "urgent")).priority == L.URGENT
    assert L.watcher_alert(watcher("radar_kill", "urgent")).priority == L.URGENT
    assert L.watcher_alert(watcher("radar_kill", "warning")).priority == L.DEFAULT
    enabled = L.watcher_alert(watcher("trip", "warning", condition="amp_enabled"))
    assert enabled.priority == L.DEFAULT and "amplifier enabled" in enabled.title
    assert L.watcher_alert(watcher("warning", "warning", warning="amp_temp_high")).key == "watcher.warning.amp_temp_high"
    assert L.watcher_alert(watcher("clear", "info", condition="amp_temp")).priority == L.LOW
    assert L.watcher_alert(watcher("reset_tool_result", "warning")).priority == L.DEFAULT
    assert L.watcher_alert(watcher("reset_tool_result", "info")) is None


@pytest.mark.parametrize("name", ["not_ok", "ok", "start", "stop", "unpowered", "powered", "reset_tool", "radar_stopped"])
def test_routine_watcher_events_are_not_alerts(name):
    assert L.watcher_alert(watcher(name, "warning" if name == "not_ok" else "info")) is None


def test_a_supply_trip_during_a_power_loss_is_not_an_alert():
    trip = watcher("trip", "warning", "28 V supply: below 26 V", condition="voltage")
    assert L.watcher_alert(trip, power_loss=True) is None
    assert L.watcher_alert(trip, power_loss=False).priority == L.DEFAULT
    # Only the supply: an over-temperature trip is urgent whatever the power does.
    assert L.watcher_alert(watcher("trip", "urgent", condition="amp_temp"), power_loss=True).priority == L.URGENT


def test_a_restart_without_a_clean_stop_in_the_same_boot_is_reported():
    start = watcher("start", "info", "safety watcher started", uptime_s=900.0)
    assert L.watcher_alert(start, {"event": "trip", "uptime_s": 800.0}).key == "watcher.restart"
    assert L.watcher_alert(start, {"event": "stop", "uptime_s": 800.0}) is None         # stopped cleanly
    assert L.watcher_alert(start, {"event": "trip", "uptime_s": 30000.0}) is None       # earlier boot: a power cut
    assert L.watcher_alert(start, None) is None
    started = {"event": "manager_started", "uptime_s": 900.0}
    assert L.manager_alert(started, {"event": "state", "uptime_s": 800.0}).key == "manager.restart"
    assert L.manager_alert(started, {"event": "manager_stopped", "uptime_s": 800.0}) is None


def test_manager_faults_are_high_and_failed_runs_low():
    fault = L.manager_alert({"event": "fault", "kind": "start_failures", "reason": "build failed"})
    assert fault.priority == L.HIGH and "start_failures" in fault.title and "build failed" in fault.message
    cleared = L.manager_alert({"event": "fault_cleared", "kind": "start_failures", "by": "button"})
    assert cleared.priority == L.LOW and cleared.clears == fault.key
    failed = L.manager_alert({"event": "ended", "failure": "radar exited with status 1", "collection": "b00005_s002_x"})
    assert failed.priority == L.LOW
    assert L.manager_alert({"event": "ended", "failure": ""}) is None
    for name in ("state", "request", "start", "stop", "gate", "collection", "manager_stopped", "sdr_state"):
        assert L.manager_alert({"event": name}) is None


def ended(errors, attempted, failure=""):
    # As in the bench's manager-events.jsonl on 2026-11-19.
    return {"event": "ended", "failure": failure, "collection": "b00004_s001_20261119T165343Z", "output_tail": [
        "[RX] Closing output file.", f"[RX] Error count: {errors}", "[RX] Total pulses written: 574000",
        f"[RX] Total pulses attempted: {attempted}", "[RX] Done. Calling join_all() on transmit thread group.", ""]}


def test_a_run_with_a_high_error_rate_is_a_warning():
    assert L.error_rate(ended(13767, 588522)) == (13767, 588522)
    assert L.manager_alert(ended(13767, 588522), error_rate_warn=0.05) is None          # 2.3 %: the bench baseline
    high = L.manager_alert(ended(60000, 588522), error_rate_warn=0.05)
    assert high.priority == L.DEFAULT and "10.2 %" in high.message and high.key == "manager.error_rate"
    assert L.manager_alert(ended(60000, 588522)) is None                                # not asked for
    assert L.manager_alert(ended(60000, 588522, failure="killed"), error_rate_warn=0.05).key == "manager.ended"
    for tail in (None, "text", [], ["[RX] Error count: 5"], ["[RX] Error count: 5", "[RX] Total pulses attempted: 0"]):
        assert L.error_rate({"event": "ended", "output_tail": tail}) is None


def test_events_with_odd_fields_do_not_raise():
    for event in ({}, {"event": None}, {"event": "trip", "condition": 5, "severity": "nonsense"},
                  {"event": "fault", "kind": ["x"], "reason": {"a": 1}}, {"event": "start", "uptime_s": "x"}):
        L.watcher_alert(event, {"event": "x", "uptime_s": "y"})
        L.manager_alert(event, {"event": "x"})


# ---- conditions from the status files

def ok(data):
    return StatusRead("ok", data, 0.5)


def telemetry(**changes):
    d = {"state": "ok", "problems": [], "boot_count": 7, "clock": {"synced": 1},
         "sources": {"gps": {"health": "ok", "reason": "", "values": {"lat": -77.8463, "lon": 166.6683}},
                     "pi": {"health": "ok", "values": {"soc_temp_c": 55.0, "sd_free_gb": 90.0, "nvme_free_gb": 900.0},
                            "throttled_now": []}},
         "logging": {"location": "primary", "writer": "running"}}
    for path, value in changes.items():
        node = d
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return d


def reads(**overrides):
    base = {"telemetry": ok(telemetry()), "watcher": ok({"ok": True, "tripped": False, "reason": ""}),
            "manager": ok({"state": "ready", "not_ready": []}),
            "power": ok({"state": "normal", "mode": "armed", "warnings": {}})}
    base.update(overrides)
    return base


def test_a_condition_becomes_an_alert_only_after_its_delay_and_is_over_after_clear_s():
    c = L.Conditions(COND["clear_s"])
    bad = reads(telemetry=ok(telemetry(**{"sources.gps.health": "stale", "sources.gps.reason": "no fix time"})))
    t = 1000.0
    assert c.update(t, L.condition_states(bad, COND)) == []
    assert c.update(t + COND["gps_s"] - 1, L.condition_states(bad, COND)) == []
    [alert] = c.update(t + COND["gps_s"], L.condition_states(bad, COND))
    assert alert.key == "cond.gps" and alert.priority == L.DEFAULT and "no fix time" in alert.message
    assert c.update(t + COND["gps_s"] + 5, L.condition_states(bad, COND)) == []        # once
    good = reads()
    assert c.update(t + 2000, L.condition_states(good, COND)) == []
    [over] = c.update(t + 2000 + COND["clear_s"], L.condition_states(good, COND))
    assert over.priority == L.LOW and over.clears == "cond.gps"


def test_a_condition_shorter_than_its_delay_is_forgotten():
    c = L.Conditions(COND["clear_s"])
    bad = reads(watcher=ok({"ok": False, "reason": "no usable amplifier telemetry"}))
    assert c.update(0.0, L.condition_states(bad, COND)) == []
    assert c.update(20.0, L.condition_states(reads(), COND)) == [] and c.entries == {}


def test_unknown_sources_change_nothing():
    c = L.Conditions(COND["clear_s"])
    bad = reads(telemetry=ok(telemetry(**{"logging.location": "fallback"})))
    c.update(0.0, L.condition_states(bad, COND))
    assert c.update(COND["logging_fallback_s"], L.condition_states(bad, COND))
    # Telemetry gone: its own condition starts; the logging condition neither clears nor raises again.
    gone = reads(telemetry=StatusRead("missing", None, None, "no such file"))
    states = L.condition_states(gone, COND)
    assert "logging" not in states and states["telemetry_status"].active is True
    for t in range(0, 600, 10):
        c.update(1000.0 + t, states)
    assert c.entries["logging"]["raised"] and "telemetry_status" in c.active()


def test_service_status_files_and_their_priorities():
    stale = StatusRead("stale", {"ok": True}, 30.0, "30.0 s old, limit 5 s")
    states = L.condition_states(reads(watcher=stale, power=stale, telemetry=stale), COND)
    assert states["watcher_status"].priority == L.HIGH and states["power_status"].priority == L.HIGH
    assert states["telemetry_status"].priority == L.DEFAULT
    assert all(states[n].delay_s == COND["status_stale_s"] for n in ("watcher_status", "power_status"))
    stopped = StatusRead("stale", {"ok": False, "running": False}, 30.0, "old")
    assert "was stopped" in L.condition_states(reads(watcher=stopped), COND)["watcher_status"].text
    # The manager only once it has been seen in this boot (it is not always installed).
    missing = StatusRead("missing", None, None, "no such file")
    assert "manager_status" not in L.condition_states(reads(manager=missing), COND)
    assert L.condition_states(reads(manager=missing), COND, manager_seen=True)["manager_status"].active


def test_power_warnings_each_become_a_condition_and_go_away_with_the_warning():
    power = ok({"state": "normal", "warnings": {"current_unreadable": "current signal unreadable",
                                                "boot_loop_guard": "boot-loop guard: 3 early shutdowns"}})
    states = L.condition_states(reads(power=power), COND)
    assert states["power.current_unreadable"].priority == L.DEFAULT
    assert states["power.boot_loop_guard"].priority == L.HIGH
    later = L.condition_states(reads(), COND, tracked={"power.boot_loop_guard": {}})
    assert later["power.boot_loop_guard"].active is False
    unreadable = L.condition_states(reads(power=StatusRead("corrupt", None, None, "x")), COND,
                                    tracked={"power.boot_loop_guard": {}})
    assert unreadable["power.boot_loop_guard"].active is None


def test_pi_hot_or_throttling_and_sd_card_low():
    hot = L.condition_states(reads(telemetry=ok(telemetry(**{"sources.pi.values.soc_temp_c": 82.0}))), COND)
    assert hot["pi_hot"].active
    throttled = L.condition_states(reads(telemetry=ok(telemetry(**{"sources.pi.throttled_now": ["throttled"]}))), COND)
    assert throttled["pi_hot"].active and "throttled" in throttled["pi_hot"].text
    assert not L.condition_states(reads(), COND)["pi_hot"].active
    sd = L.condition_states(reads(telemetry=ok(telemetry(**{"sources.pi.values.sd_free_gb": 2.0}))), COND)
    assert sd["sd_low"].active


def test_not_ready_lists_the_reasons():
    man = ok({"state": "not_ready", "not_ready": ["sdr: no SDR on USB", "data: /mnt/nvme not mounted"]})
    s = L.condition_states(reads(manager=man), COND)["not_ready"]
    assert s.active and "no SDR on USB" in s.text and "/mnt/nvme" in s.text and s.delay_s == COND["not_ready_s"]


def test_condition_texts_carry_no_position():
    # The GPS condition is built from health and reason only; the readings are never used.
    bad = reads(telemetry=ok(telemetry(**{"sources.gps.health": "stale"})))
    for state in L.condition_states(bad, COND).values():
        assert "77.84" not in state.text and "166.66" not in state.text


# ---- free space on the data drive

def test_disk_levels_alert_once_per_level_and_rearm_above_it():
    d = L.DiskLevels([200, 100, 50, 25], 0.1)
    assert d.update(900.0) == []
    [a] = d.update(150.0)
    assert a.key == "disk.200" and a.priority == L.DEFAULT
    assert d.update(150.0) == [] and d.update(199.0) == []
    [a] = d.update(30.0)        # past two levels at once: one alert, for the lower
    assert a.key == "disk.50"
    [a] = d.update(20.0)
    assert a.key == "disk.25" and a.priority == L.HIGH
    assert d.update(26.0) == []                 # not yet 10 % above 25
    assert d.update(28.0) == [] and 25.0 not in d.crossed
    [a] = d.update(24.0)
    assert a.key == "disk.25"
    restored = L.DiskLevels([200, 100, 50, 25], 0.1, sorted(d.crossed))       # kept across a reboot
    assert restored.update(24.0) == []
    assert d.update(None) == [] and d.update("x") == []


# ---- the storm limit

def alert(key="watcher.trip.amp_temp", priority=L.URGENT, text="t"):
    return L.Alert(key, priority, "title " + key, text)


def test_a_flapping_alert_gives_at_most_burst_per_window_and_says_how_many_were_held_back():
    lim = L.Limiter(3, 3600.0, 30)
    passed = [lim.admit(100.0 + i, alert(text=f"n{i}")) for i in range(200)]
    assert passed.count(True) == 3 and passed[:3] == [True, True, True]
    assert lim.notes(200.0) == []                       # the window is still full
    [note] = lim.notes(100.0 + 3600.0)                  # the first of the three has left the window
    assert note.message.startswith("197 more alerts") and "n199" in note.message and note.priority == L.URGENT
    assert not note.limited
    assert lim.notes(100.0 + 3600.5) == []
    # Or the next one that passes carries the count.
    lim = L.Limiter(3, 3600.0, 30)
    for i in range(10):
        lim.admit(i, alert())
    a = alert()
    assert lim.admit(3601.5, a) and "7 more like this held back" in a.message


def test_the_limit_is_per_kind_and_urgent_ones_are_not_held_by_the_overall_limit():
    lim = L.Limiter(3, 3600.0, 5)
    assert all(lim.admit(1.0, alert(key=f"k{i}", priority=L.DEFAULT)) for i in range(5))
    assert not lim.admit(2.0, alert(key="k-new", priority=L.HIGH))          # 5 an hour in all
    assert lim.admit(2.0, alert(key="urgent-one", priority=L.URGENT))       # urgent: only its own limit
    assert lim.admit(3700.0, alert(key="k-new", priority=L.HIGH))


def test_unlimited_alerts_and_clears():
    lim = L.Limiter(1, 3600.0, 1)
    assert all(lim.admit(1.0, L.Alert("summary", L.DEFAULT, "s", "m", limited=False)) for _ in range(5))
    assert lim.admit(1.0, alert(key="a")) and not lim.admit(2.0, alert(key="a"))
    assert not lim.admit(3.0, L.Alert("a.over", L.LOW, "over", "m", clears="a"))     # its alert was held back
    lim = L.Limiter(1, 3600.0, 10)
    assert lim.admit(1.0, alert(key="b"))
    assert lim.admit(3.0, L.Alert("b.over", L.LOW, "over", "m", clears="b"))
    assert not lim.admit(4.0, L.Alert("c.over", L.LOW, "over", "m", clears="c"))      # never raised


def test_the_limit_depends_on_uptime_only_and_restarts_with_a_new_boot():
    lim = L.Limiter(3, 3600.0, 30)
    for i in range(5):
        lim.admit(100.0 + i, alert())
    saved = copy.deepcopy(lim.snapshot())
    same = L.Limiter(3, 3600.0, 30, saved, same_boot=True)          # a restart within the boot
    assert not same.admit(200.0, alert())
    new_boot = L.Limiter(3, 3600.0, 30, saved, same_boot=False)     # uptime starts again from 0
    [note] = new_boot.notes(10.0)
    assert note.message.startswith("2 more alerts")
    assert new_boot.admit(11.0, alert())


# ---- the boot summary

def test_boot_summary_contents():
    facts = {"boot": 7, "uptime_s": 185.0, "clock_synced": 0,
             "previous": {"boot": 6, "last_uptime_s": 36000.0, "clean_stop": True,
                          "power_shutdown": "loss agreed for 8.2 s; both signals say loss", "alerts": {"5": 1, "3": 2}},
             "disk": {"nvme_free_gb": 812.4, "sd_free_gb": 97.1},
             "collections": {"count": 9, "last": {"name": "b00006_s003_20261119T185630Z", "how": "stopped",
                                                  "reason": "stop by command", "recorded_s": 7200.0, "data_gb": 3.5}},
             "services": ["Radar manager: ready.", "Safety watcher: ok."],
             "active": ["GPS: no fix or no receiver"], "queue": {"length": 2, "dropped": 0, "held": 4}}
    s = L.boot_summary(facts)
    assert s.key == "summary" and not s.limited and s.priority == L.DEFAULT and s.title == "boot 7 summary"
    for piece in ("Boot 7", "CLOCK NOT SET", "Previous boot 6", "up at least 10.0 h", "shut down by the power service",
                  "1 urgent, 2 default", "812.4 GB free", "97.1 GB free", "b00006_s003_20261119T185630Z",
                  "recorded 2.0 h", "3.50 GB", "9 collections", "Radar manager: ready.", "GPS: no fix",
                  "2 waiting", "4 held back"):
        assert piece in s.message, piece
    unclean = dict(facts, previous={"boot": 4, "last_uptime_s": 600.0, "clean_stop": False})
    text = L.boot_summary(unclean).message
    assert "without a clean stop" in text and "boots 5-6: no record" in text
    first = L.boot_summary(dict(facts, previous=None, collections={"error": "the data drive did not answer"},
                                boot=None, clock_synced=None)).message
    assert "no record" in first and "did not answer" in first and "Boot ?" in first and "clock state unknown" in first


def test_odd_input_cannot_grow_the_storm_state_without_bound():
    lim = L.Limiter(1, 3600.0, 100000)
    for i in range(5000):
        lim.admit(float(i), alert(key=f"manager.fault.kind{i}", priority=L.URGENT))
    assert len(lim.kinds) <= L.MAX_KINDS + 1 and len(lim.passed) <= 4 * L.MAX_KINDS + 1
    many = {f"code{i}": "x" for i in range(1000)}
    states = L.condition_states(reads(power=ok({"warnings": many})), COND)
    assert len([n for n in states if n.startswith("power.")]) == L.MAX_POWER_WARNINGS
