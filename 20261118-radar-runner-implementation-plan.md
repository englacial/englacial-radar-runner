This system is going to be deployed as a truck-towed radio echo sounder in Antarctica. The system specifications are listed in `hardware_reference/SORA Instrument Spec Sheet for Arctic Trucks.md`. At any given time, `HARDWARE_CONFIG.md` should specify the current actual hardware setup, which may not always match the system specs during testing.

Once deployed, the team will not have physical access to the hardware, so robustness to many possible faults is important. Equally, simplicity is extremely important. When possible, the simple solution should be used. When an existing well-tested tool for the job exists, it should be used.

This repository will serve as the radar "manager". Actual radar data acquisition code should remain in `~/orca/`. This repository is responsible for other services on this Pi and for calling the ORCA code at the right times. The primary jobs of this repository are:

1. Manage the Pi's power state, including gracefully shutting down when power is removed from the system
2. Continuously log auxiliary sensor telemetry (internal temperatures, GPS, etc)
3. Manage turning the radar on and off according to configured triggers
4. Watch for safety conditions that might require turning off the radar
5. Provide a web-based user interface for monitoring the current status
6. Manage recorded data: storage budget, offload over Starlink, and alerts to the team

This is something like a (much more full-featured) replacement for `~/orca/manager/` (used on a different type of deployment).

All of these functions must start when the Pi is booted up. Nominally, the Pi should be online via Starlink for most of the deployment, but everything in the system should be robust to not having internet.

Conventions in this document: plain text is decided. `TODO:` is an open question. `PROPOSED:` is a suggestion that has not been approved yet.

# Context and constraints (2026-11-18)

* The system ships next week for a multi-month deployment. It will be powered 8-12 hours per day. Gaps in data collection are acceptable.
* On-site technicians can be reached, but not always. They can power cycle the system, push the button and read the LEDs.
* Cold starts from -30 C are likely.
* Starlink performance is unknown; finding out is part of the test.
* No hardware over-temperature cutoff is planned for the amplifier.
* The Pi's clock has no battery. The OLA's clock does.
* The VVA setting for the deployed 10 W output is not decided. The radar config limits, the safety thresholds and the turn-on check levels all depend on it.
* The ORCA commit and the patched UHD build are to be pinned. Which ones is TBD.
* Changes to ORCA are proposed to the user one at a time, made only after approval, and never committed by an agent.

# Design principles

1. **A hard power cut at any instant must leave usable data and a bootable system.** The supercapacitor shutdown improves on this; it is not what data safety depends on.
2. **Nothing in this repository may be able to prevent boot or block SSH and Tailscale.** Only the power service may power the Pi off, and it has limits on how often it can do so (see below).
3. **Each device has one owner.** One service opens each serial port, I2C device or GPIO and publishes its readings; everything else reads the published values.
4. **The safety watcher does not trust the radar manager.** It decides whether the amplifier should be off from whether a radar process exists, not from what the manager reports.
5. **Bias:** protect hardware first, then collect data. A missing safety reading stops the radar unless the user has set an override. A missing non-safety reading (GPS, UPS monitor) produces a warning only.

# Services

| Service | Owns | May do |
|---|---|---|
| Power | UPS monitor (INA219), GPIO 26 | Power off or reboot the Pi. Ask the manager to stop the radar |
| gpsd (existing tool) | GPS receiver on USB | Nothing else |
| Telemetry | OLA serial port, Pi internal sensors | Write sensor logs, publish latest values |
| Safety watcher | Nothing; reads telemetry | Stop the radar, reset the SDR GPIO to the safe state |
| Radar manager | Button, LEDs, the ORCA process | Start and stop the radar |
| Alerts, offload, web | Nothing | Read only. These may crash without affecting anything else |

The manager refuses to run the radar unless the safety watcher's status is fresh, as the HIL harness does with `pa_guard.py` today.

# Pi power state management

If main power is removed, any running radar service should be safely stopped without loss of data, safe condition of the amplifier checked, and the Pi should be shut down. The Pi is equipped with a supercapacitor backup that allows for brief runtime to perform these tasks.

This is a critical system to prevent data corruption. It is also critical that it not fail in a way that would shut down the Pi constantly or boot loop it. Care should be taken to handle possible intermittent connections with the supercapacitor board.

This power state management should run as a separate service that can coordinate with the radar manager code as needed.

Power loss is the expected way that the radar will be shut down, not an off-nominal case.

Measured on the bench (`~/ups-bringup/UPS_BRINGUP.md`): the INA219 current reversal shows a loss within 0.25 s, GPIO 26 lags by about 1.5 s, the capacitor carries the running radar for about 58 s at room temperature, the radar exits 0.26 s after SIGINT, and an idle OS shutdown takes about 3.5 s.

Sequence on power loss:

1. On the first sign of loss, stop the radar at once. This is cheap and reversible.
2. If power is not back within 5-10 s, commit to shutdown.
3. If the amplifier-enable pin is not known to be safe (see "When to run the GPIO reset tool"), drive the SDR GPIO to the amplifier-shutdown state and verify it.
4. If the 28 V rail is present, also verify that the amplifier current is consistent with shutdown, and retry step 3 if it is not. This guards against the amplifier being left enabled when power returns while the Pi is shutting down.
5. Flush data. Power off if main power is still absent; reboot instead if it has returned.

* During a power loss the 28 V rail is gone, so the amplifier current reads near zero whether or not the amplifier is enabled, and the current check in step 4 cannot tell the difference. Step 3 therefore goes by the recorded pin state, not by the current.
* Stopping the radar needs only one loss signal. Shutting down needs both signals to agree, or one signal with the other unreadable or faulty. The ride-through time counts only while that agreement holds without interruption. Two signals that contradict each other do not cause a shutdown; they cause a warning. The one exception is the low-voltage rule below.
* A signal that reports a loss for longer than the capacitor could carry the Pi (180 s) is marked faulty and ignored until it reads normally again.
* A low capacitor voltage (below 4.5 V) while the signals agree commits to shutdown immediately, regardless of the ride-through time. A capacitor that is discharging and below 4.5 V for 2 s commits by itself, whatever GPIO 26 says, because that cannot happen with main power present.
* If power returns before the final step, the Pi reboots when every working signal confirms it.
* Boot-loop guard: the service counts shutdowns it started soon after boot. After three in a row it stops shutting down, keeps monitoring and raises an alert. A file on disk can also disable its shutdown authority.
* Remove the 30 s shutdown inhibitor delay that `unattended-upgrades` installs (`~/ups-bringup/SYSTEM_CHANGES.md`).
* Residual risk: power returning during the last few seconds of a power-off can leave the Pi off until the next power cycle. A technician power cycle recovers it. Seen on the bench on 2026-11-18: power returned 2.4 s after the power-off command and the Pi stayed off.

TODO (later improvement, not planned for now): close this gap with a wake alarm. Before every power-off the service sets the Pi's clock to wake it about 10 minutes later, longer than the capacitor can carry a halted Pi. If power is really gone the capacitor empties first and nothing happens; if power is present the Pi wakes itself. To verify first: that the alarm wakes a halted Pi under this kernel, and how long the capacitor carries a halted Pi.

TODO: Check the capacitor hold-up time when cold. Capacitance falls and resistance rises at -30 C.

# Sensor logging

The following sensors should be logged continuously at a configurable rate. Note that some of these sensors are also needed by other parts of the system, so this should be considered.

* All I2C devices on the OpenLog Artemis bus (see `HARDWARE_CONFIG.md`)
* Internal temperatures sensors, system load, and throttling state from this Pi 5
* GPS data from the ublox F9P
* UPS capacitor voltage and current, and the state of each service

Logging that runs while the radar is running must be computationally lightweight to avoid adding much load to the processor. Extra processor load during a capture has raised late-command error rates from about 1 % to 5-25 % on the bench.

The OLA stays as the path to the amplifier sensors. If it proves unreliable in testing, the sensors can move to the Pi's own I2C bus. The telemetry service keeps one connection open (each open resets the OLA) and re-applies the ADS1015 settings at each boot, as `~/radar-testbench/pa_bringup/ola_setup.py` does.

* Storage structure: append-only text files, one line per sample, a new file per boot and per day. A file cut off mid-write stays readable up to the last full line. Sensor logs go to the NVMe drive, and to the SD card if the NVMe is missing.
* GPS: gpsd owns the receiver. The motion trigger, the system clock and the raw logger are gpsd clients. This repository logs the raw data for PPK while the radar runs; ORCA does not open the GPS.
* The receiver setting that prevents the transmit-buffer overflow (UART1 NMEA output off) is in the receiver's flash and survives power cycles (found 2026-11-19; the earlier note that it was in RAM only no longer holds). The telemetry service does not send anything to the receiver; `setup/check.py` reads the setting and the transmit-buffer error flags when run by hand.
* The GPS stays connected to the OLA's Qwiic bus (user decision, 2026-11-19), with the power wire removed so that neither device can power the other. The OLA sets the receiver to 10 Hz, and the evidence suggests it saves the receiver's configuration to the receiver's flash each time the OLA starts (not proven). Each open of the OLA's serial port resets the OLA, so the telemetry service opens it once per boot and backs off for longer and longer when it has to retry.
* At 10 Hz gpsd's messages already use most of the receiver's USB output capacity (the transmit buffer has peaked at 88 %). Raw logging for PPK in stage 6 adds to that, so the rate or the message set needs a decision then.
* Time: the Pi's clock has no battery, so the system clock is set from GPS when there is no internet. File and directory names include a boot counter and a sequence number so that they stay unique and ordered if the clock is wrong at boot. The OLA's battery-backed clock is logged with every OLA reading as a cross-check; it is not used to set the system clock.

PPS will be wired to the SDR, not the Pi. The system clock therefore comes from GPS messages over USB (tens of milliseconds), and precise radar timing comes from the SDR (see "Interface to ORCA").

# Radar management

## Interface to ORCA

* The manager generates the chirp, builds and runs the `radar` binary itself. `run.py` stays as it is for other users.
* The build runs at each start. A failed build is a failed start.
* The tool that resets the SDR GPIO to the amplifier-shutdown state lives in this repository (`tools/sdr_gpio_safe/`, ported from `gpio_hold` on the bench).

### When to run the GPIO reset tool

Every UHD initialisation on this SDR drives the front-panel pins low as outputs, which enables the amplifier for about half a second with a short RF burst, until the program writes the pin. This is true of ORCA, of every UHD tool and of the reset tool itself. A reset run that fails or is killed after initialisation leaves a previously safe amplifier enabled. So the tool runs only when the pin is not known to be safe.

The pin is known to be safe when the last thing that initialised the SDR was one of:

* ORCA with the amplifier pin configured, exiting cleanly
* a successful run of the reset tool
* nothing at all since the SDR was powered (its pins float and the control board holds shutdown)

The tool runs in every other case: ORCA killed or crashed, another UHD program used, a reset run that did not succeed, unknown history, or amplifier current above 0.5 A with no radar process. Where the 28 V rail is present, the current reading decides. The manager keeps a persistent "not known to be safe" flag for the power-loss sequence to consult.

"OK" from the tool means the SDR is commanding the pin high; only the amplifier current confirms the amplifier itself.

Measured on 2026-11-19 with the scope on GPIO_0, the 28 V on and the bench guard armed (first run of the tool, first contact after power-up): the pin read high while floating; there were no edges during the firmware load (5.9 s) or the FPGA load (8.2 s); the pin then went low for 480 ms, with the 12 V feed up by about 5.5 A (about 94 W), and went high at the tool's write and stayed high. The tool took 16.7 s in total and reported OK. Time limits for callers must allow for a first contact of that length. A second run with the firmware and FPGA already loaded took 3.5 s in total (1.5 s to initialise, plus the 1.5 s recheck). In that run a triggered capture showed one falling edge with no glitch in the 25 ms before it, the 12 V feed up by about 5.2 A within 1 ms, and no RF burst within the first 95 ms of the low period above the capture's sensitivity (about +17 dBm at the amplifier output). The bursts in the bring-up notes were seen later in the low period and at lower levels with the VVA at 5 V, so this capture neither confirms nor rules them out.

TODO (deferred, user is interested): change the patched UHD so that it does not force the B2xx front-panel pins to outputs at initialisation. This should remove the start-up enable and its RF burst for ORCA, every UHD tool and the reset tool. A draft patch with its risks and test list is in `docs/uhd-proposals/`. It needs a UHD rebuild, a scope on GPIO_0 at start-up and a HIL re-run.

ORCA changes. Each needs the user's approval before work starts.

* ORCA writes data straight into a per-collection directory on the NVMe drive, in chunks, so that a hard cut loses at most the chunk being written. The config and logs are saved at the start of the collection, not at the end. Today the data is moved or copied when the radar stops.
* Stopping is SIGINT, with SIGKILL only as a last resort after the allowed exit time. The data must be valid after either.
* With PPS on the SDR, ORCA sets the SDR's clock at a PPS edge so that each record carries GPS time. If no PPS is detected, the radar still runs on host time and the collection is flagged.
* The radar's output log grew to 88 MB in 2 h on the bench. Limit it or write it to the NVMe drive.

TODO: Radar exit time. After the 2 h soak the receive thread had not exited 5 s after SIGINT (the transmit thread had), and the radar was killed. In short runs it exits in about 0.3 s. Find out what happened, estimate how long the radar actually needs to exit after short and long runs, and set the allowed time from that. The allowed time also has to fit inside the capacitor hold-up time on power loss.

## Radar config limits

Before every start the manager checks the requested config against fixed limits and refuses to run if any fail: amplifier pin set and active low, TX gain at or below a cap, enable duty at or below a cap, receive window at least as long as the transmit pulse, frequency inside the amplifier band, output on the NVMe drive. `pwr_amp_pin: -1` leaves the amplifier enable unmanaged and must never run with the amplifier powered.

TODO: Set the limits once the VVA setting is decided.

## Safe turn-on

Since this radar will be installed and operated by technicians who haven't worked on radar before, the radar manager service should start data collections with a low output power relatively safe configuration and verify that results appear as expected before switching to the requested power.

The check runs at every radar start. Its main targets are a disconnected antenna and a damaged cable. If it fails, the radar stays off.

The goal of the check is to find the direct signal between the two antennas. It arrives several metres of two-way travel later than the leakage inside the SDR, so the two peaks should be separable. Lab conditions differ too much from the field for a useful baseline, so the first start in the field is verified manually by the user, and the expected peak is set from that.

* The low-power config is the requested config with the TX gain capped at a set maximum and a short fixed pulse count. The waveform is unchanged, so the direct signal arrives at the same delay.
* Pass criteria: amplifier voltage, idle current and temperatures normal before the run; amplifier current rises as expected during it; a direct-signal peak at the expected delay after the leakage peak and above a minimum level; no clipping.
* The HIL harness already locates a leakage peak and a loopback peak (test T1). The same method applies, and the bench loopback cable stands in for the antennas during development.
* Until the user has verified the first field start and set the expected delay and level, the check records its result but does not block the radar.
* On a pass, the radar restarts at the requested power. This costs a second SDR start, about 10 s.
* On a failure, the manager shows the fault on the LED, sends an alert, and tries again at the next button press or the next trigger.
* The amplifier is rated for any load mismatch for 1 minute at 60 W and 3:1 continuously, so a disconnected antenna at 10 W is not an immediate hazard to it. The check mainly protects data quality and gives the technicians feedback.

TODO: Antenna spacing and cable lengths. At 56 MS/s one sample is about 5 m of path in air, and a 30-50 MHz chirp resolves about 6-10 m, so the direct peak may sit only one or two samples after the leakage peak. If they do not separate cleanly, the check falls back to the level of the combined peak.

## Trigger-based on/off

There should be multiple configurable (i.e. can be enabled or disabled) ways of turning the radar on and off. These will be:

1. Pushing the start/stop momentary switch
2. Issuing a command from the Pi (i.e. SSH in and manually start/stop)
3. Start/stop when motion over a set velocity is detected from the GPS (i.e. when truck is moving, run. stop when the truck has been stopped for a set amount of time)

Rules:

* If the GPS trigger is enabled, it is active from boot with no human action.
* A run started by the button is stopped only by the button (or a long timeout).
* Safety trips, power loss and a full disk stop any run.
* After a manual stop of a GPS-started run, the GPS trigger does not restart the radar until the truck has next come to a stop.

* The button and the command line behave identically and always override the GPS trigger. A button press during a GPS-started run stops it.
* Loss of GPS fix never changes the radar state. The GPS trigger stops the radar only when the receiver positively reports the truck stationary for the set time.
* A failed start is retried a limited number of times with a growing delay. Each SDR start enables the amplifier for about 0.5 s, so unlimited fast retries are not acceptable.

## States, status LEDs and front-panel switch

There are three status LEDs and a momentary push-button switch on the radar box. The LEDs are:

1. POWER (red) - NOT controlled by the Pi. Hard wired to 12 V supply. No action needed.
2. READY (yellow) - GPIO 18 (PWM-capable) - Should be solid yellow when the radar is ready to run, indicating that the user may push the button to start. A "breathing" pattern should be used to indicate that the radar is starting or stopping (i.e. in response to a trigger but not yet ready to go). Blinking may be used to indicate errors as needed.
3. RUNNING (green) - GPIO 17 - Should be on when the radar is running and off otherwise.

There is also a momentary push-button switch read by the GPIO 4 input that is one of the on/off triggers, as specified above. Inputs from it should be debounced aggressively in software.

PWM through the LED's built-in regulator works: on 2026-11-19 the user confirmed that breathing looks smooth, that the patterns are distinguishable and that button presses register. A slow blink remains available as a configured fallback.

State table. "Ready, waiting for the button" and "ready, will start on motion" look the same for now.

| State | Meaning | READY (yellow) | RUNNING (green) |
|---|---|---|---|
| Off / booting | Pi off, or services not up yet | off | off |
| Not ready | A requirement is missing (telemetry, SDR, NVMe, disk space) | fast blink | off |
| Ready | Waiting for the button or for motion | solid | off |
| Starting | Build, turn-on check, restart at power | breathing or slow blink | off |
| Running | Recording at the requested power | off | solid |
| Stopping | Radar exiting, amplifier being made safe | breathing or slow blink | off |
| Fault | Turn-on check failed, safety trip or repeated start failure | fast blink | off |
| Power fail | Shutting down on the capacitor | off | off |

# Safety watcher

The system should be watched for potential (hardware) safety conditions that might require warnings to the user or some action. Conditions to watch:

* Unsafe temperatures on any device
* Unexpected high current for a prolonged period into the power amplifier while it should be in shutdown state (see `~/radar-testbench/pa_bringup`)

One of the known risk scenarios is that the SDR is left in a state that leaves the power amplifier in its active state (not in shutdown). This can be resolved by running new code on the SDR to reset GPIO pins to a safe configuration.

The Pi should not be turned off as this will inhibit the user's ability to correct a problem while probably not solving it. (The exception is the power cut case previously described.)

Running the radar without amplifier telemetry requires a user override. The override persists across reboots until the user clears it.

After an over-temperature stop or a telemetry dropout, the radar resumes by itself once the readings recover, a limited number of times.

No hardware over-temperature cutoff is planned, so this watcher is the only protection against an overheating amplifier. It does not run while the Pi is off or hung. The datasheet lists built-in temperature protection in the amplifier, but its behaviour has not been characterised.

* Starting thresholds, from `pa_run.py` and `pa_guard.py` on the bench: warn at 50 C and stop at 55 C on the amplifier; stop if the 28 V rail is outside 26-30.5 V; stop if telemetry is lost for more than a few seconds; act if the amplifier draws more than 0.5 A for 5 s with no radar process (shutdown is 0.156 A, enabled is about 2.5 A).
* The watcher's only actions are stopping the radar and resetting the SDR GPIO. Nothing on the Pi can remove the 28 V. If the amplifier stays enabled after a reset, the response is an urgent alert and a technician cutting power. Enabled with no RF is about 72 W of heat, which runs away in a closed case with the fans off.
* Enable the Pi's hardware watchdog so that a hung Pi reboots and the watcher comes back.
* At every boot, before anything else touches the SDR, the watcher checks the amplifier current and resets the SDR GPIO if the amplifier is enabled.

TODO: Cold limits. The amplifier is rated to -40 C case temperature. Do the SDR, Pi, NVMe drive or capacitor need a warm-up before the radar starts?

# Fault responses

| Fault | Response |
|---|---|
| Amplifier telemetry missing at start | Not ready. No radar without the override |
| Amplifier telemetry lost during a run | Stop the radar, keep trying to reconnect, resume when it returns |
| Amplifier over temperature or 28 V out of range | Stop the radar, alert, resume when the reading recovers (limited number of times) |
| No PPS at the SDR | Radar runs on host time. Warning; collection flagged |
| Amplifier drawing current with no radar process | Reset SDR GPIO; if it persists, urgent alert and retry periodically |
| GPS no fix or receiver missing | Radar runs. GPS trigger holds its current state. Warning; data flagged as lacking position |
| NVMe missing or not writable | Not ready. Radar data is never written to the SD card |
| Disk nearly full | Alert at set levels; stop and refuse to start at a reserve. Never delete data that has not been offloaded |
| UPS monitor unreadable | Radar runs. Warning. Power service relies on GPIO 26 alone |
| SDR missing or on a USB 2 link | Not ready |
| Build fails, or radar exits with an error or crashes | Make the amplifier safe, retry a limited number of times, then fault |
| Turn-on check fails | Fault. Retry at the next button press or trigger |
| High late-command rate or output drift | Warning only |
| Pi hot or throttling | Warning |
| Radar manager crashes | Restarted automatically. The radar is stopped with it and the amplifier made safe |
| Safety watcher crashes | Restarted automatically. The manager stops the radar while the watcher's status is stale |
| Pi hangs | Hardware watchdog reboots it |
| No internet | No effect. Alerts are queued |
| Clock not set | Radar runs. Names stay unique through the boot counter |

# Data management

Data is offloaded regularly over Starlink. Presumming will be in the range 10-1000. The deployment lasts months and the Starlink rate is unknown, so the drive may fill. Collection stops when it does, which is acceptable.

* Offload goes to a Google Cloud Storage bucket; the user provides credentials. The credentials should be a service account limited to this bucket that can create and list objects but not delete or overwrite them, stored outside the git repository.
* Offload tolerates a slow or dropping link. An interrupted file is uploaded again from its start, so ORCA's data chunks should stay small (a few hundred MB at most). The link's availability and transfer rate are logged with the other telemetry.

* Rough data rates, assuming a 5 kHz pulse rate and about 20 kB per record: presum 1000 is about 0.1 MB/s (3.5 GB per 10 h day), presum 100 is about 1 MB/s (35 GB per day), presum 10 is about 10 MB/s (350 GB per day). At presum 10 the 1 TB drive fills in under three days, and a 10 Mbit/s uplink carries only about 45 GB in a 10 h day.
* One directory per collection holding the radar data chunks, the config used, the radar log, the raw GPS log and a copy of the sensor readings for that period.
* Offload copies data and verifies it with checksums. Data is deleted from the Pi only after a verified offload, and never automatically at first.
* Offload runs while the radar is running, because the Pi is only powered when the truck is. Its effect on late-command errors must be measured, and its rate limited.

TODO: The presum to use. With the first field measurements of the Starlink rate, this decides how much of each day can be recorded.

# Alerts

The user should receive alerts. They go to both ntfy and Slack.

* Every alert goes to Slack as the shared record. ntfy carries the same alerts with a priority, so that urgent ones (amplifier stuck enabled, over temperature) can break through on the phone.
* Alerts raised offline are queued and sent when the link returns.
* Alert text goes to third-party services, so it leaves out positions.
* A short summary is sent at each boot (disk use, last collection, any faults). A day without one is itself a sign of trouble.
* Technicians see faults only through the LED. The user contacts them when action is needed.

# System setup

These are changes outside this repository. Each is recorded in a list so that a spare SD card can be built the same way.

* Turn off automatic updates: `unattended-upgrades`, the apt timers, snap refresh, firmware refresh, Tailscale's own self-update, and the bootloader's automatic flash at boot. The kernel had changed from 6.8.0-1065 to 6.8.0-1068 between 2026-10-28 and 2026-11-18.
* A manual `apt install` must not restart services by itself (needrestart set to list only).
* Enable the hardware watchdog (30 s; never a multiple of 16 s on this driver).
* Keep more journal history: 8 GB and 1000 files. The defaults (4 GB, 100 files) held only about 50 boots, because every boot starts at least two files.
* The power service's directories (`/run/radar-runner`, `/var/lib/radar-runner`) stay writable by root only. Services running as the user get a `user` subdirectory in each. A shared writable directory would let a stray file from a non-root process stop the power service from saving its state, and it then refuses to shut down.

Applied on 2026-11-19 with `setup/setup.sh` and verified with `setup/check.py` (62 pass, 0 fail after the reboot). The list of changes is `setup/SYSTEM_CHANGES.md`. Bench results with the watchdog on: `systemctl poweroff`, `poweroff -f -n` and an armed power pull each left the Pi off until power was cycled; a reboot works.

* Pin the ORCA commit and the patched UHD build.
* Tailscale key expiry is already disabled on this node (checked 2026-11-18).
* Updates in the field: each release of this repository is a tagged commit; an update checks out a tag and restarts the services, and a rollback checks out the previous tag. The power service and system setup are not updated in the field without a strong reason.

TODO: Will a spare SD card ship with the system?

## Reproducing the setup

PROPOSED:

Two methods, for two different needs.

* **Spare card for this Pi: a clone.** After the final bench tests the SD card is imaged from another computer, and the image is kept off the Pi and written to a spare card. The spare is identical, including the Tailscale identity, which is correct for a replacement card in the same Pi. It goes stale after field updates, so the first step after a swap is to update this repository to the current release.
* **Second Pi, or a rebuild from nothing: a setup script.** One script in this repository starts from the stock Ubuntu image and applies every item on the system-changes list. It can be run again safely on a system that is already set up.

What makes either method trustworthy is a **check script** that tests every item (package versions, config files, services enabled, updates off, watchdog on, bootloader settings, ORCA commit, UHD build, Python environment) and prints pass or fail for each. The same script runs on the original Pi, on a clone, on a rebuilt system and after every field update. Any difference between two systems shows up as a failed line.

Things that must not be copied blindly to a second Pi are kept apart from the common setup:

* Identity: hostname, SSH host keys, machine ID and the Tailscale login
* Secrets: the bucket credentials, the ntfy topic and the Slack webhook, in one file outside git
* Settings measured on one unit: sensor calibration, thresholds and the expected turn-on peak, in a per-unit config file layered over the defaults
* The NVMe mount: mounted by filesystem label instead of by UUID, so that any drive with the right label works
* Settings stored in hardware, not on the card: the Pi's bootloader configuration, the GPS receiver settings and the OLA settings. The last two are applied at every boot anyway; the check script covers the bootloader

Inputs that need internet today are stored so that a rebuild works without it: the stock Ubuntu image, the Python wheels, the UHD source with its patch (or the built copy in `~/uhd-patched`), the SDR's FPGA images, and a list of installed package versions. Only the packages that matter to the radar (kernel, gpsd, chrony) are held at fixed versions.

The setup script is proven by using it: one card is built from the stock image before shipping and has to pass the check script and the stage tests. If there is no time for that, the clone is the fallback, and the script stays unproven until tested.

# Tech stack

| Area | Choice |
|---|---|
| Supervision | One systemd unit per service, restart on failure, systemd's watchdog for hangs, and a stop hook that makes the amplifier safe |
| Language | Python |
| Python environment | A dedicated virtual environment for this repository, built by uv from a lock file on the system's Python 3.12. It is separate from the `orca` mamba environment, which is used only to run ORCA's chirp generation. The wheels are kept on the Pi so that the environment can be rebuilt without internet |
| Power service | Standard library only, run by `/usr/bin/python3` as root, so that it does not depend on the virtual environment. It reads the INA219 and GPIO 26 through the kernel interfaces, as `~/ups-bringup/ups_check.py` does |
| Sharing readings | Each service writes a small JSON status file (`/run/radar-runner/power.json` for the power service, `/run/radar-runner/user/<service>.json` for the others); readers treat an old file as stale, judged by uptime and boot ID, not the wall clock. `pa_guard.py` and the HIL harness already work this way |
| Commands | A `radarctl start/stop/status` command talking to the manager over a Unix socket |
| Sensor logs | One CSV file per source per boot on the NVMe drive; service logs go to the system journal |
| GPS and time | gpsd as owner; chrony for the system clock (GPS, plus NTP when online); `gpspipe -R` for raw PPK data; `ubxtool` for receiver settings |
| Button and LEDs | gpiozero with the lgpio backend |
| GPIO reset tool | Small C++ program against the patched UHD, ported from `gpio_hold.cpp` |
| Config | One YAML file for this repository |
| Alerts | HTTP posts to ntfy and to a Slack incoming webhook, with a queue on disk for offline periods |
| Offload | rclone to a Google Cloud Storage bucket, rate-limited and low priority |
| Tests | pytest, a stand-in for the radar binary, `gpsfake` |

Python dependencies are pyserial, PyYAML, gpiozero and lgpio. All four have ARM64 wheels for Python 3.12 on PyPI (checked 2026-11-18). The gpsd client and the systemd watchdog notification need no package: both are a few lines over a socket.

uv is installed (0.13.7). chrony is installed in stage 2 together with its GPS configuration, and rclone in stage 9. Until then systemd-timesyncd stays: it restores the last saved time at boot, which Ubuntu's default chrony does not, and the Pi has no clock battery.

# Testing before shipping

* Power pulled many times in each state (idle, starting, running, stopping, during shutdown, restored during shutdown), checking for a clean boot and intact data every time.
* Each row of the fault table forced on the bench: OLA, GPS, SDR and NVMe unplugged; each service killed; the radar killed mid-run; the amplifier left enabled.
* GPS trigger tested with recorded or simulated tracks through `gpsfake`, which ships with gpsd.
* A long run with logging, offload and alerts active, compared against the HIL late-command baseline.
* Development and most tests run without transmitting, against a stand-in for the radar binary.

# Web-based interface

This part should be implemented last, but is left here for context.

A web-based interface should be made available on the local network and via tailscale. It is read-only for now. The interface should:

* Show the current status of the radar, including what trigger it to run if it's running or starting up
* List recent data collections
* Display auxiliary sensor telemetry
* Show warnings about off-nominal sensor values and other fault conditions
* Show minimal previews of data being collected to enable checking if the radar data looks ok

TODO: How do we enable useful previews without putting excess load on the processor?

With presumming, the recorded data rate is low. A low-priority process can read the newest record every few seconds, pulse-compress it and save a small image. The same numbers can feed an in-run check that the direct antenna signal is still present. The load must be measured against the late-command baseline before it is enabled.

# Implementation stages

Stages are in priority order. The parts that are hardest to fix remotely come first, and nothing transmits through the amplifier under this repository's control until the safety watcher exists. Later stages can be pushed out over Starlink.

Rules for every stage:

* A stage is finished when its "Done when" tests pass on the bench. The next stage starts from that state.
* Any stage that touches the SDR or the amplifier starts with a safety review and the user's go-ahead.
* Changes outside this repository are recorded in the system-changes list. ORCA changes are approved one at a time and never committed by an agent.
* Services are developed against stand-ins (a fake radar binary, recorded telemetry, `gpsfake`) and then tested on the real hardware.

| Stage | Result |
|---|---|
| 0. Foundation and system setup | A frozen, reproducible system and an empty service skeleton |
| 1. Power service | The Pi shuts down cleanly on power loss and never boot loops |
| 2. Telemetry and time | All sensors logged from boot; clock set without internet |
| 3. Safety watcher | The amplifier is watched and recovered independently of everything else |
| 4. Radar manager core | Button and command-line collections with data that survives a hard cut |
| 5. Alerts | The user hears about faults and gets a summary at each boot |
| 6. GPS logging and motion trigger | Raw GPS data for PPK; collections start and stop with the truck |
| 7. Turn-on check | A disconnected antenna or damaged cable is detected at each start |
| 8. PPS timing | Radar records carry GPS time |
| 9. Offload | Data reaches the cloud bucket without manual work |
| 10. Web interface | Status and previews in a browser |

## Stage 0: Foundation and system setup

Builds:

* Repository layout, the uv environment with its lock file and stored wheels, the YAML config file, and the shared code for status files and logging
* The setup script and the check script (see "Reproducing the setup"), and the system-changes list
* The changes in "System setup": automatic updates off, journal limits, hardware watchdog on, shutdown inhibitor delay removed, directories, uv installed

Needs from the user: approval of each system change (most need root).

Done when: the Pi reboots cleanly with every change in place, SSH and Tailscale work, and each change can be verified with a command recorded in the list.

## Stage 1: Power service

Builds:

* The power service as described in "Pi power state management", run first in a monitor-only mode that logs the decisions it would take, then armed
* The boot-loop guard and the disable file
* The hooks for "stop the radar" and "make the amplifier safe", which later stages fill in

Needs from the user: hands on the power connector for the pull tests.

Done when:

* Power pulled at least 20 times while idle: a clean shutdown and an unattended boot every time
* Short outages ride through without a shutdown
* Power restored during the shutdown sequence leads to a reboot, not a Pi left off
* The UPS monitor made unreadable, and the two loss signals made to disagree, cause warnings and no shutdown
* The boot-loop guard stops the service from shutting down after three early shutdowns in a row
* No false shutdown during a 24 h run in monitor-only mode

## Stage 2: Telemetry and time

Builds:

* The telemetry service: OLA connection and ADS1015 setup (from `ola_setup.py` and `pa_run.py`), Pi internal sensors, UPS readings, CSV logs and a status file
* gpsd set to own the receiver, the receiver settings applied at boot, chrony installed in place of systemd-timesyncd and taking time from GPS and from the internet when present. chrony must also restore the last saved time at boot, as systemd-timesyncd does today
* Continuous GPS position logging

Done when:

* A 24 h run has no gaps in any log
* Unplugging and replugging the OLA and the GPS recovers without a restart
* A hard power cut leaves every log readable up to its last full line
* The clock sets itself from GPS with the network unplugged
* The processor load of logging is measured and small

Found 2026-11-19 at the first boot after installing stage 2, now fixed: gpsd delivers no data from the receiver after a boot. gpsd's log at boot shows "device open of /dev/ttyACM0 failed: Device or resource busy" and "device activation failed, freeing device", which fits the receiver being opened twice: once from gpsd's command line (our drop-in) and once by Ubuntu's udev hotplug path. Before the reboot, with gpsd only restarted, it worked. At the same boot the OLA's header had no GPS columns, so the OLA did not see the receiver on its Qwiic bus either; whether that has the same cause is not known. Candidate fix: take the device off gpsd's command line (keep `-n`), leave the hotplug path as the only way the device is added, and order `gpsdctl@.service` after `gpsd.socket`. Restarting gpsd by hand cleared the open failure but not the missing fixes: the receiver's `CFG-UART1OUTPROT-NMEA` then read 1 in RAM (read-only check, 04:16 UTC), gpsd was getting only a partial message set (fix mode 0, time not advancing), and the receiver did not answer a status poll. That is the transmit-buffer overflow again. So the setting is not durably stored after all: it survived the short power cycles earlier in the day and was lost when the system was off for a few minutes. The earlier finding that it was in the receiver's flash, and the decision to remove the service's receiver check on that basis, were wrong. Needed: the setting written to the receiver's flash deliberately (user approval), and a once-per-boot check that re-applies it if it is wrong. Also: the telemetry service's read of the `get_throttled` sysfs file makes the kernel print a one-time "deprecated, use hwmon sysfs instead" warning with a stack trace at every boot; read the hwmon undervoltage alarm instead. **Resolution (2026-11-19):** (1) The double open is fixed: Ubuntu's hotplug path is switched off and gpsd is tied to the receiver's device unit, so systemd starts gpsd when the receiver appears; a reboot confirmed one open and working fixes. (2) The setting was not lost from flash. Its value in the receiver's RAM goes back to 1 at every start of gpsd (four of four observed; an OLA reset alone does not do it), so gpsd's own set-up of the receiver is the likely cause. `radar-gps-receiver.service` now turns it off in RAM 20 s after every gpsd start, and the telemetry service does the same a little after every OLA start as a second layer; the first was confirmed by restarting gpsd (found 1, set 0, full 10 Hz fixes). Removing the earlier receiver check was a mistake and is reversed in this form. Still open: the `get_throttled` kernel warning.

## Stage 3: Safety watcher

Builds:

* The SDR GPIO reset tool, ported from `gpio_hold.cpp`
* The safety watcher with the thresholds in "Safety watcher", the boot-time amplifier check, and a status file the manager can check for freshness
* The "make the amplifier safe" hook of the power service

Needs from the user: the go-ahead for tests on the powered amplifier, and the VVA setting if it changes the thresholds.

Done when:

* Every trip rule passes with injected telemetry (as `test_pa_guard.py` does today)
* On the real hardware, an amplifier deliberately left enabled is returned to shutdown within seconds, both while running and at boot
* Killing the watcher restarts it, and its status goes stale in between
* Unplugging the OLA is reported as lost telemetry

## Stage 4: Radar manager core

Builds:

* The state machine, button, LEDs and `radarctl`
* Radar config limits, the telemetry and watcher gates with the override, disk space checks, and limited retries
* The ORCA runner: chirp generation, build, run, stop, and the amplifier made safe after every exit
* Collection directories named with the boot counter and sequence number
* The "stop the radar" hook of the power service
* ORCA changes, each after approval: data written in chunks straight to the collection directory, and the fix or allowance from the radar exit time investigation

Needs from the user: approval of each ORCA change, the pinned ORCA commit and UHD build, the VVA setting for the config limits, and a check of whether PWM works on the READY LED.

Done when:

* 50 start/stop cycles by button and by command line complete without a fault
* Killing the radar or the manager mid-run leaves the amplifier in shutdown and the system recovers by itself
* A hard power cut mid-run loses at most the chunk being written
* The power-loss sequence passes at least 20 times with the radar running, including after a long run
* The fault table rows for a missing SDR, missing NVMe, lost telemetry, stale watcher and full disk behave as written
* A 2 h run with all services active stays within the HIL late-command baseline

## Stage 5: Alerts

Builds: posts to ntfy and Slack with a queue on disk, alerts for every fault table row that calls for one, and the boot summary.

Needs from the user: an ntfy topic and a Slack webhook.

Done when: alerts raised with the network unplugged arrive after it returns, an urgent alert breaks through on the phone, and a failure of either service does not affect any other service.

## Stage 6: GPS logging and motion trigger

Builds:

* Raw GPS logging for PPK into each collection directory while the radar runs
* The motion trigger with its speed threshold, stop time, and the rule for manual stops

Done when:

* The raw log converts to RINEX and contains carrier-phase observations
* Simulated drives through `gpsfake` start and stop collections as specified, including loss of fix mid-drive and a button stop mid-drive
* A drive with the real receiver outdoors behaves the same way

## Stage 7: Turn-on check

Builds: the low-power run, the search for the direct-signal peak, the mode that records without blocking, and the settings for the expected delay and level.

Needs from the user: antenna spacing and cable lengths, then manual verification of the first field start.

Done when: on the bench the loopback peak is found at the expected delay, and disconnecting the loopback cable fails the check and keeps the radar off when blocking is enabled.

## Stage 8: PPS timing

Builds: the ORCA change that sets the SDR clock at a PPS edge, and the flag on collections made without PPS.

Needs from the user: PPS wired to the SDR before shipping, and approval of the ORCA change. The software can follow after shipping.

Done when: record times agree with GPS time across repeated starts, and removing the PPS produces a flagged collection instead of a failed start.

## Stage 9: Offload

Builds: rclone installed (the apt version is 1.60.1 from 2022; check that it does what the restricted service account needs), rclone uploads of finished chunks to the bucket, checksum verification, a record of what has been offloaded, rate limiting, and logging of the link rate.

Needs from the user: the bucket and a service account limited to creating and listing objects.

Done when: an upload interrupted by a dropped link or a power cut completes later without corrupt or missing objects, and offload during a collection does not raise the late-command rate above the baseline.

## Stage 10: Web interface

Builds: the read-only status page, the list of collections, telemetry plots and low-rate data previews.

Done when: the page works over the local network and Tailscale, and generating previews during a collection does not raise the late-command rate above the baseline.

## At shipping, whatever stage has been reached

* Every service that is installed has passed its own stage tests; unfinished services are not installed
* The power-pull and amplifier-recovery tests are repeated on the final configuration
* The capacitor hold-up time is checked cold if at all possible
* The ORCA commit, UHD build and this repository's release are recorded, and the SD card is imaged
* An update and a rollback over Tailscale are each done once as a rehearsal
