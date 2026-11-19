"""Run the alerts service: .venv/bin/python -B -m radar_runner.alerts [--local-config FILE]"""
import sys

from ..common.service import run_service
from .service import Alerts

if __name__ == "__main__":
    sys.exit(run_service(Alerts))
