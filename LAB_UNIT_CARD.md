# Making the lab unit's SD card from the field unit's image

Short instructions for one job: turn a copy of the image of `englacial-radar-1`'s card (taken
2026-11-20) into the card for the second set of hardware. The full description, every option and
the reasons are in `setup/NEW_UNIT.md`.

Done on a Linux computer, as root, on the image file. The tool's loop-mount part has only been
tested with stand-ins, never on a real image: **do the dry run first, and keep the untouched
image until the new card works.** If the tool stops, keep its message; exit status 2 means
nothing was changed.

## Before you start

- The image file is as sensitive as the card (Tailscale node key, SSH host keys, alerts token,
  Wi-Fi password). Keep it readable by root only.
- Never run `tailscale logout` on either unit, and never remove or expire `englacial-radar-1`
  in the Tailscale admin console: the clone starts with the field unit's key, and expiring it
  cuts off the field unit. The tool removes the key from the copy, so the clone never uses it.
- Do not use an imager's "OS customisation" when flashing.

## 1. Get the tool and make a working copy

`new_unit_image.py` is one file and needs only Python 3, `losetup`, `mount`, `blkid`,
`ssh-keygen` and `e2fsck`. Take it from the pushed repository:

```
git clone git@github.com:englacial/englacial-radar-runner.git      # or pull an existing clone
cd englacial-radar-runner/setup
```

With the untouched image at `/path/to/englacial-radar-1.img`:

```
umask 077
sudo cp --sparse=always /path/to/englacial-radar-1.img englacial-radar-2.img
sudo chmod 600 /path/to/englacial-radar-1.img englacial-radar-2.img
```

## 2. Dry run (changes nothing)

```
set -o pipefail
sudo python3 -u new_unit_image.py image englacial-radar-2.img \
    --hostname englacial-radar-2 --disarm-power --dry-run | tee dry-run.txt
echo "exit status $?"
```

Expect status 0 and a list of changes: hostname, machine ID, SSH host keys, Tailscale state
removed, `local.yaml` replaced (cap 43), `radar.yaml` `tx_gain` lowered to 43, the state under
`/var/lib/radar-runner` reset, the `/mnt/nvme` line in `/etc/fstab` changed to
`LABEL=radar-data`, a note that both units would share one alerts topic.

Options you may want (same in step 3):

| Option | Effect |
|---|---|
| `--ntfy-topic NAME` | a separate alerts topic for the lab unit |
| `--no-alerts` | no alerts from the lab unit |
| `--disarm-power` | the power service does not shut the Pi down until you arm it (recommended until its bench tests pass on the new hardware) |
| `--max-tx-gain N` | another `tx_gain` cap than 43 (do not raise it before the second amplifier's gain is measured) |

## 3. Real run

Same command without `--dry-run`, in the same shell:

```
sudo python3 -u new_unit_image.py image englacial-radar-2.img \
    --hostname englacial-radar-2 --disarm-power | tee englacial-radar-2-report.txt
echo "exit status $?"
```

| Status | Meaning |
|---|---|
| 0 | done and cleaned up |
| 2 | stopped before anything was changed |
| 3 | still mounted or attached: do not flash until `losetup -j englacial-radar-2.img` prints nothing |
| 5, or "partly changed" in the message | start again from a fresh copy |

Keep the report: it has the new SSH host key fingerprints and the list of what to do on the unit.

## 4. Check the result, then flash

```
sudo losetup --find --show --partscan --read-only englacial-radar-2.img     # prints /dev/loopN
sudo e2fsck -fn /dev/loopNp2
sudo losetup -d /dev/loopN
```

No errors: flash. Find the new card with `lsblk` first; the `dd` overwrites that device.

```
lsblk
sudo umount /dev/sdY1 /dev/sdY2
sudo dd if=englacial-radar-2.img of=/dev/sdY bs=4M status=progress conv=fsync
```

## 5. First boot of the lab unit

The image is older than the repository (commit 4a661fc: before the turn-on check, the raw GPS
log and this tool), so the unit is updated right after it has a network identity of its own.

1. Tailscale, as a new node, then no automatic updates:

   ```
   sudo tailscale up --ssh --advertise-tags=tag:radar
   sudo tailscale set --auto-update=false
   ```

2. Update the software and the system settings, then reboot:

   ```
   git -C ~/englacial-radar-runner pull
   git -C ~/orca pull
   cd ~/englacial-radar-runner && sudo setup/setup.sh        # in tmux
   sudo reboot
   ```

3. The data drive: `/etc/fstab` now expects a drive labelled `radar-data`. Find the drive with
   `lsblk`. A new drive: a partition, then `sudo mkfs.ext4 -L radar-data <partition>`. A drive
   with a file system, while it is **not** mounted: `sudo e2label <partition> radar-data`. Then
   reboot and run `sudo setup/setup.sh` once more (it creates `/mnt/nvme/radar-runner`).
4. The GPS receiver, once: `setup/gps_receiver.py --apply`, then
   `sudo systemctl restart radar-telemetry` and `setup/check.py --receiver`.
5. `setup/check.py`: no FAIL line, and its `identity:` lines all differ from the field unit's.
6. Before transmitting above the low cap: measure the second amplifier's gain and set this unit's
   `manager.limits.max_tx_gain` in `/etc/radar-runner/local.yaml` and `RF0.tx_gain` in
   `/etc/radar-runner/radar.yaml`. The field unit's values (cap 70, with its own VVA divider) do
   not carry over.
7. The rest of the list in `setup/NEW_UNIT.md` ("On the new unit, after its first boot"): the
   amplifier current calibration, the turn-on check's expected values, the power-service bench
   tests and arming, a test alert, and a `HARDWARE_CONFIG.md` for the unit.
