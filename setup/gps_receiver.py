#!/usr/bin/python3
"""Check, and with --apply correct, that gpsd has identified the GPS receiver and the one receiver
setting this system depends on.

    setup/gps_receiver.py            report both (changes nothing, asks the receiver nothing new)
    setup/gps_receiver.py --apply    if gpsd has not identified the receiver, ask the receiver for
                                     its version; then, if CFG-UART1OUTPROT-NMEA is not 0, set it
                                     to 0 in RAM, battery-backed memory and flash, and read it back
    setup/gps_receiver.py --apply --ram   the same with the setting in RAM only (run 20 s after
                                     every start of gpsd, and by the telemetry service a little
                                     after every OLA start and when gpsd delivers no position)

Why the setting: the receiver runs at 10 Hz (the OLA sets that). With NMEA output on the unused
UART1 the receiver's transmit buffer overflows and USB carries only part of the messages: gpsd
delivers fewer fixes, or none. The value in the receiver's battery-backed memory and flash is 0
(written 2026-11-19), but the value in its RAM was found back at 1 after every start of gpsd on
2026-11-19 (three of three), and not after a reset of the OLA alone. So it is corrected in RAM 20 s
after every start of gpsd (systemd/radar-gps-receiver.service) and, as a second layer, a little
after every OLA start (the telemetry service).

Why the identification: gpsd switches on the receiver's position messages only once it has
identified the receiver from its version reply (MON-VER). On 2026-11-19, at the first boot after
a power cut of several minutes, gpsd had not (empty `subtype` in its DEVICES report): the receiver
had a fix, but sent gpsd only time and DOP, and gpsd delivered no position for 77 minutes. Asking
for the version by hand (ubxtool -p MON-VER) made gpsd identify and configure it at once. The cause
is inferred from that one occurrence: gpsd's own version request at its start went unanswered
(the receiver still starting, or the reply lost), and gpsd does not ask again. Not yet tested on
the bench with a cold boot. gpsd's configuration is what sets the UART1 setting back to 1, so the
setting is checked only after gpsd has identified the receiver and had time to configure it.

Everything goes through gpsd: its JSON socket and ubxtool. Standard library only. The run takes
seconds normally and at most about 2 minutes. Exit status:
    0  gpsd has identified the receiver (already, or after the request) and the setting is 0
    1  the setting is not 0, or the receiver did not answer
    2  gpsd has the receiver but has not identified it (also after the request, with --apply)
    3  cannot ask gpsd, or gpsd has no receiver
"""
import json
import os
import re
import socket
import subprocess
import sys
import time

KEY = "CFG-UART1OUTPROT-NMEA"
ENV = dict(os.environ, UBXOPTS="-P 27.50")
UBXTOOL = "/usr/bin/ubxtool"
GPSD = ("127.0.0.1", 2947)
UBXTOOL_TIMEOUT_S = 20      # a poll normally takes 2 s
IDENTIFY_WAIT_S = 20.0      # after the version request: how long to wait for gpsd to identify the receiver
CONFIGURE_WAIT_S = 10.0     # then for gpsd to finish configuring it (an estimate, not measured)
POLL_S = 1.0


def ubxtool(*args):
    try:
        return subprocess.run([UBXTOOL, *args], env=ENV, capture_output=True, text=True,
                              timeout=UBXTOOL_TIMEOUT_S).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"ubxtool failed: {e}"


def read(layer):
    """The key's value in one layer (0 RAM, 1 battery-backed, 2 flash), or None if no answer."""
    m = re.search(rf"item {KEY}/0x[0-9a-f]+ val (\d+)", ubxtool("-g", f"{KEY},{layer}"))
    return int(m.group(1)) if m else None


def gpsd_devices(timeout=3.0):
    """gpsd's devices (its ?DEVICES report, which only reads gpsd's state); an error text if it cannot be asked."""
    try:
        with socket.create_connection(GPSD, timeout=timeout) as sock:
            f = sock.makefile("rwb")
            f.write(b"?DEVICES;\n")
            f.flush()
            for _ in range(10):     # the VERSION line first; a reply to a question comes at once
                line = f.readline()
                if not line:
                    break
                if line.startswith(b'{"class":"DEVICES"'):
                    devices = json.loads(line).get("devices")
                    return [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []
        return "gpsd did not answer ?DEVICES"
    except (OSError, ValueError, AttributeError) as e:
        return f"cannot ask gpsd at {GPSD[0]}:{GPSD[1]}: {e}"


def identified(devices):
    return all(d.get("subtype") or d.get("subtype1") for d in devices)


def describe(devices):
    return "; ".join(f"{d.get('path')}: {d.get('subtype1') or d.get('subtype') or 'not identified'}"
                     f" (driver {d.get('driver') or 'none yet'})" for d in devices)


def identify(apply):
    """0 if gpsd has identified the receiver (already, or after the request), 2 if not, 3 if it cannot tell."""
    devices = gpsd_devices()
    if isinstance(devices, str) or not devices:
        print(devices or "gpsd has no receiver", flush=True)
        return 3
    if identified(devices):
        print(f"gpsd has identified the receiver: {describe(devices)}", flush=True)
        return 0
    print(f"gpsd has NOT identified the receiver: {describe(devices)}", flush=True)
    if not apply:
        return 2
    # gpsd identifies the receiver from the version reply, whoever asked for it, and then configures it.
    print("asking the receiver for its version (MON-VER), for gpsd to identify it", flush=True)
    asked = time.monotonic()
    ubxtool("-p", "MON-VER")
    deadline = time.monotonic() + IDENTIFY_WAIT_S
    while True:
        devices = gpsd_devices(timeout=max(min(3.0, deadline - time.monotonic()), 0.1))
        if isinstance(devices, list) and devices and identified(devices):
            print(f"gpsd has identified the receiver {time.monotonic() - asked:.0f} s after the request: "
                  f"{describe(devices)}", flush=True)
            print(f"waiting {CONFIGURE_WAIT_S:.0f} s for gpsd to configure the receiver", flush=True)
            time.sleep(CONFIGURE_WAIT_S)
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(min(POLL_S, max(deadline - time.monotonic(), 0.0)))
    print(f"gpsd has still not identified the receiver {time.monotonic() - asked:.0f} s after the request: "
          + (devices if isinstance(devices, str) else describe(devices) or "no receiver"), flush=True)
    return 2


def main(argv):
    if set(argv) - {"--apply", "--ram"}:
        sys.exit(__doc__)
    apply = "--apply" in argv
    status = identify(apply)
    if status == 3:
        return 3
    # Without identification gpsd has not configured the receiver yet: the setting is still checked
    # (it may be 1 from the OLA), and the next run checks it again after gpsd has.
    ram = read(0)
    print(f"{KEY} in RAM: {'no answer from the receiver' if ram is None else ram}", flush=True)
    if ram == 0:
        return status
    if ram is None or not apply:
        return status or 1
    if "--ram" in argv:
        # What the OLA changes at each of its starts is the RAM value only; gpsd needs no restart.
        print(f"setting {KEY} to 0 in RAM", flush=True)
        ubxtool("-z", f"{KEY},0,1")
        ram = read(0)
        print(f"{KEY} in RAM now: {ram}", flush=True)
        return status or (0 if ram == 0 else 1)
    print(f"setting {KEY} to 0 in RAM, battery-backed memory and flash", flush=True)
    ubxtool("-z", f"{KEY},0")          # no layer given: ubxtool writes all three
    values = [read(layer) for layer in (0, 1, 2)]
    print(f"{KEY} now: RAM {values[0]}, battery-backed {values[1]}, flash {values[2]}", flush=True)
    return status or (0 if values[0] == 0 else 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
