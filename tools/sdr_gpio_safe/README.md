# sdr_gpio_safe and amp_safe

Put the power amplifier into shutdown by driving the SDR pin that enables it, and
verify that it worked. Ported from `~/radar-testbench/pa_bringup/gpio_hold.cpp`
(its `--high` mode). Called after every radar exit, at boot and in the power-loss
shutdown.

**Status: built and tested without hardware only. It has never been run against the
SDR. Follow "First run" below, with the user present, before anything calls it.**

| File | What |
|---|---|
| `sdr_gpio_safe.cpp`, `uhd_port.cpp`, `gpio_port.hpp` | The program (C++, patched UHD) |
| `amp_safe` | The wrapper that the services call (standard-library Python) |
| `fake_port.cpp` | Stand-in SDR for the tests in `tests/tools/`; no UHD in that build |

## What it does

1. Looks for the SDR (`uhd::device::find`). None: exit 2, nothing written. More than
   one: exit 3, none opened.
2. Initialises it (`multi_usrp::make`).
3. Writes, on bank `FP0` bit 0 (GPIO_0): `CTRL` = 0 (manual control, not ATR),
   `DDR` = 1 (output), `OUT` = 1 (high = amplifier shutdown), in that order, then
   reads `READBACK` 50 ms later. Repeats all three writes every 100 ms until the
   readback is high, at most 30 times.
4. Waits 1.5 s without writing and reads the pin again. High: exit 0. Low: back to
   step 3, at most 3 rounds in all, then exit 3.

It has no other action and no way to drive the pin to the enabled level. The pin
keeps the level after the program exits, until the next UHD initialisation.

The pin, level and mode come from the bench: GPIO_0 high = shutdown (amplifier
control board, `pa_bringup/STATUS.md`); ORCA uses the same pin with
`pwr_amp_pin: "2"` (it subtracts 2) and `pwr_amp_active_low: true`, under ATR control.

### What it cannot avoid

* **Every run enables the amplifier for about half a second.** UHD's initialisation
  makes all `FP0` pins outputs at level 0, so GPIO_0 is low (amplifier enabled, about
  2.5 A at 28 V) from inside step 2 until the first write of step 3: 0.3-0.5 s
  measured with `gpio_hold`. One or two RF bursts of 2-3 ms leave the amplifier in
  that time: about +11 to +13 dBm at its output with VVA/J8 at 5 V, +42 to +44 dBm at
  0 V (which browned out the supply on 2026-11-17).
* **The first contact after the SDR is powered up takes longer.** UHD first loads the
  SDR's firmware, the SDR re-enumerates on USB (UHD waits up to 3 s for it), then the
  FPGA image is loaded. The bench notes do not give this time; expect several
  seconds more than a later contact. If the SDR is not back within those 3 s the
  result is NOT_FOUND: call again.
* The SDR must not be in use. While a radar process has it open, this tool reports
  NOT_FOUND. Stop the radar first.
* The verification is the SDR's own readback of the pin, not the amplifier. Check
  the amplifier current as well where the 28 V is present.
* `--shutdown-level low` (not this unit) is verified less well: the readback also
  reads 0 for a pin that does not exist or was never written. On this unit `low`
  is the enabled level: never pass it.

## Options

`sdr_gpio_safe [options]`; defaults are the bench unit.

| Option | Default | |
|---|---|---|
| `--args STRING` | `""` | UHD device arguments |
| `--bank NAME` | `FP0` | GPIO bank |
| `--pin N` | `0` | bit of the bank, 0-31 (GPIO_0; ORCA's `pwr_amp_pin` minus 2) |
| `--shutdown-level high\|low` | `high` | pin level at which the amplifier is shut down |
| `--settle-ms N` | `1500` | wait before the second reading, 0-10000 |
| `--timeout SECONDS` | `20` | internal time limit, 1-300 |
| `-h`, `--help` | | help; does not touch the SDR |

`amp_safe [--timeout SECONDS] [--tool PATH] [-- options for sdr_gpio_safe]`

| Option | Default | |
|---|---|---|
| `--timeout SECONDS` | `20` | the call returns within this time, 3-300 |
| `--tool PATH` | `build/sdr_gpio_safe` next to the script | the binary |

## Output and exit codes

One line on standard output from each (`sdr_gpio_safe: OK: ...`,
`amp_safe: OK (exit 0, 2.71 s): sdr_gpio_safe: OK: ...`). UHD's own messages go to
standard error; `amp_safe` adds its warnings and errors to its line.

| Code | Name | Meaning |
|---|---|---|
| 0 | OK | pin read back in the shutdown state, twice (or `--help`) |
| 2 | NOT_FOUND | no SDR found (absent, unpowered, or in use); nothing written |
| 3 | UNVERIFIED | SDR found, but the state could not be verified; the amplifier may be enabled |
| 4 | TIMEOUT | no result in time; pin state unknown |
| 5 | FAILED | `amp_safe` only: the binary is missing, crashed, or ended without a valid result |
| 64 | USAGE | bad command line; SDR not touched |

`amp_safe` returns 0 only if the tool exited 0 and printed its OK line.

## Time limits

* `sdr_gpio_safe` ends itself at `--timeout`, also when a UHD call never returns
  (a second thread exits the process).
* `amp_safe --timeout T` returns within T seconds in every case. It gives the tool
  T - 1 s, sends SIGKILL at T - 0.5 s, and returns by T even if the tool survives
  that (a process stuck in the kernel on a hung USB device cannot be killed). Such a
  tool is left behind; while it lives, later calls return TIMEOUT without starting
  another.
* Only one `amp_safe` runs the tool at a time; a second call waits inside its own T.
* Expected run time, from the bench notes, not yet measured with this tool: about
  1 s to initialise and write, plus the 1.5 s settle time, so about 3 s when the
  SDR has been used since power-up. The first contact after power-up is longer (above).
* The power service's default hook time of 5 s is therefore enough only for later
  contacts, with little margin, and not for a first contact. Suggested until
  measured: `amp_safe --timeout 14` with `amp_safe_timeout_s = 15.0`.

## Build

```
cmake -S tools/sdr_gpio_safe -B tools/sdr_gpio_safe/build
cmake --build tools/sdr_gpio_safe/build
```

No conda environment needs to be active. It links `~/uhd-patched/lib/libuhd.so` only
(`-DUHD_PREFIX=` to change) and takes the Boost headers from the environment named in
`~/uhd-patched/ORCA_UHD_BUILD.yaml`. Building does not touch the SDR. Check:
`ldd tools/sdr_gpio_safe/build/sdr_gpio_safe | grep libuhd` must show `uhd-patched`.

Tests (no hardware): `.venv/bin/python -B -m pytest tests/tools`

## First run (with the user present)

Before:

* Hardware as in `HARDWARE_CONFIG.md`: amplifier output into the 40 dB 100 W
  attenuator, not an antenna; VVA/J8 at 5 V; 28 V on.
* No radar, HIL session or UHD tool running (`pgrep -a radar`).
* The amplifier current is being watched: the telemetry service's reading, or the
  bench guard (`~/radar-testbench/pa_bringup/pa_guard.py -d 600`, which also
  recovers by itself with `gpio_hold --high` after 5 s of enabled current), and the
  scope if available. Expected before: about 0.156 A (shutdown).
* Emergency stop: power off the 12 V supply (this also powers off the Pi).

Steps:

1. Build, run the tests, run `tools/sdr_gpio_safe/build/sdr_gpio_safe --help`.
2. Pin already safe: `tools/sdr_gpio_safe/amp_safe`. Expect the current to rise to
   about 2.5 A for 0.3-0.5 s (the telemetry may miss it; the scope shows about
   +5.5 A on the 12 V input) and return to 0.156 A, and
   `amp_safe: OK (exit 0, ... s): ... 1 write, SDR initialised in ... s, total ... s`.
   Note the times. If this is the first SDR contact since power-up, also note that.
3. Run it again and compare the times (later contact).
4. Recovery, only after steps 2-3 passed and with the user's go-ahead: leave the
   amplifier enabled (on the bench: `gpio_hold` holding the pin low, then `kill -9`;
   the current stays at about 2.5 A) and run `amp_safe` at once. Expect 0.156 A
   again about 1 s after the start, and exit 0. The bench scripts (`pa_run.py`,
   `pa_guard.py`) run `gpio_hold --high` themselves 2.5-5 s after they see the
   current; if that overlaps with `amp_safe`, one of the two fails to open the SDR.
5. As the power service will run it, as root with an empty environment:
   `sudo env -i tools/sdr_gpio_safe/amp_safe`. Same result expected.
6. After a full power cycle, run step 2 once more for the first-contact time. Set the
   callers' time limits from the times noted.

If the amplifier stays enabled (about 2.5 A for more than 2 s after the tool has
ended, or UNVERIFIED/TIMEOUT with the current high):

1. `~/radar-testbench/pa_bringup/gpio_hold --high` (the proven bench tool).
2. If the current is still high: power off the 12 V supply. Enabled with no RF is
   about 70 W of heat.
