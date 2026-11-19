"""Example service: publishes a counter once a second. A starting point to copy; not installed.

    cd ~/englacial-radar-runner
    .venv/bin/python -B -m radar_runner.example --local-config my.yaml

where my.yaml points `paths.run_dir` at a directory you can write to.
"""
import sys

from radar_runner.common.service import Service, run_service


class Example(Service):
    """Example service: publishes a counter once a second."""
    name = "example"
    interval_s = 1.0

    def setup(self):
        self.count = 0

    def tick(self):
        self.count += 1
        return {"state": "running", "count": self.count}


if __name__ == "__main__":
    sys.exit(run_service(Example))
