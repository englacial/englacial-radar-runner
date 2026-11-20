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

Start: config check, collection directory, derived config, chirp, build, the
turn-on check if it is enabled (its low-power radar, then its analysis: see
"Turn-on check"), launch, wait for the radar's `[START]` line, check the USB
speed.

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

## Turn-on check

`turnon.py` and `tools/turn_on_check/measure.py`; settings `manager.turn_on_check`
(**off by default**: with `enabled: false` the start is exactly as without it).
**It has not run on the real radar yet.**

At every start, after the build, a short run at low power, then the analysis,
then the radar at the requested power. The low-power run is the collection's own
config (as written and checked) with three changes, and nothing else (checked):
`RF0.tx_gain` capped at `max_tx_gain` (40: 30 dB under the cap of 70, about
+9.6 dBm, 9 mW peak, at the amplifier output), `CHIRP.num_pulses` set to
`pulses` rounded up to whole records (15000: 15 records at presum 1000, 3 s at
5 kHz), and its output in `<collection>/turn_on_check/` with the data file
`low_power.bin.0`. The waveform is unchanged, so every peak is at the delay it
has at full power. It passes the same config limits, and is the same binary
started the same way: the safety watcher sees a radar process, the record says
`unknown` before it, the amplifier step follows its exit, and every stop (button,
command, a gate, the power service's hook, the manager's exit) stops it exactly
as it stops the radar at power, after which nothing more is started. The start
limit counts two SDR starts for each start (`max_starts_per_hour`).

| Criterion | From | Fails when |
|---|---|---|
| `amplifier_before` | the telemetry status just before the low-power run | idle current outside 0.10-0.20 A; the 28 V rail or a temperature beyond the safety watcher's own limits (outside 26-30.5 V; 55 C or more; no cold limit); no reading |
| `amplifier_during` | telemetry readings during its pulses (after `CHIRP.time_offset`) | the highest not 0.05 A above the idle reading (not enabled); one above 0.6 A. Fewer than `min_current_readings` (2) readings: not judged, with a warning |
| `data` | the data file | fewer records than the pulse count asks for |
| `clipping` | the raw maximum of the low-power data | at or above `clip_fs` (0.9 of full scale) |
| `signal` | the pulse-compressed profile (the chirp correlated with each record, magnitudes averaged; dB relative to the chirp, as `tools/transit_check`) | see below; not judged until `expected.verified` |
| `analysis` | | the analysis failed or ran out of time (30 s) |

Without amplifier telemetry and with the override set, the two amplifier
criteria are not judged. The raw maximum scaled to the requested `tx_gain`
(1 dB per step) is shown, and is a warning (not a failure) when it would clip.
Telemetry writes one reading every 0.5 s (`telemetry.status_interval_s`): pulses
that last less than 0.4 s plus that many readings' worth (1.4 s for 2) give a
warning in the result, since they cannot be relied on for the readings.

**The signal.** `expected.leakage_sample` is where the leakage inside the SDR
peaks, `expected.delay_samples` how much later the direct signal between the
antennas peaks. If the delay is at least `min_separation_samples` (4), the
leakage peak is looked for within `tolerance_samples` (1.5) of its place, and the
direct peak within the tolerance of the found leakage peak plus the delay. If it
is shorter, the two form one peak (one sample is 5.4 m of path in air, and a
30 MHz chirp spreads each peak over about 3 samples): the level of that combined
peak, at `leakage_sample + delay_samples`, is judged. Either way the direct (or
combined) peak must be a local maximum inside the tolerance, and its level, scaled
from the low-power `tx_gain` to `expected.level_tx_gain`, at least
`expected.min_level_db`.

What the bench loopback can show: the amplifier criteria, data, clipping (the
measurement; it does not clip at 40), every failure and stop path, and the
signal only as one combined peak: the portable loopback's 30 ft cable puts the
loopback about 3 samples after the leakage (combined peak at 78.4, -5.9 dB at
`tx_gain` 70). Disconnecting it leaves the leakage, about 30 dB lower and 3
samples earlier (the 50 m cable showed it on its own: sample 75, -35 dB at 70),
so the combined peak at 78.4 is gone. Only antennas can show the separate direct
peak after the leakage (4 samples or more: about 21 m of path in air, cables
counted by their velocity factor), the real levels, a partly damaged feed, and
whether the direct wave clips at full power.

| Outcome | Then |
|---|---|
| passed | the radar starts at the requested power |
| failed, record only (`blocking: false`, or the expected values not set) | the radar starts anyway; the collection carries the flag `turn_on_check_failed`; a low alert |
| failed, blocking (`blocking: true` and `expected.verified`), on the signal, clipping or data | the radar stays off: fault `turn_on_check` (fast blink, high alert with the reason); the next button press, `radarctl start` or motion trigger clears it and checks again |
| failed, blocking, on the amplifier readings only, or the analysis failed | a failed start, retried as any (below): a matter of telemetry or the amplifier, not of the antenna |
| the low-power radar failed (did not start, exited with an error, killed, USB 2, not finished within `time_offset` + twice its pulse time + 5 s) | a failed start, retried as any (10, 30, 90 s, then `start_failures`) |
| the amplifier not confirmed safe after it | fault `amp_unsafe`, as after any radar |
| a stop at any point | nothing more is started |
| its directory cannot be written | a failed start (`directory`) |

What it adds to a start, warm SDR: SDR set-up 4.9 s, `CHIRP.time_offset` 1 s,
the pulses 3 s, the exit 0.1 s, the amplifier step after its exit (a reading
taken 2 s after the exit: 2-2.7 s), the analysis 0.3 s: about 11.5 s. On the first
start after power-up the firmware and FPGA load (about 14 s) happens in the
low-power run instead of the radar at power, so it adds nothing. Nothing of the
check runs while the radar records.

**Files** in `<collection>/turn_on_check/`: `config.yaml`, `chirp.bin` (a copy),
`low_power.bin.0`, `uhd_stdout.log`, `uhd_stderr.log`, `gps_log.txt`,
`analysis_input.json`, `analysis.json`, `analysis.log`, `amp_safe.log` if the
tool ran, and `result.json` (settings, readings, measurement, every criterion).
`collection.json` gets the summary under `turn_on_check`, the event log an
event `turn_on_check`, and the status the last summary (`turn_on_check.last`).
About 0.2 MB per start.

**What the technician sees:** yellow breathing for the whole start (low-power run
included), green only at full power, fast blink after a blocking failure.
`radarctl status`:

```
state:      fault
fault:      turn_on_check: turn-on check failed: no combined peak at sample 78.4 +/- 1.5 ...
turn-on:    check blocking; last: FAILED, the radar stays off: ... (b00042_s003_20261201T101500Z)
```

### Setting the expected values

After the first start in the field (or after any change to the antennas,
cables, receive chain or radar config), with `blocking: false`:

1. Start the radar and let it record for a minute; stop it.
2. Verify the collection by hand: the direct signal where the antenna spacing and
   cables put it, at a sensible level.
3. Measure it, and copy the block that it prints into `/etc/radar-runner/local.yaml`
   (with `blocking: true` beside `expected` to make the check block); restart the
   manager:

   ```
   ~/miniforge3/envs/orca/bin/python tools/turn_on_check/measure.py <collection>   # or <collection>/turn_on_check
   ```

   It takes the strongest peak as the direct one and the strongest peak up to 40
   samples before it (and at most 40 dB weaker) as the leakage; `--direct` and
   `--leakage` override that, `--margin-db` (12) sets the minimum level below the
   measured one.
4. The next start: `radarctl status` shows "check blocking; last: passed ...".

On the bench (portable loopback, `b00017_s003_20261120T085831Z`, `tx_gain` 70):

```
peaks (sample, dB relative to the chirp):
     78.40     -5.9
one peak at 78.4 (-5.9 dB): the leakage and the direct signal cannot be told apart ...

manager:
  turn_on_check:
    expected:
      verified: true
      leakage_sample: 78.4
      delay_samples: 0.0
      tolerance_samples: 1.5
      level_tx_gain: 70
      min_level_db: -17.9      # -5.9 dB measured, less a margin of 12 dB
```

**Before turning blocking on, know what the thresholds rest on.** The idle-current
window (0.10-0.20 A), the current rise (0.05 A) and the levels were all set from
bench readings at room temperature. The rise comes from the enable duty (6.5 %
here, about 0.1 A) and becomes marginal below about 3 % duty. How the amplifier's
gain changes with temperature is not known (the load on the VVA node may change in
the cold), so the level at -30 C may be several dB lower than on the bench. Use a
level margin of 12 to 15 dB, not less: on the bench the level stayed within 0.4 dB
over every run and power cycle of 2026-11-20 (`docs/transit-reference/`), and with
the amplifier off (the 28 V gone, only the leakage left; b00013_s003) the peak is
27.6 dB down and 3 samples early, so 12 to 15 dB still fails that case and a
disconnected cable (about 30 dB). With the
earlier 50 m cable the same tool finds the two peaks: leakage 74.98, delay 15.18
samples (81 m of path in air), -10.6 dB.


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
| `turn_on_check` | the turn-on check failed while blocking (see "Turn-on check") |

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

## Tests

```
.venv/bin/python -B -m pytest tests/manager        # about 5 minutes; no SDR, no real pins, never the real tool
~/miniforge3/envs/orca/bin/python -B -m pytest tests/turn_on_check     # the analysis of the turn-on check (numpy)
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
