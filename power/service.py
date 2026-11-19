"""The power service process: sampling loop, hooks, power commands, publishing.

Everything that touches the outside world is passed in to `Service`, so the
tests can run it with fakes. The decisions are made in logic.py.
"""
import json
import os
import signal
import socket
import subprocess
import threading
import time
import traceback
from collections import deque

from . import logic as L

SYSTEMCTL = "/usr/bin/systemctl"

# How the Pi is powered off or rebooted (checked against systemd 255 on this system).
#
# Level 0, the normal path, asks systemd (PID 1) directly to start poweroff.target
# or reboot.target. Every service is stopped in the normal order, file systems are
# unmounted, then the machine powers off.
#   Plain `systemctl poweroff` is NOT used. Without --force it sends the request to
#   systemd-logind first, even for root, and logind holds it back while any "delay"
#   inhibitor lock is held, for up to InhibitDelayMaxSec. unattended-upgrades holds
#   such a lock permanently and raises that limit to 30 s on this image, which is
#   more than half of the capacitor's run time. `--check-inhibitors=no` does not
#   help: it only covers "block" locks. Starting the target goes straight to PID 1
#   (over its private socket, so it works even if D-Bus is down) and logind is not
#   involved. It is what `systemctl poweroff` does itself once logind lets go:
#   systemctl(1) describes poweroff as "mostly equivalent to systemctl start
#   poweroff.target --job-mode=replace-irreversibly --no-block".
# Last resort, if the Pi is still running `last_resort_after_s` after that command
#   (a service hanging in its stop job: the default stop timeout is 90 s; or PID 1
#   not responding), and again every 10 s: `poweroff -f -n` / `reboot -f -n`.
#   poweroff(8) on this system: -f "does not contact the init system", -n "Don't
#   sync hard disks/storage media before power-off". The command calls reboot(2)
#   itself, so it depends on nothing that could be hung: not PID 1, not D-Bus and
#   not a disk that no longer completes a sync (the data was already synced, with
#   a timeout, earlier in the sequence). Nothing is stopped or unmounted; the
#   journaling file systems recover as after a power cut, which is what would
#   happen anyway when the capacitor runs out. `systemctl poweroff --force --force`
#   is not used because it syncs first and systemctl(1) here has no --no-sync.
#
# Both are started with Popen and polled from the loop, so a command that hangs
# cannot stall the sampling loop or the watchdog pings.
POWER_COMMANDS = {
    ("poweroff", 0): (SYSTEMCTL, "--no-block", "--job-mode=replace-irreversibly", "start", "poweroff.target"),
    ("poweroff", 1): ("/usr/sbin/poweroff", "-f", "-n"),
    ("reboot", 0): (SYSTEMCTL, "--no-block", "--job-mode=replace-irreversibly", "start", "reboot.target"),
    ("reboot", 1): ("/usr/sbin/reboot", "-f", "-n"),
}
POWER_COMMAND_TIMEOUT_S = 5.0
READ_ERROR_LOG_S = 600.0   # a failing reader is logged this often, not for every sample


class ProcessJob:
    """A child process (hook or power command) that is polled, never waited for.

    It is killed, with any children, when its timeout passes.
    """

    def __init__(self, what, label, argv, timeout_s, now, popen, log):
        self.what = what
        self.label = label
        self.deadline = now + timeout_s
        self.log = log
        self.proc = None
        self.result = None
        if not argv:
            self.result = True          # nothing configured
            return
        # Children must not inherit the service's notification socket: systemctl
        # would send its own notifications on it.
        env = {k: v for k, v in os.environ.items()
               if k not in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID")}
        try:
            self.proc = popen(list(argv), stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        except Exception as e:
            log(L.ERROR, f"{label}: cannot start {argv[0]!r}: {e!r}")
            self.result = False

    def poll(self, now):
        """Return None while running, else True/False."""
        if self.result is not None:
            return self.result
        rc = self.proc.poll()
        if rc is not None:
            if rc != 0:
                self.log(L.ERROR, f"{self.label} exited with status {rc}")
            self.result = rc == 0
        elif now >= self.deadline:
            self.log(L.ERROR, f"{self.label} timed out: killing it")
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
            self.result = False
        return self.result


class DoneJob:
    """A job that finished before it started."""
    proc = None

    def __init__(self, what, result):
        self.what = what
        self.result = result

    def poll(self, now):
        return self.result


class SystemdActuator:
    """Powers the Pi off or reboots it. The only code in this repository that does."""

    def __init__(self, log, popen=subprocess.Popen, geteuid=os.geteuid):
        self.log = log
        self.popen = popen
        self.geteuid = geteuid

    def start(self, kind, level, now):
        """Start the command and return a job to poll. Does not wait."""
        argv = POWER_COMMANDS[(kind, level)]
        if self.geteuid() != 0:
            self.log(L.ERROR, f"not root: refusing to run {' '.join(argv)}")
            return DoneJob("power", False)
        self.log(L.WARNING, f"running: {' '.join(argv)}")
        return ProcessJob("power", f"{kind} command", argv, POWER_COMMAND_TIMEOUT_S, now,
                          self.popen, self.log)


class NullActuator:
    """Used when the service is not armed by its configuration: can never power off."""

    def __init__(self, log):
        self.log = log

    def start(self, kind, level, now):
        self.log(L.ERROR, f"monitor-only build of the service asked to {kind}: ignored")
        return DoneJob("power", True)


class Clock:
    monotonic = staticmethod(time.monotonic)
    wall = staticmethod(time.time)
    sleep = staticmethod(time.sleep)

    @staticmethod
    def uptime():
        return time.clock_gettime(time.CLOCK_BOOTTIME)


class Notifier:
    """systemd notification socket (sd_notify) without libsystemd."""

    def __init__(self, environ=os.environ):
        self.sock = None
        self.addr = None
        path = environ.get("NOTIFY_SOCKET", "")
        if path:
            self.addr = "\0" + path[1:] if path.startswith("@") else path
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
            self.sock.setblocking(False)
        # Ping at least three times per watchdog period, and at least once a second.
        try:
            usec = int(environ.get("WATCHDOG_USEC", "0"))
        except ValueError:
            usec = 0
        self.ping_interval = min(1.0, usec / 3e6) if usec > 0 else 1.0
        self._last_ping = None

    def send(self, text):
        if self.sock is None:
            return False
        try:
            self.sock.sendto(text.encode(), self.addr)
            return True
        except OSError:
            return False

    def ready(self):
        return self.send("READY=1")

    def watchdog(self, now):
        if self._last_ping is None or now - self._last_ping >= self.ping_interval:
            self._last_ping = now
            self.send("WATCHDOG=1")


class StatusWriter:
    """Writes the status file atomically: readers see the old or the new file, never a part."""

    def __init__(self, path):
        self.path = path
        self.tmp = path + ".tmp"
        self.error = ""

    def write(self, data):
        try:
            body = json.dumps(data, indent=1, allow_nan=False) + "\n"
            try:
                fd = os.open(self.tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
            except FileNotFoundError:
                os.makedirs(os.path.dirname(self.path), mode=0o755, exist_ok=True)
                fd = os.open(self.tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
            try:
                os.write(fd, body.encode())
            finally:
                os.close(fd)
            os.replace(self.tmp, self.path)
        except (OSError, ValueError) as e:
            error = str(e)
        else:
            error = ""
        changed = error != self.error
        self.error = error
        return changed


def make_logger(stream=None, environ=os.environ):
    """Log to stdout. Under systemd the "<N>" prefix sets the journal priority."""
    prefix = bool(environ.get("JOURNAL_STREAM"))

    def log(level, message):
        try:
            print(f"<{level}>{message}" if prefix else message, file=stream, flush=True)
        except (OSError, ValueError):
            pass   # a broken stdout must not stop the service
    return log


class _ThreadJob:
    """Runs a blocking call (sync, guard write) off the main loop, so that a hang cannot stall it."""

    def __init__(self, what, fn):
        self.what = what
        self.result = None
        self.proc = None

        def run():
            try:
                self.result = bool(fn())
            except Exception:
                self.result = False
        threading.Thread(target=run, name=what, daemon=True).start()

    def poll(self, now):
        return self.result


class Service:
    def __init__(self, cfg, read_ina, read_gpio, actuator, guard, *, clock=Clock, status=None,
                 notifier=None, log=None, sync=os.sync, popen=subprocess.Popen,
                 file_exists=os.path.lexists, config_problems=()):
        self.cfg = cfg
        self.read_ina = read_ina
        self.read_gpio = read_gpio
        self.actuator = actuator
        self.guard = guard
        self.clock = clock
        self.status = status or StatusWriter(cfg.status_file)
        self.notifier = notifier or Notifier({})
        self.log = log or make_logger()
        self.sync = sync
        self.popen = popen
        self.file_exists = file_exists
        self.config_problems = list(config_problems)

        self.stop = False
        self.jobs = []
        self._unreaped = []    # killed child processes, reaped without waiting
        self.read_error = {"current": "", "gpio": ""}
        self._error_logged = {"current": -1e9, "gpio": -1e9}
        self._last_publish = None
        self._published_state = None
        self._guard_retry_at = 0.0
        self.guard_error = ""
        self.samples = 0

        for p in self.config_problems:
            self.log(L.WARNING, p)
        for p in guard.load():
            self.log(L.WARNING, p)
        self.logic = L.PowerLogic(cfg, guard_count=guard.count, passive=guard.passive)
        # Writing the state now proves the state directory is writable. If it is
        # not, the guard cannot count shutdowns, so the service must not make any.
        self._save_guard(self.clock.monotonic())
        self.log(L.INFO, f"power service started: armed={cfg.armed} in config, early-shutdown count "
                 f"{guard.count}, passive={guard.passive}, ride-through {cfg.ride_through_s:g} s, "
                 f"status file {cfg.status_file}, state dir {cfg.state_dir}")

    # ------------------------------------------------------------------ guard

    def _save_guard(self, now):
        error = self.guard.save()
        if error and not self.guard_error:
            self.log(L.ERROR, f"cannot write guard state {self.guard.path}: {error}")
        elif self.guard_error and not error:
            self.log(L.INFO, "guard state is writable again")
        self.guard_error = error
        self._guard_retry_at = now + 30.0
        return not error

    def _record_shutdown(self, data):
        data = dict(data)
        data["last_shutdown"] = dict(data["last_shutdown"], wall_time=round(self.clock.wall(), 1))
        self.guard.update(data)
        return not self.guard.save()

    # ---------------------------------------------------------------- sampling

    def _read(self, name, reader, now):
        try:
            value = reader()
            self.read_error[name] = ""
            return value
        except Exception as e:   # a reader must never take the loop down
            self.read_error[name] = f"{type(e).__name__}: {e}"
            if now - self._error_logged[name] >= READ_ERROR_LOG_S:
                self._error_logged[name] = now
                self.log(L.WARNING, f"{name} signal read error: {e} (logged at most every "
                         f"{READ_ERROR_LOG_S / 60:.0f} min; the count is in the status file)")
            return None

    def tick(self):
        """Take one sample, run the logic, act on the result and publish."""
        before = self.clock.monotonic()
        ina = self._read("current", self.read_ina, before)
        gpio = self._read("gpio", self.read_gpio, before)
        current_a, voltage_v = ina if ina is not None else (None, None)
        now = self.clock.monotonic()    # after the reads: a failing read can take a second
        if self.guard_error and now >= self._guard_retry_at and self.logic.phase in (L.NORMAL, L.LOSS_PHASE):
            self._save_guard(now)
        actions = self.logic.step(now, current_a, voltage_v, gpio, self.clock.uptime(),
                                  disable_file=self.file_exists(self.cfg.disable_file),
                                  guard_ok=not self.guard_error)
        self.samples += 1
        self._perform(actions)
        self.poll_jobs()
        key = (self.logic.state, self.logic.mode, self.logic.stop_requested,
               tuple(self.logic.warnings), self.logic.current.state, self.logic.gpio.state)
        # Publish on any change, and otherwise at the sample nearest to the interval.
        due = self.cfg.publish_interval_s - self.cfg.sample_interval_s / 2
        if (key != self._published_state or self._last_publish is None
                or now - self._last_publish >= due):
            self._published_state = key
            self.publish(now)
        self.notifier.watchdog(now)

    def poll_jobs(self):
        # A finished job can start the next one (which may finish at once), so
        # repeat while there is progress. The steps are few; 10 rounds is ample.
        self._unreaped = [p for p in self._unreaped if p.poll() is None]
        for _ in range(10):
            progress = False
            now = self.clock.monotonic()
            for job in list(self.jobs):
                result = job.poll(now)
                if result is not None:
                    progress = True
                    self.jobs.remove(job)
                    if job.proc is not None and job.proc.poll() is None:
                        self._unreaped.append(job.proc)
                    self._perform(self.logic.done(now, job.what, result))
            if not progress:
                break

    def _perform(self, actions):
        queue = deque(actions)
        while queue:
            a = queue.popleft()
            now = self.clock.monotonic()
            if a.kind == "log":
                self.log(*a.data)
            elif a.kind == "hook":
                argv = getattr(self.cfg, a.name)
                timeout = getattr(self.cfg, a.name + "_timeout_s")
                self.log(L.INFO, f"hook {a.name}: " + (" ".join(argv) if argv else "not configured"))
                self.jobs.append(ProcessJob("hook:" + a.name, "hook " + a.name, argv, timeout, now,
                                            self.popen, self.log))
            elif a.kind == "sync":
                self.jobs.append(_ThreadJob("sync", lambda: (self.sync(), True)[1]))
            elif a.kind == "record":
                self.jobs.append(_ThreadJob("record", lambda d=a.data: self._record_shutdown(d)))
            elif a.kind == "save_guard":
                self.guard.update(a.data)
                self._save_guard(now)
            elif a.kind == "power":
                self.publish(now)     # last status before the command
                self.jobs.append(self.actuator.start(a.name, a.data, now))

    # -------------------------------------------------------------- publishing

    def publish(self, now):
        wall = self.clock.wall()
        data = {"version": 1,
                "time": round(wall, 3),
                "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall)),
                "uptime_s": round(self.clock.uptime(), 1),
                "pid": os.getpid()}
        data.update(self.logic.snapshot())
        for name in ("current", "gpio"):
            data["signals"][name]["last_error"] = self.read_error[name]
        data["guard"]["state_error"] = self.guard_error
        data["guard"]["last_shutdown"] = self.guard.last_shutdown
        if self.config_problems:
            data["warnings"]["config"] = "; ".join(self.config_problems)
        if self.status.write(data):
            if self.status.error:
                self.log(L.ERROR, f"cannot write status file {self.status.path}: {self.status.error}")
            else:
                self.log(L.INFO, f"status file {self.status.path} is being written")
        self._last_publish = now

    # -------------------------------------------------------------------- loop

    def run(self):
        """Run until `self.stop` is set. Returns the process exit status."""
        clock, interval = self.clock, self.cfg.sample_interval_s
        self.notifier.ready()
        failures = 0
        next_tick = clock.monotonic()
        while not self.stop:
            if clock.monotonic() >= next_tick:
                try:
                    self.tick()
                    failures = 0
                except Exception:
                    failures += 1
                    self.log(L.ERROR, "internal error in the sampling loop:\n" + traceback.format_exc())
                    if failures >= 25:
                        self.log(L.ERROR, "giving up; systemd will restart the service")
                        return 1
                # Keep the cadence, but never run two samples back to back.
                next_tick = max(next_tick + interval, clock.monotonic() + interval / 4)
            else:
                try:
                    self.poll_jobs()
                except Exception:
                    self.log(L.ERROR, "internal error while polling jobs:\n" + traceback.format_exc())
            delay = next_tick - clock.monotonic()
            if delay > 0:
                clock.sleep(min(delay, 0.02) if self.jobs else delay)
        self.log(L.INFO, "power service stopping")
        return 0
