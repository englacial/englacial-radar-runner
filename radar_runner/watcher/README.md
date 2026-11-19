# Safety watcher

Watches the amplifier readings and publishes whether the radar may run. When
it may not and a radar is still running, it stops the radar itself. When the
amplifier is left enabled with no radar running, it runs the SDR GPIO reset
tool. It runs as user `englacial` (`systemd/radar-watcher.service`).

There is no hardware over-temperature cutoff: this service is the only
protection against an overheating amplifier, and against an amplifier left
enabled with no RF (about 70 W of heat). It cannot remove the 28 V, and it
does not run while the Pi is off or hung. It never powers off or reboots the Pi.

**It does not trust the radar manager.** "A radar is running" means: a process
exists whose executable is the radar binary (`watcher.radar_exe`, looked up in
`/proc`). It never reads the manager's status, does not need the manager to be
running, and stops a radar with signals of its own. It owns no device: its
inputs are the telemetry service's status file, the power service's status
file, one flag file and `/proc`.

| File | What |
|---|---|
| `logic.py` | Every decision, as a state machine without I/O or clock |
| `service.py` | Reads the inputs, sends signals, runs the reset tool, writes the status file and the event log |

| Reads | |
|---|---|
| `/run/radar-runner/user/telemetry.json` | amplifier readings, Pi readings (telemetry service) |
| `/run/radar-runner/power.json` | is main power lost (power service) |
| `/var/lib/radar-runner/user/override-no-amp-telemetry` | the user's override (set through the manager) |
| `/proc/<pid>/exe` | which processes are radar processes |

| Writes | |
|---|---|
| `/run/radar-runner/user/watcher.json` | status, twice a second |
| `/var/lib/radar-runner/user/watcher-events.jsonl` (and `.1`) | event log |
| `/run/radar-runner/user/watcher-reset-tool.out` | output of a reset tool run, while it runs |
| `/var/lib/radar-runner/user/sdr-state` | written by `amp_safe`, not by the watcher: is the amplifier pin known to be safe |

## What counts as a usable reading

A reading is the `amp` block of `/run/radar-runner/user/telemetry.json`. It is
usable when all of this holds; otherwise there is "no telemetry":

* the file is readable, from this boot and at most 3 s old (by uptime, not wall clock);
* `amp.fresh` is exactly `true` (the telemetry service's own judgement);
* `amp.sample_uptime_s` is at most 3 s back;
* current and voltage are plausible numbers, and so is the amplifier
  temperature while the 28 V is present, and so is the case temperature
  (`require_case_temp`). Plausible: -60 to 150 C, -1 to 12 A, -2 to 40 V; the
  ADC's full scale is 409 C, 12 A and 45 V.

A reading counts once, however often the watcher sees it (the telemetry
service publishes a new one every 0.5 s).

**One reading never decides anything**, because the OLA's rows have no checksum:

* A limit trips after `trip_samples` (3) readings in a row beyond it, spanning
  at least `trip_span_s` (1 s). At one reading per 0.5 s that is 1 to 1.5 s,
  which is nothing against the amplifier's thermal time (minutes), and three
  damaged readings in a row that all look plausible and all lie beyond the same
  limit do not happen by chance.
* A trip clears after `clear_hold_s` (30 s) of readings on the good side of the
  recover level, without a single one in between that is not.
* A gap of more than 3 s between readings starts every count again, and so does
  a stall of the watcher itself.
* Loss of telemetry is judged by time alone: no usable reading for more than
  `telemetry_lost_s` (3 s). After a loss, three usable readings in a row are
  needed before the status is ok again.

## Rules

| Condition | Rule | Status | Numbers from |
|---|---|---|---|
| Amplifier temperature (only with the 28 V present) or case temperature | warning at 50 C; **trip at 55 C**; clears below 50 C for 30 s | not ok, tripped | `pa_run.py` `TEMP_WARN_C`, `TEMP_STOP_C` (thermal design limit 55 C, derating from 60 C). Recover level and hold: chosen here |
| 28 V rail while powered (20 V or more) | **trip outside 26-30.5 V**; clears inside 26.5-30.0 V for 30 s | not ok, tripped | plan; `pa_run.py` `VOLT_MIN`, `VOLT_STOP`; 30.0 is its `VOLT_MAX` |
| 28 V rail absent (below 20 V) | no trip, no event beyond one line, no action by itself. The radar may not run (`allow_unpowered: true` changes that, for bench work) | not ok, not tripped | `pa_run.py` `VOLT_POWERED`; the user's rule "no RF into an unpowered amplifier" |
| 28 V rail below 26 V while the power service reports a power loss | treated as "absent", not as a supply fault: the rail is on its way down. The `not_ok` event is then `info`, not `warning`: a power loss is the normal end of a day | not ok, not tripped | chosen here |
| No telemetry for more than 3 s | the radar may not run, with the reason from the telemetry service | not ok, not tripped | plan ("a few seconds"); `pa_run.py` `STALE_S` was 2 s on raw rows |
| No telemetry, override set | not a problem; everything that can still be judged is | ok, warning | plan |
| Enabled current (above 0.5 A) for 5 s with no radar process | run the reset tool, see below | not ok, tripped | `pa_guard.py` `IDLE_ENABLED_TRIP_S`, `CURRENT_SHUTDOWN_MAX_A` (shutdown 0.156 A, enabled about 2.5 A) |
| Status not ok and a radar process alive for 5 s | SIGINT to the radar; SIGKILL 5 s later; SIGKILL again every 5 s while it lasts | | grace: chosen here (the manager needs up to about 4 s); exit time: `pa_guard.py` `RADAR_SIGINT_GRACE_S` |
| Pi SoC at 80 C or more, Pi throttled, amplifier below -40 C, telemetry loop gaps over 1 s | warning only | ok | Pi 5 soft limit; amplifier rating. No cold limit is decided (plan) |

`pa_run.py`'s trip on the two temperature sensors disagreeing is not carried
over: the bench runs with the amplifier held enabled showed 11 to 20 C between
them in normal operation.

**Telemetry is absent for about 12 s after every start of the telemetry
service and every OLA reconnect.** In that time the status is not ok and says
why (for example "amplifier readings not fresh: the OLA is not set up for the
amplifier readings (configuring)"). Nothing is reset because of it. A radar
that is running then gets no exemption: it is stopped like under any other
not-ok status, 3 s + 5 s after the last reading, because a radar transmitting
with no amplifier readings is exactly what must not happen without the
override. The manager resumes it when the readings are back.

**At the start** (every boot, every restart) the watcher publishes nothing
until it has judged three readings, or for 3 s if none come. So its first
status is already a judgement, a restarted watcher does not report "not ok" in
passing, and an amplifier found hot or enabled is never reported ok first.
The rule for an enabled amplifier applies from the first reading on.

**When it stops** it writes a last status with `ok: false` and "the safety
watcher is not running". If it is killed instead, the file just gets old, and
readers must refuse it by age (the manager: 5 s).

## Amplifier enabled with no radar: the reset tool

Every UHD initialisation enables the amplifier for about 0.5 s, including the
reset tool's own. So the tool runs only on evidence, and never blindly:

1. Current above 0.5 A in every reading for 5 s, at least 3 readings, no radar
   process during that time. A shorter pulse, or a radar appearing, ends the count.
2. The status goes not ok and tripped, and the watcher runs
   `amp_safe --timeout 30 --state-file <state_dir>/sdr-state`. The watcher is
   the one owner of this rule.
3. While the tool runs, nothing is judged. After it, three readings at
   shutdown current end the matter, whatever the tool said.
4. If the current stays high, the tool runs again: 2 s after `ASSUME_ENABLED`
   (exit 3: the tool itself may have left the amplifier enabled), 5 s after
   anything else. `OK` (exit 0) gets the same 5 s for the current to show it.
   Every other result (`NOT_FOUND` 2, `TIMEOUT` 4, `FAILED` 5, `BUSY` 6,
   `AMBIGUOUS` 7, `STATE_FILE` 8, `USAGE` 64) changed nothing. An `amp_safe`
   that ends with any other status, or is killed, counts as `ASSUME_ENABLED`.
5. Still enabled after `stuck_after_attempts` (3) runs: **"AMPLIFIER STUCK
   ENABLED"**, an urgent event, latched, with one run every `stuck_retry_s`
   (60 s) for as long as it lasts. About 20 to 30 s pass between the first
   enabled reading and this state when each run takes a few seconds (a first
   contact with the SDR after power-up takes 17 s per run). Only a technician
   cutting the power helps then.
6. It ends by itself when the current is back at the shutdown level in three
   readings (also when the 28 V goes away).

The tool never runs while a radar process is alive (a radar that should not be
there is stopped first, by the not-ok rule), and never without a reading at
most 3 s old that says "enabled". So with telemetry lost the state stays as it
was, latched, and nothing is tried until readings return.

`amp_safe` returns within its `--timeout`. If it has not after 35 s, the
watcher kills the script (not the tool under it) and takes it as
`ASSUME_ENABLED`. When the watcher itself is stopped or killed while the tool
runs, the tool is left to finish: the unit has `KillMode=process`, because
killing the tool during the SDR's initialisation leaves the amplifier enabled.

## Status file

`/run/radar-runner/user/watcher.json`, rewritten every 0.5 s. Read it with
`radar_runner.common.status.read_status(path, 5.0)`: a file that is old, from
another boot or damaged means "not ok".

```
ok               true: the radar may run
tripped          true: a limit was exceeded or the amplifier is enabled with no radar
reason           the most urgent problem, as text; "" when ok
problems         all of them: condition, text, trip
warnings         warning, text, since_uptime_s
conditions
  telemetry        state starting | ok | lost | settling; reason; overridden; reading_age_s
  amp_temp,        state ok | warn | trip; value; since_uptime_s
  case_temp
  voltage          state ok | unpowered | trip; value; since_uptime_s
  amp_enabled      state idle | pending | resetting | stuck; value (A); tool_runs; tool_running; next_run_in_s
amp              the reading that was judged: temp_c, case_temp_c, current_a, voltage_v, sample_uptime_s
pi               soc_temp_c, throttled
radar            running, pids, exe; stop idle | interrupted | killed
reset_tool       running; command; state_file; runnable; last: time_utc, uptime_s, code, name, text, duration_s
override         no_amp_telemetry, file
limits           the levels in use
counters         readings, trips, clears, telemetry_losses, stalls, tool_runs, tool_ok, radar_sigint, radar_sigkill
loop             last_gap_s, longest_gap_s between the watcher's own rounds
events           file, written, dropped, error
running          false in the last status of a watcher that was stopped
```

The manager's gate needs `ok` true and `tripped` false and shows `reason`.

## Event log

`/var/lib/radar-runner/user/watcher-events.jsonl`: one JSON object per line,
appended and synced, in the same place and layout as the manager's
`manager-events.jsonl` (`time`, `time_utc`, `uptime_s`, then the event). At
5 MB the file is renamed to `.1` and a new one started. Every event is also a
line in the journal (`journalctl -u radar-watcher`).

Each event has `event`, `severity` (`info`, `warning`, `urgent`) and `text`;
most have `condition` and the readings. Events: `start`, `stop`, `trip`,
`clear`, `not_ok`, `ok`, `unpowered`, `powered`, `warning`, `warning_cleared`,
`reset_tool`, `reset_tool_result`, `stuck`, `radar_stop`, `radar_kill`,
`radar_stopped`, `signal_failed`, `reset_tool_left`, `stall`. Urgent are: an
over-temperature trip, `stuck`, a radar that survives SIGKILL, and a signal
that could not be sent.

## The override

"Run without amplifier telemetry" is the file
`/var/lib/radar-runner/user/override-no-amp-telemetry`, set and cleared by the
manager's `radarctl override no-amp-telemetry on|off`. Its existence is the
override; it survives reboots. With it, missing telemetry does not make the
status not ok, and the status carries a warning that says so. Trips that were
there stay until readings show that they are over.

## Bench work: what the watcher does to a radar that the manager did not start

It treats every process whose executable is `~/orca/sdr/build/radar` the same,
whoever started it: `pa_run.py`, the HIL harness, a shell. Other programs that
use the SDR (UHD tools, `gpio_hold`, ORCA's test binaries) are not radar
processes: they are never signalled.

* `pa_guard.py` and `pa_run.py` need the OLA's port, so the telemetry service
  must be stopped for them. The watcher's status is then not ok, and **it
  stops every radar about 5 s after it starts.** Stop the watcher too:

  ```
  sudo systemctl stop radar-watcher radar-telemetry     # until the next boot, or until: start
  ```

  (`pa_guard.py` then watches the amplifier, as before.) Setting the override
  instead also keeps the watcher's hands off, but then nothing here watches.
* With the telemetry service running and the 28 V on, a radar started by hand
  is left alone while the readings are good, and stopped at a trip.
* With the 28 V off the status is not ok and a radar is stopped, unless
  `watcher.allow_unpowered: true` is set in `/etc/radar-runner/local.yaml`.
* A UHD tool that leaves the amplifier enabled for more than 5 s (the bench
  notes: `uhd_usrp_probe` 1.7 s, `gpio_hold --high` 0.5 s; `gpio_hold` holding
  the pin low on purpose: as long as it runs) makes the watcher run the reset
  tool. While that program has the SDR the tool reports `BUSY` and changes
  nothing; after three tries the state is "stuck" and an urgent event is
  logged. The program is not stopped.
* A radar run as root is not seen (its `/proc` entry is not readable) and
  could not be signalled.

## Config

Section `watcher` of `config/radar-runner.yaml`, with every default and a
comment per key; per-unit values go in `/etc/radar-runner/local.yaml`.
Restart the service after a change. Settings that cannot work (a clear level
above the trip level, fewer than 2 readings to trip, ...) are replaced by the
defaults and reported in `config_warnings` in the status file.

By hand, with its files somewhere else and a stand-in for the reset tool:

```
cd ~/englacial-radar-runner
.venv/bin/python -B -m radar_runner.watcher --local-config my.yaml
```

where `my.yaml` sets `paths.run_dir`, `paths.state_dir`,
`watcher.telemetry_status_file: /run/radar-runner/user/telemetry.json` and
`watcher.reset.command`. **Without the last one a watcher run by hand runs the
real reset tool.**

## Tests

```
.venv/bin/python -B -m pytest tests/watcher     # about 1 minute; touches no hardware and no installed service
```

`test_watcher_logic.py` drives the rules with injected readings in simulated
time; `test_watcher_service.py` uses real files, a stand-in `amp_safe` and
copies of `sleep` named `radar`, and runs the service once as a real process.

## Not yet verified on the hardware

Everything that needs the real reset tool or a real radar: the tool run from
the watcher (its time, the result, the current afterwards), `BUSY` while ORCA
runs, SIGINT and SIGKILL to the real radar and the state of the amplifier
after each, the unit under systemd (the tool surviving a stop of the watcher),
and the stage's "done when" items: an amplifier deliberately left enabled
returned to shutdown while running and at boot, the watcher killed and
restarted, the OLA unplugged.
