"""Run the alerts service: .venv/bin/python -B -m radar_runner.alerts [--local-config FILE]"""
import os
import sys

from ..common.service import run_service
from .service import Alerts

if __name__ == "__main__":
    status = run_service(Alerts)
    # The state is saved by now. A send cut off in a DNS lookup, or the summary's directory
    # listing of a drive that hangs, may still hold a thread: exit without waiting for it.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)
