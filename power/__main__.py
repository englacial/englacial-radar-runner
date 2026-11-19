"""Command line of the power service.

    python3 -m power [run]        run the service (what the systemd unit does)
    python3 -m power status       print the status file and its age
    python3 -m power clear-guard  reset the boot-loop guard (then restart the service)
"""
import argparse
import json
import os
import signal
import sys
import time

from . import config
from .guard import Guard
from .hardware import GpioInput, Ina219
from .service import Notifier, NullActuator, Service, SystemdActuator, make_logger

STALE_S = 5.0


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="python3 -m power", description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", default="run", choices=("run", "status", "clear-guard"))
    ap.add_argument("--config", help=f"TOML config file (default: {config.DEFAULT_CONFIG_PATH}, optional)")
    ap.add_argument("--status-file", help="override paths.status_file")
    ap.add_argument("--state-dir", help="override paths.state_dir")
    ap.add_argument("--disable-file", help="override paths.disable_file")
    ap.add_argument("--monitor-only", action="store_true",
                    help="never act, whatever the config says (same as armed = false)")
    return ap.parse_args(argv)


def load_config(args):
    cfg, problems = config.load(args.config)
    for name in ("status_file", "state_dir", "disable_file"):
        value = getattr(args, name)
        if value:
            setattr(cfg, name, value)
    if args.monitor_only:
        cfg.armed = False
    return cfg, problems


def cmd_status(cfg):
    try:
        with open(cfg.status_file, "rb") as f:
            data = json.load(f)
        age = time.time() - float(data["time"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"cannot read {cfg.status_file}: {e}")
        return 2
    print(json.dumps(data, indent=1))
    stale = not -STALE_S <= age <= STALE_S
    print(f"status age: {age:.1f} s" + (" -- STALE: is the service running?" if stale else ""))
    return 1 if stale else 0


def cmd_clear_guard(cfg):
    guard = Guard(cfg.state_dir)
    for p in guard.load():
        print(p)
    print(f"before: early-shutdown count {guard.count}, passive {guard.passive}")
    guard.update({"count": 0, "passive": False})
    error = guard.save()
    if error:
        print(f"cannot write {guard.path}: {error}")
        return 1
    print(f"cleared {guard.path}. Restart the service to apply: systemctl restart radar-power")
    return 0


def cmd_run(cfg, problems):
    log = make_logger()
    if cfg.armed and os.geteuid() != 0:
        problems.append("armed = true needs root: running monitor-only")
        cfg.armed = False
    ina = Ina219(cfg.i2c_bus, cfg.ina219_address, cfg.shunt_ohms)
    gpio = GpioInput(cfg.gpio_line, cfg.gpio_chip_label, cfg.gpio_settle_s)
    actuator = SystemdActuator(log) if cfg.armed else NullActuator(log)
    service = Service(cfg, ina.read, gpio.read, actuator, Guard(cfg.state_dir),
                      notifier=Notifier(), log=log, config_problems=problems)

    def on_signal(signum, frame):
        service.stop = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        return service.run()
    finally:
        ina.close()
        gpio.close()


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg, problems = load_config(args)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "clear-guard":
        return cmd_clear_guard(cfg)
    return cmd_run(cfg, problems)


if __name__ == "__main__":
    sys.exit(main())
