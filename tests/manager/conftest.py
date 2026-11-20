"""Shared set-up for the manager tests: a stand-in ORCA tree and every path in a temporary directory.

No test here touches the SDR, the GPIO pins, the installed services or the real status files.
"""
import copy
import json
import os
import shutil
import sys
import threading
import time

import pytest

from radar_runner.common.status import StatusWriter
from radar_runner.common import config as manager_config

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_ORCA = os.path.join(HERE, "fake_orca")
CONFIGS = os.path.join(HERE, "fixtures", "orca_configs")


class Env:
    """A complete, isolated configuration for the manager, with helpers to steer the stand-ins."""

    def __init__(self, tmp):
        self.tmp = str(tmp)
        self.orca = os.path.join(self.tmp, "home", "orca")
        shutil.copytree(FAKE_ORCA, self.orca, ignore=shutil.ignore_patterns("__pycache__"))
        self.root = os.path.join(self.tmp, "nvme", "radar-runner", "collections")
        self.run_dir = os.path.join(self.tmp, "run")
        self.state_dir = os.path.join(self.tmp, "state")
        self.sysfs = os.path.join(self.tmp, "sysfs")
        for d in (self.root, self.run_dir, self.state_dir, self.sysfs):
            os.makedirs(d)
        self.control = os.path.join(self.tmp, "control.json")
        self.amp_safe_log = os.path.join(self.tmp, "amp_safe_calls.log")
        self.radar_config = os.path.join(self.tmp, "radar.yaml")
        shutil.copy(os.path.join(CONFIGS, "run300_d05_soak.yaml"), self.radar_config)
        self.power_file = os.path.join(self.tmp, "power.json")

        self.config = copy.deepcopy(manager_config.DEFAULTS)
        self.config["paths"].update(run_dir=self.run_dir, state_dir=self.state_dir,
                                    power_status_file=self.power_file)
        m = self.config["manager"]
        m["radar_config"] = self.radar_config
        m["tick_s"] = 0.02
        m["gate_interval_s"] = 0.05
        m["status_interval_s"] = 0.1
        m["run"].update(retry_delays_s=[0.2, 0.3], resume_holdoff_s=0.2, stable_run_s=60.0, min_start_gap_s=0.05,
                        max_starts_per_hour=600)        # the tests start far more often than a unit may
        m["gates"]["data"].update(mount_point="", root=self.root, start_min_free_gb=0.001, stop_min_free_gb=0.0005)
        m["gates"]["sdr"]["sysfs_dir"] = self.sysfs
        m["limits"]["max_tx_gain"] = 72.0       # the bench config in the fixture runs at 72
        m["orca"].update(
            dir=self.orca,
            env=[f"FAKE_ORCA_CONTROL={self.control}", "FAKE_RADAR_SPEEDUP=10",
                 f"FAKE_AMP_SAFE_LOG={self.amp_safe_log}"],
            chirp_command=[sys.executable, "generate_chirp.py", "{config}"],
            build_commands=[[sys.executable, "fake_build.py"]],
            radar_command=[sys.executable, "radar.py", "{config_rel}"],
            start_timeout_s=3.0, stop_timeout_s=1.5, chirp_timeout_s=5.0, build_timeout_s=5.0,
            urgent_grace_s=0.3, urgent_stop_budget_s=1.0)
        m["data"].update(chunk_max_s=0.4)       # two records per data file
        m["amp_safe"].update(command=[sys.executable, os.path.join(self.orca, "amp_safe.py"), "--timeout", "2"],
                             timeout_s=3.0, confirm_settle_s=0.0, confirm_timeout_s=0.5,
                             retry_s=0.3, idle_enabled_s=0.3, assume_enabled_retries=1,
                             unavailable_retries=1, unavailable_delay_s=0.1)
        m["panel"]["enabled"] = False
        # Never the live gpsd: the tests that log raw GPS data give their own gpsd stand-in.
        m["gps_log"]["enabled"] = False
        self.config["telemetry"]["gps"].update(host="127.0.0.1", port=1)
        self.sdr(speed=5000, product="USRP B200")
        self._writers = {}

    # ---- the stand-ins

    def behave(self, **control):
        """Tell the stand-in programs how to behave from their next start on: radar=, chirp=, build=, amp_safe=."""
        with open(self.control + ".tmp", "w") as f:
            json.dump(control, f)
        os.replace(self.control + ".tmp", self.control)

    def sdr_state(self):
        """The record of the SDR's state: "safe" or "unknown" (also when there is no record)."""
        from radar_runner.manager import ampsafe
        return ampsafe.SdrState(self.state_dir).get()

    def amp_safe_calls(self):
        try:
            with open(self.amp_safe_log) as f:
                return len(f.readlines())
        except OSError:
            return 0

    def sdr(self, speed=5000, product="USRP B200", present=True, pid="0023"):
        dev = os.path.join(self.sysfs, "4-1")
        shutil.rmtree(dev, ignore_errors=True)
        if present:
            os.makedirs(dev)
            for name, value in (("idVendor", "2500"), ("idProduct", pid), ("speed", str(speed)),
                                ("product", product)):
                with open(os.path.join(dev, name), "w") as f:
                    f.write(value + "\n")

    # ---- status files of the other services

    def publish(self, service, data):
        path = self.power_file if service == "power" else os.path.join(self.run_dir, service + ".json")
        writer = self._writers.setdefault(service, StatusWriter(path))
        assert writer.write(data) == ""

    def all_good(self, amp_current=0.16, amp_voltage=28.0):
        """Publish good status files for telemetry, watcher and power."""
        up = time.clock_gettime(time.CLOCK_BOOTTIME)
        # The fields of the real telemetry status file that the manager reads.
        self.publish("telemetry", {"state": "ok",
                                   "amp": {"fresh": True, "reason": "", "current_a": amp_current,
                                           "voltage_v": amp_voltage, "sample_uptime_s": up},
                                   "loop": {"last_gap_s": 0.25, "recent_longest_gap_s": 0.25}})
        self.publish("watcher", {"ok": True, "tripped": False, "reason": ""})
        self.publish("power", {"state": "normal", "radar_stop_requested": False})

    # ---- results

    def collections(self):
        return sorted(os.listdir(self.root))

    def collection_dir(self, index=-1):
        return os.path.join(self.root, self.collections()[index])

    def metadata(self, index=-1):
        with open(os.path.join(self.collection_dir(index), "collection.json")) as f:
            return json.load(f)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# The smallest run settings the tests may use (the real minimums are in logic.MINIMUMS).
TEST_MINIMUMS = {"retry_delay_s": 0.0, "resume_holdoff_s": 0.0, "stable_run_s": 0.0, "min_start_gap_s": 0.0}


class Events:
    """Collects what a runner posts; `wait` blocks until an event with that name has arrived."""

    def __init__(self):
        self.items = []
        self._cond = threading.Condition()

    def __call__(self, name, data):
        with self._cond:
            self.items.append((name, data))
            self._cond.notify_all()

    def names(self):
        with self._cond:
            return [name for name, _ in self.items]

    def wait(self, name, timeout=10.0, count=1):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                found = [data for n, data in self.items if n == name]
                if len(found) >= count:
                    return found[count - 1]
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no {name!r} event within {timeout} s; got {[n for n, _ in self.items]}")
                self._cond.wait(left)


@pytest.fixture
def events():
    return Events()


def wait_until(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"{what} not reached within {timeout} s")


@pytest.fixture
def wait():
    return wait_until
