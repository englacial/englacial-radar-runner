"""Tests of the service process (power/service.py) with fake hardware and a fake power actuator.

Nothing here can power the Pi off: the actuator is always a fake, and
SystemdActuator is only ever given a fake `run`.
"""
import json
import os
import random
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from power import logic as L
from power import service as S
from power.guard import FILE_NAME, Guard

from .helpers import FakeActuator, FakeClock, ListLog, Outage, make_config

T0 = 10.0


class FakePopen:
    """A hook process that exits at once with a given status."""
    calls = None

    def __init__(self, argv, rc=0, **kw):
        self.argv = tuple(argv)
        self.kw = kw
        self.rc = rc
        self.pid = 2 ** 31 - 1   # not a real process group, should anything try to kill it

    def poll(self):
        return self.rc


class Harness:
    def __init__(self, test, source=None, uptime0=1000.0, state_dir=None, hook_rc=0, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.cfg = make_config(status_file=os.path.join(d, "run", "power.json"),
                               state_dir=state_dir or os.path.join(d, "state"),
                               disable_file=os.path.join(d, "power-disarm"), **cfg)
        self.source = source or Outage(1e9)
        self.clock = FakeClock(uptime0)
        self.actuator = FakeActuator()
        self.log = ListLog()
        self.sync_calls = 0
        self.sync_takes = 0.0      # simulated duration of os.sync()
        self.popen_calls = []
        self.hook_rc = hook_rc
        self.stop_when_fired = True
        self.start()

    def start(self):
        """Start (or restart, as after a reboot) the service on the same directories."""
        self.guard = Guard(self.cfg.state_dir)
        self.service = S.Service(self.cfg, self.read_ina, self.read_gpio, self.actuator, self.guard,
                                 clock=self.clock, log=self.log, sync=self.sync, popen=self.popen)
        return self

    def read_ina(self):
        i = self.source.current(self.clock.t)
        if i is None:
            raise OSError(121, "Remote I/O error")
        return i, self.source.voltage(self.clock.t)

    def read_gpio(self):
        g = self.source.gpio(self.clock.t)
        if g is None:
            raise OSError(5, "Input/output error")
        return g

    def sync(self):
        self.sync_calls += 1
        self.clock.t += self.sync_takes

    def popen(self, argv, **kw):
        self.popen_calls.append((round(self.clock.t, 1), tuple(argv)))
        return FakePopen(argv, rc=self.hook_rc, **kw)

    def run(self, seconds):
        end = self.clock.t + seconds
        real_sleep = self.clock.sleep

        def sleep(s):
            real_sleep(s)
            if self.clock.t >= end or (self.stop_when_fired and self.actuator.fired):
                self.service.stop = True
        self.service.stop = False
        with mock.patch.object(self.clock, "sleep", sleep):
            self.rc = self.service.run()
        return self

    def status(self):
        with open(self.cfg.status_file) as f:
            return json.load(f)

    def guard_file(self):
        with open(os.path.join(self.cfg.state_dir, FILE_NAME)) as f:
            return json.load(f)


class ServiceTest(unittest.TestCase):
    def test_power_present(self):
        h = Harness(self, armed=True).run(120)
        self.assertEqual(h.rc, 0)
        self.assertEqual(h.actuator.fired, [])
        self.assertEqual((h.sync_calls, h.popen_calls), (0, []))
        st = h.status()
        self.assertEqual((st["state"], st["mode"], st["armed"]), ("normal", "armed", True))
        self.assertEqual(st["warnings"], {})
        self.assertFalse(st["radar_stop_requested"])
        self.assertEqual(st["signals"]["current"]["state"], "present")
        self.assertEqual(st["signals"]["gpio"]["state"], "present")
        self.assertEqual((st["capacitor_v"], st["current_a"]), (5.19, 0.02))
        self.assertEqual(st["guard"]["early_shutdowns"], 0)
        self.assertAlmostEqual(st["time"], h.clock.wall(), delta=1.0)
        self.assertAlmostEqual(st["uptime_s"], h.clock.uptime(), delta=1.0)
        self.assertGreater(h.service.samples, 550)       # 5 Hz
        self.assertNotIn("error", h.log.text().lower())

    def test_long_outage_runs_the_whole_sequence(self):
        h = Harness(self, Outage(T0), armed=True).run(60)
        self.assertEqual(h.actuator.fired, [("poweroff", 0)])
        self.assertEqual(h.sync_calls, 1)
        self.assertEqual(h.popen_calls, [])               # no hooks configured in stage 1
        self.assertGreaterEqual(h.clock.t - T0, 9.5)      # 8 s after GPIO agreed
        self.assertLessEqual(h.clock.t - T0, 10.6)
        st = h.status()                                   # published before the command
        self.assertEqual((st["state"], st["power_action"]), ("shutdown", "poweroff"))
        self.assertTrue(st["radar_stop_requested"])
        g = h.guard_file()
        self.assertEqual((g["count"], g["passive"]), (0, False))   # not an early shutdown
        self.assertFalse(g["last_shutdown"]["early"])
        self.assertAlmostEqual(g["last_shutdown"]["wall_time"], h.clock.wall(), delta=2.0)
        text = h.log.text()
        for expected in ("POWER LOSS indicated by current", "COMMIT to shutdown", "POWER OFF",
                         "hook stop_radar: not configured", "hook amp_safe: not configured"):
            self.assertIn(expected, text)

    def test_hooks_run_in_order_with_the_configured_commands(self):
        h = Harness(self, Outage(T0), armed=True, stop_radar=("/opt/x/radarctl", "stop"),
                    amp_safe=("/opt/x/amp_safe",)).run(60)
        self.assertEqual([c[1] for c in h.popen_calls], [("/opt/x/radarctl", "stop"), ("/opt/x/amp_safe",)])
        self.assertAlmostEqual(h.popen_calls[0][0], T0 + 0.2, delta=0.21)     # at the first sign of loss
        self.assertGreaterEqual(h.popen_calls[1][0] - h.popen_calls[0][0], 8.0)  # at the commit
        self.assertEqual(h.actuator.fired, [("poweroff", 0)])

    def test_failing_hooks_do_not_block_the_shutdown(self):
        h = Harness(self, Outage(T0), armed=True, hook_rc=3, stop_radar=("/opt/x/stop",),
                    amp_safe=("/opt/x/safe",)).run(60)
        self.assertEqual(h.actuator.fired, [("poweroff", 0)])
        self.assertLessEqual(h.clock.t - T0, 10.6)
        self.assertIn("hook stop_radar exited with status 3", h.log.text())
        # stop_radar is tried again at the commit
        self.assertEqual([c[1][0] for c in h.popen_calls], ["/opt/x/stop", "/opt/x/stop", "/opt/x/safe"])

    def test_short_outage_rides_through(self):
        h = Harness(self, Outage(T0, T0 + 5), armed=True, stop_radar=("/opt/x/stop",)).run(T0 + 2)
        st = h.status()
        self.assertEqual((st["state"], st["radar_stop_requested"], st["power_present"]),
                         ("loss", True, False))
        h.run(30)
        self.assertEqual(h.actuator.fired, [])
        self.assertEqual(len(h.popen_calls), 1)
        st = h.status()
        self.assertEqual((st["state"], st["radar_stop_requested"], st["power_present"]),
                         ("normal", False, True))
        self.assertEqual(h.sync_calls, 0)

    def test_power_back_during_the_sequence_reboots(self):
        h = Harness(self, Outage(T0, T0 + 11.5), armed=True)
        h.sync_takes = 3.0          # power returns while the sync is running
        h.run(60)
        self.assertEqual(h.sync_calls, 1)
        self.assertEqual(h.actuator.fired, [("reboot", 0)])
        self.assertEqual(h.status()["power_action"], "reboot")

    def test_power_back_with_the_ina219_unreadable_reboots(self):
        # The state of the bench: nothing answers at 0x40, GPIO 26 alone is used.
        src = Outage(T0, T0 + 11.5)
        src.current = lambda t: None
        h = Harness(self, src, armed=True)
        h.sync_takes = 3.0
        h.run(60)
        self.assertIn("gpio says loss, current is unreadable", h.log.text())
        self.assertEqual(h.actuator.fired, [("reboot", 0)])

    def test_last_resort_when_the_pi_keeps_running(self):
        h = Harness(self, Outage(T0), armed=True)
        h.stop_when_fired = False
        h.run(60)
        self.assertEqual(h.actuator.fired[:3], [("poweroff", 0), ("poweroff", 1), ("poweroff", 1)])
        self.assertEqual({level for _, level in h.actuator.fired}, {0, 1})

    def test_failed_power_command_goes_to_the_last_resort_at_once(self):
        h = Harness(self, Outage(T0), armed=True)
        h.actuator.ok = False
        h.stop_when_fired = False
        h.run(T0 + 12)
        self.assertEqual(h.actuator.fired, [("poweroff", 0), ("poweroff", 1)])

    def test_hung_power_command_does_not_stall_the_loop_or_the_watchdog(self):
        name = "radar-power-test-hung-%d" % os.getpid()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(server.close)
        server.bind("\0" + name)
        server.setblocking(False)
        h = Harness(self, Outage(T0), armed=True)
        h.service.notifier = S.Notifier({"NOTIFY_SOCKET": "@" + name, "WATCHDOG_USEC": "30000000"})
        self.addCleanup(h.service.notifier.sock.close)
        h.actuator.ok = None                  # every power command hangs
        h.stop_when_fired = False
        h.run(T0 + 11)                        # just past the first command
        self.assertEqual(h.actuator.fired, [("poweroff", 0)])
        while True:                           # drain the pings so far
            try:
                server.recv(4096)
            except BlockingIOError:
                break
        samples, t_fired = h.service.samples, h.clock.t
        h.run(35)
        # The loop kept sampling at 5 Hz and pinging once a second with the commands hung...
        self.assertAlmostEqual(h.service.samples - samples, 35 * 5, delta=4)
        pings = 0
        while True:
            try:
                pings += server.recv(4096) == b"WATCHDOG=1"
            except BlockingIOError:
                break
        self.assertGreaterEqual(pings, 33)
        # ...the status stayed fresh, and the last resort followed after 10 s and was repeated.
        self.assertAlmostEqual(h.status()["time"], h.clock.wall(), delta=0.6)
        self.assertEqual(h.actuator.fired, [("poweroff", 0), ("poweroff", 1), ("poweroff", 1),
                                            ("poweroff", 1)])

    def test_monitor_only_takes_no_action(self):
        h = Harness(self, Outage(T0, T0 + 40), armed=False, stop_radar=("/opt/x/stop",),
                    amp_safe=("/opt/x/safe",)).run(T0 + 20)
        st = h.status()
        self.assertEqual((st["state"], st["mode"], st["armed"], st["mode_reasons"]),
                         ("would_shutdown", "monitor", False, ["config"]))
        self.assertTrue(st["radar_stop_requested"])
        h.run(60)
        self.assertEqual(h.actuator.fired, [])
        self.assertEqual((h.sync_calls, h.popen_calls), (0, []))
        self.assertEqual(h.status()["state"], "normal")
        self.assertEqual(h.log.text().count("WOULD COMMIT to shutdown"), 1)
        self.assertIsNone(h.guard_file()["last_shutdown"])

    def test_disable_file(self):
        h = Harness(self, Outage(T0, T0 + 40), armed=True)
        open(h.cfg.disable_file, "w").close()
        h.run(T0 + 20)
        st = h.status()
        self.assertEqual((st["state"], st["mode"], st["mode_reasons"]),
                         ("would_shutdown", "monitor", ["disable_file"]))
        h.run(60)
        self.assertEqual(h.actuator.fired, [])
        self.assertEqual(h.sync_calls, 0)
        os.remove(h.cfg.disable_file)
        h.run(2)
        self.assertEqual(h.status()["mode"], "armed")

    def test_disable_file_as_a_dangling_symlink_still_counts(self):
        h = Harness(self, Outage(T0), armed=True)
        os.symlink("/nonexistent/target", h.cfg.disable_file)
        h.run(40)
        self.assertEqual(h.actuator.fired, [])

    def test_reader_exceptions_never_stop_the_loop(self):
        h = Harness(self, armed=True)
        rng = random.Random(7)

        def bad_ina():
            raise RuntimeError("boom")

        def bad_gpio():
            if rng.random() < 0.3:
                raise rng.choice([OSError(5, "I/O"), ValueError("x"), ZeroDivisionError(), KeyError("k")])
            return 1
        h.service.read_ina, h.service.read_gpio = bad_ina, bad_gpio
        h.run(300)
        self.assertEqual(h.rc, 0)
        self.assertGreater(h.service.samples, 1400)
        self.assertEqual(h.actuator.fired, [])
        st = h.status()
        self.assertEqual(st["signals"]["current"]["state"], "unreadable")
        self.assertIn("boom", st["signals"]["current"]["last_error"])
        self.assertEqual(st["signals"]["gpio"]["state"], "present")
        self.assertGreater(st["signals"]["gpio"]["read_errors"], 300)
        self.assertEqual(list(st["warnings"]), ["current_unreadable"])
        self.assertEqual((st["capacitor_v"], st["current_a"]), (None, None))
        # not logged for every sample: the first error of each signal, then every 10 min
        self.assertEqual(h.log.text().count("signal read error"), 2)
        h.run(700)
        self.assertEqual(h.log.text().count("signal read error"), 4)

    def test_internal_errors_are_survived_and_bounded(self):
        h = Harness(self, armed=True)
        real_step = h.service.logic.step
        calls = {"n": 0}

        def step(*a, **kw):
            calls["n"] += 1
            if calls["n"] in (5, 6, 7):
                raise RuntimeError("bug")
            return real_step(*a, **kw)
        h.service.logic.step = step
        h.run(20)
        self.assertEqual(h.rc, 0)
        self.assertEqual(h.log.text().count("internal error in the sampling loop"), 3)
        self.assertEqual(h.status()["state"], "normal")
        # A permanent failure makes the process exit so that systemd restarts it.
        h.service.logic.step = mock.Mock(side_effect=RuntimeError("bug"))
        h.run(60)
        self.assertEqual(h.rc, 1)
        self.assertEqual(h.actuator.fired, [])

    def test_boot_loop_guard_over_several_boots(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = os.path.join(tmp.name, "state")

        def boot(source, seconds, uptime0=20.0):
            return Harness(self, source, armed=True, uptime0=uptime0, state_dir=state_dir).run(seconds)

        # Three boots in a row that lose power within the early-boot window: three shutdowns.
        for n in (1, 2, 3):
            h = boot(Outage(T0), 60)
            self.assertEqual(h.actuator.fired, [("poweroff", 0)], n)
            self.assertEqual(h.guard_file()["count"], n)
        self.assertTrue(h.guard_file()["passive"])
        # Fourth boot: passive. Same outage, no shutdown, flagged in the status.
        h = boot(Outage(T0, T0 + 30), 60)
        self.assertEqual(h.actuator.fired, [])
        st = h.status()
        self.assertEqual((st["mode"], st["mode_reasons"]), ("passive", ["boot_loop_guard"]))
        self.assertIn("boot_loop_guard", st["warnings"])
        self.assertTrue(st["guard"]["passive"])
        self.assertEqual(st["guard"]["early_shutdowns"], 3)
        self.assertIn("WOULD COMMIT", h.log.text())
        # Fifth boot, cut short: still passive, even though the count has been reset.
        h = boot(Outage(1e9), 400)
        self.assertEqual((h.guard_file()["count"], h.guard_file()["passive"]), (0, True))
        h = boot(Outage(T0, T0 + 30), 60)
        self.assertEqual(h.actuator.fired, [])
        self.assertEqual(h.status()["mode"], "passive")
        # Sixth boot: 30 minutes of healthy power re-arm it.
        h = boot(Outage(1e9), 1790)
        self.assertEqual(h.status()["mode"], "passive")
        h.run(20)
        self.assertEqual(h.status()["mode"], "armed")
        self.assertEqual((h.guard_file()["count"], h.guard_file()["passive"]), (0, False))
        # Seventh boot acts again.
        h = boot(Outage(T0), 60)
        self.assertEqual(h.actuator.fired, [("poweroff", 0)])
        self.assertEqual(h.guard_file()["count"], 1)

    def test_early_count_resets_after_a_good_boot(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = os.path.join(tmp.name, "state")
        for n in (1, 2):
            h = Harness(self, Outage(T0), armed=True, uptime0=20.0, state_dir=state_dir).run(60)
            self.assertEqual(h.guard_file()["count"], n)
        # A boot that lasts: count back to 0 after early_boot_s; a shutdown later is not counted.
        h = Harness(self, Outage(600), armed=True, uptime0=20.0, state_dir=state_dir).run(400)
        self.assertEqual(h.guard_file()["count"], 0)
        h.run(400)
        self.assertEqual(h.actuator.fired, [("poweroff", 0)])
        self.assertEqual(h.guard_file()["count"], 0)

    def test_corrupt_guard_file_does_not_block_start(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = os.path.join(tmp.name, "state")
        os.makedirs(state_dir)
        for content in (b"", b"\x00\x01\x02", b'{"count": 7', b'{"count": "x", "passive": 0}'):
            with open(os.path.join(state_dir, FILE_NAME), "wb") as f:
                f.write(content)
            h = Harness(self, Outage(T0), armed=True, state_dir=state_dir).run(60)
            self.assertIn("corrupt", h.log.text())
            self.assertEqual(h.actuator.fired, [("poweroff", 0)])
            self.assertEqual(h.guard_file()["count"], 0)

    def test_unwritable_state_dir_suspends_shutdown_until_it_is_writable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        blocker = os.path.join(tmp.name, "blocker")
        open(blocker, "w").close()
        h = Harness(self, Outage(T0, T0 + 30), armed=True, state_dir=os.path.join(blocker, "state"))
        h.run(60)
        self.assertEqual(h.actuator.fired, [])
        st = h.status()
        self.assertEqual((st["mode"], st["mode_reasons"]), ("monitor", ["guard_state_unwritable"]))
        self.assertIn("guard_state_unwritable", st["warnings"])
        self.assertNotEqual(st["guard"]["state_error"], "")
        self.assertEqual(h.log.text().count("cannot write guard state"), 1)
        os.remove(blocker)          # now the directory can be created
        h.run(35)
        self.assertEqual(h.status()["mode"], "armed")
        self.assertIn("guard state is writable again", h.log.text())

    def test_status_is_published_at_about_2_hz_and_on_changes(self):
        h = Harness(self, Outage(T0, T0 + 3), armed=True)
        times = []
        real_write = h.service.status.write
        h.service.status.write = lambda data: (times.append((h.clock.t, data["state"])), real_write(data))[1]
        h.run(60)
        self.assertGreaterEqual(len(times), 100)
        self.assertLessEqual(len(times), 160)
        gaps = [b[0] - a[0] for a, b in zip(times, times[1:])]
        self.assertLessEqual(max(gaps), 0.61)
        # The change to "loss" is published in the same sample that detects it.
        t_loss = [t for t, s in times if s == "loss"][0]
        self.assertAlmostEqual(t_loss, T0 + 0.2, delta=0.21)

    def test_unwritable_status_file_is_logged_once_and_does_not_stop_the_service(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        blocker = os.path.join(tmp.name, "blocker")
        open(blocker, "w").close()
        h = Harness(self, Outage(T0), armed=True)
        h.service.status = S.StatusWriter(os.path.join(blocker, "run", "power.json"))
        h.run(60)
        self.assertEqual(h.actuator.fired, [("poweroff", 0)])
        self.assertEqual(h.log.text().count("cannot write status file"), 1)

    def test_sd_notify_ready_and_watchdog(self):
        name = "radar-power-test-notify-%d" % os.getpid()     # abstract socket: no path limit
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(server.close)
        server.bind("\0" + name)
        server.setblocking(False)
        h = Harness(self, armed=True)
        h.service.notifier = S.Notifier({"NOTIFY_SOCKET": "@" + name, "WATCHDOG_USEC": "30000000"})
        self.addCleanup(h.service.notifier.sock.close)
        h.run(10)
        got = []
        while True:
            try:
                got.append(server.recv(4096).decode())
            except BlockingIOError:
                break
        self.assertEqual(got[0], "READY=1")
        pings = got.count("WATCHDOG=1")
        self.assertGreaterEqual(pings, 9)       # about one a second, far inside the 30 s limit
        self.assertLessEqual(pings, 12)
        self.assertEqual(set(got), {"READY=1", "WATCHDOG=1"})


class RealHookTest(unittest.TestCase):
    """Hooks as real child processes, in real time (a few seconds)."""

    def run_service(self, **cfg):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = make_config(armed=True, status_file=os.path.join(tmp.name, "power.json"),
                          state_dir=os.path.join(tmp.name, "state"),
                          disable_file=os.path.join(tmp.name, "disarm"),
                          sample_interval_s=0.05, restore_confirm_s=0.2, ride_through_s=0.4, **cfg)
        start = time.monotonic()
        actuator = FakeActuator()
        log = ListLog()
        service = S.Service(cfg, lambda: (-1.0, 5.0) if time.monotonic() - start > 0.3 else (0.02, 5.0),
                            lambda: 0 if time.monotonic() - start > 0.3 else 1,
                            actuator, Guard(cfg.state_dir), log=log, sync=lambda: None)
        real_start = actuator.start

        def start_power(kind, level, now):
            service.stop = True
            return real_start(kind, level, now)
        actuator.start = start_power
        timer = threading.Timer(20.0, lambda: setattr(service, "stop", True))
        timer.start()
        self.addCleanup(timer.cancel)
        service.run()
        return actuator, log, time.monotonic() - start, tmp.name

    def test_hanging_hooks_are_killed_at_their_timeout(self):
        # A shell with a child process: the whole process group must be killed.
        marker = "sleep 61.%d" % os.getpid()
        hang = ("/bin/sh", "-c", f"{marker}; true")
        actuator, log, elapsed, _ = self.run_service(stop_radar=hang, stop_radar_timeout_s=0.3,
                                                     amp_safe=hang, amp_safe_timeout_s=0.3)
        self.assertEqual(actuator.fired, [("poweroff", 0)])
        # loss 0.3 + ride-through 0.4 + stop_radar retry 0.3 + amp_safe 0.3, plus slack
        self.assertLess(elapsed, 4.0)
        self.assertEqual(log.text().count("hook stop_radar timed out"), 2)
        self.assertEqual(log.text().count("hook amp_safe timed out"), 1)
        time.sleep(0.2)
        ps = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        self.assertEqual(ps.stdout.strip(), "", "hook processes were left running")

    def test_failing_and_missing_hooks(self):
        actuator, log, elapsed, _ = self.run_service(stop_radar=("/bin/false",),
                                                     amp_safe=("/nonexistent/amp_safe", "x"))
        self.assertEqual(actuator.fired, [("poweroff", 0)])
        self.assertLess(elapsed, 3.0)
        self.assertIn("hook stop_radar exited with status 1", log.text())
        self.assertIn("hook amp_safe: cannot start '/nonexistent/amp_safe'", log.text())

    def test_working_hooks_run(self):
        out = tempfile.NamedTemporaryFile(mode="r", suffix=".txt")
        self.addCleanup(out.close)
        script = 'echo "$0" >> "$1"'
        actuator, log, elapsed, _ = self.run_service(
            stop_radar=("/bin/sh", "-c", script, "stop", out.name),
            amp_safe=("/bin/sh", "-c", script, "safe", out.name))
        self.assertEqual(actuator.fired, [("poweroff", 0)])
        self.assertEqual(out.read(), "stop\nsafe\n")     # each once, in this order
        self.assertNotIn("failed", log.text())

    def test_hanging_sync_is_bounded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        release = threading.Event()
        self.addCleanup(release.set)
        cfg = make_config(armed=True, status_file=os.path.join(tmp.name, "power.json"),
                          state_dir=os.path.join(tmp.name, "state"),
                          disable_file=os.path.join(tmp.name, "disarm"),
                          sample_interval_s=0.05, ride_through_s=0.2, sync_timeout_s=0.5)
        actuator = FakeActuator()
        log = ListLog()
        service = S.Service(cfg, lambda: (-1.0, 5.0), lambda: 0, actuator, Guard(cfg.state_dir),
                            log=log, sync=release.wait)
        real_start = actuator.start
        actuator.start = lambda *a: (setattr(service, "stop", True), real_start(*a))[1]
        timer = threading.Timer(20.0, lambda: setattr(service, "stop", True))
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        service.run()
        self.assertEqual(actuator.fired, [("poweroff", 0)])
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertIn("shutdown step sync did not finish in time", log.text())


class StatusWriterTest(unittest.TestCase):
    def test_file_is_valid_json_at_all_times(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "run", "power.json")
        writer = S.StatusWriter(path)
        writer.write({"n": -1})
        stop = threading.Event()
        result = {"reads": 0, "bad": []}

        def reader():
            while not stop.is_set():
                try:
                    with open(path, "rb") as f:
                        raw = f.read()
                    data = json.loads(raw)
                    assert data["pad"] == "x" * data["len"] if "pad" in data else True
                    result["reads"] += 1
                except Exception as e:      # missing, empty or partial file
                    result["bad"].append(repr(e))
        threads = [threading.Thread(target=reader) for _ in range(3)]
        for th in threads:
            th.start()
        rng = random.Random(3)
        for n in range(3000):
            size = rng.randrange(0, 5000)
            writer.write({"n": n, "len": size, "pad": "x" * size})
        stop.set()
        for th in threads:
            th.join()
        self.assertEqual(result["bad"], [])
        self.assertGreater(result["reads"], 100)
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["power.json"])

    def test_nan_is_refused_rather_than_written_as_invalid_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "power.json")
        writer = S.StatusWriter(path)
        writer.write({"ok": 1})
        writer.write({"v": float("nan")})
        self.assertNotEqual(writer.error, "")
        with open(path) as f:
            self.assertEqual(json.load(f), {"ok": 1})

    def test_file_is_world_readable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "power.json")
        S.StatusWriter(path).write({"ok": 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644 & ~_umask())


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


class NotifierTest(unittest.TestCase):
    def test_no_socket_is_harmless(self):
        n = S.Notifier({})
        self.assertFalse(n.ready())
        n.watchdog(1.0)

    def test_missing_socket_file_is_harmless(self):
        n = S.Notifier({"NOTIFY_SOCKET": "/nonexistent/notify"})
        self.addCleanup(n.sock.close)
        self.assertFalse(n.ready())

    def test_path_socket(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A relative path, because a socket path is limited to 107 characters.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(server.close)
        server.bind("notify")
        n = S.Notifier({"NOTIFY_SOCKET": "notify"})
        self.addCleanup(n.sock.close)
        self.assertTrue(n.ready())
        self.assertEqual(server.recv(100), b"READY=1")

    def test_ping_interval_follows_the_watchdog_period(self):
        self.assertEqual(S.Notifier({}).ping_interval, 1.0)
        self.assertEqual(S.Notifier({"WATCHDOG_USEC": "30000000"}).ping_interval, 1.0)
        self.assertAlmostEqual(S.Notifier({"WATCHDOG_USEC": "1500000"}).ping_interval, 0.5)
        self.assertEqual(S.Notifier({"WATCHDOG_USEC": "junk"}).ping_interval, 1.0)


class ActuatorTest(unittest.TestCase):
    """The real actuator, with a fake in place of subprocess.Popen. No power command is executed."""

    def make(self, euid=0, rc=0, exc=None):
        calls = []

        def popen(argv, **kw):
            calls.append((tuple(argv), kw))
            if exc:
                raise exc
            return FakePopen(argv, rc=rc)
        log = ListLog()
        return S.SystemdActuator(log, popen=popen, geteuid=lambda: euid), calls, log

    def test_commands(self):
        expect = {
            ("poweroff", 0): "/usr/bin/systemctl --no-block --job-mode=replace-irreversibly start poweroff.target",
            ("poweroff", 1): "/usr/sbin/poweroff -f -n",
            ("reboot", 0): "/usr/bin/systemctl --no-block --job-mode=replace-irreversibly start reboot.target",
            ("reboot", 1): "/usr/sbin/reboot -f -n",
        }
        self.assertEqual({k: " ".join(v) for k, v in S.POWER_COMMANDS.items()}, expect)
        for (kind, level), text in expect.items():
            act, calls, _ = self.make()
            job = act.start(kind, level, 100.0)
            (argv, kw), = calls
            self.assertEqual(" ".join(argv), text)
            self.assertTrue(kw["start_new_session"])
            self.assertEqual(job.what, "power")
            self.assertIs(job.poll(100.0), True)
            self.assertEqual(job.deadline, 100.0 + S.POWER_COMMAND_TIMEOUT_S)

    def test_normal_path_does_not_go_through_logind(self):
        # `systemctl poweroff` / `reboot` without --force is delayed by logind inhibitors.
        for kind in ("poweroff", "reboot"):
            argv = S.POWER_COMMANDS[(kind, 0)]
            self.assertNotIn(kind, argv)
            self.assertEqual(argv[-2:], ("start", kind + ".target"))

    def test_last_resort_does_not_sync_or_contact_the_init_system(self):
        for kind in ("poweroff", "reboot"):
            argv = S.POWER_COMMANDS[(kind, 1)]
            self.assertEqual(argv, ("/usr/sbin/" + kind, "-f", "-n"))
            self.assertNotIn("/usr/bin/systemctl", argv)

    def test_refuses_when_not_root(self):
        act, calls, log = self.make(euid=1000)
        job = act.start("poweroff", 0, 0.0)
        self.assertIs(job.poll(0.0), False)
        self.assertEqual(calls, [])
        self.assertIn("not root", log.text())

    def test_failure_is_reported(self):
        act, _, log = self.make(rc=1)
        self.assertIs(act.start("poweroff", 0, 0.0).poll(0.0), False)
        self.assertIn("poweroff command exited with status 1", log.text())
        act, _, log = self.make(exc=FileNotFoundError("systemctl"))
        self.assertIs(act.start("reboot", 1, 0.0).poll(0.0), False)
        self.assertIn("reboot command: cannot start", log.text())

    def test_null_actuator_runs_nothing(self):
        log = ListLog()
        self.assertIs(S.NullActuator(log).start("poweroff", 0, 0.0).poll(0.0), True)
        self.assertIn("ignored", log.text())

    def test_hung_command_is_polled_and_killed_without_blocking(self):
        """A stand-in process (`sleep`) in place of the power command, in real time."""
        marker = "62.%d" % os.getpid()
        started = []

        def popen(argv, **kw):          # never runs the real command
            started.append(tuple(argv))
            return subprocess.Popen(["/bin/sleep", marker], **kw)
        act = S.SystemdActuator(ListLog(), popen=popen, geteuid=lambda: 0)
        t0 = time.monotonic()
        with mock.patch.object(S, "POWER_COMMAND_TIMEOUT_S", 0.3):
            job = act.start("poweroff", 1, t0)
        self.assertEqual(started, [("/usr/sbin/poweroff", "-f", "-n")])
        polls, slowest = 0, 0.0
        while job.poll(time.monotonic()) is None:
            before = time.monotonic()
            job.poll(before)
            slowest = max(slowest, time.monotonic() - before)
            polls += 1
            time.sleep(0.01)
            self.assertLess(time.monotonic() - t0, 5.0)
        self.assertIs(job.poll(time.monotonic()), False)
        self.assertAlmostEqual(time.monotonic() - t0, 0.3, delta=0.2)   # killed at the timeout
        self.assertGreater(polls, 10)
        self.assertLess(slowest, 0.05)                                   # no poll ever waited
        job.proc.wait(timeout=2)
        ps = subprocess.run(["pgrep", "-f", "sleep " + marker], capture_output=True, text=True)
        self.assertEqual(ps.stdout.strip(), "")

    def test_service_with_hung_real_processes_keeps_sampling(self):
        """The service loop in real time, with `sleep` standing in for every power command."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        marker = "63.%d" % os.getpid()
        started = []

        def popen(argv, **kw):
            started.append(tuple(argv))
            return subprocess.Popen(["/bin/sleep", marker], **kw)
        cfg = make_config(armed=True, status_file=os.path.join(tmp.name, "power.json"),
                          state_dir=os.path.join(tmp.name, "state"),
                          disable_file=os.path.join(tmp.name, "disarm"),
                          sample_interval_s=0.05, ride_through_s=0.2)
        log = ListLog()
        service = S.Service(cfg, lambda: (-1.0, 5.0), lambda: 0,
                            S.SystemdActuator(log, popen=popen, geteuid=lambda: 0),
                            Guard(cfg.state_dir), log=log, sync=lambda: None)
        timer = threading.Timer(2.0, lambda: setattr(service, "stop", True))
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(S, "POWER_COMMAND_TIMEOUT_S", 0.3):
            service.run()
        # level 0 hung and was killed after 0.3 s, which leads straight to the last resort
        self.assertEqual(started[:2], [S.POWER_COMMANDS[("poweroff", 0)], S.POWER_COMMANDS[("poweroff", 1)]])
        self.assertEqual(log.text().count("poweroff command timed out: killing it"), 2)
        self.assertGreater(service.samples, 30)            # 2 s at 20 Hz, never stalled
        time.sleep(0.2)
        service.poll_jobs()                                # reaps the killed processes
        self.assertEqual(service._unreaped, [])
        ps = subprocess.run(["pgrep", "-f", "sleep " + marker], capture_output=True, text=True)
        self.assertEqual(ps.stdout.strip(), "")


class LoggerTest(unittest.TestCase):
    def test_priority_prefix_only_under_systemd(self):
        import io
        out = io.StringIO()
        S.make_logger(out, {})(L.WARNING, "hello")
        S.make_logger(out, {"JOURNAL_STREAM": "8:1234"})(L.WARNING, "hello")
        self.assertEqual(out.getvalue(), "hello\n<4>hello\n")

    def test_broken_stdout_is_ignored(self):
        class Broken:
            def write(self, s):
                raise BrokenPipeError()

            def flush(self):
                raise BrokenPipeError()
        S.make_logger(Broken(), {})(L.INFO, "hello")


if __name__ == "__main__":
    unittest.main()
