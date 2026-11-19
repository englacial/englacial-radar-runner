# UHD change proposals

`fp-gpio-no-output-at-init.patch` was adopted on 2026-11-19 after scope and HIL tests (results
in the implementation plan, "When to run the GPIO reset tool"). It is commit c3e2439c5 in
`~/uhd-gpio`, on top of `~/uhd` at c87abfbe8 (the TR-switch fix), installed in
`~/uhd-patched-gpio` with `~/orca/tests/hil/scripts/build_uhd.sh ~/uhd-gpio ~/uhd-patched-gpio`.
`hil/` holds the HIL fixture used for the test (the standard HIL fixture now names this build too).
The earlier build, without the patch, was moved to `~/uhd-patched.retired-20261119`.
A consequence for programs: the pin now starts as an input, so a program must set the level
(`OUT` or the ATR levels) before it makes the pin an output (`DDR`). ORCA and the reset tool do;
the bench helper `gpio_hold` did not and drove the pin low for 0.06-0.1 ms until it was changed.
The text below is the proposal as written before the tests.

## fp-gpio-no-output-at-init.patch

**Problem.** Every UHD initialisation of the B206mini enables the amplifier for
0.3-0.5 s (0.48 s at each radar start), with RF bursts in that time, and a program
that never writes the pin (`uhd_usrp_probe`, ORCA with `pwr_amp_pin: -1`) leaves it
enabled for good.

**Cause, from the source.**

* `gpio_atr_3000`'s constructor writes 0 to the idle, RX, TX, full-duplex, DDR and
  ATR-disable registers, in that order (`gpio_atr_3000.cpp:68-73`).
* `gpio_atr_3000::make()` then makes every pin an output (`:338`; that line is from
  2015, and the `is_writeonly()` branch above it, added in 2021, does the same for
  the banks that need it).
* `b200_impl.cpp:761` creates the front-panel bank this way, before the rest of
  the constructor (`:821` on: codec clock rate, subdevices) and before
  `multi_usrp::make` returns to the application. Where the 0.4 s goes inside that
  has not been measured.
* The property tree says the opposite: its `DDR` default is `INPUT` and is not
  written to the device.

So from line 761 until the application's own writes, all eight front-panel pins are
outputs at 0. The FPGA keeps its registers when the process ends.

**Change.** A new optional argument `start_as_inputs` of `gpio_atr_3000::make()`
(default false, so no other device or bank changes), used for the B2xx front-panel
bank only:

* the pins are not made outputs at initialisation;
* the constructor writes DDR = 0 before it clears the level registers, so a pin
  that an earlier process left as an output at high is released, not driven low.

The pins have FPGA pull-ups (`b205.ucf`, `fp_gpio<0..7> ... PULLUP`), the bench
notes say the pull-up works with DDR = 0, and the amplifier control board holds
SHUTDOWN high with GPIO_0 disconnected. So GPIO_0 would stay high through the
whole initialisation.

**Effect, if it works as read from the source.**

| | Now | With the change |
|---|---|---|
| ORCA start | amplifier enabled 0.48 s, RF bursts amplified | pin stays high; `setupGpio` then sets it up as now |
| ORCA with `pwr_amp_pin: -1`, `uhd_usrp_probe`, HIL C++ tests | amplifier left enabled | pin stays an input: amplifier stays shut down |
| `sdr_gpio_safe` | enables the amplifier before it can shut it down; any failure after the initialisation began means "assume enabled" | no enable window; an initialisation alone releases a pin stuck low |

ORCA needs no change: `Sdr::setupGpio()` writes the four ATR levels, then `CTRL`,
then `DDR` for its pin (`sdr/sdr.cpp:353-365`), and does not rely on the pin being
an output beforehand. With the pin an input until that `DDR` write, the sequence
has no low level in it. `sdr_gpio_safe` already writes `CTRL`, `OUT`, `DDR` in the
order that suits both cases.

**To re-test before relying on it.**

1. Rebuild with `~/orca/tests/hil/scripts/build_uhd.sh` (new `ORCA_UHD_BUILD.yaml`;
   the pinned UHD commit changes). UHD's own unit tests that compile
   `gpio_atr_3000.cpp` must still build.
2. With the 28 V off and the scope on GPIO_0: start-up of the radar, of
   `uhd_usrp_probe` and of `sdr_gpio_safe`, each from four retained states: fresh
   FPGA load, ATR idle high (after a radar run), manual high (after
   `sdr_gpio_safe`), and low (after an unpatched `uhd_usrp_probe`). Expect no low
   level longer than the scope can resolve in the first three, and a release to
   high in the fourth. Look at the edges at microsecond scale: the register writes
   are separate USB transfers.
3. HIL T0 and the fast tier against the baselines; then with the amplifier, the
   chirp timing at GPIO_0 (`pa_lead.yaml` numbers in `pa_bringup/STATUS.md`) and
   the start-up current: no enable at start.
4. Kill and Ctrl-C exits: the pin level afterwards, as on 2026-10-30.

**Risks.**

* It is a second local patch on UHD 4.11, to carry through every UHD update. It
  could be offered upstream; whether upstream wants the default changed is unknown.
* While the pin is an input the enable line is held only by pull-ups (FPGA and
  control board). That is the state the board is in today with the SDR unpowered
  or the FPGA not loaded, but it has not been looked at with the scope during the
  noisy part of the initialisation.
* Anything else on this UHD build that expects front-panel pins to be outputs
  without setting `DDR` would stop driving them. Nothing in ORCA or the bench does.
* The RF bursts of the initialisation still leave the SDR. With the amplifier shut
  down they are no longer amplified; its off-state isolation is not measured.
* Read from the source only. The 0.3-0.5 s figure and the retained registers are
  bench measurements; that this change removes the low level is not.
