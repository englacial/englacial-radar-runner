"""The safety watcher's decisions (radar_runner/watcher/logic.py), in simulated time with injected readings."""
import random
import signal

import pytest
from watcher_fakes import ENABLED_A, GOOD, Sim, settings, telemetry_status

from radar_runner.common import status as S
from radar_runner.watcher import logic as L


def started(**changes):
    """A watcher that has been running on good readings for a while."""
    sim = Sim(**changes).run(5.0)
    assert sim.ok and not sim.tripped and sim.logic.reason == ""
    sim.clear()
    return sim


def sigints(sim):
    return [t for t, sig, _ in sim.signals if sig == signal.SIGINT]


def sigkills(sim):
    return [t for t, sig, _ in sim.signals if sig == signal.SIGKILL]


# ---- one reading must never decide anything



# ---- what counts as a usable reading

def read(data, state=S.OK, error=""):
    return S.StatusRead(state, data, 0.2, error)


def test_a_fresh_reading_is_taken_with_its_own_time():
    sample, why = L.amp_sample(read(telemetry_status(500.0)), 500.0, 3.0)
    assert why == "" and sample == L.Sample(499.75, 27.2, 19.3, 0.156, 27.98)


@pytest.mark.parametrize("state, error", [(S.STALE, "4.0 s old, limit 3 s"), (S.MISSING, "no such file"),
                                         (S.CORRUPT, "cannot parse: Expecting value")])
def test_an_old_missing_or_damaged_status_file_gives_no_reading(state, error):
    sample, why = L.amp_sample(read(telemetry_status(500.0) if state == S.STALE else None, state, error), 500.0, 3.0)
    assert sample is None and why == f"telemetry status file {state}: {error}"


def test_readings_that_the_telemetry_service_calls_not_fresh_are_not_used_and_the_reason_is_kept():
    data = telemetry_status(500.0, fresh=False, reason="the OLA is not set up for the amplifier readings (configuring)")
    sample, why = L.amp_sample(read(data), 500.0, 3.0)
    assert sample is None
    assert why == "amplifier readings not fresh: the OLA is not set up for the amplifier readings (configuring)"
    for fresh in (None, "true", 1):                 # only exactly true counts
        data["amp"]["fresh"] = fresh
        assert L.amp_sample(read(data), 500.0, 3.0)[0] is None


def test_a_reading_older_than_the_limit_or_without_a_time_is_not_used():
    assert L.amp_sample(read(telemetry_status(500.0, age=3.5)), 500.0, 3.0) == (None, "the amplifier reading is 3.5 s old")
    assert L.amp_sample(read(telemetry_status(500.0, age=-2.0)), 500.0, 3.0)[0] is None      # from the future
    data = telemetry_status(500.0)
    data["amp"]["sample_uptime_s"] = None
    assert L.amp_sample(read(data), 500.0, 3.0) == (None, "the amplifier reading has no time")
    assert L.amp_sample(read({"state": "ok"}), 500.0, 3.0) == (None, "the telemetry status has no amplifier readings")
    assert L.amp_sample(read({"amp": [1, 2]}), 500.0, 3.0)[0] is None


@pytest.mark.parametrize("name, value", [("temp_c", 187.2), ("temp_c", -80.0), ("current_a", 55.0), ("voltage_v", 280.0),
                                         ("case_temp_c", None), ("temp_c", "27.2"), ("current_a", True),
                                         ("voltage_v", float("nan")), ("temp_c", float("inf"))])
def test_a_value_that_is_missing_or_not_plausible_is_dropped_not_used(name, value):
    sample, why = L.amp_sample(read(telemetry_status(500.0, {name: value})), 500.0, 3.0)
    assert why == "" and getattr(sample, name) is None
    assert all(getattr(sample, other) == GOOD[other] for other in GOOD if other != name)



# ---- start, and telemetry that is not there

def test_at_the_start_nothing_is_published_until_three_readings_are_judged_then_ok():
    sim = Sim()
    sim.run(1.0)
    assert not sim.logic.ready(sim.t) and not sim.ok
    sim.run(0.5)
    assert sim.logic.ready(sim.t) and sim.ok and sim.state("telemetry") == "ok"
    assert sim.events == []                         # a first "ok" is not news


def test_a_hot_amplifier_at_the_start_is_never_reported_ok():
    sim = Sim()
    sim.amp["temp_c"] = 61.0
    sim.run(6.0)
    assert not any(ok for _, ok, _, _ in sim.statuses)
    assert sim.tripped and len(sim.named("trip", condition="amp_temp")) == 1


def test_no_telemetry_after_boot_is_not_ok_with_the_reason_and_causes_no_action():
    # The OLA takes about 12 s to set up after every start of the telemetry service.
    sim = Sim()
    sim.telemetry = False
    sim.run(2.75)
    assert not sim.logic.ready(sim.t)               # for 3 s it could still be the watcher's own start
    sim.run(9.25)
    assert sim.logic.ready(sim.t) and not sim.ok and not sim.tripped
    assert sim.logic.reason == ("no usable amplifier telemetry since the watcher started 12 s ago: amplifier readings "
                                "not fresh: the OLA is not set up for the amplifier readings (configuring)")
    assert sim.signals == [] and sim.tool_starts == []
    assert [e["event"] for e in sim.events] == ["not_ok"]
    sim.telemetry = True
    sim.run(1.0)
    assert not sim.ok and sim.state("telemetry") == "settling"
    sim.run(0.5)
    assert sim.ok and [e["event"] for e in sim.events] == ["not_ok", "ok"]


def test_telemetry_lost_is_not_ok_after_three_seconds_and_ok_again_after_three_readings():
    sim = started()
    sim.telemetry = False
    sim.run(2.5)
    assert sim.ok                                   # 2.75 s after the last reading
    sim.run(0.5)
    assert not sim.ok and not sim.tripped and sim.state("telemetry") == "lost"
    assert sim.logic.reason == ("no usable amplifier telemetry for 4 s: amplifier readings not fresh: "
                                "the OLA is not set up for the amplifier readings (configuring)")
    sim.run(9.0)                                    # the 12 s gap of a telemetry restart, no radar running
    assert sim.signals == [] and sim.tool_starts == []
    assert sim.logic.counters["telemetry_losses"] == 1
    sim.telemetry = True
    sim.run(1.0)
    assert not sim.ok
    sim.run(0.5)
    assert sim.ok and [e["event"] for e in sim.events] == ["not_ok", "ok"]


def test_a_radar_still_running_through_a_telemetry_gap_is_stopped_after_the_grace_period():
    sim = started()
    sim.radar = (4242,)
    sim.run(2.0)
    sim.telemetry = False
    sim.run(7.5)                                    # lost 3 s after the last reading, then 5 s of grace
    assert sim.signals == [] and not sim.ok
    sim.run(0.5)
    assert [(sig, pids) for _, sig, pids in sim.signals] == [(signal.SIGINT, (4242,))]
    assert sim.tool_starts == []
    sim.radar = ()
    sim.run(0.25)
    assert sim.named("radar_stopped") and sim.logic.stop == "idle"


def test_one_damaged_reading_changes_nothing():
    sim = started()
    for values in ({"temp_c": None}, {"current_a": None, "voltage_v": None}, {"temp_c": 70.0}, {"case_temp_c": 99.0},
                   {"voltage_v": 5.0}, {"voltage_v": 35.0}, {"current_a": 9.0}):
        sim.one_reading(**values)
        sim.run(1.5)
        assert sim.ok and not sim.tripped, values
    assert sim.events == [] and sim.signals == [] and sim.tool_starts == []
    assert all(ok for _, ok, _, _ in sim.statuses)



def test_the_case_sensor_can_be_made_optional():
    sim = started(require_case_temp=False)
    sim.amp["case_temp_c"] = None
    sim.run(5.0)
    assert sim.ok and [w["warning"] for w in sim.logic.snapshot(sim.t)["warnings"]] == ["case_temp_missing"]


# ---- the override "run without amplifier telemetry"

def test_with_the_override_lost_telemetry_does_not_make_the_status_not_ok():
    sim = started()
    sim.override = True
    sim.telemetry = False
    sim.radar = (4242,)
    sim.run(30.0)
    assert sim.ok and all(ok for _, ok, _, _ in sim.statuses) and sim.signals == []
    status = sim.logic.snapshot(sim.t, True)
    assert status["conditions"]["telemetry"]["state"] == "lost" and status["conditions"]["telemetry"]["overridden"]
    assert status["override"] == {"no_amp_telemetry": True}
    assert "OVERRIDE set" in status["warnings"][0]["text"] and "the OLA is not set up" in status["warnings"][0]["text"]
    sim.override = False                            # the user clears it: not ok at once
    sim.run(0.25)
    assert not sim.ok


def test_with_the_override_and_no_telemetry_at_boot_the_status_is_ok_after_the_start_wait():
    sim = Sim()
    sim.override, sim.telemetry = True, False
    sim.run(3.0)
    assert not sim.ok                               # readings could still be about to arrive
    sim.run(0.5)
    assert sim.ok


def test_with_the_override_everything_that_can_still_be_judged_is():
    sim = started()
    sim.override = True
    sim.amp.update(case_temp_c=None, temp_c=56.0)   # the case sensor is gone, the amplifier's own says hot
    sim.run(2.0)
    assert not sim.ok and sim.tripped and sim.named("trip", condition="amp_temp")
    # A trip stays while there are no readings to show that it is over.
    sim.telemetry = False
    sim.run(120.0)
    assert not sim.ok and sim.tripped
    sim.telemetry = True
    sim.amp["temp_c"] = 30.0
    sim.run(32.0)
    assert sim.ok


# ---- temperature

@pytest.mark.parametrize("name, condition", [("temp_c", "amp_temp"), ("case_temp_c", "case_temp")])
def test_over_temperature_trips_after_three_readings_in_a_row(name, condition):
    sim = started()
    sim.amp[name] = 55.0
    sim.run(1.0)
    assert sim.ok                                   # two readings
    sim.run(0.5)
    assert not sim.ok and sim.tripped and sim.state(condition) == "trip"
    assert "reached 55 C (now 55.0 C); clears below 50 C" in sim.logic.reason
    trip, = sim.named("trip")
    assert trip["condition"] == condition and trip["severity"] == "urgent" and trip[name] == 55.0
    assert sim.logic.counters["trips"] == 1 and sim.signals == [] and sim.tool_starts == []


def test_just_below_the_trip_level_is_a_warning_only():
    sim = started()
    sim.amp["temp_c"] = 54.9
    sim.run(60.0)
    assert sim.ok and sim.state("amp_temp") == "warn"
    assert [e["event"] for e in sim.events] == ["warning"] and sim.events[0]["warning"] == "amp_temp_high"
    sim.amp["temp_c"] = 47.9                        # the warning ends 2 C below its level, after the hold
    sim.run(32.0)
    assert sim.state("amp_temp") == "ok" and [e["event"] for e in sim.events] == ["warning", "warning_cleared"]



def test_a_trip_clears_by_itself_only_after_thirty_seconds_below_the_recover_level():
    sim = started()
    sim.amp["case_temp_c"] = 56.0
    sim.run(2.0)
    assert sim.tripped
    sim.amp["case_temp_c"] = 52.0                   # below the trip level but not below the recover level
    sim.run(120.0)
    assert sim.tripped and not sim.ok
    sim.amp["case_temp_c"] = 49.5
    sim.run(29.5)
    assert sim.tripped
    sim.one_reading(case_temp_c=51.0)               # one reading above: the 30 s start again
    sim.run(29.0)
    assert sim.tripped
    sim.run(2.0)
    assert sim.ok and not sim.tripped and sim.state("case_temp") == "warn"      # 49.5 C: still a warning
    assert [e["event"] for e in sim.events if e["event"] in ("trip", "clear", "ok", "not_ok")] == \
        ["trip", "not_ok", "clear", "ok"]
    assert sim.logic.counters["clears"] == 1


def test_one_cool_reading_does_not_clear_a_trip():
    sim = started()
    sim.amp["temp_c"] = 58.0
    sim.run(2.0)
    for _ in range(20):
        sim.one_reading(temp_c=20.0)
        sim.run(1.0)
    assert sim.tripped and sim.named("clear") == []




# ---- the 28 V rail

@pytest.mark.parametrize("volts", [25.9, 21.0, 30.6, 39.0])
def test_supply_out_of_range_while_powered_trips(volts):
    sim = started()
    sim.amp["voltage_v"] = volts
    sim.run(1.0)
    assert sim.ok
    sim.run(0.5)
    assert not sim.ok and sim.tripped and sim.state("voltage") == "trip"
    assert sim.logic.reason == f"amplifier supply outside 26-30.5 V (now {volts:.2f} V)"
    assert sim.named("trip")[0]["severity"] == "warning"


@pytest.mark.parametrize("volts", [26.0, 27.4, 30.5])
def test_supply_inside_the_limits_does_not_trip(volts):
    sim = started()
    sim.amp["voltage_v"] = volts
    sim.run(30.0)
    assert sim.ok and sim.events == []


def test_a_supply_trip_clears_only_inside_the_recover_band():
    sim = started()
    sim.amp["voltage_v"] = 25.0
    sim.run(2.0)
    sim.amp["voltage_v"] = 26.3                     # inside the limits, not yet inside 26.5-30.0 V
    sim.run(90.0)
    assert sim.tripped
    sim.amp["voltage_v"] = 26.6
    sim.run(29.0)
    assert sim.tripped
    sim.run(2.5)
    assert sim.ok and sim.named("clear", condition="voltage")


def test_an_unpowered_amplifier_is_not_ok_but_no_trip_and_no_action():
    sim = started()
    sim.amp.update(voltage_v=0.3, current_a=0.0, temp_c=0.4)
    sim.run(1.5)
    assert not sim.ok and not sim.tripped and sim.state("voltage") == "unpowered"
    assert sim.logic.reason == "amplifier 28 V absent: the radar must not transmit into an unpowered amplifier"
    sim.run(600.0)
    assert sim.signals == [] and sim.tool_starts == [] and sim.named("trip") == []
    assert [(e["event"], e["severity"]) for e in sim.events] == [("unpowered", "info"), ("not_ok", "warning")]
    sim.amp.update(GOOD)                            # the rail is back: ok after the hold
    sim.run(29.0)
    assert not sim.ok
    sim.run(2.5)
    assert sim.ok and [e["event"] for e in sim.events] == ["unpowered", "not_ok", "powered", "ok"]


def test_an_unpowered_amplifier_can_be_allowed_for_bench_work():
    sim = started(allow_unpowered=True)
    sim.amp.update(voltage_v=0.3, current_a=0.0)
    sim.run(20.0)
    assert sim.ok and "unpowered_allowed" in sim.logic.warnings


def test_a_radar_running_into_an_unpowered_amplifier_is_stopped():
    sim = started()
    sim.radar = (77,)
    sim.amp.update(voltage_v=0.3, current_a=0.0)
    sim.run(6.0)
    assert sim.signals == []                        # unpowered after 1.25 s, then 5 s of grace
    sim.run(0.5)
    assert [sig for _, sig, _ in sim.signals] == [signal.SIGINT]



# ---- amplifier enabled with no radar process: the reset tool

def enabled(sim):
    """Left enabled with no radar: the rule fires after 5 s and asks for the tool."""
    sim.amp["current_a"] = ENABLED_A
    sim.run(5.0)
    assert sim.tool_starts == [] and sim.ok and sim.state("amp_enabled") == "pending"
    sim.run(0.75)
    assert len(sim.tool_starts) == 1 and not sim.ok and sim.tripped and sim.state("amp_enabled") == "resetting"
    return sim


def test_enabled_current_for_five_seconds_with_no_radar_runs_the_reset_tool_and_ok_ends_it():
    sim = enabled(started())
    assert sim.named("trip", condition="amp_enabled")[0]["severity"] == "warning"
    assert sim.logic.reason.startswith("amplifier enabled with no radar process: resetting the SDR GPIO")
    sim.amp["current_a"] = 0.156                    # the tool puts the amplifier back into shutdown ...
    sim.tool_done(0)                                # ... and reports OK
    sim.run(1.0)
    assert not sim.ok                               # two readings at shutdown current are not yet enough
    sim.run(0.75)
    assert sim.ok and not sim.tripped and sim.state("amp_enabled") == "idle"
    assert "back at shutdown current (0.16 A) after 1 run(s)" in sim.named("clear", condition="amp_enabled")[0]["text"]
    sim.run(300.0)
    assert len(sim.tool_starts) == 1 and sim.logic.counters["tool_runs"] == 1 and sim.logic.counters["tool_ok"] == 1
    assert sim.logic.last_tool["name"] == "OK" and sim.logic.last_tool["duration_s"] == 3.5


def test_enable_pulses_shorter_than_five_seconds_do_nothing():
    # Every UHD initialisation enables the amplifier for 0.5 s (also the reset tool's own); a probe for 1.7 s.
    sim = started()
    for seconds in (0.5, 1.5, 4.5):
        sim.amp["current_a"] = ENABLED_A
        sim.run(seconds)
        sim.amp["current_a"] = 0.156
        sim.run(3.0)
    assert sim.tool_starts == [] and sim.events == [] and all(ok for _, ok, _, _ in sim.statuses)


def test_enabled_current_with_a_radar_process_alive_is_normal():
    sim = started()
    sim.radar = (500,)
    sim.amp["current_a"] = ENABLED_A
    sim.run(600.0)
    assert sim.ok and sim.tool_starts == [] and sim.signals == [] and sim.events == []
    # The radar is killed and leaves the amplifier enabled: the 5 s count from when the process is gone.
    sim.radar = ()
    sim.run(5.0)
    assert sim.tool_starts == []
    sim.run(0.75)
    assert len(sim.tool_starts) == 1


def test_a_radar_that_starts_during_the_five_seconds_ends_the_count():
    sim = started()
    sim.amp["current_a"] = ENABLED_A                # for example the enable at the start of the radar itself
    sim.run(4.0)
    assert sim.state("amp_enabled") == "pending"
    sim.radar = (600,)
    sim.run(60.0)
    assert sim.tool_starts == [] and sim.ok and sim.state("amp_enabled") == "idle" and sim.events == []


def test_an_amplifier_found_enabled_at_boot_is_reset_five_seconds_after_the_first_reading():
    sim = Sim()
    sim.telemetry = False
    sim.amp["current_a"] = ENABLED_A
    sim.run(12.0)
    assert sim.tool_starts == []                    # no readings, no evidence
    sim.telemetry = True
    sim.run(5.0)
    assert sim.tool_starts == []
    sim.run(1.0)
    assert len(sim.tool_starts) == 1


def test_ok_from_the_tool_with_the_amplifier_still_enabled_ends_as_stuck_with_slow_retries():
    sim = enabled(started())
    sim.tool_done(0)                                # "OK" only says what the SDR commands; the current decides
    t1 = sim.t
    sim.run(4.75)
    assert len(sim.tool_starts) == 1
    sim.run(0.5)
    assert len(sim.tool_starts) == 2 and sim.tool_starts[1] - t1 == pytest.approx(5.0, abs=0.3)
    sim.tool_done(0)
    sim.run(5.25)
    sim.tool_done(0)                                # the third run
    assert sim.state("amp_enabled") == "resetting" and sim.named("stuck") == []
    sim.run(5.25)
    assert sim.state("amp_enabled") == "stuck" and not sim.ok and sim.tripped
    stuck, = sim.named("stuck")
    assert stuck["severity"] == "urgent" and "AMPLIFIER STUCK ENABLED: 2.58 A" in stuck["text"]
    assert "after the reset tool ran 3 times (last: OK (exit 0))" in stuck["text"] and "cut the power" in stuck["text"]
    assert sim.logic.reason.startswith("AMPLIFIER STUCK ENABLED with no radar process: the reset tool ran 3 times")
    assert len(sim.tool_starts) == 3
    # From here one run a minute, counted from the end of the run before.
    t3 = sim.t - 5.25
    for n in (4, 5, 6):
        sim.run(60.0 - (sim.t - t3) - 0.5)
        assert len(sim.tool_starts) == n - 1
        sim.run(1.0)
        assert len(sim.tool_starts) == n
        sim.tool_done(0)
        t3 = sim.t
    assert len(sim.named("stuck")) == 1             # said once
    # It ends by itself when the current is back at the shutdown level.
    sim.amp["current_a"] = 0.156
    sim.run(2.0)
    assert sim.ok and "no longer stuck enabled" in sim.named("clear", condition="amp_enabled")[0]["text"]
    sim.run(300.0)
    assert len(sim.tool_starts) == 6


def test_assume_enabled_is_retried_promptly_a_limited_number_of_times():
    sim = enabled(started())
    ends = []
    for _ in range(3):
        sim.tool_done(3, "ASSUME_ENABLED: pin never read high")
        ends.append(sim.t)
        sim.run(2.5)
    assert [round(start - end, 1) for start, end in zip(sim.tool_starts[1:], ends)] == [2.0, 2.0]
    assert len(sim.tool_starts) == 3 and sim.state("amp_enabled") == "stuck"       # 2 s after the third run
    assert "last: ASSUME_ENABLED (exit 3)" in sim.named("stuck")[0]["text"]
    sim.run(55.0)
    assert len(sim.tool_starts) == 3
    sim.run(3.0)
    assert len(sim.tool_starts) == 4                # then once a minute



def test_a_later_success_after_failures_ends_the_matter():
    sim = enabled(started())
    sim.tool_done(6, "BUSY", took=1.0)              # something else had the SDR
    sim.run(5.5)
    sim.amp["current_a"] = 0.156
    sim.tool_done(0)
    sim.run(2.0)
    assert sim.ok and sim.named("stuck") == [] and len(sim.tool_starts) == 2



def test_the_tool_does_not_run_without_a_reading_that_says_enabled():
    sim = enabled(started())
    sim.tool_done(4, "TIMEOUT", took=1.0)
    sim.telemetry = False                           # telemetry goes away: nothing shows the amplifier enabled now
    sim.override = True                             # ... and the override does not turn that into "ok"
    sim.run(300.0)
    assert len(sim.tool_starts) == 1 and not sim.ok and sim.tripped and sim.state("amp_enabled") == "resetting"
    sim.telemetry = True
    sim.run(1.0)
    assert len(sim.tool_starts) == 2                # the readings are back and still say enabled


def test_enabled_readings_that_stop_arriving_do_not_fire_the_rule():
    sim = started()
    sim.amp["current_a"] = ENABLED_A
    sim.run(3.0)
    sim.telemetry = False
    sim.run(20.0)
    assert sim.tool_starts == [] and sim.state("amp_enabled") == "idle"



# ---- stopping a radar that the manager did not stop

def test_a_radar_alive_after_the_grace_period_gets_sigint_and_is_left_alone_once_gone():
    sim = started()
    sim.radar = (1234, 1300)
    sim.amp["temp_c"] = 60.0
    sim.run(1.5)
    assert sim.tripped
    tripped_at = sim.named("trip")[0]["t"]
    sim.run(tripped_at + 4.75 - sim.t)
    assert sim.signals == []
    sim.run(0.5)
    assert sim.signals == [(tripped_at + 5.0, signal.SIGINT, (1234, 1300))]
    assert sim.logic.stop == "interrupted" and sim.logic.counters["radar_sigint"] == 1
    assert "sent SIGINT" in sim.named("radar_stop")[0]["text"] and "reached 55 C" in sim.named("radar_stop")[0]["text"]
    sim.radar = ()                                  # it exits 0.5 s later
    sim.run(0.5)
    assert sim.logic.stop == "idle" and sim.named("radar_stopped") and len(sim.signals) == 1


def test_a_radar_that_ignores_sigint_is_killed_and_killed_again_while_it_lasts():
    sim = started()
    sim.radar = (1234,)
    sim.amp["voltage_v"] = 31.0
    sim.run(40.0)
    assert len(sigints(sim)) == 1
    kills = sigkills(sim)
    assert kills[0] - sigints(sim)[0] == pytest.approx(5.0)                 # the allowed exit time
    assert [round(b - a, 2) for a, b in zip(kills, kills[1:])] == [5.0] * (len(kills) - 1) and len(kills) >= 5
    assert sim.named("radar_kill")[0]["severity"] == "warning" and sim.named("radar_kill")[1]["severity"] == "urgent"
    sim.radar = ()
    sim.run(0.5)
    assert "after SIGKILL" in sim.named("radar_stopped")[0]["text"] and sim.logic.stop == "idle"


def test_a_radar_is_never_signalled_while_the_status_is_ok():
    sim = started()
    sim.radar = (1,)
    sim.amp.update(temp_c=54.0, voltage_v=26.1)     # warnings, no trip
    sim.run(600.0)
    assert sim.ok and sim.signals == []


def test_a_radar_that_the_manager_stops_in_time_is_never_signalled():
    sim = started()
    sim.radar = (1234,)
    sim.amp["temp_c"] = 60.0
    sim.run(4.0)
    sim.radar = ()
    sim.run(60.0)
    assert sim.signals == []



def test_a_stop_that_has_begun_is_finished_even_if_the_status_recovers():
    sim = started()
    sim.radar = (8,)
    sim.telemetry = False
    sim.run(9.0)
    assert len(sigints(sim)) == 1
    sim.telemetry = True
    sim.run(3.0)
    assert sim.ok
    sim.run(3.0)
    assert len(sigkills(sim)) == 1


# ---- warnings only



# ---- the watcher's own loop

def test_after_a_stall_of_the_watcher_counts_of_readings_start_again():
    sim = started()
    sim.amp["temp_c"] = 60.0
    sim.run(1.0)                                    # two hot readings
    sim.round(dt=10.0)                              # the loop hangs for 10 s; the next reading is hot too
    assert sim.ok and sim.logic.counters["stalls"] == 1 and sim.named("stall")
    assert sim.logic.loop == {"last_gap_s": 10.0, "recent_longest_gap_s": 10.0, "longest_gap_s": 10.0}
    sim.run(1.0)
    assert not sim.ok                               # three in a row after the stall
    # The same for the 5 s of the enabled rule.
    sim = started()
    sim.amp["current_a"] = ENABLED_A
    sim.run(4.0)
    sim.round(dt=10.0)
    sim.run(4.0)
    assert sim.tool_starts == []
    sim.run(2.0)
    assert len(sim.tool_starts) == 1


# ---- the status

# ---- k of m: one damaged reading does nothing, one good reading in between hides nothing

def test_latch_turns_on_with_three_of_the_last_five_readings_spanning_a_second():
    d = L.Latch(k=3, m=5, span_s=1.0, hold_s=30.0, max_gap_s=3.0)
    assert [d.update(t, v) for t, v in ((0.0, L.ON), (0.5, None), (1.0, L.ON), (1.5, L.OFF))] == [False] * 4
    assert d.update(2.0, L.ON) and d.active and d.since == 2.0     # 3 of the last 5
    # Two of five are not enough, however long it goes on.
    d = L.Latch(3, 5, 1.0, 30.0, 3.0)
    for n in range(100):
        assert not d.update(n * 0.5, L.ON if n % 5 in (0, 2) else L.OFF)
    # Three within less than the span are not enough; three need not be in a row.
    d = L.Latch(3, 5, 1.0, 30.0, 3.0)
    assert [d.update(t, L.ON) for t in (0.0, 0.1, 0.2)] == [False] * 3
    assert d.update(1.0, L.ON)
    # A gap longer than max_gap_s forgets the readings before it.
    d = L.Latch(3, 5, 1.0, 30.0, 3.0)
    assert [d.update(t, L.ON) for t in (0.0, 0.5, 5.0, 5.5)] == [False] * 4
    assert d.update(6.0, L.ON)


def test_latch_turns_off_only_after_the_hold_of_unbroken_off_readings():
    d = L.Latch(3, 5, 1.0, 30.0, 3.0)
    for t in (0.0, 0.5, 1.0):
        d.update(t, L.ON)
    assert d.active
    t = 1.0
    while t < 30.5:
        t += 0.5
        assert not d.update(t, L.OFF)
    assert not d.update(31.0, None)                 # one reading in between: the 30 s start again
    t = 31.0
    while t < 61.0:
        t += 0.5
        assert not d.update(t, L.OFF)
    assert d.update(61.5, L.OFF) and not d.active
    assert not d.update(62.0, L.ON) and not d.update(62.5, L.ON)    # the readings from before the clear are forgotten


def test_latch_that_is_not_proven_either_way_at_the_start():
    d = L.Latch(3, 5, 1.0, 30.0, 3.0, active=None)
    assert [d.update(t, None) for t in (0.0, 0.5, 1.0, 1.5)] == [False] * 4 and d.active is None
    assert [d.update(t, L.OFF) for t in (2.0, 2.5)] == [False, False]
    assert d.update(3.0, L.OFF) and d.active is False
    d = L.Latch(3, 5, 1.0, 30.0, 3.0, active=None)
    assert [d.update(t, L.ON) for t in (0.0, 0.5, 1.0)] == [False, False, True] and d.active is True


@pytest.mark.parametrize("pattern", [(55.4, 54.8), (58.0, 58.0, 54.0), (60.0, 30.0), (56.0, None, 56.0, 52.0, 30.0)])
def test_readings_that_come_and_go_around_the_limit_still_trip(pattern):
    # The reviewer's scenarios: with "three in a row" none of these ever tripped.
    sim = started()
    for n in range(40):
        sim.one_reading(temp_c=pattern[n % len(pattern)])
        if sim.tripped:
            break
    assert sim.tripped and sim.named("trip", condition="amp_temp") and n < 12


def test_one_or_two_hot_readings_in_five_never_trip():
    sim = started()
    for n in range(200):
        sim.one_reading(temp_c=70.0 if n % 5 in (0, 2) else 30.0)
    assert sim.ok and not sim.tripped and sim.named("trip") == []


def test_readings_without_a_needed_value_count_as_lost_telemetry():
    for name in ("case_temp_c", "temp_c", "current_a", "voltage_v"):
        sim = started()
        sim.amp[name] = None
        sim.run(3.5)
        assert not sim.ok and not sim.tripped
        assert sim.logic.reason.endswith(f"missing or not plausible: {name}")


# ---- a voltage reading that is wrong must not hide anything

def test_the_amplifier_sensor_is_judged_whatever_the_voltage_reading_says():
    # The reviewer's scenario: the voltage sense fails (reads 0 V) and the amplifier is at 90 C.
    sim = started(allow_unpowered=True)
    sim.radar = (1,)
    sim.amp.update(voltage_v=0.0, temp_c=90.0, case_temp_c=40.0)
    sim.run(2.0)
    assert not sim.ok and sim.tripped and sim.state("amp_temp") == "trip"
    sim.run(6.0)
    assert sigints(sim)


def test_enabled_current_without_the_28_v_is_a_contradiction_not_ok_and_not_a_trip():
    sim = started(allow_unpowered=True)
    sim.radar = (1,)                                # so that the enabled rule stays out of it
    sim.amp.update(voltage_v=0.2, current_a=ENABLED_A)
    sim.run(2.0)
    assert not sim.ok and not sim.tripped
    assert sim.logic.reason.startswith("readings contradict each other: enabled current without the 28 V")
    assert "contradiction" in sim.logic.warnings and sim.logic.snapshot(sim.t)["conditions"]["voltage"]["contradiction"]
    sim.amp.update(current_a=0.0)                   # really unpowered: allowed here
    sim.run(32.0)
    assert sim.ok


def test_a_fast_drop_is_unpowered_and_a_rail_that_sags_is_a_supply_trip():
    sim = started()
    sim.amp["voltage_v"] = 15.0
    sim.run(10.0)
    assert not sim.ok and not sim.tripped and sim.state("voltage") == "unpowered"
    sim = started()
    sim.amp["voltage_v"] = 23.0
    sim.run(2.0)
    assert sim.tripped and sim.named("trip", condition="voltage")


# ---- a restart must not lose what was known

def test_a_restart_in_the_middle_of_a_trip_restores_the_trip():
    sim = started()
    sim.amp["temp_c"] = 56.0
    sim.run(3.0)
    sim.amp["temp_c"] = 53.0
    sim.run(5.0)
    assert sim.tripped
    kept = sim.logic.persistent()
    assert kept == {"trips": {"amp_temp": True, "case_temp": False, "voltage": False}, "enabled": "idle",
                    "signalled": False}
    again = Sim()
    again.t = sim.t
    again.logic = L.WatcherLogic(again.cfg, again.t, kept)
    again.amp["temp_c"] = 53.0
    again.run(60.0)
    assert not any(ok for _, ok, _, _ in again.statuses) and again.tripped and again.state("amp_temp") == "trip"
    again.amp["temp_c"] = 45.0
    again.run(32.0)
    assert again.ok


def test_without_a_record_a_limit_between_recover_and_trip_level_starts_as_not_ok():
    # The reviewer's scenario without the persisted state (a reboot): 53 C may be the tail of a trip.
    sim = Sim()
    sim.amp["temp_c"] = 53.0
    sim.run(60.0)
    assert not any(ok for _, ok, _, _ in sim.statuses) and not sim.tripped and sim.state("amp_temp") == "unproven"
    assert sim.logic.reason.startswith("amplifier temperature not yet seen below 50 C since the watcher started (now 53.0 C)")
    sim.amp["temp_c"] = 49.0
    sim.run(2.0)
    assert sim.ok
    sim.amp["temp_c"] = 53.0                        # once proven, the band in between is only a warning
    sim.run(60.0)
    assert sim.ok and sim.state("amp_temp") == "warn"
    # The same for the supply: 26.2 V is inside the limits and outside the recover band.
    sim = Sim()
    sim.amp["voltage_v"] = 26.2
    sim.run(10.0)
    assert not sim.ok and not sim.tripped and sim.state("voltage") == "unproven"


def test_a_restart_keeps_a_stuck_amplifier_stuck_and_does_not_run_the_tool_at_once():
    sim = enabled(started())
    for _ in range(3):
        sim.tool_done(3, "ASSUME_ENABLED")
        sim.run(2.5)
    assert sim.state("amp_enabled") == "stuck"
    kept = sim.logic.persistent()
    again = Sim()
    again.t = sim.t
    again.logic = L.WatcherLogic(again.cfg, again.t, kept)
    again.amp["current_a"] = ENABLED_A
    again.run(30.0)
    assert again.state("amp_enabled") == "stuck" and again.tripped and again.tool_starts == []
    assert not any(ok for _, ok, _, _ in again.statuses)
    again.run(31.0)
    assert len(again.tool_starts) == 1              # one a minute, as before the restart
    for junk in (None, [], "x", {"trips": 3, "enabled": 7}):
        L.WatcherLogic(sim.cfg, 1.0, junk).step(2.0, L.Inputs())    # a damaged record is not a reason to fail


# ---- results that say "nothing was changed" are not evidence that a reset does not help

@pytest.mark.parametrize("code, name", [(2, "NOT_FOUND"), (4, "TIMEOUT"), (5, "FAILED"), (6, "BUSY"), (7, "AMBIGUOUS"),
                                        (8, "STATE_FILE"), (64, "USAGE")])
def test_results_that_changed_nothing_never_make_stuck_but_are_urgent_after_thirty_seconds(code, name):
    sim = enabled(started())
    ends = []
    for _ in range(4):
        sim.tool_done(code, f"{name}: stand-in", took=1.0)
        ends.append(sim.t)
        sim.run(5.5)
    assert [round(start - end, 1) for start, end in zip(sim.tool_starts[1:], ends)] == [5.0] * 4
    assert sim.state("amp_enabled") == "resetting" and sim.named("blocked") == []       # 26 s of it so far
    sim.tool_done(code, f"{name}: stand-in", took=1.0)
    sim.run(5.5)
    assert sim.state("amp_enabled") == "blocked" and sim.named("stuck") == [] and not sim.ok and sim.tripped
    blocked, = sim.named("blocked")
    assert blocked["severity"] == "urgent" and "THE RESET TOOL CANNOT RUN" in blocked["text"]
    assert f"{name} (exit {code})" in blocked["text"] and "held by another program" in blocked["text"]
    assert sim.logic.reason.startswith("AMPLIFIER ENABLED with no radar process and the reset tool cannot run")
    assert sim.logic.ran == 0 and sim.logic.counters["tool_ok"] == 0
    # From here one try every 15 s (a try that cannot run costs no enable pulse).
    n = len(sim.tool_starts)
    sim.tool_done(code, name, took=1.0)
    sim.run(14.0)
    assert len(sim.tool_starts) == n
    sim.run(1.5)
    assert len(sim.tool_starts) == n + 1
    # The other program lets go: the tool runs, and the matter ends.
    sim.amp["current_a"] = 0.156
    sim.tool_done(0)
    sim.run(2.0)
    assert sim.ok and sim.state("amp_enabled") == "idle"


def test_only_runs_of_the_tool_that_ran_count_towards_stuck():
    sim = enabled(started())
    for code in (6, 3, 6, 3, 6):                    # two real runs among tries that could not run
        sim.tool_done(code, "x", took=1.0)
        sim.run(5.5)
    assert sim.state("amp_enabled") == "resetting" and sim.logic.ran == 2 and sim.named("stuck") == []
    sim.tool_done(3, "ASSUME_ENABLED", took=1.0)    # the third real run
    sim.run(2.5)
    assert sim.state("amp_enabled") == "stuck" and "the reset tool ran 3 times" in sim.named("stuck")[0]["text"]


def test_the_tool_never_runs_while_a_radar_process_is_alive():
    sim = enabled(started())
    sim.tool_done(3, "ASSUME_ENABLED", took=1.0)
    sim.radar = (900,)                              # a radar appears (the status is not ok: it should not have)
    sim.run(300.0)
    assert len(sim.tool_starts) == 1
    assert sigints(sim) and sigkills(sim)           # ... and it is stopped, as with any not-ok status
    sim.radar = ()
    sim.run(1.0)
    assert len(sim.tool_starts) == 2                # gone: the tool runs again at once
    for _ in range(2):
        sim.tool_done(3, "ASSUME_ENABLED", took=1.0)
        sim.run(2.5)
    assert sim.state("amp_enabled") == "stuck" and len(sim.tool_starts) == 3
    sim.radar = (901,)
    sim.run(200.0)
    assert len(sim.tool_starts) == 3


def test_the_28_v_going_away_ends_a_stuck_state():
    sim = enabled(started())
    for _ in range(3):
        sim.tool_done(3, "ASSUME_ENABLED", took=1.0)
        sim.run(2.5)
    assert sim.state("amp_enabled") == "stuck"
    sim.amp.update(voltage_v=0.1, current_a=0.0)
    sim.run(3.0)
    assert sim.state("amp_enabled") == "idle" and not sim.ok and not sim.tripped     # unpowered now


# ---- the order of things around a run of the tool

def test_the_tripped_status_is_published_a_round_before_the_tool_starts():
    sim = started()
    sim.amp["current_a"] = ENABLED_A
    while sim.ok:
        sim.round()
    assert sim.tripped and sim.tool_starts == [] and sim.state("amp_enabled") == "resetting"
    sim.round()
    assert sim.tool_starts == [sim.t]


def test_while_another_run_of_the_tool_holds_its_lock_the_watcher_waits_and_then_judges_afresh():
    sim = started()
    sim.tool_busy = True                            # the manager, or the power service's hook, is running it
    sim.amp["current_a"] = ENABLED_A
    sim.run(20.0)
    assert sim.tripped and sim.tool_starts == []
    sim.amp["current_a"] = 0.156                    # that run did the job
    sim.run(2.0)
    sim.tool_busy = False
    sim.run(60.0)
    assert sim.ok and sim.tool_starts == []         # nothing was queued behind it
    sim.tool_busy = True                            # ... and if it did not, the watcher's own run follows
    sim.amp["current_a"] = ENABLED_A
    sim.run(20.0)
    sim.tool_busy = False
    sim.round()
    assert len(sim.tool_starts) == 1


def test_a_run_of_the_tool_that_is_never_reported_does_not_latch_the_logic():
    sim = enabled(started())                        # the caller fails between the decision and the start
    sim.run(44.0)
    assert sim.logic.tool_running and len(sim.tool_starts) == 1
    sim.run(2.0)                                    # 30 s + 5 s + 10 s after the start: written off
    assert not sim.logic.tool_running and sim.logic.last_tool["name"] == "ASSUME_ENABLED"
    assert "lost track" in sim.logic.last_tool["text"]
    sim.run(3.0)
    assert len(sim.tool_starts) == 2


def test_while_the_five_seconds_count_the_status_stays_ok():
    # Deliberate: every start of the SDR by a tool enables the amplifier for half a second, and
    # the radar manager must not be turned away for each of those.
    sim = started()
    sim.amp["current_a"] = ENABLED_A
    sim.run(4.0)
    assert sim.ok and sim.state("amp_enabled") == "pending"


# ---- stopping a radar: not while it sets up the SDR, and not fooled by flapping

def test_a_radar_younger_than_the_set_up_allowance_is_not_signalled():
    # ORCA has no SIGINT handler until the SDR is set up (17 s at a first contact); a radar that
    # dies there leaves the amplifier enabled.
    sim = started()
    sim.amp["temp_c"] = 60.0
    sim.run(60.0)
    sim.radar_age = 0.0
    sim.radar = (55,)
    sim.run(64.5)
    assert sim.signals == [] and "radar_setting_up" in sim.logic.warnings
    assert sim.logic.snapshot(sim.t)["radar"]["setting_up"] == [55]
    sim.run(1.0)
    assert [(sig, pids) for _, sig, pids in sim.signals] == [(signal.SIGINT, (55,))]
    assert "radar_setting_up" not in sim.logic.warnings


def test_an_old_radar_is_stopped_while_a_young_one_next_to_it_is_left_until_it_is_old_enough():
    sim = started()
    sim.radar = (10,)
    sim.amp["temp_c"] = 60.0
    sim.run(3.0)
    sim.radar_age = 0.0
    sim.radar = (10, 11)
    sim.run(10.0)
    assert {pids for _, _, pids in sim.signals} == {(10,)}
    sim.radar = (11,)
    sim.run(60.0)
    assert [pids for _, sig, pids in sim.signals if pids == (11,)][:1] == [(11,)]


def test_after_the_watcher_signalled_a_radar_the_pin_counts_as_not_known_safe():
    sim = started()
    sim.radar = (7,)
    sim.amp["temp_c"] = 60.0
    sim.run(8.0)
    assert sigints(sim) and sim.logic.signalled and sim.logic.persistent()["signalled"] is True
    sim.radar = ()
    sim.amp["temp_c"] = 30.0                        # it exited cleanly: shutdown current, seen in the readings
    sim.run(3.0)
    assert not sim.logic.signalled


def test_telemetry_that_comes_and_goes_does_not_keep_a_radar_from_being_stopped():
    # The reviewer's scenario: 2 s of readings, 4 s without, the manager dead.
    sim = started()
    sim.radar = (1,)
    for _ in range(8):
        sim.telemetry = True
        sim.run(2.0)
        sim.telemetry = False
        sim.run(4.0)
    assert sigints(sim) and sigints(sim)[0] - 105.0 < 40.0


# ---- no telemetry, no radar, and the pin not known to be safe

def blind(sim):
    """The reviewer's scenario: the watcher stops a radar after telemetry is lost; it is killed and
    leaves the amplifier enabled; telemetry stays lost."""
    sim.radar = (1234,)
    sim.run(2.0)
    sim.telemetry = False
    sim.amp["current_a"] = ENABLED_A
    sim.run(30.0)
    assert sigkills(sim) and sim.logic.signalled
    sim.radar = ()
    sim.clear()
    return sim


def test_an_amplifier_left_enabled_by_a_stopped_radar_is_reset_without_telemetry():
    sim = blind(started())
    sim.run(19.5)
    assert sim.tool_starts == [] and not sim.tripped            # 20 s without a radar first
    sim.run(1.5)
    assert len(sim.tool_starts) == 1 and sim.tripped and not sim.ok and sim.state("pin") == "unknown"
    trip, = sim.named("trip", condition="pin")
    assert trip["severity"] == "urgent" and "the watcher signalled a radar" in trip["text"]
    assert sim.logic.reason.startswith("amplifier pin not known to be safe, with no amplifier telemetry")
    sim.tool_done(3, "ASSUME_ENABLED")              # it did not work: again after 5 minutes, not before
    sim.run(298.0)
    assert len(sim.tool_starts) == 1 and sim.tripped
    sim.run(3.0)
    assert len(sim.tool_starts) == 2
    sim.pin_safe = True
    sim.tool_done(0)                                # verified: the matter ends, with telemetry still lost
    sim.run(1.0)
    assert not sim.tripped and sim.state("pin") == "ok" and not sim.logic.signalled
    assert sim.named("clear", condition="pin") and not sim.ok       # still no telemetry
    sim.run(900.0)
    assert len(sim.tool_starts) == 2


def test_a_record_that_does_not_say_safe_is_enough_without_telemetry():
    sim = started()
    sim.pin_safe = False                            # the manager launched a radar and died with it, say
    sim.telemetry = False
    sim.run(19.0)
    assert sim.tool_starts == []                    # 20 s after the last reading
    sim.run(1.5)
    assert len(sim.tool_starts) == 1 and "the shared record does not say safe" in sim.named("trip")[0]["text"]
    # With the override the status would otherwise be ok: this still trips.
    sim = started()
    sim.override, sim.pin_safe, sim.telemetry = True, False, False
    sim.run(30.0)
    assert sim.tripped and not sim.ok and len(sim.tool_starts) == 1


@pytest.mark.parametrize("change", [{"sdr": L.SDR_BOOTLOADER}, {"sdr": L.SDR_ABSENT}, {"sdr": L.SDR_UNKNOWN},
                                    {"pin_safe": True}, {"radar": (5,)}])
def test_the_tool_is_not_run_blind_when_the_pin_is_safe_or_cannot_be_reached(change):
    sim = started()
    sim.pin_safe, sim.telemetry = False, False
    for key, value in change.items():
        setattr(sim, key, value)
    sim.run(900.0)
    assert sim.tool_starts == [] and not sim.tripped and sim.state("pin") == "ok"


def test_a_telemetry_restart_alone_never_causes_a_blind_run():
    sim = started()
    sim.pin_safe = False
    for _ in range(20):                             # the 12 s gap of every telemetry start, again and again
        sim.telemetry = False
        sim.run(15.0)
        sim.telemetry = True
        sim.run(10.0)
    assert sim.tool_starts == [] and not sim.tripped


def test_when_telemetry_returns_the_current_decides_again():
    sim = blind(started())
    sim.run(21.0)
    sim.tool_done(6, "BUSY", took=1.0)
    sim.telemetry = True                            # readings are back and say enabled
    sim.run(4.0)
    assert sim.state("pin") == "ok" and sim.state("amp_enabled") == "pending"
    sim.run(3.0)
    assert sim.state("amp_enabled") == "resetting" and len(sim.tool_starts) == 2


def test_the_blind_run_waits_for_another_run_of_the_tool():
    sim = blind(started())
    sim.tool_busy = True
    sim.run(60.0)
    assert sim.tripped and sim.tool_starts == []
    sim.tool_busy = False
    sim.round()
    assert len(sim.tool_starts) == 1


# ---- the status, and anything at all as input

def test_the_status_has_the_fields_the_manager_reads_and_the_detail():
    sim = started()
    sim.amp["temp_c"] = 56.0
    sim.radar = (321,)
    sim.run(2.0)
    status = sim.logic.snapshot(sim.t, False)
    assert status["ok"] is False and status["tripped"] is True and "reached 55 C" in status["reason"]
    assert status["problems"] == [{"condition": "amp_temp", "text": status["reason"], "trip": True}]
    c = status["conditions"]
    assert set(c) == {"telemetry", "amp_temp", "case_temp", "voltage", "amp_enabled", "pin"}
    assert c["amp_temp"]["state"] == "trip" and c["amp_temp"]["value"] == 56.0
    assert c["amp_temp"]["since_uptime_s"] == pytest.approx(sim.t - 1.0, abs=0.5)
    assert c["telemetry"]["state"] == "ok" and c["amp_enabled"]["state"] == "idle"
    assert c["pin"] == {"state": "ok", "since_uptime_s": None, "record_safe": True, "signalled_by_watcher": False,
                        "sdr": "ready"}
    assert status["amp"]["current_a"] == 0.156 and status["radar"] == {
        "running": True, "pids": [321], "stop": "idle", "stop_since_uptime_s": None, "setting_up": []}
    assert status["reset_tool"] == {"running": False, "last": None}
    assert status["counters"]["trips"] == 1 and status["limits"]["temp_trip_c"] == 55.0
    assert status["warnings"][0]["warning"] == "amp_temp_high"
    assert set(status["loop"]) == {"last_gap_s", "recent_longest_gap_s", "longest_gap_s"}


def test_the_recent_longest_gap_forgets_after_a_minute_or_two():
    sim = started()
    sim.round(dt=2.0)
    assert sim.logic.loop["recent_longest_gap_s"] == 2.0
    sim.run(125.0)
    assert sim.logic.loop["recent_longest_gap_s"] == 0.25 and sim.logic.loop["longest_gap_s"] == 2.0


def test_no_input_whatever_makes_the_logic_raise():
    # After the reviewer's fuzz: random readings, radars, records and tool results.
    rng = random.Random(1)

    def value(low, high):
        return None if rng.random() < 0.1 else rng.uniform(low, high)

    for _ in range(40):
        t = 100.0
        logic = L.WatcherLogic(settings(), t, rng.choice([None, {"trips": {"amp_temp": True}, "enabled": "stuck"}]))
        up, tool_end = 99.0, None
        for _ in range(600):
            t += rng.choice([0.5, 0.5, 0.5, 0.1, 4.0])
            if rng.random() < 0.9:
                up = t - rng.uniform(0, 0.6)
            sample = None if rng.random() < 0.1 else L.Sample(
                up, value(-60, 150), value(-60, 150), rng.choice([0.156, 2.58, None, value(-1, 12)]),
                rng.choice([28.0, 0.1, 22.0, value(-2, 40)]))
            inp = L.Inputs(sample=sample, telemetry_reason="x", override=rng.random() < 0.1,
                           radar=rng.choice([(), (), ((5, rng.uniform(0, 100)),)]), pin_safe=rng.random() < 0.5,
                           sdr=rng.choice([L.SDR_READY, L.SDR_BOOTLOADER, L.SDR_ABSENT, L.SDR_UNKNOWN]),
                           tool_busy=rng.random() < 0.1)
            if any(a.kind == "run_tool" for a in logic.step(t, inp)):
                tool_end = t + 3
            if tool_end is not None and t >= tool_end and logic.tool_running:
                logic.tool_done(t, rng.choice([0, 2, 3, 4, 5, 6, 7, 8, 64, 99]), "x", 3.0)
                tool_end = None
            status = logic.snapshot(t, inp.override)
            assert status["ok"] == (not status["problems"]) and (not status["tripped"] or not status["ok"])
            logic.persistent()
