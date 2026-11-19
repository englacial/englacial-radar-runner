"""The loop that every service is built on.

A service is a subclass of `Service` with a name and a `tick` method:

    class Telemetry(Service):
        name = "telemetry"          # status file /run/radar-runner/user/telemetry.json
        interval_s = 1.0

        def setup(self):            # open devices; an exception here ends the process
            ...
        def tick(self):             # one round of work; returns the status to publish
            return {"state": "ok", "amp_temp_c": 31.2}
        def cleanup(self):          # close devices
            ...

    if __name__ == "__main__":
        sys.exit(run_service(Telemetry))

What the loop provides:

* `tick` is called every `interval_s`. The dict it returns is published in the
  status file (None publishes nothing). If it raises, nothing is published for
  that round, so the status goes stale and readers stop trusting it; after
  `max_failures` rounds in a row the process exits with status 1 and systemd
  restarts it.
* The systemd watchdog is pinged while the loop turns. A `tick` that hangs
  stops the pings, and systemd kills and restarts the service.
* SIGTERM and SIGINT end the loop after the current round; `cleanup` runs.

The matching unit settings are Type=notify, NotifyAccess=main, Restart=always,
WatchdogSec= several times `interval_s`, and
ExecStart=<repository>/.venv/bin/python -B -u -m radar_runner.<module>
with WorkingDirectory=<repository> (see systemd/radar-power.service).
"""
import argparse
import logging
import signal
import threading
import time

from . import config as config_module
from .log import setup_logging
from .sdnotify import Notifier
from .status import StatusWriter, status_path


class Service:
    name = "service"
    interval_s = 1.0
    max_failures = 10

    def __init__(self, config, *, config_warnings=(), notifier=None, status=None,
                 monotonic=time.monotonic):
        self.config = config
        self.config_warnings = list(config_warnings)
        self.log = logging.getLogger(self.name)
        self.notifier = notifier if notifier is not None else Notifier()
        self.status = status if status is not None else StatusWriter(
            status_path(self.name, config["paths"]["run_dir"]))
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._status_error = ""

    # ---- for subclasses

    def setup(self):
        pass

    def tick(self):
        raise NotImplementedError

    def cleanup(self):
        pass

    # ---- the loop

    def request_stop(self, *_):
        """Ask the loop to end. Safe to call from a signal handler or another thread."""
        self._stop.set()

    def publish(self, data):
        body = dict(data)
        if self.config_warnings:
            body["config_warnings"] = self.config_warnings
        error = self.status.write(body)
        if error != self._status_error:     # log a change, not every failed write
            if error:
                self.log.error("cannot write status file %s: %s", self.status.path, error)
            else:
                self.log.info("status file %s is being written", self.status.path)
            self._status_error = error

    def _wait_until(self, deadline):
        """Sleep until `deadline` or a stop request, pinging the watchdog on the way."""
        while not self._stop.is_set():
            self.notifier.watchdog()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, self.notifier.ping_interval))

    def run(self):
        """Run until stopped. Returns the process exit status."""
        for warning in self.config_warnings:
            self.log.warning("config: %s", warning)
        try:
            self.setup()
        except Exception:
            self.log.exception("setup failed; systemd will restart the service")
            return 1
        self.notifier.ready()
        self.log.info("%s service started", self.name)
        exit_status = 0
        failures = 0
        next_tick = self._monotonic()
        while not self._stop.is_set():
            try:
                data = self.tick()
                if data is not None:        # None: nothing to publish this round
                    self.publish(data)
                failures = 0
            except Exception:
                failures += 1
                self.log.exception("error in round %d of at most %d in a row",
                                   failures, self.max_failures)
                if failures >= self.max_failures:
                    self.log.error("giving up; systemd will restart the service")
                    exit_status = 1
                    break
            # Keep the cadence, but never run two rounds back to back.
            now = self._monotonic()
            next_tick = max(next_tick + self.interval_s, now + self.interval_s / 4)
            self._wait_until(next_tick)
        self.notifier.stopping()
        self.log.info("%s service stopping", self.name)
        try:
            self.cleanup()
        except Exception:
            self.log.exception("cleanup failed")
            exit_status = 1
        return exit_status


def run_service(service_class, argv=None):
    """Load the config, set up logging and signals, and run the service."""
    parser = argparse.ArgumentParser(description=(service_class.__doc__ or "").strip() or None)
    parser.add_argument("--local-config", default=config_module.LOCAL_FILE,
                        help="per-unit config file (default: %(default)s)")
    parser.add_argument("--secrets", default=config_module.SECRETS_FILE,
                        help="secrets file (default: %(default)s)")
    args = parser.parse_args(argv)
    config, warnings = config_module.load(local_file=args.local_config, secrets_file=args.secrets)
    problem = setup_logging(config["logging"]["level"])
    if problem:
        warnings.append(problem)
    service = service_class(config, config_warnings=warnings)
    signal.signal(signal.SIGTERM, service.request_stop)
    signal.signal(signal.SIGINT, service.request_stop)
    return service.run()
