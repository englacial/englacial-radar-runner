"""The OLA connection (radar_runner/telemetry/ola.py) against a replayed real session and failure cases."""
import copy

import pytest
from telemetry_fakes import (CONFIGURED_HEADER, DEFAULT_HEADER, FakeSerial, Ports, configured_row,
                             load_session)

from radar_runner.common import config as C
from radar_runner.telemetry import ola as O

CFG = C.DEFAULTS["telemetry"]["ola"]
BANNER = ("Artemis OpenLog v2.11\r\nSD card offline\r\nIMU online\r\nIdentifying Qwiic Devices...\r\n"
          "ADC-ADS1015 online at address 0x4B\r\nContent-Type: text/csv\r\n\r\n")


class Bench:
    """An Ola on fake ports, in simulated time: each step is one poll, 0.25 s apart."""

    def __init__(self, *ports, **overrides):
        self.t = 0.0
        self.ports = Ports(*ports)
        self.ola = O.Ola(dict(copy.deepcopy(CFG), **overrides), open_port=self.ports, exists=self.ports.exists,
                         monotonic=lambda: self.t)
        self.rows = []
        self.opened_at = []

    def run(self, seconds, until=None, chunks=1):
        for _ in range(int(seconds / 0.25)):
            self.t += 0.25
            self.ports.release(chunks)
            opens = self.ola.opens
            self.rows += self.ola.poll()
            if self.ola.opens != opens:
                self.opened_at.append(self.t)
            if until and until():
                return True
        return until is None

    def configured(self):
        """Run the recorded session to the end of the menus and the rows after them."""
        port = self.ports.ports[0] if self.ports.ports else self.ports.opened[-1]
        assert self.run(80, until=lambda: self.ola.configured and not port.script)
        return port


def session_port():
    return FakeSerial(load_session())


def default_row(n):
    return configured_row(n=n).replace(",9.5,", ",1.0,9.5,")       # five ADC fields: the default columns


def test_bench_formulas():
    # The values pa_run.py computes: temp mv*2/10, current mv*2/500*1.5, voltage mv*11/1000.
    assert O.amp_values(CFG, 140.0, 26.0, 2542.0) == pytest.approx((28.0, 0.156, 27.962))
    assert O.amp_values(CFG, 264.0, 430.0, 2500.0) == pytest.approx((264 * 2 / 10.0, 430 * 2 / 500.0 * 1.5, 27.5))
    cal = dict(CFG, current_cal=1.0, temp_mv_per_c=20.0)
    assert O.amp_values(cal, 140.0, 26.0, 2542.0) == pytest.approx((14.0, 0.104, 27.962))


def test_recorded_session_configures_and_streams():
    port = session_port()
    b = Bench(port)
    assert b.run(60, until=lambda: not port.script)
    b.run(1)
    ola = b.ola
    # Exactly the key presses of the recorded ola_setup.py run, numbers read from the menus.
    assert b"".join(port.all_written) == b"\r" + b"6" + b"3\r" + b"2\r3\r4\r5\r7\r8\r9\r" + b"x" + b"x" + b"h"
    assert ola.state == O.STREAMING and ola.configured and ola.error == ""
    assert ola.opens == 1 and ola.configures == 1 and ola.bad_lines == 0 and ola.restarts == 0
    assert ola.columns[-4:] == ("A0A3mV", "A1A3mV", "A2A3mV", "output_Hz")
    assert len(b.rows) >= 20 and len(b.rows) == ola.rows
    for row in b.rows:              # only rows after the menus, all with the differential columns
        assert row["columns"] == ola.columns and len(row["fields"]) == len(ola.columns)
        assert row["amp"]["amp_temp_c"] == pytest.approx(float(row["fields"][-4]) * 0.2)
        assert 27.0 < row["amp"]["amp_voltage_v"] < 29.0 and 0.1 < row["amp"]["amp_current_a"] < 0.2
        assert 0.0 <= row["age_s"] <= 0.5
    first = b.rows[0]
    assert first["fields"][0] == "01/01/2000" and first["fields"][-4:-1] == ["140.0", "26.0", "2542.0"]
    assert first["amp"] == pytest.approx({"amp_temp_c": 28.0, "amp_current_a": 0.156, "amp_voltage_v": 27.962})
    assert ola.retry_state() == {"failures_in_a_row": 0, "waiting_for": None, "next_attempt_in_s": None,
                                 "reopen_wait_s": 5.0, "configure_wait_s": 30.0}


def test_menu_dialogue_takes_one_step_per_poll_and_never_sleeps():
    port = session_port()
    b = Bench(port)
    writes_per_poll = []
    while port.script:
        before = len(port.all_written)
        b.run(0.25)
        writes_per_poll.append(len(port.all_written) - before)
        assert b.t < 60
    assert max(writes_per_poll) == 1 and sum(writes_per_poll) == 13


# ---- how old a row is

def test_every_poll_reads_the_port_until_it_is_empty():
    b = Bench(session_port())
    port = b.configured()
    for n in range(40):
        port.feed(configured_row(n=n))
    reads = port.reads
    b.run(0.25, chunks=40)                  # 40 chunks have arrived: one poll takes them all
    assert port.reads - reads == 41 and b.ola.drained       # 40 with data, and the one that came back empty
    assert [r["fields"][1] for r in b.rows[-40:]] == [configured_row(n=n).split(",")[1] for n in range(40)]


def test_backlog_after_a_stall_is_dated_back_not_stamped_now():
    b = Bench(session_port())
    port = b.configured()
    b.rows.clear()
    # The loop stalls for 25 s while the OLA keeps sending ten rows a second: 250 rows wait in the tty.
    for n in range(250):
        port.feed(configured_row(n=n))
    b.t += 25.0
    b.run(0.25, chunks=1000)
    assert len(b.rows) == 250 and b.ola.drained
    assert b.ola.window_s == pytest.approx(25.25)           # all that is certain: they arrived within this
    ages = [r["age_s"] for r in b.rows]
    assert ages[-1] == 0.0                                  # the newest is taken as now
    assert ages[0] == pytest.approx(24.9)                   # the others by the OLA's own clock: 0.1 s apart
    assert ages == sorted(ages, reverse=True)
    assert all(a - c == pytest.approx(0.1, abs=1e-6) for a, c in zip(ages, ages[1:]))
    # The next round is an ordinary one again.
    port.feed(configured_row(n=250), configured_row(n=251))
    b.run(0.25, chunks=2)
    assert [round(r["age_s"], 2) for r in b.rows[-2:]] == [0.1, 0.0] and b.ola.window_s == pytest.approx(0.25)


def test_rows_never_go_back_in_time_and_a_useless_ola_clock_is_replaced_by_even_spacing():
    b = Bench(session_port())
    port = b.configured()
    b.rows.clear()
    port.feed(configured_row(n=0))
    b.run(0.25)
    # The OLA's clock jumps (it was set, or wrapped): ten rows whose clock says they are hours apart.
    for n in range(10):
        port.feed(configured_row(n=n * 36000))
    b.t += 5.0
    b.run(0.25, chunks=100)
    ages = [r["age_s"] for r in b.rows[-10:]]
    assert ages[-1] == 0.0 and ages[0] == pytest.approx(9 * 5.25 / 10)      # spread evenly over the window
    assert ages == sorted(ages, reverse=True)
    # A row in the next poll that its clock puts before the last one returned is held at that one's time.
    port.feed(configured_row(n=9 * 36000 - 9), configured_row(n=9 * 36000 + 1))     # 1.0 s apart by its clock
    b.run(0.25, chunks=2)
    assert b.rows[-2]["age_s"] == pytest.approx(0.25)       # not 1.0: never before the poll before
    assert b.rows[-1]["age_s"] == 0.0


def test_poll_that_cannot_empty_the_port_holds_its_rows_back(monkeypatch):
    b = Bench(session_port())
    port = b.configured()
    b.rows.clear()
    monkeypatch.setattr(O, "MAX_POLL_BYTES", 2000)          # about ten rows per poll
    for n in range(100):
        port.feed(configured_row(n=n))
    b.t += 10.0
    polls = 0
    while not b.rows:
        b.run(0.25, chunks=1000)
        polls += 1
        if not b.rows:
            assert not b.ola.drained                        # more is waiting: nothing is handed out as recent
    assert 5 <= polls <= 12 and len(b.rows) == 100 and b.ola.drained
    assert b.ola.window_s == pytest.approx(10.0 + 0.25 * polls)     # since the port was last read empty
    assert b.rows[0]["age_s"] == pytest.approx(9.9) and b.rows[-1]["age_s"] == 0.0


def test_rows_held_back_too_long_are_dropped_and_counted(monkeypatch):
    b = Bench(session_port())
    port = b.configured()
    b.rows.clear()
    monkeypatch.setattr(O, "MAX_POLL_BYTES", 2000)
    monkeypatch.setattr(O, "MAX_HELD_ROWS", 30)
    for n in range(100):
        port.feed(configured_row(n=n))
    b.run(5, chunks=1000)
    assert len(b.rows) == 30 and b.ola.dropped_rows == 70
    assert b.rows[-1]["fields"][1] == configured_row(n=99).split(",")[1]       # the newest are kept


# ---- failures and recovery

def test_port_missing_at_start_then_appearing():
    port = session_port()
    b = Bench(port)
    b.ports.missing = True
    b.run(12)
    assert b.ola.state == O.CLOSED and "cannot open the port" in b.ola.error and b.ola.opens == 0
    assert not b.ola.port_present
    b.ports.missing = False
    b.run(0.25)
    assert b.ola.opens == 1                                 # at once when the device appears
    assert b.run(70, until=lambda: b.ola.configured)


def test_port_disappears_and_returns():
    first, second = session_port(), session_port()
    b = Bench(first, second)
    b.configured()
    first.fail_read = OSError(5, "Input/output error")          # the USB adapter is unplugged
    b.run(1)
    assert b.ola.state == O.CLOSED and not b.ola.configured and "port lost" in b.ola.error
    assert first.closed
    b.ports.missing = True
    b.run(20)
    assert b.ola.state == O.CLOSED and "cannot open" in b.ola.error
    b.ports.missing = False
    rows_before = len(b.rows)
    b.run(0.25)
    assert b.ola.opens == 2                                     # plugged in again: opened at once
    assert b.run(80, until=lambda: b.ola.configured)            # and configured again
    b.run(5)
    assert b.ola.configures == 2 and len(b.rows) > rows_before


def test_ola_that_does_not_answer_its_menus():
    port = FakeSerial()
    port.feed(DEFAULT_HEADER + "\r\n")
    b = Bench(port, FakeSerial())
    # It streams rows but ignores the key presses.
    for n in range(400):
        port.feed(default_row(n))
    b.run(10)
    assert b.ola.state == O.CONFIGURING and b.rows == []
    b.run(10)
    assert b.ola.state == O.STREAMING and not b.ola.configured
    assert "did not answer (waiting for the main menu)" in b.ola.error
    assert b.rows and all(row["amp"] is None for row in b.rows)         # still read and logged, without amp values
    assert b.ola.opens == 1                                             # not reset while it is streaming
    state = b.ola.retry_state()
    assert state["waiting_for"] == "setting the ADS1015 again" and state["failures_in_a_row"] == 1
    assert 20 < state["next_attempt_in_s"] <= 30 and state["configure_wait_s"] == 60.0
    writes = len(port.all_written)
    b.run(31)
    assert len(port.all_written) == writes + 1                          # the next try, 30 s later


def test_silent_ola_is_not_reset_in_a_loop():
    ports = [FakeSerial() for _ in range(40)]
    for p in ports:
        p.silent = True
    b = Bench(*ports)
    b.run(1500)
    gaps = [round(later - earlier) for earlier, later in zip(b.opened_at, b.opened_at[1:])]
    # Each try: 25 s without a header, 15 s for a menu that does not come, 20 s without a row. Then the pause.
    assert gaps[:6] == [66, 71, 81, 101, 141, 221]
    state = b.ola.retry_state()
    assert state["failures_in_a_row"] >= 12 and state["reopen_wait_s"] >= 320
    b.run(12 * 3600 - 1500)
    assert len(b.opened_at) <= 26                           # half a day: about two an hour at first, then one
    gaps = [later - earlier for earlier, later in zip(b.opened_at, b.opened_at[1:])]
    assert max(gaps) == pytest.approx(3661, abs=1) and gaps[-1] == pytest.approx(3661, abs=1)
    assert all(p.closed for p in b.ports.opened[:-1])
    state = b.ola.retry_state()
    assert state["reopen_wait_s"] == 3600.0 and state["configure_wait_s"] == 3600.0


def test_waits_start_short_again_only_after_a_stable_period():
    ports = [FakeSerial() for _ in range(4)]
    for p in ports:
        p.silent = True
    good = session_port()
    b = Bench(*ports, good)
    assert b.run(1200, until=lambda: b.ola.configured)      # the fifth open reaches a working OLA
    state = b.ola.retry_state()
    assert state["failures_in_a_row"] == 8 and state["reopen_wait_s"] == 80.0 and state["configure_wait_s"] == 480.0
    for n in range(4000):
        good.feed(configured_row(n=n))
    b.run(250, chunks=1)
    assert b.ola.retry_state()["failures_in_a_row"] == 8    # not yet: 300 s of good rows are asked for
    b.run(60, chunks=1)
    assert b.ola.retry_state() == {"failures_in_a_row": 0, "waiting_for": None, "next_attempt_in_s": None,
                                   "reopen_wait_s": 5.0, "configure_wait_s": 30.0}


def test_a_failure_before_the_stable_period_keeps_the_longer_wait():
    silent = FakeSerial()
    silent.silent = True
    first, second, third = session_port(), session_port(), session_port()
    b = Bench(silent, first, second, third)
    assert b.run(300, until=lambda: b.ola.configured)       # second open works, for a few seconds
    assert b.run(60, until=lambda: b.ola.state == O.CLOSED)
    state = b.ola.retry_state()
    assert state["waiting_for"] == "reopening the port (resets the OLA)" and state["failures_in_a_row"] == 3
    assert 9 < state["next_attempt_in_s"] <= 10 and state["reopen_wait_s"] == 20.0      # 5 s, then 10 s, next 20 s


def test_device_that_drops_off_and_returns_within_seconds_waits_like_any_failure():
    ports = [session_port() for _ in range(30)]
    b = Bench(*ports)
    for cycle in range(12):
        # Each time it is opened it streams for a moment, then falls off USB for 2 s and comes back.
        assert b.run(4000, until=lambda: b.ola.state != O.CLOSED)
        b.run(3)
        b.ports.opened[-1].fail_read = OSError(5, "Input/output error")
        b.run(0.25)
        b.ports.missing = True
        b.run(2)
        b.ports.missing = False
    gaps = [round(later - earlier) for earlier, later in zip(b.opened_at, b.opened_at[1:])]
    assert gaps[0] <= 6                                     # the first return is taken as a replug
    assert gaps[1:7] == [13, 23, 43, 83, 163, 323]          # after that: the growing wait, whatever it does


def test_ola_that_goes_silent_while_streaming_is_reset():
    first, second = session_port(), session_port()
    b = Bench(first, second)
    b.configured()
    b.run(19)
    assert b.ola.opens == 1
    b.run(3)
    assert b.ola.state == O.CLOSED and "no valid row for 20 s" in b.ola.error
    assert b.run(80, until=lambda: b.ola.configured) and b.ola.opens == 2


def test_ola_restart_seen_by_its_banner_is_set_up_again_on_the_same_connection():
    port = session_port()
    b = Bench(port)
    b.configured()
    assert b.ola.configures == 1
    rows = len(b.rows)
    # The OLA resets by itself: banner, default header, default rows. Then it answers the menus again.
    port.script.extend(load_session())
    assert b.run(5, until=lambda: not b.ola.configured)
    assert b.ola.restarts == 1 and b.ola.state in (O.BOOTING, O.CONFIGURING)
    assert b.run(60, until=lambda: b.ola.configured)
    b.run(10)
    assert b.ola.opens == 1 and b.ola.configures == 2           # same connection, set a second time
    assert len(b.rows) > rows and all(row["amp"] is not None for row in b.rows)


def test_ola_restart_with_its_new_header_lost_recovers():
    """The reviewer's case: the OLA resets, the header line is lost, and rows with the default columns follow."""
    first, second = session_port(), session_port()
    b = Bench(first, second)
    b.configured()
    rows = len(b.rows)
    for n in range(3000):                                   # 28 fields where the header in use has 27
        first.feed(default_row(n))
    b.run(19)
    assert b.ola.state == O.STREAMING and b.ola.configured and len(b.rows) == rows      # none of them is a valid row
    assert b.ola.bad_lines >= 70
    b.run(2)
    assert b.ola.state == O.CLOSED and "no valid row for 20 s" in b.ola.error            # bytes kept coming all along
    assert b.run(80, until=lambda: b.ola.configured)
    assert b.ola.opens == 2 and b.ola.configures == 2
    b.run(5)
    assert len(b.rows) > rows and b.rows[-1]["amp"] is not None


def test_ola_restart_with_banner_seen_and_header_damaged_recovers_without_a_reset():
    port = session_port()
    b = Bench(port)
    b.configured()
    port.feed(BANNER, "rtcDat\xff\xfe,garbled\r\n")         # the banner arrives, the header does not
    for n in range(95):                                     # then rows, ten a second
        port.feed("".join(default_row(n * 3 + k) for k in range(3)))
    session = load_session()
    start = next(i for i, (kind, data) in enumerate(session) if kind == "tx")
    port.script.extend(session[start:])                     # and it answers the menus as recorded
    b.run(3)
    assert b.ola.restarts == 1 and b.ola.state == O.BOOTING and not b.ola.configured
    b.run(21)
    assert b.ola.state == O.BOOTING                         # no header: waits out boot_timeout_s
    assert b.run(60, until=lambda: b.ola.configured)        # then drives the menus anyway
    assert b.ola.opens == 1 and b.ola.configures == 2


def test_garbage_that_keeps_coming_is_not_mistaken_for_a_working_ola():
    first, second = session_port(), session_port()
    b = Bench(first, second)
    b.configured()
    for _ in range(400):
        first.feed(b"\x00\xff\x13garbage line\r\n")
    b.run(19)
    assert b.ola.opens == 1 and b.ola.bad_lines >= 70
    b.run(2)
    assert b.ola.state == O.CLOSED and "no valid row" in b.ola.error


def test_open_that_did_not_reset_the_ola_still_runs_the_menus():
    port = FakeSerial()
    for n in range(110):                                    # rows at once, no banner and no header
        port.feed(default_row(n))
    session = load_session()
    start = next(i for i, (kind, data) in enumerate(session) if kind == "tx")
    port.script.extend(session[start:])
    b = Bench(port)
    b.run(24)
    assert b.ola.state == O.BOOTING and port.all_written == []      # rows alone start nothing
    b.run(2)
    assert port.all_written == [b"\r"]                              # after boot_timeout_s the menus are driven
    assert b.run(60, until=lambda: b.ola.configured) and b.ola.opens == 1


def test_header_reprinted_with_the_same_columns_changes_nothing():
    b = Bench(session_port())
    port = b.configured()
    writes = len(port.all_written)
    port.feed(CONFIGURED_HEADER + "\r\n", configured_row(n=1), configured_row(n=2))
    b.run(2)
    assert b.ola.configured and len(port.all_written) == writes and b.rows[-1]["amp"] is not None


def test_garbage_and_partial_lines_are_dropped_and_counted():
    b = Bench(session_port())
    port = b.configured()
    b.run(1)
    good = len(b.rows)
    whole = configured_row(n=3)
    port.feed(whole[:60])                                   # a row in two parts: one row
    port.feed(whole[60:])
    port.feed(b"\xff\xfe\x00garbage\r\n")                   # counted
    port.feed(whole[:80] + "\r\n")                          # cut off: too few fields
    port.feed(whole.replace("140.0", "1x0.0"))              # not a number in an ADC column
    port.feed(whole.replace("26.0", "nan"))
    port.feed(whole[:50] + whole)                           # two rows run together
    port.feed("\r\n\r\n")                                   # empty lines are nothing
    port.feed(configured_row(n=4))
    b.run(3)
    assert len(b.rows) == good + 2
    assert b.ola.bad_lines == 5
    assert b.ola.configured and b.ola.state == O.STREAMING


def test_a_very_long_line_without_end_does_not_grow_the_buffer():
    b = Bench(session_port())
    port = b.configured()
    for _ in range(40):
        port.feed(b"x" * 1000)
    b.run(12)
    assert len(b.ola._buf) <= O.MAX_LINE and b.ola.bad_lines >= 1
    port.feed("\r\n", configured_row(n=5))
    rows = len(b.rows)
    b.run(1)
    assert len(b.rows) == rows + 1


def test_columns_are_found_by_name():
    port = FakeSerial()
    # Another column order and an extra sensor, as after a change of the OLA's devices.
    header = "rtcDate,rtcTime,A2A3mV,degC,extra,A0A3mV,degC,A1A3mV,"
    port.feed(header + "\r\n")
    port.expect(b"\r")
    port.feed("Menu: Main Menu\r\n6) Detect / Configure Attached Devices\r\nx) Return to logging\r\n")
    port.expect(b"6")
    port.feed("Menu: Configure Attached Devices\r\n1) u-blox\r\n9) ADS1015 ADC (0x48)\r\nx) Exit\r\n")
    port.expect(b"9\r")
    port.feed("Menu: Configure ADS1015 ADC\r\n1) Sensor Logging: Enabled\r\n2) Log A0: Disabled\r\n"
              "4) Log A0-A3: Enabled\r\n5) Log A1-A3: Enabled\r\n6) Log A2-A3: Enabled\r\n8) Gain x1: Enabled\r\nx) Exit\r\n")
    port.expect(b"x")
    port.feed("Menu: Configure Attached Devices\r\n9) ADS1015 ADC (0x48)\r\nx) Exit\r\n")
    port.expect(b"x")
    port.feed("Menu: Main Menu\r\nx) Return to logging\r\n")
    port.expect(b"h")
    port.feed(header + "\r\n", "01/01/2000,00:00:01.00,2500.0,21.5,7,100.0,22.5,50.0,\r\n")
    b = Bench(port)
    assert b.run(30, until=lambda: b.rows)
    assert b.ola.columns == ("rtcDate", "rtcTime", "A2A3mV", "degC", "extra", "A0A3mV", "degC_2", "A1A3mV")
    assert b.rows[0]["amp"] == pytest.approx({"amp_temp_c": 20.0, "amp_current_a": 0.3, "amp_voltage_v": 27.5})


def test_missing_adc_is_reported_not_retried_in_a_tight_loop():
    port = FakeSerial()
    port.feed(DEFAULT_HEADER + "\r\n")
    port.expect(b"\r")
    port.feed("Menu: Main Menu\r\n6) Detect / Configure Attached Devices\r\nx) Return to logging\r\n")
    port.expect(b"6")
    port.feed("Menu: Configure Attached Devices\r\n1) u-blox GPS Receiver (0x42)\r\nx) Exit\r\n")
    b = Bench(port)
    b.run(10)
    assert "not configured: menu has no option matching" in b.ola.error and "ADS1015" in b.ola.error
    writes = len(port.all_written)
    b.run(15)
    assert len(port.all_written) == writes
