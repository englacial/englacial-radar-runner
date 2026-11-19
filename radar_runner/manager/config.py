"""Settings of the radar manager: the `manager` section of the shared config.

MERGE NOTE: MANAGER_DEFAULTS belongs in `radar_runner/common/config.py` DEFAULTS
as the section "manager", and docs/merge/manager-config-section.yaml in
`config/radar-runner.yaml`. Until then this module layers it on top of the
shared defaults, so /etc/radar-runner/local.yaml can already set `manager:`
values. The rules of the shared config apply: the type of each default is the
type the setting must have; an empty text cannot be set from a file.

Values marked PER UNIT depend on hardware settings of one unit and belong in
/etc/radar-runner/local.yaml.
"""
import copy

from radar_runner.common import config as common_config

MANAGER_DEFAULTS = {
    "tick_s": 0.1,                  # main loop period: reaction time to the button and to radarctl
    "gate_interval_s": 0.5,         # how often the gates are evaluated
    "status_interval_s": 1.0,       # the status file is rewritten at least this often
    "radar_config": "/etc/radar-runner/radar.yaml",    # the requested ORCA config (PER UNIT)

    "triggers": {
        "button": True,
        "command": True,            # radarctl start/toggle; stop and clear-fault always work
        "gps": False,               # the motion trigger (stage 6)
    },

    "run": {
        "manual_timeout_s": 50400.0,            # a manually started run is stopped after this (14 h); 0 = never
        "retry_delays_s": [10.0, 30.0, 90.0],   # wait before each retry of a failed start; then fault
        "max_resumes": 5,                       # automatic resumes after a gate stopped the run; then fault
        "resume_holdoff_s": 10.0,               # the gates must have been good for this long before a resume
        "stable_run_s": 300.0,                  # running this long clears the retry and resume counts
        "max_restarts_per_hour": 12,            # automatic restarts (retries and resumes) in any hour; then fault
    },

    "gates": {
        # Status file of the telemetry service. "" = <paths.run_dir>/telemetry.json
        "telemetry": {
            "status_file": "",
            "max_age_s": 3.0,
            "require_true": ["amp.fresh"],      # dotted paths that must be true
            "require_false": [],                # dotted paths that must be false
            "reason_field": "amp.reason",       # shown when a requirement fails
        },
        # Status file of the safety watcher (stage 3; format not final). "" = <paths.run_dir>/watcher.json
        "watcher": {
            "status_file": "",
            "max_age_s": 5.0,
            "require_true": ["ok"],
            "require_false": ["tripped"],
            "reason_field": "reason",
        },
        "power": {
            "max_age_s": 5.0,                   # of paths.power_status_file
        },
        "data": {
            "mount_point": "/mnt/nvme",         # must be a mounted file system other than the SD card
            "root": "/mnt/nvme/radar-runner/collections",
            "start_min_free_gb": 20.0,          # do not start below this
            "stop_min_free_gb": 5.0,            # stop a running collection below this
        },
        "sdr": {
            "sysfs_dir": "/sys/bus/usb/devices",
            "usb_vendor": "2500",                               # Ettus Research
            "usb_products": ["0020", "0021", "0022", "0023"],   # B200, B200mini, B205mini, B206mini
            "min_speed_mbps": 5000,                             # USB 3
            # Until the first UHD start after power-up the SDR runs its boot
            # loader, which is a USB 2 device with this product name. Its link
            # speed is then unknown and the gate passes; the runner checks the
            # radar's own "Operating over USB 3" line.
            "bootloader_product": "WestBridge",
        },
    },

    # Fixed limits on the radar config. Checked before every start.
    "limits": {
        "amp_pin": 2,                       # GPIO.pwr_amp_pin as wired on the bench (DB15 pin 2 = GPIO_0)
        "amp_active_low": True,
        "gpio_bank": "FP0",
        # PER UNIT, CONSERVATIVE DEFAULT. The cap depends on the VVA (J8)
        # setting, which is not decided. 43 keeps the output near 1.6 W even at
        # full amplifier gain (VVA 0 V: tx_gain 41 = 1.0 W, 52 = 10.9 W, measured
        # 2026-11-05). At VVA 5 V the bench limit is 72 (amplifier input 3 dB
        # under its +10 dBm rating at 300 MHz); never above 76.
        "max_tx_gain": 43.0,
        # PER UNIT. CHIRP.tx_duration / CHIRP.pulse_rep_int: the amplifier is
        # enabled for tx_duration - tx_lead in every pulse (measured). 0.15 is
        # the bench fixture's limit; the 10 W soak ran at 0.14.
        "max_enable_duty": 0.15,
        "min_freq_hz": 225.0e6,             # the chirp band must lie inside these (antennas: 225-400 MHz)
        "max_freq_hz": 400.0e6,
        "clk_refs": ["internal", "external"],   # "gpsdo" makes ORCA open and reconfigure the GPS receiver
    },

    "orca": {
        "dir": "/home/englacial/orca",
        "env": [],                          # extra environment for the commands below, as "NAME=value"
        "chirp_command": ["/home/englacial/miniforge3/envs/orca/bin/python", "generate_chirp.py", "{config}"],
        "chirp_cwd": "preprocessing",       # relative to dir
        "chirp_timeout_s": 120.0,
        "build_commands": [["cmake", ".."], ["make", "radar"]],     # only the radar target, not ORCA's tests
        "build_cwd": "sdr/build",
        "build_timeout_s": 900.0,
        "radar_command": ["./radar", "{config_rel}"],   # {config_rel}: the config, relative to dir
        "radar_cwd": "sdr/build",           # ORCA resolves its paths from two levels below dir
        # Put in front of the radar command: the radar is killed if the manager dies.
        "launch_prefix": ["/usr/bin/setpriv", "--pdeathsig", "KILL"],
        "start_line": "[START]",            # the radar's own "recording has begun" line
        # From launch to that line. The first start after power-up loads the
        # SDR's firmware and FPGA first: 14 s measured, then about 5 s of set-up.
        "start_timeout_s": 60.0,
        "stop_timeout_s": 3.0,              # from SIGINT to exit; then SIGKILL
        # An urgent stop (power loss, the manager's own exit): the radar has no
        # SIGINT handler before its start line, so the stop waits for that line
        # for at most urgent_grace_s; the radar process is gone, killed if need
        # be, urgent_stop_budget_s after the stop was asked for.
        "urgent_grace_s": 1.0,
        "urgent_stop_budget_s": 3.0,
        "require_usb3": True,               # after the start line the SDR must be at min_speed_mbps (sysfs)
    },

    "data": {
        "chunk_mb": 128.0,                  # the radar starts a new data file at about this size ...
        "chunk_max_s": 300.0,               # ... or after this long, whichever is first
        "log_part_mb": 32.0,                # the radar's output log is split into parts of this size; all is kept
    },

    # Making the amplifier safe (ampsafe.py): the tool that drives the SDR's
    # amplifier pin to the shutdown level. It runs only when the pin is not
    # known to be safe. A command that is missing or not executable means the
    # radar is never started.
    "amp_safe": {
        # The manager adds "--state-file <state_dir>/sdr-state". --timeout is
        # mandatory for the tool: 30 s covers a first contact after power-up
        # (16.7 s measured: firmware 5.9 s, FPGA 8.2 s).
        "command": ["/home/englacial/englacial-radar-runner/tools/sdr_gpio_safe/amp_safe", "--timeout", "30"],
        "timeout_s": 40.0,                  # the manager kills the tool after this; must be longer than --timeout
        "assume_enabled_retries": 2,        # immediate retries when the tool says ASSUME_ENABLED (exit 3)
        "unavailable_retries": 3,           # after a radar exit: retries of NOT_FOUND, BUSY and TIMEOUT ...
        "unavailable_delay_s": 5.0,         # ... this far apart (the SDR may be coming back after a kill)
        "retry_s": 60.0,                    # with no radar and the state unknown: try the tool this often
        "idle_enabled_s": 5.0,              # enabled current this long with no radar process: run the tool
        # The reading in the telemetry status file that decides, when it is fresh.
        "current_field": "amp.current_a",
        "voltage_field": "amp.voltage_v",
        "sample_uptime_field": "amp.sample_uptime_s",
        "enabled_current_a": 0.5,           # at or above: enabled (shutdown 0.16 A, enabled about 2.5 A)
        "min_voltage_v": 20.0,              # below: the 28 V rail is off and the current says nothing
        # A shutdown-current reading counts only if the Pi read it this long
        # after the last radar or tool run ended: 1 s, plus 1 s because a row is
        # read later than the ADC measured it.
        "confirm_settle_s": 2.0,
        "confirm_timeout_s": 5.0,           # after a radar exit: wait this long for such a reading
    },

    "panel": {
        "enabled": True,
        "button_gpio": 4,
        "green_gpio": 17,
        "yellow_gpio": 18,
        "breathing": "pwm",                 # "pwm", or "blink" if PWM does not work through the LED's regulator
        "pwm_hz": 200,                      # PWM frequency of the READY LED
        "kernel_debounce_s": 0.02,          # edges closer together than this never reach the manager
        "press_min_s": 0.1,                 # a press is the button held between these two times ...
        "press_max_s": 5.0,
        "press_gap_s": 1.0,                 # ... and at least this long after the press before
        "fast_blink_s": 0.15,               # on time = off time
        "slow_blink_s": 0.6,
        "breath_s": 1.2,                    # fade in = fade out
    },

    "events": {
        "file": "",                         # "" = <paths.state_dir>/manager-events.jsonl
        "max_mb": 5.0,                      # then the file is renamed to .1 and a new one started
    },
}


def defaults():
    """The shared defaults with the manager section added."""
    merged = copy.deepcopy(common_config.DEFAULTS)
    merged.setdefault("manager", copy.deepcopy(MANAGER_DEFAULTS))
    return merged


def load(repo_file=common_config.REPO_FILE, local_file=common_config.LOCAL_FILE,
         secrets_file=common_config.SECRETS_FILE):
    """Return (config, warnings) like the shared loader, including the `manager` section."""
    return common_config.load(repo_file, local_file, secrets_file, defaults=defaults())
