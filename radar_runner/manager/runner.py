"""Runs ORCA for the manager: one collection from "start" to "amplifier made safe".

    runner = Runner(config, post)       # post(name, data) is called from the runner's threads
    runner.start({"trigger": "button", "attempt": 1, "resume": False}, flags=[...])
    runner.stop("why")                  # stop the radar, or abort the start sequence
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
build, turn-on check (hook), launch the radar, wait for its start line.

How ORCA is made to write straight into the collection directory, unchanged
(see docs/orca-proposals/README.md): the derived config gives FILES.output_dir
as a path relative to the ORCA checkout and sets FILES.max_chirps_per_file, and
the radar is given the config's path relative to the checkout as well. The
radar then writes rx_samps.bin.0, .1, ... there itself, and the chirp generator
puts chirp.bin there. Nothing is moved or copied afterwards.

One thread, started once and kept for the life of the manager, runs every
sequence and is the parent of the radar process. That matters: the kernel
sends the "parent died" signal (`setpriv --pdeathsig`) when the *thread* that
started the process ends.
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
from . import limits

log = logging.getLogger("manager.runner")

STDOUT_LOG, STDERR_LOG, PREPARE_LOG, AMP_SAFE_LOG = "uhd_stdout.log", "uhd_stderr.log", "prepare.log", "amp_safe.log"
STDERR_MAX_BYTES = 16 << 20
# After the output log is full only these lines are still written: the radar's
# file and exit reports. The lines dropped then are the per-pulse error reports.
KEEP_PREFIXES = ("[OPEN FILE]", "[CLOSE FILE]", "[RX]", "[TX] Done", "[TX] Closing", "[TX] stop", "[START]",
                 "[VERSION]")
FLOOD_PREFIXES = ("[ERROR]", "[TX] (Chirp")     # not worth a flush each


class OutputLog:
    """The radar's output as "[unix time] \\t<line>" (the format of ORCA's run.py), in parts, capped.

    Parts are <name>, <name>.1, <name>.2 ... Never raises: a write error is
    kept in `error` and the output is dropped from then on.
    """

    def __init__(self, directory, name, part_bytes, max_bytes, wall=time.time):
        self.directory, self.name = directory, name
        self.part_bytes, self.max_bytes = max(4096, int(part_bytes)), int(max_bytes)
        self._wall = wall
        self.bytes = 0          # written in all parts
        self.parts = 0
        self.dropped = 0        # lines not written because the log was full
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
        if self.bytes >= self.max_bytes and not line.startswith(KEEP_PREFIXES):
            self.dropped += 1
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
        if self.dropped and not self.error:
            limit, self.max_bytes = self.max_bytes, float("inf")
            self.write(f"[manager] {self.dropped} lines were not logged after the log reached "
                       f"{limit / 1e6:.0f} MB")
        try:
            if self._file is not None:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
        except OSError as e:
            self.error = self.error or f"{e.strerror}"
        self._file = None

    def snapshot(self):
        return {"bytes": self.bytes, "parts": self.parts, "lines_dropped": self.dropped, "error": self.error}


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
        self.directory = None
        self.name = None
        self.meta = None
        self.proc = None
        self.launched = False       # a radar process was started
        self.started = threading.Event()
        self.started_at = None
        self.usb2 = threading.Event()
        self.killed = False
        self.exit_status = None
        self.failure = ""
        self.kind = ""
        self.fatal = False
        self.amp_safe = None        # None: no radar was launched; True/False: confirmed safe or not
        self.amp_safe_ran = False   # the command was run (see ampsafe.decide)
        self.amp_safe_detail = ""
        self.chunks_closed = 0
        self.open_file = ""
        self.tail = collections.deque(maxlen=12)
        self.stdout_log = None

    def fail(self, kind, text, fatal=False):
        if not self.failure:
            self.kind, self.failure, self.fatal = kind, text, fatal

    def info(self):
        return {"requested": self.stop.is_set(), "stop_reason": self.stop_reason,
                "failure": self.failure, "kind": self.kind, "fatal": self.fatal,
                "launched": self.launched, "started": self.started.is_set(),
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
        self.making_safe = False    # a run of the tool outside a radar run is queued or in progress
        self._pending = 0
        # HOOK (stage 7, turn-on check): called after the build and before the
        # radar is launched, with the _Run. Returns (passed, text). A failure
        # is a fault ("turn_on_check"); the radar is not launched.
        self.turn_on_check = turn_on_check or (lambda run: (True, ""))
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
        The runner's one thread does both, so the tool never runs while a
        radar that this manager started is alive, and a start that is asked
        for meanwhile waits for the tool.
        """
        with self._lock:
            if self._current is not None or self.making_safe:
                return False
            self.making_safe = True
            self._pending += 1
            self._idle.clear()
            self._queue.put(("safe", why))
        return True

    def stop(self, reason):
        with self._lock:
            run = self._current
            if run is not None and not run.stop.is_set():
                run.stop_reason = reason
                run.stop.set()

    def add_flag(self, flag):
        """Mark the current collection, e.g. "no_amp_telemetry"."""
        with self._lock:
            run = self._current
            if run is not None and flag not in run.flags:
                run.flags.append(flag)

    def shutdown(self, timeout_s):
        """Stop whatever runs and wait for the amplifier to be made safe. True if idle in time."""
        self.stop("the manager is stopping")
        return self._idle.wait(timeout_s)

    def snapshot(self):
        with self._lock:
            run = self._current
        if run is None:
            return {"active": False}
        proc = run.proc
        out = {"active": True, "collection": run.name, "directory": run.directory,
               "pid": proc.pid if proc is not None and run.exit_status is None else None,
               "recording": run.started.is_set(), "chunks_closed": run.chunks_closed,
               "writing": run.open_file, "flags": list(run.flags)}
        if run.stdout_log is not None:
            out["log"] = run.stdout_log.snapshot()
        return out

    # ------------------------------------------------------------------- thread

    def _loop(self):
        while True:
            run = self._queue.get()
            if isinstance(run, tuple):          # ("safe", why): the tool, outside a radar run
                result = {"ok": False, "state": ampsafe.UNKNOWN, "detail": "internal error", "why": run[1],
                          "status": None}
                try:
                    known = self.sdr_state.known_safe
                    state, ok, detail, status = self._tool(os.path.join(self.state_dir, AMP_SAFE_LOG), known)
                    self._record_state(state, f"{detail} ({run[1]})")
                    result.update(ok=ok, state=state, detail=detail, status=status)
                except Exception:
                    log.exception("internal error while making the amplifier safe")
                with self._lock:
                    self.making_safe = False
                    self._pending -= 1
                    if not self._pending:
                        self._idle.set()
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
                if run.launched and run.amp_safe is None:
                    self._make_amp_safe(run)
                self._finish(run)
            except Exception as e:
                log.exception("internal error while ending the run")
                run.fail("internal", f"internal error while ending the run: {type(e).__name__}: {e}")
                if run.launched and run.amp_safe is None:
                    run.amp_safe, run.amp_safe_detail = False, "not attempted: internal error"
            info = run.info()
            with self._lock:
                self._current = None
                self._pending -= 1
                if not self._pending:
                    self._idle.set()
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

        # 2. The collection directory and its metadata.
        try:
            run.directory, boot, seq, problem = coll.create(root, self.state_dir, self._wall, self._boot_number)
        except OSError as e:
            return run.fail("directory", f"cannot create a collection directory in {root}: {e.strerror or e}")
        run.name = os.path.basename(run.directory)
        if problem:
            log.warning("boot counter: %s", problem)
        per_file = limits.pulses_per_file(config, cfg["data"]["chunk_mb"] * 1e6, cfg["data"]["chunk_max_s"])
        run.meta = coll.Metadata(run.directory, {
            "name": run.name, "boot": boot, "sequence": seq, "boot_id": coll.current_boot_id(),
            "trigger": run.request.get("trigger"), "attempt": run.request.get("attempt"),
            "resume": bool(run.request.get("resume")), "flags": list(run.flags),
            "created": coll.stamp(self._wall), "requested_config": cfg["radar_config"],
            "orca": {"dir": orca, "commit": coll.git_commit(orca)},
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

        # 4. Chirp, 5. build. A failure of either is a failed start.
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

        # 6. HOOK: the turn-on check of stage 7 goes here (see __init__).
        passed, text = self.turn_on_check(run)
        if not passed:
            return run.fail("turn_on_check", f"turn-on check failed: {text}", fatal=True)
        if run.stop.is_set():
            return

        # 7. The radar.
        radar_cwd = os.path.join(orca, orca_cfg["radar_cwd"])
        command = self._fill(orca_cfg["radar_command"], run, config_path)
        if not command or not os.path.isfile(os.path.join(radar_cwd, command[0])):
            return run.fail("launch", f"the radar program {command[0] if command else ''} is not there "
                                      "after the build")
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
        elif "Operating over USB 2" in line:
            run.usb2.set()

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

    def _radar(self, run, argv, cwd):
        orca_cfg, data_cfg = self.cfg["orca"], self.cfg["data"]
        run.stdout_log = OutputLog(run.directory, STDOUT_LOG, data_cfg["log_part_mb"] * 1e6,
                                   data_cfg["log_max_mb"] * 1e6, self._wall)
        stderr_log = OutputLog(run.directory, STDERR_LOG, STDERR_MAX_BYTES, STDERR_MAX_BYTES, self._wall)
        self._sync_queue = queue.Queue()
        # From here on the SDR's state is not known to be safe, until _make_amp_safe says so.
        before = (self.sdr_state.get(), self.sdr_state.why())
        error = self.sdr_state.set(ampsafe.UNKNOWN, f"radar launched, collection {run.name}")
        if error:
            return run.fail("state", f"cannot record that the SDR state is unknown: {error}")
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, env=self._env())
        except OSError as e:
            self._record_state(*before)     # nothing touched the SDR
            return run.fail("launch", f"cannot start the radar ({argv[0]}): {e.strerror or e}")
        run.proc = proc
        run.launched = True
        log.info("radar started: pid %d, collection %s", proc.pid, run.name)
        readers = [_Reader(proc.stdout, run.stdout_log, lambda line, now: self._on_line(run, cwd, line, now),
                           self._wall),
                   _Reader(proc.stderr, stderr_log, None, self._wall)]
        syncer = threading.Thread(target=self._sync_closed_files, args=(run,), daemon=True, name="radar-sync")
        for thread in readers + [syncer]:
            thread.start()

        deadline = time.monotonic() + orca_cfg["start_timeout_s"]
        announced = False
        while True:
            try:
                run.exit_status = proc.wait(0.05)
                break
            except subprocess.TimeoutExpired:
                pass
            if run.stop.is_set():
                self._stop_radar(run)
                break
            if run.usb2.is_set() and orca_cfg["require_usb3"]:
                run.fail("usb2", "the radar reports the SDR on a USB 2 link")
                self._stop_radar(run)
                break
            if run.started.is_set():
                if not announced:
                    announced = True
                    run.meta.data["recording_started"] = coll.stamp(self._wall)
                    run.meta.data["flags"] = list(run.flags)
                    run.meta.write()
                    self.post("started", {})
            elif time.monotonic() >= deadline:
                run.fail("start_timeout", f"the radar did not report {orca_cfg['start_line']} within "
                                          f"{orca_cfg['start_timeout_s']:g} s")
                self._stop_radar(run)
                break

        for thread in readers:
            thread.join(5.0)        # the pipes close when the radar is gone
        if not run.stop.is_set() and not run.failure:
            if run.exit_status != 0:
                run.fail("exit", f"the radar exited with status {run.exit_status}"
                                 + ("" if run.started.is_set() else " before it started recording"))
            elif not run.started.is_set():
                run.fail("exit", "the radar exited before it started recording")
        log.info("radar exited: status %s%s", run.exit_status, ", KILLED" if run.killed else "")
        self._make_amp_safe(run)    # first: the amplifier does not wait for the drive
        self._sync_queue.put(None)
        syncer.join(30.0)

    def _stop_radar(self, run):
        """SIGINT, wait the allowed time, SIGKILL only as the last resort."""
        proc = run.proc
        timeout_s = self.cfg["orca"]["stop_timeout_s"]
        try:
            proc.send_signal(signal.SIGINT)
        except OSError:
            pass
        try:
            run.exit_status = proc.wait(timeout_s)
        except subprocess.TimeoutExpired:
            log.error("LAST RESORT: the radar (pid %d) did not exit within %g s of SIGINT: sending SIGKILL",
                      proc.pid, timeout_s)
            self._kill(run)

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

    def _make_amp_safe(self, run):
        """After every radar exit, however it happened, and only then. Not interruptible.

        Whether the tool runs is decided in ampsafe.decide (setting amp_safe.policy).
        `run.amp_safe` becomes True if the pin is known to be safe afterwards.
        """
        cfg = self.cfg["amp_safe"]
        exit_uptime = self._uptime()
        facts = {"reached_running": run.started.is_set(), "killed": run.killed,
                 "exit_status": run.exit_status, "failure": run.failure}
        run_it, known, why = ampsafe.decide(cfg["policy"], facts,
                                            lambda: self.amp_telemetry.confirm_shutdown(exit_uptime))
        run.amp_safe_ran = run_it
        if not run_it:
            run.amp_safe, run.amp_safe_detail = True, f"command not run: {why}"
            log.info("amplifier safe, %s", run.amp_safe_detail)
            self._record_state(ampsafe.SAFE, why)
            return
        log_path = os.path.join(run.directory, AMP_SAFE_LOG) if run.directory else None
        state, ok, detail, _ = self._tool(log_path, known)
        run.amp_safe, run.amp_safe_detail = ok, f"{detail} ({why})"
        self._record_state(state, run.amp_safe_detail)
        if not ok:
            log.error("AMPLIFIER NOT CONFIRMED SAFE after the radar exited: %s", run.amp_safe_detail)

    def _tool(self, log_path, known_before):
        """Run the make-amplifier-safe tool. Returns (state, ok, text, exit status).

        Only ever called from the runner's thread, with no radar of this
        manager alive. ASSUME_ENABLED (the tool initialised the SDR and could
        not verify the pin) is urgent: the tool is run again at once, up to
        `assume_enabled_retries` times.
        """
        cfg = self.cfg["amp_safe"]
        command = [str(a) for a in cfg["command"]]
        if not command:
            state, ok = ampsafe.after_tool(None, known_before)
            return state, ok, "the make-amplifier-safe command is not configured", None
        if log_path is not None:
            try:
                if os.path.getsize(log_path) > 1 << 20:     # the log outside a collection must not grow for ever
                    os.replace(log_path, log_path + ".1")
            except OSError:
                pass
        attempts = 1 + max(0, cfg["assume_enabled_retries"])
        for attempt in range(1, attempts + 1):
            _, text, status = self._command(None, command, None, cfg["timeout_s"], "make amplifier safe", log_path)
            if status != ampsafe.ASSUME_ENABLED:
                break
            log.error("URGENT: the make-amplifier-safe tool initialised the SDR and could not verify the pin "
                      "(attempt %d of %d)", attempt, attempts)
        state, ok = ampsafe.after_tool(status, known_before)
        detail = f"command run: {ampsafe.describe(status)}"
        if status is None or status == ampsafe.KILLED_BY_CALLER:
            detail += f": {text}"
        if status == ampsafe.ASSUME_ENABLED:
            detail += f" in each of {attempts} attempts"
        elif not ok or (status != ampsafe.OK and known_before):
            detail += "; the state was " + ("known safe before and is unchanged" if ok else "not known safe before")
        return state, ok, detail, status

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
        else:
            how = "finished"
        if run.killed:
            run.meta.add_flag("killed")
        if run.stdout_log is not None and run.stdout_log.dropped:
            run.meta.add_flag("log_truncated")
        for flag in run.flags:
            run.meta.add_flag(flag)
        end = coll.stamp(self._wall)
        end.update({"how": how, "reason": run.failure or run.stop_reason, "exit_status": run.exit_status,
                    "killed": run.killed, "amp_safe": run.amp_safe, "amp_safe_command_run": run.amp_safe_ran,
                    "amp_safe_detail": run.amp_safe_detail,
                    "data_files_closed": run.chunks_closed,
                    "log": run.stdout_log.snapshot() if run.stdout_log is not None else None})
        run.meta.data["end"] = end
        error = run.meta.write()
        if error:
            log.error("cannot write %s: %s", run.meta.path, error)
