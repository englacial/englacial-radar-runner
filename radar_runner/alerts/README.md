# Alerts

Sends alerts to ntfy: faults that the other services report, and a summary at
every boot. It runs as user `englacial` (`systemd/radar-alerts.service`).
Stage 5 of the plan.

**It is an observer.** It opens no device, signals no process and writes only
its own files. No other unit depends on it, and nothing else reads its files
(only `alertctl` and `setup/check.py`, by hand). If it crashes, hangs or fills
its queue, nothing else changes; systemd restarts it.

| File | What |
|---|---|
| `logic.py` | Every decision, without I/O or clock: the event rules, the conditions, the storm limit, the position and secret filter, the boot summary |
| `service.py` | The loop: reads the inputs, queues, sends, writes the status and state files |
| `queue.py` | The queue on disk |
| `tail.py` | Following an event log through its rotation |
| `ntfy.py` | One HTTPS request to ntfy, standard library only |
| `ctl.py` (`bin/alertctl`) | Status, queue and a test alert by hand |

| Reads (never writes) | |
|---|---|
| `/run/radar-runner/user/telemetry.json`, `watcher.json`, `manager.json` | status of the other services |
| `/run/radar-runner/power.json` | the power service's status (warnings, mode, `guard.last_shutdown`) |
| `/var/lib/radar-runner/user/watcher-events.jsonl` (and `.1`) | the safety watcher's event log |
| `/var/lib/radar-runner/user/manager-events.jsonl` (and `.1`) | the radar manager's event log |
| `/var/lib/radar-runner/user/boot_count.json` | the boot number, when telemetry has not published it yet |
| `/mnt/nvme/radar-runner/collections/` | the newest collection, once per boot, for the summary |
| `/etc/radar-runner/secrets.yaml` | `alerts: ntfy: {server, topic, token}` (through the config loader) |

| Writes | |
|---|---|
| `/run/radar-runner/user/alerts.json` | status, once a second |
| `/var/lib/radar-runner/user/alerts-state.json` | positions in the event logs, the storm limit, conditions, what the boot summary needs about the boot before |
| `/var/lib/radar-runner/user/alerts-queue/` | one file per alert waiting to be sent |
| `/var/lib/radar-runner/user/alerts-inbox/` | test alerts from `alertctl` (it deletes them once queued) |

The unit enforces this: `ProtectSystem=strict` with write access to the two
`user` directories only. A test runs the service under a Python audit hook and
fails on any write outside these files and on any signal sent.

## What becomes an alert

Priorities are ntfy's: **5 urgent** (max: breaks through on the phone), **4
high**, **3 default**, **2 low** (no sound). Every alert says the unit's host
name, when it was raised (and that the clock was not set, if so) and the uptime.

The plan's fault table, row by row. "After" is how long a condition must last.

| Fault (plan) | Source | Priority | Alert | |
|---|---|---|---|---|
| Amplifier over temperature (amplifier or case sensor) | `watcher-events`: `trip`, condition `amp_temp`/`case_temp` (urgent) | 5 | "URGENT: safety trip: amplifier temperature" | covered |
| ... 50 C warning before the stop | `watcher-events`: `warning` `amp_temp_high` | 3 | "safety watcher warning" | covered |
| ... trip over | `watcher-events`: `clear` | 2 | "safety trip over: ..." (only if the trip went out) | covered |
| 28 V out of range | `watcher-events`: `trip`, condition `voltage` (warning) | 3 | "safety trip: 28 V supply"; **not sent while `power.json` reports a loss of main power** (the 28 V falls with it at every shutdown) | covered |
| Amplifier drawing current with no radar process | `watcher-events`: `trip` `amp_enabled` (warning); then `stuck` or `blocked` (urgent); `reset_tool_result` not OK (warning); `trip` `pin` without telemetry (urgent) | 3, then 5 | "AMPLIFIER STUCK ENABLED", "AMPLIFIER ENABLED, RESET TOOL CANNOT RUN" | covered |
| Amplifier telemetry missing at start, or lost during a run | `watcher.json` `ok` false, with `reason` | 3 after 5 min | "radar kept off by the safety watcher" | covered |
| ... and the watcher had to stop or kill a radar itself | `watcher-events`: `radar_stop`, `radar_kill` (warning; urgent when SIGKILL did not help), `signal_failed` (urgent) | 3 / 5 | | covered |
| No PPS at the SDR | none yet (stage 8) | | | **not available yet** |
| GPS no fix or receiver missing | `telemetry.json` `sources.gps`: `health` not ok, or a `reason`. The title by `no_position`: `gpsd_unidentified` "GPS receiver sends no position (not identified by gpsd)", `no_position_messages` "GPS receiver sends no position messages", `no_fix` "GPS: no fix (antenna or sky?)"; else by `health`: `missing` "GPS: no receiver, or gpsd not answering", `stale` "GPS: no data from the receiver", `error` "GPS receiver not working" | 3 after 10 min | the title and telemetry's reason (never a reading) | covered |
| NVMe missing or not writable | `telemetry.json` `logging`: `location` not `primary`, or `writer` not `running`; and `manager.json` `not_ready` | 3 after 5 min (10 min) | "sensor logs not on the data drive", "radar not ready: data: ..." | covered |
| Disk nearly full | `telemetry.json` `sources.pi.values.nvme_free_gb` below 200, 100, 50 GB; below 25 GB | 3; 4 | "data drive below N GB free", once per level (again after 10 % above it; kept across reboots) | covered |
| ... SD card | `sources.pi.values.sd_free_gb` below 5 GB | 3 after 1 min | "SD card nearly full" | covered |
| UPS monitor unreadable | `power.json` `warnings.current_unreadable` | 3 after 30 s | "power service: current unreadable" | covered |
| SDR missing or on a USB 2 link | `manager.json` `state` `not_ready`, `not_ready` reasons | 3 after 10 min | "radar not ready: sdr: ..." | covered |
| Build fails, radar exits with an error or crashes | `manager-events`: `ended` with `failure`; `fault` once the retries are used up | 2; 4 | "radar run failed"; "radar FAULT: start_failures" | covered |
| Turn-on check fails | `manager-events`: `fault` kind `turn_on_check` | 4 | "radar FAULT: turn_on_check" | **not available yet** (stage 7 does not exist; the rule needs no change when it does) |
| High late-command rate | `manager-events`: `ended`, ORCA's last lines in `output_tail`: `[RX] Error count` over `[RX] Total pulses attempted`, above `error_rate_warn` (5 %) | 3 | "radar error rate high" | covered **after each run** only (nothing publishes the rate during a run). That the error count is the late-command count is assumed; the bench runs of 2026-11-19 show 2.0-2.3 % |
| Output drift | none | | | **not available yet** |
| Pi hot or throttling | `telemetry.json` `sources.pi`: `throttled_now` not empty, or `values.soc_temp_c` at or above 80 C | 3 after 1 min | "Pi hot or throttling" | covered |
| Radar manager crashes, or never comes up | `manager-events`: `manager_started` with no `manager_stopped` before it (see "Event logs"); `manager.json` old or missing for 2 min from the start of this service | 3; 3 | "radar manager restarted", "radar manager not running" | covered |
| Safety watcher crashes | `watcher-events`: `start` with no `stop` before it (see "Event logs"); `watcher.json` old or missing | 3; **4** after 2 min | "safety watcher restarted", "safety watcher not running" | covered |
| Pi hangs | the next boot summary: "ended without a clean stop" | 3 | (a hang and a power cut cannot be told apart) | covered |
| No internet | | | alerts wait in the queue | covered |
| Clock not set | the boot summary and the alert footer say "clock not set" | | | covered |

Beyond the table, from the same files:

| Condition | Source | Priority |
|---|---|---|
| Power service not running (no clean shutdown at a power loss) | `power.json` old or missing, 2 min | 4 |
| Power service warnings | `power.json` `warnings`: `no_power_monitoring`, `boot_loop_guard`, `guard_state_unwritable` (4); `signals_disagree`, `*_unreadable`, `*_faulty`, ... (3); 30 s | 4 / 3 |
| Telemetry service not running | `telemetry.json` old or missing, 2 min | 3 |
| Safety watcher loop stalled | `watcher-events`: `stall` | 3 |
| Fault cleared | `manager-events`: `fault_cleared` (only if the fault went out) | 2 |
| Any condition above over | gone for 1 min (only if its alert went out) | 2 |

Not alerts: the watcher's `not_ok`/`ok` (they come and go at every start; the
condition above covers a long one), `start`, `stop`, `unpowered`, `powered`,
`reset_tool`, `radar_stopped`; the manager's `state`, `request`, `start`,
`stop`, `gate`, `collection`, `sdr_state`. Power losses are the normal way the
system stops: the boot summary reports them.

No other service was changed for this stage. Every row above uses files that
existed already.

## Positions and secrets

Alert text goes to a third party, so:

* Only named fields go into an alert: event `text`, `reason`, `failure`,
  `kind`, `condition`, and the status fields named in the table. GPS readings
  are never read (only `health`, `reason` and the cause `no_position`).
* Every title and message is then filtered (`logic.scrub`), when it is queued
  and again just before it is sent. This is a second line of defence, narrow
  enough that readings, sizes, counts, durations, percentages and PIDs pass.
  Replaced by `[removed]`:
  * one or two numbers after a word that names a position (`lat`, `lon`,
    `lng`, `latitude`, `longitude`, `pos`, `position`, `coord(inate)s`,
    `northing`, `easting`, `geo`, also with a suffix such as `lat_deg`, and
    `gps_Lat`/`gps_Long`), with their sign;
  * degrees (not degrees C or F) with minutes and seconds;
  * a hemisphere letter (capital N, S, E, W) after or before a decimal number:
    `77.85S 166.67E`, `S 77.85 E 166.67`, or one of them alone with three or
    more decimals;
  * two numbers side by side with three or more decimals, one of them with two
    or three digits before the point;
  * a number of degrees with four or more decimals (1 to 999 before the point:
    `0.0290 A` passes), and integers of 8 to 10 digits (the OLA's degrees x 1e7).

  Large counts in this service's own texts are written as "14.4 million".
* The ntfy token is replaced wherever it appears, and so are token patterns
  (`tk_...`), `Bearer ...`, `token=`/`password:` values and `user:password@`
  in URLs. The server and the topic are not replaced: they are where every
  alert goes, so they tell ntfy's readers nothing (and a topic that is part of
  the host name would otherwise spoil every title). None of the three is ever
  in the status file, the queue or the log. The topic goes in the JSON body,
  the token only in the `Authorization` header; a token that could not go in a
  header (a space, a newline) is refused before anything is sent.

## The queue

One JSON file per alert, `p<priority>-<sequence>.json`, written to a temporary
name, synced, renamed and the directory synced: a power cut at any moment
leaves each alert complete or absent. A file that cannot be read as an alert
(damaged card) is deleted and counted. Sent highest priority first, then
oldest first; deleted only after ntfy answered 2xx.

* At most `queue_max` (200) files, at most 4000 bytes of message each (ntfy's
  limit is 4096), titles 120 characters: under 2 MB in all.
* **Full:** a new alert pushes out the oldest alert of the lowest priority
  queued, but only if that priority is not higher than its own. An urgent alert
  is never dropped for a lower one; an alert lower than everything queued is
  itself dropped. Counted as `dropped` in the status and the boot summary.
* **A file that cannot be read** (an I/O error) is passed over for 10 minutes;
  the others are sent meanwhile (`unreadable` in the status).
* **Sent, but the file cannot be deleted** (a card gone read-only): never sent
  again by this process; deletion is tried again every minute
  (`sent_not_deleted` in the status, one log line). After a restart with the
  card still read-only it is sent once more.

What can come twice, and what can be lost:

* **Twice:** the alert that was being sent when the process died, if ntfy had
  accepted it and the file was not deleted yet; the alerts raised in the round
  (one second) in which the process died, before the state was saved; a sent
  alert whose file could not be deleted, once per restart; a send that was
  given up on (its time limit, or a stop) although the server had taken it.
* **Lost:** alerts that could not be queued (the card not writable, or the
  queue full under the rule above); events that never reached the drive before
  a power cut (both writers sync each line, the manager's from a thread within
  moments of the write); the end of an event log that was rotated twice while
  the service was not running (counted in `lost`); a test alert whose inbox
  file cannot be deleted (not sent; said in the status). A restart, a reboot or
  a power cut loses nothing that was already queued.

## Sending

`POST` of a JSON message to the server's root URL (ntfy's "publish as JSON"):
topic, title, message, priority, tags. HTTPS with the system's certificates;
plain HTTP only to this host itself (the tests). Standard library
`http.client`: no proxy, no redirects (a redirect would carry the token
elsewhere).

Each send runs in a thread of its own; the loop never waits on the network for
more than half a second per round, so the watchdog pings and the status file go
on. A send is given up on after `ntfy.timeout_s` (15 s) in all, a server that
sends one byte at a time included: its socket is shut, which ends a read at
once. A DNS lookup, a TCP connect or a TLS handshake in progress is not ended
that way: a connect or handshake ends at the 10 s socket timeout, a lookup when
the resolver gives up; the send runs on in its thread meanwhile (the loop and a
stop do not wait for it), and no new send starts while two are left like that.
Sent means a complete 2xx answer: the end of the headers and the whole body
(Content-Length matched, or the end of a chunked body). A connection that
closes before that, or a send that was cut off before its answer was whole, is
never counted as sent.

| Answer | Then |
|---|---|
| 2xx | deleted from the queue; the next one is sent at once (at most `max_per_round`, 5, per round) |
| no connection, timeout, 5xx, 408, 3xx, 401, 403, 404 | kept; wait `retry_s` (10 s), doubling with each failure in a row up to `retry_max_s` (300 s); the link is back at most 5 minutes after it returns |
| 429 | as above, and at least the server's `Retry-After` (at most an hour) |
| 400, 413, 422 | kept, not tried again for `retry_max_s`, and from then on sent after every alert that was never refused (refused ones in the order of their last refusal), so a refused alert never holds up another. A refusal counts against the alert only if another alert went through since its previous refusal; otherwise it is taken as the settings (a refused topic, a change at the server) and handled like a failure above. An alert is dropped (`rejected`) after three refusals that count, so never while the server refuses everything |

A Pi whose clock is not set yet fails the certificate check ("certificate not
accepted ... is the clock set?"): its alerts wait like any others.

**Stopping** cuts off a send in progress (the alert stays queued), saves the
state with `clean_stop` and exits without waiting for a thread left in a DNS
lookup (`os._exit` once the state is saved). Measured: under 0.1 s with a
server that never answers and with a DNS lookup that hangs (the tests require
under 2 s). The unit allows 5 s (`TimeoutStopSec`): the power service forces
the power-off 10 s after asking for it, and this service must never be what it
waits for.

## The storm limit

Per kind of alert (the watcher's event and condition, the manager's fault
kind, each condition): at most `storm.burst` (3) in any `storm.window_s`
(1 hour). The ones held back are counted. The next alert of that kind that
goes out ends with "(N more like this held back before this one.)"; if none
comes, a note "N more alerts of this kind were held back (storm limit). The
last one: ..." goes out as soon as the window allows, with the highest priority
among them. All kinds together: at most `storm.max_per_hour` (30); urgent ones
neither count towards this nor are held back by it. An "over" alert goes out
only if the alert it ends went out, also when it ends in a later boot. The boot
summary and test alerts are never held back.

A reading that flaps around a limit 300 times gives 3 trips, their 3 "over"
alerts and, an hour later, one note with the count (a test does exactly this).

## Time

The Pi has no clock battery; its clock can step by years when GPS or NTP sets
it. **No decision here uses the wall clock.** Ages, delays, the storm limit
and the backoff all use the time since boot (`CLOCK_BOOTTIME`). What is kept in
the state file from these is used only within the same boot (kernel boot ID);
a new boot starts them afresh and keeps only the counts (and which alerts went
out, for their "over"). Status files are
judged by `read_status` (uptime and boot ID). The wall clock appears only as
text in the alerts, with "clock not set" when telemetry says so.

## The boot summary

Once per boot, at `summary_after_s` (3 min) of uptime, and only once the
status files of telemetry, the watcher, the manager and the power service have
been fresh and past their start (telemetry reading the Pi, neither the watcher
nor the manager stopping) for `summary_settle_s` (30 s) without a break, as
seen by this service: a summary sent at a later start, in the middle of a
restart of the others, waits for them. It waits at most `summary_wait_max_s`
(10 min) after this service's start; then it goes out with a first line saying
what was missing ("unknown" then means not reported, not a fault). Priority 3.
A restart of the service in the same boot does not send another. A collection
without an end is "recording now" when the manager's status names it as its
current run, "no end recorded yet" when the manager's state is not known, and
"cut off" only otherwise. Contents:

* boot number, uptime, whether the clock is set;
* the boot before: how long it ran (at least: the state file is updated every
  5 minutes), and how it ended: "shut down by the power service (why)" when
  `power.json`'s `guard.last_shutdown` changed since the last summary; "the
  alerts service was stopped cleanly" (a shutdown, a reboot or by hand);
  otherwise "ended without a clean stop (power cut, hang and watchdog, or
  crash)". Boots without any record are named. The number of alerts per
  priority in that boot;
* free space on the data drive and the SD card;
* the newest collection: name, how it ended (or "cut off"), how long it
  recorded, its data size, the number of collections. If the drive does not
  answer the listing within 10 s, the summary goes without it;
* one line each for the manager (state, fault, not-ready reasons), the safety
  watcher, telemetry (problems) and the power service (state, mode, warnings),
  and the number of config warnings;
* active alerts, and the queue: waiting, dropped, held back.

A day without a summary is itself a sign of trouble.

## Event logs

Followed from where the service last was, also across restarts and reboots
(position in the state file: the file's inode, its first 64 bytes, the offset,
and fingerprints of the last 50 lines taken). The very first start begins at
the end: history is not sent. Otherwise a log is never read from its start again:

* renamed to `.1`: the rest of `.1`, then the new file;
* renamed twice while the service was not running: the end of the older file is
  gone (`events.<name>.lost`), then all of `.1`, then the new file;
* cut short (its end lost in a power cut before it reached the drive), whether
  or not the next boot has written past the old offset since: the line ending
  at the offset is checked against the last one taken every round; if it is not
  that line, on after the last line already taken that is still in the file, so
  the lines the next boot added are read and nothing is read twice (`lost`).
  The same for `.1`, when the cut file was renamed before it was read again;
* a file read to its end is never read from its start again (the last three are
  remembered): a power cut that undoes the rename, or the new file after it,
  does not replay it; at `<file>` again it is read on from where it ended.

Only whole lines are read; a line cut off by a power cut, NUL bytes, a line
that is not JSON or longer than 64 kB are skipped and counted. At most
`events.max_read_kb` (256 kB) per file per round.

"Restarted" (a `start` or `manager_started` after something other than a
clean stop) is judged only between two events that were both added to the log
after this process first read it: the logs carry no boot ID, and an event that
was there already may be from the boot before (a power cut leaves no `stop`).
A service that crashes while the alerts service is not running is therefore not
reported as restarted; its status file going stale still is.

## Failure modes of the service itself

| What | Then |
|---|---|
| It crashes, or a part of its round raises | A part that raises is logged and shown under `errors`, and the rest of the round goes on; one event that cannot be judged is skipped. Ten failed rounds in a row: exit, systemd restarts it after 10 s. Queue and positions are on disk |
| It hangs | no watchdog ping for 60 s: SIGKILL and restart |
| The queue is full | the drop rule above; counted |
| The SD card cannot be written | alerts cannot be queued: logged, `queue.error` in the status. Sending what is queued goes on |
| No network, wrong token, server down | alerts wait; `link.state` `failing` with the reason; `setup/check.py` fails "alerts reach ntfy" |
| No secrets file, or a key missing | `ntfy.configured` false; alerts are queued, not sent |
| The other services' files missing, old, damaged, huge (over 1 MB) or odd | conditions as in the table; nothing raises |
| Its own state file damaged | started afresh, at the end of the event logs; said in the status (`state_file.note`) |
| Memory | `MemoryMax=200M`; everything it reads is bounded (status files 1 MB, 256 kB of event log per round, 64 kB per line) |
| It is stopped | the send in progress is cut off (it stays queued), the state saved with `clean_stop`, the status says `running: false`; exit within a second |
| A send never ends (no answer, a DNS lookup that hangs) | given up on after 15 s; the loop goes on meanwhile. The longest gap between two status writes is about a second plus the card's sync time, so `alertctl` and `setup/check.py` (10 s limit) do not take a busy service for a stopped one. Two sends stuck in DNS lookups stop all sending (no third is started): if that lasts 10 minutes the service exits with status 1 and systemd restarts it; the queue is on disk |
| A test alert's inbox file cannot be deleted (`sudo alertctl test`) | not sent; `inbox.problem` in the status. `alertctl` refuses to run as root and never creates the inbox |

## Status file

`/run/radar-runner/user/alerts.json`, once a second:

```
ntfy        configured, problem
link        state idle | ok | failing | unconfigured; last_result, last_status, failures_in_a_row, next_attempt_in_s
queue       length, by_priority, max, dropped, damaged, error
counters    queued, sent, held_back, rejected, events, voltage_trips_in_power_loss, inbox
boot, summary_sent
conditions  the conditions being followed: since_uptime_s, raised, title
disk_levels_crossed
events      per log: file, position, lines, skipped, lost, error
inputs      telemetry, watcher, manager, power: ok | stale | missing | corrupt
sending     the send on its way: id, for_s; stale_sends: given up on, still in a DNS lookup
sent_not_deleted, unreadable, refused_kept    queue files kept out of sending
inbox       dir, problem
recent      the last 20 alerts: id, title, priority, result
errors      parts of the round that failed
running     false in the last status of a service that was stopped
```

## alertctl

```
alertctl status [--json]        link, queue, conditions, the last alerts
alertctl queue                  what is waiting, in sending order
alertctl test [--priority urgent|high|default|low] [--text TEXT] [--wait S]
```

`test` puts a request in the inbox; the service queues it and sends it like any
other alert (filter, queue, ntfy). `--wait` waits until it is sent. Run as user
`englacial`. It reads the config for the paths only, never the secrets file.
`setup/setup.sh` links it into `/usr/local/bin`.

## Config

Section `alerts` of `config/radar-runner.yaml`, with every default and a
comment per key. Server, topic and token go in `/etc/radar-runner/secrets.yaml`
(mode 0640, `root:englacial`):

```
alerts:
  ntfy:
    server: https://ntfy.sh
    topic: ...
    token: tk_...
```

Settings that cannot work (a storm window under a minute, a timeout longer
than the watchdog allows, ...) are replaced by the defaults and listed in
`config_warnings` in the status file. Restart the service after a change.

The `offload` section that the secrets file already has belongs to stage 9:
the config loader passes over it without a warning and nothing uses it yet
(`RESERVED` in `radar_runner/common/config.py`).

By hand, with its files somewhere else and the stand-in server:

```
cd ~/englacial-radar-runner
.venv/bin/python -B tests/alerts/fake_ntfy.py 18931 &        # prints what it receives
.venv/bin/python -B -m radar_runner.alerts --local-config my.yaml --secrets /nonexistent
```

where `my.yaml` sets `paths.run_dir`, `paths.state_dir` (scratch directories;
link the other services' status files into the run directory to read them),
`alerts.events.watcher_file`, `alerts.events.manager_file` (the real logs, read
only) and `alerts.ntfy: {server: "http://127.0.0.1:18931", topic: x, token: "-"}`.

## Tests

```
.venv/bin/python -B -m pytest tests/alerts       # about 2 minutes; only 127.0.0.1, no installed service
```

`test_alerts_logic.py`: the rules in simulated time (priorities, the filter,
conditions, storm limit, summary). `test_alerts_files.py`: queue and event log
(power cut damage, rotation). `test_alerts_ntfy.py`: the request against a
stand-in server that accepts, refuses, hangs, trickles, redirects and answers
4xx/5xx/429. `test_alerts_service.py`: the service in this process with a
simulated clock (offline then online, restarts mid-send, positions, storms,
clock steps, reboots). `test_alerts_process.py`: real processes (kill -9 during
a send), the write audit, the unit file, `alertctl`.

## First start under systemd

The unit's sandboxing (`ProtectSystem=strict`, `ReadWritePaths`,
`ProtectHome=read-only`, `PrivateTmp`, `NoNewPrivileges`, `MemoryMax`) has not
yet run under systemd. Right after the first start:

1. `systemctl status radar-alerts`: active, and its status line updating
   (`journalctl -u radar-alerts -b` without errors; no watchdog kill).
2. `alertctl status` from the user's own shell: `service: running`, `link: ok`
   or `idle`, no `ERROR` lines.
3. `alertctl test --wait 60`: "sent" (DNS and TLS work inside the sandbox).
4. At 3 minutes of uptime the boot summary arrives and names the free space on
   the data drive and the newest collection (the drive is readable).
5. `ls -l /var/lib/radar-runner/user/alerts-*` belong to `englacial`.

If any of this fails: remove `ProtectSystem`, `ProtectHome`, `ReadWritePaths`
and `PrivateTmp` from the unit (keep the rest), `sudo systemctl daemon-reload`
and `sudo systemctl restart radar-alerts`.

## Bench tests ("Done when")

With the user, after `sudo setup/setup.sh` has installed and started the unit:

1. `alertctl test --priority default --wait 60`: arrives on the phone.
2. The uplink unplugged (Starlink or Ethernet cable; SSH and Tailscale go with
   it, so work from the local console or plan the steps): `alertctl test
   --priority high`, then `alertctl status` shows `link: failing` and
   `1 waiting`. Reboot the Pi while still unplugged; plug in again: the test
   alert and the boot summary arrive within 5 minutes of the link returning.
3. `alertctl test --priority urgent --wait 60` with the phone on silent/do not
   disturb: it breaks through (ntfy app: priority 5 notifications, "override
   do not disturb" allowed for the topic).
4. `sudo systemctl kill -s KILL radar-alerts`: `systemctl status` of
   radar-power, radar-telemetry, radar-watcher and radar-manager unchanged (same
   PIDs), their status files fresh; radar-alerts back within 10 s; no alert
   lost (`alertctl status`).
5. `sudo systemctl stop radar-watcher` for 3 minutes: "safety watcher not
   running" (high) arrives; start it again: "over: ..." (low).
