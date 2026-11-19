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
exists whose executable is the radar binary (looked up in `/proc`). It never
reads the manager's status, does not need the manager to be running, and stops
a radar with signals of its own. It owns no device.

**`setup/setup.sh` installs and enables it but does not start it.** The first
start is by hand with someone watching the amplifier
(`sudo systemctl start radar-watcher`), because from then on it may run the
real reset tool. After that it starts at every boot.

| File | What |
|---|---|
| `logic.py` | Every decision, as a state machine without I/O or clock |
| `service.py` | Reads the inputs, sends signals, runs the reset tool, writes the status file and the event log |

| Reads | |
|---|---|
| `/run/radar-runner/user/telemetry.json` | amplifier readings (telemetry service) |
| `/var/lib/radar-runner/user/override-no-amp-telemetry` | the user's override (set through the manager) |
| `/var/lib/radar-runner/user/sdr-state` | the shared record: is the amplifier pin known to be safe (first line `safe`) |
| `/proc/<pid>/exe`, `/proc/<pid>/stat` | which processes are radar processes, and how old they are |
| `/sys/bus/usb/devices/*/idVendor`, `product` | is the SDR there, and has it been started since its power-up |

| Writes | |
|---|---|
| `/run/radar-runner/user/watcher.json` | status, twice a second |
| `/run/radar-runner/user/watcher-state.json` | what a restarted watcher must still know: trips, a stuck amplifier, "pin not known safe" (this boot only) |
| `/var/lib/radar-runner/user/watcher-events.jsonl` (and `.1`) | event log |
| `/run/radar-runner/user/watcher-reset-tool.out` | output of a reset tool run, while it runs |

`sdr-state` is written by `amp_safe` and the manager, never by the watcher.

## What counts as a usable reading

A reading is the `amp` block of `telemetry.json`. It is usable when all of
this holds; otherwise there is "no telemetry":

* the file is readable, from this boot and at most 3 s old (by uptime, not wall clock);
* `amp.fresh` is exactly `true` (the telemetry service's own judgement);
* `amp.sample_uptime_s` is at most 3 s back;
* current, voltage and amplifier temperature are plausible numbers, and so is
  the case temperature (`require_case_temp`). Plausible: -60 to 150 C, -1 to
  12 A, -2 to 40 V; the ADC's full scale is 409 C, 12 A and 45 V.

A reading counts once, however often the watcher sees it (the telemetry
service publishes a new one every 0.5 s).

**One reading never decides anything**, because the OLA's rows have no
checksum. **And one good reading in between hides nothing:**

* A limit trips when `trip_samples` (3) of the last `trip_window` (5) readings
  are beyond it, and those three span at least `trip_span_s` (1 s). At one
  reading per 0.5 s that is 1 to 2.5 s, nothing against the amplifier's thermal
  time (minutes). Two damaged readings among five do nothing; readings that
  alternate around the limit, or two of every three beyond it, trip.
* A trip clears after `clear_hold_s` (30 s) of readings on the good side of the
  recover level, without a single one in between that is not.
* A gap of more than 3 s between readings forgets the readings before it, and
  so does a stall of the watcher itself.
* Loss of telemetry is judged by time alone: no usable reading for more than
  `telemetry_lost_s` (3 s). After a loss, three usable readings in a row are
  needed before the status is ok again.

## Rules

| Condition | Rule | Status | Numbers from |
|---|---|---|---|
| Amplifier temperature or case temperature (each whenever it has a value, whatever the voltage reads) | warning at 50 C; **trip at 55 C**; clears below 50 C for 30 s | not ok, tripped | `pa_run.py` `TEMP_WARN_C`, `TEMP_STOP_C` (thermal design limit 55 C, derating from 60 C). Recover level and hold: chosen here |
| 28 V rail at 20 V or more | **trip outside 26-30.5 V**; clears inside 26.5-30.0 V for 30 s | not ok, tripped | plan; `pa_run.py` `VOLT_MIN`, `VOLT_STOP`; 30.0 is its `VOLT_MAX` |
| 28 V rail absent (below 20 V) | no trip, no action by itself. The radar may not run (`allow_unpowered: true` changes that, for bench work) | not ok, not tripped | `pa_run.py` `VOLT_POWERED`; the user's rule "no RF into an unpowered amplifier" |
| Enabled current together with a voltage below 20 V | the two readings contradict each other: warning, the radar may not run (also with `allow_unpowered`) | not ok, not tripped | chosen here |
| A limit not yet proven at the start (see "Restart") | the radar may not run | not ok, not tripped | chosen here |
| No telemetry for more than 3 s | the radar may not run, with the reason from the telemetry service | not ok, not tripped | plan ("a few seconds"); `pa_run.py` `STALE_S` was 2 s on raw rows |
| No telemetry, override set | not a problem; everything that can still be judged is | ok, warning | plan |
| Enabled current (above 0.5 A) for 5 s with no radar process | run the reset tool, see below | not ok, tripped | `pa_guard.py` `IDLE_ENABLED_TRIP_S`, `CURRENT_SHUTDOWN_MAX_A` (shutdown 0.156 A, enabled about 2.5 A) |
| No telemetry and no radar process for 20 s, and the pin not known to be safe | run the reset tool, see below | not ok, tripped | chosen here |
| Status not ok for 5 s with a radar process alive that is at least 65 s old | SIGINT; SIGKILL 5 s later; SIGKILL again every 5 s while it lasts | | see "Stopping a radar" |

A falling rail during a power loss passes through 20-26 V. If three readings
catch it there, that is a supply trip in the event log at the end of a day;
the watcher no longer reads the power service's status to tell the two apart.
Warnings about the Pi (temperature, throttling), about cold and about the
telemetry loop are not the watcher's: they belong to telemetry and alerts.
`pa_run.py`'s trip on the two temperature sensors disagreeing is not carried
over: the bench showed 11 to 20 C between them in normal operation.

**Telemetry is absent for about 12 s after every start of the telemetry
service and every OLA reconnect.** In that time the status is not ok and says
why (for example "amplifier readings not fresh: the OLA is not set up for the
amplifier readings (configuring)"). Nothing is reset because of it: the rule
for a pin that is not known safe needs 20 s. A radar that is running then gets
no exemption beyond the two below, because a radar transmitting with no
amplifier readings is what must not happen without the override.

## Stopping a radar

The manager stops the radar when the status goes not ok. The watcher does it
only if a radar process is still there afterwards:

* **Grace period:** `stop_grace_s` (5 s) of "not ok". Seconds not ok count up;
  seconds ok count down at a quarter of that rate. So telemetry that comes and
  goes (2 s of readings, 4 s without) does not start the 5 s afresh each time,
  and 20 s of ok do.
* **Set-up allowance:** a radar process younger than `radar_setup_s` (65 s) is
  never signalled; it is signalled when it reaches that age if the status is
  still not ok. ORCA has no SIGINT handler until the SDR is set up (firmware
  and FPGA load: 17 s at a first contact after power-up), and UHD holds the
  amplifier pin low during the set-up: a radar that dies there leaves the
  amplifier enabled. The status shows such a radar under `radar.setting_up`
  with the warning `radar_setting_up`. There is no exception for an
  over-temperature or stuck trip: the allowance is itself the bound, and the
  manager does not start a radar while the status is not ok. The 65 s are the
  manager's `orca.start_timeout_s` (60 s, for which it holds a stop until ORCA's
  start line) plus its `stop_timeout_s` (3 s) and a margin: until then the stop
  is the manager's, and the watcher's SIGINT cannot land in ORCA's set-up while
  the manager is still waiting for it to end. A manager that dies takes its
  radar with it (`setpriv --pdeathsig KILL`).
* SIGINT, then SIGKILL after `radar_exit_s` (5 s, `pa_guard.py`
  `RADAR_SIGINT_GRACE_S`), then SIGKILL every `kill_retry_s` (5 s).
* **After any signal from the watcher the pin counts as not known safe**
  (`conditions.pin.signalled_by_watcher`), until the reset tool has verified it
  or the readings show shutdown current with no radar running.

## Amplifier enabled with no radar: the reset tool

Every UHD initialisation enables the amplifier for about 0.5 s, including the
reset tool's own. So the tool runs on evidence:

1. Current above 0.5 A in every reading for 5 s, at least 3 readings, no radar
   process during that time. A shorter pulse, or a radar appearing, ends the
   count. **During these 5 s the status stays ok.** That is deliberate: every
   start of the SDR by any tool is such a pulse, and the manager's gate would
   otherwise refuse or stop a radar for each of them. So an amplifier found
   enabled is reported ok for up to 5 s before it is reported tripped.
2. The status goes not ok and tripped and is published. One round (0.5 s)
   later the watcher runs `amp_safe --timeout 30 --state-file <state_dir>/sdr-state`.
   If another `amp_safe` holds the tool's lock at that moment (the manager's,
   or the power service's hook), the watcher does not queue behind it: it
   looks again every round and decides from the readings of that round.
3. While the tool runs, nothing is judged. After it, three readings in a row
   at shutdown current end the matter, whatever the tool said.
4. If the current stays high, what happens next depends on whether the tool ran:
   * **It ran** (`OK` 0, or `ASSUME_ENABLED` 3: it may itself have left the
     amplifier enabled): again 2 s after exit 3, 5 s after exit 0. After
     `stuck_after_attempts` (3) such runs: **"AMPLIFIER STUCK ENABLED"**,
     urgent, latched, one run every `stuck_retry_s` (60 s). Only a technician
     cutting the power helps then.
   * **It could not run** (`NOT_FOUND` 2, `TIMEOUT` 4, `FAILED` 5, `BUSY` 6,
     `AMBIGUOUS` 7, `STATE_FILE` 8, `USAGE` 64: nothing was changed): again
     after 5 s. These tries cost no enable pulse and say nothing about whether
     a reset would help, so they never count towards "stuck". If nothing but
     such results has come for `blocked_after_s` (30 s): **"AMPLIFIER ENABLED
     ... AND THE RESET TOOL CANNOT RUN"** (`BUSY`: the SDR is held by another
     program), urgent, state `blocked`, one try every `blocked_retry_s` (15 s).
   * An `amp_safe` that ends with any other status, or is killed, counts as
     `ASSUME_ENABLED`.
5. It ends by itself when the current is back at the shutdown level in three
   readings (also when the 28 V goes away).

The tool never runs while a radar process is alive, and under this rule never
without a reading at most 3 s old that says "enabled".

**Without telemetry** the current cannot decide. Then, and only then, the
record decides: if there has been no usable reading for `blind_delay_s` (20 s)
and no radar process for as long, and the pin is not known to be safe (the
watcher itself signalled a radar, or the first line of `sdr-state` is not
`safe`), the status goes tripped with an urgent event, and the tool is run
once; then again every `blind_retry_s` (300 s) while all of that still holds.
It ends when the tool reports OK (the record then says `safe`), when readings
return (the current decides again) or when a radar process appears. This also
holds with the override set. The tool is never run this way when the SDR is in
its boot loader (USB vendor 2500 with the product string "WestBridge": it has
not been initialised since its power-up, its pins float, the control board
holds shutdown) or is not on USB at all.

If `amp_safe` cannot write the state file (`STATE_FILE`, exit 8: a card that
has gone read-only) it does not start the tool. The watcher's next runs are
then without `--state-file`: an enabled amplifier matters more than the record.

`amp_safe` returns within its `--timeout`. If it has not after 35 s, the
watcher kills the script (not the tool under it) and takes it as
`ASSUME_ENABLED`; a run that was never reported at all is written off after
45 s. When the watcher itself is stopped or killed while the tool runs, the
tool is left to finish: the unit has `KillMode=process`, because killing the
tool during the SDR's initialisation leaves the amplifier enabled.

## Start, restart, stop

* At the start the watcher publishes nothing until it has judged three
  readings, or for 3 s if none come. So its first status is already a judgement.
* **A restart loses nothing within a boot.** Trips, a stuck or resetting
  amplifier, "the watcher signalled a radar" and "the record cannot be written"
  are kept in `/run/radar-runner/user/watcher-state.json` and restored. A trip
  that was in force stays in force until its 30 s of good readings. The file of
  another boot is ignored.
* **Without that file** (after a reboot) a limit is "unproven" until three of
  five readings are on the good side of its recover level: at 53 C the watcher
  cannot know whether a stop was in force, so the status is not ok (not
  tripped) until the reading is below 50 C. Once proven, 50 to 55 C is only a
  warning, as always.
* When it is stopped it writes a last status with `ok: false` and "the safety
  watcher is not running". If it is killed instead, the file just gets old, and
  readers must refuse it by age (the manager: 5 s).

## Status file

`/run/radar-runner/user/watcher.json`, rewritten every 0.5 s. Read it with
`radar_runner.common.status.read_status(path, 5.0)`: a file that is old, from
another boot or damaged means "not ok".

```
ok               true: the radar may run
tripped          true: a limit was exceeded, the amplifier is enabled with no radar, or its pin is not known safe
reason           the most urgent problem, as text; "" when ok
problems         all of them: condition, text, trip
warnings         warning, text, since_uptime_s
conditions
  telemetry        state starting | ok | lost | settling; reason; overridden; reading_age_s
  amp_temp,        state ok | warn | trip | unproven; value; since_uptime_s
  case_temp
  voltage          state ok | unpowered | trip | unproven; value; since_uptime_s; contradiction
  amp_enabled      state idle | pending | resetting | blocked | stuck; value (A); tool_runs; tool_ran; next_run_in_s
  pin              state ok | unknown; record_safe; signalled_by_watcher; sdr ready | bootloader | absent | unknown
amp              the reading that was judged: temp_c, case_temp_c, current_a, voltage_v, sample_uptime_s
radar            running, pids, setting_up, exe; stop idle | interrupted | killed
reset_tool       running; command; state_file; runnable; last: time_utc, uptime_s, code, name, text, duration_s
override         no_amp_telemetry, file
limits           the levels in use
counters         readings, trips, clears, telemetry_losses, stalls, tool_runs, tool_ok, radar_sigint, radar_sigkill
loop             last_gap_s, recent_longest_gap_s (last one to two minutes), longest_gap_s of the watcher's own rounds
events           file, written, dropped, error
running          false in the last status of a watcher that was stopped
```

The manager's gate needs `ok` true and `tripped` false and shows `reason`.
`sdr` is looked at only while there is no usable reading.

## Event log

`/var/lib/radar-runner/user/watcher-events.jsonl`: one JSON object per line,
appended and synced, in the same place and layout as the manager's
`manager-events.jsonl` (`time`, `time_utc`, `uptime_s`, then the event). At
5 MB the file is renamed to `.1` and a new one started. Every event is also a
line in the journal (`journalctl -u radar-watcher`).

Each event has `event`, `severity` (`info`, `warning`, `urgent`) and `text`;
most have `condition` and the readings. Events: `start`, `stop`, `trip`,
`clear`, `not_ok`, `ok`, `unpowered`, `powered`, `warning`, `warning_cleared`,
`reset_tool`, `reset_tool_result`, `stuck`, `blocked`, `radar_stop`,
`radar_kill`, `radar_stopped`, `signal_failed`, `reset_tool_left`, `stall`.
Urgent are: an over-temperature trip, a pin that is not known safe without
telemetry, `stuck`, `blocked`, a radar that survives SIGKILL, and a signal
that could not be sent.

## The override

"Run without amplifier telemetry" is the file
`/var/lib/radar-runner/user/override-no-amp-telemetry`, set and cleared by the
manager's `radarctl override no-amp-telemetry on|off`. Its existence is the
override; it survives reboots. With it, missing telemetry does not make the
status not ok, and the status carries a warning that says so. Trips that were
there stay until readings show that they are over, and the rule for a pin that
is not known safe still applies.

## Bench work: stop two units first

**For a bench session with `pa_guard.py`, `pa_run.py` or the HIL harness:**

```
sudo systemctl stop radar-watcher radar-telemetry      # until the next boot, or until: start
```

Both, and the watcher first. The bench tools need the OLA's port, so the
telemetry service has to be stopped for them; with only that one stopped the
watcher's status is not ok and **it stops every bench radar** once the radar
is 65 s old, and after 20 s without telemetry it runs the reset tool if the
record does not say `safe`. The watcher is enabled, so it is back after every
reboot: stop the two again.

What it does to programs that the manager did not start, while it runs:

* Every process whose executable is the radar binary (`~/orca/sdr/build/radar`)
  is a radar process, whoever started it: `pa_run.py`, `run.py`, the HIL
  harness, a shell. Other programs that use the SDR (UHD tools, `gpio_hold`,
  ORCA's test binaries) are not: they are never signalled.
* With the telemetry service running and the 28 V on, a radar started by hand
  is left alone while the readings are good, and stopped at a trip.
* With the 28 V off the status is not ok and a radar is stopped, unless
  `watcher.allow_unpowered: true` is set in `/etc/radar-runner/local.yaml`.
* A UHD tool that leaves the amplifier enabled for more than 5 s makes the
  watcher run the reset tool. While that program has the SDR the tool reports
  `BUSY` and changes nothing; after 30 s of that the state is `blocked` and an
  urgent event is logged. The program is not stopped.
* A radar run as root is not seen (its `/proc` entry is not readable) and
  could not be signalled.

## Config

Section `watcher` of `config/radar-runner.yaml`, with every default and a
comment per key; per-unit values go in `/etc/radar-runner/local.yaml`.
Restart the service after a change. Settings that cannot work (a clear level
above the trip level, fewer than 2 readings to trip, ...) are replaced by the
defaults and reported in `config_warnings` in the status file.

`radar_exe: manager` (the default) takes the radar binary from the manager's
own settings (`manager.orca`: `dir`, `radar_cwd`, first word of
`radar_command`), so that the watcher and the manager cannot mean different
programs; a full path overrides that. The path in use is `radar.exe` in the
status file.

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
runs, the lock probe against a real `amp_safe`, SIGINT and SIGKILL to the real
radar and the state of the amplifier after each, whether 65 s covers ORCA's
set-up in every case, the SDR's USB product string before and after its first
start, the unit under systemd (the tool surviving a stop of the watcher), and
the stage's "done when" items.
