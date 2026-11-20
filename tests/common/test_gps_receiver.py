"""setup/gps_receiver.py against a stand-in gpsd (a real TCP socket) and a stand-in ubxtool."""
import importlib.util
import json
import os
import socket
import subprocess
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNIDENTIFIED = {"class": "DEVICE", "path": "/dev/ttyACM0", "driver": "u-blox", "subtype": "", "activated": "x",
                "flags": 1, "native": 1}
IDENTIFIED = dict(UNIDENTIFIED, subtype="SW EXT CORE 1.00 (9e1716),HW 00190000",
                  subtype1="ROM BASE 0x118B2060,FWVER=HPG 1.51,PROTVER=27.50,MOD=ZED-F9P")
NMEA, PVT = "CFG-UART1OUTPROT-NMEA", "CFG-MSGOUT-UBX_NAV_PVT_USB"
SIG = "CFG-MSGOUT-UBX_NAV_SIG_USB"
READ_NMEA, READ_PVT = ["-g", f"{NMEA},0"], ["-g", f"{PVT},0"]
WANT = {f"CFG-MSGOUT-UBX_NAV_{m}_USB": 1 for m in ("PVT", "SAT", "SIG", "EOE", "POSECEF", "VELECEF", "DOP", "TIMEGPS")}
WANT.update({"CFG-USBOUTPROT-UBX": 1, NMEA: 0})


def read_layer(layer, keys=WANT):
    return [a for key in keys for a in ("-g", f"{key},{layer}")]


READ_ALL = [read_layer(0), read_layer(1), read_layer(2)]

# The receiver's values are files in `state` named after the key (RAM) and the key with "@1"
# (battery-backed) or "@2" (flash). No file in those two: not stored there, left out of the reply,
# and the read refused (ACK-NAK) if none is stored. A value "-" means that the reply to a read of
# that layer is lost (no answer), as in the overflow state. "-z KEY,VALUE" writes all three layers,
# "-z KEY,VALUE,1" RAM only, as ubxtool does.
UBXTOOL_STAND_IN = """#!/usr/bin/python3
import json, os, sys
state = STATE
with open(os.path.join(state, "calls"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
items = [args[i + 1].split(",") for i, a in enumerate(args) if a in ("-z", "-g")]
def path(key, layer):
    return os.path.join(state, key + ("" if layer == 0 else "@%d" % layer))
if "-z" in args:
    for key, value, *layer in items:
        for n in ([0] if layer == ["1"] else [0, 1, 2]):
            open(path(key, n), "w").write(value)
elif "-g" in args:
    layer = int(items[-1][1])
    print("layer %d position 0 end None " % layer)
    values = {key: open(path(key, layer)).read() for key, *_ in items if os.path.exists(path(key, layer))}
    if "-" in values.values():
        pass
    elif values:
        print("UBX-CFG-VALGET:\\n version 1 layer %d position 0\\n  layers (%s)" % (layer, ["ram", "bbr", "flash"][layer]))
        for key, value in values.items():
            print("    item %s/0x10740002 val %s" % (key, value))
        print("UBX-ACK-ACK:\\n  ACK to Class x06 (CFG) ID x8b (VALGET)")
    else:
        print("UBX-ACK-NAK:\\n  NAK to Class x06 (CFG) ID x8b (VALGET)")
    print("UBX-NAV-PVT:\\n  iTOW 445152900 time 2026/11/20 03:38:54 valid x37")
"""


class FakeGpsd:
    """Answers ?DEVICES; as gpsd 3.25 does. `identify_after_monver`: the receiver is identified once
    the stand-in ubxtool has been asked for MON-VER (gpsd sees the reply). `silent`: accepts the
    connection and never answers."""

    def __init__(self, calls, devices, identify_after_monver=False, silent=False):
        self.calls, self.devices, self.identify_after_monver, self.silent = calls, devices, identify_after_monver, silent
        self.questions = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.held = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _devices(self):
        asked = self.calls.exists() and "MON-VER" in self.calls.read_text()
        if self.identify_after_monver and asked:
            return [IDENTIFIED]
        return self.devices

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            if self.silent:
                self.held.append(conn)
                continue
            with conn:
                conn.sendall(b'{"class":"VERSION","release":"3.25","rev":"3.25","proto_major":3,"proto_minor":15}\r\n')
                line = conn.makefile("rb").readline()
                self.questions.append(line)
                if line.startswith(b"?DEVICES;"):
                    reply = {"class": "DEVICES", "devices": self._devices()}
                    conn.sendall(json.dumps(reply, separators=(",", ":")).encode() + b"\r\n")

    def close(self):
        try:
            self.server.shutdown(socket.SHUT_RDWR)      # wakes the accept, which close alone does not
        except OSError:
            pass
        self.server.close()
        for conn in self.held:
            conn.close()


@pytest.fixture
def tool(tmp_path):
    spec = importlib.util.spec_from_file_location("gps_receiver", os.path.join(REPO, "setup", "gps_receiver.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "receiver"
    state.mkdir()
    stand_in = tmp_path / "ubxtool"
    stand_in.write_text(UBXTOOL_STAND_IN.replace("STATE", repr(str(state))))
    stand_in.chmod(0o755)
    module.UBXTOOL = str(stand_in)
    module.IDENTIFY_WAIT_S, module.CONFIGURE_WAIT_S, module.POLL_S = 1.5, 0.2, 0.1
    module.calls_file = state / "calls"

    def stored(key, layer, value=None):
        """The key's value in a layer (None: not stored); `value` sets it, or "" removes it."""
        path = state / (key + ("" if layer == 0 else f"@{layer}"))
        if value == "":
            path.unlink()
        elif value is not None:
            path.write_text(str(value))
        return path.read_text() if path.exists() else None
    module.stored = stored
    module.ram = lambda key, value=None: stored(key, 0, value)
    module.all_layers = lambda key: [stored(key, layer) for layer in range(3)]
    for key, value in WANT.items():          # a provisioned receiver
        for layer in range(3):
            stored(key, layer, value)
    servers = []

    def gpsd(devices, **kw):
        servers.append(FakeGpsd(module.calls_file, devices, **kw))
        module.GPSD = ("127.0.0.1", servers[-1].port)
        return servers[-1]
    module.gpsd = gpsd
    yield module
    for server in servers:
        server.close()


def calls(tool):
    return [json.loads(line) for line in tool.calls_file.read_text().splitlines()] if tool.calls_file.exists() else []


def test_already_identified_no_version_request(tool, capsys):
    gpsd = tool.gpsd([IDENTIFIED])
    assert tool.main([]) == 0
    assert tool.main(["--apply", "--ram"]) == 0
    assert calls(tool) == READ_ALL + [READ_PVT, READ_NMEA]
    out = capsys.readouterr().out
    assert "gpsd has identified the receiver: /dev/ttyACM0: ROM BASE" in out and "MON-VER" not in out
    assert f"{PVT}: RAM 1, battery-backed 1, flash 1\n" in out and f"{NMEA}: RAM 0, battery-backed 0, flash 0\n" in out
    assert "all 10 settings are right in RAM, battery-backed memory and flash" in out
    assert f"{PVT} in RAM: 1" in out and f"{NMEA} in RAM: 0" in out
    assert set(gpsd.questions) == {b"?DEVICES;\n"}


def test_provisioning_with_everything_right_writes_nothing(tool, capsys):
    tool.gpsd([IDENTIFIED])
    assert tool.main(["--apply"]) == 0
    assert calls(tool) == READ_ALL                          # one read per layer, no write
    out = capsys.readouterr().out
    assert out.count(": RAM 1, battery-backed 1, flash 1\n") == 9 and "setting in" not in out


def test_provisioning_writes_a_message_off_in_flash_only_to_all_three_layers(tool, capsys):
    tool.gpsd([IDENTIFIED])
    tool.stored(SIG, 2, 0)                                  # as the OLA's save would leave a gpsd switch-off
    assert tool.main([]) == 1                               # read only: reported, not changed
    assert tool.all_layers(SIG) == ["1", "1", "0"] and [c for c in calls(tool) if "-z" in c] == []
    tool.calls_file.unlink()
    assert tool.main(["--apply"]) == 0
    assert tool.all_layers(SIG) == ["1", "1", "1"]
    # No layer given: ubxtool writes RAM, battery-backed memory and flash. Then only that key is read back.
    assert calls(tool) == READ_ALL + [["-z", f"{SIG},1"]] + [read_layer(n, [SIG]) for n in range(3)]
    out = capsys.readouterr().out
    assert f"{SIG}: RAM 1, battery-backed 1, flash 0\n" in out
    assert f"setting in RAM, battery-backed memory and flash: {SIG} 1\n" in out
    assert f"{SIG} now: RAM 1, battery-backed 1, flash 1\n" in out


def test_provisioning_turns_uart1_nmea_off_when_on_in_flash_or_not_stored(tool, capsys):
    """RAM alone looked right on 2026-11-19 while the stored layers were not: all three are read."""
    tool.gpsd([IDENTIFIED])
    tool.stored(NMEA, 2, 1)
    tool.stored(PVT, 1, "")                                 # not stored: the default (0) applies at power-up
    assert tool.main(["--apply", "--ram"]) == 0             # the boot-time run looks at RAM only
    assert tool.main(["--apply"]) == 0
    assert tool.all_layers(NMEA) == ["0", "0", "0"] and tool.all_layers(PVT) == ["1", "1", "1"]
    assert [c for c in calls(tool) if "-z" in c] == [["-z", f"{PVT},1", "-z", f"{NMEA},0"]]
    out = capsys.readouterr().out
    assert f"{PVT}: RAM 1, battery-backed not stored, flash 1\n" in out
    assert f"{NMEA}: RAM 0, battery-backed 0, flash 1\n" in out
    assert f"{PVT} now: RAM 1, battery-backed 1, flash 1\n" in out and f"{NMEA} now: RAM 0, battery-backed 0, flash 0\n" in out


def test_provisioning_a_receiver_at_factory_settings(tool, capsys):
    """Nothing stored in battery-backed memory or flash: the read of those layers is refused (ACK-NAK)."""
    tool.gpsd([IDENTIFIED])
    for key in WANT:
        tool.stored(key, 1, "")
        tool.stored(key, 2, "")
    tool.ram(NMEA, 1)
    tool.ram("CFG-MSGOUT-UBX_NAV_EOE_USB", 0)
    assert tool.main(["--apply"]) == 0
    assert all(tool.all_layers(key) == [str(value)] * 3 for key, value in WANT.items())
    out = capsys.readouterr().out
    assert f"{NMEA}: RAM 1, battery-backed not stored, flash not stored\n" in out
    assert out.count(" now: RAM 1, battery-backed 1, flash 1\n") == 9


def test_provisioning_writes_nothing_without_an_answer(tool, capsys):
    tool.gpsd([IDENTIFIED])
    tool.stored(SIG, 2, 0)
    tool.stored(PVT, 2, "-")                                # every reply for flash is lost
    started = time.monotonic()
    assert tool.main(["--apply"]) == 1
    assert time.monotonic() - started < 10.0
    assert calls(tool) == READ_ALL + [read_layer(2)]       # flash asked once more, then nothing written
    out = capsys.readouterr().out
    assert f"{SIG}: RAM 1, battery-backed 1, flash no answer\n" in out
    assert "the receiver did not answer every read: nothing written, run again" in out


def test_unidentified_then_identified_after_the_request(tool, capsys):
    tool.gpsd([UNIDENTIFIED], identify_after_monver=True)
    tool.ram(NMEA, 1)                       # what gpsd's configuration leaves in RAM
    assert tool.main(["--apply", "--ram"]) == 0
    # The version request first, then NAV-PVT, then UART1 (gpsd's configuration sets that one).
    assert calls(tool) == [["-p", "MON-VER"], READ_PVT, READ_NMEA, ["-z", f"{NMEA},0,1"], READ_NMEA]
    out = capsys.readouterr().out
    assert "gpsd has NOT identified the receiver: /dev/ttyACM0: not identified (driver u-blox)" in out
    assert "gpsd has identified the receiver 0 s after the request" in out
    assert f"{NMEA} in RAM now: 0" in out


def test_never_identified_ends_in_bounded_time_with_status_2(tool, capsys):
    tool.gpsd([UNIDENTIFIED])
    tool.ram(NMEA, 1)
    tool.ram(PVT, 0)
    started = time.monotonic()
    assert tool.main(["--apply", "--ram"]) == 2
    took = time.monotonic() - started
    assert tool.IDENTIFY_WAIT_S <= took < tool.IDENTIFY_WAIT_S + 2.0
    # The settings are still checked and corrected; the next run checks them again.
    assert calls(tool) == [["-p", "MON-VER"], READ_PVT, ["-z", f"{PVT},1,1"], READ_PVT,
                           READ_NMEA, ["-z", f"{NMEA},0,1"], READ_NMEA]
    out = capsys.readouterr().out
    assert "gpsd has still not identified the receiver" in out and "not identified (driver u-blox)" in out


def test_identified_without_position_message_gets_nav_pvt_in_ram_only(tool, capsys):
    """gpsd identified, but NAV-PVT is off in RAM: no version request helps, the RAM write does."""
    tool.gpsd([IDENTIFIED])
    tool.ram(PVT, 0)
    assert tool.main(["--ram"]) == 1                        # read only: reported, not changed
    assert tool.ram(PVT) == "0"
    assert tool.main(["--apply", "--ram"]) == 0
    assert tool.all_layers(PVT) == ["1", "1", "1"]
    assert calls(tool) == [READ_PVT, READ_NMEA, READ_PVT, ["-z", f"{PVT},1,1"], READ_PVT, READ_NMEA]
    out = capsys.readouterr().out
    assert f"setting {PVT} to 1 in RAM\n" in out and f"{PVT} in RAM now: 1" in out
    assert "MON-VER" not in out


def test_no_answer_is_written_anyway_in_ram(tool, capsys):
    """Right after gpsd's configuration the output can overflow and a reply be lost: write, then read back."""
    tool.gpsd([IDENTIFIED])
    tool.ram(NMEA, "-")
    tool.ram(PVT, "-")
    assert tool.main(["--apply", "--ram"]) == 0             # written, and the read-back answers
    assert tool.ram(NMEA) == "0" and tool.ram(PVT) == "1"
    assert calls(tool) == [READ_PVT, ["-z", f"{PVT},1,1"], READ_PVT, READ_NMEA, ["-z", f"{NMEA},0,1"], READ_NMEA]
    out = capsys.readouterr().out
    assert f"{NMEA} in RAM: no answer from the receiver" in out and f"{NMEA} in RAM now: 0" in out
    # Without --ram (all layers, at setup) nothing is written on a guess.
    tool.ram(NMEA, "-")
    tool.calls_file.unlink()
    assert tool.main(["--apply"]) == 1
    assert [c for c in calls(tool) if "-z" in c] == []


def test_plain_read_reports_but_asks_nothing_new(tool, capsys):
    tool.gpsd([UNIDENTIFIED])
    tool.stored(SIG, 2, 0)
    assert tool.main([]) == 2
    assert calls(tool) == READ_ALL                          # reads only: no version request, no write
    assert "NOT identified" in capsys.readouterr().out


def test_gpsd_not_reachable_without_receiver_or_silent(tool, capsys):
    gpsd = tool.gpsd([])
    assert tool.main(["--apply", "--ram"]) == 3
    assert "gpsd has no receiver" in capsys.readouterr().out
    gpsd.close()                            # nothing listens there now
    assert tool.main(["--apply", "--ram"]) == 3
    assert "cannot ask gpsd at 127.0.0.1:" in capsys.readouterr().out
    tool.gpsd([IDENTIFIED], silent=True)    # accepts the connection, never answers
    started = time.monotonic()
    assert tool.main(["--apply", "--ram"]) == 3
    assert time.monotonic() - started < 5.0
    assert "cannot ask gpsd at 127.0.0.1:" in capsys.readouterr().out
    assert calls(tool) == []


def test_setting_not_corrected_is_status_1(tool):
    tool.gpsd([IDENTIFIED])
    tool.ram(NMEA, 1)
    assert tool.main([]) == 1               # read only: reported, not changed
    assert tool.ram(NMEA) == "1"
    tool.UBXTOOL = "/nonexistent/ubxtool"
    assert tool.main(["--apply", "--ram"]) == 1


def test_wrong_options_have_their_own_status():
    r = subprocess.run(["/usr/bin/python3", "-B", "-E", "-s", os.path.join(REPO, "setup", "gps_receiver.py"), "--fix"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 4 and "Exit status" in r.stderr and r.stdout == ""
