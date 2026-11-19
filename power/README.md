# Power service

Shuts the Pi down cleanly when main power is lost, using the run time of the
supercapacitor UPS. It is the only part of this repository that may power the
Pi off. Python standard library only; run by `/usr/bin/python3` as root.

## What it does

Two signals are sampled every 0.2 s:

| Signal | Source | Loss means |
|---|---|---|
| `current` | INA219 on I2C bus 1, address 0x40 (read only) | capacitor current below -0.3 A (discharging) |
| `gpio` | GPIO 26, input, bias disabled | line low (follows a real loss by about 1.5 s) |

Each signal has a state: `present`, `loss`, `unreadable` (no valid reading for
2 s), `faulty` (said loss for more than 180 s while the Pi kept running; ignored
until it reads present for 60 s) or `unknown` (just started, nothing confirmed
yet). A loss needs 2 consecutive samples, a restore 1 s of present readings.

1. First confirmed loss on either healthy signal: `radar_stop_requested`
   becomes true in the status file and the `stop_radar` hook runs. If power
   returns, the request is cleared.
2. The shutdown is committed when the agreement rule has held for 8 s without
   a break. The rule holds while both signals say loss, or one says loss and the
   other is unreadable or faulty (`single_signal_action`). A single "present"
   sample from a signal that says loss starts the 8 s again; a failed read does
   not. Two readable signals that disagree never start the timer; they raise a
   warning. Because GPIO 26 lags, a real outage commits about 10 s after the
   loss, and outages shorter than that ride through.
3. Capacitor below 4.5 V: while the rule holds, the 8 s wait is skipped. And a
   capacitor that has been discharging (current signal in loss) below 4.5 V
   for 2 s commits by itself, whatever GPIO 26 says, because that cannot happen
   with main power present.
4. Once committed there is no abort: `stop_radar` hook (if it has not already
   succeeded), `amp_safe` hook, `sync`, record the shutdown for the boot-loop
   guard, then a final power check on a fresh sample. If every healthy signal
   is confirmed present (and at least one is healthy): reboot. Otherwise: power
   off. Each step has a timeout and a failure never blocks the next step.
5. If the Pi is still running 10 s after the command, or the command fails, the
   last resort is an immediate power-off without sync, repeated every 10 s (see
   the comment in `service.py`).

Timeline of a real outage, in seconds after the loss:

| | Typical | Worst case (every step hangs to its timeout) |
|---|---|---|
| Radar stop requested | 0.4 | 0.4 |
| GPIO 26 agrees, 8 s timer starts | 1.8 | 1.8 |
| Commit | 9.8 | 9.8 |
| `stop_radar` hook, second try (5 s + 1 s) | | 15.8 |
| `amp_safe` hook (5 s + 1 s) | | 21.8 |
| `sync` (5 s) | | 26.8 |
| Guard record (3 s) | | 29.8 |
| Power-off command | 10.1 | 30.2 |
| Last resort, if still running | | 40.2 |
| OS down | about 14 | |

With no hooks configured (stage 1) the worst case is 18 s to the command and
28 s to the last resort. The capacitor carried the running radar for about 58 s
at room temperature; it has not been measured cold.

With one signal out of use:

- GPIO 26 stuck high (says present during a real outage): the signals disagree,
  so there is no shutdown until the capacitor has fallen below 4.5 V (from a
  full charge about 23 s into the outage at radar load, about 40 s when idle);
  then rule 3 shuts down.
- GPIO 26 stuck low: warning `signals_disagree`, the radar stop request stays on
  for 180 s, then GPIO 26 is marked faulty and the current signal alone decides.
- INA219 unreadable (the state of the bench on 2026-11-18: nothing answers at
  0x40): warning `current_unreadable`; GPIO 26 alone decides, with the same 8 s
  rule, and the low-voltage rules do not apply. The boot-loop guard, once
  passive, does not re-arm by itself in this state.

It never shuts down when:

- `armed = false` (the default, monitor-only): same decisions, logged as
  `WOULD COMMIT`, no hooks, no power-off
- the disable file `/etc/radar-runner/power-disarm` exists
- the boot-loop guard is passive: after 3 shutdowns in a row that each happened
  within 300 s of boot. It re-arms after 30 min of both signals healthy and
  present, or by hand (below). The count resets when a boot passes 300 s
- the guard state in `/var/lib/radar-runner/` cannot be written
- both signals are unreadable or faulty

`radar_stop_requested` is published in every mode.

## Checking it

```
systemctl status radar-power
journalctl -u radar-power -b            # state changes and decisions
cat /run/radar-runner/power.json        # status, rewritten about twice a second
cd ~/englacial-radar-runner && python3 -B -m power status    # same, plus its age
```

Status fields: `time` (Unix seconds; readers should treat a value more than a
few seconds old as stale), `uptime_s` (seconds since boot, unaffected by clock
changes), `state` (`normal`, `loss`,
`would_shutdown`, `committing`, `shutdown`), `mode` (`armed`, `monitor`,
`passive`) with `mode_reasons`, `power_present`, `radar_stop_requested`,
`signals.current` and `signals.gpio` (state, value, read errors), `capacitor_v`,
`current_a`, `warnings`, `guard` (early-shutdown count, passive, last shutdown).

`~/ups-bringup/ups_check.py` cannot run while the service is running: GPIO 26
can only be held by one process.

## Configuration

Optional file `/etc/radar-runner/power.toml`; `config/power.toml` lists every
setting with its default. The ones most likely to change:

| Setting | Default | Meaning |
|---|---|---|
| `armed` | `false` | `true` lets the service run hooks and power off |
| `single_signal_action` | `"shutdown"` | `"quiesce"`: never power off on one signal alone (rule 3 still applies) |
| `shutdown.ride_through_s` | 8 | how long the signals must agree on a loss |
| `shutdown.low_voltage_v` | 4.5 | capacitor voltage for rule 3 |
| `shutdown.last_resort_after_s` | 10 | delay before the immediate power-off |
| `hooks.stop_radar`, `hooks.amp_safe` | `[]` | commands as argument lists, with `*_timeout_s` (5 s) |
| `guard.early_boot_s`, `guard.max_early_shutdowns`, `guard.passive_rearm_s` | 300, 3, 1800 | boot-loop guard |
| `paths.status_file`, `paths.state_dir`, `paths.disable_file` | see above | also `--status-file`, `--state-dir`, `--disable-file` |

A setting that cannot be used is ignored, its default applies, and the problem
appears in the journal and under `warnings.config` in the status file. Restart
the service after a change.

## Installing (needs root)

```
sudo mkdir -p /etc/radar-runner
sudo cp ~/englacial-radar-runner/config/power.toml /etc/radar-runner/power.toml   # armed = false
sudo cp ~/englacial-radar-runner/systemd/radar-power.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radar-power
```

The service creates `/run/radar-runner` and `/var/lib/radar-runner` itself.
`HandlePowerKey=ignore` must be set in logind (`~/ups-bringup/SYSTEM_CHANGES.md`).

- Arm: set `armed = true` in `/etc/radar-runner/power.toml`, then `sudo systemctl restart radar-power`
- Disarm at once, no restart needed: `sudo touch /etc/radar-runner/power-disarm` (remove the file to re-arm)
- Clear the boot-loop guard:
  `cd ~/englacial-radar-runner && sudo /usr/bin/python3 -B -m power clear-guard && sudo systemctl restart radar-power`

To try it without root or systemd (monitor-only, nothing installed):

```
cd ~/englacial-radar-runner
python3 -B -m power --monitor-only --status-file /tmp/p/power.json --state-dir /tmp/p/state
```

## Power-pull tests (stage 1 "Done when")

"Pull" means unplugging the 12 V input of the UPS, not the USB-C cable to the
Pi. Keep `journalctl -fu radar-power` open in an SSH session. After a shutdown,
read the previous boot with `journalctl -b -1 -u radar-power`.

Monitor-only first (`armed = false`):

1. Power present: status shows `state: normal`, both signals `present`, no warnings.
2. Pull for 4 s, restore. Expect `POWER LOSS indicated by current` within 0.5 s,
   then `back to normal`. No `WOULD COMMIT`.
3. Pull for 15 s, restore. Expect `WOULD COMMIT to shutdown` about 10 s after the
   pull, then `back to normal`. The Pi stays up.
4. Leave it running for 24 h. `journalctl -u radar-power --since -24h | grep -c -e "POWER LOSS" -e "WOULD COMMIT"` must print 0.

Then armed (`armed = true`, restart the service):

5. Clean shutdown, 20 times: wait until the Pi has been up for more than
   5 minutes (earlier pulls count towards the boot-loop guard, test 9; setting
   `early_boot_s = 0` under `[guard]` switches the guard off for this test),
   pull and leave it out for 30 s, restore. Expect the Pi to power off about 14 s after
   the pull and to boot by itself after the restore. After each boot check
   `journalctl -b -1 -u radar-power` (ends with `COMMIT to shutdown`, `POWER OFF`
   and the `systemctl` command, no `last resort`) and
   `journalctl -b -k | grep -i -e recover -e "EXT4-fs error"` (no output).
6. Short outage: pull for 4 s, restore. The Pi stays up.
7. Power restored during the sequence: to make the sequence long enough to hit
   by hand, set `amp_safe = ["/bin/sleep", "4"]` under `[hooks]` and restart the
   service. Pull, restore after 12 s (the commit is at about 10 s, the hook runs
   until 14 s). Expect `REBOOT instead of power-off` and a reboot without help. Remove the hook afterwards.
8. Signal faults, with power present:
   - UPS monitor unreadable: set `ina219_address = 0x41` under `[signals]` and
     restart. Expect the warning `current signal unreadable` and no shutdown.
     (A real pull in this condition shuts down on GPIO 26 alone, about 10 s
     after the pull, unless `single_signal_action = "quiesce"`.)
   - Signals disagree: set `gpio_line` under `[signals]` to a line that is held
     low and restart. GPIO 17 should do, through the LED gate pull-down on the
     shim board (not tried yet). Expect `signals disagree` after 5 s, no
     shutdown, and `gpio: loss -> faulty` after 180 s.
   Restore both settings afterwards.
9. Boot-loop guard: three times in a row, pull within 5 minutes of boot and
   restore after 30 s. On the fourth boot the status shows `mode: passive` and
   the warning `boot_loop_guard`. Pull for 15 s: `WOULD COMMIT`, no shutdown.
   Then clear the guard (above).

## Tests

```
cd ~/englacial-radar-runner && /usr/bin/python3 -B -m unittest
```

They use fakes for the hardware and for the power-off command, and replay the
recorded outages in `~/ups-bringup/logs/`. No test can power the Pi off.
