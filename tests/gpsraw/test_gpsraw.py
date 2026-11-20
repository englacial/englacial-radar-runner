"""The raw GPS log (radar_runner/gpsraw): framing, the check of a log, and the logger against stand-ins
for gpsd: a TCP stand-in of its raw pass-through, and a private gpsd run by gpsfake on a free port.

Nothing here connects to the live gpsd or sends anything to a receiver.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest
import ubx_fakes as F

from radar_runner.common.config import REPO
from radar_runner.gpsraw import ubx
from radar_runner.gpsraw.logger import RawLogger

T0 = 1795136400.0       # 2026-11-20T01:00:00Z


def epoch(t, **kw):
    return F.nav_pvt(t, -79.5, -112.0, 3.0) + F.rawx(t, **kw) + F.sfrbx() + F.nav_eoe(t)


# ---------------------------------------------------------------------- framing

def test_framer_cuts_whole_messages_from_any_pieces_and_skips_gpsd_json():
    messages = [epoch(T0 + i) for i in range(3)]
    stream = (b'{"class":"VERSION","release":"3.25"}\r\n' + messages[0] + b"$GNGGA,junk*00\r\n"
              + messages[1] + b'{"class":"DEVICE","path":"/dev/ttyACM0","activated":0}\r\n' + messages[2])
    for size in (1, 2, 7, 100, len(stream)):
        framer = ubx.Framer()
        out = []
        for i in range(0, len(stream), size):
            out += framer.feed(stream[i:i + size])
        assert b"".join(m for _, _, m in out) == b"".join(messages)
        assert [ubx.name(c, i) for c, i, _ in out][:4] == ["NAV-PVT", "RXM-RAWX", "RXM-SFRBX", "NAV-EOE"]
        assert framer.pending == 0 and framer.skipped == len(stream) - len(b"".join(messages))


def test_framer_resynchronises_after_an_impossible_length():
    bogus = b"\xb5\x62\x02\x15\xff\xff" + b"x" * 10
    framer = ubx.Framer()
    out = framer.feed(bogus + F.nav_eoe(T0))
    assert [m for _, _, m in out] == [F.nav_eoe(T0)]


def test_rawx_summary_counts_carrier_phase():
    payload = F.rawx(T0, n_sats=5)[6:-2]
    tow, week, leap, n, cp = ubx.rawx_summary(payload)
    assert (n, cp, leap) == (5, 5, 18) and abs(ubx.gps_to_unix(week, tow, leap) - T0) < 1e-6
    assert ubx.rawx_summary(F.rawx(T0, cp_valid=False)[6:-2])[4] == 0


# ---------------------------------------------------------------------- the check

def test_scan_counts_and_tolerates_a_cut_off_last_message(tmp_path):
    path = tmp_path / "gps_raw.ubx"
    data = b"".join(epoch(T0 + i, n_sats=6) for i in range(10))
    path.write_bytes(data)
    full = ubx.scan(str(path))
    assert full["messages"] == {"NAV-PVT": 10, "RXM-RAWX": 10, "RXM-SFRBX": 10, "NAV-EOE": 10}
    assert (full["rawx_epochs"], full["measurements"], full["carrier_phase_valid"]) == (10, 60, 60)
    assert full["by_system"] == {"GPS": {"measurements": 60, "carrier_phase_valid": 60}}
    assert full["first_epoch"]["utc"] == "2026-11-20T01:00:00.000Z"
    assert full["last_epoch"]["utc"] == "2026-11-20T01:00:09.000Z"
    assert full["bad_checksums"] == full["skipped_bytes"] == full["partial_last_bytes"] == 0
    # A power cut in the middle of the last RAWX: everything before it is read, the rest reported.
    cut = len(data) - len(F.nav_eoe(T0)) - len(F.sfrbx()) - 50
    path.write_bytes(data[:cut])
    part = ubx.scan(str(path))
    assert part["rawx_epochs"] == 9 and part["messages"]["NAV-PVT"] == 10 and part["bad_checksums"] == 0
    assert part["partial_last_bytes"] > 0


def test_scan_skips_a_damaged_message_and_finds_the_next(tmp_path):
    good = [epoch(T0 + i) for i in range(3)]
    broken = bytearray(good[1])
    broken[120] ^= 0xFF                     # inside the RAWX payload
    path = tmp_path / "gps_raw.ubx"
    path.write_bytes(good[0] + bytes(broken) + good[2])
    result = ubx.scan(str(path))
    assert result["bad_checksums"] == 1 and result["rawx_epochs"] == 2 and result["messages"]["NAV-PVT"] == 3


def test_scan_finds_good_messages_after_a_damaged_length_near_the_end(tmp_path):
    good = [epoch(T0 + i) for i in range(3)]
    damaged = bytearray(F.nav_eoe(T0))
    damaged[4:6] = (4000).to_bytes(2, "little")     # a length that runs past the end of the file
    path = tmp_path / "gps_raw.ubx"
    path.write_bytes(good[0] + bytes(damaged) + good[1] + good[2])
    result = ubx.scan(str(path))
    assert result["rawx_epochs"] == 3 and result["partial_last_bytes"] == 0 and result["skipped_bytes"] > 0


def test_epoch_times_round_to_the_millisecond_without_losing_a_second():
    week, tow = F.gps_week_tow(T0 + 0.9996)
    assert ubx.epoch_time(week, tow, 18)["utc"] == "2026-11-20T01:00:01.000Z"
    assert ubx.epoch_time(week, float("inf"), 18)["utc"] is None


def run_check(path, *args):
    return subprocess.run([sys.executable, "-B", "-m", "radar_runner.gpsraw", "check", str(path), *args],
                          cwd=REPO, capture_output=True, text=True, timeout=60)


def test_check_command_says_whether_there_is_carrier_phase(tmp_path):
    path = tmp_path / "gps_raw.ubx"
    path.write_bytes(b"".join(epoch(T0 + i) for i in range(3)))
    r = run_check(path)
    assert r.returncode == 0 and "RXM-RAWX epochs: 3, measurements: 24, with carrier phase: 24" in r.stdout
    assert "carrier phase: yes" in r.stdout and "GPS week 2445" in r.stdout
    r = run_check(path, "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["carrier_phase"] is True
    path.write_bytes(F.nav_pvt(T0, -79.5, -112.0, 0.0) + F.rawx(T0, cp_valid=False))
    r = run_check(path)
    assert r.returncode == 1 and "carrier phase: NO" in r.stdout
    assert run_check(tmp_path / "missing.ubx").returncode == 2


# ---------------------------------------------------------------------- the logger

KEEP = ["RXM-RAWX", "RXM-SFRBX", "NAV-PVT"]


def logger_for(tmp_path, gpsd, **kw):
    return RawLogger(str(tmp_path / "gps_raw.ubx"), port=gpsd.port, keep=kw.pop("keep", KEEP),
                     reconnect_s=0.1, **kw)


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def run_in_thread(logger):
    import threading
    result = {}
    thread = threading.Thread(target=lambda: result.update(logger.run()), daemon=True)
    thread.start()
    return thread, result


def test_logger_writes_only_the_kept_messages_and_carries_on_after_gpsd_restarts(tmp_path):
    first = b"".join(epoch(T0 + i) for i in range(5))
    second = b"".join(epoch(T0 + 10 + i) for i in range(4))
    # Closed after each: gpsd restarting. The second connection: gpsd up again without the receiver.
    gpsd = F.RawGpsd([first, b"", second], close=True)
    logger = logger_for(tmp_path, gpsd)
    thread, result = run_in_thread(logger)
    wait_for(lambda: logger.rawx_epochs == 9)
    logger.stopping = True
    thread.join(5)
    assert not thread.is_alive()
    assert all(w == b'?WATCH={"enable":true,"raw":2};\n' for w in gpsd.watches)     # nothing else is sent
    expected = b"".join(m for e in [first, second] for _, _, m in ubx.Framer().feed(e)
                        if ubx.name(*m[2:4]) in KEEP)
    assert (tmp_path / "gps_raw.ubx").read_bytes() == expected
    assert result["messages"] == {"NAV-PVT": 9, "RXM-RAWX": 9, "RXM-SFRBX": 9}
    assert result["rawx_epochs"] == 9 and result["carrier_phase_valid"] == 72 and result["bytes"] == len(expected)
    assert result["gpsd_connects"] >= 3 and result["gpsd_disconnects"] >= 2
    assert result["first_rawx"]["utc"] == "2026-11-20T01:00:00.000Z"
    assert result["last_rawx"]["utc"] == "2026-11-20T01:00:13.000Z"
    assert result["first_rawx"]["received_uptime_s"] <= result["last_rawx"]["received_uptime_s"]
    gpsd.close()


def test_logger_waits_for_gpsd_and_reports_when_none_is_there(tmp_path):
    with socket.socket() as s:                  # a port with nothing listening
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    logger = RawLogger(str(tmp_path / "gps_raw.ubx"), port=port, reconnect_s=0.05, max_s=0.5)
    result = logger.run()
    assert result["bytes"] == 0 and result["rawx_epochs"] == 0 and result["gpsd_connects"] == 0
    assert "cannot connect to gpsd" in result["error"]
    assert (tmp_path / "gps_raw.ubx").read_bytes() == b""


def start_logger(tmp_path, port, *extra):
    return subprocess.Popen([sys.executable, "-B", "-m", "radar_runner.gpsraw", "log", "--out",
                             str(tmp_path / "gps_raw.ubx"), "--port", str(port), "--keep", ",".join(KEEP), *extra],
                            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_logger_process_stops_at_once_on_sigterm_and_prints_its_summary(tmp_path):
    gpsd = F.RawGpsd([b"".join(epoch(T0 + i) for i in range(20))], chunk=500, pause_s=0.01)
    proc = start_logger(tmp_path, gpsd.port, "--fsync-s", "1")
    wait_for(lambda: os.path.getsize(tmp_path / "gps_raw.ubx") > 3000 if (tmp_path / "gps_raw.ubx").exists() else False)
    time.sleep(0.5)                              # idle in recv now: the stop must not wait for its timeout
    asked = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=5)
    assert time.monotonic() - asked < 0.5
    assert proc.returncode == 0
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["rawx_epochs"] == 20 and summary["file"] == "gps_raw.ubx" and summary["error"] == ""
    assert summary["bytes"] == os.path.getsize(tmp_path / "gps_raw.ubx")
    assert "connected to gpsd" in err
    assert ubx.scan(str(tmp_path / "gps_raw.ubx"))["rawx_epochs"] == 20
    gpsd.close()


def test_logger_process_ends_at_once_when_its_parent_dies(tmp_path):
    """The kernel's parent-death signal, not only the look at the parent between two reads: idle in
    recv (1 s timeout), the logger is gone well within that time."""
    gpsd = F.RawGpsd([b""])                     # connected, nothing ever sent
    code = ("import os, subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-B', '-m', 'radar_runner.gpsraw', 'log', '--out', "
            f"{str(tmp_path / 'gps_raw.ubx')!r}, '--port', '{gpsd.port}', '--parent-pid', str(os.getpid())], "
            "stdout=subprocess.DEVNULL)\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(30)\n")
    parent = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=REPO, stdout=subprocess.PIPE, text=True)
    pid = int(parent.stdout.readline())
    try:
        wait_for(lambda: gpsd.connections == 1)
        time.sleep(0.3)
        parent.kill()
        parent.wait()
        died = time.monotonic()
        wait_for(lambda: not F.alive(pid), timeout=5)
        assert time.monotonic() - died < 0.4
    finally:
        if F.alive(pid):
            os.kill(pid, signal.SIGKILL)
        gpsd.close()


def test_logger_process_ends_if_its_parent_is_gone_before_it_starts(tmp_path):
    """A manager that died while the logger was starting: the PID it was given is not its parent."""
    gpsd = F.RawGpsd([b""])
    proc = start_logger(tmp_path, gpsd.port, "--parent-pid", "1")
    try:
        out, _ = proc.communicate(timeout=5)
        assert json.loads(out.strip().splitlines()[-1])["gpsd_connects"] == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        gpsd.close()


def test_fsync_interval_has_a_floor(tmp_path):
    for value in (0.0, -5.0):
        assert RawLogger(str(tmp_path / "x"), fsync_s=value).fsync_s == 1.0


def test_a_gpsd_that_closes_every_connection_is_not_hammered(tmp_path):
    import io
    gpsd = F.RawGpsd([], close=True)            # accepts, answers the WATCH, closes: again and again
    err = io.StringIO()
    logger = RawLogger(str(tmp_path / "gps_raw.ubx"), port=gpsd.port, reconnect_s=0.2, max_s=1.5, err=err)
    result = logger.run()
    gpsd.close()
    assert result["gpsd_connects"] <= 10 and result["gpsd_disconnects"] <= 10
    assert len(err.getvalue().splitlines()) <= 4                # the same text is not repeated


def test_the_same_problem_is_written_to_the_log_at_most_once_a_minute(tmp_path):
    import io
    err = io.StringIO()
    clock = [0.0]
    logger = RawLogger(str(tmp_path / "x"), err=err, monotonic=lambda: clock[0])
    for i in range(600):
        logger._say("cannot connect to gpsd" if i % 2 else "connected to gpsd")
        clock[0] += 1.0
    assert 10 <= len(err.getvalue().splitlines()) <= 25


def test_a_message_cut_off_by_a_disconnect_is_not_written(tmp_path):
    whole = [epoch(T0 + i) for i in range(3)]
    cut = whole[0] + whole[1][:150]             # the connection drops inside the second epoch's RXM-RAWX
    gpsd = F.RawGpsd([cut, b"".join(whole[1:])], close=True)
    logger = logger_for(tmp_path, gpsd)
    thread, result = run_in_thread(logger)
    wait_for(lambda: logger.rawx_epochs == 3)
    logger.stopping = True
    thread.join(5)
    gpsd.close()
    scan = ubx.scan(str(tmp_path / "gps_raw.ubx"))
    assert scan["bad_checksums"] == 0 and scan["skipped_bytes"] == 0 and scan["partial_last_bytes"] == 0
    assert scan["messages"] == result["messages"] and scan["rawx_epochs"] == result["rawx_epochs"] == 3
    assert result["skipped_bytes"] >= 150                       # the cut-off start: counted, not written


class Broken:
    def write(self, text):
        raise OSError(5, "Input/output error")

    def flush(self):
        raise OSError(5, "Input/output error")


def test_a_full_drive_and_a_broken_log_are_reported_and_the_logger_carries_on(tmp_path):
    gpsd = F.RawGpsd([b"".join(epoch(T0 + i) for i in range(5))])
    logger = RawLogger("/dev/full", port=gpsd.port, keep=KEEP, max_s=1.0, err=Broken())
    result = logger.run()
    gpsd.close()
    assert result["rawx_epochs"] == 5 and result["bytes"] == 0
    assert "No space left on device" in result["error"]


def test_an_unforeseen_error_in_the_loop_is_counted_and_the_loop_carries_on(tmp_path):
    gpsd = F.RawGpsd([b"".join(epoch(T0 + i) for i in range(5))], chunk=200, pause_s=0.05)
    logger = logger_for(tmp_path, gpsd, max_s=3.0)
    logger.pause_after_error_s = 0.1
    real, calls = logger._take, []

    def take(data, now):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("unforeseen")
        return real(data, now)
    logger._take = take
    result = logger.run()
    gpsd.close()
    assert result["loop_errors"] == 1 and "unforeseen" in result["error"] and result["rawx_epochs"] >= 3


def test_a_non_finite_receiver_time_does_not_break_the_summary_or_the_check(tmp_path):
    import struct
    bad = bytearray(F.rawx(T0)[6:-2])
    bad[0:8] = struct.pack("<d", float("nan"))
    gpsd = F.RawGpsd([ubx.frame(0x02, 0x15, bytes(bad))])
    logger = logger_for(tmp_path, gpsd, max_s=0.8)
    result = logger.run()
    gpsd.close()
    json.dumps(result, allow_nan=False)
    assert result["rawx_epochs"] == 1 and result["first_rawx"]["utc"] is None
    assert ubx.scan(str(tmp_path / "gps_raw.ubx"))["first_epoch"]["utc"] is None
    assert run_check(tmp_path / "gps_raw.ubx").returncode == 0


def test_the_longest_gap_counts_the_start_and_the_end_of_the_run(tmp_path):
    clock = [0.0]
    logger = RawLogger(str(tmp_path / "x"), monotonic=lambda: clock[0])
    logger._fd = os.open(str(tmp_path / "x"), os.O_WRONLY | os.O_CREAT)
    logger._started_at = 0.0
    clock[0] = 7.0
    logger._take(epoch(T0), clock[0])
    clock[0] = 8.0
    logger._take(epoch(T0 + 1), clock[0])
    clock[0] = 20.0
    assert logger.summary()["longest_gap_s"] == 12.0
    logger.last_message_at = None
    assert logger.summary()["longest_gap_s"] == 20.0            # nothing at all: the whole run


def test_logger_refuses_unknown_message_names(tmp_path):
    r = subprocess.run([sys.executable, "-B", "-m", "radar_runner.gpsraw", "log", "--out", str(tmp_path / "x"),
                        "--keep", "RXM-RAWX,NAV-NOPE"], cwd=REPO, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "NAV-NOPE" in r.stderr


# ---------------------------------------------------------------------- through a real gpsd

def gpsfake_session(log_path, cycle_s):
    if not shutil.which("gpsfake"):
        pytest.skip("gpsfake is not installed")
    return F.gpsfake_start(log_path, cycle_s)


def stop_gpsfake(proc):
    F.gpsfake_stop(proc)


def test_gpsfake_is_stopped_without_leaving_its_socket_or_shared_memory(tmp_path):
    feed = tmp_path / "receiver.ubx"
    feed.write_bytes(b"".join(epoch(T0 + i) for i in range(5)))
    fake, port = gpsfake_session(feed, 0.01)
    socket.create_connection(("127.0.0.1", port), timeout=1).close()
    gpsd = F.children(fake.pid)
    stop_gpsfake(fake)
    assert gpsd and not any(F.alive(p) for p in gpsd)
    assert not os.path.exists(f"/tmp/gpsfake-{fake.pid}.sock")
    key = "0x4770%04x" % port
    assert key not in subprocess.run(["ipcs", "-m"], capture_output=True, text=True).stdout


def test_through_gpsd_the_log_holds_rawx_with_carrier_phase(tmp_path):
    feed = tmp_path / "receiver.ubx"
    feed.write_bytes(b"".join(epoch(T0 + i) for i in range(40)))
    fake, port = gpsfake_session(feed, 0.01)
    try:
        proc = start_logger(tmp_path, port, "--fsync-s", "1")
        def complete():
            last = ubx.scan(str(tmp_path / "gps_raw.ubx"))["last_epoch"] if (tmp_path / "gps_raw.ubx").exists() else None
            return last is not None and last["utc"] == "2026-11-20T01:00:39.000Z"
        wait_for(complete, timeout=20)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=5)
    finally:
        stop_gpsfake(fake)
    result = ubx.scan(str(tmp_path / "gps_raw.ubx"))
    # gpsd takes the first packets to recognise the receiver: the log starts with the epoch after.
    assert result["rawx_epochs"] >= 20 and result["carrier_phase_valid"] == 8 * result["rawx_epochs"]
    assert result["bad_checksums"] == 0 and result["skipped_bytes"] == 0 and result["partial_last_bytes"] == 0
    assert set(result["messages"]) == set(KEEP)
    assert json.loads(out.strip().splitlines()[-1])["rawx_epochs"] == result["rawx_epochs"]
    assert result["last_epoch"]["utc"] == "2026-11-20T01:00:39.000Z"
