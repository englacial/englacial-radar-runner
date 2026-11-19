"""Configuration of the radar manager services (not the power service: power/config.py).

Settings are merged from four layers, each overriding the one before:

    1. DEFAULTS below                       every setting, with its built-in value
    2. config/radar-runner.yaml             in this repository: common to all units
    3. /etc/radar-runner/local.yaml         this unit only (calibration, thresholds); optional
    4. /etc/radar-runner/secrets.yaml       tokens and credentials; optional, mode 0640 or 0600

Layers 3 and 4 are outside git. A layer may set any subset of the settings.

Nothing here raises. A file that is missing, unreadable or not valid YAML is
skipped; an unknown setting is ignored; a value of the wrong type is ignored
and the value from the layer before stays. Each of these adds a line to the
list of warnings that `load` returns, for the service to log and publish.
Warnings name settings and types, never values, so they are safe to show.

To add settings for a new service: add a section to DEFAULTS and the same
section, with comments, to config/radar-runner.yaml (a test checks that the
two agree). The type of each default is the type that the setting must have.
"""
import copy
import math
import os
import stat

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_FILE = os.path.join(REPO, "config", "radar-runner.yaml")
LOCAL_FILE = "/etc/radar-runner/local.yaml"
SECRETS_FILE = "/etc/radar-runner/secrets.yaml"

DEFAULTS = {
    "paths": {
        # Owned by user englacial. Their parent directories belong to root and
        # hold only the power service's files.
        "run_dir": "/run/radar-runner/user",             # status files: <run_dir>/<service>.json
        "state_dir": "/var/lib/radar-runner/user",       # small state that must survive a reboot
        "power_status_file": "/run/radar-runner/power.json",   # written by the power service; read only
    },
    "status": {
        "stale_after_s": 5.0,                    # a status file older than this is not trusted
    },
    "logging": {
        "level": "info",                         # debug, info, warning or error
    },
    # The telemetry service (radar_runner/telemetry/README.md).
    "telemetry": {
        "loop_interval_s": 0.25,                 # how often the sources are read
        "status_interval_s": 0.5,                # how often telemetry.json is rewritten
        "data_root": "/mnt/nvme/radar-runner",   # sensor logs go to <data_root>/telemetry/
        "data_mount": "/mnt/nvme",               # must be a mount point for data_root to be used ("-": no check)
        "fallback_dir": "/var/lib/radar-runner/user/telemetry-fallback",   # on the SD card
        "fallback_max_mb": 1000,                 # oldest fallback files are deleted above this
        "flush_interval_s": 1.0,                 # rows are handed to the kernel this often
        "fsync_interval_s": 30.0,                # each file is forced to the drive this often
        "ola": {
            "port": "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0",
            "baud": 115200,
            "log_interval_s": 0.0,               # 0: log every row (about 10 per second)
            "fresh_s": 2.0,                      # amplifier readings older than this are not fresh
            "boot_timeout_s": 25.0,              # no header this long after opening: drive the menus anyway
            "menu_timeout_s": 15.0,              # wait for each menu
            "silent_s": 20.0,                    # no valid row for this long: reopen the port (resets the OLA)
            "reopen_s": 5.0,                     # wait before reopening; doubles with every failure, up to:
            "reopen_max_s": 3600.0,
            "retry_s": 30.0,                     # wait before driving the menus again; doubles, up to:
            "retry_max_s": 3600.0,
            "stable_s": 300.0,                   # good rows for this long: the waits start short again
            # Calibration of the three ADC readings (bench values; see amp_values in telemetry/ola.py).
            "temp_divider": 2.0,
            "temp_mv_per_c": 10.0,
            "current_divider": 2.0,
            "current_mv_per_a": 500.0,
            "current_cal": 1.5,
            "voltage_ratio": 11.0,
        },
        "pi": {
            "interval_s": 2.0,
        },
        "ups": {
            "interval_s": 1.0,
            "stale_after_s": 3.0,                # the power service rewrites its file about once a second
        },
        "gps": {
            "host": "127.0.0.1",
            "port": 2947,
            "interval_s": 1.0,
            "stale_after_s": 5.0,                # no new fix time for this long: stale
            "answer_timeout_s": 10.0,            # no answer from gpsd for this long: reconnect
            "reconnect_s": 5.0,
            "no_data_error_s": 30.0,             # receiver present but no fix time for this long: error
            # Run a little after every OLA start ("" = never): see setup/gps_receiver.py
            "receiver_fix_command": "/usr/bin/python3 -B /home/englacial/englacial-radar-runner/setup/gps_receiver.py --apply --ram",
            "receiver_fix_delay_s": 15.0,
        },
    },
    # The safety watcher (radar_runner/watcher/README.md, which says where each number comes from).
    "watcher": {
        "loop_interval_s": 0.5,                  # how often everything is looked at and watcher.json rewritten
        # A process with this executable is a radar. "manager": the binary that the radar manager runs
        # (manager.orca: dir/radar_cwd/radar_command), so that the two cannot diverge; or a full path.
        "radar_exe": "manager",
        "telemetry_status_file": "telemetry.json",      # a name in paths.run_dir, or a full path
        "override_file": "override-no-amp-telemetry",   # these three: a name in paths.state_dir, or a full path
        "events_file": "watcher-events.jsonl",
        "events_max_mb": 5.0,                    # then the file is renamed to .1 and a new one started
        "telemetry_lost_s": 3.0,                 # no usable amplifier reading for this long: not ok
        "require_case_temp": True,               # a reading without the case temperature is not usable
        "trip_samples": 3,                       # a limit trips when this many ...
        "trip_window": 5,                        # ... of the last this many readings are beyond it ...
        "trip_span_s": 1.0,                      # ... and those span at least this long
        "clear_hold_s": 30.0,                    # a trip clears after this long on the good side of the recover level
        "temp_warn_c": 50.0,                     # amplifier or case temperature: warning
        "temp_trip_c": 55.0,                     # ... stop
        "temp_clear_c": 50.0,                    # ... a stop clears below this
        "voltage_min_v": 26.0,                   # 28 V rail, while the amplifier is powered: stop outside these
        "voltage_max_v": 30.5,
        "voltage_recover_margin_v": 0.5,         # a stop clears this far inside the two limits
        "unpowered_below_v": 20.0,               # below this the amplifier is unpowered: no trip, but not ok ...
        "allow_unpowered": False,                # ... unless this is true (bench work with the 28 V off)
        "enabled_current_a": 0.5,                # above this the amplifier is enabled
        "idle_enabled_s": 5.0,                   # enabled this long with no radar process: run the reset tool
        "reset": {
            "command": "/home/englacial/englacial-radar-runner/tools/sdr_gpio_safe/amp_safe",
            "timeout_s": 30.0,                   # amp_safe --timeout
            "kill_margin_s": 5.0,                # amp_safe is given up on this long after its own timeout
            "state_file": "sdr-state",           # amp_safe --state-file
            "retry_prompt_s": 2.0,               # wait before the next run after ASSUME_ENABLED (exit 3)
            "retry_s": 5.0,                      # ... after any other result, while the amplifier stays enabled
            "stuck_after_attempts": 3,           # still enabled after this many runs: stuck (urgent)
            "stuck_retry_s": 60.0,               # while stuck: one run this often
            "blocked_after_s": 30.0,             # the tool cannot run (BUSY, ...) for this long: urgent
            "blocked_retry_s": 15.0,             # ... and then one try this often
            "blind_delay_s": 20.0,               # no telemetry and no radar this long, pin not known safe: run it
            "blind_retry_s": 300.0,              # ... and again this often while that lasts
        },
        "stop_grace_s": 5.0,                     # not ok for this long with a radar process alive: SIGINT
        "radar_setup_s": 30.0,                   # a radar process younger than this is never signalled
        "radar_exit_s": 5.0,                     # still alive this long after SIGINT: SIGKILL
        "kill_retry_s": 5.0,                     # still alive: SIGKILL again this often
    },
}

_TYPE_NAMES = {bool: "true or false", int: "a whole number", float: "a number", str: "text",
               list: "a list", dict: "a section"}


def _type_name(value):
    if value is None:
        return "nothing"
    if type(value) is float and not math.isfinite(value):
        return "an infinite or undefined number"
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def _convert(default, value):
    """Return `value` as the type of `default`, or raise ValueError."""
    kind = type(default)
    if kind is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError
        return float(value)
    if type(value) is not kind or (kind is str and not value):
        raise ValueError
    return value


def _merge(base, layer, where, prefix, warnings):
    """Merge the mapping `layer` into `base`, which has the shape of the defaults."""
    for key, value in layer.items():
        name = f"{prefix}{key}"
        if key not in base:
            warnings.append(f"{where}: unknown setting {name}: ignored")
        elif isinstance(base[key], dict):
            if isinstance(value, dict):
                _merge(base[key], value, where, name + ".", warnings)
            elif value is not None:     # "section:" with nothing under it is an empty section
                warnings.append(f"{where}: {name} must be a section, not {_type_name(value)}: ignored")
        else:
            try:
                base[key] = _convert(base[key], value)
            except ValueError:
                warnings.append(f"{where}: {name} must be {_type_name(base[key])}, "
                                f"not {_type_name(value)}: ignored")


def _read_layer(path, optional, secret, warnings):
    """Return the mapping in a YAML file, or {} with a warning if it cannot be used."""
    try:
        with open(path, "rb") as f:
            if secret and stat.S_IMODE(os.fstat(f.fileno()).st_mode) & 0o007:
                warnings.append(f"{path}: readable by every user: set its mode to 0640 or 0600")
            data = yaml.safe_load(f)
    except FileNotFoundError:
        if not optional:
            warnings.append(f"{path}: not found: built-in defaults used")
        return {}
    except OSError as e:
        warnings.append(f"{path}: cannot read ({e.strerror}): skipped")
        return {}
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as e:
        problem = getattr(e, "problem", None) or type(e).__name__
        mark = getattr(e, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark is not None else ""
        warnings.append(f"{path}: not valid YAML ({problem}{line}): skipped")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        warnings.append(f"{path}: the top level must be a mapping of sections: skipped")
        return {}
    return data


def load(repo_file=REPO_FILE, local_file=LOCAL_FILE, secrets_file=SECRETS_FILE, defaults=None):
    """Return (config, warnings): a nested dict shaped like DEFAULTS, and a list of text lines."""
    config = copy.deepcopy(DEFAULTS if defaults is None else defaults)
    warnings = []
    for path, optional, secret in ((repo_file, False, False), (local_file, True, False),
                                   (secrets_file, True, True)):
        if path is None:         # None leaves a layer out
            continue
        try:
            _merge(config, _read_layer(path, optional, secret, warnings), path, "", warnings)
        except Exception as e:   # a config problem must never stop a service
            warnings.append(f"{path}: internal error ({type(e).__name__}): skipped")
    return config, warnings


if __name__ == "__main__":
    # Show what the services will use. The secrets file is left out of the
    # listing; problems in it are still reported.
    shown, _ = load(secrets_file=None)
    print(yaml.safe_dump(shown, sort_keys=False), end="")
    for line in load()[1]:
        print("WARNING:", line)
