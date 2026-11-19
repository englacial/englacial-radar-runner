"""The telemetry service as a whole (radar_runner/telemetry/service.py), in simulated time and as a real process."""
import copy
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest
from telemetry_fakes import (CONFIGURED_HEADER, FakeSerial, GpsdServer, Ports, configured_row, load_session,
                             make_pi_tree)

from radar_runner.common import bootcount as B
from radar_runner.common import config as C
from radar_runner.common import status as S
from radar_runner.telemetry import csvlog as L
from radar_runner.telemetry import gps as G
from radar_runner.telemetry import ola as O
from radar_runner.telemetry import pi as P
from radar_runner.telemetry.service import Telemetry

REPO = C.REPO
WALL0 = 1795046400.0 + 3600         # 2026-11-19T01:00:00Z
UP0 = 500.0


class Notifier:
    ping_interval = 1.0

    def ready(self):
        pass

    def stopping(self):
        pass

    def watchdog(self):
        pass


class SyncStore:
    """The log store without its thread: written at every status request, so tests are exact."""

    def __init__(self, store):
        self.store = store

    def write(self, *row):
        self.store.write(*row)

    def status(self):
        self.store.tick()
        return dict(self.store.status(), writer=self.writer)

    writer = "running"

    def close(self):
        self.store.close()


class Bench:
    """A telemetry service on stand-ins for every device, in simulated time (0.25 s per round)."""

    def __init__(self, tmp_path, *, ports=None, power=True, **telemetry):
        self.tmp = tmp_path
        self.t = 0.0                # one clock drives monotonic time, uptime and the wall clock
        self.wall_offset = WALL0
        config = copy.deepcopy(C.DEFAULTS)
        for d in ("run", "state", "drive", "card"):
            (tmp_path / d).mkdir(exist_ok=True)
        config["paths"].update(run_dir=str(tmp_path / "run"), state_dir=str(tmp_path / "state"),
                               power_status_file=str(tmp_path / "power.json"))
        tel = config["telemetry"]
        tel["gps"]["receiver_fix_command"] = ""       # never touch the real receiver from a test
        tel.update(data_root=str(tmp_path / "drive"), data_mount="-", fallback_dir=str(tmp_path / "card" / "fb"))
        for key, value in telemetry.items():
            section, _, name = key.partition("__")
            if name:
                tel[section][name] = value
            else:
                tel[key] = value
        self.config = config
        mono, up, wall = (lambda: self.t), (lambda: UP0 + self.t), (lambda: self.wall_offset + self.t)
        self.port = FakeSerial(load_session())
        self.ports = ports if ports is not None else Ports(self.port)
        self.gpsd = GpsdServer()
        self.power = S.StatusWriter(str(tmp_path / "power.json"), wall=wall, uptime=up, boot_id="")
        self.power_on = power
        store = SyncStore(L.LogStore(primary_root=str(tmp_path / "drive" / "telemetry"),
                                     fallback_root=tel["fallback_dir"], boot=7, boot_id="this-boot", monotonic=mono,
                                     flush_interval_s=0.0))
        self.service = Telemetry(
            config, notifier=Notifier(), monotonic=mono, wall=wall, uptime=up, store=store,
            status=S.StatusWriter(str(tmp_path / "run" / "telemetry.json"), wall=wall, uptime=up),
            ola=O.Ola(tel["ola"], open_port=self.ports, exists=self.ports.exists, monotonic=mono),
            gps=G.Gps(tel["gps"], connect=self.gpsd, monotonic=mono),
            pi=P.PiSensors(root=make_pi_tree(str(tmp_path / "root")), data_mount="", clock_synced=lambda: True))
        self.service.boot = 7
        self.service.setup()
        self.published = None

    def run(self, seconds, chunks=1):
        for _ in range(int(round(seconds / 0.25))):
            self.t += 0.25
            self.ports.release(chunks)
            if self.power_on and abs(self.t % 1.0) < 1e-9:
                self.power.write({"state": "normal", "mode": "armed", "power_present": True, "capacitor_v": 5.17,
                                  "current_a": 0.016, "radar_stop_requested": False, "low_voltage": False})
            data = self.service.tick()
            if data is not None:
                self.service.publish(data)
                self.published = data
        return self.published

    def snap(self):
        """What the service would publish right now (the file itself is written every other round)."""
        return self.service.snapshot(UP0 + self.t)

    def keep_streaming(self, seconds, start=100):
        """Ten rows per second, arriving as the port delivers them: two or three per read."""
        rows = [configured_row(n=start + n) for n in range(int(seconds * 10))]
        while rows:
            self.port.feed("".join(rows[:3]), "".join(rows[3:5]))
            rows = rows[5:]

    def status_file(self, max_age=5.0):
        return S.read_status(str(self.tmp / "run" / "telemetry.json"), max_age, uptime=lambda: UP0 + self.t)

    def log_files(self, where="drive"):
        root = self.tmp / where
        return sorted(os.path.relpath(os.path.join(r, n), root) for r, _, names in os.walk(root) for n in names
                      if n.endswith(".csv"))

    def log_rows(self, source, where="drive"):
        rows = []
        for name in self.log_files(where):
            if os.path.basename(name).startswith(source + "_"):
                with open(self.tmp / where / name, newline="") as f:
                    rows += [dict(row) for row in csv.DictReader(f)]
        return rows


@pytest.fixture
def bench(tmp_path):
    return Bench(tmp_path)


def started(bench, seconds=20):
    """Run until the OLA is configured and streaming, then keep rows coming."""
    bench.run(seconds)
    assert bench.service.ola.configured
    return bench


def test_all_sources_are_published_and_logged(bench):
    bench.keep_streaming(40)
    bench.run(30)
    data = bench.published
    assert data["state"] == "ok" and data["problems"] == [] and data["boot_count"] == 7
    assert {name: s["health"] for name, s in data["sources"].items()} == dict.fromkeys(("ola", "pi", "ups", "gps"), "ok")
    assert all(s["reason"] == "" and s["age_s"] is not None and s["age_s"] <= 2.0 for s in data["sources"].values())
    ola = data["sources"]["ola"]
    assert ola["configured"] and ola["connection"] == "streaming" and ola["bad_lines"] == 0
    assert ola["values"]["A0A3mV"] == 140.0 and ola["values"]["rtcDate"] == "01/01/2000"
    assert ola["values"]["amp_current_a"] == 0.156
    assert data["sources"]["pi"]["values"]["soc_temp_c"] == 50.15
    assert data["sources"]["pi"]["throttled_now"] == ["under_voltage", "throttled"]
    assert data["sources"]["ups"]["values"]["capacitor_v"] == 5.17 and data["sources"]["ups"]["values"]["state"] == "normal"
    gps = data["sources"]["gps"]
    assert gps["values"]["mode"] == 3 and gps["values"]["lat"] == 40.0 and gps["new_fix_age_s"] <= 1.0
    assert data["logging"]["location"] == "primary" and data["logging"]["rows_dropped"] == 0
    assert data["clock"] == {"steps": 0, "last_step_s": None, "last_step_uptime_s": None, "synced": 1}
    assert data["loop"]["rounds"] == 120 and data["loop"]["longest_gap_s"] == 0.25      # every round on time
    assert set(data["loop"]["slowest_step_s"]) == {"ola", "gps", "gps_receiver", "pi", "ups", "status_file"}
    # The published file, read the way every other service reads it.
    read = bench.status_file()
    assert read.ok and read.data["amp"]["fresh"] is True
    # One log per source, named by boot, sequence and date; every row has both clocks.
    assert bench.log_files() == [f"telemetry/b00007/{s}_b00007_00001_20261119.csv" for s in ("gps", "ola", "pi", "ups")]
    rows = bench.log_rows("ola")
    assert list(rows[0])[:4] == ["time_utc", "uptime_s", "rtcDate", "rtcTime"]
    assert list(rows[0])[-3:] == ["amp_temp_c", "amp_current_a", "amp_voltage_v"]
    assert rows[0]["time_utc"].startswith("2026-11-19T01:00:") and 500 < float(rows[0]["uptime_s"]) < 530
    assert rows[0]["rtcDate"] == "01/01/2000" and rows[0]["gps_Time"]            # the OLA's own clock and GPS time
    assert len(rows) == data["sources"]["ola"]["rows"] >= 100
    uptimes = [float(r["uptime_s"]) for r in rows]
    assert uptimes == sorted(uptimes)
    gps_rows = bench.log_rows("gps")
    assert list(gps_rows[0])[:5] == ["time_utc", "uptime_s", "gps_time", "new_fix", "mode"]
    assert 27 <= len(gps_rows) <= 30 and gps_rows[-1]["gps_time"].startswith("2026-11-19T01:00:")
    assert 15 <= len(bench.log_rows("pi")) <= 16 and 29 <= len(bench.log_rows("ups")) <= 31
    assert bench.log_rows("ups")[-1]["status"] == "ok" and bench.log_rows("ups")[-1]["power_present"] == "1"


def test_intervals_hold_from_the_first_round_when_uptime_is_large(bench):
    bench.t = 86400.0                       # the service starts a day after boot
    bench.run(10)
    for source, interval in (("ups", 1.0), ("pi", 2.0), ("gps", 1.0)):
        times = [float(r["uptime_s"]) for r in bench.log_rows(source)]
        steps = [round(b - a, 2) for a, b in zip(times, times[1:])]
        assert steps and set(steps) == {interval}, (source, steps)


def test_amplifier_values_match_the_bench_formulas(bench):
    started(bench)
    bench.port.script.clear()
    for a0, a1, a2 in ((140.0, 26.0, 2542.0), (264.0, 430.0, 2500.0), (275.0, 433.0, 2496.0)):
        bench.port.feed(configured_row(a0, a1, a2, tmp117=31.5))
        amp = bench.run(0.5)["amp"]
        # pa_run.py: temp_c = mv*2/10, current_a = mv*2/500*1.5, voltage_v = mv*11/1000
        assert amp["temp_c"] == pytest.approx(a0 * 2 / 10.0, abs=0.01)
        assert amp["current_a"] == pytest.approx(a1 * 2 / 500.0 * 1.5, abs=0.001)
        assert amp["voltage_v"] == pytest.approx(a2 * 11 / 1000.0, abs=0.001)
        assert amp["case_temp_c"] == 31.5 and amp["fresh"] is True
    last = bench.log_rows("ola")[-1]
    assert (last["amp_temp_c"], last["amp_current_a"], last["amp_voltage_v"]) == ("55.0", "2.598", "27.456")


def test_amplifier_freshness_and_ages_follow_uptime(bench):
    started(bench)
    bench.port.script.clear()
    bench.run(0.5)
    bench.port.feed(configured_row())
    bench.run(0.5)
    amp = bench.published["amp"]
    sampled = amp["sample_uptime_s"]
    # The reading arrived at some time since the round before: that round is its sample time.
    assert amp["fresh"] is True and 0.25 <= amp["age_s"] <= 0.5 and amp["fresh_limit_s"] == 2.0
    assert amp["sample_time_utc"].startswith("2026-11-19T01:00:")
    amp = bench.run(1.5)["amp"]             # no more rows: still within the limit
    assert amp["fresh"] is True and amp["age_s"] == pytest.approx(UP0 + bench.t - sampled, abs=0.01)
    amp = bench.run(1.0)["amp"]
    assert amp["fresh"] is False and "old" in amp["reason"] and amp["sample_uptime_s"] == sampled
    assert amp["temp_c"] == 28.0            # the last value stays visible, marked as not fresh
    data = bench.published
    assert data["sources"]["ola"]["health"] == "stale" and "no row for" in data["sources"]["ola"]["reason"]
    assert data["state"] == "degraded" and "ola: stale" in data["problems"]
    # A reader works the age out itself from the sample's uptime, whatever the wall clock does.
    read = bench.status_file()
    assert read.ok and UP0 + bench.t - read.data["amp"]["sample_uptime_s"] == pytest.approx(3.0, abs=0.26)
    bench.port.feed(configured_row(n=9))
    assert bench.run(0.5)["amp"]["fresh"] is True
    # If the service itself stops, the whole file goes stale for every reader.
    bench.t += 30
    assert bench.status_file().state == S.STALE


def test_backlog_after_a_loop_stall_is_not_published_as_fresh(bench):
    """The reviewer's case: the loop stalls, the tty fills, and the backlog must not look like new readings."""
    started(bench)
    bench.port.script.clear()
    bench.keep_streaming(3, start=0)
    bench.run(3)
    assert bench.snap()["amp"]["fresh"] is True
    logged = len(bench.log_rows("ola"))
    # 12 s without a round. The OLA sent 100 rows and then stopped 2 s before the loop came back:
    # the newest row in the tty is 2 s old, the oldest 12 s.
    stalled_from = UP0 + bench.t
    for n in range(100):
        bench.port.feed(configured_row(a0a3=300.0, n=1000 + n))
    bench.t += 12.0
    bench.run(0.25, chunks=1000)
    data = bench.snap()
    amp = data["amp"]
    assert amp["fresh"] is False
    assert amp["temp_c"] == 60.0                            # the value is shown, with the only time that is certain:
    assert amp["sample_uptime_s"] == pytest.approx(stalled_from, abs=0.01)      # it arrived after the round before the stall
    assert amp["age_s"] == pytest.approx(12.25, abs=0.01) and "12.2 s old" in amp["reason"]
    assert data["loop"]["last_gap_s"] == 12.25 and data["loop"]["recent_longest_gap_s"] == 12.25
    assert data["loop"]["longest_gap_s"] == 12.25
    # In the log the rows are dated back by the OLA's own clock, 0.1 s apart, not all stamped with the same time.
    rows = bench.log_rows("ola")[logged:]
    assert len(rows) == 100
    times = [float(r["uptime_s"]) for r in rows]
    assert times[-1] == pytest.approx(UP0 + bench.t, abs=0.01) and times[0] == pytest.approx(UP0 + bench.t - 9.9, abs=0.01)
    assert all(b - a == pytest.approx(0.1, abs=0.002) for a, b in zip(times, times[1:]))
    assert rows[0]["time_utc"] < rows[-1]["time_utc"]
    # Nothing new arrives (the OLA has stopped): the stale reading never turns fresh.
    for _ in range(8):
        bench.run(0.25)
        amp = bench.snap()["amp"]
        assert amp["fresh"] is False and amp["sample_uptime_s"] == pytest.approx(stalled_from, abs=0.01)
    assert bench.snap()["loop"]["last_gap_s"] == 0.25 and bench.snap()["loop"]["recent_longest_gap_s"] == 12.25
    # The OLA sends again: the first row read in an ordinary round is fresh.
    bench.port.feed(configured_row(n=2000))
    bench.run(0.25)
    amp = bench.snap()["amp"]
    assert amp["fresh"] is True and amp["temp_c"] == 28.0 and amp["age_s"] == 0.25
    # The stall leaves the "recent" figure after one to two minutes; the all-time figure keeps it.
    bench.keep_streaming(130, start=3000)
    bench.run(125)
    assert bench.snap()["loop"]["recent_longest_gap_s"] == 0.25 and bench.snap()["loop"]["longest_gap_s"] == 12.25


def test_stall_at_the_moment_it_is_published_is_never_fresh(bench):
    """Even if a row has just been read: after a gap longer than the limit the status of that round is not fresh."""
    started(bench)
    bench.port.script.clear()
    bench.keep_streaming(2, start=0)
    bench.run(2)
    for n in range(30):
        bench.port.feed(configured_row(n=500 + n))
    bench.t += 3.0                          # longer than the 2 s limit
    bench.run(0.25, chunks=1000)
    amp = bench.snap()["amp"]
    assert amp["fresh"] is False            # it arrived some time in the last 3.25 s: not shown to be recent
    bench.service._amp["uptime"] = UP0 + bench.t        # and were the sample time wrong, the loop gap alone says no
    amp = bench.snap()["amp"]
    assert amp["fresh"] is False and amp["reason"] == "the service loop stalled for 3.2 s"
    bench.port.feed(configured_row(n=530), configured_row(n=531))
    bench.run(0.25, chunks=2)
    assert bench.snap()["amp"]["fresh"] is True


def test_port_that_cannot_be_read_empty_does_not_refresh_the_amplifier_status(bench, monkeypatch):
    started(bench)
    bench.port.script.clear()
    bench.keep_streaming(2, start=0)
    bench.run(2)
    before = bench.published["amp"]["sample_uptime_s"]
    monkeypatch.setattr(O, "MAX_POLL_BYTES", 2000)
    for n in range(60):
        bench.port.feed(configured_row(a0a3=300.0, n=700 + n))
    bench.run(0.25, chunks=1000)            # the first round cannot empty the port
    assert not bench.service.ola.drained
    amp = bench.snap()["amp"]
    assert amp["sample_uptime_s"] == before and amp["temp_c"] == 28.0       # nothing refreshed, no row handed out
    assert len([r for r in bench.log_rows("ola") if r["A0A3mV"] == "300.0"]) == 0
    bench.run(3, chunks=1000)
    assert bench.service.ola.drained and bench.snap()["amp"]["temp_c"] == 60.0
    assert len([r for r in bench.log_rows("ola") if r["A0A3mV"] == "300.0"]) == 60


def test_amplifier_is_fresh_only_while_the_ola_is_set_up_for_it(bench):
    started(bench)
    bench.port.script.clear()
    bench.keep_streaming(2, start=0)
    assert bench.run(2)["amp"]["fresh"] is True
    # The OLA restarts by itself: within the same half second the readings stop counting as fresh,
    # although the last one is not yet 2 s old.
    bench.port.feed("Artemis OpenLog v2.11\r\n")
    bench.run(0.5)
    amp = bench.snap()["amp"]
    assert amp["fresh"] is False and amp["age_s"] < 2.0
    assert "not set up for the amplifier readings" in amp["reason"] and amp["temp_c"] == 28.0
    assert bench.snap()["sources"]["ola"]["restarts_seen"] == 1


def test_ola_retry_state_is_published(tmp_path):
    silent = FakeSerial()
    silent.silent = True
    b = Bench(tmp_path, ports=Ports(silent, FakeSerial(load_session())))
    data = b.run(62)
    ola = data["sources"]["ola"]
    assert ola["health"] == "error" and "no valid row for" in ola["reason"] and ola["connection"] == "closed"
    assert ola["port_opens"] == 1
    assert ola["retry"]["failures_in_a_row"] == 2 and ola["retry"]["waiting_for"] == "reopening the port (resets the OLA)"
    assert 0 < ola["retry"]["next_attempt_in_s"] <= 5 and ola["retry"]["reopen_wait_s"] == 10.0
    assert data["amp"]["fresh"] is False and "no valid row" in data["amp"]["reason"]
    for _ in range(160):
        b.run(0.25)
        if b.service.ola.configured and b.service.ola.rows:
            break
    ola = b.snap()["sources"]["ola"]
    assert ola["health"] == "ok" and ola["port_opens"] == 2
    assert ola["retry"]["failures_in_a_row"] == 2           # kept until five good minutes have passed
    assert ola["retry"]["waiting_for"] is None and ola["retry"]["reopen_wait_s"] == 10.0


def test_clock_step_keeps_ages_and_starts_a_new_file(bench, caplog):
    caplog.set_level("INFO")
    started(bench)
    bench.keep_streaming(60)
    bench.run(5)
    bench.wall_offset += 400 * 86400.0      # GPS or the network sets the clock: more than a year forward
    data = bench.run(5)
    assert data["amp"]["fresh"] is True and data["amp"]["age_s"] <= 0.5
    assert all(s["health"] == "ok" and s["age_s"] <= 2.0 for s in data["sources"].values())
    assert data["clock"]["steps"] == 1 and data["clock"]["last_step_s"] == pytest.approx(400 * 86400.0)
    assert "the system clock was stepped by +34560000.000 s" in caplog.text
    assert bench.status_file().ok
    names = [n for n in bench.log_files() if "/ola_" in n]
    assert [n[-16:] for n in names] == ["001_20261119.csv", "002_20271224.csv"]
    rows = bench.log_rows("ola")
    uptimes = [float(r["uptime_s"]) for r in rows]
    assert uptimes == sorted(uptimes)
    across = [u for u in uptimes if u > UP0 + 21]           # the rows around the step: in order and without a gap
    assert len(across) >= 90 and max(b - a for a, b in zip(across, across[1:])) <= 0.5
    bench.wall_offset -= 800 * 86400.0      # and far back
    data = bench.run(3)
    assert data["clock"]["steps"] == 2 and data["amp"]["fresh"] is True
    assert [n[-16:-13] for n in bench.log_files() if "/ola_" in n] == ["001", "002", "003"]


def test_ola_missing_does_not_stop_the_other_sources(tmp_path, monkeypatch):
    ports = Ports()
    ports.missing = True
    b = Bench(tmp_path, ports=ports)
    data = b.run(15)
    assert data["sources"]["ola"]["health"] == "missing" and "cannot open the port" in data["sources"]["ola"]["reason"]
    assert data["amp"]["fresh"] is False and "cannot open the port" in data["amp"]["reason"]
    assert data["amp"]["temp_c"] is None and data["amp"]["age_s"] is None
    assert [data["sources"][s]["health"] for s in ("pi", "ups", "gps")] == ["ok", "ok", "ok"]
    assert data["state"] == "degraded" and data["problems"] == ["ola: missing"]
    assert not [n for n in b.log_files() if "/ola_" in n] and len(b.log_files()) == 3


def test_ola_unconfigured_rows_are_logged_but_not_fresh(tmp_path, monkeypatch):
    port = FakeSerial()
    port.feed(CONFIGURED_HEADER.replace("A0A3mV,A1A3mV,A2A3mV", "A0mV,A1mV,A2mV,A3mV") + "\r\n")
    for n in range(600):
        port.feed(configured_row(n=n).replace(",9.5,", ",1.0,9.5,"))
    b = Bench(tmp_path, ports=Ports(port))
    data = b.run(40)
    assert data["sources"]["ola"]["health"] == "error" and "did not answer" in data["sources"]["ola"]["reason"]
    assert data["amp"]["fresh"] is False and "did not answer" in data["amp"]["reason"]
    rows = b.log_rows("ola")
    assert rows and "A0mV" in rows[0] and rows[0]["amp_temp_c"] == ""


def test_ups_status_missing_stale_and_damaged(bench):
    started(bench)
    data = bench.run(2)
    assert data["sources"]["ups"]["health"] == "ok"
    bench.power_on = False                  # the power service stops writing
    data = bench.run(5)
    ups = data["sources"]["ups"]
    assert ups["health"] == "stale" and "limit 3 s" in ups["reason"]
    assert ups["values"]["status"] == "stale" and ups["values"]["capacitor_v"] is None     # an old value is not a reading
    assert bench.log_rows("ups")[-1]["status"] == "stale" and bench.log_rows("ups")[-1]["capacitor_v"] == ""
    (bench.tmp / "power.json").write_text("{ not json")
    assert bench.run(2)["sources"]["ups"]["health"] == "error"
    os.remove(bench.tmp / "power.json")
    data = bench.run(2)
    assert data["sources"]["ups"]["health"] == "missing" and bench.log_rows("ups")[-1]["status"] == "missing"
    bench.power_on = True
    assert bench.run(2)["sources"]["ups"]["health"] == "ok"


def test_gps_down_stale_and_without_receiver(bench):
    started(bench)
    bench.keep_streaming(200)
    assert bench.run(2)["sources"]["gps"]["health"] == "ok"
    conn = bench.gpsd.connections[-1]
    conn.advance = False
    data = bench.run(8)
    assert data["sources"]["gps"]["health"] == "stale" and "no fix time from the receiver for" in data["sources"]["gps"]["reason"]
    assert bench.log_rows("gps")[-1]["new_fix"] == "0"
    data = bench.run(25)                    # still present, still nothing new: what a choked receiver output looks like
    gps = data["sources"]["gps"]
    assert gps["health"] == "error" and "receiver present but no fix data for 3" in gps["reason"]
    assert "setup/check.py --receiver" in gps["reason"] and "gps: error" in data["problems"]
    conn.advance, conn.active = True, 0
    data = bench.run(3)
    assert data["sources"]["gps"]["health"] == "missing" and data["sources"]["gps"]["reason"] == "gpsd has no receiver"
    conn.active = 1
    conn.tpv.update(mode=1, lat=None, lon=None)
    data = bench.run(3)
    assert data["sources"]["gps"]["health"] == "ok" and data["sources"]["gps"]["reason"] == "no position fix"
    conn.eof = True
    bench.gpsd.down = True
    data = bench.run(8)
    assert data["sources"]["gps"]["health"] == "missing" and "cannot connect to gpsd" in data["sources"]["gps"]["reason"]
    assert data["amp"]["fresh"] is True and data["sources"]["ola"]["health"] == "ok"
    bench.gpsd.down = False
    assert bench.run(8)["sources"]["gps"]["health"] == "ok"


def test_unexpected_error_in_one_source_is_contained(bench, caplog):
    started(bench)
    bench.keep_streaming(60)

    def broken():
        raise RuntimeError("sensor bug")
    bench.service.pi.read = broken
    data = bench.run(10)
    assert data["sources"]["pi"]["health"] == "error"
    assert data["sources"]["pi"]["reason"] == "internal error: RuntimeError: sensor bug"
    assert data["amp"]["fresh"] is True and data["sources"]["gps"]["health"] == "ok"
    assert caplog.text.count("pi: unexpected error") == 1               # logged once, not every round
    bench.service.ola.poll = lambda now: 1 / 0
    data = bench.run(3)
    assert data["sources"]["ola"]["health"] == "error" and data["amp"]["fresh"] is False
    assert "ZeroDivisionError" in data["amp"]["reason"]


def test_pi_readings_from_the_reader_thread(bench, caplog):
    """As in the real service: the Pi's sensors are read by a thread, and the loop picks the samples up."""
    class Reader:
        def __init__(self):
            self.ready = []

        def take(self):
            ready, self.ready = self.ready, []
            return ready

        def close(self):
            pass
    reader = bench.service._pi_reader = Reader()
    started(bench)
    bench.keep_streaming(60)
    values = bench.service.pi.read()
    assert bench.run(2)["sources"]["pi"]["health"] == "missing"            # nothing finished yet
    reader.ready.append((WALL0 + bench.t - 0.7, UP0 + bench.t - 0.7, values))
    pi = bench.run(0.5)["sources"]["pi"]
    assert pi["health"] == "ok" and pi["values"]["soc_temp_c"] == 50.15
    assert 0.7 <= pi["age_s"] <= 1.3                                        # counted from when the read began
    assert float(bench.log_rows("pi")[-1]["uptime_s"]) == pytest.approx(pi["sample_uptime_s"])
    data = bench.run(8)                     # the drive stalls the reader: only the Pi readings go stale
    assert data["sources"]["pi"]["health"] == "stale" and data["amp"]["fresh"] is True
    assert data["loop"]["longest_gap_s"] == 0.25
    reader.ready.append((WALL0 + bench.t, UP0 + bench.t, RuntimeError("sensor bug")))
    data = bench.run(3)
    assert data["sources"]["pi"]["reason"] == "internal error: RuntimeError: sensor bug"
    assert caplog.text.count("pi: unexpected error") == 1
    reader.ready.append((WALL0 + bench.t, UP0 + bench.t, values))
    assert bench.run(0.5)["sources"]["pi"]["health"] == "ok"


def test_ola_log_rate_can_be_reduced(tmp_path, monkeypatch):
    b = Bench(tmp_path, ola__log_interval_s=1.0)
    started(b)
    rows0, logged0 = b.service.ola.rows, len(b.log_rows("ola"))
    b.keep_streaming(20)
    data = b.run(20)
    assert b.service.ola.rows - rows0 >= 190
    assert 19 <= len(b.log_rows("ola")) - logged0 <= 21                 # one per second in the file
    assert data["amp"]["fresh"] is True and data["amp"]["age_s"] <= 0.25    # the status still has every row


def test_logs_fall_back_to_the_card_and_return(tmp_path, monkeypatch):
    b = Bench(tmp_path)
    drive = str(tmp_path / "drive")
    mounted = {"yes": False}
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: mounted["yes"] if p == drive else real(p))
    b.service.store.store.mount_point = drive
    b.keep_streaming(200)
    data = b.run(30)
    assert data["logging"]["location"] == "fallback" and "is not mounted" in data["logging"]["reason"]
    assert data["state"] == "degraded" and data["problems"] == ["logging: fallback"]
    assert data["amp"]["fresh"] is True
    assert b.log_files() == [] and len(b.log_files("card")) == 4
    mounted["yes"] = True
    data = b.run(15)
    assert data["logging"]["location"] == "primary" and data["state"] == "ok"
    assert [n[-16:-13] for n in b.log_files() if "/ola_" in n] == ["002"]
    before, after = b.log_rows("ola", "card"), b.log_rows("ola")
    assert float(before[-1]["uptime_s"]) < float(after[0]["uptime_s"]) <= float(before[-1]["uptime_s"]) + 1.5


def test_hung_or_dead_log_writer_changes_the_overall_state(bench):
    started(bench)
    bench.keep_streaming(30)
    data = bench.run(3)
    assert data["state"] == "ok" and data["logging"]["writer"] == "running"
    bench.service.store.writer = "stalled"              # as ThreadedLogStore reports a drive that hangs
    data = bench.run(1)
    assert data["state"] == "degraded" and "logging: writer stalled" in data["problems"]
    assert data["amp"]["fresh"] is True                 # the readings themselves go on
    bench.service.store.writer = "dead"
    data = bench.run(1)
    assert data["state"] == "degraded" and data["problems"] == ["logging: writer dead"]
    bench.service.store.writer = "running"
    assert bench.run(1)["state"] == "ok"


def test_boot_number_follows_the_logger_when_the_directory_belongs_to_another_boot(bench):
    # The counter said 7, but the drive already has a b00007 from an earlier boot (the counter was lost).
    old = bench.tmp / "drive" / "telemetry" / "b00007"
    old.mkdir(parents=True)
    (old / ".boot_id").write_text("an-earlier-boot\n")
    (old / "ola_b00007_00001_20261101.csv").write_text("time_utc,uptime_s\nold,1\n")
    assert B.boot_count(str(bench.tmp / "state"))[0] == 1
    data = bench.run(5)
    assert data["boot_count"] == 8 and data["logging"]["boot"] == 8
    assert all("/b00008/" in name for name in bench.log_files() if "20261119" in name)
    assert (old / "ola_b00007_00001_20261101.csv").read_text() == "time_utc,uptime_s\nold,1\n"
    assert sorted(os.listdir(old)) == [".boot_id", "ola_b00007_00001_20261101.csv"]        # nothing added
    # The shared counter follows, so that later names of this boot (collections) carry the same number.
    assert B.boot_count(str(bench.tmp / "state"))[0] == 8


def test_the_service_starts_no_other_program():
    """It must never run ubxtool or anything else: no subprocess in the whole package."""
    package = os.path.join(REPO, "radar_runner", "telemetry")
    for name in sorted(os.listdir(package)):
        if name.endswith(".py"):
            source = open(os.path.join(package, name)).read()
            for word in ("os.system", "os.exec", "os.spawn", "ubxtool", "gpsctl"):
                assert word not in source, (name, word)
            if name != "service.py":       # service.py starts one configured command after an OLA start
                assert "subprocess" not in source and "Popen" not in source, name
    assert C.DEFAULTS["telemetry"]["gps"]["receiver_fix_command"].endswith("setup/gps_receiver.py --apply --ram")


def test_receiver_correction_runs_once_after_each_ola_start(tmp_path):
    marker = tmp_path / "ran"
    bench = Bench(tmp_path)
    cfg = bench.service.cfg["gps"]
    cfg["receiver_fix_command"] = f"/bin/sh -c 'echo x >> {marker}'"
    cfg["receiver_fix_delay_s"] = 2.0
    svc = bench.service

    def at(t, configured):
        svc._ola_started(t, configured)     # in the OLA's step: bookkeeping only
        svc._step_receiver(t)               # in its own step: the command itself
    at(0.0, False)
    at(1.0, True)                           # the OLA has just been configured
    at(2.0, True)
    assert not marker.exists()              # not before the delay
    at(3.5, True)
    svc._fix_proc.wait(timeout=5)
    for t in (4.0, 5.0, 60.0):
        at(t, True)                         # still configured: not again
    assert marker.read_text() == "x\n"
    at(61.0, False)                         # the OLA resets ...
    at(62.0, True)                          # ... and is configured again
    at(65.0, True)
    svc._fix_proc.wait(timeout=5)
    assert marker.read_text() == "x\nx\n"


def retry_bench(tmp_path, command, **kw):
    """A started bench whose receiver command is `command`, with short no-position waits."""
    settings = dict(gps__no_position_warn_s=10.0, gps__no_position_retry_s=30.0, gps__no_position_retry_max_s=120.0)
    bench = Bench(tmp_path, gps__receiver_fix_command=command, **dict(settings, **kw))
    bench.keep_streaming(900)
    started(bench, 40)                       # the OLA start's run of the command is over by now
    wait_for_command(bench)
    assert bench.service._fix["last_why"] == "the OLA has just started"
    return bench


def wait_for_command(bench):
    if bench.service._fix_proc is not None:
        bench.service._fix_proc.wait(timeout=10)
    bench.run(0.25)                          # the next round picks up its exit status


def run_watching_starts(bench, seconds):
    """Run in simulated time; return the reasons the receiver command was started for."""
    starts = []
    for _ in range(int(seconds)):
        runs = bench.service._fix["runs"]
        bench.run(1)
        if bench.service._fix["runs"] != runs:
            starts.append(bench.service._fix["last_why"])
            wait_for_command(bench)
    return starts


def test_gpsd_without_identification_is_reported_and_the_command_retried_with_growing_waits(tmp_path, caplog):
    caplog.set_level("INFO")
    marker = tmp_path / "ran"
    bench = retry_bench(tmp_path, f"/bin/sh -c 'echo x >> {marker}; exit 2'")
    gps = bench.snap()["sources"]["gps"]
    assert gps["health"] == "ok" and gps["receiver_identified"] is True and gps["no_position"] == ""
    assert gps["receiver_check"] == {"runs": 1, "running": False, "last_status": 2, "last_why": "the OLA has just started",
                                     "error": "", "next_retry_at_no_position_s": None}
    # What 2026-11-19 looked like: gpsd has the receiver, fix times come, but mode 0 and no position.
    conn = bench.gpsd.connections[-1]
    conn.devices[0].update(subtype="", subtype1="")
    conn.tpv.update(mode=0, lat=None, lon=None)
    bench.run(8)
    gps = bench.snap()["sources"]["gps"]
    assert gps["health"] == "ok" and gps["reason"] == "no position fix" and gps["no_position"] == ""
    assert gps["receiver_identified"] is False and 7 <= gps["no_position_s"] <= 9
    bench.run(4)
    data = bench.snap()
    gps = data["sources"]["gps"]
    assert gps["health"] == "error" and gps["no_position"] == "gpsd_unidentified" and "gps: error" in data["problems"]
    assert "gpsd has not identified the receiver (no version reply seen)" in gps["reason"]
    assert "no position for 1" in gps["reason"]
    assert caplog.text.count("GPS: gpsd has a receiver but no position for") == 1
    # Retried at 30 s without a position, then after 60, 120, 120 s (the maximum).
    starts = run_watching_starts(bench, 335)
    assert starts == [f"no position for {s} s" for s in (30, 90, 210, 330)]
    assert marker.read_text() == "x\n" * 5
    check = bench.snap()["sources"]["gps"]["receiver_check"]
    assert check["runs"] == 5 and check["last_status"] == 2 and check["last_why"] == "no position for 330 s"
    assert check["next_retry_at_no_position_s"] == pytest.approx(450, abs=2)
    assert "GPS receiver check finished with status 2 (gpsd has not identified the receiver)" in caplog.text
    assert caplog.text.count("GPS: gpsd has a receiver but no position for") == 1      # warned once
    # gpsd identifies the receiver and the positions follow.
    conn.devices = [dict(conn.devices[0], subtype="SW EXT CORE 1.00", subtype1="PROTVER=27.50,MOD=ZED-F9P")]
    conn.tpv.update(mode=3, lat=40.0, lon=-120.0)
    assert run_watching_starts(bench, 12) == []
    data = bench.snap()
    gps = data["sources"]["gps"]
    assert gps["health"] == "ok" and gps["reason"] == "" and gps["no_position"] == "" and data["problems"] == []
    assert gps["receiver_identified"] is True and gps["receiver_check"]["next_retry_at_no_position_s"] is None
    assert "GPS: position again, after 3" in caplog.text
    assert run_watching_starts(bench, 300) == []


def test_identified_but_mode_0_is_an_error_too(tmp_path, caplog):
    """gpsd identified, but the receiver sends no NAV-PVT (its enable lost): fix times, mode 0."""
    caplog.set_level("INFO")
    bench = retry_bench(tmp_path, "/bin/true")
    conn = bench.gpsd.connections[-1]
    conn.tpv.update(mode=0, lat=None, lon=None)
    starts = run_watching_starts(bench, 35)
    data = bench.snap()
    gps = data["sources"]["gps"]
    assert gps["receiver_identified"] is True and gps["no_position"] == "no_position_messages"
    assert gps["health"] == "error" and "gpsd reports mode 0" in gps["reason"] and "gps: error" in data["problems"]
    assert "the receiver sends gpsd no position message (NAV-PVT off)" in caplog.text
    assert starts == ["no position for 30 s"]
    # No mode at all in gpsd's answer counts the same.
    conn.tpv.pop("mode")
    bench.run(2)
    assert bench.snap()["sources"]["gps"]["no_position"] == "no_position_messages"


def test_no_satellites_is_not_called_a_fault_and_the_retry_is_harmless(tmp_path, caplog):
    caplog.set_level("INFO")
    bench = retry_bench(tmp_path, "/bin/true")
    conn = bench.gpsd.connections[-1]
    conn.tpv.update(mode=1, lat=None, lon=None)     # NAV-PVT without a fix: an antenna indoors
    starts = run_watching_starts(bench, 95)
    gps = bench.snap()["sources"]["gps"]
    assert gps["health"] == "ok" and gps["reason"].startswith("no position fix for 9")
    assert gps["no_position"] == "no_fix" and gps["receiver_identified"] is True
    assert "gpsd gets the position message without a fix: most likely no satellites" in caplog.text
    assert len(starts) == 2 and gps["receiver_check"]["last_status"] == 0
    # gpsd going away ends the count without a position, and it starts again.
    conn.eof = True
    bench.run(2)
    assert "GPS: no position for 9" in caplog.text and "counted again from now" in caplog.text
    assert bench.snap()["sources"]["gps"]["no_position"] == ""


def test_a_timeless_mode_0_reply_does_not_change_the_cause(tmp_path, caplog):
    """A POLL between gpsd clearing its fix and the next message: no time, mode 0 (3 of 1227 rows on 2026-11-19)."""
    caplog.set_level("INFO")
    bench = retry_bench(tmp_path, "/bin/true")
    conn = bench.gpsd.connections[-1]
    conn.tpv.update(mode=1, lat=None, lon=None)
    bench.run(8)
    conn.tpv.update(mode=0, time=None)      # such replies just as the warning is due
    bench.run(3)
    gps = bench.snap()["sources"]["gps"]
    assert gps["values"]["mode"] == 0 and gps["values"]["gps_time"] is None
    assert gps["no_position"] == "no_fix" and gps["health"] == "ok"
    assert "most likely no satellites" in caplog.text and "NAV-PVT off" not in caplog.text
    conn.tpv.update(mode=1, time="2026-11-19T01:00:00.000Z")
    bench.run(2)
    assert bench.snap()["sources"]["gps"]["no_position"] == "no_fix"


def test_no_fix_time_either_is_no_data(tmp_path):
    bench = retry_bench(tmp_path, "/bin/true")
    conn = bench.gpsd.connections[-1]
    conn.advance = False                    # gpsd repeats its last fix: nothing new from the receiver
    bench.run(40)
    gps = bench.snap()["sources"]["gps"]
    assert gps["no_position"] == "no_data" and gps["health"] == "error" and "no fix data for" in gps["reason"]


def test_retry_interval_has_a_floor(tmp_path):
    bench = retry_bench(tmp_path, "/bin/true", gps__no_position_retry_s=0.0, gps__no_position_retry_max_s=0.0)
    bench.gpsd.connections[-1].tpv.update(mode=1, lat=None, lon=None)
    starts = run_watching_starts(bench, 100)
    assert starts == [f"no position for {s} s" for s in (30, 60, 90)]


def test_receiver_command_that_hangs_is_stopped_with_its_children(tmp_path, caplog):
    caplog.set_level("INFO")
    child = tmp_path / "child.pid"
    bench = Bench(tmp_path, gps__receiver_fix_command=f"/bin/sh -c 'sleep 60 & echo $! > {child}; wait'",
                  gps__receiver_fix_timeout_s=5.0)
    svc = bench.service
    svc._start_fix(0.0, "test")
    for _ in range(100):
        if child.exists() and child.read_text().strip():
            break
        time.sleep(0.05)
    pid = int(child.read_text())
    svc._reap_fix(4.0)
    assert svc._fix_proc is not None and svc._fix["running"]
    svc._reap_fix(6.0)                      # stopped: never waited for here
    assert "GPS receiver check still running after 5 s: stopped" in caplog.text
    svc._fix_proc.wait(timeout=5)
    svc._reap_fix(6.25)
    assert svc._fix_proc is None and svc._fix["last_status"] == -9 and not svc._fix["running"]
    for _ in range(100):                    # the child (as ubxtool would be) is gone too
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the command's child was left running")


@pytest.mark.parametrize("command", ["/bin/sh -c 'unbalanced", "/nonexistent/command"])
def test_broken_receiver_command_never_touches_the_amplifier_readings(tmp_path, caplog, command):
    caplog.set_level("INFO")
    bench = Bench(tmp_path, gps__receiver_fix_command=command)
    bench.keep_streaming(60)
    started(bench, 40)                       # the OLA start's run of the command was due at about 27 s
    data = bench.run(10)
    assert data["amp"]["fresh"] is True and data["sources"]["ola"]["health"] == "ok"
    assert "GPS receiver check could not start" in caplog.text
    assert bench.service._fix["runs"] == 1 and bench.service._fix_proc is None


def test_failure_in_the_receiver_step_is_contained(tmp_path, monkeypatch):
    bench = Bench(tmp_path)
    bench.keep_streaming(60)
    started(bench, 20)

    def broken(mono):
        raise RuntimeError("receiver bug")
    monkeypatch.setattr(bench.service, "_step_receiver", broken)
    data = bench.run(10)
    assert data["amp"]["fresh"] is True and data["sources"]["ola"]["health"] == "ok"
    assert data["sources"]["gps"]["receiver_check"]["error"] == "internal error: RuntimeError: receiver bug"


def test_empty_receiver_command_never_runs(tmp_path):
    bench = retry_bench(tmp_path, "/bin/true")
    bench.service.cfg["gps"]["receiver_fix_command"] = ""
    bench.gpsd.connections[-1].tpv.update(mode=1, lat=None, lon=None)
    assert run_watching_starts(bench, 100) == []


def test_no_position_defaults():
    gps = C.DEFAULTS["telemetry"]["gps"]
    assert (gps["no_position_warn_s"], gps["no_position_retry_s"], gps["no_position_retry_max_s"]) == (60.0, 120.0, 1800.0)
    assert gps["receiver_fix_timeout_s"] == 180.0


def test_boot_number_comes_from_the_counter_and_the_existing_directories(tmp_path):
    config = copy.deepcopy(C.DEFAULTS)
    for d in ("run", "state", "drive/telemetry/b00041", "card"):
        (tmp_path / d).mkdir(parents=True)
    config["paths"].update(run_dir=str(tmp_path / "run"), state_dir=str(tmp_path / "state"))
    config["telemetry"].update(data_root=str(tmp_path / "drive"), data_mount="-", fallback_dir=str(tmp_path / "card"))
    ports = Ports()
    ports.missing = True

    def service():
        s = Telemetry(config, notifier=Notifier(), ola=O.Ola(config["telemetry"]["ola"], open_port=ports),
                      gps=G.Gps(config["telemetry"]["gps"], connect=GpsdServer()))
        s.setup()
        s.cleanup()
        return s
    first = service()
    assert first.boot == 42 and first.boot_problem == ""            # one more than the data drive has seen
    assert service().boot == 42                                     # a restart in the same boot keeps the number
    assert json.loads((tmp_path / "state" / "boot_count.json").read_text())["count"] == 42
    assert os.path.isdir(tmp_path / "drive" / "telemetry" / "b00042")


# ---- as a real process

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_process(tmp_path, extra=""):
    for d in ("run", "state", "drive"):
        (tmp_path / d).mkdir(exist_ok=True)
    (tmp_path / "local.yaml").write_text(f"""
paths:
  run_dir: {tmp_path}/run
  state_dir: {tmp_path}/state
  power_status_file: {tmp_path}/power.json
telemetry:
  data_root: {tmp_path}/drive
  data_mount: "-"
  fallback_dir: {tmp_path}/card
  flush_interval_s: 0.2
  ola:
    port: {tmp_path}/no-such-port
  gps:
    port: {free_port()}
{extra}""")
    env = dict(os.environ, PYTHONPATH=REPO)
    env.pop("NOTIFY_SOCKET", None)
    return subprocess.Popen([sys.executable, "-B", "-u", "-m", "radar_runner.telemetry", "--local-config",
                             str(tmp_path / "local.yaml"), "--secrets", str(tmp_path / "none.yaml")],
                            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for_status(tmp_path, proc, condition, timeout=20):
    deadline = time.monotonic() + timeout
    read = None
    while time.monotonic() < deadline:
        read = S.read_status(str(tmp_path / "run" / "telemetry.json"), 3.0)
        if read.ok and read.data["pid"] == proc.pid and condition(read.data):
            return read.data
        time.sleep(0.1)
    raise AssertionError(f"status never satisfied the condition: {read}")


def test_process_runs_without_ola_and_gpsd_and_stops_cleanly(tmp_path):
    proc = start_process(tmp_path)
    try:
        data = wait_for_status(tmp_path, proc, lambda d: d["logging"]["rows_written"].get("pi", 0) >= 2)
        assert data["sources"]["ola"]["health"] == "missing" and data["sources"]["gps"]["health"] == "missing"
        assert data["sources"]["ups"]["health"] == "missing" and data["sources"]["pi"]["health"] in ("ok", "error")
        assert data["amp"]["fresh"] is False and data["state"] == "degraded"
        assert data["boot_count"] == 1 and data["logging"]["location"] == "primary"
        assert data["logging"]["writer"] == "running" and data["pid"] == proc.pid
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    assert "telemetry service stopping" in out and "Traceback" not in out
    names = sorted(n for n in os.listdir(tmp_path / "drive" / "telemetry" / "b00001") if n.endswith(".csv"))
    assert [n.split("_")[0] for n in names] == ["pi", "ups"]
    for name in names:                      # everything written out at the stop, in whole lines
        data = (tmp_path / "drive" / "telemetry" / "b00001" / name).read_bytes()
        assert data.endswith(b"\n") and data.count(b"\n") >= 3


def test_process_killed_mid_write_leaves_readable_files_and_restarts_with_new_ones(tmp_path):
    proc = start_process(tmp_path)
    try:
        wait_for_status(tmp_path, proc, lambda d: d["logging"]["rows_written"].get("ups", 0) >= 3)
    finally:
        proc.kill()                         # as abrupt as a process can end
        proc.communicate(timeout=20)
    directory = tmp_path / "drive" / "telemetry" / "b00001"
    first = sorted(n for n in os.listdir(directory) if n.endswith(".csv"))
    for name in first:
        lines = (directory / name).read_bytes().split(b"\n")
        assert lines[0].startswith(b"time_utc,uptime_s,") and lines[-1] == b""
        assert all(line.count(b",") == lines[0].count(b",") for line in lines[:-1])
    proc = start_process(tmp_path)
    try:
        data = wait_for_status(tmp_path, proc, lambda d: d["logging"]["rows_written"].get("ups", 0) >= 2)
        assert data["boot_count"] == 1
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=20)
    names = sorted(n for n in os.listdir(directory) if n.endswith(".csv"))
    assert [n for n in names if n.startswith("ups_")] == [f"ups_b00001_0000{i}_{first[-1][-12:]}" for i in (1, 2)]


def test_process_uses_the_fallback_when_the_drive_is_unusable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    (tmp_path / "drive").mkdir()
    (tmp_path / "drive").chmod(0o555)
    proc = start_process(tmp_path)
    try:
        data = wait_for_status(tmp_path, proc, lambda d: d["logging"]["rows_written"].get("pi", 0) >= 1)
        assert data["logging"]["location"] == "fallback" and "Permission denied" in data["logging"]["reason"]
        assert data["logging"]["directory"] == f"{tmp_path}/card/b00001"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=20)
        (tmp_path / "drive").chmod(0o755)
    assert proc.returncode == 0 and os.listdir(tmp_path / "card" / "b00001")
