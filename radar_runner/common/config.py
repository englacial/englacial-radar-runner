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
