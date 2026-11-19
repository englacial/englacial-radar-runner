"""Replays of the real power-loss recordings in ~/ups-bringup/logs through the logic.

The recordings (UPS_BRINGUP.md steps 6 to 9) were sampled every 0.25 s:

    powerloss_20261028_235027   idle, input unplugged for about 8.3 s, then restored
    powerloss_20261029_001627   idle, unplugged for about 14 s, then 4.4 min of recharge
    wakeup_20261029_003538      idle, unplugged; the log ends 12.8 s into the outage
    radarload_20261029_005433   radar running at full load, no outage
    radarload_20261029_005638   radar running, unplugged for about 25 s, then restored

The 8 s ride-through is counted from when both signals agree. GPIO 26 follows
the current reversal by 1.3 to 1.5 s in these recordings, so the commit comes
9.6 to 9.8 s after the first discharge sample: the 8.3 s outage must ride
through and the others must commit.
"""
import unittest

from .helpers import LOG_DIR, Replay, load_recording, make_config

OUTAGE_8S = "powerloss_20261028_235027.ups.log"
OUTAGE_14S = "powerloss_20261029_001627.ups.log"
OUTAGE_WAKEUP = "wakeup_20261029_003538.ups.log"
RADAR_NO_OUTAGE = "radarload_20261029_005433.ups.log"
RADAR_OUTAGE_25S = "radarload_20261029_005638.ups.log"
ALL = (OUTAGE_8S, OUTAGE_14S, OUTAGE_WAKEUP, RADAR_NO_OUTAGE, RADAR_OUTAGE_25S)



def loss_samples(rows):
    return [t for t, i, v, g in rows if i < -0.3]


def gpio_low_samples(rows):
    return [t for t, i, v, g in rows if g == 0]


def extend(rows, seconds, dt=0.25):
    """Hold the last reading for a while longer (for a recording that stops early)."""
    t, i, v, g = rows[-1]
    return rows + [(round(t + dt * k, 3), i, v, g) for k in range(1, int(seconds / dt) + 1)]


@unittest.skipUnless(LOG_DIR.is_dir(), f"recordings not found in {LOG_DIR}")
class ReplayTest(unittest.TestCase):
    def replay(self, name, armed=True, rows=None, **cfg):
        rows = load_recording(name) if rows is None else rows
        return Replay(rows, cfg=make_config(armed=armed, **cfg)).run(), rows

    def test_recordings_parse(self):
        expect = {OUTAGE_8S: 127, OUTAGE_14S: 1193, OUTAGE_WAKEUP: 113, RADAR_NO_OUTAGE: 200,
                  RADAR_OUTAGE_25S: 383}
        for name in ALL:
            rows = load_recording(name)
            self.assertEqual(len(rows), expect[name], name)
            self.assertTrue(all(0.2 < b[0] - a[0] < 0.35 for a, b in zip(rows, rows[1:])), name)

    def test_nothing_happens_before_the_outage(self):
        for name in ALL:
            r, rows = self.replay(name)
            first = (loss_samples(rows) or [1e9])[0]
            self.assertEqual([a for t, a in r.actions if t < first], [], name)
            self.assertEqual([m for t, lvl, m in r.logs if t < first and lvl < 6], [], name)

    def test_every_outage_requests_a_radar_stop_promptly(self):
        for name in (OUTAGE_8S, OUTAGE_14S, OUTAGE_WAKEUP, RADAR_OUTAGE_25S):
            for armed in (True, False):
                r, rows = self.replay(name, armed=armed)
                first = loss_samples(rows)[0]
                t_loss = [t for t, s in r.states if s == "loss"][0]
                self.assertLessEqual(t_loss - first, 0.3, name)      # the second loss sample
                if armed:
                    self.assertEqual(r.find("hook", "stop_radar")[0][0], t_loss, name)

    def test_gpio_lag_is_never_reported_as_a_disagreement(self):
        for name in ALL:
            r, _ = self.replay(name, armed=False)
            self.assertEqual(r.logged("disagree|unreadable|faulty"), [], name)

    def test_8s_outage_rides_through(self):
        for armed in (True, False):
            r, rows = self.replay(OUTAGE_8S, armed=armed)
            self.assertEqual(r.power(), [])
            self.assertEqual(r.state_names(), ["normal", "loss"])
            self.assertEqual(r.logged("COMMIT"), [])
            # The recording stops 0.5 s after the restore (the Pi was then shut down by
            # the power-key event, since fixed). The restore is seen but not yet confirmed.
            self.assertTrue(r.logic.current.restoring and r.logic.gpio.restoring)
            # The two signals agreed for 6.5 s, short of the 8 s ride-through.
            agreed = loss_samples(rows)[-1] - gpio_low_samples(rows)[0]
            self.assertAlmostEqual(agreed, 6.5, delta=0.3)
            # With a little more of the same readings it is back to normal.
            r, rows = self.replay(OUTAGE_8S, armed=armed, rows=extend(rows, 2.0))
            self.assertEqual(r.state_names(), ["normal", "loss", "normal"])
            self.assertEqual(r.kinds(), [("hook", "stop_radar")] if armed else [])
            self.assertFalse(r.logic.stop_requested)

    def check_commit(self, name, after_first_loss):
        """The outage commits 8 s after GPIO agrees and powers off before the input returns."""
        r, rows = self.replay(name)
        first, last = loss_samples(rows)[0], loss_samples(rows)[-1]
        gpio_low = gpio_low_samples(rows)[0]
        self.assertEqual(r.kinds(), [("hook", "stop_radar"), ("hook", "amp_safe"), ("sync",),
                                     ("record",), ("power", "poweroff")])
        t_commit, message = r.logged("COMMIT to shutdown")[0]
        self.assertIn("loss agreed for 8", message)       # by ride-through, not by low voltage
        self.assertIn("both signals say loss", message)
        self.assertGreaterEqual(t_commit - gpio_low, 8.0)
        self.assertLessEqual(t_commit - gpio_low, 8.6)    # GPIO confirmed on its second low sample
        self.assertAlmostEqual(t_commit - first, after_first_loss, delta=0.05)
        (t_power, kind, level), = r.power()
        self.assertEqual((kind, level), ("poweroff", 0))
        self.assertLessEqual(t_power - t_commit, 0.55)    # two samples of the recording
        self.assertLess(t_power, last)
        return r, rows, t_power

    def test_14s_outage_commits_and_powers_off(self):
        r, rows, t_power = self.check_commit(OUTAGE_14S, 9.80)
        # The command comes 3.5 s before the last discharge sample. An idle OS shutdown
        # takes about 3.5 s, so here the input returned just as the shutdown finished.
        self.assertAlmostEqual(loss_samples(rows)[-1] - t_power, 3.5, delta=0.1)

    def test_14s_outage_monitor_only(self):
        r, rows = self.replay(OUTAGE_14S, armed=False)
        last = loss_samples(rows)[-1]
        self.assertEqual(r.actions, [])
        self.assertEqual(len(r.logged("WOULD COMMIT")), 1)
        self.assertEqual(r.state_names(), ["normal", "loss", "would_shutdown", "loss", "normal"])
        self.assertLessEqual(r.states[-1][0] - last, 1.6)
        # 4.4 minutes of recharging afterwards: nothing more.
        self.assertGreater(rows[-1][0] - r.states[-1][0], 250)
        self.assertEqual(r.logic.warnings, {})

    def test_wakeup_recording_commits_before_the_manual_poweroff(self):
        r, rows, t_power = self.check_commit(OUTAGE_WAKEUP, 9.80)
        self.assertLess(t_power - loss_samples(rows)[0], 12.6)    # when the user typed `systemctl poweroff`

    def test_radar_running_without_outage_does_nothing(self):
        for armed in (True, False):
            r, rows = self.replay(RADAR_NO_OUTAGE, armed=armed)
            self.assertEqual(r.actions, [])
            self.assertEqual(r.state_names(), ["normal"])
            self.assertEqual([m for _, lvl, m in r.logs if lvl < 6], [])

    def test_radar_running_25s_outage_commits_and_powers_off(self):
        r, rows, t_power = self.check_commit(RADAR_OUTAGE_25S, 9.63)
        self.assertGreater(loss_samples(rows)[-1] - t_power, 14.0)
        # The capacitor was still well above the low-voltage floor at that point.
        v_at_power = [v for t, i, v, g in rows if t <= t_power][-1]
        self.assertGreater(v_at_power, 4.75)

    def test_radar_running_25s_outage_monitor_only_returns_to_normal(self):
        r, rows = self.replay(RADAR_OUTAGE_25S, armed=False)
        self.assertEqual(r.actions, [])
        self.assertEqual(r.state_names(), ["normal", "loss", "would_shutdown", "loss", "normal"])
        self.assertEqual(r.logic.warnings, {})

    def test_outages_seen_through_one_signal_only(self):
        """Each recording again with the other signal unreadable throughout."""
        for drop in ("current", "gpio"):
            def strip(rows):
                if drop == "current":
                    return [(t, None, None, g) for t, i, v, g in rows]
                return [(t, i, v, None) for t, i, v, g in rows]

            r, _ = self.replay(OUTAGE_8S, rows=strip(extend(load_recording(OUTAGE_8S), 2.0)))
            self.assertEqual(r.power(), [], drop)
            self.assertEqual(r.state_names(), ["normal", "loss", "normal"], drop)

            for name in (OUTAGE_14S, RADAR_OUTAGE_25S):
                rows = load_recording(name)
                last = loss_samples(rows)[-1]
                r, _ = self.replay(name, rows=strip(rows))
                self.assertEqual([p[1:] for p in r.power()], [("poweroff", 0)], (name, drop))
                self.assertLess(r.power()[0][0], last, (name, drop))
                self.assertIn("loss agreed for 8", r.logged("COMMIT")[0][1], (name, drop))

    def test_quiesce_with_one_signal(self):
        """single_signal_action = "quiesce": no power-off on one signal alone."""
        def only_gpio(rows):
            return [(t, None, None, g) for t, i, v, g in rows]

        def only_current(rows):
            return [(t, i, v, None) for t, i, v, g in rows]

        for name in (OUTAGE_14S, RADAR_OUTAGE_25S):
            r, _ = self.replay(name, rows=only_gpio(load_recording(name)), single_signal_action="quiesce")
            self.assertEqual(r.power(), [], name)
            self.assertIn("loss", r.state_names(), name)
        r, _ = self.replay(OUTAGE_14S, rows=only_current(load_recording(OUTAGE_14S)),
                           single_signal_action="quiesce")
        self.assertEqual(r.power(), [])
        # In the 25 s outage the capacitor fell below 4.5 V while still discharging.
        # That is not a single-signal decision: it commits, 2 s after crossing the floor.
        rows = load_recording(RADAR_OUTAGE_25S)
        r, _ = self.replay(RADAR_OUTAGE_25S, rows=only_current(rows), single_signal_action="quiesce")
        t_commit, message = r.logged("COMMIT to shutdown")[0]
        self.assertIn("capacitor discharging below floor", message)
        t_floor = [t for t, i, v, g in rows if v < 4.5][0]
        self.assertAlmostEqual(t_commit - t_floor, 2.0, delta=0.3)
        self.assertEqual([p[1:] for p in r.power()], [("poweroff", 0)])

    def test_25s_outage_with_gpio_stuck_high(self):
        rows = [(t, i, v, 1) for t, i, v, g in load_recording(RADAR_OUTAGE_25S)]
        r, _ = self.replay(RADAR_OUTAGE_25S, rows=rows)
        t_commit, message = r.logged("COMMIT to shutdown")[0]
        self.assertIn("capacitor discharging below floor", message)
        self.assertIn("gpio says present", message)
        self.assertEqual([p[1:] for p in r.power()], [("poweroff", 0)])
        self.assertAlmostEqual(t_commit - loss_samples(rows)[0], 23.3, delta=0.5)
        # The 14 s outage never reached the floor: with GPIO stuck high it is not acted on.
        rows = [(t, i, v, 1) for t, i, v, g in load_recording(OUTAGE_14S)]
        r, _ = self.replay(OUTAGE_14S, rows=rows)
        self.assertEqual(r.power(), [])
        self.assertTrue(r.logged("warning: signals disagree"))

if __name__ == "__main__":
    unittest.main()
