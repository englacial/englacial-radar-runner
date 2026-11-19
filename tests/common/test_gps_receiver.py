"""setup/gps_receiver.py against a stand-in gpsd (a real TCP socket) and a stand-in ubxtool."""
import importlib.util
import json
import os
import socket
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNIDENTIFIED = {"class": "DEVICE", "path": "/dev/ttyACM0", "driver": "u-blox", "subtype": "", "activated": "x",
                "flags": 1, "native": 1}
IDENTIFIED = dict(UNIDENTIFIED, subtype="SW EXT CORE 1.00 (9e1716),HW 00190000",
                  subtype1="ROM BASE 0x118B2060,FWVER=HPG 1.51,PROTVER=27.50,MOD=ZED-F9P")

UBXTOOL_STAND_IN = """#!/usr/bin/python3
import json, sys
with open({log!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "-z" in args:
    open({value!r}, "w").write(args[args.index("-z") + 1].split(",")[1])
elif "-g" in args:
    print("UBX-CFG-VALGET:\\n version 1 layer 0 position 0\\n  layers (ram)\\n"
          "    item CFG-UART1OUTPROT-NMEA/0x10740002 val " + open({value!r}).read())
"""


class FakeGpsd:
    """Answers ?DEVICES; as gpsd 3.25 does. `identify_after_monver`: the receiver is identified once
    the stand-in ubxtool has been asked for MON-VER (gpsd sees the reply)."""

    def __init__(self, log, devices, identify_after_monver=False):
        self.log, self.devices, self.identify_after_monver = log, devices, identify_after_monver
        self.questions = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _devices(self):
        asked = self.log.exists() and "MON-VER" in self.log.read_text()
        if self.identify_after_monver and asked:
            return [IDENTIFIED]
        return self.devices

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
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


@pytest.fixture
def tool(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gps_receiver", os.path.join(REPO, "setup", "gps_receiver.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    log, value = tmp_path / "ubxtool.log", tmp_path / "value"
    value.write_text("0")
    stand_in = tmp_path / "ubxtool"
    stand_in.write_text(UBXTOOL_STAND_IN.format(log=str(log), value=str(value)))
    stand_in.chmod(0o755)
    module.UBXTOOL = str(stand_in)
    module.IDENTIFY_WAIT_S, module.CONFIGURE_WAIT_S, module.POLL_S = 1.5, 0.2, 0.1
    module.log, module.value = log, value
    servers = []

    def gpsd(devices, **kw):
        servers.append(FakeGpsd(log, devices, **kw))
        module.GPSD = ("127.0.0.1", servers[-1].port)
        return servers[-1]
    module.gpsd = gpsd
    yield module
    for server in servers:
        server.close()


def calls(tool):
    return [json.loads(line) for line in tool.log.read_text().splitlines()] if tool.log.exists() else []


def test_already_identified_no_version_request(tool, capsys):
    gpsd = tool.gpsd([IDENTIFIED])
    assert tool.main([]) == 0
    assert tool.main(["--apply", "--ram"]) == 0
    assert calls(tool) == [["-g", "CFG-UART1OUTPROT-NMEA,0"]] * 2
    out = capsys.readouterr().out
    assert "gpsd has identified the receiver: /dev/ttyACM0: ROM BASE" in out and "MON-VER" not in out
    assert set(gpsd.questions) == {b"?DEVICES;\n"}


def test_unidentified_then_identified_after_the_request(tool, capsys):
    tool.gpsd([UNIDENTIFIED], identify_after_monver=True)
    tool.value.write_text("1")              # what gpsd's configuration leaves in RAM
    assert tool.main(["--apply", "--ram"]) == 0
    assert calls(tool) == [["-p", "MON-VER"], ["-g", "CFG-UART1OUTPROT-NMEA,0"], ["-z", "CFG-UART1OUTPROT-NMEA,0,1"],
                           ["-g", "CFG-UART1OUTPROT-NMEA,0"]]
    out = capsys.readouterr().out
    assert "gpsd has NOT identified the receiver: /dev/ttyACM0: not identified (driver u-blox)" in out
    assert "gpsd has identified the receiver 0 s after the request" in out
    assert "CFG-UART1OUTPROT-NMEA in RAM now: 0" in out


def test_never_identified_ends_in_bounded_time_with_status_2(tool, capsys):
    tool.gpsd([UNIDENTIFIED])
    tool.value.write_text("1")
    started = time.monotonic()
    assert tool.main(["--apply", "--ram"]) == 2
    took = time.monotonic() - started
    assert tool.IDENTIFY_WAIT_S <= took < tool.IDENTIFY_WAIT_S + 2.0
    # The setting is still checked and corrected; the next run checks it again.
    assert calls(tool)[0] == ["-p", "MON-VER"] and calls(tool)[-2:] == [["-z", "CFG-UART1OUTPROT-NMEA,0,1"],
                                                                          ["-g", "CFG-UART1OUTPROT-NMEA,0"]]
    assert [c for c in calls(tool) if "MON-VER" in c] == [["-p", "MON-VER"]]       # asked once per run
    out = capsys.readouterr().out
    assert "gpsd has still not identified the receiver" in out and "not identified (driver u-blox)" in out


def test_plain_read_reports_but_asks_nothing_new(tool, capsys):
    tool.gpsd([UNIDENTIFIED])
    assert tool.main([]) == 2
    assert calls(tool) == [["-g", "CFG-UART1OUTPROT-NMEA,0"]]
    assert "NOT identified" in capsys.readouterr().out


def test_gpsd_not_reachable_or_without_receiver(tool, capsys):
    gpsd = tool.gpsd([])
    assert tool.main(["--apply", "--ram"]) == 3
    assert "gpsd has no receiver" in capsys.readouterr().out
    gpsd.close()                            # nothing listens there now
    assert tool.main(["--apply", "--ram"]) == 3
    assert "cannot ask gpsd at 127.0.0.1:" in capsys.readouterr().out
    assert calls(tool) == []


def test_setting_not_corrected_or_no_answer_is_status_1(tool):
    tool.gpsd([IDENTIFIED])
    tool.value.write_text("1")
    assert tool.main([]) == 1               # read only: reported, not changed
    assert tool.value.read_text() == "1"
    tool.UBXTOOL = "/nonexistent/ubxtool"
    assert tool.main(["--apply", "--ram"]) == 1


def test_rejects_unknown_options(tool):
    with pytest.raises(SystemExit) as e:
        tool.main(["--fix"])
    assert "Exit status" in str(e.value)
