This file documents the current state of the hardware connected to this system.

AGENTS: Ask permission before updating this file. If you come across evidence that the documented configuration is incorrect or that necessary details are omitted, you should ask the user to update this file.

Lines beginning with "#" should be ignored. They are "commented-out" old configurations being saved to re-apply.

SDR:
* rPi USB 3 -> CoolGear CG-30ACTSSL-05M with copper braid -> B206mini (SDR). Pi enclosure wrapped in copper foil (2026-11-17). On USB 3 the GPS keeps a fix with ~10 dB more L1 noise; see `~/radar-testbench/gps_jam/README.md`
* SDR TX -> short cable -> Empower 1094 PA (28 V on; VVA/J8 at 3.452 V from a fixed divider since 2026-11-20, see "Empower 1094 amplifier and control board") -> short cable -> 40 dB attenuator (100 W) -> Z99SC-62-S+ splitter A
  * Splitter A output 1 -> Splitter B input (Z99SC-62-S+)
  * Splitter output 2 -> 30 dB attenuator (1 W) -> 50 m loopback cable -> short cable -> VLFX-450+ low-pass filter -> ZFLM-252-1WL-S+ limiter -> ZX60-P103LN+ LNA (powered) -> ZFLM-252-1WL-S+ limiter -> 10 dB attenuator -> SDR RX
* Splitter B:
  * Splitter B output 1 -> Logic MSO Blue via 50 ohm pass-through terminator
  * Splitter B output 2 -> ZX47-40-S+ power detector (powered) -> Logic MSO Red (direct, no 50 ohm terminator)
* The scope (run by a peer Claude session, `configure-scope-gpio-capture`) also measures the current and voltage into the system
* All devices, including the Pi, run from one 12 V supply (the PA's 28 V comes from it through a regulator). Emergency shutdown = power off the whole system
* Datasheets for the front-end parts and the 1094 are in `hardware_reference/datasheets/`
* HIL fixture for this chain: `~/radar-testbench/fixtures/loopback_50m_pa_rx_frontend.yaml` (not yet updated: it still describes the amplifier gain at VVA 5 V, about 24 dB, and the 30 dB attenuator ahead of splitter A)

# * SDR TX -> shorter jumper -> 30 dB attenuator -> 50 m loopback cable -> VLFX-450+ low-pass filter -> ZFLM-252-1WL-S+ limiter -> ZX60-P103LN+ LNA -> ZFLM-252-1WL-S+ limiter -> 10 dB attenuator -> short jumper -> SDR RX
# * The two jumpers are about 1 ft each; they were added after the 2026-10 HIL baselines
# * HIL fixture for this chain: `fixtures/loopback_50m_rx_frontend.yaml` (extends `fixtures/loopback_50m_jumpers.yaml`)

# * SDR TX -> 30 dB attenuator -> 50 m loopback cable -> SDR RX

# * SDR TX -> Splitter A
# * Splitter A output 2 -> Empower 1094 amplifier RF input
# * Splitter A output 1 -> 30 dB attenuator -> Logic MSO oscilloscope (red channel, 50 ohm pass-thru terminator)
# * 1094 RF output -> 70 dB attenuator (100W rated) -> Splitter B
# * Splitter B output 1 -> Logic MSO oscope (blue channel, 50 ohm pass-thru terminator)
# * Splitter B output 2 -> SDR RX
# Both splitters are MiniCircuits Z99SC62-S+

NVMe drives:
* Two Patriot M.2 P300 1 TB drives on a Pimoroni NVMe Base Duo board. The kernel names (`nvme0n1`, `nvme1n1`) swap between boots, so identify the drives by serial number or filesystem UUID, never by device name
* Data drive: serial `P300ZDCB25080614869`, one ext4 partition (label `orca-hil`, UUID `4803c5eb-0612-4afa-a67b-1ad14c085f79`) mounted at `/mnt/nvme` (fstab, by UUID, `nofail`). Used by the orca HIL harness (`/mnt/nvme/orca-hil`)
* Second drive (added 2026-11-18): serial `P300ZCCB250902170`, no partition or filesystem, not mounted, not in use yet

UPS:
* Geekworm UPS Scap 5V5A (supercapacitor UPS HAT for Pi 5, "100F"; measured effective capacitance ~60 F)
* 12 V power supply -> UPS DC barrel jack input; UPS USB-C output -> rPi USB-C power input
* INA219 monitor on I2C bus 1, address 0x40 (supercap voltage and current; shunt assumed 0.010 ohm from supplier code, not yet verified)
* External-power-present signal on BCM GPIO26 (high = present). Weakly driven: read it with the pull resistor disabled
* Wake-up contact acts as the Pi 5 power button: pressed ~0.5 s after input power returns
* Setup, test results and system changes are in `~/ups-bringup/` (`UPS_BRINGUP.md`, `SYSTEM_CHANGES.md`)

50m loopback Cable:
* In the SDR loopback path (see SDR above)
* The 50 m loopback cable is made of LMR-100 (velocity factor approximately 0.66)
* S12 and TDR results are in `hardware_reference/50m_loopback/`

OpenLog Artemis:
* An OpenLog Artemis is connected on a USB port. It's I2C bus (separate from the Pi) has the following sensors
* [0x4B] ADS1015 ADC on the Empower control board (online 2026-11-16)
  * AIN0 - 1094 Temperature
  * AIN1 - 1094 Current
  * AIN2 - 1094 Voltage
* TMP117 temperature sensor on the 1094 amplifier case (the only TMP117; the ambient one was removed 2026-11-16 in favour of the OLA's built-in temperature sensor)
* [0x42] u-blox GPS receiver, on the Qwiic bus and also connected to the Pi over USB (`/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00`, currently `/dev/ttyACM0`; confirmed 2026-11-17). Antenna is indoors at present. On 2026-11-16 the OLA boot-looped with the GPS USB connected, possibly from the GPS backfeeding the Qwiic bus. The power wire on the GPS's Qwiic connection has since been removed (data lines only), so the OLA does not power the GPS and the GPS cannot power the OLA.
* Built-in temperature (ambient reference) and IMU from the OLA
* No microSD card in the OLA (removed 2026-11-17). After a Pi reboot the OLA streams only after the first
  serial open (which resets it); with the card in, that open twice made it drop off USB instead

Empower 1094 amplifier and control board:
* The control board is on the OLA's Qwiic bus
* Control board replaced 2026-11-20 (the first replacement read a false, slowly decaying amplifier current after power-up; this one reads 0.144-0.156 A at idle from the first sample)
* VVA (J8) is set by a fixed divider on the control board: 1k from VVA to 5 V, 2.2k + 470 ohm from VVA to ground; measured 3.452 V (2026-11-20). The VVA node is loaded by about 14.7 kohm, so an unloaded divider calculation does not apply. Without a divider the board's 100k pull-down gives 0 V = full gain
* Amplifier gain at this setting is about 37 dB at 300 MHz (24.6 dB at VVA 5 V, about 55 dB at 0 V), and about 45 dB per volt of VVA around this point
* Output at this setting, peak chirp power at the amplifier output (power detector, +/-1 dB): tx_gain 60 = +29.4 dBm, 66 = +35.6 dBm, 70 = +39.6 dBm (about 9-10 W). tx_gain 70 is the cap (`manager.limits.max_tx_gain` in `/etc/radar-runner/local.yaml`); 72 would give about 14 W
* Power reference: the ZX47-40-S+ detector is about 46.6 dB below the amplifier output (40 dB attenuator and two splitters, nominal); datasheet in `hardware_reference/datasheets/`
* 12 V input at tx_gain 70 (scope, 110 s continuous record, 2026-11-20; raw clamp readings, the clamp's zero offset of about 0.18 A not subtracted): idle 1.76 A / 24.1 W; transmitting 2.18 A / 29.7 W, essentially constant (no per-pulse current reaches the input); peaks 2.51 A / 34.2 W instantaneous, 2.41 A / 32.8 W over 2 ms, 2.21 A / 30.1 W over 1 s; bus 13.64 V transmitting, minimum 13.42 V
* 12 V input at power-up (scope, 2026-11-20): inrush peak 73 A (+/-11 A), above 50 A for 100 us and above 10 A for 230 us, 10.9 mC (input capacitance of roughly 790 uF ahead of the power board's switch, charged straight from the supply); then a soft-start of about 5 ms at 0.3-1.3 A and a boot peak of 3.2 A (10 ms mean); about 3.0 J in the first 100 ms. No amplifier enable at power-up: the control board's pull-up holds the shutdown input while the SDR's pin is undriven
* For about the first 10 minutes after a power-up from a long off period the UPS recharges its supercapacitor: first in bursts (50-160 Hz fluctuation on the 12 V input, peaks up to 3.06 A / 41.5 W while transmitting), then steadily at about +0.31 A (4 W)
* When the 12 V supply is cut, the amplifier (28 V from the 12 V) loses power at once, while the Pi (UPS) and the SDR (USB from the Pi) run on for about 12 s; the manager stops the radar 0.7 s after the cut
* See `hardware_reference/empower_1094/`

Pi GPIO connections:
* Schematics for the GPIO breakout board are in `hardware_reference/pi_gpio_shim/`
* GPIO 17 controls the green led (RUNNING indicator)
* GPIO 18 controls the yellow led (READY indicator)
* GPIO 4 reads the state of the NO momentary push button on the case


