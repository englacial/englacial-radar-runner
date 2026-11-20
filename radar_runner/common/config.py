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
            # Run a little after every OLA start and without a position ("" = never): see setup/gps_receiver.py
            "receiver_fix_command": "/usr/bin/python3 -B /home/englacial/englacial-radar-runner/setup/gps_receiver.py --apply --ram",
            "receiver_fix_delay_s": 15.0,
            "receiver_fix_timeout_s": 180.0,     # the command is stopped after this (it limits itself to about 100 s); at least 30
            # gpsd has a receiver but no 2D/3D fix for this long: a warning, and the status says why
            # (sources.gps.no_position). "gpsd_unidentified" and "no_position_messages" are errors;
            # "no_fix" (no satellites) is not. 60 s: a cold start under open sky takes about 25 s.
            "no_position_warn_s": 60.0,
            # Then the receiver command is run again: after this long without a position, then after
            # twice the previous wait each time, up to the maximum (2, 6, 14, 30 min, then every
            # 30 min). Both at least 30.
            "no_position_retry_s": 120.0,
            "no_position_retry_max_s": 1800.0,
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
        # A radar process younger than this is never signalled. The manager waits for ORCA's start line
        # for manager.orca.start_timeout_s (60 s) and then needs stop_timeout_s (3 s): this is longer.
        "radar_setup_s": 65.0,
        "radar_exit_s": 5.0,                     # still alive this long after SIGINT: SIGKILL
        "kill_retry_s": 5.0,                     # still alive: SIGKILL again this often
    },
    # ---------------------------------------------------------------------------
    # The radar manager (radar_runner/manager/README.md). Values marked PER UNIT
    # depend on the hardware of one unit and belong in /etc/radar-runner/local.yaml.
    "manager": {
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
            "max_starts_per_hour": 40,              # starts of any kind in any hour, fresh runs included; then fault
            "min_start_gap_s": 5.0,                 # no start sooner than this after the last run ended
        },

        "gates": {
            # Status file of the telemetry service. "-" = <paths.run_dir>/telemetry.json
            "telemetry": {
                "status_file": "-",
                "max_age_s": 3.0,
                "require_true": ["amp.fresh"],      # dotted paths that must be true
                "require_false": [],                # dotted paths that must be false
                "reason_field": "amp.reason",       # shown when a requirement fails
            },
            # Status file of the safety watcher: at least ok, tripped and reason. "-" = <paths.run_dir>/watcher.json
            "watcher": {
                "status_file": "-",
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
                # speed is then unknown and the gate passes; the runner reads the
                # speed from sysfs again once the radar has printed its start line.
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
            # Extra environment for the commands below, as "NAME=value". ORCA's build reads CONDA_PREFIX.
            "env": ["CONDA_PREFIX=/home/englacial/miniforge3/envs/orca"],
            "chirp_command": ["/home/englacial/miniforge3/envs/orca/bin/python", "generate_chirp.py", "{config}"],
            "chirp_cwd": "preprocessing",       # relative to dir
            "chirp_timeout_s": 120.0,
            # Only the radar target, not ORCA's tests. The UHD is named in full, so that a new build
            # directory and an old one give the same binary: the build that leaves the SDR's
            # front-panel pins as inputs at initialisation (setup/pins.toml: uhd_prefix), with the
            # environment that holds Boost and yaml-cpp. The two -U drop the library and headers
            # that an earlier configure found: without them an old build directory keeps its UHD.
            "build_commands": [["cmake", "..", "-UUHD_LIBRARIES", "-UUHD_INCLUDE_DIRS",
                                "-DCMAKE_PREFIX_PATH=/home/englacial/uhd-patched-gpio;/home/englacial/miniforge3/envs/orca",
                                "-DUHD_DIR=/home/englacial/uhd-patched-gpio/lib/cmake/uhd",
                                "-DCMAKE_BUILD_RPATH=/home/englacial/uhd-patched-gpio/lib"],
                               ["make", "radar"]],
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
            # The file that the tool's wrapper locks while it runs. "-" = the first word of `command`
            # (the wrapper locks itself). The manager never records `safe` while the lock is held.
            "lock_file": "-",
            "timeout_s": 40.0,                  # the manager kills the tool after this; must be longer than --timeout
            "assume_enabled_retries": 2,        # immediate retries when the tool says ASSUME_ENABLED (exit 3)
            "unavailable_retries": 3,           # after a radar exit: retries of NOT_FOUND, BUSY and TIMEOUT ...
            "unavailable_delay_s": 5.0,         # ... this far apart (the SDR may be coming back after a kill)
                "retry_s": 60.0,                    # with no radar and the state unknown: try the tool this often
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
            # ... and only if the telemetry service's loop was not stalled when it read the row.
            "loop_gap_field": "loop.last_gap_s",
            "max_loop_gap_s": 1.0,
        },

        # The turn-on check (radar_runner/manager/README.md, "Turn-on check"): a short low-power run
        # before every start. Levels in dB relative to the chirp, as tools/turn_on_check/measure.py prints them.
        "turn_on_check": {
            "enabled": False,
            "blocking": False,                  # a failed check keeps the radar off; needs expected.verified
            "max_tx_gain": 40.0,                # the low-power run: RF0.tx_gain capped at this (70 is about 9 W)
            "pulses": 15000,                    # error-free pulses (rounded up to whole records); 3 s at 5 kHz
            "analysis_command": ["/home/englacial/miniforge3/envs/orca/bin/python", "-I",
                                 "/home/englacial/englacial-radar-runner/tools/turn_on_check/measure.py"],
            "analysis_timeout_s": 30.0,
            # Before the run (the telemetry status): the idle current; the 28 V rail and both temperatures
            # against the safety watcher's own limits (watcher.voltage_min_v, voltage_max_v, temp_trip_c).
            "idle_current_min_a": 0.10,         # 0.144-0.156 A measured (2026-11-20)
            "idle_current_max_a": 0.20,
            # During the pulses: the highest reading at least this far above the idle reading, none above the
            # maximum. Fewer readings than this (one every 0.5 s): not judged. 0.25-0.31 A at 6.5 % enable duty.
            "current_rise_min_a": 0.05,
            "current_max_a": 0.6,
            "min_current_readings": 2,
            "clip_fs": 0.9,                     # raw maximum at or above this (of full scale): clipping
            # Peaks closer than this form one: the level of the combined peak is judged.
            "min_separation_samples": 4.0,
            # The expected peak, from a verified start (measure.py prints these). Until `verified` is true the
            # check only records.
            "expected": {
                "verified": False,
                "leakage_sample": 0.0,          # where the leakage peak inside the SDR is
                "delay_samples": 0.0,           # the direct peak this far after it (0: one combined peak)
                "tolerance_samples": 1.5,
                "level_tx_gain": 0.0,           # the minimum level is for this tx_gain (scaled 1 dB per step)
                "min_level_db": 0.0,
            },
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
            "file": "-",                        # "-" = <paths.state_dir>/manager-events.jsonl
            "max_mb": 5.0,                      # then the file is renamed to .1 and a new one started
        },
    },
    # ---------------------------------------------------------------------------
    # Alerts to ntfy (radar_runner/alerts/README.md). Read only: nothing here
    # changes what another service does.
    "alerts": {
        "loop_interval_s": 1.0,
        "unit_name": "-",                       # shown in every alert; "-" = the host name
        # These three: a name in paths.state_dir, or a full path.
        "queue_dir": "alerts-queue",            # one file per alert waiting to be sent
        "inbox_dir": "alerts-inbox",            # test alerts from alertctl
        "state_file": "alerts-state.json",
        "queue_max": 200,                       # then the lowest priority, oldest first, is dropped
        "title_max_chars": 120,
        "message_max_chars": 1500,
        # server, topic and token belong in /etc/radar-runner/secrets.yaml. "-" = not set: alerts are queued only.
        "ntfy": {
            "server": "-",                      # https://... (plain http only to this host itself)
            "topic": "-",
            "token": "-",
            "timeout_s": 15.0,                  # the whole request, connection included
            "retry_s": 10.0,                    # wait after a failed send; doubles with every failure, up to:
            "retry_max_s": 300.0,
            "max_per_round": 5,                 # sends per loop round at most
        },
        # Storm limit, per kind of alert: at most `burst` in any `window_s`; the
        # rest are counted and reported. All kinds together, urgent ones excepted:
        # at most `max_per_hour`.
        "storm": {
            "burst": 3,
            "window_s": 3600.0,
            "max_per_hour": 30,
        },
        "summary_after_s": 180.0,               # the boot summary is sent at this uptime (or at the start, if later)
        "summary_settle_s": 30.0,               # ... once the other services' status has been usable this long
        "summary_wait_max_s": 600.0,            # ... but at most this long after this service's start
        # How long each condition must last before it is an alert.
        "conditions": {
            "status_stale_s": 120.0,            # the status file of telemetry, watcher, manager or power old or missing
            "watcher_not_ok_s": 300.0,          # the safety watcher says the radar may not run
            "not_ready_s": 600.0,               # the radar manager is not ready
            "gps_s": 600.0,                     # no GPS fix, or no receiver
            "logging_fallback_s": 300.0,        # the sensor logs are not on the data drive
            "pi_hot_s": 60.0,                   # the Pi throttling, or its SoC at or above pi_temp_c
            "pi_temp_c": 80.0,
            "power_warning_s": 30.0,            # a warning of the power service
            "clear_s": 60.0,                    # gone for this long: "cleared"
        },
        "error_rate_warn": 0.05,                # a run with ORCA's error count above this fraction of its pulses
        "disk": {
            "free_gb_levels": [200.0, 100.0, 50.0, 25.0],   # data drive: one alert at each; the last one is high priority
            "rearm_fraction": 0.1,              # a level alerts again once the free space was 10 % above it
            "sd_free_gb_min": 5.0,              # the SD card
        },
        "events": {
            "watcher_file": "-",                # "-" = watcher.events_file in paths.state_dir
            "manager_file": "-",                # "-" = manager.events.file
            "max_read_kb": 256,                 # read from each file per round at most
        },
    },
}

# Sections that a later stage will define. A file may already contain them
# (offload: stage 9); they are passed over without a warning and not used.
RESERVED = ("offload",)

# Text settings for which "" is a value of its own (otherwise empty text is refused).
_EMPTY_ALLOWED = {"telemetry.gps.receiver_fix_command"}         # "": never run it
# Lower limits: a smaller value is raised to the limit, with a warning.
_MINIMUM = {
    "telemetry.gps.no_position_retry_s": 30.0,          # 0 would run the receiver command back to back
    "telemetry.gps.no_position_retry_max_s": 30.0,
    "telemetry.gps.receiver_fix_timeout_s": 30.0,       # shorter would stop every run
}

_TYPE_NAMES = {bool: "true or false", int: "a whole number", float: "a number", str: "text",
               list: "a list", dict: "a section"}


def _type_name(value):
    if value is None:
        return "nothing"
    if type(value) is float and not math.isfinite(value):
        return "an infinite or undefined number"
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def _convert(default, value, empty_ok=False):
    """Return `value` as the type of `default`, or raise ValueError."""
    kind = type(default)
    if kind is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError
        return float(value)
    if type(value) is not kind or (kind is str and not value and not empty_ok):
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
                base[key] = _convert(base[key], value, name in _EMPTY_ALLOWED)
            except ValueError:
                warnings.append(f"{where}: {name} must be {_type_name(base[key])}, "
                                f"not {_type_name(value)}: ignored")
                continue
            if name in _MINIMUM and base[key] < _MINIMUM[name]:
                warnings.append(f"{where}: {name} must be at least {_MINIMUM[name]:g}: {_MINIMUM[name]:g} used")
                base[key] = _MINIMUM[name]


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
            layer = _read_layer(path, optional, secret, warnings)
            _merge(config, {k: v for k, v in layer.items() if k not in RESERVED}, path, "", warnings)
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
