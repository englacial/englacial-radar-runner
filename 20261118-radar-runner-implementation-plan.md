This system is going to be deployed as a truck-towed radio echo sounder in Antarctica. The system specifications are listed in `hardware_reference/SORA Instrument Spec Sheet for Arctic Trucks.md`. At any given time, `HARDWARE_CONFIG.md` should specify the current actual hardware setup, which may not always match the system specs during testing.

Once deployed, the team will not have physical access to the hardware, so robustness to many possible faults is important. Equally, simplicity is extremely important. When possible, the simple solution should be used. When an existing well-tested tool for the job exists, it should be used.

This repository will serve as the radar "manager". Actual radar data acquisition code should remain in `~/orca/`. This repository is responsible for other services on this Pi and for calling the ORCA code at the right times. The primary jobs of this repository are:

1. Manage the Pi's power state, including gracefully shutting down when power is removed from the system
2. Continuously log auxiliary sensor telemetry (internal temperatures, GPS, etc)
3. Manage turning the radar on and off according to configured triggers
4. Watch for safety conditions that might require turning off the radar
5. Provide a web-based user interface for monitoring the current status

This is something like a (much more full-featured) replacement for `~/orca/manager/` (used on a different type of deployment).

All of these functions must start when the Pi is booted up. Nominally, the Pi should be online via Starlink for most of the deployment, but everything in the system should be robust to not having internet.

Each function is described in a section below.

# Pi power state management

If main power is removed, any running radar service should be safely stopped without loss of data, safe condition of the amplifier checked, and the Pi should be shut down. The Pi is equipped with a supercapacitor backup that allows for brief runtime to perform these tasks.

This is a critical system to prevent data corruption. It is also critical that it not fail in a way that would shut down the Pi constantly or boot loop it. Care should be taken to handle possible intermittent connections with the supercapacitor board.

This power state management should run as a separate service that can coordinate with the radar manager code as needed.

Power loss is the expected way that the radar will be shut down, not an off-nominal case.

# Sensor logging

The following sensors should be logged continuously at a configurable rate. Note that some of these sensors are also needed by other parts of the system, so this should be considered.

* All I2C devices on the OpenLog Artemis bus (see `HARDWARE_CONFIG.md`)
* Internal temperatures sensors, system load, and throttling state from this Pi 5
* GPS data from the ublox F9P

TODO: What kind of storage structure should be used to save this data?

Full raw GPS data for later processing PPK solutions only needs to be saved when the radar is actually running. This may be better handled directly by ORCA.
TODO: Decide if ORCA or this repo handles logging GPS data for PPK solutions.

Logging that runs while the radar is running must be computationally lightweight to avoid adding much load to the processor.

# Radar management

## Safe turn-on

Since this radar will be installed and operated by technicians who haven't worked on radar before, the radar manager service should start data collections with a low output power relatively safe configuration and verify that results appear as expected before switching to the requested power.

TODO: What a "safe" config looks like needs to be explored. Always a single config? Same as requested config but with capped TX gain? How do we verify and what happens if the verification doesn't pass.

## Trigger-based on/off

There should be multiple configurable (i.e. can be enabled or disabled) ways of turning the radar on and off. These will be:

1. Pushing the start/stop momentary switch
2. Issuing a command from the Pi (i.e. SSH in and manually start/stop)
3. Start/stop when motion over a set velocity is detected from the GPS (i.e. when truck is moving, run. stop when the truck has been stopped for a set amount of time)

## Status LEDs and front-panel switch

There are three status LEDs and a momentary push-button switch on the radar box. The LEDs are:

1. POWER (red) - NOT controlled by the Pi. Hard wired to 12 V supply. No action needed.
2. READY (yellow) - GPIO 18 (PWM-capable) - Should be solid yellow when the radar is ready to run, indicating that the user may push the button to start. A "breathing" pattern should be used to indicate that the radar is starting or stopped (i.e. in response to a trigger but not yet ready to go). Blinking may be used to indicate errors as needed.
3. RUNNING (green) - GPIO 17 - Should be on when the radar is running and off otherwise.

There is also a momentary push-button switch read by the GPIO 4 input that is one of the on/off triggers, as specified above. Inputs from it should be debounced aggressively in software.

# Safety watcher

The system should be watched for potential (hardware) safety conditions that might require warnings to the user or some action. Conditions to watch:

* Unsafe temperatures on any device
* Unexpected high current for a prolonged period into the power amplifier while it should be in shutdown state (see `~/radar-testbench/pa_bringup`)

TODO: Need to set (configurable) thresholds.
TODO: Decide how the user is notified for issues and when notification vs immediate action is required

One of the known risk scenarios is that the SDR is left in a state that leaves the power amplifier in its active state (not in shutdown). This can be resolved by running new code on the SDR to reset GPIO pins to a safe configuration.

The Pi should not be turned off as this will inhibit the user's ability to correct a problem while probably not solving it. (The exception is the power cut case previously described.)

# Web-based interface

This part should be implemented last, but is left here for context.

A web-based interface should be made available on the local network and via tailscale. The interface should:

* Show the current status of the radar, including what trigger it to run if it's running or starting up
* List recent data collections
* Display auxiliary sensor telemetry
* Show warnings about off-nominal sensor values and other fault conditions
* Show minimal previews of data being collected to enable checking if the radar data looks ok

TODO: How do we enable useful previews without putting excess load on the processor?