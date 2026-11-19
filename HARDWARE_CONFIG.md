This file documents the current state of the hardware connected to this system.

AGENTS: Ask permission before updating this file. If you come across evidence that the documented configuration is incorrect or that necessary details are omitted, you should ask the user to update this file.

Lines beginning with "#" should be ignored. They are "commented-out" old configurations being saved to re-apply.

SDR:
* rPi USB 3 -> CoolGear CG-30ACTSSL-05M with copper braid -> B206mini (SDR). Pi enclosure wrapped in copper foil (2026-11-17). On USB 3 the GPS keeps a fix with ~10 dB more L1 noise; see `~/radar-testbench/gps_jam/README.md`
* SDR TX -> short cable -> Empower 1094 PA (28 V on, VVA/J8 at 5 V, confirmed 2026-11-17) -> short cable -> 40 dB attenuator (100 W) -> 30 dB attenuator (1 W) -> Z99SC-62-S+ splitter A
  * Splitter A output 1 -> Splitter B input (Z99SC-62-S+)
  * Splitter output 2 -> 50 m loopback cable -> short cable -> VLFX-450+ low-pass filter -> ZFLM-252-1WL-S+ limiter -> ZX60-P103LN+ LNA (powered) -> ZFLM-252-1WL-S+ limiter -> 10 dB attenuator -> SDR RX
* Splitter B:
  * Splitter B output 1 -> Logic MSO Blue via 50 ohm pass-through terminator
  * Splitter B output 2 -> ZX47-40-S+ power detector (powered) -> Logic MSO Red (direct, no 50 ohm terminator)
* The scope (run by a peer Claude session, `configure-scope-gpio-capture`) also measures the current and voltage into the system
* All devices, including the Pi, run from one 12 V supply (the PA's 28 V comes from it through a regulator). Emergency shutdown = power off the whole system
* Datasheets for the front-end parts and the 1094 are in `hardware_reference/datasheets/`
* HIL fixture for this chain: `~/radar-testbench/fixtures/loopback_50m_pa_rx_frontend.yaml`

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
* See `hardware_reference/empower_1094/`

Pi GPIO connections:
* Schematics for the GPIO breakout board are in `hardware_reference/pi_gpio_shim/`
* GPIO 17 controls the green led (RUNNING indicator)
* GPIO 18 controls the yellow led (READY indicator)
* GPIO 4 reads the state of the NO momentary push button on the case


