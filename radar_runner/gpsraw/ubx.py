"""UBX framing and the few message fields the raw log needs. Standard library only.

The u-blox binary protocol: 0xB5 0x62, class, id, payload length (2 bytes,
little-endian), payload, two checksum bytes (8-bit Fletcher over class to the
end of the payload).

`Framer` cuts a byte stream from gpsd's raw pass-through into whole messages.
gpsd passes on only packets that its own reader accepted, so the stream is a
sequence of complete UBX messages, NMEA sentences and gpsd's own JSON lines;
`Framer` keeps the UBX messages and skips everything else. It does not compute
checksums (that would cost more processor time than everything else here);
`scan`, for files afterwards, does.
"""
import struct

SYNC = b"\xb5\x62"
MAX_PAYLOAD = 8192          # RXM-RAWX with 255 measurements is 8176 bytes

NAMES = {(0x01, 0x07): "NAV-PVT", (0x01, 0x20): "NAV-TIMEGPS", (0x01, 0x26): "NAV-TIMELS",
         (0x01, 0x61): "NAV-EOE", (0x01, 0x35): "NAV-SAT", (0x01, 0x43): "NAV-SIG", (0x01, 0x04): "NAV-DOP",
         (0x01, 0x01): "NAV-POSECEF", (0x01, 0x11): "NAV-VELECEF",
         (0x02, 0x15): "RXM-RAWX", (0x02, 0x13): "RXM-SFRBX",
         (0x0a, 0x04): "MON-VER", (0x0a, 0x36): "MON-COMMS", (0x0a, 0x38): "MON-RF", (0x0a, 0x31): "MON-SPAN",
         (0x05, 0x01): "ACK-ACK", (0x05, 0x00): "ACK-NAK", (0x06, 0x8b): "CFG-VALGET"}
IDS = {name: key for key, name in NAMES.items()}
RAWX, SFRBX = IDS["RXM-RAWX"], IDS["RXM-SFRBX"]
GNSS = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 5: "QZSS", 6: "GLONASS"}
CP_VALID = 0x02             # RXM-RAWX trkStat: carrier phase valid
GPS_EPOCH_UNIX = 315964800  # 1980-01-06T00:00:00Z


def name(cls, msg_id):
    return NAMES.get((cls, msg_id), f"0x{cls:02x}-0x{msg_id:02x}")


def checksum(body):
    """The two checksum bytes for class, id, length and payload."""
    a = b = 0
    for byte in body:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def frame(cls, msg_id, payload=b""):
    """One complete UBX message."""
    body = struct.pack("<BBH", cls, msg_id, len(payload)) + payload
    return SYNC + body + checksum(body)


def rawx_summary(payload):
    """(rcvTow s, week, leapS or None, measurements, with carrier phase valid) of an RXM-RAWX payload, or None."""
    if len(payload) < 16:
        return None
    tow, week, leap, count, rec_stat = struct.unpack_from("<dHbBB", payload, 0)
    count = min(count, (len(payload) - 16) // 32)
    cp = sum(1 for i in range(count) if payload[16 + 32 * i + 30] & CP_VALID)
    return tow, week, leap if rec_stat & 0x01 else None, count, cp


def gps_to_unix(week, tow, leap):
    """UTC as Unix time from GPS week and time of week; `leap` = GPS - UTC in seconds."""
    return GPS_EPOCH_UNIX + week * 604800 + tow - leap


class Framer:
    """Cuts whole UBX messages out of a byte stream; everything between them is skipped and counted."""

    def __init__(self):
        self._buf = b""
        self.skipped = 0            # bytes that were not part of a UBX message

    def feed(self, data):
        """Returns a list of (class, id, message bytes) for every message completed by `data`."""
        buf = self._buf + data if self._buf else data
        out = []
        pos, end = 0, len(buf)
        while True:
            start = buf.find(SYNC, pos)
            if start < 0:
                keep = end - 1 if end > pos and buf[end - 1] == 0xB5 else end   # a sync byte cut in two
                self.skipped += keep - pos
                pos = keep
                break
            self.skipped += start - pos
            if end - start < 6:
                pos = start
                break
            length = buf[start + 4] | buf[start + 5] << 8
            if length > MAX_PAYLOAD:
                self.skipped += 2
                pos = start + 2
                continue
            stop = start + 8 + length
            if stop > end:
                pos = start
                break
            out.append((buf[start + 2], buf[start + 3], buf[start:stop]))
            pos = stop
        self._buf = buf[pos:]
        return out

    @property
    def pending(self):
        """Bytes held back: the start of a message that is not complete yet."""
        return len(self._buf)


def scan(path, block=1 << 20):
    """Read a raw log and count what is in it, with checksums. Returns a dict (see `check` in __main__.py).

    A message whose length runs past the end of the file is the cut-off last one, unless good
    messages follow it: then its header was damaged, and the search goes on after its sync bytes.
    """
    counts, bad, junk = {}, 0, 0
    epochs = meas = cp = 0
    first = last = None
    systems = {}
    buf = b""
    tail = None                     # at the end of the file: (start of a message that does not fit, junk then)
    with open(path, "rb") as f:
        while True:
            data = f.read(block)
            at_end = not data
            buf = buf + data if buf else data
            pos, end = 0, len(buf)
            while True:
                start = buf.find(SYNC, pos)
                if start < 0:
                    keep = end - 1 if end > pos and buf[end - 1] == 0xB5 and not at_end else end
                    junk += keep - pos
                    pos = keep
                    break
                junk += start - pos
                if end - start < 6 and not at_end:
                    pos = start
                    break
                length = buf[start + 4] | buf[start + 5] << 8 if end - start >= 6 else MAX_PAYLOAD + 1
                stop = start + 8 + length
                if end - start >= 6 and length > MAX_PAYLOAD:
                    junk += 1
                    pos = start + 1
                    continue
                if stop > end:
                    if not at_end:
                        pos = start
                        break
                    if tail is None:
                        tail = (start, junk)
                    junk += 1
                    pos = start + 1
                    continue
                if checksum(buf[start + 2:stop - 2]) != buf[stop - 2:stop]:
                    bad += 1
                    junk += 1
                    pos = start + 1
                    continue
                tail = None
                key = name(buf[start + 2], buf[start + 3])
                counts[key] = counts.get(key, 0) + 1
                if (buf[start + 2], buf[start + 3]) == RAWX:
                    payload = buf[start + 6:stop - 2]
                    info = rawx_summary(payload)
                    if info is not None:
                        epochs += 1
                        meas += info[3]
                        cp += info[4]
                        if first is None:
                            first = info
                        last = info
                        for i in range(info[3]):
                            o = 16 + 32 * i
                            gnss = GNSS.get(payload[o + 20], str(payload[o + 20]))
                            entry = systems.setdefault(gnss, [0, 0])
                            entry[0] += 1
                            entry[1] += 1 if payload[o + 30] & CP_VALID else 0
                pos = stop
            if at_end:
                break
            buf = buf[pos:]
    partial = 0
    if tail is not None:
        partial, junk = len(buf) - tail[0], tail[1]
    return {"messages": counts, "bad_checksums": bad, "skipped_bytes": junk, "partial_last_bytes": partial,
            "rawx_epochs": epochs, "measurements": meas, "carrier_phase_valid": cp,
            "by_system": {k: {"measurements": v[0], "carrier_phase_valid": v[1]} for k, v in sorted(systems.items())},
            "first_epoch": None if first is None else epoch_time(first[1], first[0], first[2]),
            "last_epoch": None if last is None else epoch_time(last[1], last[0], last[2])}


def utc_text(t):
    """Unix time as ISO text to the millisecond, or None if it is not a usable number."""
    import math
    import time
    if not isinstance(t, (int, float)) or not math.isfinite(t):
        return None
    try:
        seconds, ms = divmod(int(round(t * 1000)), 1000)
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{ms:03d}Z"
    except (OverflowError, OSError, ValueError):
        return None


def epoch_time(week, tow, leap):
    """{week, tow_s, leap_s, utc} of a receiver epoch; tow_s and utc are None if tow is not finite."""
    import math
    finite = isinstance(tow, (int, float)) and math.isfinite(tow)
    return {"week": week, "tow_s": round(tow, 3) if finite else None, "leap_s": leap,
            "utc": utc_text(gps_to_unix(week, tow, leap)) if finite and leap is not None else None}
