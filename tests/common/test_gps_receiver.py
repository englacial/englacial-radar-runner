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
READ_NMEA, READ_PVT = ["-g", f"{NMEA},0"], ["-g", f"{PVT},0"]

# The receiver's RAM values are files named after the key in `state`; a value "-" means that the
# reply to a read is lost (no answer), as in the overflow state.
UBXTOOL_STAND_IN = """#!/usr/bin/python3
import json, os, sys
state = {state!r}
with open(os.path.join(state, "calls"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "-z" in args:
    key, value = args[args.index("-z") + 1].split(",")[:2]
    open(os.path.join(state, key), "w").write(value)
elif "-g" in args:
    key = args[args.index("-g") + 1].split(",")[0]
    value = open(os.path.join(state, key)).read()
    if value != "-":
        print("UBX-CFG-VALGET:\\n version 1 layer 0 position 0\\n  layers (ram)\\n"
              f"    item {{key}}/0x10740002 val {{value}}")
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
    stand_in.write_text(UBXTOOL_STAND_IN.format(state=str(state)))
    stand_in.chmod(0o755)
    module.UBXTOOL = str(stand_in)
    module.IDENTIFY_WAIT_S, module.CONFIGURE_WAIT_S, module.POLL_S = 1.5, 0.2, 0.1
    module.calls_file = state / "calls"

    def ram(key, value=None):
        if value is not None:
            (state / key).write_text(str(value))
        return (state / key).read_text()
    module.ram = ram
    ram(NMEA, 0)
    ram(PVT, 1)
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
    assert calls(tool) == [READ_PVT, READ_NMEA] * 2
    out = capsys.readouterr().out
    assert "gpsd has identified the receiver: /dev/ttyACM0: ROM BASE" in out and "MON-VER" not in out
    assert f"{PVT} in RAM: 1" in out and f"{NMEA} in RAM: 0" in out
    assert set(gpsd.questions) == {b"?DEVICES;\n"}


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
    """gpsd identified, but its enable of NAV-PVT was lost: no version request helps, the RAM write does."""
    tool.gpsd([IDENTIFIED])
    tool.ram(PVT, 0)
    assert tool.main([]) == 1                               # read only: reported, not changed
    assert tool.ram(PVT) == "0"
    assert tool.main(["--apply"]) == 0                      # also with --apply alone: RAM only
    assert tool.ram(PVT) == "1"
    writes = [c for c in calls(tool) if "-z" in c]
    assert writes == [["-z", f"{PVT},1,1"]]                 # layer 1 = RAM; never battery-backed or flash
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
    # Without --ram (all layers, by hand) nothing is written on a guess.
    tool.ram(NMEA, "-")
    tool.calls_file.unlink()
    assert tool.main(["--apply"]) == 1
    assert [c for c in calls(tool) if "-z" in c] == []


def test_plain_read_reports_but_asks_nothing_new(tool, capsys):
    tool.gpsd([UNIDENTIFIED])
    assert tool.main([]) == 2
    assert calls(tool) == [READ_PVT, READ_NMEA]
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
