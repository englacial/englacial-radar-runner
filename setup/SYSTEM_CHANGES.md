# System changes for the radar manager

Every change outside this repository. Use it to rebuild a system, to compare
two systems, or to undo a change. Add each new system change here when it is made.

Reference host: `englacial-radar-1`, Raspberry Pi 5, Ubuntu 24.04.5 LTS, kernel
`6.8.0-1068-raspi`, systemd 255. Written 2026-11-18 (stage 0); sections 11-13
added 2026-11-19 (stage 2); sections 14 and 15 added 2026-11-19 (stages 4 and 3).

```
setup/setup.sh --dry-run     # show what would change; no root needed
tmux                         # a dropped SSH session must not interrupt the next command
sudo setup/setup.sh          # apply sections 1-16; safe to run again, also after an interruption
sudo reboot                  # logind and journald read their files at boot
setup/check.py               # one PASS/FAIL line per item below
```

The script only writes files that this project owns (copied from
`setup/files/`, same path as on the system), so reverting a change is deleting
its file. It never touches network configuration or SSH, never restarts or
stops the power service, the telemetry service, the radar manager or the safety watcher, and never
reboots. It installs the radar manager's unit but never enables or starts it (section 14), and it
enables the safety watcher's unit but does not start it (section 15). Its one
Tailscale change is section 5. Its one package change is section 13 (chrony
and fake-hwclock in place of systemd-timesyncd), which is the last step and the
only one that needs the internet. It restarts gpsd when gpsd is not running
the command line of its drop-in (section 11), and chrony when its settings
change (section 13).

After section 8 (watchdog) is applied, the three bench tests listed there must
be done before the system ships.

## Applied by `setup/setup.sh`

### 1. Directories for status files, state and config

- **What:**

  | Directory | Owner, mode | Written by |
  |---|---|---|
  | `/run/radar-runner`, `/var/lib/radar-runner` | `root:root 0755` | the power service only (`power.json`, `power_guard.json`) |
  | `/run/radar-runner/user`, `/var/lib/radar-runner/user` | `englacial:englacial 0755` | all other services (status files `<service>.json`, their state) |
  | `/etc/radar-runner` | `root:root 0755` | the administrator |

- **Why:** root and `englacial` never write in the same directory. In a
  directory that both could write, a file left by `englacial` under one of the
  power service's file names would make the power service's writes fail (the
  kernel refuses root that open, `fs.protected_regular=2`), and the power
  service does not shut the Pi down when it cannot save its guard state.
  Everyone can read everything, so the manager reads `power.json` and the power
  service's hooks can read the others.
- **No conflict with the power service**, which creates `/run/radar-runner`
  itself (`root:root 0755`, the same) if it starts before systemd-tmpfiles, and
  never changes a directory that exists. systemd-tmpfiles makes parents before
  the directories inside them, at every boot.
- **File:** `/etc/tmpfiles.d/radar-runner.conf`
- **Verify:** `stat -c '%U:%G %a %n' /run/radar-runner /run/radar-runner/user /var/lib/radar-runner /var/lib/radar-runner/user /etc/radar-runner`
- **Revert:** delete the file; `sudo rmdir /run/radar-runner/user /var/lib/radar-runner/user`

### 2. Ignore the power key (UPS wake-up contact)

- **What:** `HandlePowerKey=ignore` in logind. Already on this system since
  2026-10-29; the script installs the identical file on a rebuilt system.
- **Why:** the UPS presses the Pi's power button when input power returns; with
  the default, a running Pi then powers off and stays off. Details and test
  results: `~/ups-bringup/SYSTEM_CHANGES.md`.
- **File:** `/etc/systemd/logind.conf.d/ups.conf`
- **Verify (after a reboot):** `busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager HandlePowerKey` prints `s "ignore"`
- **Revert:** delete the file, reboot.

### 3. Shutdown inhibitor delay back to 5 s

- **What:** `InhibitDelayMaxSec=5` (the systemd default).
- **Why:** the `unattended-upgrades` package raises it to 30 s
  (`/usr/lib/systemd/logind.conf.d/unattended-upgrades-logind-maxdelay.conf`),
  so any program holding a "delay" lock could hold up `systemctl poweroff` for
  half of the capacitor's run time. The power service does not go through
  logind (`power/service.py`), so this is a second line of defence, mainly for
  a shutdown typed by hand. Section 4 also stops the program that holds the lock.
- **File:** `/etc/systemd/logind.conf.d/zz-radar-runner.conf` (the name sorts
  after the package's file, so it wins)
- **Verify (after a reboot):** same command as above with `InhibitDelayMaxUSec`: prints `t 5000000`. `systemd-inhibit --list` shows no "Unattended Upgrades Shutdown" line.
- **Revert:** delete the file, reboot.

### 4. Automatic updates off

- **What and why:** nothing may change the software during the deployment
  (the kernel went from 6.8.0-1065 to -1068 by itself in three weeks).

  | Part | Method | Verify | Revert |
  |---|---|---|---|
  | apt periodic jobs (list update, unattended upgrade) | `/etc/apt/apt.conf.d/99zz-radar-runner-no-auto-updates` sets `APT::Periodic::Enable "0"` and the individual jobs to `"0"`; sorts last, so it overrides `10periodic` and `20auto-upgrades` | `apt-config dump \| grep Periodic` | delete the file |
  | `apt-daily.timer`, `apt-daily-upgrade.timer` | `systemctl mask --now` | `systemctl is-enabled apt-daily.timer` prints `masked` | `sudo systemctl unmask apt-daily.timer apt-daily-upgrade.timer && sudo systemctl start apt-daily.timer apt-daily-upgrade.timer` |
  | `unattended-upgrades.service` (finishes upgrades at shutdown; holds the delay lock of section 3) | `systemctl mask --now` | `masked`, as above | `sudo systemctl unmask unattended-upgrades.service && sudo systemctl start unattended-upgrades.service` |
  | `fwupd-refresh.timer` (firmware metadata download) | `systemctl mask --now` | `masked`, as above | `sudo systemctl unmask fwupd-refresh.timer && sudo systemctl start fwupd-refresh.timer` |
  | `rpi-eeprom-update.service` (runs `rpi-eeprom-update -s -a` at every boot: flashes a newer bootloader by itself once a package upgrade brings one) | `systemctl mask --now` | `masked`, as above | `sudo systemctl unmask rpi-eeprom-update.service` (it runs again at the next boot) |
  | snap automatic refresh (only the `snapd` snap is installed) | `snap refresh --hold` (kept in snapd's state, not in a file) | `snap refresh --time` shows `hold: forever` | `sudo snap refresh --unhold` |

- Masking keeps the packages installed and is undone exactly by `unmask`: the
  unit's "enabled" links are not touched. Two methods for apt on purpose: the
  masked timers never fire, and if something started the job anyway the
  setting makes it exit at once.
- `sudo apt update` and `sudo apt install ...` by hand still work.
- With `rpi-eeprom-update.service` masked the bootloader only changes when
  someone runs `sudo rpi-eeprom-update -a` on purpose.

### 5. Tailscale automatic self-update off

- **What:** `tailscale set --auto-update=false`. Nothing else about Tailscale
  is read or changed. Skipped if Tailscale is not installed or already off.
- **Why:** this node had `AutoUpdate` `Check: true, Apply: true`
  (`tailscale debug prefs`), so tailscaled upgraded and restarted itself
  whenever a release appeared: an unplanned change to the only remote access.
- **File:** none; the setting is kept in Tailscale's own state.
- **Verify:** `tailscale debug prefs | grep -A3 AutoUpdate` shows `"Apply": false`
- **Revert:** `sudo tailscale set --auto-update=true`

### 6. needrestart: list only

- **What:** `$nrconf{restart} = 'l';`
- **Why:** on Ubuntu 24.04, after any `apt install` needrestart restarts by
  itself the services whose libraries changed, and that can include `ssh` and
  `tailscaled`. With this file it only lists them; restart them by hand, or reboot.
- **File:** `/etc/needrestart/conf.d/radar-runner.conf`
- **Verify:** `setup/check.py` evaluates needrestart's config ("needrestart only lists services"). An `apt install` then prints "Disabling Ubuntu mode, explicit restart mode configured".
- **Revert:** delete the file.

### 7. Journal limits

- **What:** `Storage=persistent`, `SystemMaxUse=8G`, `SystemMaxFiles=1000`.
- **Why:** the journal is the record of what happened in the field. Every boot
  starts at least two journal files (system and user) of 8 MB each, more when
  the clock jumps, so 99 files and 436 MB had accumulated in 71 days on the
  bench, almost all of it empty space. The defaults (4 GB, and 100 files) keep
  only about 50 boots; these limits keep 340 or more, on a 117 GB card with
  92 GB free. It is still a hard cap: a service that logs without end cannot
  fill the card.
- **File:** `/etc/systemd/journald.conf.d/radar-runner.conf`
- **Verify (after a reboot):** `journalctl -b -t systemd-journald -o cat | grep "System Journal"` ends with `max 8.0G, ...`
- **Revert:** delete the file, reboot.

### 8. Hardware watchdog

- **What:** `RuntimeWatchdogSec=30s`, `RebootWatchdogSec=10min` (the latter is
  the default, written out).
- **Why:** a hung Pi cannot watch the amplifier. systemd (PID 1) pings the
  Pi's watchdog every 15 s; if PID 1 stops for 30 s the Pi resets, and if the
  kernel itself hangs the hardware resets it within about 15 s, its own limit
  (the kernel keeps the hardware timer fed for longer timeouts). 30 s instead
  of 15 s gives a healthy but stalled system (slow SD card, cold start) a wide
  margin, and costs nothing that matters.
- **Never a multiple of 16 s** (16 s, 32 s, 48 s): the hardware counter of this
  driver (`bcm2835_wdt`) holds just under 16 s and wraps, so such a value
  programs a timeout of zero.
- **Shutdown, as documented by systemd:** during the first phase of any
  shutdown PID 1 keeps pinging. For a reboot the watchdog is re-armed with
  10 minutes, so a reboot that hangs in its last phase is forced; 10 minutes is
  long enough for a slow final sync.
- **Shutdown, expected but not yet confirmed here:** for a power-off, systemd
  switches the watchdog off, and for `poweroff -f -n` (the power service's last
  resort) the kernel stops it, so that the Pi stays off. If either were wrong,
  the Pi would start again by itself while on the capacitor, and the power
  service would shut it down again. The bench tests below settle it.
- **File:** `/etc/systemd/system.conf.d/radar-runner-watchdog.conf`
- **Takes effect:** at `systemctl daemon-reload` (the script ends with one) and at boot.
- **Verify:** `systemctl show -p RuntimeWatchdogUSec` prints `30s`;
  `cat /sys/class/watchdog/watchdog0/state` prints `active`;
  `cat /sys/class/watchdog/watchdog0/timeleft`, read a few times, is never near
  zero. After a reboot, `journalctl -b -1 | grep -i watchdog` shows the 10 min re-arm.
- **Revert:** delete the file, `sudo systemctl daemon-reload` (check that the state is `inactive`; if not, reboot).
- **Bench tests that must follow enabling it.** Each with main power present
  and the Pi up for more than 5 minutes (so that nothing counts towards the
  boot-loop guard):

  1. `sudo systemctl poweroff`. The Pi must stay off for more than 60 s. Press
     the power button to start it again.
  2. `sudo poweroff -f -n`. Same expectation.
  3. A power pull with the power service armed. The Pi must power off, stay
     off, and boot when power returns.

  If the Pi comes back by itself in test 1 or 2, delete the file, reboot, and
  report it: the watchdog must then not ship as configured.

### 9. Power service

- **What:** `systemd/radar-power.service` copied to `/etc/systemd/system/`.
  `/etc/radar-runner/power.toml` is created from `config/power.toml`
  (`armed = false`) only if it does not exist. If the unit is not enabled (a
  new system, or an earlier run that was interrupted) it is enabled and
  started. An existing config is never changed, and a running service is never
  restarted or stopped: a changed unit file is used from the next reboot. A
  service that is enabled but stopped, or masked, is left as it is.
- **Why and how it works:** `power/README.md`.
- **Verify:** `systemctl status radar-power`; `python3 -B -m power status` in the repository.
- **Revert:** `sudo systemctl disable --now radar-power && sudo rm /etc/systemd/system/radar-power.service && sudo systemctl daemon-reload`

### 10. `systemctl daemon-reload` at the end of every run

So that a run that was interrupted after installing a file is completed by
simply running the script again.

### 11. gpsd owns the GPS receiver

**Added 2026-11-20, stage 6: raw messages for PPK. Not applied to the receiver yet.**
`setup/gps_receiver.py --apply` now also provisions two messages on USB, in all three layers,
for the raw GPS log in each collection (`radar_runner/gpsraw/README.md`):

- `CFG-MSGOUT-UBX_RXM_RAWX_USB` = `manager.gps_log.rawx_every` (default 10: RXM-RAWX at every
  10th epoch, 1 Hz at the OLA's 10 Hz; 2 = 5 Hz, 1 = 10 Hz). The script reads the setting
  from the repository's config and `/etc/radar-runner/local.yaml` with the repository's Python
  environment and prints the value, the file it came from and every warning of the config
  loader. If the setting cannot be read, or with `--apply` while the config loads with a
  warning about `local.yaml` or `manager.gps_log`, the raw pair is neither read nor written,
  the other 17 settings are still checked (and with `--apply` provisioned), and the exit
  status is 1.
- `CFG-MSGOUT-UBX_RXM_SFRBX_USB` = 1 (the broadcast navigation data, as it comes); both 0
  with `rawx_every: 0`.
- **Why 1 Hz by default:** the USB output is already 21 kB/s with its transmit buffer peaking at
  88 % (below). RXM-RAWX is 16 + 32 bytes per signal tracked: about 1.5 to 2 kB per epoch for the
  40 to 60 signals this receiver tracks under open sky (dual frequency, four systems), so about
  +2 kB/s at 1 Hz and +15 to 20 kB/s at 10 Hz (nearly doubling the output); RXM-SFRBX adds
  about 1 to 2 kB/s at any rate. These are estimates from the message sizes, not measured. The
  peak of the buffer is set by the largest burst at one epoch, which grows by one RXM-RAWX at
  any rate; the rate decides the average load. 1 Hz is the usual rate for PPK of a slow
  vehicle (a few metres between epochs at truck speed).
- **Not in the RAM-only run after every gpsd start** (`--apply --ram`): it still checks only
  NAV-PVT and UART1 NMEA, the two settings that decide whether gpsd has positions. Every write
  reaches flash at the next OLA start, and a missing raw message costs PPK data, not positions:
  it shows as `gps_raw.no_raw_data` (flag `no_raw_gps`) in each collection and in
  `setup/check.py --receiver`.
- **Apply:** `setup/gps_receiver.py` (reads only: RAWX and SFRBX show as 0 or "not stored"),
  then `setup/gps_receiver.py --apply` (writes the two, and anything else wrong, to all three
  layers), then `sudo systemctl restart radar-telemetry` (an OLA start, so that the OLA's save
  carries them), then `setup/gps_receiver.py` and `setup/check.py --receiver`.
- **Measure:** `setup/gps_receiver.py --comms` polls MON-COMMS twice 10 s apart (a poll, sets
  nothing) and prints each port's transmit-buffer use in the last period, its peak since the
  receiver started, the bytes per second, and the error flags. Before and after the change; a
  peak near 100 % or a memory/allocation error flag means lost output. A larger `rawx_every`
  lowers the average, not the burst; what shrinks the burst is fewer messages at the same
  epoch: MON-SPAN and MON-RF (the `gps_jam` logger's, every 10 epochs, possibly the same
  epochs as RXM-RAWX at 1 Hz) off, or NAV-SAT and NAV-SIG at a lower rate (not a setting here).
- **Revert:** `manager.gps_log.rawx_every: 0` and `manager.gps_log.enabled: false` in
  `/etc/radar-runner/local.yaml`, then `setup/gps_receiver.py --apply` (both messages 0 in all
  three layers) and an OLA start (`sudo systemctl restart radar-telemetry`).

**Changed 2026-11-20: gpsd runs passive (`-p`)** (this paragraph overrides anything below that
contradicts it). The drop-in's command line is `gpsd -n -p <receiver>`. With `-p` gpsd does not
configure the receiver; it still asks for its version, identifies it, and passes ubxtool
commands on. Everything below was measured on `englacial-radar-1` on 2026-11-19/20 unless it
says otherwise.

- **Why:** without `-p`, gpsd 3.25 configures the receiver at every start (from its source,
  `driver_ubx.c`). Our device path has no "/ttyACM" in it, so gpsd addresses UART1 and switches
  NMEA output on there (`CFG-UART1OUTPROT-NMEA` = 1 in RAM), which at 10 Hz overflows the
  receiver's transmit buffer; and if it reads a position message before the receiver's version
  reply it assumes an old protocol version and switches NAV-PVT and the other position messages
  off, leaving only NAV-TIMEGPS and NAV-DOP. The 77-minute outage of 2026-11-19 (below) was in
  exactly that state; that gpsd got there this way is inferred, and it was never reproduced.
- **The OLA saves the receiver's settings.** It copies the receiver's current RAM settings to
  battery-backed memory and flash each time it starts: `CFG-UART1OUTPROT-NMEA` set to 1 in RAM
  only read 1/0/0 (RAM/battery-backed/flash); after a restart of the telemetry service, which
  restarts the OLA, it read 1/1/1. At boot the OLA starts within a second or two of gpsd, so
  whatever passing state gpsd had put the receiver in became what the receiver powered up with
  the next time. This settles the "likely cause ... not proven" further down.
- **Without `-p`, ten gpsd restarts:** gpsd identified the receiver every time, but UART1 NMEA
  was on after every start (corrected each time by `setup/gps_receiver.py`), and two of the
  script's reads went unanswered (the overflow).
- **With `-p`, nine gpsd restarts:** receiver identified 9/9, NAV-PVT on and UART1 NMEA off 9/9,
  nothing to correct, no unanswered read, chrony's GPS source fed throughout (newest sample never
  older than 12 s). **Six cold boots** after power-offs of several to 22 minutes (boot counters 6
  to 11, 2026-11-19 23:02 to 2026-11-20 03:22 UTC): gpsd had position messages from the
  telemetry service's first sample at every one; a 3D fix within 0.5 to 1 s of that sample on
  four; on two the receiver itself needed about 44 s and about 60 s to reacquire after the long
  power-off (gpsd reported "no fix" meanwhile: the receiver's state, not gpsd's); the receiver
  was identified and the receiver check had nothing to correct. The three layers read identical:
  NAV-PVT, SAT, SIG, EOE, POSECEF, VELECEF, DOP and TIMEGPS on USB 1; NMEA GGA and RMC on USB 0;
  `CFG-USBOUTPROT-UBX` 1; `CFG-UART1OUTPROT-NMEA` 0; `CFG-RATE-MEAS` 100. Read later on
  2026-11-20: all six NMEA sentences that the receiver sends on USB by default (GGA, GLL, GSA,
  GSV, RMC, VTG) are 0 in RAM and battery-backed memory and 1 in the receiver's defaults (layer
  7), `CFG-USBOUTPROT-NMEA` is 1, and NAV-TIMELS on USB is 255 (gpsd's rate, every 255 epochs).
  gpsd without `-p` is what had switched those six off and NAV-TIMELS on.
- **What still guards the receiver:** `radar-gps-receiver.service` (20 s after every start of
  gpsd) and the telemetry service (after every OLA start, and without a position) still run
  `setup/gps_receiver.py --apply --ram`. They now cover gpsd not identifying the receiver (they
  ask for its version), NAV-PVT off or UART1 NMEA on in RAM for any other reason (the OLA
  configures the receiver at its start), and report what they find.
- **A new unit or a replaced receiver:** nothing sets the USB output now. A receiver at factory
  settings has the UBX messages off, the six NMEA sentences on and UART1 NMEA on; at the OLA's
  10 Hz the NMEA sentences would come at every epoch on top of the UBX set, which alone is
  21 kB/s with the USB transmit buffer peaking at 88 % (below): likely the overflow again (a
  prediction, not measured). Once, after the OLA is connected and the telemetry service runs:
  `setup/gps_receiver.py --apply`. It reads 17 settings in all three layers (the eight UBX
  messages at 1, NAV-TIMELS at 255, the six NMEA sentences on USB at 0, `CFG-USBOUTPROT-UBX` 1,
  `CFG-UART1OUTPROT-NMEA` 0: this unit's tested state), writes those that are wrong in any layer
  to all three, and reports each key's three layers. Then one OLA start (`sudo systemctl restart
  radar-telemetry`), so that the OLA stores its own settings (10 Hz) with them, then
  `setup/gps_receiver.py` (only reads) to see all three layers right, and `setup/check.py
  --receiver`, which must pass (no transmit errors; look at the buffer peak). This path has not
  been tested with a receiver at factory settings. Not in the list: the rate (the OLA sets 10 Hz
  and saves it at every start; a different value written here would be overwritten),
  `CFG-USBOUTPROT-NMEA` (switching NMEA off as a protocol is untested), and the MON-RF and
  MON-SPAN messages (from the `gps_jam` logger; table below).
- **Revert:** the drop-in without `-p`, `sudo systemctl daemon-reload`, `sudo systemctl restart
  gpsd`.

**Changed 2026-11-19 after the first boot with stage 2** (overridden by the paragraph above
where they differ):

- Ubuntu's hotplug path is switched off by a second drop-in,
  `/etc/systemd/system/gpsdctl@.service.d/radar-runner.conf` (`gpsdctl add` becomes a no-op).
  With the receiver both on gpsd's command line and added by the hotplug path, gpsd opened it
  twice at boot ("Device or resource busy ... device activation failed, freeing device") and
  delivered nothing.
- gpsd is tied to the receiver instead: `BindsTo=`/`After=` the receiver's device unit in the
  gpsd drop-in, and a link `/etc/systemd/system/<device unit>.wants/gpsd.service`, so systemd starts gpsd
  when the receiver appears and stops it when it is unplugged. Revert: delete both drop-ins,
  delete that link, `systemctl daemon-reload`.
- The receiver setting `CFG-UART1OUTPROT-NMEA = 0` (no NMEA on the unused UART1; with it on and
  the OLA's 10 Hz rate the transmit buffer overflows and gpsd gets no fix) was written to RAM,
  battery-backed memory and flash on 2026-11-19 with `ubxtool -z CFG-UART1OUTPROT-NMEA,0`. It
  had been believed to be in flash already and was found back at 1 after a power-off of a few
  minutes. `radar-gps-receiver.service` was added to set it again if it is not 0 (it runs after
  every start of gpsd, in RAM only: next point). Revert: `systemctl disable
  radar-gps-receiver.service` and delete the unit file.
- Correction to the two points above, same day: the value in the receiver's RAM goes back to 1
  at every start of gpsd (four of four observed; not after an OLA reset alone), while the
  battery-backed and flash values stay 0. `radar-gps-receiver.service` therefore runs
  `setup/gps_receiver.py --apply --ram` 20 s after every start of gpsd, as user englacial, and
  does not restart gpsd. The telemetry service runs the same command about 15 s after every
  OLA start (`telemetry.gps.receiver_fix_command`; "" switches that off).
- Added the same evening: at the first boot after a power cut of several minutes gpsd delivered
  no position for 77 minutes (TPV mode 0 with fix times, chrony's GPS source never reached)
  while the receiver had a 3D fix. gpsd had not identified the receiver (empty `subtype` in
  `?DEVICES;`), and the receiver sent gpsd only NAV-TIMEGPS and NAV-DOP. `ubxtool -P 27.50 -p
  MON-VER` by hand made gpsd identify the receiver and switch the position messages on at once.
  What gpsd 3.25 does (from its source, `driver_ubx.c`): at its start it sends MON-VER and an
  unacknowledged burst of configuration that switches all position messages on while it does not
  know the protocol version. The first NAV-PVT it reads makes it assume version 14, and it then
  switches NAV-PVT, POSECEF, VELECEF, SAT, SIG and EOE off, leaving exactly NAV-DOP and
  NAV-TIMEGPS; only a MON-VER reply (or a NAV-SAT, NAV-SIG or NAV-EOE) moves it on, and it never
  asks for the version again. The same burst addresses UART1 (gpsd treats a device path without
  "/ttyACM" as UART1) with NMEA on, which is why `CFG-UART1OUTPROT-NMEA` is 1 after every start
  of gpsd. Inferred, not proven: the MON-VER reply was lost or came after the first NAV-PVT,
  or part of the burst was lost. If the second burst (after the reply) loses the NAV-PVT enable,
  gpsd is identified but still has no position, and another MON-VER does not help.
  `setup/gps_receiver.py --apply` therefore: asks for the version when gpsd has not identified
  the receiver (waits up to 20 s, then 3 s for the receiver to take gpsd's configuration);
  switches `CFG-MSGOUT-UBX_NAV_PVT_USB` on in RAM (only RAM) if it is 0 (on a gpsd that has
  never read a NAV-PVT this can take two runs: gpsd turns it off once); and then checks the UART1
  setting. With `--ram` a read that gets no answer is followed by the write anyway. Exit status
  2 means still not identified; `radar-gps-receiver.service` (`Restart=on-failure`) then runs it
  again about once a minute (20 s restart delay, 20 s wait, the run). The telemetry service
  reports a receiver without a position and why (`sources.gps.no_position`,
  `radar_runner/telemetry/README.md`), and runs the command again at 2, 6, 14 and 30 minutes
  without a position, then every 30 minutes. `setup/check.py` fails when gpsd has a receiver
  that it has not identified, or a fix time with mode 0. The cold-boot test on the bench
  (2026-11-19/20, top of this section) was done with gpsd passive: six clean cold boots. Without
  `-p` no cold boot was repeated, so whether gpsd ends up in this state by itself, and whether
  the service's run recovers it, was not shown; `-p` removes the mechanism believed responsible.

- **What:** a drop-in for `gpsd.service` that replaces its command line with
  `gpsd -n -p /dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00`
  and adds `Restart=on-failure`; `systemctl enable gpsd.service`; one restart of
  gpsd when it is not yet running with exactly that command line (a file that
  differs only in its comments needs none). `/etc/default/gpsd` is not touched.
- **Why:** as shipped, gpsd is started on demand (by its socket), opens the
  receiver only while a client is connected, and has no receiver after a
  restart of gpsd itself.
  - `-n`: gpsd reads the receiver all the time, so chrony always has GPS time
    (gpsd(8) asks for `-n` with a time daemon).
  - `-p` (from 2026-11-20): gpsd leaves the receiver's settings alone (top of
    this section).
  - enabled: gpsd runs from boot, whether or not anything connects.
  - the receiver on the command line, by its stable name: gpsd has it again
    after gpsd restarts. The name has no serial number, so a replacement
    receiver of the same type gets the same name.
- **Hotplug is systemd's, not Ubuntu's** (changed 2026-11-19, top of this
  section). Ubuntu's path (udev `60-gpsd.rules`, USB ID `1546:01a9`, through
  `gpsdctl@ttyACM<n>.service` running `gpsdctl add /dev/ttyACM<n>`) is switched
  off by the gpsdctl drop-in. Do not switch it back on: with both, the receiver
  was opened twice at boot ("Device or resource busy") and gpsd delivered
  nothing. Instead gpsd is bound to the receiver's device unit: systemd stops
  gpsd when the receiver goes and starts it, with the stable name, when the
  receiver comes back. A restart is needed because gpsd 3.25 drops a device
  that it cannot open (tested with a private gpsd instance: "device activation
  failed, freeing device") and never looks for it again. `USBAUTO` in
  `/etc/default/gpsd` no longer matters, since `gpsdctl add` is a no-op.
  Clients should still not filter on the device path (the telemetry service
  does not).
- **Load:** gpsd reading this receiver (10 Hz, all satellite messages) uses
  0.7 % of one core, all the time.
- **File:** `/etc/systemd/system/gpsd.service.d/radar-runner.conf`
- **Verify:** `ps -C gpsd -o args=`; `systemctl is-enabled gpsd`;
  `gpspipe -w -n 5`; unplug and replug the receiver and repeat.
- **Revert:** delete the file, `sudo systemctl daemon-reload`,
  `sudo systemctl disable gpsd.service`, `sudo systemctl restart gpsd`.

**Receiver settings (stored in the receiver, not on the card).** Checked on
2026-11-19 with `ubxtool -g <key>,<layer>` after a power cycle, before and
after an OLA reset (ZED-F9P, HPG 1.51, protocol 27.50):

| Key | RAM | BBR | Flash | Default |
|---|---|---|---|---|
| `CFG-UART1OUTPROT-NMEA` | 0 | 0 | 0 | 1 |
| `CFG-RATE-MEAS` (ms) | 100 | 100 | 100 | 1000 |
| `CFG-I2COUTPROT-NMEA` | 0 | 0 | 0 | 1 |
| `CFG-MSGOUT-UBX_NAV_PVT_USB`, `..._NAV_SAT_USB`, `..._NAV_SIG_USB` and five more that gpsd switches on | 1 | 1 | 1 | 0 |
| `CFG-MSGOUT-UBX_MON_SPAN_USB`, `..._MON_RF_USB` (from the `gps_jam` logger) | 10 | 10 | 10 | 0 |

The message settings change. Read again on 2026-11-19 after the 77-minute outage (above):

| Key | RAM | BBR | Flash | When |
|---|---|---|---|---|
| `CFG-MSGOUT-UBX_NAV_PVT_USB` | 1 | 0 | 0 | after the recovery at about 20:09, before the OLA start at 20:12 |
| `CFG-MSGOUT-UBX_NAV_DOP_USB` | 1 | 1 | 1 | the same |
| `CFG-MSGOUT-UBX_NAV_TIMEGPS_USB` | 1 | 1 | not read | the same |
| `CFG-USBOUTPROT-NMEA`, `CFG-USBOUTPROT-UBX` | 1 | 1 | 1 | the same |
| `CFG-MSGOUT-UBX_NAV_PVT_USB`, `..._NAV_DOP_USB`, `..._NAV_TIMEGPS_USB`, `..._NAV_SAT_USB`, `..._NAV_EOE_USB` | 1 | 1 | 1 | 20:55, after the OLA start at 20:12 |

So at about 20:09 NAV-PVT was off in battery-backed memory and flash while NAV-DOP and
NAV-TIMEGPS were on in both. If that was also so at the power-up (not measured), the receiver
started with exactly the two messages seen during the outage. By 20:55 NAV-PVT was 1 in all
three layers, which fits the OLA saving the receiver's configuration at its start (below). The
first table (earlier the same day) had NAV-PVT at 1 in all layers: what is in battery-backed
memory and flash is not stable, so `setup/gps_receiver.py` sets NAV-PVT in RAM rather than
relying on it.

- The setting that prevents the transmit-buffer overflow (`CFG-UART1OUTPROT-NMEA`
  = 0) **is in the receiver's flash and battery-backed memory already**, and it
  survived the power cycle of 2026-11-19 00:29. Settings that gpsd and the
  bench tools only ever wrote to RAM are in flash as well. The likely cause: the
  OLA's firmware saves the receiver's whole current configuration to flash
  whenever the OLA starts (the SparkFun library's `saveConfiguration()`).
  Nobody ran a save on purpose.
- An OLA reset (opening its serial port) changed none of the 30 keys read, in
  any layer. The receiver skipped one 100 ms epoch twice (while the OLA
  configured it); no NMEA and no error text appeared on USB; the fix was kept.
- No longer so (decided on this basis on 2026-11-19, reversed the same day):
  "nothing is applied at boot, and nothing in this repository writes to the
  receiver". `radar-gps-receiver.service` and the telemetry service set NAV-PVT
  and UART1 NMEA in RAM (`setup/gps_receiver.py --apply --ram`), and
  `setup/gps_receiver.py --apply` provisions the receiver once at setup (top of
  this section). The telemetry service also reports a receiver that is present
  but delivers no fix data (`sources.gps.health: error`), which is how the
  overflow showed. `setup/check.py --receiver` reads NAV-PVT and UART1 NMEA in
  all three layers and the transmit-buffer error flags, read-only.
- The OLA does save the receiver's configuration when it starts (measured
  2026-11-20, top of this section), so a value written to RAM only is in
  battery-backed memory and flash after the next OLA start.
- **A replaced or factory-reset receiver** is provisioned with
  `setup/gps_receiver.py --apply` (top of this section). Writing only
  `CFG-UART1OUTPROT-NMEA` by hand, as this section said before, leaves the
  message set gpsd needs unset.
- **Every OLA start touches the receiver.** Opening the OLA's serial port
  resets the OLA, and the OLA then configures the receiver and saves its RAM
  settings to battery-backed memory and flash. The telemetry service therefore opens the port once and
  spaces out reopenings after failures up to an hour
  (`radar_runner/telemetry/README.md`).
- Not changed, to decide: the receiver runs at 10 Hz because the OLA sets it
  (and saved it). gpsd then asks for all satellite messages at every epoch:
  21 kB/s on USB, whose transmit buffer has peaked at 88 %
  (`ubxtool -p MON-COMMS`).

### 12. Telemetry service

- **What:** `systemd/radar-telemetry.service` copied to `/etc/systemd/system/`,
  enabled and started if it is not enabled yet; the directory
  `/mnt/nvme/radar-runner` created for user `englacial` (the drive's top
  directory belongs to root). A running service is never restarted or stopped;
  one that was stopped by hand stays stopped. If another program has the OLA's
  serial port open (a bench run of `pa_guard.py`), the script stops there and
  does not enable or start the service: starting it would reset the OLA under
  that program.
- **Why and how it works:** `radar_runner/telemetry/README.md`.
- **Side effect:** the service keeps the OLA's serial port. The bench tools
  that open it (`pa_guard.py`, `pa_run.py`) cannot run until it is stopped:
  `sudo systemctl stop radar-telemetry`.
- **Verify:** `systemctl status radar-telemetry`;
  `python3 -m json.tool /run/radar-runner/user/telemetry.json`;
  `ls /mnt/nvme/radar-runner/telemetry/`
- **Revert:** `sudo systemctl disable --now radar-telemetry && sudo rm /etc/systemd/system/radar-telemetry.service && sudo systemctl daemon-reload`.
  The logs stay.

### 13. Time: chrony and fake-hwclock in place of systemd-timesyncd

- **What:** `apt-get install --no-install-recommends chrony fake-hwclock`
  (chrony 4.5-1ubuntu4, fake-hwclock 0.13, and chrony's dependency
  `tzdata-legacy`); apt removes `systemd-timesyncd`, which cannot be installed
  together with chrony. Three files:

  | File | What it does |
  |---|---|
  | `/etc/chrony/conf.d/radar-runner.conf` | GPS time from gpsd: `refclock SHM 0 refid GPS poll 3 offset 0.070 delay 0.2` |
  | `/etc/systemd/system/chrony.service.d/radar-runner.conf` | `chronyc makestep 1 -1` after chrony starts: step the clock whenever it is off by more than 1 s |
  | `/etc/systemd/system/fake-hwclock-save.timer.d/radar-runner.conf` | save the clock every minute (the package: every hour) |

  Ubuntu's `/etc/chrony/chrony.conf` (NTP pools, `rtcsync`) is not changed.
- **The install is guarded.** The script first asks apt what it would do
  (`apt-get -s`) and stops, with nothing changed, if anything but
  `systemd-timesyncd` would be removed or any installed package upgraded. It
  downloads everything first, including `systemd-timesyncd` itself. If the
  install fails, or chrony does not run afterwards, it installs
  `systemd-timesyncd` again from the downloaded copy and says so. apt runs
  non-interactively with needrestart switched off for the call (section 6 makes
  it list-only anyway); no package script in this set touches ssh or tailscaled.
  Without internet the step fails at the download and nothing changes; it is
  the last step, so everything else is applied. If the install itself was cut
  short (power, a killed session), the next run says so and names the command
  that finishes it: `sudo dpkg --configure -a`. The once-a-minute clock save
  adds two lines a minute to the journal ("Starting", "Finished"); that is
  left as it is.
- **Why chrony:** it takes time from gpsd as well as from NTP, and works with
  neither. No PPS reaches the Pi, so GPS time is the time of the receiver's
  messages over USB: they arrive about 70 ms late (measured against NTP: median
  69 ms, 58-82 ms), which `offset 0.070` corrects, and `delay 0.2` makes chrony
  prefer NTP servers whenever it can reach them. gpsd delivers time only once
  the receiver has a position fix.
- **Why fake-hwclock:** the Pi's clock chip has no battery (after a long
  power-off it reads 1970-01-01; after a short one it is still right).
  `systemd-timesyncd` saved the time about every minute and restored it at
  boot. Ubuntu's chrony does not. Compared:

  | Way to restore the time at boot | Behind after a clean shutdown | Behind after a hard power cut |
  |---|---|---|
  | `systemd-timesyncd` (before) | by the off time | off time + up to about 1 min |
  | `chronyd -s` with its drift file | by the off time | off time + up to 1 h; the whole session if chrony never had a time source in it (the drift file is written at exit, and at most hourly while synchronised) |
  | `fake-hwclock` as packaged | by the off time | off time + up to 1 h |
  | **`fake-hwclock`, saving every minute (chosen)** | by the off time | off time + up to 1 min |
  | the initramfs `fixrtc` script alone (root file system's last mount time) | by the whole previous session | the same |

  `chronyd -s` would also read the clock chip itself, a second mechanism to
  reason about. fake-hwclock is one shell script: `date > /etc/fake-hwclock.data`
  every minute and at shutdown, `date -s` at boot (before `sysinit.target`) if
  the saved time is later than the clock. Like `systemd-timesyncd`, it saves
  whatever the clock says, so the clock never runs backwards from boot to boot.
- **What the clock does at boot:**

  | Boot | Clock |
  |---|---|
  | always, first | the kernel reads the clock chip (right after a short outage, 1970 after a long one); `fixrtc` raises it to the root file system's last mount time; fake-hwclock raises it to the last saved time. It is now behind by the time the Pi was off (plus up to 1 min after a hard cut), never ahead |
  | with internet | chrony reaches the NTP pools within seconds of the link coming up and steps the clock. GPS is then a second source, within its 0.1 s margin |
  | no internet, GPS with sky view | about half a minute after the receiver has a fix (and gpsd has seen four fixes), chrony steps the clock to GPS time, good to a few tens of milliseconds |
  | neither | the clock keeps running from the restored time, behind by the off time. Logs are ordered by boot counter and uptime. When GPS or the internet appears later, even hours later, chrony steps the clock then |

  After a long offline period chrony looks up the NTP pool names again with
  growing pauses (up to about an hour), so NTP can take that long to come back
  after the link does; GPS covers the gap.
- **Why step at any time (`makestep 1 -1`):** Ubuntu's `makestep 1 3` allows a
  step only in chrony's first three clock updates (24 s after the first GPS
  time). If that first time was wrong, the clock would then be slewed, at
  most 5 s per minute, for the rest of the day. Stepping whenever the clock is
  off by more than 1 s costs a visible jump in the wall clock, which nothing
  here depends on: status ages use uptime, the logs carry uptime in every row
  and start a new file when the date changes, and the telemetry service logs
  each step. It is set with `chronyc` from the service drop-in because a file in
  `/etc/chrony/conf.d` cannot override the `makestep 1 3` that follows it in
  Ubuntu's `chrony.conf`. If the command fails, chrony runs with `makestep 1 3`.
- **Not verifiable before the install** (no root here): that chrony attaches
  to gpsd's shared memory under Ubuntu's AppArmor profile (from the source it
  does so as root before dropping privileges, and segment 0 belongs to root),
  the timer change, and the whole install on this system.
- **Verify:** `chronyc sources` (a `#* GPS` or `#+ GPS` line with a non-zero
  reach; `^*` marks a selected NTP server); `chronyc tracking`;
  `journalctl -b -u chrony | grep makestep` shows `New makestep 1.000000 -1`;
  `cat /etc/fake-hwclock.data` is less than a minute old;
  `systemctl list-timers fake-hwclock-save.timer`.
  Without internet: unplug the network, reboot, and watch `chronyc sources`
  select GPS and `journalctl -b -u chrony` report the step.
- **Revert:** `sudo apt-get install systemd-timesyncd` (removes chrony),
  `sudo apt-get remove fake-hwclock`, delete the three files,
  `sudo systemctl daemon-reload`.
- **An alternative in hardware:** a clock battery on the Pi 5's J5 connector
  would keep the right time across power-offs and make fake-hwclock a fallback.

### 14. Radar manager: unit installed, `radarctl` linked; not enabled, not started

- **What:** `systemd/radar-manager.service` copied to `/etc/systemd/system/`, and the link
  `/usr/local/bin/radarctl` -> `bin/radarctl` in the repository. **The unit is not enabled and
  not started by the script**, and a running manager is never restarted or stopped (a restart
  stops a running radar). The first start is done by hand with the radar under supervision:
  `radar_runner/manager/README.md`, "First start with the real radar". After that:
  `sudo systemctl enable --now radar-manager`.
- **Why and how it works:** `radar_runner/manager/README.md`. The unit's stop hook
  (`bin/radar-manager-stop-hook`) runs the make-amplifier-safe tool after the manager has
  stopped or died, if the record `/var/lib/radar-runner/user/sdr-state` does not say `safe`.
- **Not applied, printed only: the power service's hooks.** The script prints the two lines for
  `[hooks]` in `/etc/radar-runner/power.toml` (`stop_radar`: `radarctl stop --interrupt --wait
  --timeout 4` with a 5 s limit; `amp_safe`: `bin/radar-manager-stop-hook --timeout 10
  /var/lib/radar-runner/user/sdr-state` with a 12 s limit, which runs the tool only if the
  record does not say `safe` and the SDR is not in its boot loader; both through
  `setpriv`, so that root never runs a file from the user's checkout). Put them in by hand
  when the manager has passed its first start, then `sudo systemctl restart radar-power`
  (on the bench, with nothing at stake: a restart of the power service is itself a change
  to review). Worst case of the shutdown sequence with these limits: commit at about 10 s,
  then 5 + 1 s, 12 + 1 s, sync 5 s, record 3 s: the power-off command at about 37 s
  after the loss and the last resort at 47 s, against about 58 s of capacitor at room
  temperature (not measured cold). The normal case is far shorter: the radar is gone in
  under a second, the manager records `safe` and the tool does not run at all (it takes
  3.5 s when it does). `setup/check.py` checks the installed hooks against this.
- **Also needed by hand before the first start:** `/etc/radar-runner/radar.yaml` (the ORCA
  config to run) and the unit's limits (`manager.limits` in `/etc/radar-runner/local.yaml`).
- **Verify:** `setup/check.py` (section "setup.sh: radar manager"); `radarctl status`.
- **Revert:** `sudo systemctl disable --now radar-manager; sudo rm /etc/systemd/system/radar-manager.service /usr/local/bin/radarctl && sudo systemctl daemon-reload`.
  Collections on the data drive and the files in `/var/lib/radar-runner/user/` stay.

### 15. Safety watcher

- **What:** `systemd/radar-watcher.service` copied to `/etc/systemd/system/`
  and enabled, so that it runs from the next boot. **The script does not start
  it:** the first start is by hand, with someone watching the amplifier
  (`sudo systemctl start radar-watcher`), because from then on it may run the
  SDR GPIO reset tool and stops radar processes. A running watcher is never
  restarted or stopped. It needs no directory of its own: its status file goes
  to `/run/radar-runner/user`, its event log to `/var/lib/radar-runner/user`
  (item 1). The script says whether the SDR GPIO reset tool is built; it does
  not build it (`tools/sdr_gpio_safe/README.md`, "Build", as user `englacial`).
- **Why and how it works:** `radar_runner/watcher/README.md`. It runs with
  `Nice=-5` and `OOMScoreAdjust=-900` so that it keeps running on a busy Pi, and
  with `KillMode=process` so that systemd never kills a reset tool that the
  watcher started (a tool killed while it initialises the SDR leaves the
  amplifier enabled).
- **Side effects, once it runs (also after every boot):** a radar started
  outside the radar manager (`pa_run.py`, the HIL harness) is stopped when the
  watcher's status is not ok, which it is whenever the telemetry service is
  stopped or the amplifier's 28 V is off. The reset tool is run (a UHD
  initialisation: the amplifier is enabled for about 0.5 s) when the amplifier
  draws more than 0.5 A for 5 s with no radar process, and after 20 s without
  telemetry if the record of the pin does not say `safe`. **For bench sessions
  stop two units first:** `sudo systemctl stop radar-watcher radar-telemetry`.
- **Verify:** `systemctl status radar-watcher`;
  `python3 -m json.tool /run/radar-runner/user/watcher.json` (`ok`, `reason`);
  `tail /var/lib/radar-runner/user/watcher-events.jsonl`
- **Revert:** `sudo systemctl disable --now radar-watcher && sudo rm /etc/systemd/system/radar-watcher.service && sudo systemctl daemon-reload`.
  The event log stays.

### 16. Alerts service

- **What:** `systemd/radar-alerts.service` copied to `/etc/systemd/system/`,
  enabled and started if it is not enabled yet (a running one is never
  restarted or stopped), and `/usr/local/bin/alertctl` linked to
  `bin/alertctl`. Its files go to `/var/lib/radar-runner/user` (state, queue,
  inbox) and `/run/radar-runner/user/alerts.json` (item 1). It needs
  `/etc/radar-runner/secrets.yaml` (mode 0640, `root:englacial`) with
  `alerts: ntfy: {server, topic, token}`; the script only says whether the file
  exists and never reads it. Added 2026-11-19 (stage 5).
- **Why and how it works:** `radar_runner/alerts/README.md`. It only reads the
  other services' files. The unit allows writes only in the two `user`
  directories (`ProtectSystem=strict`), runs at `Nice=10` with
  `MemoryMax=200M` and `OOMScoreAdjust=500`, and no other unit depends on it.
- **Side effect:** a boot summary goes to ntfy at every boot (at 3 minutes of
  uptime), and alerts whenever the other services report faults.
- **Verify:** `setup/check.py` (section "setup.sh: alerts"); `alertctl status`;
  `alertctl test --wait 60`
- **Revert:** `sudo systemctl disable --now radar-alerts && sudo rm /etc/systemd/system/radar-alerts.service /usr/local/bin/alertctl && sudo systemctl daemon-reload`.
  Its state and queue (`/var/lib/radar-runner/user/alerts-*`) may be deleted.

## Deferred to later stages (not done by `setup.sh`)

- **rclone**: stage 9, with the offload. Ubuntu's package is 1.60.1 (2022);
  check then that it is recent enough.

## Optional: only with a flag, after deciding

### `--nvme-by-label`: mount the data drive by label

- **What:** in `/etc/fstab`, the `/mnt/nvme` line's `UUID=...` becomes
  `LABEL=<the label the drive has now>`. Nothing else on the line changes. To
  use another label first: `sudo e2label /dev/nvme0n1p1 radar-data`, then run
  the script; that order works. A `LABEL=` line that names a label the
  mounted drive does not have is corrected the same way; one that matches is
  left alone.
- **Why:** any drive with the right label then works as a replacement.
- **Label cache:** `e2label` on a mounted drive sets the label through an
  ioctl, and udev is not told, so `lsblk`, `findmnt -o LABEL` and
  `/dev/disk/by-label/` keep the old label until the next boot. The script
  therefore reads the label from the filesystem itself (`setup/fs_label.py`,
  no root needed) and says when udev's differs; the next boot refreshes it.
  Without root, `findmnt --verify` cannot find the new label yet; the script
  accepts that one error, for that line only, when the mounted filesystem has
  the label. `setup/check.py` fails a line that would not mount the drive
  mounted now, and reports a stale cache. By hand: `setup/fs_label.py
  /mnt/nvme` (the filesystem's label) against `lsblk -no LABEL
  /dev/nvme0n1p1` (udev's) and the `/mnt/nvme` line of `/etc/fstab`.
- **Do not refresh udev by hand** (`udevadm trigger`) for the mounted data
  drive. On a unit that booted with a `LABEL=` line, `mnt-nvme.mount` requires
  `systemd-fsck@dev-disk-by\x2dlabel-<label>.service`, which is bound to the
  `/dev/disk/by-label/<label>` device. A change event after a relabel removes
  that link, and systemd can then stop the fsck service and with it unmount
  the drive. For the same reason, do not relabel the data drive while it is
  mounted on a unit that booted by label, unless a reboot follows directly.
  On a unit that booted by `UUID=` (as before this option), relabel, run the
  script, and reboot.
- **Risk:** `/etc/fstab` is not a drop-in file; a broken line can stop the boot
  and with it SSH. The script therefore only touches a line that has `nofail`,
  changes one field, checks the result with `findmnt --verify`, refuses a label
  that is missing or used twice, and keeps `/etc/fstab.before-radar-runner`.
  Remaining risk: two drives that later carry the same label (the second NVMe
  drive, once formatted) make the mount ambiguous. Test with a reboot on the bench.
- **Revert:** `sudo cp /etc/fstab.before-radar-runner /etc/fstab`, reboot.

### `--hold-packages`: hold kernel, firmware, bootloader, gpsd and tailscale

- **What:** `apt-mark hold` on the installed ones of `linux-image-raspi`,
  `linux-headers-raspi`, `linux-raspi`, the running kernel's image and modules
  packages, `linux-firmware-raspi`, `rpi-eeprom`, `flash-kernel`, `gpsd`,
  `gpsd-clients`, `libgps30t64`, `chrony`, `tailscale`.
- **Why:** a manual `apt upgrade` then leaves them alone. Automatic updates are
  already off (sections 4 and 5), so this only guards against a person.
- **Risk:** held packages also miss security fixes and can block other
  upgrades until someone remembers `apt-mark unhold`.
- **Verify / revert:** `apt-mark showhold` / `sudo apt-mark unhold <package>`

## Already present on `englacial-radar-1`, not automated

`setup/check.py` checks the items marked with *.

| Item | State | Needed for |
|---|---|---|
| uv 0.13.7 in `~/.local/bin` (no root) | installed 2026-11-18 from the GitHub release `uv-aarch64-unknown-linux-gnu.tar.gz`, SHA-256 `71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc` * | building `.venv` |
| `.venv` and `wheelhouse/` in this repository (no root) | built by `setup/fetch_wheels.py` and `setup/build_venv.sh` * | all services except the power service |
| `/etc/radar-runner/power.toml` with `armed = true` | set by hand in stage 1 | power service armed |
| `dtparam=i2c_arm=on` in `/boot/firmware/config.txt` * | in the stock image | INA219 on `/dev/i2c-1` |
| User `englacial` in groups `dialout`, `i2c` * | set at provisioning | GPIO, serial ports and I2C without root |
| `/etc/fstab` line for `/mnt/nvme` (by UUID, `noatime,nofail,x-systemd.device-timeout=10s`) * | added by hand | data drive |
| `englacial - rtprio 99` in `/etc/security/limits.conf` * | added by hand (an edit of a distribution file) | realtime threads of the radar |
| `/etc/udev/rules.d/uhd-usrp.rules` * | copied from UHD | SDR access without root |
| Packages added at provisioning (cloud-init) | `gpsd gpsd-clients git i2c-tools mosh rpi.gpio-common net-tools cmake exfat-fuse g++ util-linux-extra`; `modemmanager` removed | GPS, builds, tools |
| Packages added later by hand | `tailscale` (from its own apt repository), `gh`, `unzip`, `poppler-utils`, `uhubctl`, `avahi-daemon libnss-mdns` | remote access, tools |
| Tailscale logged in, key expiry disabled in the admin console * | done by hand | remote access |
| SSH enabled (`ssh.socket`) * | stock image | remote access |
| Network configuration | changed by hand on 2026-11-14 (`~/20261114-network-backups`); not described here and never touched by this repository | Starlink, local access |
| miniforge in `~/miniforge3` with the `orca` environment * | installed by hand | ORCA chirp generation, UHD build |
| Patched UHD in `~/uhd-patched-gpio` (source `~/uhd-gpio` at the commit in `setup/pins.toml`, `ORCA_UHD_BUILD.yaml`) * | built by hand in the `orca` environment: `~/orca/tests/hil/scripts/build_uhd.sh ~/uhd-gpio ~/uhd-patched-gpio`; patches: `~/radar-testbench/UHD_751_FIX.md` (TR switch) and `docs/uhd-proposals/` (front-panel pins) | the radar, the GPIO reset tool |
| ORCA checkout in `~/orca` * | cloned by hand | the radar |
| Bootloader EEPROM: version 2026/01/21, `BOOT_ORDER=0xf461`, defaults otherwise * | stored in the Pi, not on the card | boot |

## Considered and left alone

- `motd-news.timer`, `update-notifier-*.timer`, `ua-timer.timer`,
  `apport-autoreport.timer`, snapd itself: they fetch or report, and install nothing.
- `rsyslog` writes a second copy of the log to `/var/log/syslog` (rotated
  weekly); `sysstat` samples every 10 minutes. Both are small.

## Identity: never copied between units

Hostname, machine ID, SSH host keys and the Tailscale login. `check.py` prints
them as `identity:` lines; the same value on two systems means a card was
cloned without giving the copy its own identity.

## Rebuild from the stock Ubuntu image: what is still done by hand

In this order, before `setup/setup.sh`:

1. Flash Ubuntu Server 24.04 for Raspberry Pi; create user `englacial`; set
   the hostname; add the user to `dialout` and `i2c`.
2. Network and remote access: network configuration, SSH keys, install
   Tailscale and log in, disable key expiry for the node in the admin console.
3. Packages: the two lists in the table above.
4. `/etc/fstab` line for the NVMe drive (with `nofail`), `rtprio` line in
   `/etc/security/limits.conf`, UHD udev rules.
5. miniforge and the `orca` environment; the patched UHD build in
   `~/uhd-patched-gpio` (and no other UHD that a program here could load:
   `setup/check.py` checks what the reset tool and the radar load); the ORCA
   checkout at the pinned commit; then build the reset tool
   (`tools/sdr_gpio_safe/README.md`).
6. This repository in `/home/englacial/englacial-radar-runner` (the unit file
   expects that path) at the release tag; uv into `~/.local/bin` (verify the
   checksum above); copy `wheelhouse/` from another unit or run
   `setup/fetch_wheels.py`; then `setup/build_venv.sh`.
7. `sudo setup/setup.sh` (in tmux), reboot, `setup/check.py --receiver`, then the three
   watchdog bench tests of section 8.
8. Per-unit files in `/etc/radar-runner/`: `power.toml` (arm it after the
   stage 1 tests), `local.yaml` (calibration and thresholds), `secrets.yaml`
   (`root:englacial`, mode `0640`).
9. Settings stored in hardware: bootloader EEPROM (compare with `check.py`).
   The OLA's ADC settings are applied by the telemetry service at every start.
   The GPS receiver's settings are in its flash: provision a new or replaced
   receiver once with `setup/gps_receiver.py --apply`, then restart the
   telemetry service once, and run `setup/check.py --receiver` again (section
   11; on a receiver not yet provisioned it fails in step 7). The receiver check after every start
   of gpsd and the telemetry service set NAV-PVT and UART1 NMEA in RAM if needed.

Not yet stored for a rebuild without internet: the Ubuntu image, the `.deb`
files of the packages above, the UHD source and FPGA images.
