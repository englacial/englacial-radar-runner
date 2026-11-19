"""Tests of the pure decision logic (power/logic.py) in simulated time."""
import random
import unittest

from power import logic as L

from .helpers import Outage, Sim, make_config

T0 = 10.0   # when the simulated outage starts


def flaky(fn, rate, seed):
    """Wrap a signal function so that a fraction of the reads fail."""
    rng = random.Random(seed)
    return lambda t: None if rng.random() < rate else fn(t)


class SignalTrackerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.s = L.SignalTracker("x", self.cfg)
        self.t = 0.0

    def feed(self, raw, n=1, dt=0.2):
        for _ in range(n):
            self.s.update(self.t, raw)
            self.t = round(self.t + dt, 6)

    def test_starts_unknown_and_needs_a_second_of_present(self):
        self.feed(L.PRESENT, 5)            # samples at 0 .. 0.8 s
        self.assertEqual(self.s.state, L.UNKNOWN)
        self.feed(L.PRESENT)               # 1.0 s
        self.assertEqual(self.s.state, L.PRESENT)

    def test_loss_needs_two_consecutive_samples(self):
        self.feed(L.PRESENT, 10)
        for _ in range(20):                # isolated loss samples never confirm
            self.feed(L.LOSS)
            self.feed(L.PRESENT)
        self.assertEqual(self.s.state, L.PRESENT)
        self.feed(L.LOSS, 2)
        self.assertEqual(self.s.state, L.LOSS)

    def test_restore_needs_continuous_present(self):
        self.feed(L.LOSS, 2)
        self.feed(L.PRESENT, 3)
        self.assertTrue(self.s.restoring)
        self.feed(L.LOSS)                  # interrupted: start again
        self.assertFalse(self.s.restoring)
        self.feed(L.PRESENT, 5)
        self.assertEqual(self.s.state, L.LOSS)
        self.feed(L.PRESENT)
        self.assertEqual(self.s.state, L.PRESENT)

    def test_isolated_errors_do_not_change_the_state(self):
        self.feed(L.PRESENT, 10)
        for _ in range(50):
            self.feed(None, 3)             # 0.6 s of errors, below unreadable_after_s
            self.feed(L.PRESENT)
        self.assertEqual(self.s.state, L.PRESENT)
        self.assertEqual(self.s.errors, 150)

    def test_sustained_errors_make_it_unreadable_then_unknown_then_present(self):
        self.feed(L.PRESENT, 10)
        self.feed(None, 10)                # errors from t to t + 1.8 s
        self.assertEqual(self.s.state, L.PRESENT)
        self.feed(None)                    # 2.0 s
        self.assertEqual(self.s.state, L.UNREADABLE)
        self.feed(L.PRESENT)
        self.assertEqual(self.s.state, L.UNKNOWN)
        self.feed(L.PRESENT, 5)
        self.assertEqual(self.s.state, L.PRESENT)

    def test_loss_longer_than_the_capacitor_lasts_is_faulty(self):
        self.feed(L.LOSS, 2)
        self.feed(L.LOSS, int(179 / 0.2))
        self.assertEqual(self.s.state, L.LOSS)
        self.feed(L.LOSS, 10)
        self.assertEqual(self.s.state, L.FAULTY)
        self.assertFalse(self.s.healthy)
        # Needs 60 s of uninterrupted present to recover.
        self.feed(L.PRESENT, int(59 / 0.2))
        self.feed(L.LOSS)
        self.feed(L.PRESENT, int(59 / 0.2))
        self.feed(None)
        self.feed(L.PRESENT, int(59 / 0.2))
        self.assertEqual(self.s.state, L.FAULTY)
        self.feed(L.PRESENT, 10)
        self.assertEqual(self.s.state, L.PRESENT)


class OutageTest(unittest.TestCase):
    def test_power_present_does_nothing(self):
        sim = Sim().run(900)
        self.assertEqual(sim.actions, [])
        self.assertEqual(sim.state_names(), ["normal"])
        self.assertEqual(sim.logic.warnings, {})
        self.assertFalse(sim.logic.stop_requested)
        self.assertIs(sim.logic.snapshot()["power_present"], True)

    def test_short_outage_rides_through(self):
        sim = Sim(source=Outage(T0, T0 + 5)).run(40)
        self.assertEqual(sim.kinds(), [("hook", "stop_radar")])
        t_stop = sim.find("hook", "stop_radar")[0][0]
        self.assertLessEqual(t_stop - T0, 0.5)       # radar stop is prompt
        self.assertEqual(sim.state_names(), ["normal", "loss", "normal"])
        self.assertFalse(sim.logic.stop_requested)
        self.assertEqual(sim.power(), [])
        t_back = [t for t, s in sim.states if s == "normal"][-1]
        self.assertAlmostEqual(t_back, T0 + 5 + 1.0, delta=0.45)   # restore confirmed in about 1 s

    def test_outage_shorter_than_lag_plus_ride_through_does_not_commit(self):
        # The ride-through runs from when GPIO agrees, about 1.8 s after the loss.
        for length in (7.8, 9.0, 9.6):
            sim = Sim(source=Outage(T0, T0 + length)).run(40)
            self.assertEqual(sim.power(), [], length)
            self.assertEqual(sim.state_names(), ["normal", "loss", "normal"], length)

    def test_long_outage_commits_and_powers_off(self):
        sim = Sim(source=Outage(T0)).run(60)
        self.assertEqual(sim.kinds(), [("hook", "stop_radar"), ("hook", "amp_safe"), ("sync",),
                                       ("record",), ("power", "poweroff")])
        t_stop = sim.find("hook", "stop_radar")[0][0]
        t_commit = sim.find("hook", "amp_safe")[0][0]
        self.assertLessEqual(t_stop - T0, 0.5)
        t_agree = sim.logged("gpio: present -> loss")[0][0]
        self.assertAlmostEqual(t_agree, T0 + 1.5 + 0.3, delta=0.15)
        self.assertGreaterEqual(t_commit - t_agree, 8.0 - 1e-6)    # ride-through from the agreement
        self.assertLessEqual(t_commit - t_agree, 8.25)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))
        self.assertLessEqual(sim.power()[0][0] - t_commit, 0.5)
        self.assertEqual(sim.state_names(), ["normal", "loss", "committing", "shutdown"])
        self.assertTrue(sim.logic.stop_requested)

    def test_outage_already_present_at_start(self):
        sim = Sim(source=Outage(0.0)).run(30)
        self.assertLessEqual(sim.find("hook", "stop_radar")[0][0], 0.5)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))
        self.assertLessEqual(sim.power()[0][0], 10.6)

    def test_service_restart_in_the_middle_of_an_outage(self):
        # A fresh PowerLogic fed a loss that both signals already show: it commits,
        # and counts the ride-through from its own confirmation.
        sim = Sim(source=Outage(-20.0)).run(30)
        self.assertEqual(sim.find("hook", "stop_radar")[0][0], 0.2)
        t_commit = sim.logged("COMMIT to shutdown: loss agreed for 8")[0][0]
        self.assertGreaterEqual(t_commit, 0.2 + 8.0 - 1e-6)
        self.assertLessEqual(t_commit, 0.2 + 8.25)
        self.assertEqual([p[1:] for p in sim.power()], [("poweroff", 0)])

    def test_second_outage_stops_the_radar_again(self):
        src1, src2 = Outage(T0, T0 + 3), Outage(T0 + 20, T0 + 23)
        sim = Sim()
        sim.current = lambda t: min(src1.current(t), src2.current(t))
        sim.gpio = lambda t: min(src1.gpio(t), src2.gpio(t))
        sim.run(60)
        self.assertEqual(sim.kinds(), [("hook", "stop_radar"), ("hook", "stop_radar")])
        self.assertEqual(sim.state_names(), ["normal", "loss", "normal", "loss", "normal"])

    def test_power_back_before_the_final_check_reboots(self):
        # The sync takes 3 s; power returns while it runs.
        sim = Sim(source=Outage(T0, T0 + 11.5))
        sim.delay["sync"] = 3.0
        sim.run(60)
        self.assertLess(sim.logged("COMMIT to shutdown")[0][0], T0 + 11.5)
        self.assertEqual(sim.kinds()[-1], ("power", "reboot"))
        self.assertEqual(len(sim.power()), 1)

    def test_power_back_just_before_the_final_check_waits_for_confirmation(self):
        # Power returns during the sync: the restore is seen but not yet confirmed
        # when the final check is reached, so the check waits for it.
        sim = Sim(source=Outage(T0, T0 + 10.5))
        sim.delay["sync"] = 1.0
        sim.run(60)
        self.assertLess(sim.logged("COMMIT to shutdown")[0][0], T0 + 10.5)
        t_record = sim.find("record")[0][0]
        self.assertLess(t_record, T0 + 10.5 + 1.0)        # reached before the restore is confirmed
        (t_power, kind, level), = sim.power()
        self.assertEqual((kind, level), ("reboot", 0))
        self.assertAlmostEqual(t_power, T0 + 10.5 + 1.0, delta=0.35)

    def test_gpio_flicker_at_final_check_cannot_delay_it_for_long(self):
        # GPIO alternates every sample after the commit: never confirmed present.
        src = Outage(T0)
        sim = Sim(source=src)
        sim.delay["sync"] = 0.5
        sim.gpio = lambda t: src.gpio(t) if t < T0 + 10.3 else int(round(t / 0.2)) % 2
        sim.run(60)
        t_commit = sim.find("hook", "amp_safe")[0][0]
        self.assertLess(t_commit, T0 + 10.3)
        (t_power, kind, level), = sim.power()
        self.assertEqual(kind, "poweroff")
        self.assertLessEqual(t_power - t_commit, 0.5 + 2.5)

    def test_no_abort_once_committed(self):
        # Power returns after the power-off command: nothing more happens.
        sim = Sim(source=Outage(T0, T0 + 12))
        sim.survive = True
        sim.run(29)
        self.assertEqual(sim.power(), [(sim.power()[0][0], "poweroff", 0)])
        self.assertLess(sim.power()[0][0], T0 + 12)
        self.assertEqual(sim.logic.state, "shutdown")

    def test_gpio_lag_is_not_a_contradiction(self):
        for lag in (1.5, 4.0):
            sim = Sim(source=Outage(T0, gpio_lag=lag)).run(40)
            self.assertEqual(sim.power()[0][1:], ("poweroff", 0), lag)
            self.assertGreaterEqual(sim.power()[0][0] - T0, lag + 8.0, lag)
            self.assertLessEqual(sim.power()[0][0] - T0, lag + 9.0, lag)
            self.assertEqual(sim.logged("disagree"), [], lag)

    def test_gpio_agreeing_late_still_shuts_down(self):
        sim = Sim(source=Outage(T0, gpio_lag=12.0)).run(40)
        self.assertEqual(len(sim.logged("warning: signals disagree")), 1)
        (t_power, kind, _), = sim.power()
        self.assertEqual(kind, "poweroff")
        self.assertAlmostEqual(t_power, T0 + 12.0 + 8.0 + 0.5, delta=0.4)   # 8 s after it agreed


class DisagreementTest(unittest.TestCase):
    def test_current_says_loss_gpio_says_present_never_shuts_down(self):
        # A false "loss" from the current signal: power is present, so the capacitor stays charged.
        sim = Sim(source=Outage(T0, gpio_lag=1e9, dv_dt=0.0)).run(120)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.kinds(), [("hook", "stop_radar")])
        self.assertIn("signals_disagree", sim.logic.warnings)
        self.assertTrue(sim.logic.stop_requested)
        t_warn = sim.logged("warning: signals disagree")[0][0]
        self.assertAlmostEqual(t_warn, T0 + 0.2 + 5.0, delta=0.45)
        # Longer than the capacitor could last: the current signal is marked faulty,
        # the loss indication ends, the radar may run again.
        sim.run(100)
        self.assertEqual(sim.logic.current.state, L.FAULTY)
        self.assertIn("current_faulty", sim.logic.warnings)
        self.assertNotIn("signals_disagree", sim.logic.warnings)
        self.assertEqual(sim.logic.state, "normal")
        self.assertFalse(sim.logic.stop_requested)
        sim.run(600)
        self.assertEqual(sim.power(), [])

    def test_gpio_stuck_low_with_current_fine(self):
        sim = Sim()
        sim.gpio = lambda t: 0 if T0 <= t < T0 + 400 else 1
        sim.run(300)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logic.gpio.state, L.FAULTY)
        self.assertEqual(sim.logic.state, "normal")
        t_faulty = sim.logged("gpio: loss -> faulty")[0][0]
        self.assertAlmostEqual(t_faulty, T0 + 180, delta=1.0)
        # A real outage while GPIO is faulty: the current signal alone decides.
        sim.current = lambda t: -1.0 if t >= 320 else 0.02
        sim.run(40)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))

    def test_faulty_gpio_recovers_after_a_minute_of_present(self):
        sim = Sim()
        sim.gpio = lambda t: 0 if T0 <= t < T0 + 200 else 1
        sim.run(T0 + 200 + 59)
        self.assertEqual(sim.logic.gpio.state, L.FAULTY)
        sim.run(2)
        self.assertEqual(sim.logic.gpio.state, L.PRESENT)
        self.assertEqual(sim.logic.warnings, {})

    def test_one_failed_read_during_a_disagreement_does_not_shut_down(self):
        # GPIO stuck low, current fine. A glitch on the I2C bus must not turn this
        # into "one signal says loss, the other is unreadable".
        sim = Sim()
        sim.gpio = lambda t: 0 if t >= T0 else 1
        sim.current = lambda t: None if (30.0 <= t < 30.2 or 40.0 <= t < 41.6) else 0.02
        sim.run(100)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logged("current: .* -> unreadable"), [])


class UnreadableTest(unittest.TestCase):
    def test_current_unreadable_gpio_low_shuts_down_by_default(self):
        src = Outage(T0)
        sim = Sim(source=src)
        sim.current = lambda t: None
        sim.run(40)
        self.assertIn(("power", "poweroff"), sim.kinds())
        t_stop = sim.find("hook", "stop_radar")[0][0]
        self.assertAlmostEqual(t_stop, T0 + 1.5 + 0.2, delta=0.25)
        self.assertGreaterEqual(sim.power()[0][0] - t_stop, 8.0 - 1e-6)
        self.assertLessEqual(sim.power()[0][0] - t_stop, 8.7)
        self.assertEqual(len(sim.logged("warning: current signal unreadable")), 1)

    def test_current_unreadable_gpio_low_quiesce_never_powers_off(self):
        src = Outage(T0)
        sim = Sim(source=src, single_signal_action="quiesce")
        sim.current = lambda t: None
        sim.run(100)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.kinds(), [("hook", "stop_radar")])
        self.assertTrue(sim.logic.stop_requested)
        self.assertEqual(sim.logic.state, "loss")
        self.assertIn("single_signal_quiesce", sim.logic.warnings)
        sim.run(600)
        self.assertEqual(sim.power(), [])

    def test_quiesce_still_shuts_down_when_both_agree(self):
        sim = Sim(source=Outage(T0), single_signal_action="quiesce").run(40)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))

    def test_gpio_unreadable_current_loss(self):
        for action, expect in (("shutdown", True), ("quiesce", False)):
            sim = Sim(source=Outage(T0, dv_dt=-0.01), single_signal_action=action)   # stays above 4.5 V
            sim.gpio = lambda t: None
            sim.run(60)
            self.assertEqual(bool(sim.power()), expect, action)
            self.assertEqual(sim.find("hook", "stop_radar")[0][0], T0 + 0.2)

    def test_current_dies_during_an_outage(self):
        src = Outage(T0)
        sim = Sim(source=src)
        sim.current = lambda t: None if t >= T0 + 3 else src.current(t)
        sim.run(40)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))
        self.assertLessEqual(sim.power()[0][0] - T0, 10.6)  # the agreement held throughout

    def test_both_unreadable_takes_no_action(self):
        sim = Sim()
        sim.current = lambda t: None
        sim.gpio = lambda t: None
        sim.run(600)
        self.assertEqual(sim.actions, [])
        self.assertEqual(sim.logic.state, "normal")
        self.assertIn("no_power_monitoring", sim.logic.warnings)
        self.assertIsNone(sim.logic.snapshot()["power_present"])
        self.assertEqual(len(sim.logged("warning: both power signals")), 1)   # logged once

    def test_both_become_unreadable_during_a_loss(self):
        src = Outage(T0)
        sim = Sim(source=src)
        sim.current = lambda t: None if t >= T0 + 3 else src.current(t)
        sim.gpio = lambda t: None if t >= T0 + 3 else src.gpio(t)
        sim.run(120)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logic.state, "normal")
        self.assertIn("no_power_monitoring", sim.logic.warnings)

    def test_intermittent_errors_with_power_present(self):
        sim = Sim()
        sim.current = flaky(sim.current, 0.3, 1)
        sim.gpio = flaky(sim.gpio, 0.3, 2)
        sim.run(1800)
        self.assertEqual(sim.actions, [])
        self.assertEqual(sim.state_names(), ["normal"])
        self.assertEqual(sim.logic.warnings, {})
        self.assertGreater(sim.logic.current.errors, 2000)
        self.assertEqual(sim.logged("unreadable"), [])

    def test_intermittent_errors_during_an_outage(self):
        for seed in range(20):
            src = Outage(T0)
            sim = Sim(source=src)
            sim.current = flaky(src.current, 0.3, seed)
            sim.gpio = flaky(src.gpio, 0.3, seed + 100)
            sim.run(40)
            self.assertEqual([p[1:] for p in sim.power()], [("poweroff", 0)], seed)
            self.assertLessEqual(sim.find("hook", "stop_radar")[0][0] - T0, 1.5, seed)
            self.assertLessEqual(sim.power()[0][0] - T0, 13.0, seed)   # errors do not restart the timer

    def test_intermittent_errors_during_a_short_outage(self):
        for seed in range(20):
            src = Outage(T0, T0 + 4)
            sim = Sim(source=src)
            sim.current = flaky(src.current, 0.3, seed)
            sim.gpio = flaky(src.gpio, 0.3, seed + 100)
            sim.run(60)
            self.assertEqual(sim.power(), [], seed)
            self.assertEqual(sim.logic.state, "normal", seed)


class LowVoltageTest(unittest.TestCase):
    def test_low_voltage_waives_the_ride_through(self):
        sim = Sim(source=Outage(T0, v0=4.4)).run(30)
        (t_power, kind, _), = sim.power()
        self.assertEqual(kind, "poweroff")
        # As soon as GPIO agrees (1.5 s lag + confirmation), not after 8 s.
        self.assertAlmostEqual(t_power, T0 + 1.5 + 0.3, delta=0.4)
        self.assertTrue(sim.logged("COMMIT to shutdown: capacitor at"))

    def test_voltage_falling_below_the_floor_during_the_ride_through(self):
        sim = Sim(source=Outage(T0, v0=4.6, dv_dt=-0.05)).run(30)   # crosses 4.5 V after 2 s
        t_power = sim.power()[0][0]
        self.assertGreater(t_power, T0 + 2.0)
        self.assertLess(t_power, T0 + 3.0)

    def test_low_voltage_alone_does_not_commit_without_a_loss_on_the_current_signal(self):
        # Capacitor low but charging (after a restore, or a cold start), GPIO stuck low:
        # the signals disagree, and low voltage does not replace the agreement.
        sim = Sim()
        sim.voltage = lambda t: 4.0
        sim.current = lambda t: 1.6
        sim.gpio = lambda t: 0 if t >= T0 else 1
        sim.run(400)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logged("COMMIT"), [])

    def test_discharging_below_the_floor_commits_whatever_gpio_says(self):
        # GPIO stuck high during a real outage with the capacitor already low.
        sim = Sim(source=Outage(T0, gpio_lag=1e9, v0=4.3)).run(60)
        t_commit, message = sim.logged("COMMIT to shutdown")[0]
        self.assertIn("capacitor discharging below floor", message)
        self.assertIn("gpio says present", message)
        self.assertGreaterEqual(t_commit, T0 + 0.2 + 2.0 - 1e-6)     # sustained for 2 s
        self.assertLessEqual(t_commit, T0 + 0.2 + 2.25)
        # GPIO still says present at the final check, the current signal says loss: power off.
        self.assertEqual([p[1:] for p in sim.power()], [("poweroff", 0)])

    def test_discharging_below_the_floor_after_a_long_outage_with_gpio_stuck_high(self):
        # From a full charge the floor is reached after about 35 s at this rate.
        sim = Sim(source=Outage(T0, gpio_lag=1e9, v0=5.19, dv_dt=-0.02)).run(120)
        self.assertIn("signals_disagree", sim.logic.warnings)
        t_power = sim.power()[0][0]
        self.assertAlmostEqual(t_power, T0 + (5.19 - 4.5) / 0.02 + 2.0, delta=1.0)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))

    def test_short_low_voltage_dips_do_not_commit_alone(self):
        for dip in (0.2, 0.4, 1.8):
            src = Outage(T0, gpio_lag=1e9)
            sim = Sim(source=src)
            sim.voltage = lambda t, dip=dip: 4.0 if 20.0 <= t < 20.0 + dip else 5.0
            sim.run(100)
            self.assertEqual(sim.power(), [], dip)

    def test_a_present_current_sample_restarts_the_low_voltage_timer(self):
        src = Outage(T0, gpio_lag=1e9, v0=4.3, dv_dt=0.0)
        sim = Sim(source=src)
        sim.current = lambda t: 0.02 if int(round(t / 0.2)) % 8 == 0 else src.current(t)   # every 1.6 s
        sim.run(100)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logic.current.state, L.LOSS)

    def test_read_errors_do_not_restart_the_low_voltage_timer(self):
        for seed in range(10):
            src = Outage(T0, gpio_lag=1e9, v0=4.3)
            sim = Sim(source=src)
            sim.current = flaky(src.current, 0.3, seed)
            sim.run(40)
            self.assertEqual([p[1:] for p in sim.power()], [("poweroff", 0)], seed)
            self.assertLessEqual(sim.power()[0][0] - T0, 4.5, seed)

    def test_discharging_below_the_floor_in_monitor_mode_and_with_quiesce(self):
        sim = Sim(cfg=make_config(armed=False), source=Outage(T0, T0 + 30, gpio_lag=1e9, v0=4.3)).run(25)
        self.assertEqual(sim.actions, [])
        self.assertEqual(sim.logic.state, "would_shutdown")
        # It is not a single-signal decision: single_signal_action does not apply.
        sim = Sim(source=Outage(T0, v0=4.3), single_signal_action="quiesce")
        sim.gpio = lambda t: None
        sim.run(40)
        self.assertEqual([p[1:] for p in sim.power()], [("poweroff", 0)])

    def test_low_voltage_with_power_present_does_nothing(self):
        sim = Sim()
        sim.voltage = lambda t: 3.0    # capacitor still charging after a cold start
        sim.run(300)
        self.assertEqual(sim.actions, [])
        self.assertEqual(sim.logic.warnings, {})

    def test_one_low_voltage_sample_is_not_enough(self):
        src = Outage(T0)
        sim = Sim(source=src)
        sim.voltage = lambda t: 0.0 if 13.0 <= t < 13.2 else src.voltage(t)
        sim.run(30)
        self.assertGreaterEqual(sim.power()[0][0] - T0, 8.0)


class ModeTest(unittest.TestCase):
    def test_monitor_only_logs_the_decision_and_does_nothing(self):
        sim = Sim(cfg=make_config(armed=False), source=Outage(T0, T0 + 30)).run(60)
        self.assertEqual(sim.actions, [])              # no hooks, no sync, no power command
        # "loss" again for the second it takes to confirm the restore
        self.assertEqual(sim.state_names(), ["normal", "loss", "would_shutdown", "loss", "normal"])
        t_would = sim.logged("WOULD COMMIT")[0][0]
        self.assertEqual(len(sim.logged("WOULD COMMIT")), 1)
        self.assertAlmostEqual(t_would, T0 + 1.8 + 8.0, delta=0.4)
        self.assertEqual(sim.logic.mode, "monitor")
        self.assertEqual(sim.logic.mode_reasons, ["config"])

    def test_monitor_only_publishes_the_radar_stop_request(self):
        sim = Sim(cfg=make_config(armed=False), source=Outage(T0, T0 + 30)).run(T0 + 1)
        self.assertTrue(sim.logic.snapshot()["radar_stop_requested"])
        self.assertIs(sim.logic.snapshot()["power_present"], False)
        sim.run(60)
        self.assertFalse(sim.logic.snapshot()["radar_stop_requested"])

    def test_monitor_only_ride_through(self):
        sim = Sim(cfg=make_config(armed=False), source=Outage(T0, T0 + 5)).run(60)
        self.assertEqual(sim.logged("WOULD"), [])
        self.assertEqual(sim.state_names(), ["normal", "loss", "normal"])

    def test_disable_file_behaves_as_monitor_only(self):
        sim = Sim(source=Outage(T0, T0 + 30))
        sim.disable_file = lambda t: True
        sim.run(60)
        self.assertEqual(sim.actions, [])
        self.assertEqual(len(sim.logged("WOULD COMMIT")), 1)
        self.assertEqual(sim.logic.mode_reasons, ["disable_file"])
        self.assertFalse(sim.logic.armed)

    def test_disable_file_created_during_the_ride_through(self):
        sim = Sim(source=Outage(T0))
        sim.disable_file = lambda t: t >= T0 + 4
        sim.run(60)
        self.assertEqual(sim.kinds(), [("hook", "stop_radar")])   # ran while still armed
        self.assertEqual(sim.power(), [])

    def test_disable_file_removed_during_the_ride_through_stops_the_radar_then(self):
        sim = Sim(source=Outage(T0, T0 + 6))
        sim.disable_file = lambda t: t < T0 + 4
        sim.run(60)
        self.assertEqual(sim.kinds(), [("hook", "stop_radar")])
        self.assertAlmostEqual(sim.find("hook", "stop_radar")[0][0], T0 + 4, delta=0.21)

    def test_disable_file_removed_during_a_loss_arms_again(self):
        sim = Sim(source=Outage(T0))
        sim.disable_file = lambda t: t < T0 + 20
        sim.run(60)
        (t_power, kind, _), = sim.power()
        self.assertEqual(kind, "poweroff")
        self.assertAlmostEqual(t_power, T0 + 20.2, delta=0.4)
        # The stop-radar hook had not run in monitor mode: it runs at the commit.
        self.assertEqual(sim.kinds()[0], ("hook", "stop_radar"))
        self.assertAlmostEqual(sim.find("hook", "stop_radar")[0][0], T0 + 20, delta=0.3)

    def test_disable_file_after_the_commit_does_not_abort(self):
        sim = Sim(source=Outage(T0))
        sim.delay["sync"] = 3.0
        sim.disable_file = lambda t: t >= T0 + 11.0
        sim.run(60)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))


class GuardTest(unittest.TestCase):
    def record(self, sim):
        (_, a), = sim.find("record")
        return a.data

    def test_early_shutdown_is_counted(self):
        sim = Sim(source=Outage(T0), uptime0=20.0).run(40)
        data = self.record(sim)
        self.assertEqual((data["count"], data["passive"]), (1, False))
        self.assertTrue(data["last_shutdown"]["early"])
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))

    def test_late_shutdown_resets_the_count(self):
        sim = Sim(source=Outage(T0), uptime0=1000.0, guard_count=2).run(40)
        data = self.record(sim)
        self.assertEqual((data["count"], data["passive"]), (0, False))
        self.assertFalse(data["last_shutdown"]["early"])

    def test_third_early_shutdown_sets_passive_for_the_next_boot(self):
        sim = Sim(source=Outage(T0), uptime0=20.0, guard_count=2).run(40)
        data = self.record(sim)
        self.assertEqual((data["count"], data["passive"]), (3, True))
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))     # the third one still happens

    def test_passive_after_three_never_shuts_down(self):
        sim = Sim(source=Outage(T0, T0 + 60), uptime0=20.0, guard_count=3).run(120)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.find("hook"), [])
        self.assertEqual(sim.logic.mode, "passive")
        self.assertEqual(sim.logic.mode_reasons, ["boot_loop_guard"])
        self.assertIn("boot_loop_guard", sim.logic.warnings)
        self.assertEqual(len(sim.logged("WOULD COMMIT")), 1)
        self.assertTrue(sim.logic.snapshot()["guard"]["passive"])
        # passive is persisted so that it survives a power cycle
        self.assertEqual(sim.find("save_guard")[0][1].data, {"count": 3, "passive": True})

    def test_count_resets_when_a_boot_passes_the_early_window(self):
        sim = Sim(uptime0=100.0, guard_count=2).run(199)
        self.assertEqual(sim.find("save_guard"), [])
        sim.run(2)
        (t, a), = sim.find("save_guard")
        self.assertEqual(a.data, {"count": 0, "passive": False})
        self.assertAlmostEqual(t, 200.0, delta=0.3)
        sim.run(600)
        self.assertEqual(len(sim.find("save_guard")), 1)

    def test_no_guard_write_when_there_is_nothing_to_reset(self):
        sim = Sim(uptime0=100.0, guard_count=0).run(600)
        self.assertEqual(sim.actions, [])

    def test_count_reset_keeps_passive(self):
        sim = Sim(uptime0=100.0, guard_count=3).run(400)
        self.assertEqual([a.data for _, a in sim.find("save_guard")],
                         [{"count": 3, "passive": True}, {"count": 0, "passive": True}])
        self.assertEqual(sim.logic.mode, "passive")

    def test_passive_rearms_after_30_min_of_healthy_power(self):
        sim = Sim(uptime0=20.0, passive=True, guard_count=3).run(1800)
        self.assertEqual(sim.logic.mode, "passive")
        sim.run(3)
        self.assertEqual(sim.logic.mode, "armed")
        self.assertEqual(sim.find("save_guard")[-1][1].data, {"count": 0, "passive": False})
        self.assertNotIn("boot_loop_guard", sim.logic.warnings)
        # and it acts again
        src = Outage(sim.t + 5)
        sim.current, sim.voltage, sim.gpio = src.current, src.voltage, src.gpio
        sim.run(30)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))

    def test_passive_does_not_rearm_while_power_is_unstable_or_a_signal_is_bad(self):
        sim = Sim(passive=True)
        sim.gpio = lambda t: 0 if 1000 <= t < 1003 else 1      # a short dip after 1000 s
        sim.run(1000 + 1790)
        self.assertEqual(sim.logic.mode, "passive")             # the 30 min started again
        sim.run(20)
        self.assertEqual(sim.logic.mode, "armed")

        sim = Sim(passive=True)
        sim.current = lambda t: None
        sim.run(4000)
        self.assertEqual(sim.logic.mode, "passive")
        self.assertEqual(sim.find("save_guard"), [])

    def test_unwritable_guard_state_suspends_shutdown(self):
        sim = Sim(source=Outage(T0, T0 + 30))
        sim.guard_ok = lambda t: False
        sim.run(60)
        self.assertEqual(sim.actions, [])
        self.assertEqual(sim.logic.mode_reasons, ["guard_state_unwritable"])
        self.assertIn("guard_state_unwritable", sim.logic.warnings)


class HookTest(unittest.TestCase):
    def test_failed_stop_hook_is_retried_at_commit_and_does_not_block(self):
        sim = Sim(source=Outage(T0))
        sim.ok["hook:stop_radar"] = False
        sim.run(40)
        self.assertEqual(sim.kinds(), [("hook", "stop_radar"), ("hook", "stop_radar"),
                                       ("hook", "amp_safe"), ("sync",), ("record",),
                                       ("power", "poweroff")])
        self.assertLessEqual(sim.power()[0][0] - T0, 10.6)

    def test_hanging_stop_hook_does_not_block(self):
        sim = Sim(source=Outage(T0))
        sim.delay["hook:stop_radar"] = None
        sim.run(40)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))
        self.assertLessEqual(sim.power()[0][0] - T0, 10.8)  # its time ran out during the ride-through

    def test_slow_stop_hook_is_waited_for(self):
        sim = Sim(source=Outage(T0, v0=4.4))                # low capacitor: commit after about 1.7 s
        sim.delay["hook:stop_radar"] = 3.0
        sim.run(40)
        t_hook = sim.find("hook", "stop_radar")[0][0]
        self.assertEqual(len(sim.find("hook", "stop_radar")), 1)
        self.assertAlmostEqual(sim.find("hook", "amp_safe")[0][0], t_hook + 3.0, delta=0.25)

    def test_hanging_amp_safe_hook_is_bounded_by_its_timeout(self):
        sim = Sim(source=Outage(T0))
        sim.delay["hook:amp_safe"] = None
        sim.run(60)
        t_commit = sim.find("hook", "amp_safe")[0][0]
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))
        self.assertAlmostEqual(sim.power()[0][0] - t_commit, 5.0 + 1.0, delta=0.5)

    def test_failing_amp_safe_hook_does_not_block(self):
        sim = Sim(source=Outage(T0))
        sim.ok["hook:amp_safe"] = False
        sim.run(40)
        self.assertLessEqual(sim.power()[0][0] - T0, 10.6)
        self.assertTrue(sim.logged("hook amp_safe failed"))

    def test_hanging_sync_and_guard_write_are_bounded(self):
        sim = Sim(source=Outage(T0))
        sim.delay["sync"] = None
        sim.delay["record"] = None
        sim.run(60)
        t_commit = sim.find("hook", "amp_safe")[0][0]
        self.assertAlmostEqual(sim.power()[0][0] - t_commit, 5.0 + 3.0, delta=0.6)

    def test_failed_guard_write_still_powers_off(self):
        sim = Sim(source=Outage(T0))
        sim.ok["record"] = False
        sim.run(40)
        self.assertEqual(sim.power()[0][1:], ("poweroff", 0))

    def test_everything_hanging_has_a_bounded_worst_case(self):
        sim = Sim(source=Outage(T0))
        for what in ("hook:stop_radar", "hook:amp_safe", "sync", "record"):
            sim.delay[what] = None
        sim.run(80)
        # GPIO lag 1.8 + ride-through 8 + amp_safe 6 + sync 5 + record 3 (the stop hook timed
        # out earlier), then one sample for the final check
        self.assertLessEqual(sim.power()[0][0] - T0, 1.8 + 8.2 + 6 + 5 + 3 + 0.8)

    def test_worst_case_with_a_stop_hook_that_fails_late(self):
        # The stop hook fails at its timeout during the ride-through, is retried at the
        # commit and hangs again; everything else hangs too.
        sim = Sim(source=Outage(T0))
        sim.delay["hook:stop_radar"] = 5.0
        sim.ok["hook:stop_radar"] = False
        for what in ("hook:amp_safe", "sync", "record"):
            sim.delay[what] = None
        sim.survive = True
        sim.run(80)
        times = [round(t - T0, 1) for t, _, _ in sim.power()]
        self.assertLessEqual(times[0], 1.8 + 8.2 + 5 + 6 + 5 + 3 + 0.8)    # about 30 s
        self.assertAlmostEqual(times[1] - times[0], 10.0, delta=0.3)        # last resort


class EscalationTest(unittest.TestCase):
    def test_last_resort_if_the_pi_is_still_running(self):
        sim = Sim(source=Outage(T0))
        sim.survive = True
        sim.run(80)
        t0 = sim.power()[0][0]
        got = [(round(t - t0), kind, level) for t, kind, level in sim.power()]
        self.assertEqual(got[:4], [(0, "poweroff", 0), (10, "poweroff", 1), (20, "poweroff", 1),
                                   (30, "poweroff", 1)])
        self.assertEqual({level for _, _, level in sim.power()}, {0, 1})

    def test_reboot_has_the_same_last_resort(self):
        sim = Sim(source=Outage(T0, T0 + 10.5))
        sim.delay["sync"] = 1.0
        sim.survive = True
        sim.run(60)
        self.assertEqual([p[1:] for p in sim.power()][:3], [("reboot", 0), ("reboot", 1), ("reboot", 1)])

    def test_failed_command_goes_to_the_last_resort_at_once(self):
        sim = Sim(source=Outage(T0))
        sim.ok["power"] = False
        sim.run(42)
        t0 = sim.power()[0][0]
        self.assertEqual([(round(t - t0, 1), level) for t, _, level in sim.power()],
                         [(0.0, 0), (0.0, 1), (10.0, 1), (20.0, 1)])    # a failed last resort is repeated


class ReviewScenarioTest(unittest.TestCase):
    """Scenarios from the review (scratchpad scen.py): each one used to end wrongly."""

    def test_a_long_disagreement_then_a_short_gpio_dip_does_not_commit(self):
        # Current stuck at "loss" with power present; 60 s later GPIO dips for 0.4 s.
        sim = Sim()
        sim.current = lambda t: -0.5 if t >= 10 else 0.02
        sim.gpio = lambda t: 0 if 70.0 <= t < 70.4 else 1
        sim.run(100)
        self.assertEqual(sim.logged("gpio: present -> loss")[0][0], 70.2)   # the dip was seen as a loss
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logged("COMMIT"), [])

    def test_a2_gpio_stuck_low_then_a_short_current_glitch_does_not_commit(self):
        sim = Sim()
        sim.gpio = lambda t: 0 if t >= 10 else 1
        sim.current = lambda t: -0.5 if 60.0 <= t < 60.4 else 0.02
        sim.run(100)
        self.assertEqual(sim.logged("current: present -> loss")[0][0], 60.2)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logged("COMMIT"), [])

    def test_a_dip_must_last_the_whole_ride_through(self):
        # The same disagreement, but GPIO then really agrees for 7 s (no) and for 9 s (yes).
        for length, expect in ((7.0, []), (9.0, [("poweroff", 0)])):
            sim = Sim()
            sim.current = lambda t: -0.5 if t >= 10 else 0.02
            sim.gpio = lambda t, length=length: 0 if 70.0 <= t < 70.0 + length else 1
            sim.run(100)
            self.assertEqual([p[1:] for p in sim.power()], expect, length)

    def test_b_current_unreadable_and_power_back_during_the_sequence_reboots(self):
        sim = Sim(source=Outage(10, t1=21.5))
        sim.current = lambda t: None
        sim.delay["hook:amp_safe"] = 4.0
        sim.run(60)
        self.assertLess(sim.logged("COMMIT to shutdown")[0][0], 21.5)
        self.assertEqual([p[1:] for p in sim.power()], [("reboot", 0)])

    def test_b_current_unreadable_and_power_still_absent_powers_off(self):
        sim = Sim(source=Outage(10))
        sim.current = lambda t: None
        sim.delay["hook:amp_safe"] = 4.0
        sim.run(60)
        self.assertEqual([p[1:] for p in sim.power()], [("poweroff", 0)])

    def test_c_noisy_gpio_with_current_unreadable_never_commits(self):
        for seed in range(10):
            rng = random.Random(seed)
            sim = Sim()
            sim.current = lambda t: None
            sim.gpio = lambda t, rng=rng: 1 if t < 10 else rng.choice((0, 1))
            sim.run(600)
            self.assertTrue(sim.logged("gpio: .* -> loss"), seed)     # it did look like a loss
            self.assertEqual(sim.power(), [], seed)
            self.assertEqual(sim.logged("COMMIT"), [], seed)

    def test_c2_periodic_gpio_dips_with_current_unreadable_never_commit(self):
        # 0.4 s low every 0.8 s: never 1 s of "present", so the state stays "loss".
        sim = Sim()
        sim.current = lambda t: None
        sim.gpio = lambda t: 1 if t < 10 else (0 if int(round(t / 0.2)) % 4 in (0, 1) else 1)
        sim.run(100)
        self.assertEqual(sim.logic.gpio.state, L.LOSS)
        self.assertGreater(sim.logic.snapshot()["loss_duration_s"], 80)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logged("COMMIT"), [])
        sim.run(500)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logic.gpio.state, L.FAULTY)

    def test_d_gpio_faulty_and_power_back_during_the_sequence_reboots(self):
        sim = Sim()
        sim.gpio = lambda t: 0
        sim.current = lambda t: -1.0 if 300 <= t < 311 else 0.02
        sim.delay["hook:amp_safe"] = 4.0
        sim.run(400)
        self.assertEqual(sim.logic.gpio.state, L.FAULTY)
        self.assertLess(sim.logged("COMMIT to shutdown")[0][0], 311)
        self.assertEqual([p[1:] for p in sim.power()], [("reboot", 0)])

    def test_no_healthy_signal_at_the_final_check_powers_off(self):
        # Both signals stop answering after the commit: nothing says power is back.
        src = Outage(T0)
        sim = Sim(source=src)
        sim.delay["hook:amp_safe"] = 4.0
        sim.current = lambda t: None if t >= T0 + 10.5 else src.current(t)
        sim.gpio = lambda t: None if t >= T0 + 10.5 else src.gpio(t)
        sim.run(60)
        self.assertEqual([p[1:] for p in sim.power()], [("poweroff", 0)])

    def test_one_signal_present_and_one_still_in_loss_powers_off(self):
        src = Outage(T0)
        sim = Sim(source=src)
        sim.delay["hook:amp_safe"] = 4.0
        sim.gpio = lambda t: 1 if t >= T0 + 10.5 else src.gpio(t)       # GPIO alone says power is back
        sim.run(60)
        self.assertEqual([p[1:] for p in sim.power()], [("poweroff", 0)])

    def test_noise_on_gpio_with_both_signals_readable_never_commits(self):
        for seed in range(10):
            rng = random.Random(seed)
            sim = Sim()
            sim.gpio = lambda t, rng=rng: 1 if t < 10 else rng.choice((0, 1))
            sim.run(600)
            self.assertEqual(sim.power(), [], seed)
        sim = Sim()
        sim.gpio = lambda t: 1 if t < 10 else (0 if int(round(t / 0.2)) % 4 in (0, 1) else 1)
        sim.run(600)
        self.assertEqual(sim.power(), [])
        self.assertEqual(sim.logged("COMMIT"), [])

    def test_noise_on_both_signals_never_commits(self):
        for seed in range(10):
            rng = random.Random(seed)
            sim = Sim()
            sim.gpio = lambda t, rng=rng: 1 if t < 10 else rng.choice((0, 1))
            sim.current = lambda t, rng=rng: 0.02 if t < 10 else rng.choice((-0.5, 0.02))
            sim.run(600)
            self.assertTrue(sim.logged("POWER LOSS"), seed)
            self.assertEqual(sim.power(), [], seed)
            self.assertEqual(sim.logged("COMMIT"), [], seed)

    def test_one_present_sample_restarts_the_ride_through(self):
        # A real outage with a single "present" GPIO sample 6 s in: commit 8 s after that sample.
        src = Outage(T0)
        sim = Sim(source=src)
        sim.gpio = lambda t: 1 if 16.0 <= t < 16.2 else src.gpio(t)
        sim.run(60)
        t_commit = sim.logged("COMMIT to shutdown")[0][0]
        self.assertGreaterEqual(t_commit, 16.2 + 8.0 - 1e-6)
        self.assertLessEqual(t_commit, 16.2 + 8.25)

    def test_agreement_time_is_published(self):
        sim = Sim(cfg=make_config(armed=False), source=Outage(T0, T0 + 6)).run(T0 + 1)
        self.assertIsNone(sim.logic.snapshot()["agreement_s"])          # GPIO has not agreed yet
        sim.run(4)
        self.assertAlmostEqual(sim.logic.snapshot()["agreement_s"], 3.2, delta=0.3)
        sim.run(10)
        self.assertIsNone(sim.logic.snapshot()["agreement_s"])


class SnapshotTest(unittest.TestCase):
    def test_snapshot_is_plain_json(self):
        import json
        sim = Sim(source=Outage(T0, T0 + 5))
        for _ in range(100):
            sim.run(0.2)
            snap = json.loads(json.dumps(sim.logic.snapshot(), allow_nan=False))
            self.assertIn(snap["state"], ("normal", "loss", "would_shutdown", "committing", "shutdown"))
            self.assertIn(snap["signals"]["current"]["state"],
                          ("unknown", "present", "loss", "unreadable", "faulty"))


if __name__ == "__main__":
    unittest.main()
