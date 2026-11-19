# Telemetry service

Reads the sensors, logs them, and publishes the latest values for the other
services. It runs as user `englacial` on the repository's Python environment
(`systemd/radar-telemetry.service`). It is the only program that may open the
OLA's serial port.

| Source | Read from | Default rate | Log size per 24 h |
|---|---|---|---|
| `ola` | OpenLog Artemis on USB serial: amplifier temperature, current and 28 V rail (ADS1015), TMP117 on the amplifier case, IMU, the OLA's GPS columns and its own clock | every row, about 10 per second | 210 MB |
| `gps` | gpsd (`?POLL;` on its JSON socket): time, fix mode, position, speed, heading, error estimates | 1 per second | 14.5 MB |
| `ups` | the power service's status file `/run/radar-runner/power.json`: capacitor voltage and current, power state | 1 per second | 5.8 MB |
| `pi` | `/sys` and `/proc`: SoC temperature, throttling flags, load, memory, free space, drive temperatures, fan, clock state | every 2 s | 4.5 MB |

About 235 MB per 24 h in all, 100 MB for a 10 h day (measured on the bench,
2026-11-19). The service uses 0.6 % of one processor core and 20 MB of memory;
gpsd, which it keeps busy, another 0.75 %.

## Status file

`/run/radar-runner/user/telemetry.json`, rewritten twice a second. Read it with
`radar_runner.common.status.read_status(path, max_age_s)`, which refuses a file
that is old or from another boot.

```
state            "ok", or "degraded" with the reasons in `problems`
amp              the amplifier readings, for the safety watcher:
  fresh            true only if all three hold: the OLA is set up for the differential readings,
                   the newest reading is at most fresh_limit_s old, and the service's own loop
                   has not just stalled for longer than that
  reason           why not, when fresh is false
  temp_c, current_a, voltage_v, case_temp_c
  sample_uptime_s  the earliest moment the reading can have arrived (see below); age_s counts from it
  sample_time_utc
sources.<name>   for ola, gps, ups, pi:
  health           "ok", "stale", "missing" or "error"
  reason           short text when not ok
  age_s, sample_uptime_s
  values           the latest value of every reading
sources.ola.retry  after a failure: failures in a row, what is waited for, seconds to the next attempt
sources.gps      also no_position_s, receiver_identified, no_position, receiver_check (below, "GPS receiver")
logging          location "primary", "fallback" or "off"; writer "running", "stalled" or "dead";
                 directory, file names, rows written, waiting and dropped
clock            steps of the system clock seen since the service started; `synced`
loop             last_gap_s, recent_longest_gap_s (last one to two minutes), longest_gap_s: time
                 between two rounds of the service loop (0.25 s when all is well); slowest step
boot_count       the boot counter used in the file names
```

Ages are counted in uptime, not wall-clock time, so they stay right when GPS or
the network sets the clock. A reader that acts on the amplifier values must
check three things: `read_status(...).ok`, `amp.fresh`, and, if it wants its own
limit, `uptime now - amp.sample_uptime_s`.

**How old an amplifier reading is.** The service reads the OLA's port until it
is empty in every round. A row read in such a round arrived after the round
before it, and that is all that is certain; so `amp.sample_uptime_s` is the
time of the round before (normally 0.25 s earlier), and `amp.age_s` is an upper
limit, 0.25 to 0.75 s when all is well. After a stall of the loop the rows that
piled up come out together: they are logged with times dated back by the OLA's
own clock, and the amplifier status shows the newest of them as old as the
stall was long, so not fresh. If a round cannot empty the port (more than
260 kB waiting), nothing is handed out until one does.

`amp.fresh` is false while the OLA is being set up (about 12 s after every
start of the service or reconnect of the OLA), when the OLA does not answer or
has restarted, when its port is gone, and in the round after a stall.

**The OLA is not reset in a loop.** Opening its port resets it (and it then
configures the GPS receiver again). After a failure the service waits before
the next reopening, and before the next try at the menus: 5 s and 30 s at
first, doubling with every failure in a row up to an hour, and short again only
after five minutes of good rows. A broken OLA is therefore reset about 25 times
in the first half day and once an hour after that. Unplugging it for 10 s or
more and plugging it in again is tried at once. `sources.ola.retry` shows the
state. systemd spaces restarts of a service that keeps dying in the same way,
from 2 s up to 5 min.

To look at it: `python3 -m json.tool /run/radar-runner/user/telemetry.json`

## Logs

```
/mnt/nvme/radar-runner/telemetry/b00042/ola_b00042_00001_20261119.csv
                                 |          |      |     UTC date when the file was started
                                 |          |      sequence number within the boot, per source
                                 |          boot counter
                                 one directory per boot (with the kernel's boot ID in .boot_id)
```

* CSV with a header line. Every row starts with `time_utc` (wall clock) and
  `uptime_s` (seconds since boot); the `ola` rows also carry the OLA's own clock
  (`rtcDate`, `rtcTime`) and its GPS time, the `gps` rows gpsd's fix time.
  The Pi's clock has no battery, so `time_utc` can be wrong until GPS or the
  network has set it (`clock_synced` in the `pi` log); `uptime_s` and the file
  names always give the true order.
* Time stamps: the newest row of a round gets the time of the read, the rows
  before it in the same round are dated back by the OLA's own clock (0.1 s
  apart). They are good to about the loop interval (0.25 s). `gps`, `ups` and
  `pi` rows carry the time they were read.
* A new file is started at every start of the service, at midnight UTC, when
  the set of columns changes (the OLA's header), when the clock is stepped to
  another date, and after a write error. Files are never reopened.
* A boot directory is only written by the boot that made it. If the boot
  counter was lost and a number comes up again, the next free number is used
  (and the counter raised to it).
* A hard power cut loses at most about 35 s of rows: they are written out
  every `flush_interval_s` (1 s) and each file is synced to the drive every
  `fsync_interval_s` (30 s), one file per second. A normal shutdown (the power
  service's, or `systemctl stop`) writes out and syncs everything. What is on
  the drive is readable; only the last line of a file can be cut off.
  **Readers must drop a last line that does not end with a newline.**
  The 30 s is cautious: a sync took 2 ms with the drive idle, but with the
  radar recording it can carry the radar's data along. **To measure in stage 4.**
* If `/mnt/nvme` is not mounted or not writable, the logs go to
  `/var/lib/radar-runner/user/telemetry-fallback/` on the SD card, limited to
  `fallback_max_mb` (oldest files deleted first), and return to the drive
  within 10 s of it becoming usable. Files stay where they were written.
* A drive that is mounted but fails (I/O errors, read-only, full) is left alone
  for a minute, then two, doubling up to an hour, and the logs stay on the
  card meanwhile. No row is lost in the change: a row leaves the buffer only
  when it is in a file. If nothing can be written at all, 20000 rows per source
  are kept in memory (half an hour of OLA rows) and newer ones are dropped and
  counted (`logging.rows_dropped`).
* `ola` columns: the OLA's own columns under the names in its header (they
  change if sensors are added), then `amp_temp_c`, `amp_current_a`,
  `amp_voltage_v`. `gps_Lat`/`gps_Long` are degrees x 1e7, `gps_Alt` mm.
* `gps` `sats_used` is mostly empty: gpsd reports the satellite count only in
  some answers. The OLA logs the same receiver's count as `gps_SIV`.
* `pi` `throttled` is the value of `vcgencmd get_throttled` (bits 0-3 now,
  16-19 since boot: under-voltage, frequency capped, throttled, soft
  temperature limit).

## GPS receiver

The service asks gpsd for positions and, while there is no position, for its
list of devices (`?DEVICES;`, which shows whether gpsd has identified the
receiver). The service itself sends nothing to the receiver. The one program it
starts is `gps.receiver_fix_command` (`setup/gps_receiver.py --apply --ram`):
15 s after every OLA start, and when gpsd has a receiver but no position (see
below). It is never waited for, runs in a session of its own, writes to this
service's journal, and is stopped after `receiver_fix_timeout_s`. It is started
in a step of its own, apart from the OLA's: nothing about it can affect the
amplifier readings. `""` switches it off.

If gpsd has the receiver but no new fix time comes out of it for 30 s,
`sources.gps.health` is `error` with the reason; that is what the receiver's
output overflow of 2026-11-16 looked like.

**No position.** On 2026-11-19, at the first boot after a power cut of several
minutes, gpsd delivered fix times with mode 0 for 77 minutes while the receiver
had a 3D fix: gpsd had not identified the receiver (empty `subtype`) and the
receiver sent it only NAV-TIMEGPS and NAV-DOP, no position message. Asking for
the receiver's version by hand fixed it at once. What gpsd does at its start,
and why that can end in this state, is in `setup/gps_receiver.py`; the way in is
inferred from that one occurrence, and a cold-boot test on the bench (several
minutes without power) is still to be done. gpsd clears the fix at every cycle,
so a fix time with mode 0 means that no position message came, and mode 1 a
position message without a fix. The service reports:

```
sources.gps.no_position_s        for how long gpsd has had a receiver without a 2D/3D fix (a second or
                                 less when all is well)
sources.gps.receiver_identified  gpsd's last DEVICES report: true, false, or null (no device or not known)
sources.gps.no_position          "" while no_position_s < no_position_warn_s (60 s), then why:
                                 "gpsd_unidentified"     gpsd has not identified the receiver: health "error"
                                 "no_position_messages"  identified, fix times come, mode 0: the receiver
                                                         sends no NAV-PVT (gpsd's enable lost): "error"
                                 "no_data"               no new fix time either: "stale", then "error" as above
                                 "no_fix"                mode 1: most likely no satellites (antenna indoors,
                                                         poor sky): health stays "ok", the reason says
                                                         "no position fix for N s"
sources.gps.receiver_check       runs, running, last_status (setup/gps_receiver.py's exit status),
                                 last_why, error (an internal error of this step), next_retry_at_no_position_s
```

A warning is logged once when `no_position` is set and an info line when the
position is back. Without a position the receiver command is run again: after
`no_position_retry_s` (120 s), then after twice the previous wait each time, up
to `no_position_retry_max_s` (30 min): at 2, 6, 14 and 30 minutes, then every
30 minutes; never more often than every 30 s. With an identified receiver that
has no satellites it only reads two settings of the receiver, so it is harmless
indoors.

To look at the receiver's side by hand:

```
setup/check.py --receiver     # reads CFG-UART1OUTPROT-NMEA and the transmit-buffer error flags; sets nothing
setup/gps_receiver.py         # identification, NAV-PVT and UART1 NMEA in RAM; sets nothing
```

Background: `setup/SYSTEM_CHANGES.md`, section 11.

## Config

Section `telemetry` of `config/radar-runner.yaml`, with every default and a
comment per key. Per-unit values (the amplifier calibration, another port) go
in `/etc/radar-runner/local.yaml`. Restart the service after a change.

| Key | Meaning |
|---|---|
| `data_root`, `data_mount` | log directory on the data drive, and the mount point that must be mounted for it to be used |
| `fallback_dir`, `fallback_max_mb` | SD card directory and its size limit |
| `flush_interval_s`, `fsync_interval_s` | how often rows are written and forced to the drive |
| `ola.port` | serial port of the OLA |
| `ola.log_interval_s` | 0 logs every row; 1.0 one row per second (21 MB per 24 h). The status file always has the newest row |
| `ola.fresh_s` | age limit of the amplifier readings for `amp.fresh` |
| `ola.silent_s` | no valid row for this long: reopen the port |
| `ola.reopen_s`, `ola.reopen_max_s`, `ola.retry_s`, `ola.retry_max_s`, `ola.stable_s` | the waits after a failure (see above) |
| `ola.temp_*`, `ola.current_*`, `ola.voltage_ratio` | calibration of the three ADC readings; where each number comes from is in the YAML file |
| `pi.interval_s`, `ups.interval_s`, `gps.interval_s` | sampling and logging intervals |
| `gps.no_data_error_s` | receiver present but no fix time for this long: `error` |
| `gps.receiver_fix_command`, `gps.receiver_fix_delay_s`, `gps.receiver_fix_timeout_s` | the receiver command, its delay after an OLA start, and its time limit |
| `gps.no_position_warn_s`, `gps.no_position_retry_s`, `gps.no_position_retry_max_s` | no position: warning, first retry of the receiver command, longest wait between retries |

## Bench work: stopping the service

While the service runs, nothing else can open the OLA's port: the bench tools
`pa_guard.py` and `pa_run.py` (and with them `hil_pa_run.sh` and the HIL
harness with the amplifier) fail with "device busy".

```
sudo systemctl stop radar-telemetry      # until the next boot, or until:
sudo systemctl start radar-telemetry
sudo systemctl disable --now radar-telemetry    # stays off across reboots; enable --now to undo
```

Stopping it makes the status file stale, so anything that requires fresh
amplifier telemetry (from stage 3: the safety watcher, and through it the
radar manager) will refuse to run the radar.

By hand, with output on the terminal and its files somewhere else:

```
cd ~/englacial-radar-runner
.venv/bin/python -B -m radar_runner.telemetry --local-config my.yaml
```

where `my.yaml` sets `paths.run_dir`, `paths.state_dir`, `telemetry.data_root`
and `telemetry.fallback_dir`.

## Logs of the service itself

`journalctl -u radar-telemetry` (warnings: `-p warning`). It logs changes, not
every reading: OLA connection and configuration, gpsd connection, log files
started, storage location changes, clock steps.
