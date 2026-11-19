# sdr_gpio_safe and amp_safe

Put the power amplifier into shutdown by driving the SDR pin that enables it
(B206mini front panel, `FP0` GPIO_0, high = shutdown), and verify it. Ported from
`~/radar-testbench/pa_bringup/gpio_hold.cpp` (its `--high` mode).

**Status: built and tested without hardware only. It has never been run against the
SDR. Follow "First run" below, with the user present, before anything calls it.**

**Running it is not free: it enables the amplifier before it shuts it down, and a
run that fails or is killed part-way can leave an amplifier enabled that was safe
before.** Callers run it only when the pin is not known to be safe. The calling
rule is being decided with the user; the facts that matter for it:

* After a clean ORCA exit the pin is already high (ORCA's ATR idle level, kept by
  the SDR).
* While the SDR has not been initialised since it was powered up, its pins are
  not driven (high-Z) and the amplifier control board holds SHUTDOWN high.
* Every run costs one enable window: about 2.5 A at 28 V for 0.3-0.5 s, with one
  or two RF bursts of 2-3 ms out of the amplifier (about +11 to +13 dBm at its
  output with VVA/J8 at 5 V; +42 to +44 dBm at 0 V, which browned out the supply
  on 2026-11-17).

| File | What |
|---|---|
| `sdr_gpio_safe.cpp`, `uhd_port.cpp`, `gpio_port.hpp` | The program (C++, patched UHD) |
| `amp_safe` | The wrapper that the services call (standard-library Python) |
| `fake_port.cpp` | Stand-in SDR for the tests in `tests/tools/`; no UHD in that build |

## What it does

1. Looks for the SDR (`uhd::device::find`). The SDR is not initialised in this
   step. None found: exit 2 or 6. More than one: exit 7.
2. Initialises it (`multi_usrp::make`). **From inside this step the pin is low and
   the amplifier enabled:** UHD makes every `FP0` pin an output at level 0
   (`gpio_atr_3000.cpp`), and the SDR keeps that when the process ends. Any end
   between here and step 4, for any reason, leaves the amplifier enabled.
3. Writes `CTRL` = 0 (manual control, not ATR), `OUT` = 1 (high), `DDR` = 1
   (output) for bit 0, in that order, and reads `READBACK` 50 ms later. Repeats
   all three every 100 ms until the readback is high, at most 30 times.
4. Waits 1.5 s without writing and reads again. High: exit 0. Low: back to step
   3, at most 3 rounds in all, then exit 3.
5. Exits at once, without closing the SDR. The pin keeps its level until the next
   UHD initialisation.

The bank, pin, level and settle time are fixed in the source and there is no
option for them: a wrong value would report OK with the amplifier enabled.
ORCA uses the same pin as `pwr_amp_pin: "2"` (it subtracts 2) with
`pwr_amp_active_low: true`.

What OK means: `READBACK` of an output is the level the FPGA is commanding
(`gpio_atr.v`), not the level at the pin. Exit 0 says that the FPGA commands
GPIO_0 high as a manual output, twice 1.5 s apart. Only the amplifier current
(about 0.156 A in shutdown, about 2.5 A enabled) confirms the amplifier itself,
and only while the 28 V is present. The readback is 0 right after the
initialisation because the pin really is low then.

## Results

One line on standard output from each program, for example
`amp_safe: OK (exit 0, 2.71 s): sdr_gpio_safe: OK: FP0 GPIO_0 is a manual output commanded high ...`.
UHD's own messages go to standard error; `amp_safe` adds its warnings and errors
to its line.

| Code | Name | SDR initialised by this run? | Meaning | Amplifier |
|---|---|---|---|---|
| 0 | OK | yes | pin commanded high and read back twice (`sdr_gpio_safe --help` also exits 0) | shut down, as far as the SDR can tell |
| 3 | ASSUME_ENABLED | yes, or possibly | initialisation started and the pin was not verified high: initialisation failed, pin never read high or went back, time limit, tool killed, crashed or ended without a valid result | **assume enabled by this run: retry and alert** |
| 2 | NOT_FOUND | no | no SDR for the device arguments and no Ettus device on USB (unplugged, unpowered) | unknown, unchanged by this run |
| 6 | BUSY | no | an Ettus USB device (vendor 2500) is connected but UHD found none it can use: open in another process (a running radar looks like this), not responding, or not matching `--args` | unknown, unchanged by this run |
| 7 | AMBIGUOUS | no | more than one SDR matches; none opened | unknown, unchanged by this run |
| 4 | TIMEOUT | no | time ran out before the initialisation started (looking for the SDR, or `amp_safe` waiting for another run) | unknown, unchanged by this run |
| 5 | FAILED | no | `amp_safe` only: the binary is missing or could not be started | unknown, unchanged by this run |
| 64 | USAGE | no | bad command line | unknown, unchanged by this run |

Only 0 means safe. None of the others does: "unchanged by this run" says nothing
about the state before it. BUSY or NOT_FOUND on the first contact after power-up
can also be an SDR that is slow to come back after UHD loaded its firmware (UHD
waits 3 s): call again.

## Options

`sdr_gpio_safe [--args STRING] [--timeout SECONDS]`

| Option | Default | |
|---|---|---|
| `--args STRING` | `""` | UHD device arguments |
| `--timeout SECONDS` | `20` | internal time limit, 1-300 |
| `-h`, `--help` | | help; does not touch the SDR |

`amp_safe --timeout SECONDS [--args STRING]`

| Option | | |
|---|---|---|
| `--timeout SECONDS` | required, 3-300 | the call returns within this time |
| `--args STRING` | optional | passed to `sdr_gpio_safe` |

`amp_safe` runs `build/sdr_gpio_safe` next to itself and passes nothing else on.

## Time limits

* `sdr_gpio_safe` ends itself at `--timeout`, also when a UHD call never returns
  (a second thread exits the process): TIMEOUT before the initialisation started,
  ASSUME_ENABLED after.
* `amp_safe --timeout T` returns within T seconds in every case. It gives the tool
  T - 1 s, sends SIGKILL at T - 0.5 s, and returns by T even if the tool survives
  that (a process stuck in the kernel on a hung USB device cannot be killed). Such a
  tool is left behind; while it lives, later calls return TIMEOUT without starting
  another.
* **A caller's own time limit for `amp_safe` must be longer than `--timeout`.** The
  power service kills the hook's process group at `amp_safe_timeout_s`; if that
  comes first it kills the tool inside the initialisation and leaves the pin low.
* Only one `amp_safe` runs the tool at a time; a second call waits inside its own
  T. The lock is on the `amp_safe` script itself.
* Expected run time, from the bench notes, not yet measured with this tool: about
  1 s to initialise and write plus the 1.5 s settle time, so about 3 s when the
  SDR has been used since power-up. The first contact after power-up also loads
  the firmware (the SDR then re-enumerates on USB) and the FPGA image; the bench
  notes give no time for it.
* Starting point until the first-contact time is measured: `amp_safe --timeout 14`
  with `amp_safe_timeout_s = 15.0` in the power service. The power service's
  default of 5 s is too short.

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

Emergency stop throughout: power off the 12 V supply (this also powers off the Pi).
No radar, HIL session, bench guard or UHD tool may be running unless a step says so.
Build first, run the tests, and run `tools/sdr_gpio_safe/build/sdr_gpio_safe --help`.

### A. Amplifier 28 V off, scope on GPIO_0

Nothing can overheat in this part. Trigger the scope on GPIO_0 falling.

1. First contact. Power-cycle the SDR (whole system off and on) so that it has no
   firmware loaded (`cat /sys/bus/usb/devices/*/product` shows `WestBridge`).
   Run `tools/sdr_gpio_safe/amp_safe --timeout 60`. Record the total time, the
   "SDR initialised in" time, and on the scope: when GPIO_0 goes low, for how long,
   and that it then stays high. Expected: one low period of 0.3-0.5 s, exit 0.
   **No timeout is fixed anywhere before this time is known.**
2. Later contact: run it again, same records. Expected about 3 s in all.
3. Readback against the scope: in both runs the OK line must come only after
   GPIO_0 is high on the scope, and GPIO_0 must still be high after the tool has
   exited.
4. Kill during initialisation: start `tools/sdr_gpio_safe/build/sdr_gpio_safe` and
   send it SIGKILL shortly afterwards (`... & sleep 0.2; kill -9 $!`; lengthen the
   delay until the scope shows the kill landing inside the low period). Expected:
   GPIO_0 stays low. This is the ASSUME_ENABLED case. Recover with
   `amp_safe --timeout 60`: GPIO_0 high, exit 0.
5. Busy: with ORCA running (a low-power loopback config with the amplifier pin
   set, as in the bench fixtures), run `amp_safe --timeout 14`. Expected: BUSY
   (exit 6) within the time, and no disturbance of the running radar. Stop the
   radar.
6. The real hook path: configure the power service's `amp_safe` hook with the
   intended command and `amp_safe_timeout_s`, and let the service run it as root
   (the bench procedure in `power/README.md`). Expected: exit 0 in the journal,
   GPIO_0 as in step 2, hook time inside the limit.

### B. Amplifier powered, VVA/J8 at 5 V, output into the attenuators

Only after part A passed. Hardware as in `HARDWARE_CONFIG.md`: amplifier output
into the 40 dB 100 W attenuator, not an antenna. Watch the amplifier current
(telemetry service, or the bench guard) and the scope. The bench guard
(`pa_guard.py`) and `pa_run.py` run `gpio_hold --high` themselves 2.5-5 s after
they see enabled current; if that overlaps with `amp_safe`, one of the two finds
the SDR busy.

1. Before: about 0.156 A (shutdown).
2. `tools/sdr_gpio_safe/amp_safe --timeout 14`. Expect about 2.5 A for 0.3-0.5 s
   (the telemetry may miss it; the scope shows about +5.5 A on the 12 V input),
   then 0.156 A, and exit 0.
3. Recovery, with the user's go-ahead: leave the amplifier enabled (on the bench:
   `gpio_hold` holding the pin low, then `kill -9`; the current stays at about
   2.5 A) and run `amp_safe --timeout 14` at once. Expect 0.156 A again about 1 s
   after the start, and exit 0.

If the amplifier stays enabled (about 2.5 A for more than 2 s after the tool has
ended, or ASSUME_ENABLED with the current high):

1. `~/radar-testbench/pa_bringup/gpio_hold --high` (the proven bench tool).
2. If the current is still high: power off the 12 V supply. Enabled with no RF is
   about 70 W of heat.
