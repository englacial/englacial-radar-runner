# Raw GPS log for PPK

While the radar records, the GPS receiver's raw observations (UBX RXM-RAWX:
pseudorange, carrier phase, Doppler; RXM-SFRBX: the broadcast navigation data)
are logged into the collection's directory, for post-processed kinematic (PPK)
positions. Stage 6 of the plan.

**Status: tested against stand-ins (synthetic UBX through a private gpsd run by
gpsfake) only. The receiver does not send RXM-RAWX or RXM-SFRBX until it is
provisioned for them (below), and no collection with real raw data has been
made yet.**

## How it works

* gpsd stays the receiver's only owner. The logger (`logger.py`) is an
  ordinary gpsd client that uses gpsd's raw pass-through
  (`?WATCH={"enable":true,"raw":2}`) and sends nothing else.
* The radar manager's runner starts one logger per collection just before it
  launches the radar, and signals it (SIGTERM) as soon as the radar process is
  gone. It is a process of its own, at a lower priority (nice 5): it cannot
  delay, stop or fail a radar run or its stop, and the stop of the radar
  (`radarctl stop --wait`, the power service's hook) does not wait for it. At
  the end of the run's bookkeeping the runner waits at most
  `manager.gps_log.stop_wait_s` (1 s) for it; by then it has normally exited,
  and if not it is killed. It also exits when the manager dies.
* gpsd restarting or the receiver unplugged (systemd then stops gpsd) ends the
  connection; the logger waits a second, connects again and carries on in the
  same file (a message cut off by the lost connection is dropped, not
  written). The summary counts the connections and the longest time without a
  message, from the start of the run to its end.
* An error inside the logger's loop (a write error, a full or failing drive,
  anything not foreseen) is counted (`loop_errors`), reported, and the loop
  carries on after a pause. Its own log (`gps_raw.log`) has each text at most
  once a minute.
* The file is a plain u-blox binary log: whole UBX messages, exactly as the
  receiver sent them, in order, nothing added. gpsd's own JSON and anything
  that is not UBX are left out; so are the UBX messages not in
  `manager.gps_log.messages` (default RXM-RAWX, RXM-SFRBX, NAV-PVT and
  NAV-TIMELS; `[]` keeps every UBX message).
* Power cuts: every batch is one append; a thread forces the file to the drive
  every `fsync_interval_s` (10 s), so reading gpsd never waits for the drive.
  After a hard cut the file holds whole messages up to the last sync, and
  possibly more, the last one perhaps cut off. `check` (below) reads such a
  file up to the last whole message (tested here on a cut file). RTKLIB's
  convbin should too: its UBX reader looks for the sync bytes of each message
  and stops at the end of the file (from its source; convbin is not installed
  here, so not tested).

Load: the logger blocks in recv(2) between packets and reads only message
headers (and one byte per RXM-RAWX measurement for the carrier-phase count);
no checksums are computed while logging. **Its processor load during a
collection has not been measured on the Pi yet** (see "Testing on the unit").

## In the collection

```
gps_raw.ubx     the log
gps_raw.log     the logger's own messages (connections, errors)
gps_log.txt     created empty by ORCA; not used
```

`collection.json` gets `gps_raw` at the end of the run (from the logger's
summary; nothing is read back from the file):

```
file, bytes
messages              count per message name
rawx_epochs           RXM-RAWX epochs; measurements, carrier_phase_valid (cpValid set)
no_raw_data           true when no RXM-RAWX epoch arrived; the flag "no_raw_gps" is then also in "flags"
first_rawx, last_rawx receiver time (GPS week, tow_s, leap_s, utc) and when it reached the Pi
                      (received_utc on the Pi's clock, received_uptime_s)
gpsd_connects, gpsd_disconnects, longest_gap_s, skipped_bytes
rawx_every            the receiver setting this unit is meant to have (manager.gps_log.rawx_every)
error                 "" or the last problem
```

A collection cut off by a power cut has `"end": null` and no `gps_raw`; `check`
counts the file instead.

**Tying the log to the radar records.** Already in the collection: the Pi's
clock on every line of `uhd_stdout.log` (ORCA's per-pulse lines included) and
on the steps in `collection.json` (`created`, `recording_started`, `end`, with
uptime). Added here: the receiver time of the first and last raw epoch with the
Pi's clock and uptime on arrival, and NAV-PVT (receiver time, position, 10 Hz)
in the log itself. The Pi's clock follows GPS through chrony (tens of
milliseconds); the telemetry service's `gps` log (one row a second with the
Pi's clock, uptime and gpsd's fix time, per boot under
`/mnt/nvme/radar-runner/telemetry/`) covers clock steps during a run. The
radar's own sample clock is not GPS-locked until stage 8 (PPS).

## Checking a log, and RINEX

```
.venv/bin/python -B -m radar_runner.gpsraw check <collection>/gps_raw.ubx [--json]
```

counts the messages (with checksums), the RXM-RAWX epochs and measurements
with a valid carrier phase, per system, and gives the receiver time of the
first and last epoch. Exit status 0 if there is carrier phase, 1 if not. It
reads the whole file in Python: a long log takes about a minute of one
processor core. **Do not run it during a collection.**

RINEX: RTKLIB's `convbin` (not installed on the unit: `sudo apt install
rtklib`, Ubuntu's RTKLIB 2.4.3; or on a laptop, where RTKLIB "demo5" from
rtklibexplorer is the better choice for the ZED-F9P):

```
convbin -r ubx -v 3.04 -od -os -oi -ot -ol -o gps_raw.obs -n gps_raw.nav gps_raw.ubx
```

`-r ubx` the input format, `-v 3.04` RINEX 3, `-od -os` Doppler and signal
strength, `-oi -ot -ol` ionosphere, time and leap-second parameters in the
navigation file. The `.obs` file then has carrier-phase (`L..`) columns for
both frequencies of each system. RTKLIB's `rnx2rtkp`, RTKPOST or
another PPK program takes it with a base station's or a CORS's observations.
Not run here: RTKLIB is not installed and nothing was installed for this.

gpsd's `gpsrinex` is no substitute: it writes observations only (no
navigation file), and only from a live gpsd fed with the log (`gpsfake`): its
`-F` input (gpsd JSON made with `gpsdecode -j`) lost about half of the lines
in a test here.

## The receiver

`setup/gps_receiver.py --apply` provisions RXM-RAWX at every
`manager.gps_log.rawx_every`-th epoch and RXM-SFRBX on USB, in all three
layers (`setup/SYSTEM_CHANGES.md`, section 11). `setup/check.py --receiver`
checks both; `setup/gps_receiver.py --comms` shows the USB transmit buffer's
use and peak and the bytes per second.

## Settings

`manager.gps_log` in `config/radar-runner.yaml`: `enabled`, `file`,
`messages`, `fsync_interval_s`, `stop_wait_s`, `rawx_every`. gpsd's address is
`telemetry.gps.host` and `port`.
