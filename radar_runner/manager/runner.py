"""Runs ORCA for the manager: one collection from "start" to "amplifier made safe".

    runner = Runner(config, post)       # post(name, data) is called from the runner's threads
    runner.start({"trigger": "button", "attempt": 1, "resume": False}, flags=[...])
    runner.stop("why")                  # stop the radar, or abort the start sequence
    runner.stop("why", urgent=True, power=True)     # power loss: within a fixed time, nothing checked after
    runner.shutdown(timeout)            # stop and wait; for the manager's own exit

For every start, `post` is called with, in order:
    "collection"  {"name", "dir"}       the collection directory exists
    "started"     {}                    the radar printed its start line (only if it got that far)
    "ended"       {...}                 always, exactly once: the radar process is gone and the
                                        amplifier step is done (ampsafe.py; see `_Run.info`)

`runner.make_safe(why)` runs the make-amplifier-safe tool outside a radar run
and posts "safe_done".

The start sequence: read and check the requested config, create the collection
directory, write the derived config and check that too, generate the chirp,
build, turn-on check (hook), launch the radar, wait for its start line, check
the SDR's USB speed.

How ORCA is made to write straight into the collection directory, unchanged
(see docs/orca-proposals/README.md): the derived config gives FILES.output_dir
as a path relative to the ORCA checkout and sets FILES.max_chirps_per_file, and
the radar is given the config's path relative to the checkout as well. The
radar then writes rx_samps.bin.0, .1, ... there itself, and the chirp generator
puts chirp.bin there. Nothing is moved or copied afterwards.

Stopping a radar that has not printed its start line yet. ORCA installs its
SIGINT handler only after the device set-up (firmware and FPGA load, tuning),
so a SIGINT before that kills it in the middle of the set-up, with the SDR's
pins as UHD's initialisation left them. A stop that is asked for in that time
is therefore held until the start line has been seen, or until a limit has
passed: the start timeout normally, `orca.urgent_grace_s` for an urgent stop
(power loss, the manager's own exit). A radar that had to be signalled before
its start line is treated as killed in mid-set-up: the pin is not known safe.
An urgent stop has the radar process gone `orca.urgent_stop_budget_s` after it
was asked for, whatever state the radar is in.

One thread, started once and kept for the life of the manager, runs every
sequence and every run of the tool, and is the parent of the radar process.
That matters twice: the tool can never run while a radar of this manager is
alive, and the kernel sends the "parent died" signal (`setpriv --pdeathsig`)
when the *thread* that started the process ends.
"""
import collections
import logging
import os
import queue
import select
import signal
import subprocess
import threading
import time

from radar_runner.common.status import boottime_s

from . import ampsafe
from . import collection as coll
from . import gates
from . import limits

log = logging.getLogger("manager.runner")

STDOUT_LOG, STDERR_LOG, PREPARE_LOG, AMP_SAFE_LOG = "uhd_stdout.log", "uhd_stderr.log", "prepare.log", "amp_safe.log"
FLOOD_PREFIXES = ("[ERROR]", "[TX] (Chirp")     # per-pulse reports: not worth a flush each


class OutputLog:
    """The radar's output as "[unix time] \\t<line>" (the format of ORCA's run.py), in parts.

    Everything is written: the per-pulse lines ("[ERROR] (Chirp N) ...", "[TX]
    (Chirp N) time_offset increased by D") are needed to process the data.
    Parts are <name>, <name>.1, <name>.2 ... of `part_bytes` each. Never
    raises: a write error is kept in `error` and the output is dropped from
    then on (the data-drive gate stops the radar when the drive is the cause).
    """

    def __init__(self, directory, name, part_bytes, wall=time.time):
        self.directory, self.name = directory, name
        self.part_bytes = max(4096, int(part_bytes))
        self._wall = wall
        self.bytes = 0          # written in all parts
        self.lines = 0
        self.parts = 0
        self.error = ""
        self._file = None
        self._part_bytes = 0

    def _open(self):
        path = os.path.join(self.directory, self.name + (f".{self.parts}" if self.parts else ""))
        self._file = open(path, "ab")
        self.parts += 1
        self._part_bytes = 0

    def write(self, line, now=None):
        """`line` is text without its newline."""
        if self.error:
            return
        try:
            if self._file is None or self._part_bytes >= self.part_bytes:
                if self._file is not None:
                    self._file.close()
                self._open()
            data = f"[{self._wall() if now is None else now:0.3f}] \t{line}\n".encode("utf-8", "replace")
            self._file.write(data)
            self._part_bytes += len(data)
            self.bytes += len(data)
            self.lines += 1
            if not line.startswith(FLOOD_PREFIXES):
                self._file.flush()
        except OSError as e:
            self.error = f"{e.strerror}"

    def flush(self):
        try:
            if self._file is not None:
                self._file.flush()
        except OSError as e:
            self.error = self.error or f"{e.strerror}"

    def close(self):
        try:
            if self._file is not None:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
        except OSError as e:
            self.error = self.error or f"{e.strerror}"
        self._file = None

    def snapshot(self):
        return {"bytes": self.bytes, "lines": self.lines, "parts": self.parts, "error": self.error}


class _Reader(threading.Thread):
    """Drains one pipe of the radar into a log, never blocking the radar for long."""

    def __init__(self, pipe, out, on_line=None, wall=time.time):
        super().__init__(daemon=True, name="radar-output")
        self.pipe, self.out, self.on_line, self._wall = pipe, out, on_line, wall

    def run(self):
        fd = self.pipe.fileno()
        pending = b""
        try:
            while True:
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    self.out.flush()            # quiet for a second: get what we have onto the disk
                    continue
                data = os.read(fd, 1 << 16)
                if not data:
                    break
                *lines, pending = (pending + data).split(b"\n")
                if len(pending) > 1 << 16:      # no newline for a long time
                    lines.append(pending)
                    pending = b""
                now = self._wall()
                for raw in lines:
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if self.on_line is not None:
                        try:
                            self.on_line(line, now)
                        except Exception:
                            log.exception("error while reading the radar output")
                    self.out.write(line, now)
            if pending:
                self.out.write(pending.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass
        finally:
            self.out.close()
            try:
                self.pipe.close()
            except OSError:
                pass


class _Run:
    """State of one start request."""

    def __init__(self, request, flags):
        self.request = dict(request)
        self.flags = list(flags)
        self.stop = threading.Event()
        self.stop_reason = ""
        self.urgent_at = None       # monotonic time of the first urgent stop request
        self.power = False          # the stop came from the power service
        self.directory = None
        self.name = None
        self.meta = None
        self.proc = None
        self.launching = False      # decided, under the lock, that the radar will be launched
        self.launched = False       # a radar process was started
        self.radar_over = False     # ... and is gone again
        self.started = threading.Event()
        self.started_at = None
        self.signalled_before_start = False
        self.continuous = True      # CHIRP.num_pulses < 0: the radar runs until it is stopped
        self.interrupted = ""       # it exited with status 0 all the same, unasked
        self.killed = False
        self.exit_status = None
        self.failure = ""
        self.kind = ""
        self.fatal = False
        self.amp_safe = None        # None: nothing checked; True/False: the pin is / is not known safe
        self.amp_safe_ran = False   # the tool was run
        self.amp_safe_detail = ""
        self.usb_speed = None
        self.chunks_closed = 0
        self.open_file = ""
        self.tail = collections.deque(maxlen=12)
        self.stdout_log = None
        self.stderr_log = None

    def fail(self, kind, text, fatal=False):
        if not self.failure:
            self.kind, self.failure, self.fatal = kind, text, fatal

    def info(self):
        return {"requested": self.stop.is_set(), "stop_reason": self.stop_reason,
                "failure": self.failure, "kind": self.kind, "fatal": self.fatal,
                "interrupted": self.interrupted,
                "launched": self.launched, "started": self.started.is_set(),
                "signalled_before_start": self.signalled_before_start,
                "exit_status": self.exit_status, "killed": self.killed,
                "amp_safe": self.amp_safe, "amp_safe_command_run": self.amp_safe_ran,
                "amp_safe_detail": self.amp_safe_detail,
                "collection": self.name, "chunks_closed": self.chunks_closed,
                "output_tail": list(self.tail)}


class Runner:
    def __init__(self, config, post, *, wall=time.time, turn_on_check=None, boot_number=coll.boot_number,
                 amp_telemetry=None, uptime=boottime_s):
        self.cfg = config["manager"]
        self.state_dir = config["paths"]["state_dir"]
        self.post = post
        self._wall = wall
        self._uptime = uptime
        self._boot_number = boot_number
        self.amp_telemetry = amp_telemetry or ampsafe.AmpTelemetry(config)
        self.sdr_state = ampsafe.SdrState(self.state_dir)
        self.sdr_gate = gates.SdrGate(self.cfg["gates"]["sdr"])
        self.making_safe = False    # a run of the tool outside a radar run is queued or in progress
        # Uptime at which the last radar or tool run ended; a shutdown-current
        # reading counts only if it is newer than this (ampsafe.AmpTelemetry.now).
        self.last_activity_uptime = uptime()
        self._pending = 0
        # HOOK (stage 7, turn-on check): called after the build and before the
        # radar is launched, with the _Run. Returns (passed, text). A failure
        # is a fault ("turn_on_check"); the radar is not launched. The SDR's
        # state is recorded as unknown before the hook is called.
        self.turn_on_check = turn_on_check
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._current = None
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="radar-runner")
        self._thread.start()

    # ---------------------------------------------------------------- interface

    @property
    def busy(self):
        return not self._idle.is_set()

    def start(self, request, flags=()):
        with self._lock:
            if self._current is not None:
                raise RuntimeError("a run is already in progress")
            self._current = _Run(request, flags)
            self._pending += 1
            self._idle.clear()
            self._queue.put(self._current)

    def make_safe(self, why):
        """Run the make-amplifier-safe tool now, outside a radar run. Posts "safe_done" {ok, state, detail, why}.

        Returns False (and does nothing) while a radar run or another such
        request is in progress: the end of a radar run decides for itself.
        """
        with self._lock:
            if self._current is not None or self.making_safe:
                return False
            self.making_safe = True
            self._pending += 1
            self._idle.clear()
            self._queue.put(("safe", why))
        return True

    def stop(self, reason, urgent=False, power=False):
        """Stop the radar or abort the start. May be called again to make a stop urgent."""
        with self._lock:
            run = self._current
            if run is None:
                return
            if not run.stop.is_set():
                run.stop_reason = reason
                run.stop.set()
            if urgent and run.urgent_at is None:
                run.urgent_at = time.monotonic()
            run.power = run.power or power

    def add_flag(self, flag):
        """Mark the current collection, e.g. "no_amp_telemetry"."""
        with self._lock:
            run = self._current
            if run is not None and flag not in run.flags:
                run.flags.append(flag)

    def radar_gone(self):
        """True if no radar process of this manager exists, and none is about to be started."""
        with self._lock:
            run = self._current
            if run is None or run.radar_over:
                return True
            if run.launching:
                return False
            return run.stop.is_set()        # it will never be launched (decided under this lock)

    def shutdown(self, timeout_s):
        """Stop whatever runs, urgently, and wait until all is done. True if idle in time."""
        self.stop("the manager is stopping", urgent=True)
        return self._idle.wait(timeout_s)

    def snapshot(self):
        with self._lock:
            run = self._current
        if run is None:
            return {"active": False}
        proc = run.proc
        out = {"active": True, "collection": run.name, "directory": run.directory,
               "pid": proc.pid if proc is not None and not run.radar_over else None,
               "recording": run.started.is_set(), "chunks_closed": run.chunks_closed,
               "writing": run.open_file, "flags": list(run.flags), "usb_speed_mbps": run.usb_speed}
        if run.stdout_log is not None:
            out["log"] = run.stdout_log.snapshot()
        return out

    # ------------------------------------------------------------------- thread

    def _done(self):
        with self._lock:
            self._pending -= 1
            if not self._pending:
                self._idle.set()

    def _loop(self):
        while True:
            run = self._queue.get()
            if isinstance(run, tuple):          # ("safe", why): the tool, outside a radar run
                result = {"ok": False, "state": ampsafe.UNKNOWN, "detail": "internal error", "why": run[1],
                          "status": None}
                try:
                    ok, detail, status = self._tool(os.path.join(self.state_dir, AMP_SAFE_LOG), patient=False)
                    result.update(ok=ok, state=self.sdr_state.get(), detail=detail, status=status)
                except Exception:
                    log.exception("internal error while making the amplifier safe")
                with self._lock:
                    self.making_safe = False
                self._done()
                self.post("safe_done", result)
                continue
            try:
                self._run(run)
            except Exception as e:      # never lose the thread: it is the radar's parent
                log.exception("internal error in the runner")
                run.fail("internal", f"internal error in the runner: {type(e).__name__}: {e}")
            try:
                if run.proc is not None and run.proc.poll() is None:
                    self._kill(run)     # only after an internal error: no radar may outlive its run
                    run.radar_over = True
                    self.last_activity_uptime = self._uptime()
                if run.launched and run.amp_safe is None and not run.amp_safe_detail:
                    self._after_radar(run)
                self._finish(run)
            except Exception as e:
                log.exception("internal error while ending the run")
                run.fail("internal", f"internal error while ending the run: {type(e).__name__}: {e}")
                if run.launched and run.amp_safe is None:
                    run.amp_safe, run.amp_safe_detail = False, "not attempted: internal error"
            info = run.info()
            with self._lock:
                self._current = None
            self._done()
            self.post("ended", info)

    def _env(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID")}
        for item in self.cfg["orca"]["env"]:
            name, sep, value = str(item).partition("=")
            if sep and name:
                env[name] = value
        return env

    def _command(self, stop, argv, cwd, timeout_s, label, log_path):
        """Run a helper command to its end. Returns (ok, text, exit status).

        Output goes to `log_path` (appended). `stop` is an Event that aborts
        the command, or None for a command that must not be interrupted. The
        status is None if the command could not be started, and
        ampsafe.KILLED_BY_CALLER if it was killed here (timeout or abort).
        """
        try:
            out = open(log_path, "ab")
            out.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {label}: {' '.join(argv)}\n"
                      .encode())
            out.flush()
        except (OSError, TypeError):
            out = None              # the data drive is gone: the command must still run
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=out if out is not None else subprocess.DEVNULL,
                                    stderr=subprocess.STDOUT, start_new_session=True, env=self._env())
        except OSError as e:
            return False, f"{label}: cannot run {argv[0]}: {e.strerror or e}", None
        finally:
            if out is not None:
                out.close()
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                status = proc.wait(0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            timed_out = time.monotonic() >= deadline
            if timed_out or (stop is not None and stop.is_set()):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
                return (False, f"{label}: " + (f"no result within {timeout_s:g} s" if timed_out else "aborted"),
                        ampsafe.KILLED_BY_CALLER)
        if status == 0:
            return True, "", 0
        return False, f"{label} failed (status {status}; see {os.path.basename(str(log_path))})", status

    def _fill(self, argv, run, config_path):
        orca = os.path.realpath(self.cfg["orca"]["dir"])
        values = {"config": config_path, "config_rel": os.path.relpath(config_path, orca),
                  "collection": run.directory, "orca": orca}
        out = []
        for arg in argv:
            arg = str(arg)
            for key, value in values.items():
                arg = arg.replace("{" + key + "}", value)
            out.append(arg)
        return out

    # ------------------------------------------------------------- the sequence

    def _run(self, run):
        cfg, orca_cfg = self.cfg, self.cfg["orca"]
        orca = os.path.realpath(orca_cfg["dir"])
        root = os.path.realpath(cfg["gates"]["data"]["root"])

        # 1. The requested config, exactly as it is on disk now.
        try:
            with open(cfg["radar_config"], "rb") as f:
                requested = f.read(1 << 20)
            config = limits.load(requested.decode("utf-8"))
        except (OSError, UnicodeDecodeError, limits.ConfigError) as e:
            return run.fail("config", f"radar config {cfg['radar_config']}: {getattr(e, 'strerror', None) or e}",
                            fatal=True)
        problems = limits.check(config, cfg["limits"])
        if problems:
            return run.fail("config", "radar config refused: " + "; ".join(problems), fatal=True)
        run.continuous = limits.continuous(config)

        # 2. The collection directory and its metadata.
        try:
            run.directory, boot, seq, problem = coll.create(root, self.state_dir, self._wall, self._boot_number)
        except OSError as e:
            return run.fail("directory", f"cannot create a collection directory in {root}: {e.strerror or e}")
        run.name = os.path.basename(run.directory)
        if problem:
            log.warning("boot counter: %s", problem)
        per_file = limits.pulses_per_file(config, cfg["data"]["chunk_mb"] * 1e6, cfg["data"]["chunk_max_s"])
        dirty, changed = coll.git_dirty(orca)
        run.meta = coll.Metadata(run.directory, {
            "name": run.name, "boot": boot, "sequence": seq, "boot_id": coll.current_boot_id(),
            "trigger": run.request.get("trigger"), "attempt": run.request.get("attempt"),
            "resume": bool(run.request.get("resume")), "flags": list(run.flags),
            "created": coll.stamp(self._wall), "requested_config": cfg["radar_config"],
            # dirty: the working tree differs from the commit (None = could not be told); changed: which files
            "orca": {"dir": orca, "commit": coll.git_commit(orca), "dirty": dirty, "changed": changed},
            "radar_binary": None,
            "data": {"file": limits.DATA_FILE, "pulses_per_file": per_file,
                     "record_bytes": limits.record_bytes(config), "sample_format": "complex64 little-endian"},
        })
        self.post("collection", {"name": run.name, "dir": run.directory})

        # 3. The derived config: what the radar reads. Checked again as written.
        config_path = os.path.join(run.directory, "config.yaml")
        try:
            derived = limits.derive(config, os.path.relpath(run.directory, orca), per_file)
            coll.write_durable(os.path.join(run.directory, "config_requested.yaml"), requested)
            coll.write_durable(config_path, limits.dump(derived))
            written = limits.load_file(config_path)
        except (OSError, limits.ConfigError) as e:
            return run.fail("config", f"cannot write the collection config: {getattr(e, 'strerror', None) or e}",
                            fatal=isinstance(e, limits.ConfigError))
        problems = limits.check(written, cfg["limits"]) + limits.check_output(written, orca, root)
        if written != derived:
            problems.append("the written config does not read back as it was derived")
        if problems:
            return run.fail("config", "collection config refused: " + "; ".join(problems), fatal=True)
        run.meta.write()
        if run.stop.is_set():
            return

        # 4. Chirp, 5. build. A failure of either is a failed start. No radar exists yet:
        # a stop aborts these at once.
        prepare_log = os.path.join(run.directory, PREPARE_LOG)
        ok, text, _ = self._command(run.stop, self._fill(orca_cfg["chirp_command"], run, config_path),
                                    os.path.join(orca, orca_cfg["chirp_cwd"]), orca_cfg["chirp_timeout_s"],
                                    "chirp generation", prepare_log)
        if ok:
            try:
                ok = os.path.getsize(os.path.join(run.directory, limits.CHIRP_FILE)) > 0
            except OSError:
                ok = False
            text = "" if ok else "chirp generation wrote no chirp file into the collection directory"
        if not ok:
            return None if run.stop.is_set() else run.fail("chirp", text)
        for argv in orca_cfg["build_commands"]:
            if not isinstance(argv, list) or not argv:
                return run.fail("build", "orca.build_commands must be a list of commands", fatal=True)
            ok, text, _ = self._command(run.stop, self._fill(argv, run, config_path),
                                        os.path.join(orca, orca_cfg["build_cwd"]), orca_cfg["build_timeout_s"],
                                        "build", prepare_log)
            if not ok:
                return None if run.stop.is_set() else run.fail("build", text)
        radar_cwd = os.path.join(orca, orca_cfg["radar_cwd"])
        command = self._fill(orca_cfg["radar_command"], run, config_path)
        binary = os.path.join(radar_cwd, command[0]) if command else ""
        if not command or not os.path.isfile(binary):
            return run.fail("launch", f"the radar program {command[0] if command else ''} is not there "
                                      "after the build")
        run.meta.data["radar_binary"] = {"path": binary, "sha256": coll.sha256_file(binary)}
        run.meta.write()

        # 6. HOOK: the turn-on check of stage 7 goes here (see __init__). It runs a
        # radar of its own, so the SDR's state is unknown from here on.
        if self.turn_on_check is not None:
            if run.stop.is_set():
                return
            self._record_state(ampsafe.UNKNOWN, f"turn-on check, collection {run.name}")
            passed, text = self.turn_on_check(run)
            if not passed:
                return run.fail("turn_on_check", f"turn-on check failed: {text}", fatal=True)

        # 7. The radar.
        self._radar(run, list(orca_cfg["launch_prefix"]) + command, radar_cwd)

    def _on_line(self, run, radar_cwd, line, now):
        run.tail.append(line[:300])
        if line.startswith(self.cfg["orca"]["start_line"]):
            run.started_at = now
            run.started.set()
        elif line.startswith("[OPEN FILE] "):
            run.open_file = os.path.basename(line[12:].strip())
        elif line.startswith("[CLOSE FILE] "):
            run.chunks_closed += 1
            self._sync_queue.put(os.path.join(radar_cwd, line[13:].strip()))

    def _sync_closed_files(self, run):
        """Make each data file durable when the radar has closed it (the radar itself does not sync)."""
        root = os.path.realpath(run.directory)
        while True:
            path = self._sync_queue.get()
            if path is None:
                return
            try:
                real = os.path.realpath(path)
                if os.path.dirname(real) != root:
                    continue
                fd = os.open(real, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                coll.fsync_dir(root)
            except OSError as e:
                log.warning("cannot sync %s: %s", path, e.strerror)

    def _usb_check(self, run):
        """After the start line: the SDR's link speed, from sysfs. Returns "" / a failure text / None (not yet)."""
        cfg = self.cfg["gates"]["sdr"]
        found = self.sdr_gate.find()
        if len(found) != 1:
            return None                 # not visible right now: look again
        name, speed, _ = found[0]
        run.usb_speed = speed
        if speed < cfg["min_speed_mbps"]:
            return (f"the SDR is on a USB 2 link ({speed} Mbit/s at {name}); the radar needs USB 3 "
                    f"({cfg['min_speed_mbps']} Mbit/s)")
        return ""

    def _radar(self, run, argv, cwd):
        orca_cfg, data_cfg = self.cfg["orca"], self.cfg["data"]
        part_bytes = data_cfg["log_part_mb"] * 1e6
        run.stdout_log = OutputLog(run.directory, STDOUT_LOG, part_bytes, self._wall)
        run.stderr_log = OutputLog(run.directory, STDERR_LOG, part_bytes, self._wall)
        self._sync_queue = queue.Queue()
        with self._lock:                # against radar_gone(): a stop from here on finds a radar
            if run.stop.is_set():
                return
            run.launching = True
        # From here on the SDR's state is not known to be safe, until _after_radar says so.
        before = (self.sdr_state.get(), self.sdr_state.why())
        error = self.sdr_state.set(ampsafe.UNKNOWN, f"radar launched, collection {run.name}")
        if error:
            run.launching = False
            return run.fail("state", f"cannot record that the SDR state is unknown: {error}")
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, env=self._env())
        except OSError as e:
            if before[1] == "no record":        # nothing touched the SDR: put the record back as it was
                try:
                    os.unlink(self.sdr_state.path)
                except OSError:
                    pass
            else:
                self._record_state(*before)
            run.launching = False
            return run.fail("launch", f"cannot start the radar ({argv[0]}): {e.strerror or e}")
        run.proc = proc
        run.launched = True
        log.info("radar started: pid %d, collection %s", proc.pid, run.name)
        readers = [_Reader(proc.stdout, run.stdout_log, lambda line, now: self._on_line(run, cwd, line, now),
                           self._wall),
                   _Reader(proc.stderr, run.stderr_log, None, self._wall)]
        syncer = threading.Thread(target=self._sync_closed_files, args=(run,), daemon=True, name="radar-sync")
        for thread in readers + [syncer]:
            thread.start()

        start_deadline = time.monotonic() + orca_cfg["start_timeout_s"]
        announced = False
        usb_checked = not orca_cfg["require_usb3"]
        usb_deadline = None
        while True:
            try:
                run.exit_status = proc.wait(0.05)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            started = run.started.is_set()
            if started and not usb_checked:
                usb_deadline = usb_deadline or now + 3.0
                problem = self._usb_check(run)
                if problem:
                    run.fail("usb2", problem)
                    usb_checked = True
                elif problem == "" or now >= usb_deadline:
                    usb_checked = True
                    if problem is None:
                        log.warning("the SDR's USB speed could not be read from sysfs")
                        self.add_flag("usb_speed_unknown")
            if run.stop.is_set() or run.failure:
                if started:
                    self._stop_radar(run)
                    break
                # No handler for SIGINT in the radar yet: hold the stop until the start line.
                hold_until = start_deadline
                if run.urgent_at is not None:
                    hold_until = min(hold_until, run.urgent_at + orca_cfg["urgent_grace_s"])
                if now >= hold_until:
                    run.signalled_before_start = True
                    log.warning("stopping the radar before its start line: it dies in the middle of the SDR set-up")
                    self._stop_radar(run)
                    break
                continue
            if started:
                if not announced and usb_checked:
                    announced = True
                    run.meta.data["recording_started"] = coll.stamp(self._wall)
                    run.meta.data["flags"] = list(run.flags)
                    run.meta.data["usb_speed_mbps"] = run.usb_speed
                    run.meta.write()
                    self.post("started", {})
            elif now >= start_deadline:
                run.fail("start_timeout", f"the radar did not report {orca_cfg['start_line']} within "
                                          f"{orca_cfg['start_timeout_s']:g} s")
                run.signalled_before_start = True
                self._stop_radar(run)
                break
        with self._lock:
            run.radar_over = True
        self.last_activity_uptime = self._uptime()

        for thread in readers:
            thread.join(5.0)        # the pipes close when the radar is gone
        if not run.stop.is_set() and not run.failure:
            if run.exit_status != 0:
                run.fail("exit", f"the radar exited with status {run.exit_status}"
                                 + ("" if run.started.is_set() else " before it started recording"))
            elif not run.started.is_set():
                run.fail("exit", "the radar exited before it started recording")
            elif run.continuous:
                run.interrupted = "the radar exited with status 0 without being asked to (num_pulses is -1)"
        log.info("radar exited: status %s%s", run.exit_status, ", KILLED" if run.killed else "")
        self._after_radar(run)      # first: the amplifier does not wait for the drive
        self._sync_queue.put(None)
        syncer.join(30.0)

    def _stop_radar(self, run):
        """SIGINT, wait the allowed time, SIGKILL only as the last resort.

        Normally the radar gets `stop_timeout_s` to exit. An urgent stop has
        it gone `urgent_stop_budget_s` after the stop was asked for.
        """
        proc, cfg = run.proc, self.cfg["orca"]
        try:
            proc.send_signal(signal.SIGINT)
        except OSError:
            pass
        deadline = time.monotonic() + cfg["stop_timeout_s"]
        while True:
            try:
                run.exit_status = proc.wait(0.05)
                return
            except subprocess.TimeoutExpired:
                pass
            limit = deadline
            if run.urgent_at is not None:
                limit = min(limit, run.urgent_at + cfg["urgent_stop_budget_s"])
            if time.monotonic() >= limit:
                log.error("LAST RESORT: the radar (pid %d) did not exit in time after SIGINT: sending SIGKILL",
                          proc.pid)
                self._kill(run)
                return

    def _kill(self, run):
        proc = run.proc
        if proc is None or proc.poll() is not None:
            return
        run.killed = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            run.exit_status = proc.wait(10.0)
        except subprocess.TimeoutExpired:
            log.error("the radar (pid %d) is still there 10 s after SIGKILL", proc.pid)

    # --------------------------------------------------------- amplifier made safe

    def _after_radar(self, run):
        """After every radar exit, however it happened, and only then: is the pin safe? (ampsafe.py)

        `run.amp_safe` becomes True if the pin is known to be safe afterwards,
        False if not, and stays None if nothing was checked (power loss).
        """
        exit_uptime = self._uptime()
        facts = {"reached_running": run.started.is_set(), "killed": run.killed,
                 "exit_status": run.exit_status, "failure": run.failure,
                 "signalled_before_start": run.signalled_before_start}
        power = ""
        if run.power:
            power = "the stop came from the power service"
        elif self.amp_telemetry.rail_absent():
            power = "the 28 V rail reads absent"
        action, why = ampsafe.after_exit(facts, lambda: self.amp_telemetry.confirm_shutdown(exit_uptime), power)
        if action == "leave":
            run.amp_safe_detail = why
            log.warning("amplifier pin not checked after the radar exit: %s", why)
        elif action == "safe":
            run.amp_safe, run.amp_safe_detail = True, f"tool not run: {why}"
            log.info("amplifier safe, %s", run.amp_safe_detail)
            self._record_state(ampsafe.SAFE, why)
        else:
            run.amp_safe_ran = True
            log_path = os.path.join(run.directory, AMP_SAFE_LOG) if run.directory else None
            ok, detail, _ = self._tool(log_path, patient=run.urgent_at is None)
            run.amp_safe, run.amp_safe_detail = ok, f"{detail} ({why})"
            if not ok:
                log.error("AMPLIFIER NOT CONFIRMED SAFE after the radar exited: %s", run.amp_safe_detail)

    def _tool(self, log_path, patient):
        """Run the make-amplifier-safe tool. Returns (ok, text, exit status).

        Only ever called from the runner's thread, with no radar of this
        manager alive. The tool's wrapper keeps the record itself
        (--state-file): `unknown` before it starts, `safe` after an OK.

        ASSUME_ENABLED (the tool initialised the SDR and could not verify the
        pin) is urgent: the tool is run again at once, up to
        `assume_enabled_retries` times. NOT_FOUND, BUSY and TIMEOUT can mean
        that the SDR is coming back after a radar was killed: if `patient`,
        the tool is tried again up to `unavailable_retries` times,
        `unavailable_delay_s` apart.
        """
        cfg = self.cfg["amp_safe"]
        command = [str(a) for a in cfg["command"]]
        if not command:
            return False, "the make-amplifier-safe command is not configured", None
        command += ["--state-file", self.sdr_state.path]
        if log_path is not None:
            try:
                if os.path.getsize(log_path) > 1 << 20:     # the log outside a collection must not grow for ever
                    os.replace(log_path, log_path + ".1")
            except OSError:
                pass
        runs, urgent_left, later_left = 0, max(0, cfg["assume_enabled_retries"]), max(0, cfg["unavailable_retries"])
        while True:
            _, text, status = self._command(None, command, None, cfg["timeout_s"], "make amplifier safe", log_path)
            runs += 1
            self.last_activity_uptime = self._uptime()
            if status == ampsafe.ASSUME_ENABLED and urgent_left:
                urgent_left -= 1
                log.error("URGENT: the make-amplifier-safe tool initialised the SDR and could not verify the pin: "
                          "running it again")
                continue
            if patient and status in ampsafe.RETRY_LATER and later_left:
                later_left -= 1
                log.warning("make-amplifier-safe tool: %s; trying again in %g s", ampsafe.describe(status),
                            cfg["unavailable_delay_s"])
                time.sleep(cfg["unavailable_delay_s"])
                continue
            break
        detail = f"tool run {runs} time{'s' if runs != 1 else ''}: {ampsafe.describe(status)}"
        if status is None or status == ampsafe.KILLED_BY_CALLER:
            detail += f": {text}"
        return status == ampsafe.OK, detail, status

    def _record_state(self, state, why):
        error = self.sdr_state.set(state, why)
        if error:
            log.warning("cannot record the SDR state: %s", error)

    def _finish(self, run):
        """Write the end of the collection's metadata."""
        if run.meta is None:
            return
        if run.failure:
            how = "failed"
        elif not run.launched:
            how = "not_started"
        elif run.stop.is_set():
            how = "stopped"
        elif run.interrupted:
            how = "interrupted"
        else:
            how = "finished"
        if run.killed:
            run.meta.add_flag("killed")
        if run.signalled_before_start:
            run.meta.add_flag("stopped_before_start")
        for flag in run.flags:
            run.meta.add_flag(flag)
        end = coll.stamp(self._wall)
        end.update({"how": how, "reason": run.failure or run.interrupted or run.stop_reason,
                    "exit_status": run.exit_status, "killed": run.killed,
                    "amp_safe": run.amp_safe, "amp_safe_command_run": run.amp_safe_ran,
                    "amp_safe_detail": run.amp_safe_detail,
                    "data_files_closed": run.chunks_closed,
                    # the radar's output log (uhd_stdout.log, .1, ...) and UHD's own messages (uhd_stderr.log)
                    "log": run.stdout_log.snapshot() if run.stdout_log is not None else None,
                    "stderr_log": run.stderr_log.snapshot() if run.stderr_log is not None else None})
        run.meta.data["end"] = end
        error = run.meta.write()
        if error:
            log.error("cannot write %s: %s", run.meta.path, error)
