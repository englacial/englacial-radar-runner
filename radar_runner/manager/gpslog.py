"""The raw GPS log of one collection, for the runner: start the logger, stop it, collect its summary.

The logger (radar_runner/gpsraw) is a process of its own, a gpsd client. It is
started just before the radar and signalled to stop as soon as the radar
process is gone. Nothing here waits for it except `finish`, at the end of the
run's bookkeeping, for at most `stop_wait_s`; by then it has normally exited.
It can never stop, hold up or fail a radar run: no method here raises, and
every problem ends up as text in collection.json, under "gps_raw".
"""
import json
import logging
import os
import signal
import subprocess
import sys

from radar_runner.common.config import REPO

log = logging.getLogger("manager.gpslog")
LOG_NAME = "gps_raw.log"        # the logger's own messages, in the collection directory
MAX_STOP_WAIT_S = 5.0           # the end of a run's bookkeeping waits this long at most


def problems(cfg):
    """What is wrong with the manager.gps_log settings, as a list of text (empty if nothing)."""
    from radar_runner.gpsraw import ubx        # here, under the callers' guard: a broken module costs the log only
    out = []
    unknown = [m for m in cfg["messages"] if not isinstance(m, str) or m not in ubx.IDS]
    if unknown:
        out.append(f"manager.gps_log.messages: unknown message names {unknown}")
    if not isinstance(cfg["file"], str) or not cfg["file"] or os.path.basename(cfg["file"]) != cfg["file"]:
        out.append("manager.gps_log.file must be a plain file name")
    return out


class GpsLog:
    def __init__(self, cfg, gpsd_cfg, directory):
        self.cfg, self.gpsd, self.directory = cfg, gpsd_cfg, directory
        self.path = os.path.join(directory, str(cfg["file"]))
        self.proc = None
        self.signalled = False
        self.error = ""

    def _argv(self):
        return [sys.executable, "-B", "-m", "radar_runner.gpsraw", "log", "--out", self.path,
                "--host", str(self.gpsd["host"]), "--port", str(self.gpsd["port"]),
                "--fsync-s", str(self.cfg["fsync_interval_s"]), "--keep", ",".join(self.cfg["messages"]),
                "--parent-pid", str(os.getpid())]

    def start(self):
        """Start the logger. Never raises; a failure is kept for the summary."""
        try:
            problem = problems(self.cfg)
            if problem:
                self.error = "; ".join(problem)
                return
            with open(os.path.join(self.directory, LOG_NAME), "ab") as err:
                self.proc = subprocess.Popen(self._argv(), cwd=REPO, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=err, start_new_session=True)
        except Exception as e:
            self.error = f"cannot start the raw GPS logger: {getattr(e, 'strerror', None) or e}"
            log.warning("%s", self.error)

    def stop(self):
        """Ask the logger to finish (SIGTERM). Returns at once, never raises."""
        try:
            if self.proc is not None and not self.signalled:
                self.signalled = True
                self.proc.send_signal(signal.SIGTERM)
        except Exception:
            pass

    def finish(self):
        """Stop the logger if need be, wait at most stop_wait_s, and return the entry for collection.json."""
        out = {"file": self.cfg.get("file"), "bytes": None, "rawx_epochs": None, "no_raw_data": True,
               "error": self.error}
        try:
            self._finish(out)
        except Exception as e:
            out["error"] = (out["error"] + "; " if out["error"] else "") + f"internal error: {type(e).__name__}: {e}"
        out["rawx_every"] = self.cfg.get("rawx_every")     # the receiver setting this unit is meant to have
        return out

    def _finish(self, out):
        summary = {}
        if self.proc is not None:
            self.stop()
            wait = min(max(float(self.cfg["stop_wait_s"]), 0.0), MAX_STOP_WAIT_S)
            try:
                self.proc.wait(wait)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                out["error"] = f"the logger did not exit within {wait:g} s: killed"
                try:
                    self.proc.wait(1.0)         # not longer: a process stuck on a failing drive cannot die
                except subprocess.TimeoutExpired:
                    out["error"] += ", and still there 1 s later"
            if self.proc.returncode is not None:
                try:
                    text = self.proc.stdout.read(1 << 16).decode("utf-8", "replace").strip().splitlines()
                    summary = json.loads(text[-1]) if text else {}
                except (OSError, ValueError):
                    summary = {}
                finally:
                    self.proc.stdout.close()
        if isinstance(summary, dict) and summary:
            error = out["error"]
            out.update(summary)
            out["error"] = "; ".join(t for t in (error, summary.get("error")) if t)
            out["no_raw_data"] = not out.get("rawx_epochs")
            return
        try:
            out["bytes"] = os.path.getsize(self.path)
        except OSError:
            pass
        if self.proc is not None:
            missing = f"summary missing (the logger's exit status {self.proc.returncode})"
            out["error"] = "; ".join(t for t in (out["error"], missing) if t)
            # Without a summary only the file can tell: empty is no raw data, anything else is not known.
            out["no_raw_data"] = True if not out["bytes"] else None
