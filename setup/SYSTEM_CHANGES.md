# System changes for the radar manager

Every change outside this repository. Use it to rebuild a system, to compare
two systems, or to undo a change. Add each new system change here when it is made.

Reference host: `englacial-radar-1`, Raspberry Pi 5, Ubuntu 24.04.5 LTS, kernel
`6.8.0-1068-raspi`, systemd 255. Written 2026-11-18 (stage 0).

```
setup/setup.sh --dry-run     # show what would change; no root needed
tmux                         # a dropped SSH session must not interrupt the next command
sudo setup/setup.sh          # apply sections 1-10; safe to run again, also after an interruption
sudo reboot                  # logind and journald read their files at boot
setup/check.py               # one PASS/FAIL line per item below
```

The script only writes files that this project owns (copied from
`setup/files/`, same path as on the system), so reverting a change is deleting
its file. It never touches network configuration or SSH, installs or removes no
package, never restarts or stops the power service, and never reboots. Its one
Tailscale change is section 5.

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

## Deferred to later stages (not done by `setup.sh`)

- **chrony** (replacing `systemd-timesyncd`): stage 2, together with its GPS
  time source. Not before, because this Pi has no clock battery:
  `systemd-timesyncd` saves the time regularly and restores it at boot, so the
  clock at least never runs backwards. chrony as Ubuntu configures it does not,
  and every boot without internet would start at the time of an earlier boot.
  `check.py` shows which time daemon is running.
- **rclone**: stage 9, with the offload. Ubuntu's package is 1.60.1 (2022);
  check then that it is recent enough.

## Optional: only with a flag, after deciding

### `--nvme-by-label`: mount the data drive by label

- **What:** in `/etc/fstab`, the `/mnt/nvme` line's `UUID=...` becomes
  `LABEL=<the label the drive has now>` (today `orca-hil`). Nothing else on the
  line changes. To use another label first: `sudo e2label /dev/nvme0n1p1 radar-data`.
- **Why:** any drive with the right label then works as a replacement.
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
| Patched UHD in `~/uhd-patched` (source `~/uhd`, `ORCA_UHD_BUILD.yaml`) * | built by hand, see `~/radar-testbench/UHD_751_FIX.md` | the radar |
| ORCA checkout in `~/orca` * | cloned by hand | the radar |
| Bootloader EEPROM: version 2026/01/21, `BOOT_ORDER=0xf461`, defaults otherwise * | stored in the Pi, not on the card | boot |
| `systemd-timesyncd` as the time daemon * | stock image; see "Deferred" | clock |

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
   `~/uhd-patched`; the ORCA checkout at the pinned commit.
6. This repository in `/home/englacial/englacial-radar-runner` (the unit file
   expects that path) at the release tag; uv into `~/.local/bin` (verify the
   checksum above); copy `wheelhouse/` from another unit or run
   `setup/fetch_wheels.py`; then `setup/build_venv.sh`.
7. `sudo setup/setup.sh` (in tmux), reboot, `setup/check.py`, then the three
   watchdog bench tests of section 8.
8. Per-unit files in `/etc/radar-runner/`: `power.toml` (arm it after the
   stage 1 tests), `local.yaml` (calibration and thresholds), `secrets.yaml`
   (`root:englacial`, mode `0640`).
9. Settings stored in hardware: bootloader EEPROM (compare with `check.py`);
   the GPS receiver and OLA settings are applied at every boot from stage 2.

Not yet stored for a rebuild without internet: the Ubuntu image, the `.deb`
files of the packages above, the UHD source and FPGA images.
