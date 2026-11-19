"""Run the telemetry service: .venv/bin/python -B -m radar_runner.telemetry [--local-config FILE]"""
import sys

from ..common.service import run_service
from .service import Telemetry

if __name__ == "__main__":
    sys.exit(run_service(Telemetry))
