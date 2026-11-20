#!/usr/bin/python3
"""Check, and with --apply correct, that gpsd has identified the GPS receiver and gets positions
from it, and the receiver settings this system depends on.

    setup/gps_receiver.py            report: the settings below in all three layers (only reads)
    setup/gps_receiver.py --apply    provisioning, once at setup: the settings below are written to
                                     RAM, battery-backed memory and flash where any layer differs
    setup/gps_receiver.py --apply --ram   NAV-PVT and UART1 NMEA in RAM only (run 20 s after every
                                     start of gpsd, and by the telemetry service a little after
                                     every OLA start and when gpsd delivers no position)
    setup/gps_receiver.py --comms    the receiver's port statistics (MON-COMMS, a poll that sets
                                     nothing), twice 10 s apart: transmit-buffer usage and peak,
                                     bytes per second, error flags

The settings are the USB output tested on this unit, which gpsd without -p used to set: the
messages gpsd needs (NAV-PVT, SAT, SIG, EOE, POSECEF, VELECEF, DOP and TIMEGPS at every epoch,
NAV-TIMELS every 255 epochs), the six NMEA sentences that a receiver at factory settings sends on
USB (GGA, GLL, GSA, GSV, RMC, VTG) off, UBX output on USB, and no NMEA output on the unused UART1;
and for the raw GPS log (PPK, radar_runner/gpsraw) RXM-RAWX every `manager.gps_log.rawx_every`
epochs (a setting, read from the repository's config and /etc/radar-runner/local.yaml; 10 = 1 Hz
at 10 Hz) and RXM-SFRBX (the broadcast navigation data) as it comes; rawx_every 0 sets both off. gpsd runs passive (-p) and
sets none of them. Left on, the NMEA sentences would come at every
epoch at the OLA's 10 Hz on top of the UBX messages, which already fill most of the USB transmit
buffer. The OLA copies the receiver's settings from RAM to battery-backed memory and flash at
every OLA start (measured 2026-11-20), so what is in RAM then is what the receiver powers up with
next time. Left alone: the measurement rate (the OLA sets 10 Hz and saves it), NMEA as a protocol
on USB (CFG-USBOUTPROT-NMEA: switching it off is untested), and the MON-RF and MON-SPAN messages
(the gps_jam logger's). Provisioning a receiver at factory settings has not been tested: after
it, `setup/check.py --receiver` (transmit errors, buffer peak) must pass.

In this order:
 1. If gpsd has not identified the receiver (no MON-VER reply seen: empty `subtype` in its
    DEVICES report), with --apply ask the receiver for its version (MON-VER) and wait up to 20 s
    for gpsd to identify it.
 2. With --ram: if CFG-MSGOUT-UBX_NAV_PVT_USB (the position message) is not 1 in RAM, set it to
    1 in RAM; then the same for CFG-UART1OUTPROT-NMEA and 0. A read that gets no answer is
    followed by the write anyway: both writes are idempotent, and the state in which an answer
    gets lost is the one that needs the write.
    Without --ram: read every setting in the three layers (one read per layer, asked once more if
    the reply is lost) and report them. With --apply, write those that are wrong in any layer to
    all three, in one write, and report them again. Nothing is written while a read has no
    answer, or when the receiver refuses the read of RAM (RAM holds every setting it knows, so
    that means a setting unknown to its firmware). A warning is printed when gpsd is not running
    passive: at its next start it would set part of this again.

What gpsd 3.25 does without -p (driver_ubx.c): at its start it sends MON-VER and, without waiting
for acknowledgements, a burst of configuration. That burst addresses UART1 (gpsd takes a device
path without "/ttyACM" for UART1, and ours is the by-id name) and turns its NMEA output on: this
is why CFG-UART1OUTPROT-NMEA was back at 1 in RAM after every start of gpsd. With the protocol
version unknown it switches on NAV-PVT and the other position messages. The first NAV-PVT it reads
tells it "protocol version at least 14"; it then configures again for version 14 and switches
NAV-PVT, NAV-POSECEF, NAV-VELECEF, NAV-SAT, NAV-SIG and NAV-EOE off, leaving NAV-DOP and
NAV-TIMEGPS. Only a MON-VER reply (version 27.50) or one of NAV-SAT, NAV-SIG or NAV-EOE moves it
on, and then it configures again with everything on. It does not ask for the version again by
itself. With -p it sends none of this, but still asks for the version.

Observed on 2026-11-19, at the first boot after a power cut of several minutes (gpsd without -p):
gpsd's `subtype` was empty, the receiver sent gpsd only NAV-TIMEGPS and NAV-DOP although it had a
3D fix, and gpsd delivered fix times with mode 0 for 77 minutes. A MON-VER request by hand made
gpsd identify the receiver and switch the position messages on at once. That is the version-14
state above; the way in (NAV-PVT read before any MON-VER reply, or part of gpsd's burst lost) is
inferred and was never reproduced. Passive gpsd removes that way in: after nine restarts and six
cold boots with -p (2026-11-19/20) there was nothing to correct. The runs after every start remain
as the safety net for what -p does not cover: gpsd not identifying the receiver, the position
message or UART1 NMEA changed in RAM by something else (the OLA configures the receiver at its
start), and a replaced receiver that was never provisioned.

Why CFG-UART1OUTPROT-NMEA: the receiver runs at 10 Hz (the OLA sets that). With NMEA output on
the unused UART1 its transmit buffer overflows and USB carries only part of the messages: gpsd
delivers fewer fixes, or none.

The RAM-only run after every start of gpsd keeps to NAV-PVT and UART1 NMEA: those two decide
whether gpsd has positions at all, and every write reaches flash at the next OLA start. The raw
messages missing show in each collection's collection.json (`gps_raw.no_raw_data`) and in
`setup/check.py --receiver`.

Everything goes through gpsd: its JSON socket and ubxtool. Standard library only; the setting
`rawx_every` is read with the repository's Python environment, as setup/check.py does. The run
takes seconds normally and at most about 100 s (about 170 s without --ram). Exit status:
    0  gpsd has identified the receiver (already, or after the request) and every setting checked
       is right (after any correction)
    1  a setting checked is not right after the run, or the receiver did not answer
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
# Provisioned in all three layers, the USB output tested on this unit (gpsd without -p used to set
# it): the messages gpsd needs at every epoch, NAV-TIMELS every 255 epochs (gpsd's rate; its only
# source of a leap-second warning), the six default NMEA sentences off, UBX on USB, no NMEA on UART1.
SETTINGS = {f"CFG-MSGOUT-UBX_NAV_{m}_USB": 1
            for m in ("PVT", "SAT", "SIG", "EOE", "POSECEF", "VELECEF", "DOP", "TIMEGPS")}
SETTINGS["CFG-MSGOUT-UBX_NAV_TIMELS_USB"] = 255
SETTINGS.update({f"CFG-MSGOUT-NMEA_ID_{m}_USB": 0 for m in ("GGA", "GLL", "GSA", "GSV", "RMC", "VTG")})
SETTINGS.update({"CFG-USBOUTPROT-UBX": 1, KEY: 0})
# The raw GPS log: RXM-RAWX every `rawx_every` epochs (manager.gps_log.rawx_every), RXM-SFRBX as it comes.
RAWX, SFRBX = "CFG-MSGOUT-UBX_RXM_RAWX_USB", "CFG-MSGOUT-UBX_RXM_SFRBX_USB"
# One read (4 bytes a key) and one write (5 bytes a key: all of these are one-byte values) must
# each fit in the 255 bytes that gpsd passes on per message, with 12 bytes of frame and header.
assert 12 + 5 * (len(SETTINGS) + 2) <= 255
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(REPO, ".venv", "bin", "python")
LAYERS = ("RAM", "battery-backed", "flash")
NOT_STORED = "not stored"   # left out of the reply: that layer holds no value and the default applies
# RAM holds every setting the firmware knows: one left out of a RAM reply, or a refused RAM read,
# means a setting unknown to this receiver.
UNKNOWN = "refused (unknown to the receiver?)"
ENV = dict(os.environ, UBXOPTS="-P 27.50")
UBXTOOL = "/usr/bin/ubxtool"
GPSD = ("127.0.0.1", 2947)
UBXTOOL_TIMEOUT_S = 10      # a poll normally takes 2 s
IDENTIFY_WAIT_S = 20.0      # after the version request: how long to wait for gpsd to identify the receiver
# Without -p gpsd writes its configuration in the same step as it reads the version reply, before
# the DEVICES report shows `subtype`: this only leaves the receiver time to act on it.
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


def parse_valget(text, layer):
    """{key: value} from ubxtool's VALGET reply for `layer`. A key not stored in that layer is left
    out of the reply ({} if the receiver refused the read because none is); None if no reply."""
    values, current, refused = None, False, False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        after = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("UBX-"):
            current = line.startswith("UBX-CFG-VALGET") and bool(re.match(rf"\s*version \d+ layer {layer}\b", after))
            if current and values is None:
                values = {}
            refused = refused or (line.startswith("UBX-ACK-NAK") and "(VALGET)" in after)
        elif current:
            m = re.search(r"item (\S+)/0x[0-9a-fA-F]+ val (-?\d+)", line)
            if m:
                values[m.group(1)] = int(m.group(2))
    return values if values is not None else ({} if refused else None)


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
    # gpsd identifies the receiver from the version reply, whoever asked for it.
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


def correct(key, want, now, apply):
    """Read `key` in RAM (already done: `now`), write `want` in RAM if it differs; True if it is `want` at the end."""
    print(f"{key} in RAM: {'no answer from the receiver' if now is None else now}", flush=True)
    if now == want or not apply:
        return now == want
    print(f"setting {key} to {want} in RAM", flush=True)
    ubxtool("-z", f"{key},{want},1")
    now = read(key)
    print(f"{key} in RAM now: {'no answer from the receiver' if now is None else now}", flush=True)
    return now == want


def layers(keys):
    """{key: [RAM, battery-backed, flash]}: a value, NOT_STORED, or None (no answer) in each layer."""
    table = {key: [] for key in keys}
    for layer in range(3):
        for _ in range(2):          # asked once more if the reply was lost
            got = parse_valget(ubxtool(*[a for key in keys for a in ("-g", f"{key},{layer}")]), layer)
            if got is not None:
                break
        for key in keys:
            table[key].append(None if got is None else got.get(key, NOT_STORED if layer else UNKNOWN))
    return table


def gpsd_args():
    """The running gpsd's command line as a list ([] if none is found): /proc, which is never cut short."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != "gpsd":
                    continue
            with open(f"/proc/{pid}/cmdline") as f:
                return f.read().rstrip("\0").split("\0")
        except OSError:
            continue
    return []


def show(key, values, now=""):
    print(f"{key}{now}: " + ", ".join(f"{name} {'no answer' if v is None else v}"
                                      for name, v in zip(LAYERS, values)), flush=True)


def rawx_every():
    """(manager.gps_log.rawx_every, "") from the merged config, or (None, why not)."""
    code = ("import json; from radar_runner.common import config as C\n"
            "c, w = C.load(secrets_file=None); print(json.dumps(c['manager']['gps_log']['rawx_every']))")
    try:
        out = subprocess.run([VENV_PYTHON, "-B", "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
        value = json.loads(out.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return None, f"cannot read manager.gps_log.rawx_every with {VENV_PYTHON}: {e}"
    if type(value) is not int or not 0 <= value <= 255:
        return None, f"manager.gps_log.rawx_every is {value!r}: it must be a whole number from 0 to 255"
    return value, ""


def wanted():
    """The settings to provision ({key: value}), or None with the reason printed."""
    every, why = rawx_every()
    if why:
        print(why, flush=True)
        return None
    return dict(SETTINGS, **{RAWX: every, SFRBX: 1 if every else 0})     # 0: no raw messages


def provision(apply):
    """Check the settings in the three layers and, with `apply`, write the wrong ones to all three; True if all are right."""
    settings = wanted()
    if settings is None:
        return False
    args = gpsd_args()
    if apply and args and "-p" not in args[1:]:
        print(f"warning: gpsd is not running passive (-p): at its next start it sets part of this again "
              f"({' '.join(args)}; setup/setup.sh installs it with -p)", flush=True)
    table = layers(settings)
    for key, values in table.items():
        show(key, values)
    wrong = {key: settings[key] for key, values in table.items() if any(v != settings[key] for v in values)}
    if not wrong:
        print(f"all {len(settings)} settings are right in RAM, battery-backed memory and flash", flush=True)
        return True
    if any(values[0] == UNKNOWN for values in table.values()):
        print("the receiver refused the read of RAM, which holds every setting it knows: a setting unknown to "
              "its firmware" + (": nothing written" if apply else ""), flush=True)
        return False
    if any(None in values for values in table.values()):
        print("the receiver did not answer every read" + (": nothing written, run again" if apply else ""), flush=True)
        return False
    if not apply:
        return False
    print("setting in RAM, battery-backed memory and flash: "
          + ", ".join(f"{key} {value}" for key, value in wrong.items()), flush=True)
    ubxtool(*[a for key, value in wrong.items() for a in ("-z", f"{key},{value}")])    # no layer given: all three
    table = layers(wrong)
    for key, values in table.items():
        show(key, values, " now")
    return all(v == settings[key] for key, values in table.items() for v in values)


PORT_RE = re.compile(r"Port: \S+ \((\w+)\)\s+txPending (\d+) txBytes (\d+) txUsage (\d+) txPeakUsage (\d+)")
COMMS_GAP_S = 10.0


def parse_comms(text):
    """(txErrors or None, {port: (txBytes, txUsage %, txPeakUsage %)}) from ubxtool's MON-COMMS decode."""
    m = re.search(r"txErrors x([0-9a-fA-F]+)", text)
    ports = {p: (int(b), int(u), int(k)) for p, _, b, u, k in PORT_RE.findall(text)}
    return (int(m.group(1), 16) if m else None), ports


def comms():
    """Report the port statistics twice, COMMS_GAP_S apart; 0 if read with no error flags, 1 if not."""
    polls = []
    for n in range(2):
        if n:
            time.sleep(COMMS_GAP_S)
        for _ in range(2):          # asked once more if the reply was lost
            errors, ports = parse_comms(ubxtool("-p", "MON-COMMS"))
            if errors is not None:
                break
        polls.append((time.monotonic(), errors, ports))
        if errors is None:
            print("no MON-COMMS reply from the receiver", flush=True)
            return 1
    (t0, _, first), (t1, errors, last) = polls
    for port, (sent, usage, peak) in last.items():
        rate = (sent - first[port][0]) / (t1 - t0) if port in first and sent >= first[port][0] else None
        print(f"{port}: transmit buffer {usage} % (highest in the last period), peak {peak} % (since the "
              f"receiver started)" + (f", {rate:.0f} bytes/s" if rate is not None else ""), flush=True)
    flags = [name for bit, name in ((0x40, "memory"), (0x80, "allocation")) if errors & bit]
    print(f"txErrors 0x{errors:02x}" + (f": {' and '.join(flags)} errors (output lost)" if flags else ": none"),
          flush=True)
    return 1 if flags else 0


def main(argv):
    if set(argv) - {"--apply", "--ram", "--comms"} or ("--comms" in argv and len(argv) > 1):
        print(__doc__, file=sys.stderr)
        return 4
    if "--comms" in argv:
        return comms()
    apply, ram_only = "--apply" in argv, "--ram" in argv
    status = identify(apply)
    if status == 3:
        return 3
    if not ram_only:
        settings_ok = provision(apply)
        return status or (0 if settings_ok else 1)
    # Also without identification: if gpsd has read a NAV-PVT already, it keeps it on now; if not
    # (only without -p), it turns it off once and the next run sets it again.
    pvt_ok = correct(PVT, 1, read(PVT), apply)
    # gpsd's configuration (without -p) is what set this one back to 1, so it is checked last.
    nmea_ok = correct(KEY, 0, read(KEY), apply)
    return status or (0 if pvt_ok and nmea_ok else 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
