"""The safety watcher's decisions (radar_runner/watcher/logic.py), in simulated time with injected readings."""
import signal

import pytest
from watcher_fakes import ENABLED_A, GOOD, Sim, telemetry_status

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

def test_debounce_needs_readings_in_a_row_and_a_time_span():
    d = L.Debounce(n=3, span_s=1.0, hold_s=30.0, max_gap_s=3.0)
    assert not d.update(0.0, L.ON) and not d.update(0.5, L.ON)
    assert not d.update(1.0, None)                  # neither: the count starts again
    assert not d.update(1.5, L.ON) and not d.update(2.0, L.ON) and not d.active
    assert d.update(2.5, L.ON) and d.active and d.since == 2.5
    # Three readings in a row are not enough if they come within less than the span.
    d = L.Debounce(3, 1.0, 30.0, 3.0)
    assert [d.update(t, L.ON) for t in (0.0, 0.1, 0.2)] == [False] * 3
    assert d.update(1.0, L.ON)
    # ... and a long enough span is not enough with fewer than three readings.
    d = L.Debounce(3, 1.0, 30.0, 3.0)
    assert not d.update(0.0, L.ON) and not d.update(2.5, L.ON)
    assert d.update(3.0, L.ON)


def test_debounce_turns_off_only_after_the_hold_and_a_gap_starts_the_count_again():
    d = L.Debounce(3, 1.0, 30.0, 3.0)
    for t in (0.0, 0.5, 1.0):
        d.update(t, L.ON)
    assert d.active
    t = 1.0
    while t < 30.5:
        t += 0.5
        assert not d.update(t, L.OFF)
    assert d.update(31.5, L.OFF) and not d.active       # 30 s after the first OFF reading (1.5)
    # A gap in the readings longer than max_gap_s: the readings before it do not count.
    d = L.Debounce(3, 1.0, 30.0, 3.0)
    assert [d.update(t, L.ON) for t in (0.0, 0.5, 5.0, 5.5)] == [False] * 4
    assert d.update(6.0, L.ON)


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


def test_pi_readings_are_taken_only_from_a_healthy_source():
    data = telemetry_status(500.0, pi_temp=61.5, throttled=["throttled"])
    assert L.pi_sample(read(data)) == L.PiSample(499.0, 61.5, ("throttled",))
    data["sources"]["pi"]["health"] = "stale"
    assert L.pi_sample(read(data)) is None
    assert L.pi_sample(read(None, S.MISSING)) is None and L.pi_sample(read({"sources": 3})) is None


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


def test_readings_without_a_needed_value_count_as_lost_telemetry():
    sim = started()
    sim.amp["case_temp_c"] = None
    sim.run(3.5)
    assert not sim.ok and not sim.tripped
    assert sim.logic.reason.endswith("missing or not plausible: case_temp_c")
    # Without the 28 V the amplifier's own sensor is not needed (it reads nothing then).
    sim = started(allow_unpowered=True)
    sim.amp.update(voltage_v=0.2, temp_c=None, current_a=0.0)
    sim.run(5.0)
    assert sim.ok and sim.state("telemetry") == "ok"


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


def test_hot_readings_interrupted_by_a_normal_one_do_not_trip():
    sim = started()
    for _ in range(10):
        sim.amp["temp_c"] = 60.0
        sim.run(1.0)                                # two hot readings
        sim.amp["temp_c"] = 30.0
        sim.run(0.5)                                # one normal reading
    assert sim.ok and sim.named("trip") == []


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


def test_the_amplifier_sensor_is_ignored_without_the_28_v_but_the_case_sensor_is_not():
    sim = started()
    sim.amp.update(voltage_v=0.3, current_a=0.0, temp_c=99.0)       # what the sensor reads unpowered means nothing
    sim.run(10.0)
    assert not sim.tripped and sim.state("amp_temp") == "ok"
    sim.amp["case_temp_c"] = 57.0
    sim.run(2.0)
    assert sim.tripped and sim.state("case_temp") == "trip"


def test_cold_is_a_warning_only():
    sim = started()
    sim.amp.update(temp_c=-45.0, case_temp_c=-47.0)
    sim.run(10.0)
    assert sim.ok and sim.logic.warnings["amp_cold"]["text"] == "amplifier below -40 C"
    sim.amp.update(temp_c=-36.0, case_temp_c=-37.0)
    sim.run(32.0)
    assert "amp_cold" not in sim.logic.warnings


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


def test_the_rail_falling_during_a_power_loss_is_not_a_supply_trip():
    sim = started()
    sim.power_lost = True                           # the power service reports the loss within 0.25 s
    for volts in (25.0, 23.0, 21.5, 15.0, 4.0, 0.2, 0.2, 0.2):
        sim.amp["voltage_v"] = volts
        sim.run(1.0)
    assert not sim.tripped and sim.named("trip") == [] and not sim.ok
    assert sim.state("voltage") == "unpowered" and sim.signals == [] and sim.tool_starts == []
    # The normal end of a day: nothing in the event log that asks for attention.
    assert [(e["event"], e["severity"]) for e in sim.events] == [("unpowered", "info"), ("not_ok", "info")]
    # The same voltages with the power service saying nothing about a loss: a supply fault.
    sim = started()
    sim.amp["voltage_v"] = 23.0
    sim.run(2.0)
    assert sim.tripped and sim.named("trip", condition="voltage")


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
    assert "after 3 runs of the reset tool (last: OK (exit 0))" in stuck["text"] and "cut the power" in stuck["text"]
    assert sim.logic.reason.startswith("AMPLIFIER STUCK ENABLED with no radar process: 3 runs")
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


@pytest.mark.parametrize("code, name", [(2, "NOT_FOUND"), (4, "TIMEOUT"), (5, "FAILED"), (6, "BUSY"), (7, "AMBIGUOUS"),
                                        (8, "STATE_FILE"), (64, "USAGE")])
def test_results_that_changed_nothing_are_retried_after_five_seconds_then_stuck(code, name):
    sim = enabled(started())
    ends = []
    for _ in range(3):
        sim.tool_done(code, f"{name}: stand-in", took=1.0)
        ends.append(sim.t)
        sim.run(5.5)
    assert [round(start - end, 1) for start, end in zip(sim.tool_starts[1:], ends)] == [5.0, 5.0]
    assert sim.state("amp_enabled") == "stuck" and f"last: {name} (exit {code})" in sim.named("stuck")[0]["text"]
    results = sim.named("reset_tool_result")
    assert [r["code"] for r in results] == [code] * 3 and all(r["severity"] == "warning" for r in results)
    assert sim.logic.counters["tool_ok"] == 0 and not sim.ok and sim.tripped


def test_a_later_success_after_failures_ends_the_matter():
    sim = enabled(started())
    sim.tool_done(6, "BUSY", took=1.0)              # something else had the SDR
    sim.run(5.5)
    sim.amp["current_a"] = 0.156
    sim.tool_done(0)
    sim.run(2.0)
    assert sim.ok and sim.named("stuck") == [] and len(sim.tool_starts) == 2


def test_the_tool_never_runs_while_a_radar_process_is_alive():
    sim = enabled(started())
    sim.tool_done(6, "BUSY", took=1.0)
    sim.radar = (900,)                              # a radar appears (the status is not ok: it should not have)
    sim.run(300.0)
    assert len(sim.tool_starts) == 1
    assert sigints(sim) and sigkills(sim)           # ... and it is stopped, as with any not-ok status
    sim.radar = ()
    sim.run(1.0)
    assert len(sim.tool_starts) == 2                # gone: the tool runs again at once
    # The same once stuck.
    sim.tool_done(6, "BUSY", took=1.0)
    sim.run(5.5)
    sim.tool_done(6, "BUSY", took=1.0)
    sim.run(5.5)
    assert sim.state("amp_enabled") == "stuck" and len(sim.tool_starts) == 3
    sim.radar = (901,)
    sim.run(200.0)
    assert len(sim.tool_starts) == 3


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


def test_the_28_v_going_away_ends_a_stuck_state():
    sim = enabled(started())
    for _ in range(3):
        sim.tool_done(6, "BUSY", took=1.0)
        sim.run(5.5)
    assert sim.state("amp_enabled") == "stuck"
    sim.amp.update(voltage_v=0.1, current_a=0.0)
    sim.run(3.0)
    assert sim.state("amp_enabled") == "idle" and not sim.ok and not sim.tripped     # unpowered now


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


def test_a_radar_started_while_not_ok_gets_the_grace_period_from_its_start():
    sim = started()
    sim.amp["temp_c"] = 60.0
    sim.run(60.0)
    sim.radar = (55,)
    sim.run(4.75)
    assert sim.signals == []
    sim.run(0.5)
    assert len(sigints(sim)) == 1


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

def test_a_hot_or_throttled_pi_is_a_warning_only():
    sim = started()
    sim.pi = {"soc_temp_c": 86.0, "throttled": ("throttled", "frequency capped")}
    sim.run(20.0)
    assert sim.ok and not sim.tripped and sim.signals == []
    assert sim.logic.warnings["pi_temp_high"]["text"] == "Pi SoC temperature 86.0 C, at or above 80 C"
    assert sim.logic.warnings["pi_throttled"]["text"] == "the Pi is throttled: throttled, frequency capped"
    assert sorted(e["warning"] for e in sim.named("warning")) == ["pi_temp_high", "pi_throttled"]
    sim.pi = {"soc_temp_c": 60.0, "throttled": ()}
    sim.run(40.0)
    assert sim.logic.warnings == {} and len(sim.named("warning_cleared")) == 2


def test_gaps_in_the_telemetry_loop_are_a_warning_only():
    sim = started()
    sim.gap = 1.6
    sim.run(2.0)
    assert sim.ok and "stalled for 1.6 s" in sim.logic.warnings["telemetry_gaps"]["text"]


# ---- the watcher's own loop

def test_after_a_stall_of_the_watcher_counts_of_readings_start_again():
    sim = started()
    sim.amp["temp_c"] = 60.0
    sim.run(1.0)                                    # two hot readings
    sim.round(dt=10.0)                              # the loop hangs for 10 s; the next reading is hot too
    assert sim.ok and sim.logic.counters["stalls"] == 1 and sim.named("stall")
    assert sim.logic.loop == {"last_gap_s": 10.0, "longest_gap_s": 10.0}
    sim.run(1.0)
    assert not sim.ok                               # three in a row after the stall
    # The same for the 5 s of the enabled rule.
    sim = started()
    sim.amp["current_a"] = ENABLED_A
    sim.run(4.0)
    sim.round(dt=10.0)
    sim.run(4.0)
    assert sim.tool_starts == []
    sim.run(1.5)
    assert len(sim.tool_starts) == 1


# ---- the status

def test_the_status_has_the_fields_the_manager_reads_and_the_detail():
    sim = started()
    sim.amp["temp_c"] = 56.0
    sim.radar = (321,)
    sim.run(2.0)
    status = sim.logic.snapshot(sim.t, False)
    assert status["ok"] is False and status["tripped"] is True and "reached 55 C" in status["reason"]
    assert status["problems"] == [{"condition": "amp_temp", "text": status["reason"], "trip": True}]
    c = status["conditions"]
    assert set(c) == {"telemetry", "amp_temp", "case_temp", "voltage", "amp_enabled"}
    assert c["amp_temp"]["state"] == "trip" and c["amp_temp"]["value"] == 56.0
    assert c["amp_temp"]["since_uptime_s"] == pytest.approx(sim.t - 1.0, abs=0.5)
    assert c["telemetry"]["state"] == "ok" and c["amp_enabled"]["state"] == "idle"
    assert status["amp"]["current_a"] == 0.156 and status["radar"] == {
        "running": True, "pids": [321], "stop": "idle", "stop_since_uptime_s": None}
    assert status["reset_tool"] == {"running": False, "last": None}
    assert status["counters"]["trips"] == 1 and status["limits"]["temp_trip_c"] == 55.0
    assert status["warnings"][0]["warning"] == "amp_temp_high"
