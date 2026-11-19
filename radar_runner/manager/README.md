# Radar manager

Starts and stops the radar on triggers (button, `radarctl`, later GPS motion),
only while every safety gate is good, and makes the amplifier safe after every
radar exit. It runs as user `englacial` (`systemd/radar-manager.service`) and
owns the button, the two LEDs and the ORCA process. Stage 4 of the plan.

**Not installed yet, and it has never run the real radar.** Everything below
was tested against stand-ins (`tests/manager/fake_orca`); the first start on
the hardware needs the safety review and the items under "Before the first
real start".

## States

| State | LEDs (yellow READY, green RUNNING) | Meaning |
|---|---|---|
| `not_ready` | fast blink, off | A gate refuses a start. `radarctl status` says which |
| `ready` | on, off | Waiting for the button, a command or motion |
| `starting` | breathing, off | Start sequence running, or waiting to retry a failed start |
| `running` | off, on | The radar reported that it records |
| `stopping` | breathing, off | The radar is exiting and the amplifier is being made safe |
| `fault` | fast blink, off | Latched. See "Faults" |
| `power_fail` | off, off | The power service reports a loss of main power |

"Breathing" is a PWM fade, or a slow blink with `manager.panel.breathing: blink`.

## Triggers

* **Button**: one press starts a collection when `ready`, and cancels a run
  that is wanted (starting, running, waiting to retry or resume). While
  `not_ready` or `stopping` a press does nothing. A press is the button held
  for 0.1 to 5 s, counted at the release, at least 1 s after the previous one.
* **`radarctl start` / `stop` / `toggle`**: the same as the button (`toggle`
  is exactly a press). `stop` always works and never clears a fault.
* **Automatic source** (the GPS motion trigger of stage 6; `Manager.report_auto`):
  while it says "moving" and nothing blocks it, a run is started as soon as the
  gates allow. It stops only runs it started itself, and only on "stationary";
  "unknown" (no fix) changes nothing. After any manual stop, a fault, or a
  radar that finished by itself, it is blocked until it has said "stationary".
  `radarctl start` during an automatic run takes the run over as a manual one.

A manual run ends with a manual stop, after `run.manual_timeout_s` (14 h), or
when the radar finishes by itself (a config with a pulse count).

## Gates

`not_ready` while any of these fails; the ones marked "run" also stop a
running radar, which resumes by itself when they are good again (hold-off
`run.resume_holdoff_s`, at most `run.max_resumes` times, then fault).

| Gate | Good when | Run |
|---|---|---|
| `telemetry` | telemetry status fresh and `amp.fresh` true, or the override is set | yes |
| `watcher` | safety watcher status fresh, `ok` true, `tripped` false (format not final) | yes |
| `power` | power status fresh, state `normal`, no radar stop requested | yes |
| `data` | data drive mounted and writable; free space above `start_min_free_gb` (20) | below `stop_min_free_gb` (5), or drive gone |
| `sdr` | SDR on USB at 5000 Mbit/s, or still in its boot loader | no |
| `config` | the requested radar config passes the limits | no |
| `orca` | the ORCA checkout is there | no |
| `amp_safe` | the make-amplifier-safe command exists | no |

Override: `radarctl override no-amp-telemetry on` lets the radar run without
amplifier telemetry. It is the file `<state_dir>/override-no-amp-telemetry`,
survives reboots, shows in the status and marks each collection made with it
(`no_amp_telemetry`). It does not affect the watcher gate.

## Radar config limits

Checked by the `config` gate and again on the exact text the radar is given
(`limits.py`, settings `manager.limits`): amplifier pin 2 and active low, bank
FP0; `RF0.tx_gain` at most `max_tx_gain`; `tx_duration / pulse_rep_int` at most
`max_enable_duty`; `rx_duration` at least `tx_duration`; the chirp band
(`RF0.freq + lo_offset_sw` ± half the bandwidth) inside
`min_freq_hz`..`max_freq_hz`; transmit enabled; one channel; `clk_ref` not
`gpsdo`; output below the data root. **`max_tx_gain` is 43 by default, a
conservative value for an undecided VVA setting; set the unit's value in
`/etc/radar-runner/local.yaml`.**

## Faults

| Kind | When |
|---|---|
| `start_failures` | a start failed or the radar exited with an error, and the retries (after 10, 30, 90 s) are used up |
| `resume_limit` | a gate stopped the run more than `max_resumes` times |
| `amp_unsafe` | the amplifier pin could not be confirmed safe (see "Making the amplifier safe") |
| `config` | the config failed its limits at the start itself |
| `turn_on_check` | the turn-on check refused (stage 7; the hook passes for now) |

A fault is left by a button press or `radarctl start` (clears it and starts
if the gates allow), by `radarctl clear-fault` (clears only), or by the
automatic source reporting motion after it has reported a stop. A restart of
the manager also clears it.

## A collection

`<data root>/b00042_s003_20261119T015500Z/`: boot counter, sequence number
within the boot, UTC time. Contents and the metadata file `collection.json`
are described at the top of `collection.py`. The radar writes its data chunks
there itself; nothing is moved afterwards. How this works with ORCA unchanged,
and the proposed ORCA patches: `docs/orca-proposals/README.md`.

## Making the amplifier safe

`ampsafe.py`. The tool `tools/sdr_gpio_safe/amp_safe` drives the SDR's
amplifier pin to the shutdown level. Every run of it enables the amplifier for
0.3-0.5 s itself, so **it runs only when the pin is not known to be safe**
(`manager.amp_safe.policy: when_needed`, the default; `always` also runs it
after every radar exit).

The record is the file `<state_dir>/sdr-state`: its first line is `safe` or
`unknown`; no file means unknown. It is set to `unknown` before every radar
launch.

| Situation | What happens |
|---|---|
| The radar exited cleanly (reached running, not killed, status 0) | safe; the tool does not run |
| The radar was killed, crashed, exited non-zero or never reached running | the tool runs |
| Either of the two, with a fresh amplifier reading taken after the exit and the 28 V present | the current decides: shutdown current is safe, enabled current runs the tool |
| No radar running and the record says unknown, or there is no record (also at manager start) | the tool runs; if a fresh reading shows shutdown current, the record is set to safe without it |
| The SDR is still in its boot loader (nothing initialised it since power-up) | safe |
| Enabled current for `idle_enabled_s` (5 s) with no radar process | the tool runs, at most every `retry_s` (60 s) |
| The tool says ASSUME_ENABLED (exit 3: it initialised the SDR and could not verify the pin) | run again at once, up to `assume_enabled_retries` (2) times, then fault `amp_unsafe` |
| The tool fails otherwise (NOT_FOUND, BUSY, AMBIGUOUS, TIMEOUT, FAILED, USAGE: nothing was changed) | the record stays as it was; if that is unknown, after a radar exit: fault `amp_unsafe`; with no radar: retried every `retry_s` |

The tool is only ever run by the runner's one thread, never while a radar
that the manager started is alive; a start asked for meanwhile waits for it.
The unit's stop hook (`bin/radar-manager-stop-hook`) runs the tool after the
manager has stopped or died, if the record does not say `safe`.

## radarctl

`bin/radarctl` (standard library only, run by `/usr/bin/python3`):

```
radarctl status [--json]
radarctl start | stop [--wait] [--timeout S] | toggle | clear-fault
radarctl stop --interrupt --wait        # the power service's stop hook
radarctl override no-amp-telemetry on|off
```

Exit status 0 done, 1 refused or failed, 2 usage, 3 manager not running.
`stop --wait` returns when the radar process is gone and the amplifier step
has finished. `--interrupt` stops like a failing gate does: the run stays
wanted and resumes when the gates are good again (power came back).

## Files

| | |
|---|---|
| `/run/radar-runner/user/manager.json` | status, at least once a second |
| `/run/radar-runner/user/manager.sock` | command socket (mode 0660) |
| `/var/lib/radar-runner/user/manager-events.jsonl` | event log: state changes, requests, starts, stops, faults, gate changes |
| `/var/lib/radar-runner/user/override-no-amp-telemetry` | the override |
| `/var/lib/radar-runner/user/sdr-state` | is the SDR's amplifier pin known to be safe: first line `safe` or `unknown` |
| `/var/lib/radar-runner/user/amp_safe.log` | output of the tool when it ran outside a collection |

## Tests

```
.venv/bin/python -B -m pytest tests/manager        # about 2 minutes; touches no hardware
.venv/bin/python -B -m radar_runner.manager.panel_test    # live: LEDs and button; see its --help
```

## Before the first real start

1. Safety review and the user's go-ahead (stage rule).
2. `/etc/radar-runner/radar.yaml` (the requested ORCA config) and the unit's
   `manager.limits` in `/etc/radar-runner/local.yaml`.
3. The stage 3 tool at `tools/sdr_gpio_safe/amp_safe`, and the watcher's
   status file: check the field names in `manager.gates.watcher`.
4. The items "To check on the bench" in `docs/orca-proposals/README.md`.
5. The panel test, watched by a person.
