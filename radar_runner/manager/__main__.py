"""Run the radar manager: python -m radar_runner.manager [--local-config FILE] [--secrets FILE]"""
import argparse
import signal
import sys

from radar_runner.common import config as common_config
from radar_runner.common.log import setup_logging

from . import config as manager_config
from .service import Manager


def main(argv=None):
    # As radar_runner.common.service.run_service, with the manager's settings
    # layered in (MERGE NOTE in config.py: afterwards run_service(Manager) does).
    parser = argparse.ArgumentParser(description=Manager.__doc__)
    parser.add_argument("--local-config", default=common_config.LOCAL_FILE,
                        help="per-unit config file (default: %(default)s)")
    parser.add_argument("--secrets", default=common_config.SECRETS_FILE,
                        help="secrets file (default: %(default)s)")
    args = parser.parse_args(argv)
    config, warnings = manager_config.load(local_file=args.local_config, secrets_file=args.secrets)
    problem = setup_logging(config["logging"]["level"])
    if problem:
        warnings.append(problem)
    service = Manager(config, config_warnings=warnings)
    signal.signal(signal.SIGTERM, service.request_stop)
    signal.signal(signal.SIGINT, service.request_stop)
    return service.run()


if __name__ == "__main__":
    sys.exit(main())
