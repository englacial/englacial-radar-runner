"""The raw GPS log of one collection, for the runner: start the logger, stop it, collect its summary.

The logger (radar_runner/gpsraw) is a process of its own, a gpsd client. It is
started just before the radar and signalled to stop as soon as the radar
process is gone. Nothing here waits for it except `finish`, at the end of the
run's bookkeeping, for at most `stop_wait_s`; by then it has normally exited.
It can never stop, hold up or fail a radar run: every error ends up as text in
collection.json, under "gps_raw".
"""
import json
import logging
import os
import signal
import subprocess
import sys

from radar_runner.common.config import REPO
from radar_runner.gpsraw import ubx

log = logging.getLogger("manager.gpslog")
LOG_NAME = "gps_raw.log"        # the logger's own messages, in the collection directory


def problems(cfg):
    """What is wrong with the manager.gps_log settings, as a list of text (empty if nothing)."""
    out = []
    unknown = [m for m in cfg["messages"] if m not in ubx.IDS]
    if unknown:
        out.append(f"manager.gps_log.messages: unknown message names {unknown}")
    if not cfg["file"] or os.path.basename(cfg["file"]) != cfg["file"]:
        out.append("manager.gps_log.file must be a plain file name")
    if not 0 <= cfg["rawx_every"] <= 255:
        out.append("manager.gps_log.rawx_every must be between 0 and 255")
    return out


class GpsLog:
    def __init__(self, cfg, gpsd_cfg, directory):
        self.cfg, self.gpsd, self.directory = cfg, gpsd_cfg, directory
        self.path = os.path.join(directory, cfg["file"])
        self.proc = None
        self.signalled = False
        self.error = ""

    def start(self):
        """Start the logger. Never raises; a failure is kept for the summary."""
        problem = problems(self.cfg)
        if problem:
            self.error = "; ".join(problem)
            return
        argv = [sys.executable, "-B", "-m", "radar_runner.gpsraw", "log", "--out", self.path,
                "--host", str(self.gpsd["host"]), "--port", str(self.gpsd["port"]),
                "--fsync-s", str(self.cfg["fsync_interval_s"]), "--keep", ",".join(self.cfg["messages"])]
        try:
            with open(os.path.join(self.directory, LOG_NAME), "ab") as err:
                self.proc = subprocess.Popen(argv, cwd=REPO, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=err, start_new_session=True)
        except OSError as e:
            self.error = f"cannot start the raw GPS logger: {e.strerror or e}"
            log.warning("%s", self.error)

    def stop(self):
        """Ask the logger to finish (SIGTERM). Returns at once."""
        if self.proc is not None and not self.signalled:
            self.signalled = True
            try:
                self.proc.send_signal(signal.SIGTERM)
            except OSError:
                pass

    def finish(self):
        """Stop the logger if need be, wait at most stop_wait_s, and return the entry for collection.json."""
        out = {"file": self.cfg["file"], "bytes": None, "rawx_epochs": None, "no_raw_data": True,
               "error": self.error}
        if self.proc is not None:
            self.stop()
            try:
                self.proc.wait(self.cfg["stop_wait_s"])
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                out["error"] = f"the logger did not exit within {self.cfg['stop_wait_s']:g} s: killed"
                try:
                    self.proc.wait(1.0)         # not longer: a process stuck on a failing drive cannot die
                except subprocess.TimeoutExpired:
                    out["error"] += ", and still there 1 s later"
            summary = {}
            if self.proc.returncode is not None:
                try:
                    text = self.proc.stdout.read(1 << 16).decode("utf-8", "replace").strip().splitlines()
                    summary = json.loads(text[-1]) if text else {}
                except (OSError, ValueError):
                    summary = {}
                finally:
                    self.proc.stdout.close()
            if isinstance(summary, dict) and summary:
                out.update(summary)
                out["error"] = out.get("error") or self.error
            elif not out["error"]:
                out["error"] = f"no summary from the logger (exit status {self.proc.returncode})"
        if out.get("bytes") is None:
            try:
                out["bytes"] = os.path.getsize(self.path)
            except OSError:
                pass
        out["no_raw_data"] = not out.get("rawx_epochs")
        out["rawx_every"] = self.cfg["rawx_every"]     # the receiver setting this unit is meant to have
        return out
