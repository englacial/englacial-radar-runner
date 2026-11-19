#!/usr/bin/python3
"""Check, and with --apply correct, that gpsd has identified the GPS receiver and gets positions
from it, and the one receiver setting this system depends on.

    setup/gps_receiver.py            report (changes nothing, asks the receiver nothing new)
    setup/gps_receiver.py --apply    as below; CFG-UART1OUTPROT-NMEA is set to 0 in RAM,
                                     battery-backed memory and flash
    setup/gps_receiver.py --apply --ram   everything in RAM only (run 20 s after every start of
                                     gpsd, and by the telemetry service a little after every OLA
                                     start and when gpsd delivers no position)

With --apply, in this order:
 1. If gpsd has not identified the receiver (no MON-VER reply seen: empty `subtype` in its
    DEVICES report), ask the receiver for its version (MON-VER) and wait up to 20 s for gpsd to
    identify it. gpsd then configures the receiver again (see below).
 2. If CFG-MSGOUT-UBX_NAV_PVT_USB (the position message) is 0 in RAM, set it to 1 in RAM. Never
    to battery-backed memory or flash.
 3. If CFG-UART1OUTPROT-NMEA is not 0, set it to 0.
With --ram, a setting whose read gets no answer is written anyway (both writes are idempotent and
RAM only), because the state in which an answer gets lost is the one that needs the write.

What gpsd 3.25 does (driver_ubx.c): when it recognises UBX at its start it sends MON-VER and,
without waiting for acknowledgements, a burst of configuration. That burst addresses UART1 (gpsd
takes a device path without "/ttyACM" for UART1, and ours is the by-id name) and turns its NMEA
output on: this is why CFG-UART1OUTPROT-NMEA is back at 1 in RAM after every start of gpsd. With
the protocol version unknown it switches on NAV-PVT and the other position messages. The first
NAV-PVT it reads tells it "protocol version at least 14"; it then configures again for version
14 and switches NAV-PVT, NAV-POSECEF, NAV-VELECEF, NAV-SAT, NAV-SIG and NAV-EOE off, leaving
NAV-DOP and NAV-TIMEGPS. Only a MON-VER reply (version 27.50) or one of NAV-SAT, NAV-SIG or
NAV-EOE moves it on, and then it configures again with everything on. It does not ask for the
version again by itself.

Observed on 2026-11-19, at the first boot after a power cut of several minutes: gpsd's `subtype`
was empty, the receiver sent gpsd only NAV-TIMEGPS and NAV-DOP although it had a 3D fix, and
gpsd delivered fix times with mode 0 for 77 minutes. A MON-VER request by hand made gpsd identify
the receiver and switch the position messages on at once. That is the version-14 state above;
the way in (NAV-PVT read before any MON-VER reply, or part of gpsd's burst lost) is inferred
from this one occurrence and not yet reproduced. A cold-boot test on the bench (several minutes
without power) is still to be done. If gpsd's second burst loses the NAV-PVT enable, gpsd is
identified but has no position, and a further MON-VER does not help: step 2 is for that. On a
gpsd that has never read a NAV-PVT, step 2 can take two runs to stick (gpsd turns NAV-PVT off
once, when it reads the first one).

Why CFG-UART1OUTPROT-NMEA: the receiver runs at 10 Hz (the OLA sets that). With NMEA output on
the unused UART1 its transmit buffer overflows and USB carries only part of the messages: gpsd
delivers fewer fixes, or none. gpsd's configuration and every OLA start set it to 1 in RAM.

Everything goes through gpsd: its JSON socket and ubxtool. Standard library only. The run takes
seconds normally and at most about 100 s. Exit status:
    0  gpsd has identified the receiver (already, or after the request), NAV-PVT is on and the
       setting is 0 (after any correction)
    1  NAV-PVT is off or the setting is not 0 after the run, or the receiver did not answer
    2  gpsd has the receiver but has not identified it (also after the request, with --apply)
    3  cannot ask gpsd, or gpsd has no receiver
    4  wrong options
"""
import json
import os
import re
import socket
import subprocess
import sys
import time

KEY = "CFG-UART1OUTPROT-NMEA"
PVT = "CFG-MSGOUT-UBX_NAV_PVT_USB"
ENV = dict(os.environ, UBXOPTS="-P 27.50")
UBXTOOL = "/usr/bin/ubxtool"
GPSD = ("127.0.0.1", 2947)
UBXTOOL_TIMEOUT_S = 10      # a poll normally takes 2 s
IDENTIFY_WAIT_S = 20.0      # after the version request: how long to wait for gpsd to identify the receiver
# gpsd writes its configuration in the same step as it reads the version reply, before the DEVICES
# report shows `subtype`: this only leaves the receiver time to act on it.
CONFIGURE_WAIT_S = 3.0
POLL_S = 1.0


def ubxtool(*args):
    try:
        return subprocess.run([UBXTOOL, *args], env=ENV, capture_output=True, text=True,
                              timeout=UBXTOOL_TIMEOUT_S).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"ubxtool failed: {e}"


def read(key, layer=0):
    """The key's value in one layer (0 RAM, 1 battery-backed, 2 flash), or None if no answer."""
    m = re.search(rf"item {key}/0x[0-9a-f]+ val (\d+)", ubxtool("-g", f"{key},{layer}"))
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
    # `subtype` comes from the MON-VER reply ("Unknown" is gpsd's placeholder for older receivers).
    return all(d.get("subtype1") or d.get("subtype") not in (None, "", "Unknown") for d in devices)


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
            time.sleep(CONFIGURE_WAIT_S)
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(min(POLL_S, max(deadline - time.monotonic(), 0.0)))
    print(f"gpsd has still not identified the receiver {time.monotonic() - asked:.0f} s after the request: "
          + (devices if isinstance(devices, str) else describe(devices) or "no receiver"), flush=True)
    return 2


def correct(key, want, now, apply, ram_only):
    """Read `key` in RAM (already done: `now`), write `want` if it differs; True if it is `want` at the end."""
    print(f"{key} in RAM: {'no answer from the receiver' if now is None else now}", flush=True)
    if now == want or not apply or (now is None and not ram_only):
        return now == want
    if ram_only:
        print(f"setting {key} to {want} in RAM", flush=True)
        ubxtool("-z", f"{key},{want},1")
        now = read(key)
        print(f"{key} in RAM now: {'no answer from the receiver' if now is None else now}", flush=True)
        return now == want
    print(f"setting {key} to {want} in RAM, battery-backed memory and flash", flush=True)
    ubxtool("-z", f"{key},{want}")          # no layer given: ubxtool writes all three
    values = [read(key, layer) for layer in (0, 1, 2)]
    print(f"{key} now: RAM {values[0]}, battery-backed {values[1]}, flash {values[2]}", flush=True)
    return values[0] == want


def main(argv):
    if set(argv) - {"--apply", "--ram"}:
        print(__doc__, file=sys.stderr)
        return 4
    apply, ram_only = "--apply" in argv, "--ram" in argv
    status = identify(apply)
    if status == 3:
        return 3
    # Also without identification: if gpsd has read a NAV-PVT already, it keeps it on now; if not,
    # it turns it off once and the next run sets it again.
    pvt_ok = correct(PVT, 1, read(PVT), apply, True)        # RAM only, also with --apply alone
    # gpsd's configuration is what sets this one back to 1, so it is checked last.
    nmea_ok = correct(KEY, 0, read(KEY), apply, ram_only)
    return status or (0 if pvt_ok and nmea_ok else 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
