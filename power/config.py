"""Configuration for the power service.

Every setting has a built-in default, so the service runs with no config file.
A bad or missing file never stops the service: the affected settings keep their
defaults (which are monitor-only) and the problem is reported in the log and in
the status file.
"""
import tomllib
from dataclasses import dataclass, fields

DEFAULT_CONFIG_PATH = "/etc/radar-runner/power.toml"


@dataclass
class Config:
    # --- top level ---
    armed: bool = False                     # False = monitor-only: decide and log, never act
    single_signal_action: str = "shutdown"  # one signal says loss, the other is unreadable/faulty:
                                            # "shutdown", or "quiesce" (radar stays stopped, no power-off)
    # --- [paths] ---
    status_file: str = "/run/radar-runner/power.json"
    state_dir: str = "/var/lib/radar-runner"
    disable_file: str = "/etc/radar-runner/power-disarm"
    # --- [sampling] ---
    sample_interval_s: float = 0.2
    publish_interval_s: float = 0.5
    # --- [signals] ---
    i2c_bus: int = 1
    ina219_address: int = 0x40
    shunt_ohms: float = 0.010
    loss_current_a: float = -0.3       # capacitor current below this = discharging = main power lost
    gpio_line: int = 26                # high = external power present
    gpio_chip_label: str = "pinctrl-rp1"
    gpio_settle_s: float = 0.1
    loss_confirm_samples: int = 2      # consecutive loss samples to confirm a loss
    restore_confirm_s: float = 1.0     # continuous "present" needed to confirm a restore
    unreadable_after_s: float = 2.0    # no valid reading for this long -> signal is "unreadable"
    faulty_after_s: float = 180.0      # loss reported for longer than the capacitor can last -> "faulty"
    faulty_clear_s: float = 60.0       # continuous "present" needed to clear "faulty"
    disagree_warn_s: float = 5.0       # contradiction must last this long before it is a warning
    # --- [shutdown] ---
    ride_through_s: float = 8.0        # the signals must agree on a loss for this long, unbroken
    low_voltage_v: float = 4.5         # capacitor below this during a loss waives the ride-through
    sync_timeout_s: float = 5.0
    last_resort_after_s: float = 10.0  # still running this long after the power command: last resort
    # --- [guard] ---
    early_boot_s: float = 300.0
    max_early_shutdowns: int = 3
    passive_rearm_s: float = 1800.0
    # --- [hooks] --- (argument lists, run without a shell; empty = no hook)
    stop_radar: tuple = ()
    stop_radar_timeout_s: float = 5.0
    amp_safe: tuple = ()
    amp_safe_timeout_s: float = 5.0


# TOML table -> settings it may contain ("" is the top level).
SECTIONS = {
    "": ("armed", "single_signal_action"),
    "paths": ("status_file", "state_dir", "disable_file"),
    "sampling": ("sample_interval_s", "publish_interval_s"),
    "signals": ("i2c_bus", "ina219_address", "shunt_ohms", "loss_current_a", "gpio_line",
                "gpio_chip_label", "gpio_settle_s", "loss_confirm_samples", "restore_confirm_s",
                "unreadable_after_s", "faulty_after_s", "faulty_clear_s", "disagree_warn_s"),
    "shutdown": ("ride_through_s", "low_voltage_v", "sync_timeout_s", "last_resort_after_s"),
    "guard": ("early_boot_s", "max_early_shutdowns", "passive_rearm_s"),
    "hooks": ("stop_radar", "stop_radar_timeout_s", "amp_safe", "amp_safe_timeout_s"),
}

# Allowed range (inclusive) for numeric settings. Values outside are rejected.
LIMITS = {
    "sample_interval_s": (0.05, 2.0),
    "publish_interval_s": (0.1, 10.0),
    "i2c_bus": (0, 64),
    "ina219_address": (0x03, 0x77),
    "shunt_ohms": (0.0001, 1.0),
    "loss_current_a": (-10.0, -0.01),
    "gpio_line": (0, 53),
    "gpio_settle_s": (0.0, 2.0),
    "loss_confirm_samples": (2, 50),
    "restore_confirm_s": (0.2, 30.0),
    "unreadable_after_s": (0.2, 60.0),
    "faulty_after_s": (30.0, 86400.0),
    "faulty_clear_s": (1.0, 86400.0),
    "disagree_warn_s": (0.0, 3600.0),
    "ride_through_s": (0.0, 60.0),
    "low_voltage_v": (0.0, 6.0),
    "sync_timeout_s": (0.5, 60.0),
    "last_resort_after_s": (5.0, 300.0),
    "early_boot_s": (0.0, 86400.0),
    "max_early_shutdowns": (1, 100),
    "passive_rearm_s": (1.0, 7 * 86400.0),
    "stop_radar_timeout_s": (0.1, 30.0),
    "amp_safe_timeout_s": (0.1, 30.0),
}

CHOICES = {"single_signal_action": ("shutdown", "quiesce")}


def _convert(name, default, value):
    """Return the validated value for setting `name`, or raise ValueError."""
    if isinstance(default, bool):
        if type(value) is not bool:
            raise ValueError("must be true or false")
    elif isinstance(default, int):
        if type(value) is not int:
            raise ValueError("must be an integer")
    elif isinstance(default, float):
        if type(value) not in (int, float):
            raise ValueError("must be a number")
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
    elif isinstance(default, str):
        if type(value) is not str or not value:
            raise ValueError("must be a non-empty string")
    elif isinstance(default, tuple):
        if type(value) is not list or not all(type(v) is str and v for v in value):
            raise ValueError('must be a list of strings, e.g. ["/usr/bin/cmd", "arg"]')
        value = tuple(value)
    if name in LIMITS:
        lo, hi = LIMITS[name]
        if not lo <= value <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
    if name in CHOICES and value not in CHOICES[name]:
        raise ValueError("must be one of " + ", ".join(CHOICES[name]))
    return value


def from_dict(data):
    """Build a Config from parsed TOML. Returns (config, problems)."""
    cfg = Config()
    problems = []
    defaults = {f.name: f.default for f in fields(Config)}
    for key, value in data.items():
        if key in SECTIONS and key != "":
            if not isinstance(value, dict):
                problems.append(f"[{key}] is not a table: ignored")
                continue
            items = [(key, k, v) for k, v in value.items()]
        else:
            items = [("", key, value)]
        for section, name, v in items:
            where = f"{section}.{name}" if section else name
            if name not in SECTIONS.get(section, ()):
                problems.append(f"unknown setting {where}: ignored")
                continue
            try:
                setattr(cfg, name, _convert(name, defaults[name], v))
            except ValueError as e:
                problems.append(f"{where} = {v!r} {e}: using default {defaults[name]!r}")
    if cfg.faulty_after_s < cfg.ride_through_s + 20.0:
        # Otherwise a signal could be declared faulty before a real loss is acted on.
        problems.append("signals.faulty_after_s must exceed shutdown.ride_through_s by 20 s or more: "
                        "using defaults for both")
        cfg.faulty_after_s = defaults["faulty_after_s"]
        cfg.ride_through_s = defaults["ride_through_s"]
    return cfg, problems


def load(path=None):
    """Load the config. Returns (config, problems). Never raises.

    path=None means the default path, which is optional. A path given
    explicitly that cannot be read is reported as a problem.
    """
    explicit = path is not None
    path = path or DEFAULT_CONFIG_PATH
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if explicit:
            return Config(), [f"config file {path} not found: using defaults (monitor-only)"]
        return Config(), []
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return Config(), [f"config file {path} unreadable ({e}): using defaults (monitor-only)"]
    cfg, problems = from_dict(data)
    return cfg, [f"config file {path}: {p}" for p in problems]
