"""Tests of the configuration, the command line, the example config and the unit file."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import asdict, fields
from pathlib import Path
from unittest import mock

from power import __main__ as cli
from power import config
from power.config import Config
from power.guard import Guard
from power.service import NullActuator, SystemdActuator

REPO = Path(__file__).resolve().parents[2]


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, text):
        path = os.path.join(self.tmp.name, "power.toml")
        with open(path, "w") as f:
            f.write(text)
        return config.load(path)

    def test_defaults_are_monitor_only_and_match_the_brief(self):
        c = Config()
        self.assertIs(c.armed, False)
        self.assertEqual(c.single_signal_action, "shutdown")
        self.assertEqual((c.status_file, c.state_dir, c.disable_file),
                         ("/run/radar-runner/power.json", "/var/lib/radar-runner",
                          "/etc/radar-runner/power-disarm"))
        self.assertEqual((c.i2c_bus, c.ina219_address, c.shunt_ohms, c.loss_current_a, c.gpio_line),
                         (1, 0x40, 0.010, -0.3, 26))
        self.assertEqual((c.sample_interval_s, c.publish_interval_s, c.loss_confirm_samples,
                          c.restore_confirm_s), (0.2, 0.5, 2, 1.0))
        self.assertEqual((c.ride_through_s, c.low_voltage_v, c.faulty_after_s, c.faulty_clear_s),
                         (8.0, 4.5, 180.0, 60.0))
        self.assertEqual((c.sync_timeout_s, c.last_resort_after_s), (5.0, 10.0))
        self.assertEqual((c.early_boot_s, c.max_early_shutdowns, c.passive_rearm_s), (300.0, 3, 1800.0))
        self.assertEqual((c.stop_radar, c.amp_safe), ((), ()))

    def test_no_config_file_is_fine(self):
        with mock.patch.object(config, "DEFAULT_CONFIG_PATH", os.path.join(self.tmp.name, "none.toml")):
            cfg, problems = config.load(None)
        self.assertEqual((cfg, problems), (Config(), []))

    def test_missing_explicit_file_is_reported_and_defaults_apply(self):
        cfg, problems = config.load(os.path.join(self.tmp.name, "none.toml"))
        self.assertEqual(cfg, Config())
        self.assertEqual(len(problems), 1)

    def test_example_config_equals_the_defaults_and_covers_every_setting(self):
        path = REPO / "config" / "power.toml"
        cfg, problems = config.load(str(path))
        self.assertEqual(problems, [])
        self.assertEqual(cfg, Config())
        import tomllib
        data = tomllib.loads(path.read_text())
        named = {k for k, v in data.items() if not isinstance(v, dict)}
        for v in data.values():
            if isinstance(v, dict):
                named |= set(v)
        self.assertEqual(named, {f.name for f in fields(Config)})

    def test_every_setting_belongs_to_exactly_one_section(self):
        listed = [name for names in config.SECTIONS.values() for name in names]
        self.assertEqual(sorted(listed), sorted(f.name for f in fields(Config)))

    def test_settings_are_applied(self):
        cfg, problems = self.load("""
            armed = true
            single_signal_action = "quiesce"
            [paths]
            status_file = "/x/power.json"
            [shutdown]
            ride_through_s = 6
            [signals]
            ina219_address = 0x41
            [hooks]
            stop_radar = ["/usr/local/bin/radarctl", "stop"]
            stop_radar_timeout_s = 3.5
        """)
        self.assertEqual(problems, [])
        self.assertIs(cfg.armed, True)
        self.assertEqual(cfg.single_signal_action, "quiesce")
        self.assertEqual(cfg.status_file, "/x/power.json")
        self.assertEqual(cfg.ride_through_s, 6.0)
        self.assertIsInstance(cfg.ride_through_s, float)
        self.assertEqual(cfg.ina219_address, 0x41)
        self.assertEqual(cfg.stop_radar, ("/usr/local/bin/radarctl", "stop"))
        self.assertEqual(cfg.stop_radar_timeout_s, 3.5)
        self.assertEqual(cfg.state_dir, Config().state_dir)

    def test_bad_values_fall_back_to_the_default_with_a_problem(self):
        bad = ['armed = "yes"', "armed = 1", 'single_signal_action = "reboot"',
               "[shutdown]\nride_through_s = -1", "[shutdown]\nride_through_s = 500",
               '[shutdown]\nride_through_s = "8"', "[shutdown]\nride_through_s = true",
               "[shutdown]\nride_through_s = nan", "[shutdown]\nlow_voltage_v = inf",
               "[signals]\nloss_current_a = 0.3", "[signals]\nloss_confirm_samples = 1",
               "[signals]\nloss_confirm_samples = 2.5", "[signals]\ngpio_line = 99",
               '[hooks]\nstop_radar = "radarctl stop"', "[hooks]\nstop_radar = [1, 2]",
               '[hooks]\nstop_radar = [""]', "[paths]\nstatus_file = 5", '[paths]\nstate_dir = ""',
               "[guard]\nmax_early_shutdowns = 0", "[sampling]\nsample_interval_s = 0"]
        for text in bad:
            cfg, problems = self.load(text)
            self.assertEqual(cfg, Config(), text)
            self.assertEqual(len(problems), 1, text)

    def test_unknown_settings_and_sections_are_reported(self):
        cfg, problems = self.load("armd = true\n[shutdown]\nride_thru_s = 3\n[bogus]\nx = 1\n")
        self.assertEqual(cfg, Config())
        self.assertEqual(len(problems), 3)

    def test_a_setting_in_the_wrong_section_is_not_applied(self):
        cfg, problems = self.load("[signals]\narmed = true\n")
        self.assertIs(cfg.armed, False)
        self.assertEqual(len(problems), 1)

    def test_a_bad_setting_does_not_discard_the_good_ones(self):
        cfg, problems = self.load("armed = true\n[shutdown]\nride_through_s = 999\nlow_voltage_v = 4.2\n")
        self.assertIs(cfg.armed, True)
        self.assertEqual((cfg.ride_through_s, cfg.low_voltage_v), (8.0, 4.2))
        self.assertEqual(len(problems), 1)

    def test_unparseable_file_gives_defaults(self):
        for text in ("armed = true\n[shutdown\n", "\x00\x01", "armed = true\narmed = false\n", "= 3"):
            cfg, problems = self.load(text)
            self.assertEqual(cfg, Config(), repr(text))
            self.assertIs(cfg.armed, False)
            self.assertEqual(len(problems), 1, repr(text))

    def test_binary_garbage_and_directories_are_handled(self):
        path = os.path.join(self.tmp.name, "garbage.toml")
        with open(path, "wb") as f:
            f.write(b"\xff\xfe\x00armed = true")
        self.assertEqual(config.load(path)[0], Config())
        self.assertEqual(config.load(self.tmp.name)[0], Config())    # a directory

    def test_faulty_time_must_be_longer_than_the_ride_through(self):
        cfg, problems = self.load("[shutdown]\nride_through_s = 40\n[signals]\nfaulty_after_s = 45\n")
        self.assertEqual((cfg.ride_through_s, cfg.faulty_after_s), (8.0, 180.0))
        self.assertEqual(len(problems), 1)
        cfg, problems = self.load("[shutdown]\nride_through_s = 40\n")
        self.assertEqual((cfg.ride_through_s, problems), (40.0, []))

    def test_config_is_json_serialisable(self):
        json.dumps(asdict(Config()))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.status = os.path.join(self.tmp.name, "power.json")
        self.state = os.path.join(self.tmp.name, "state")
        self.paths = ["--config", os.path.join(self.tmp.name, "power.toml"), "--status-file", self.status,
                      "--state-dir", self.state, "--disable-file", os.path.join(self.tmp.name, "disarm")]
        with open(self.paths[1], "w") as f:
            f.write("armed = true\n")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(list(argv) + self.paths)
        return rc, out.getvalue()

    def test_paths_can_be_overridden_on_the_command_line(self):
        cfg, problems = cli.load_config(cli.parse_args(self.paths))
        self.assertEqual(problems, [])
        self.assertEqual((cfg.status_file, cfg.state_dir), (self.status, self.state))
        self.assertEqual(cfg.disable_file, os.path.join(self.tmp.name, "disarm"))
        self.assertIs(cfg.armed, True)

    def test_monitor_only_flag_overrides_the_config(self):
        cfg, _ = cli.load_config(cli.parse_args(self.paths + ["--monitor-only"]))
        self.assertIs(cfg.armed, False)

    def test_default_command_is_run(self):
        self.assertEqual(cli.parse_args([]).command, "run")

    def test_status_command(self):
        rc, out = self.run_cli("status")
        self.assertEqual(rc, 2)                       # no file
        with open(self.status, "w") as f:
            json.dump({"time": time.time(), "state": "normal"}, f)
        rc, out = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn('"state": "normal"', out)
        with open(self.status, "w") as f:
            json.dump({"time": time.time() - 60, "state": "normal"}, f)
        rc, out = self.run_cli("status")
        self.assertEqual(rc, 1)
        self.assertIn("STALE", out)
        with open(self.status, "w") as f:
            f.write("{")
        self.assertEqual(self.run_cli("status")[0], 2)

    def test_clear_guard_command(self):
        g = Guard(self.state)
        g.update({"count": 3, "passive": True, "last_shutdown": {"early": True}})
        self.assertEqual(g.save(), "")
        rc, out = self.run_cli("clear-guard")
        self.assertEqual(rc, 0)
        self.assertIn("before: early-shutdown count 3, passive True", out)
        g2 = Guard(self.state)
        self.assertEqual(g2.load(), [])
        self.assertEqual((g2.count, g2.passive), (0, False))
        self.assertEqual(g2.last_shutdown, {"early": True})      # kept as a record

    def test_clear_guard_with_a_corrupt_file(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "power_guard.json"), "w") as f:
            f.write("garbage")
        rc, out = self.run_cli("clear-guard")
        self.assertEqual(rc, 0)
        self.assertEqual(Guard(self.state).load(), [])

    def run_cmd_run(self, euid, extra=()):
        """cmd_run() with the service replaced by a recorder. Nothing is started."""
        made = {}

        class FakeService:
            stop = False

            def __init__(self, cfg, read_ina, read_gpio, actuator, guard, **kw):
                made.update(cfg=cfg, actuator=actuator, problems=kw["config_problems"])

            def run(self):
                return 0
        args = cli.parse_args(self.paths + list(extra))
        cfg, problems = cli.load_config(args)
        with mock.patch.object(cli, "Service", FakeService), \
                mock.patch.object(cli.os, "geteuid", lambda: euid), \
                mock.patch.object(cli.signal, "signal"):
            self.assertEqual(cli.cmd_run(cfg, problems), 0)
        return made

    def test_armed_as_root_gets_the_real_actuator(self):
        made = self.run_cmd_run(0)
        self.assertIsInstance(made["actuator"], SystemdActuator)   # constructed, never fired
        self.assertIs(made["cfg"].armed, True)

    def test_armed_without_root_runs_monitor_only(self):
        made = self.run_cmd_run(1000)
        self.assertIsInstance(made["actuator"], NullActuator)
        self.assertIs(made["cfg"].armed, False)
        self.assertEqual(len(made["problems"]), 1)

    def test_not_armed_can_never_power_off(self):
        made = self.run_cmd_run(0, ["--monitor-only"])
        self.assertIsInstance(made["actuator"], NullActuator)


class UnitFileTest(unittest.TestCase):
    def setUp(self):
        text = (REPO / "systemd" / "radar-power.service").read_text()
        self.lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]

    def value(self, key):
        found = [ln.split("=", 1)[1] for ln in self.lines if ln.startswith(key + "=")]
        self.assertEqual(len(found), 1, key)
        return found[0]

    def test_runs_the_module_as_root_with_the_system_python(self):
        self.assertEqual(self.value("User"), "root")
        self.assertTrue(self.value("ExecStart").startswith("/usr/bin/python3 "))
        self.assertTrue(self.value("ExecStart").endswith(" -m power"))
        self.assertEqual(self.value("WorkingDirectory"), str(REPO))
        self.assertTrue((REPO / "power" / "__main__.py").is_file())

    def test_never_gives_up_restarting(self):
        self.assertEqual(self.value("Restart"), "always")
        self.assertLessEqual(float(self.value("RestartSec")), 5)
        self.assertEqual(self.value("StartLimitIntervalSec"), "0")

    def test_watchdog_and_notify(self):
        self.assertEqual(self.value("Type"), "notify")
        self.assertGreaterEqual(float(self.value("WatchdogSec")), 10)

    def test_no_dependency_on_the_network_or_other_services(self):
        for key in ("Requires", "Wants", "BindsTo", "PartOf", "Requisite"):
            self.assertEqual([ln for ln in self.lines if ln.startswith(key + "=")], [], key)
        self.assertEqual(self.value("After"), "systemd-remount-fs.service")
        self.assertEqual(self.value("DefaultDependencies"), "no")
        self.assertNotIn("network", " ".join(self.lines))


if __name__ == "__main__":
    unittest.main()
