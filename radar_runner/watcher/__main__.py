"""Run the safety watcher: .venv/bin/python -B -m radar_runner.watcher [--local-config FILE]"""
import sys

from ..common.service import run_service
from .service import Watcher

if __name__ == "__main__":
    sys.exit(run_service(Watcher))
