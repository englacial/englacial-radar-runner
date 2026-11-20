# A card for a second unit, from the first unit's image

`setup/new_unit_image.py` turns a copy of a unit's SD-card image into the card for a second,
identical set of hardware (the lab unit that tests updates before they go to the field unit). It
runs as root on another Linux computer, on the image file, before it is flashed. The clone never
boots with the first unit's identity, so it never reaches a network as that unit.

It needs only Python 3 and the usual tools (`losetup`, `mount`, `blkid`, `ssh-keygen`); copy the
one file to that computer. A spare card for the *same* Pi is a plain copy and needs none of this
(plan, "Reproducing the setup").

## What it changes in the image

| Area | Change | Why |
|---|---|---|
| Hostname | `/etc/hostname`, the `127.0.1.1` line of `/etc/hosts`, `hostname:` in the boot partition's `user-data`, cloud-init's `previous-hostname` and `set-hostname`, and `hostname:` in cloud-init's cached `cloud-config.txt` (the run stops if that file still names the old host) and copies of the user-data; the stale `obj.pkl` removed | The image got its hostname from cloud-init. At every boot cloud-init reads the boot partition's `user-data` again and rewrites `/etc/hosts` from it, but first sets the hostname from the previous boot's `cloud-config.txt`: with the old name left there, the clone would spend its first boot as the field unit. The instance ID is not changed, so no first-boot module runs again (users, SSH keys, packages, `write_files`) |
| Machine ID | a new random one in `/etc/machine-id` (never empty: systemd would then apply unit presets at the next boot); `/var/lib/dbus/machine-id` is a link to it; the journal directory named after the old ID and `/var/lib/systemd/random-seed` removed | Two units must not share it (DHCP identity, journal) |
| SSH host keys | new keys of each type present, made here with `ssh-keygen` (`root:root`, 0600 and 0644); fingerprints printed | Nothing in this image makes host keys at boot (only cloud-init at its first boot), and without keys sshd does not start |
| Tailscale | everything in `/var/lib/tailscale` and `/var/cache/tailscale` removed | The unit starts logged out and is logged in as a new node. Never log either unit out instead: they share one node key, and logging out expires it, which cuts off the field unit |
| `/etc/radar-runner/local.yaml` | replaced by a template: `max_tx_gain` 43 (the repository's default; `--max-tx-gain`), turn-on check not blocking, no expected values | The first unit's 70 belongs to its amplifier board and gain divider |
| `/etc/radar-runner/radar.yaml` | `RF0.tx_gain` lowered to the cap if above it; nothing else, comments kept | The manager refuses a config above the cap at every start (a fatal fault). Lowered, the new unit has a config it may run, at a power safe for an amplifier not yet measured |
| `/etc/radar-runner/power.toml` | kept (same hardware); `--disarm-power` sets `armed = false` | |
| `/var/lib/radar-runner` | power guard state, boot counter, event logs, alerts state, queued alerts, overrides, motion block, fallback sensor logs removed; the SDR record set to `unknown`; any file the tool does not know stops the run | All of it describes the first unit's history |
| `/var/lib/chrony/chrony.drift` | removed | The first unit's clock drift |
| `/etc/fstab` | the `/mnt/nvme` line mounts `LABEL=radar-data` (`--label`) instead of the first drive's UUID; the options are kept. Refused without exactly one such line with `nofail` | Any drive with the label works |
| ntfy alerts (`secrets.yaml`) | kept by default: both units then post to one topic (each alert names its hostname). `--ntfy-topic NAME` sets another topic, `--no-alerts` removes the alerts section. Owner and mode are kept; the file is never printed | |
| `/etc/radar-runner/new-unit-image.json` | the record that the tool has run (time, hostnames). A second run needs `--force` | |

Not touched: the repository checkout, the Python environments, ORCA, the UHD build, the home
directory, the network configuration. The report lists what is left that you may want to clear by
hand (shell history, bench files, Claude Code data, the GitHub CLI login, the user's own SSH key,
system logs, snapd's device identity).

The tool stops at the first unexpected state, before it changes anything, and in a dry run
writes nothing. Paths are resolved inside the image: an absolute symlink in the image points into
the image, never at the computer's own files. It refuses the computer's own root, and a tree
that is on the same file system as, or on the same whole disk, btrfs device or ZFS pool as, any of
the computer's `/`, `/boot`, `/boot/efi`, `/boot/firmware`, `/usr`, `/var` and `/home` (from
the device numbers and the kernel's mount table). It cannot see further than that, for example
into a network file system or a disk image the computer itself runs from.

## Procedure

1. **Copy the image.** Keep the first unit's image as it is and work on a copy. With the card in
   a reader on the other computer (check the device with `lsblk`; here `/dev/sdX`):

   ```
   sudo dd if=/dev/sdX of=englacial-radar-1.img bs=4M status=progress conv=fsync
   cp --sparse=always englacial-radar-1.img englacial-radar-2.img
   ```

2. **Dry run** on the copy. It attaches the image read-only, prints every change and the report,
   and writes nothing:

   ```
   sudo python3 new_unit_image.py image englacial-radar-2.img --hostname englacial-radar-2 --dry-run | tee dry-run.txt
   ```

   Read it: the identity lines, the state files, the fstab line, the alerts note, and anything
   listed as still naming the old hostname.

3. **Real run**, with the same options:

   ```
   sudo python3 new_unit_image.py image englacial-radar-2.img --hostname englacial-radar-2 | tee englacial-radar-2-report.txt
   ```

   Options: `--label`, `--max-tx-gain`, `--disarm-power`, `--ntfy-topic` or `--no-alerts`. The
   report holds no secrets; keep it (it has the new SSH fingerprints). If the run stops half way,
   start again from a fresh copy.

4. **Flash** the copy to the new card (at least as large as the first one), and do not use an
   imager's OS customisation: it writes a new `user-data` with a new instance ID, and cloud-init
   would then run its first-boot modules again.

   ```
   sudo dd if=englacial-radar-2.img of=/dev/sdY bs=4M status=progress conv=fsync
   ```

For a card that is already mounted (both partitions), the same core runs on the two directories:

```
sudo python3 new_unit_image.py tree --root /media/$USER/writable --boot /media/$USER/system-boot --hostname englacial-radar-2 --dry-run
```

## On the new unit, after its first boot

In this order (the report prints the same list):

1. Tailscale: `sudo tailscale up` and open the link it prints, as a new node; in the admin console
   disable key expiry for it; then `sudo tailscale set --auto-update=false` (that setting was in
   the removed state; SYSTEM_CHANGES.md section 5).
2. `setup/check.py`: the `identity:` lines (hostname, machine-id, SSH host keys, Tailscale node)
   must all differ from the field unit's.
3. Label the data drive: `sudo e2label /dev/nvme0n1p1 radar-data`, then `sudo mount /mnt/nvme`
   (or reboot) and `sudo setup/setup.sh`, which creates `/mnt/nvme/radar-runner` on it.
4. Provision the GPS receiver once: `setup/gps_receiver.py --apply`,
   `sudo systemctl restart radar-telemetry`, `setup/gps_receiver.py`, `setup/check.py --receiver`
   (SYSTEM_CHANGES.md section 11).
5. Compare the bootloader settings with the field unit's (`setup/check.py`; `rpi-eeprom-config`).
6. Measure the amplifier's gain (output power against `tx_gain`, with this unit's VVA) before
   raising `manager.limits.max_tx_gain` in `local.yaml` and `RF0.tx_gain` in `radar.yaml`.
7. Turn-on check: once a start at low power is verified on this unit, put the expected values
   that `tools/turn_on_check/measure.py` prints in `local.yaml`.
8. The power service's bench tests on the new hardware (`power/README.md`) and the three
   watchdog tests (SYSTEM_CHANGES.md section 8).
9. A `HARDWARE_CONFIG.md` for this unit.

## Tests

`tests/common/test_new_unit_image.py` runs the core on a fake image tree without root. The image
wrapper (loop device and mounts) and the file owners need root and are not covered there.
