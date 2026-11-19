"""Run the radar manager: python -m radar_runner.manager [--local-config FILE] [--secrets FILE]"""
import sys

from radar_runner.common.service import run_service

from .service import Manager

if __name__ == "__main__":
    sys.exit(run_service(Manager))
