# Radar manager

Starts and stops the radar on triggers (button, `radarctl`, later GPS motion),
only while every safety gate is good, and sees to it that the amplifier is
safe after every radar exit. It runs as user `englacial`
(`systemd/radar-manager.service`) and owns the button, the two LEDs and the
ORCA process. Stage 4 of the plan.

**Status: tested against stand-ins only. It has never run the real radar and
has never called the real reset tool.** `setup/setup.sh` installs the unit
without enabling or starting it. The first start is "First start with the real
radar" below, with the user present.

## States

| State | LEDs (yellow READY, green RUNNING) | Meaning |
|---|---|---|
| `not_ready` | fast blink, off | A gate refuses a start. `radarctl status` says which |
| `ready` | on, off | Waiting for the button, a command or motion |
| `starting` | breathing, off | Start sequence running, or waiting to retry a failed start |
| `running` | off, on | The radar reported that it records |
| `stopping` | breathing, off | The radar is exiting and the amplifier step is running |
| `fault` | fast blink, off | Latched. See "Faults" |
| `power_fail` | off, off | The power service reports a loss of main power |

"Breathing" is a PWM fade, or a slow blink with `manager.panel.breathing: blink`.

## Triggers

* **Button**: one press starts a collection when `ready`, and cancels a run
  that is wanted (starting, running, waiting to retry or resume). In
  `not_ready` and while `stopping` a press changes nothing at all: no start is
  remembered and a fault is not cleared. A press is the button held for 0.1 to
  5 s, counted at the release, at least 1 s after the previous one.
* **`radarctl start` / `stop` / `toggle`**: the same as the button (`toggle`
  is exactly a press). `stop` always works and never clears a fault.
* **Automatic source** (the GPS motion trigger of stage 6; `Manager.report_auto`):
  while it says "moving" and nothing blocks it, a run is started as soon as the
  gates allow. It stops only runs it started itself, and only on "stationary";
  "unknown" (no fix) changes nothing. After any manual stop, a fault, or a
  radar that finished by itself, it is blocked until it has said "stationary".
  `radarctl start` during an automatic run takes the run over as a manual one.

### The GPS motion trigger

Off unless `manager.triggers.gps` is true; off, none of it runs and the status
shows `"motion": {"enabled": false}`. `motion.py` reads the fixes in the
telemetry status file (`sources.gps`, one a second from gpsd) at every gate
check and reports a change to the logic above. It has no gpsd connection of
its own. A fix counts if the GPS source is healthy, the fix is new and at most
`stale_s` (5 s) old, 3D (`min_mode`), with gpsd's horizontal error estimate at
most `max_eph_m` (5 m). Settings in `manager.motion`, times in receiver time:

* "moving": every counting fix at least `start_speed_mps` (2 m/s) for
  `start_s` (20 s);
* "stationary": every counting fix at most `stop_speed_mps` (0.5 m/s) for
  `stop_s` (120 s);
* "unknown": nothing that counts for `stale_s`, and at start-up. A fix that
  does not count, a speed between the two, a gap of more than `max_gap_s`
  (3 s; below the smaller of `start_s` and `stop_s`, at most `stale_s`)
  between counting fixes, or `stale_s` without a counting fix starts both
  counts again. Each answer holds
  until another is reached: a truck creeping between the two speeds keeps
  the last one.

The defaults come from 30 h of fixes with the antenna parked indoors
(2026-11-19/20, telemetry logs): 93 % of fixes under 0.3 m/s and 99.5 % under
1 m/s, but stretches of up to 11 s in a row above 2 m/s (9 s with the error
estimate at most 5 m; with it at most 3 m, nothing above 1.5 m/s for more
than a second) and single fixes up to 10 m/s. `start_s` is at least 5 and
`stop_s` at least 30 (refused below, with a warning, and the default used).
`radarctl status` shows the answer, why, and how the newest fix counts.

A manual run ends with a manual stop, after `run.manual_timeout_s` (14 h), or
when a radar with a pulse count finishes by itself. A radar that runs until
stopped (`num_pulses: -1`) and exits with status 0 unasked is an interruption:
it is resumed like one.

## Gates

`not_ready` while any of these fails; the ones marked "run" also stop a
running radar, which resumes by itself when they are good again (hold-off
`run.resume_holdoff_s`, at most `run.max_resumes` times, then fault).

| Gate | Good when | Run |
|---|---|---|
| `telemetry` | `telemetry.json` fresh and `amp.fresh` true, or the override is set | yes |
| `watcher` | `watcher.json` fresh (5 s), `ok` true, `tripped` false; the text is `reason` | yes |
| `power` | power status fresh, state `normal`, no radar stop requested | yes, urgently |
| `data` | data drive mounted and writable; free space above `start_min_free_gb` (20) | below `stop_min_free_gb` (5), or drive gone |
| `sdr` | SDR on USB at 5000 Mbit/s, or still in its boot loader (product "WestBridge") | no |
| `config` | the requested radar config passes the limits | no |
| `orca` | the ORCA checkout is there | no |
| `amp_safe` | the reset tool exists, has `--timeout`, and the manager's limit is longer | no |

The field names of the two status files are settings (`manager.gates.*`); a
missing, old or damaged file and a field that is not exactly true or false all
count as "not good".

The SDR's link speed can only be judged once its firmware is loaded: before the
first UHD start after power-up it is a USB 2 device (`2500:0023` "WestBridge",
480 Mbit/s) and passes the gate. After the radar's start line the manager reads
the speed again and stops the run if it is not USB 3.

**Override:** `radarctl override no-amp-telemetry on` lets the radar run
without amplifier telemetry. It is the file
`<state_dir>/override-no-amp-telemetry`, survives reboots, shows in the status
and marks each collection made with it (`no_amp_telemetry`). It opens the
telemetry gate only. **The safety watcher has to honour the same file**: it
trips on lost telemetry, and while it is tripped its gate keeps the radar off
whatever the override says. (Note for the watcher's stage: test for the file
`override-no-amp-telemetry` in the user state directory.)

## Radar config limits

Checked by the `config` gate and again on the exact text the radar is given
(`limits.py`, settings `manager.limits`). The checks follow ORCA's arithmetic,
which is not what the field names suggest: the hardware is set from `RF0`, but
the length of every burst is computed from `RF1`'s rates, and the chirp file,
generated at `GENERATE.sample_rate`, is played at `RF0.tx_rate`. So:

* `RF1.tx_rate` = `RF0.tx_rate`, `RF1.rx_rate` = `RF0.rx_rate`,
  `GENERATE.sample_rate` = `RF0.tx_rate`; chirp and pulse no longer than
  `tx_duration`; the chirp fits in the sample rate;
* `DEVICE.clk_rate` between 5 and 61.44 MHz, and `RF0`'s rates equal to it
  divided by a whole number (up to 128). The SDR can run at no other rate: UHD
  quietly picks the nearest one and ORCA only prints a warning, so a rate of
  200 MS/s with a 56 MHz clock would run at 56 MS/s with every burst 3.6 times
  longer. As a second line of defence, ORCA's `[WARNING]: Requested ... rate`
  line before `[START]` is a failed start and a fault (`rate`);
* the mixer frequency `RF0.freq + RF0.lo_offset` inside the allowed band (its
  leakage is transmitted), and the chirp within half the clock rate of it;
* amplifier pin 2, active low, bank FP0;
* `RF0.tx_gain` at most `max_tx_gain`;
* enable duty (real transmit burst / `pulse_rep_int`) at most `max_enable_duty`;
* the receive window at least as long as the transmit burst;
* the chirp band on the air inside `min_freq_hz`..`max_freq_hz`;
* transmit enabled, one channel, `clk_ref` not `gpsdo` (ORCA would open and
  reconfigure the GPS receiver), `cpu_format` fc32;
* output below the data root, plain file names.

**`max_tx_gain` is 43 by default, a conservative value for an undecided VVA
setting; set the unit's value in `/etc/radar-runner/local.yaml`.**

## Starting and stopping the radar

Start: config check, collection directory, derived config, chirp, build, (the
turn-on check hook of stage 7,) launch, wait for the radar's `[START]` line,
check the USB speed.

ORCA installs its SIGINT handler only after the SDR set-up (firmware and FPGA
load, tuning). A SIGINT before that kills it in mid set-up, with the SDR's
pins as UHD's initialisation left them: inputs with the UHD build in use
(amplifier off), but low (amplifier on) with any other UHD, so the rules
below assume the worse case. So:

* A stop asked for before `[START]` is held until that line has been seen:
  up to the start timeout (60 s) normally, `orca.urgent_grace_s` (1 s) for an
  urgent stop.
* A radar that had to be signalled before `[START]` is never treated as safe:
  the reset tool runs.
* An urgent stop (power loss, the power service's hook, the manager's own
  exit) has the radar process gone `orca.urgent_stop_budget_s` (3 s) after it
  was asked for, killed if need be.
* Otherwise: SIGINT, `orca.stop_timeout_s` (3 s), then SIGKILL as the last
  resort (logged, collection marked `killed`).

`docs/orca-proposals/0003-...` would let ORCA take a SIGINT at any time.

## Making the amplifier safe

`ampsafe.py`. The tool `tools/sdr_gpio_safe/amp_safe` drives the SDR's
amplifier pin to the shutdown level. Every run of it enables the amplifier for
0.3-0.5 s itself, so **it runs only when the pin is not known to be safe**.

The record is the file `<state_dir>/sdr-state`: its first line is `safe` or
`unknown`; no file means unknown. The tool's wrapper keeps it around its own
runs (`--state-file`: `unknown` before, `safe` after an OK). The manager writes
`unknown` before every radar launch (and before the turn-on check hook), and
`safe` when it finds the pin safe without the tool.

| Situation | What happens |
|---|---|
| The radar exited cleanly (reached running, not killed, status 0) | safe; the tool does not run |
| The radar was killed, crashed, exited non-zero or never reached running | the tool runs |
| Either of the two, with an amplifier reading that counts (below) | the current decides: shutdown current is safe, enabled current runs the tool |
| The radar was signalled before `[START]` | the tool runs, whatever the reading |
| The stop came from the power service, or the 28 V rail reads absent | no reading is waited for and the tool is not run. A clean exit: `safe` is recorded from that alone, before `radarctl stop --wait` returns. Otherwise the record stays `unknown` and the power service's `amp_safe` hook runs the tool |
| Power loss or the manager's exit begins while a reading is waited for or the tool runs | the wait ends; the attempt of the tool in progress is finished, no further one is made |
| Another program's run of the tool is in progress (its lock is held) | the manager does not write `safe`: that run has written `unknown` and records its own outcome |
| The pin is safe but the record cannot be written (state directory not writable) | `safe` is kept in this manager's memory while the file stays as it is, so the tool is not run again and again; the status shows `sdr_state_problem` |
| No radar, record `unknown` or missing (also at manager start), SDR not in its boot loader | a reading that counts and shows shutdown current: `safe`. No reading: the tool runs, again every `retry_s` (60 s) until it succeeds |
| The SDR is still in its boot loader (nothing initialised it since power-up) | safe |
| Enabled current with no radar process | shown in the status (`amp_enabled_while_idle`); never marked safe; **the safety watcher acts on it**, not the manager |
| The tool says ASSUME_ENABLED (exit 3) | run again at once, up to `assume_enabled_retries` (2) times, then fault `amp_unsafe` |
| The tool says NOT_FOUND, BUSY or TIMEOUT after a radar exit | tried again `unavailable_retries` (3) times, `unavailable_delay_s` (5 s) apart (the SDR may be coming back after a kill), then fault `amp_unsafe` |
| Any other failure of the tool after a radar exit | fault `amp_unsafe` |

A reading counts if the telemetry status is fresh with `amp.fresh` true, the
28 V rail is present (`amp.voltage_v` at least 20 V), the telemetry loop is
running evenly (`loop.last_gap_s` at most 1 s: after a stall the rows that
piled up come out together), and, for shutdown current, `amp.sample_uptime_s`
is at least `confirm_settle_s` (2 s) after the end of the last radar or tool
run. That time is when the row reached the Pi; the ADC measured it earlier.
A reading taken during a stall means "wait for the next one" (for at most
`confirm_timeout_s`, 5 s), not "no reading": one stall does not run the tool.

The tool is only ever run by the runner's one thread, never while a radar that
the manager started is alive; a start asked for meanwhile waits for it. The
manager's limit for the tool (`timeout_s`, 40 s) is longer than the tool's own
`--timeout` (30 s; a first contact after power-up took 17 s). The unit's stop
hook (`bin/radar-manager-stop-hook`) runs the tool after the manager has
stopped or died, if the record does not say `safe` and the SDR is not in its
boot loader. The power service's `amp_safe` hook is the same script with
`--timeout 10`, so on a power loss the tool runs only when it is needed.

### The power-off sequence

With the two hook lines that `setup/setup.sh` prints (`stop_radar`:
`radarctl stop --interrupt --wait --timeout 4`, limit 5 s; `amp_safe`:
`bin/radar-manager-stop-hook --timeout 10`, limit 12 s), in seconds after the
loss of main power (the power service's own steps are in `power/README.md`):

| | Typical | Worst case (every step at its limit) |
|---|---|---|
| Commit | 9.8 | 9.8 |
| `stop_radar` hook (5 s + 1 s) | radar gone in under 1 s; `safe` recorded | 15.8 |
| `amp_safe` hook (12 s + 1 s) | record says `safe`: nothing run | 28.8 |
| `sync` (5 s) | | 33.8 |
| Guard record (3 s) | | 36.8 |
| Power-off command | about 11 | 37.2 |
| Last resort, if still running | | 47.2 |

The capacitor carried the running radar for about 58 s at room temperature
(not measured cold): 21 s are left after the command and 11 s after the last
resort in the worst case. When the tool does run (the radar was killed, or
signalled during its set-up), it takes about 3.5 s with the SDR's firmware
loaded; with the SDR still in its boot loader the hook does nothing.
`setup/check.py` adds up the installed hooks' limits and fails above 48 s to
the last resort.

## Faults

| Kind | When |
|---|---|
| `start_failures` | a start failed or the radar exited with an error, and the retries (after 10, 30, 90 s) are used up |
| `resume_limit` | the run was interrupted more than `max_resumes` times without a stable run in between |
| `restart_limit` | more than `max_restarts_per_hour` (12) automatic restarts (retries and resumes) in an hour |
| `start_limit` | more than `max_starts_per_hour` (40) starts of any kind in an hour, fresh runs included |
| `rate` | the SDR does not run at the requested sample rate (ORCA's warning before `[START]`) |
| `amp_unsafe` | the amplifier pin could not be confirmed safe |
| `config` | the config failed its limits at the start itself |
| `turn_on_check` | the turn-on check refused (stage 7; no hook is installed yet) |

Leaving a fault:

* `amp_unsafe` clears by itself when the record says `safe` again (a later run
  of the tool that succeeded, or a reading that counts), and with
  `radarctl clear-fault`. A button press does not clear it.
* Every other fault: a button press or `radarctl start` while the gates allow
  a start (the fault is cleared and the radar starts); `radarctl clear-fault`
  (clears only); the automatic source reporting motion after it has reported a
  stop.
* A press or `start` while a gate is bad changes nothing: the fault and its
  reason stay.
* A restart of the manager clears any fault.
* The two per-hour counts are reset by time only, and by a person clearing a
  `restart_limit` or `start_limit` fault (button, `start`, `clear-fault`):
  both lists then start afresh. The automatic source clearing the fault does
  not reset them.

Every start initialises the SDR, which enables the amplifier for about half a
second. So no start comes sooner than `min_start_gap_s` (5 s) after the last
run ended, and `max_starts_per_hour` is 40: one start every 90 s, kept up for
a whole hour. Stop-and-go driving with the motion trigger stays well below
that as long as the trigger needs some tens of seconds to call a stop; a
trigger or a gate that flaps faster is stopped by this limit.

The status keeps the last fault and how it was cleared (`last_fault`).

Settings that would defeat the limits are refused with a warning and the
default used: a retry delay under 5 s, `stable_run_s` under 60 s,
`resume_holdoff_s` under 2 s, `min_start_gap_s` under 2 s.

## A collection

`<data root>/b00042_s003_20261119T015500Z/`: boot counter (the shared one in
`radar_runner/common/bootcount.py`), sequence number within the boot, UTC
time. Contents and the metadata file `collection.json` are described at the
top of `collection.py`. The radar writes its data chunks there itself; nothing
is moved afterwards. The radar's output (`uhd_stdout.log`, `.1`, ...) is kept
complete, in parts of 32 MB: its per-pulse lines are needed to process the
data. `collection.json` also records the ORCA commit, whether the ORCA working
tree had uncommitted changes, and the SHA-256 of the radar binary that ran.
How this works with ORCA unchanged, and the proposed ORCA patches:
`docs/orca-proposals/README.md`.

## radarctl

`bin/radarctl` (standard library only, run by `/usr/bin/python3`;
`setup/setup.sh` links it into `/usr/local/bin`):

```
radarctl status [--json]
radarctl start | stop [--wait] [--timeout S] | toggle | clear-fault
radarctl stop --interrupt --wait --timeout 4     # the power service's stop hook
radarctl override no-amp-telemetry on|off
```

Exit status 0 done, 1 refused or failed, 2 usage, 3 manager not running.
`stop --wait` returns as soon as the radar process is gone (the amplifier step
that may follow is not waited for), and never later than `--timeout`. The
manager answers that question at the moment it is asked, not from its last
status snapshot.
`--interrupt` is an urgent stop that keeps the run wanted: it resumes when the
gates are good again (power came back).

Time of `radarctl stop --interrupt --wait --timeout 4`, from the call to its
return, with the default settings:

| Radar is ... | With ORCA patch 0001 (exits in 0.5 s) | Without it |
|---|---|---|
| recording | about 0.2 s (manager and polling) + the exit: under 1 s; at most 3.2 s if the exit hangs | typically 0.5 s; **3.2 s** when the exit hangs (60 s in ORCA: killed at the 3 s budget) |
| in its SDR set-up (before `[START]`) | 1 s of grace, then the signal kills it: about 1.2 s; if `[START]` comes within the grace, the exit follows: at most 3.2 s | the same |
| not launched yet (chirp, build) | about 0.2 s | the same |

Worst case in every state: `urgent_stop_budget_s` (3.0 s) + about 0.2 s, inside
the command's own 4 s and the hook's 5 s.

## Files

| | |
|---|---|
| `/run/radar-runner/user/manager.json` | status, at least once a second |
| `/run/radar-runner/user/manager.sock` | command socket (mode 0660) |
| `/var/lib/radar-runner/user/manager-events.jsonl` | event log: state changes, requests, starts, stops, faults, gate changes. Each line is synced to the drive by a thread of its own (never holding up a stop); at its own stop, once the radar is stopped, the manager waits up to 1 s for the last lines to be synced |
| `/var/lib/radar-runner/user/override-no-amp-telemetry` | the override |
| `/var/lib/radar-runner/user/sdr-state` | is the SDR's amplifier pin known to be safe: first line `safe` or `unknown` |
| `/var/lib/radar-runner/user/amp_safe.log` | output of the tool when it ran outside a collection |
| `/etc/radar-runner/radar.yaml` | the ORCA config to run (not created by anything here) |
| `<collection>/gps_raw.ubx` | the raw GPS log for PPK, from just before the radar's launch to its exit (`radar_runner/gpsraw/README.md`) |

## Tests

```
.venv/bin/python -B -m pytest tests/manager        # about 5 minutes; no SDR, no real pins, never the real tool
.venv/bin/python -B -m radar_runner.manager.panel_test    # live: LEDs and button; see its --help
```

`tests/manager/test_manager_live.py` runs the service against the installed
telemetry and power services' real status files (read only) with stand-ins for
everything else; it is skipped where those files are not fresh.

## First start with the real radar

With the user present. Emergency stop throughout: power off the 12 V supply.

Before:

1. The reset tool has passed its own first run (`tools/sdr_gpio_safe/README.md`).
2. `setup/check.py`: the section "setup.sh: radar manager" has no FAIL other
   than what this procedure is about to change.
3. Hardware as in `HARDWARE_CONFIG.md`: amplifier output into the attenuators,
   not an antenna; VVA/J8 as decided; scope on GPIO_0 if available. Telemetry
   service running, amplifier at shutdown current (about 0.156 A).
4. `/etc/radar-runner/radar.yaml`: start with a low-power config (the bench's
   300 MHz config with a low `tx_gain`). `/etc/radar-runner/local.yaml`:
   `manager.limits.max_tx_gain` for this unit, if the default 43 is too low.
5. The safety watcher must be running (its gate), or, for this supervised run
   only, point the watcher gate at a stand-in file by hand; do not leave that
   in place.
6. No other program uses the SDR (HIL harness, bench guard).

Steps:

1. In a terminal: `journalctl -fu radar-manager`. In another:
   `sudo systemctl start radar-manager` (start only; do not enable yet).
2. `radarctl status`: every gate `ok`, state `ready`, yellow LED on. If the
   SDR record was unknown and telemetry was missing, the reset tool has run
   once (no enable with the UHD build in use): the journal says so.
3. `radarctl start`. Watch: yellow breathing during chirp, build and SDR
   set-up (about 5 s; about 20 s on the first start after power-up), no
   enable at the UHD initialisation (a 0.48 s enable there means the radar
   was built against another UHD), then green on, amplifier current
   as on the bench for this duty, no trip.
4. `ls` the newest directory under `/mnt/nvme/radar-runner/collections/`:
   `rx_samps.bin.0` growing, `uhd_stdout.log`, `collection.json`.
5. `radarctl stop --wait`: returns within a second; current back at 0.156 A;
   the journal says the tool was not run (clean exit); `radarctl status` shows
   `ready`.
6. Button: one press starts, one press stops. LEDs as in the table.
7. Stop during the set-up: `radarctl start`, and `radarctl stop` two seconds
   later: the stop is held until the start line, then a clean exit.
8. Kill the radar (`kill -9` its pid from `radarctl status`): the tool runs
   unless telemetry shows shutdown current; the radar restarts after 10 s.
9. `sudo systemctl stop radar-manager` while recording: the radar is gone
   within 3 s; the journal of the stop hook shows nothing to do.
10. Only then: `sudo systemctl enable radar-manager`, the power service's two
    hooks (printed by `setup/setup.sh`), and the power-pull tests of the plan.

To stop at any time: `radarctl stop`; if the manager does not answer,
`sudo systemctl stop radar-manager`; if the amplifier stays enabled (about
2.5 A), `tools/sdr_gpio_safe/amp_safe --timeout 30`, and failing that the 12 V
supply.
