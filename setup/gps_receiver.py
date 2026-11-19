#!/usr/bin/python3
"""Check, and with --apply correct, the one GPS receiver setting this system depends on.

    setup/gps_receiver.py            read CFG-UART1OUTPROT-NMEA and report (changes nothing)
    setup/gps_receiver.py --apply    if it is not 0, set it to 0 in RAM, battery-backed memory
                                     and flash, then read it back
    setup/gps_receiver.py --apply --ram   the same in RAM only (run by the telemetry service a
                                     little after every OLA start: the OLA turns it back on)

Why: the receiver runs at 10 Hz (the OLA sets that). With NMEA output on the unused UART1 the
receiver's transmit buffer overflows and USB carries only part of the messages: gpsd delivers
fewer fixes, or none. The value in the receiver's battery-backed memory and flash is 0 (written
2026-11-19), but the value in its RAM was found back at 1 after every start of gpsd on
2026-11-19 (three of three), and not after a reset of the OLA alone. So it is corrected in RAM
20 s after every start of gpsd (systemd/radar-gps-receiver.service) and, as a second layer, a
little after every OLA start (the telemetry service). Everything goes through gpsd with ubxtool;
standard library only. Exit status: 0 the setting is 0; 1 it is not, or the receiver did not
answer.
"""
import os
import re
import subprocess
import sys

KEY = "CFG-UART1OUTPROT-NMEA"
ENV = dict(os.environ, UBXOPTS="-P 27.50")


def ubxtool(*args):
    try:
        return subprocess.run(["/usr/bin/ubxtool", *args], env=ENV, capture_output=True, text=True,
                              timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"ubxtool failed: {e}"


def read(layer):
    """The key's value in one layer (0 RAM, 1 battery-backed, 2 flash), or None if no answer."""
    m = re.search(rf"item {KEY}/0x[0-9a-f]+ val (\d+)", ubxtool("-g", f"{KEY},{layer}"))
    return int(m.group(1)) if m else None


def main(argv):
    if set(argv) - {"--apply", "--ram"}:
        sys.exit(__doc__)
    ram = read(0)
    print(f"{KEY} in RAM: {'no answer from the receiver' if ram is None else ram}", flush=True)
    if ram == 0:
        return 0
    if ram is None or "--apply" not in argv:
        return 1
    if "--ram" in argv:
        # What the OLA changes at each of its starts is the RAM value only; gpsd needs no restart.
        print(f"setting {KEY} to 0 in RAM", flush=True)
        ubxtool("-z", f"{KEY},0,1")
        ram = read(0)
        print(f"{KEY} in RAM now: {ram}", flush=True)
        return 0 if ram == 0 else 1
    print(f"setting {KEY} to 0 in RAM, battery-backed memory and flash", flush=True)
    ubxtool("-z", f"{KEY},0")          # no layer given: ubxtool writes all three
    values = [read(layer) for layer in (0, 1, 2)]
    print(f"{KEY} now: RAM {values[0]}, battery-backed {values[1]}, flash {values[2]}", flush=True)
    if values[0] != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
