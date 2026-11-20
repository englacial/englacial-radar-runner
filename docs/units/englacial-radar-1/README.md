# Unit `englacial-radar-1`: installed settings, and how to recreate its card

The files here are copies of this unit's own settings as installed on 2026-11-20 (they live in
`/etc/radar-runner/`, outside git, and are never changed by an update of the repository):

| File | Installed as | What it holds |
|---|---|---|
| `radar.yaml` | `/etc/radar-runner/radar.yaml` | The radar config: 300 MHz, 30 MHz chirp, `tx_gain` 40 (about 9 mW at the amplifier output). `tx_gain` 70 gives about 9 W and is the cap |
| `local.yaml` | `/etc/radar-runner/local.yaml` | The `tx_gain` cap of 70 for this amplifier and VVA divider; the turn-on check on, not blocking, with loose bounds |
| `power.toml` | `/etc/radar-runner/power.toml` | The power service, armed, with the two radar hooks |

`/etc/radar-runner/secrets.yaml` (the ntfy topic and token) is not here and must be copied by hand.

Settings stored in this unit's hardware, not on the card: the GPS receiver's message set
(provisioned with `setup/gps_receiver.py --apply`, including the raw messages at 1 Hz), the
bootloader configuration, and the data drive's label `radar-data`.

## The card image, and bringing a card made from it up to date

The card was imaged once, on 2026-11-20 between 06:16 and 06:47 UTC, with this repository at
commit 4a661fc and ORCA at 0b0b051. Everything done after that is in this repository and in
ORCA's `hil-fixtures` branch, apart from the per-unit files above. A card written from that image
(a spare for this Pi) is brought to the state of this repository with:

1. Update the two checkouts: `git -C ~/englacial-radar-runner pull` and `git -C ~/orca pull`
   (ORCA at the commit in `setup/pins.toml`). No Python package, unit file or installed file
   changed after the image, so `.venv` and `wheelhouse/` need nothing.
2. `sudo setup/setup.sh --nvme-by-label` in tmux: the `/mnt/nvme` line in `/etc/fstab` then names
   the drive by its label instead of its UUID (`setup/SYSTEM_CHANGES.md`, `--nvme-by-label`).
3. Install the three files above:
   `sudo cp docs/units/englacial-radar-1/{radar.yaml,local.yaml,power.toml} /etc/radar-runner/`.
4. Reboot, then `setup/check.py` and `setup/check.py --receiver`: no FAIL line.
5. Optional: `sudo apt install rtklib`, for converting a raw GPS log on the unit.

A card for another unit is made from the same image with `setup/new_unit_image.py`
(`setup/NEW_UNIT.md`); steps 1, 2, 4 and 5 then follow on that unit, and its own settings take
the place of step 3.
