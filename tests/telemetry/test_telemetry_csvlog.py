"""Sensor log files (radar_runner/telemetry/csvlog.py) and the boot counter (radar_runner/common/bootcount.py)."""
import csv
import errno
import json
import os
import re
import threading
import time

import pytest

from radar_runner.common import bootcount as B
from radar_runner.telemetry import csvlog as L

DAY1 = 1795046400.0             # 2026-11-19T00:00:00Z
COLS = ("a", "b")


class Store:
    """A LogStore on two scratch directories ("drive" and "card"), in simulated time."""

    def __init__(self, tmp_path, boot=7, boot_id="this-boot", **kw):
        self.drive, self.card = tmp_path / "drive", tmp_path / "card"
        self.drive.mkdir(exist_ok=True)
        self.card.mkdir(exist_ok=True)
        self.t = 100.0
        self.store = L.LogStore(primary_root=str(self.drive / "telemetry"), fallback_root=str(self.card / "fb"),
                                boot=boot, boot_id=boot_id, monotonic=lambda: self.t, **kw)

    def row(self, source="ola", values=(1, 2.5), wall=DAY1 + 60, columns=COLS, uptime=12.0):
        date, line = L.format_row(values, wall, uptime)
        self.store.write(source, columns, date, line)

    def tick(self, seconds=1.0):
        self.t += seconds
        self.store.tick()

    def files(self, where=None):
        """Log files under a location, without the boot markers."""
        root = where or self.drive
        return sorted(os.path.relpath(os.path.join(r, n), root) for r, _, names in os.walk(root) for n in names
                      if n.endswith(".csv"))

    def values(self, path):
        return [r[2] for r in read_rows(path)[1:]]


def read_rows(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


def read_complete_lines(path):
    """As a reader must after a power cut: drop a last line that has no newline."""
    data = open(path, "rb").read()
    return data[:data.rfind(b"\n") + 1].decode().splitlines()


def seq(name):
    return int(re.search(r"_b\d+_(\d+)_\d{8}\.csv$", name).group(1))


class FailingWrites:
    """Makes os.write fail for files under one directory, as a drive with I/O errors or without space does."""

    def __init__(self, monkeypatch, directory, error=errno.EIO, after_bytes=None, only=""):
        self.directory, self.error, self.after_bytes, self.only = str(directory), error, after_bytes, only
        self.active = True
        self.real = os.write
        monkeypatch.setattr(os, "write", self)

    def __call__(self, fd, data):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            target = ""
        if self.active and target.startswith(self.directory) and target.endswith(self.only):
            if self.after_bytes:
                written, self.after_bytes = self.real(fd, bytes(data[:self.after_bytes])), None
                return written
            raise OSError(self.error, os.strerror(self.error))
        return self.real(fd, data)


def test_row_format():
    date, line = L.format_row(["x", 1, 2.5, None, True, False, "a,b\n", 0.1 + 0.2], DAY1 + 3723.4567, 12.3456)
    assert date == "20261119"
    assert line == b"2026-11-19T01:02:03.456Z,12.346,x,1,2.5,,1,0,a;b ,0.3\n"
    assert L.utc_text(DAY1 + 0.9999) == "2026-11-19T00:00:00.999Z"
    assert L.format_row([], 0.0, 0.0)[0] == "19700101"          # a clock that was never set still gives a name


def test_names_header_and_rows(tmp_path):
    s = Store(tmp_path)
    s.row(values=(1, 2.5))
    s.row(values=(3, None), uptime=12.1)
    s.row(source="gps", columns=("lat",), values=(40.5,))
    s.tick()
    assert s.files() == ["telemetry/b00007/gps_b00007_00001_20261119.csv",
                         "telemetry/b00007/ola_b00007_00001_20261119.csv"]
    rows = read_rows(s.drive / "telemetry/b00007/ola_b00007_00001_20261119.csv")
    assert rows == [["time_utc", "uptime_s", "a", "b"], ["2026-11-19T00:01:00.000Z", "12.000", "1", "2.5"],
                    ["2026-11-19T00:01:00.000Z", "12.100", "3", ""]]
    status = s.store.status()
    assert status["location"] == "primary" and status["rows_written"] == {"ola": 2, "gps": 1}
    assert status["files"]["ola"] == "ola_b00007_00001_20261119.csv" and status["rows_waiting"] == 0
    assert status["boot"] == 7 and status["left_alone_for_s"] == {}
    assert (s.drive / "telemetry/b00007/.boot_id").read_text() == "this-boot\n"


def test_new_file_per_day_and_when_the_clock_is_stepped_back(tmp_path):
    s = Store(tmp_path)
    s.row(wall=DAY1 + 86399.5)
    s.row(wall=DAY1 + 86400.5)              # midnight UTC
    s.tick()
    s.row(wall=DAY1 - 5 * 86400)            # the clock is stepped back five days
    s.tick()
    assert [f[-25:] for f in s.files()] == ["b00007_00001_20261119.csv", "b00007_00002_20261120.csv",
                                            "b00007_00003_20261114.csv"]        # still in the order written
    assert [len(read_rows(s.drive / f)) for f in s.files()] == [2, 2, 2]


def test_new_file_when_the_columns_change(tmp_path):
    s = Store(tmp_path)
    s.row()
    s.row(columns=("a", "b", "c"), values=(1, 2, 3))
    s.row(columns=("a", "b", "c"), values=(4, 5, 6))
    s.row()
    s.tick()
    assert [seq(f) for f in s.files()] == [1, 2, 3]
    assert read_rows(s.drive / s.files()[1])[0] == ["time_utc", "uptime_s", "a", "b", "c"]
    assert len(read_rows(s.drive / s.files()[1])) == 3


def test_a_restart_in_the_same_boot_never_reopens_a_file(tmp_path):
    s = Store(tmp_path)
    s.row()
    s.tick()
    s.store.close()
    before = open(s.drive / s.files()[0], "rb").read()
    s2 = Store(tmp_path)                    # the service restarts: same boot, same directory
    s2.row()
    s2.tick()
    assert [seq(f) for f in s2.files()] == [1, 2] and all("/b00007/" in f for f in s2.files())
    assert open(s.drive / s.files()[0], "rb").read() == before
    s3 = Store(tmp_path, boot=8, boot_id="next-boot")               # and the next boot has its own directory
    s3.row()
    s3.tick()
    assert s3.files()[-1] == "telemetry/b00008/ola_b00008_00001_20261119.csv"


def test_names_sort_in_the_order_written_past_999_files(tmp_path):
    s = Store(tmp_path)
    directory = s.drive / "telemetry" / "b00007"
    directory.mkdir(parents=True)
    (directory / ".boot_id").write_text("this-boot\n")
    (directory / "ola_b00007_00998_20261119.csv").write_text("time_utc,uptime_s,a,b\n")
    (directory / "ola_b00007_999_20261119.csv").write_text("time_utc,uptime_s,a,b\n")      # a name of the old width
    for n in range(3):
        s.row(values=(n, 0), columns=("a", "b") + ("c",) * (n % 2))     # each row forces a new file
        s.tick()
    new = [f for f in s.files() if seq(f) >= 1000]
    assert [seq(f) for f in new] == [1000, 1001, 1002]
    assert new == sorted(new) and "telemetry/b00007/ola_b00007_00998_20261119.csv" < new[0]
    assert [s.values(s.drive / f) for f in new] == [["0"], ["1"], ["2"]]


def test_rows_reach_the_file_at_the_flush_interval_in_whole_lines(tmp_path):
    s = Store(tmp_path, flush_interval_s=1.0)
    s.row()
    s.tick(0.1)                 # the first round writes at once
    path = s.drive / s.files()[0]
    size = os.path.getsize(path)
    s.row()
    s.row()
    s.tick(0.5)                 # not yet
    assert os.path.getsize(path) == size
    s.tick(0.6)
    data = open(path, "rb").read()
    assert len(data) > size and data.endswith(b"\n") and data.count(b"\n") == 4


def test_each_file_is_synced_at_the_fsync_interval_one_per_round(tmp_path, monkeypatch):
    assert L.LogStore(primary_root="x", fallback_root="y", boot=1).fsync_interval_s == 30.0     # the default
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    s = Store(tmp_path, flush_interval_s=1.0, fsync_interval_s=30.0)
    for source in ("ola", "gps", "pi"):
        s.row(source=source)
    s.tick()
    fds = {src: st.fd for src, st in s.store._sources.items()}
    assert set(fds.values()) <= set(synced)                 # each new file: header synced
    synced.clear()
    for _ in range(29):
        s.row()
        s.tick()
    assert synced == []                                     # nothing for 30 s
    rounds = []
    for _ in range(4):
        synced.clear()
        s.row()
        s.tick()
        rounds.append(list(synced))
    assert [len(r) for r in rounds] == [1, 1, 1, 0]         # three files due: one per round, then none
    assert len({fd for r in rounds for fd in r}) == 3
    synced.clear()
    s.row()
    s.store.close()                                         # a normal stop writes out and syncs what is left
    assert fds["ola"] in synced
    assert s.values(s.drive / [f for f in s.files() if "/ola_" in f][0])[-1] == "1"


def test_file_cut_off_by_a_power_cut_is_readable_up_to_its_last_full_line(tmp_path):
    s = Store(tmp_path)
    for n in range(50):
        s.row(values=(n, n * 1.5), uptime=float(n))
    s.tick()
    path = s.drive / s.files()[0]
    whole = open(path, "rb").read()
    lines = whole.decode().splitlines()
    for cut in (len(whole) - 1, len(whole) - 7, len(whole) // 2, 30, 5, 0):
        with open(path, "wb") as f:
            f.write(whole[:cut])
        kept = read_complete_lines(path)
        assert kept == lines[:len(kept)]                    # every kept line is a line that was written
        assert len(kept) >= whole[:cut].count(b"\n")
        for row in csv.reader(kept[1:]):
            assert len(row) == 4 and float(row[3]) == int(row[2]) * 1.5


# ---- the drive missing, failing, full

def test_fallback_when_the_drive_is_not_mounted_and_back_when_it_returns(tmp_path, monkeypatch):
    mounted = {"yes": False}
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: mounted["yes"] if p == str(tmp_path / "drive") else real_ismount(p))
    s = Store(tmp_path, mount_point=str(tmp_path / "drive"), retry_s=10.0)
    s.row(values=(1, 1))
    s.tick()
    assert s.store.location == "fallback" and "is not mounted" in s.store.reason
    assert s.files() == [] and s.files(s.card) == ["fb/b00007/ola_b00007_00001_20261119.csv"]
    mounted["yes"] = True                   # the drive appears
    s.row(values=(2, 2))
    s.tick(5)
    assert s.store.location == "fallback"   # looked for every 10 s
    s.row(values=(3, 3))
    s.tick(6)
    assert s.store.location == "primary" and s.store.reason == ""
    assert s.files() == ["telemetry/b00007/ola_b00007_00002_20261119.csv"]     # the sequence continues across locations
    assert s.values(s.card / "fb/b00007/ola_b00007_00001_20261119.csv") == ["1", "2"]
    assert s.values(s.drive / s.files()[0]) == ["3"]
    mounted["yes"] = False                  # and goes again
    s.row(values=(4, 4))
    s.tick(11)
    assert s.store.location == "fallback"
    assert s.files(s.card)[-1] == "fb/b00007/ola_b00007_00003_20261119.csv"
    assert s.store.status()["location_changes"] == 2


def test_fallback_when_the_drive_is_not_writable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    s = Store(tmp_path)
    s.drive.chmod(0o555)
    try:
        s.row()
        s.tick()
        assert s.store.location == "fallback" and "Permission denied" in s.store.reason
        assert s.files(s.card) == ["fb/b00007/ola_b00007_00001_20261119.csv"]
    finally:
        s.drive.chmod(0o755)


def test_write_error_loses_no_row_and_the_file_is_never_appended_to_again(tmp_path, monkeypatch):
    s = Store(tmp_path)
    s.row(values=(1, 1))
    s.tick()
    first = s.drive / s.files()[0]
    failing = FailingWrites(monkeypatch, s.drive)
    s.row(values=(2, 2))
    s.row(values=(3, 3))
    s.tick()
    assert s.store.location == "off" and s.store._sources["ola"].fd is None
    assert s.store.status()["rows_waiting"] == 2            # still in the buffer: nothing was lost
    s.tick()
    assert s.store.location == "fallback" and "write error: Input/output error" in s.store.reason
    assert s.values(first) == ["1"]                         # never appended to again
    assert s.values(s.card / s.files(s.card)[0]) == ["2", "3"]
    assert len(s.files()) == 1 and s.store.status()["rows_dropped"] == 0
    failing.active = False


def test_write_cut_off_in_the_middle_of_a_row_writes_that_row_again_and_no_other(tmp_path, monkeypatch):
    s = Store(tmp_path)
    s.row(values=(0, 0))
    s.tick()
    first = s.drive / s.files()[0]
    one = len(L.format_row((1, 1), DAY1 + 60, 12.0)[1])
    # The drive runs full in the middle of the third of four rows: ENOSPC after a short write.
    FailingWrites(monkeypatch, s.drive, error=errno.ENOSPC, after_bytes=2 * one + 5)
    for n in (1, 2, 3, 4):
        s.row(values=(n, n))
    s.tick()
    s.tick()
    assert s.store.location == "fallback" and "No space left on device" in s.store.reason
    kept = read_complete_lines(first)
    assert [line.split(",")[2] for line in kept[1:]] == ["0", "1", "2"]         # whole rows; a cut-off tail after them
    assert not open(first, "rb").read().endswith(b"\n")
    assert s.values(s.card / s.files(s.card)[0]) == ["3", "4"]                  # the cut-off row again, in full; no row twice


def test_failing_drive_is_left_alone_for_longer_and_longer(tmp_path, monkeypatch):
    """A drive that is mounted but fails every write: the logs settle on the card."""
    s = Store(tmp_path, retry_s=10.0, error_holdoff_s=60.0)
    failing = FailingWrites(monkeypatch, s.drive)
    tried_at, on_primary = [], 0
    real_mark = s.store._mark_bad
    monkeypatch.setattr(s.store, "_mark_bad", lambda location, why, now: (tried_at.append(s.t), real_mark(location, why, now))[1])
    n = 0
    while s.t < 100 + 4 * 3600:
        n += 1
        s.row(values=(n, 0))
        s.tick(10.0)
        on_primary += s.store.location == "primary"
    gaps = [round(b - a) for a, b in zip(tried_at, tried_at[1:])]
    assert gaps[:6] == [60, 120, 240, 480, 960, 1920]       # doubling ...
    assert max(gaps) == 3600 and len(tried_at) <= 10        # ... to once an hour: ten tries in four hours
    assert on_primary == 0 and s.files() == []              # no file was made on the bad drive, not even an empty one
    card = s.files(s.card)
    assert len(card) == 1                                   # and one file on the card, not one per try
    assert s.values(s.card / card[0]) == [str(i) for i in range(1, n + 1)]      # every row, once, in order
    assert s.store.status()["left_alone_for_s"]["primary"] > 0
    assert "cannot write: Input/output error" in s.store.reason and "next try in" in s.store.reason
    # The drive works again: found at the next try, and after ten good minutes the waits start short again.
    failing.active = False
    for _ in range(430):
        n += 1
        s.row(values=(n, 0))
        s.tick(10.0)
    assert s.store.location == "primary" and s.store._bad["primary"]["holdoff"] == 60.0
    assert s.values(s.drive / s.files()[0])[-1] == str(n)


def test_drive_that_takes_the_probe_but_fails_real_writes_settles_too(tmp_path, monkeypatch):
    s = Store(tmp_path, retry_s=10.0, error_holdoff_s=60.0)
    FailingWrites(monkeypatch, s.drive, only=".csv")        # the marker and the probe pass, log files fail
    opened = []
    real_open = s.store._open
    monkeypatch.setattr(s.store, "_open", lambda name, source, columns, date, now:
                        (opened.append((s.t, s.store.location)), real_open(name, source, columns, date, now))[1])
    n = 0
    while s.t < 100 + 2 * 3600:
        n += 1
        s.row(values=(n, 0))
        s.tick(5.0)
    primary_tries = [t for t, where in opened if where == "primary"]
    assert len(primary_tries) <= 8                          # not every 60-70 s for two hours
    gaps = [round(b - a) for a, b in zip(primary_tries, primary_tries[1:])]
    assert gaps[:5] == [65, 125, 245, 485, 965]
    values = [v for f in s.files(s.card) for v in s.values(s.card / f)]
    assert values == [str(i) for i in range(1, n + 1)]      # no row lost at any of the switches, none twice
    assert len(s.files(s.card)) == len(primary_tries)       # a new file on the card only after each try
    assert all(os.path.getsize(s.drive / f) == 0 for f in s.files())    # what the tries left on the bad drive: empty files


def test_read_only_drive_is_found_by_the_probe_before_any_file_is_made(tmp_path, monkeypatch):
    s = Store(tmp_path)

    def probe(path):
        if path.startswith(str(s.drive)):
            raise OSError(errno.EROFS, "Read-only file system")
    monkeypatch.setattr(s.store, "_probe", probe)
    s.row(values=(1, 1))
    s.tick()
    assert s.store.location == "fallback" and "cannot write: Read-only file system" in s.store.reason
    assert s.files() == [] and s.values(s.card / s.files(s.card)[0]) == ["1"]


def test_fallback_directory_is_size_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "ismount", lambda p: False)
    limit = 20000
    old = tmp_path / "card" / "fb" / "b00003"
    old.mkdir(parents=True)
    for n in (1, 2, 3):
        (old / f"ola_b00003_{n:05d}_20261101.csv").write_bytes(b"x" * 6000)
    (old / "notes.txt").write_text("not a log file")
    s = Store(tmp_path, mount_point="/nowhere", fallback_max_bytes=limit, retry_s=10.0)
    for n in range(100):
        s.row(values=(n, "y" * 60))
    s.tick()
    s.tick(11)
    left = s.files(s.card)
    assert "fb/b00003/ola_b00003_00001_20261101.csv" not in left       # the oldest went first
    assert (old / "notes.txt").exists()                                 # only log files are ever deleted
    assert "fb/b00007/ola_b00007_00001_20261119.csv" in left            # never the file being written
    assert sum(os.path.getsize(s.card / f) for f in left) <= limit
    # When only the open files are left and they are over the limit, logging pauses; nothing else is deleted.
    for n in range(400):
        s.row(values=(n, "y" * 60))
    s.tick(11)
    s.tick(11)
    assert s.store.location == "off" and "fallback directory is full" in s.store.reason
    assert (old / "notes.txt").exists()


def test_nothing_writable_keeps_a_bounded_backlog(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "MAX_PENDING_ROWS", 50)
    s = Store(tmp_path, mount_point="/nowhere")
    s.store.roots["fallback"] = ""
    for n in range(80):
        s.row(values=(n, 0))
    s.tick()
    assert s.store.location == "off" and "not configured" in s.store.reason
    assert s.store.status()["rows_dropped"] == 30 and s.store.status()["rows_waiting"] == 50
    s.store.mount_point = ""                # the drive comes back: the backlog is written
    s.tick(11)
    assert s.store.location == "primary"
    assert s.values(s.drive / s.files()[0]) == [str(n) for n in range(50)]      # what did not fit was the newest


def test_highest_boot_number_in_the_log_directories(tmp_path):
    (tmp_path / "a" / "b00012").mkdir(parents=True)
    (tmp_path / "a" / "b00003").mkdir()
    (tmp_path / "a" / "backup").mkdir()
    (tmp_path / "c" / "b123456").mkdir(parents=True)
    assert L.highest_boot([str(tmp_path / "a")]) == 12
    assert L.highest_boot([str(tmp_path / "a"), str(tmp_path / "missing"), str(tmp_path / "c")]) == 123456
    assert L.highest_boot([]) == 0


# ---- a boot directory that belongs to another boot

def old_boot(root, number, marker="an-earlier-boot", files=("ola",)):
    directory = root / ("b%05d" % number)
    directory.mkdir(parents=True)
    if marker is not None:
        (directory / ".boot_id").write_text(marker + "\n")
    for name in files:
        (directory / f"{name}_b{number:05d}_00001_20261101.csv").write_text("time_utc,uptime_s,a,b\nold,1,1,1\n")
    return directory


def test_directory_of_another_boot_is_never_written_into(tmp_path):
    # The counter was lost and restarted at 7, and the drive already has b00007 and b00008 from earlier boots.
    drive = tmp_path / "drive" / "telemetry"
    seven, eight = old_boot(drive, 7), old_boot(drive, 8, marker=None)       # one marked, one from before the markers
    before = {p: p.read_bytes() for d in (seven, eight) for p in d.iterdir()}
    s = Store(tmp_path, boot=7)
    s.row(values=(1, 1))
    s.tick()
    assert s.store.boot == 9 and s.store.status()["boot"] == 9
    assert s.files()[-1] == "telemetry/b00009/ola_b00009_00001_20261119.csv"
    assert (drive / "b00009" / ".boot_id").read_text() == "this-boot\n"
    assert {p: p.read_bytes() for d in (seven, eight) for p in d.iterdir()} == before      # untouched


def test_directory_met_later_on_the_other_location_moves_both(tmp_path, monkeypatch):
    # The boot starts with the drive away (counter low), logs go to the card; then the drive appears
    # and its b00007 belongs to an earlier boot.
    mounted = {"yes": False}
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: mounted["yes"] if p == str(tmp_path / "drive") else real_ismount(p))
    old_boot(tmp_path / "drive" / "telemetry", 7)
    s = Store(tmp_path, boot=7, mount_point=str(tmp_path / "drive"))
    s.row(values=(1, 1))
    s.tick()
    assert s.files(s.card) == ["fb/b00007/ola_b00007_00001_20261119.csv"]
    mounted["yes"] = True
    s.row(values=(2, 2))
    s.tick(11)
    assert s.store.boot == 8 and s.store.location == "primary"
    assert [f for f in s.files() if "b00008" in f] == ["telemetry/b00008/ola_b00008_00001_20261119.csv"]
    assert s.values(s.drive / "telemetry/b00008/ola_b00008_00001_20261119.csv") == ["2"]
    assert len(s.files()) == 2                              # the old boot's file and ours


def test_own_and_unused_directories_are_kept(tmp_path):
    drive = tmp_path / "drive" / "telemetry"
    old_boot(drive, 7, marker="this-boot")                  # ours: the service restarted in this boot
    s = Store(tmp_path, boot=7)
    s.row()
    s.tick()
    assert s.store.boot == 7 and [seq(f) for f in s.files()] == [1, 2]
    (tmp_path / "x" / "drive" / "telemetry" / "b00007").mkdir(parents=True)     # made, never used, no marker
    s2 = Store(tmp_path / "x", boot=7)
    s2.row()
    s2.tick()
    assert s2.store.boot == 7 and (tmp_path / "x/drive/telemetry/b00007/.boot_id").read_text() == "this-boot\n"
    s3 = Store(tmp_path / "y" if (tmp_path / "y").mkdir() is None else None, boot=7, boot_id="")
    old_boot(s3.drive / "telemetry", 7)
    s3.row()
    s3.tick()
    assert s3.store.boot == 7               # without a readable boot ID nothing can be told apart: as before


# ---- the writer thread

def test_threaded_store_writes_and_stops(tmp_path):
    s = Store(tmp_path, flush_interval_s=0.05)
    threaded = L.ThreadedLogStore(s.store, interval_s=0.02)
    for n in range(200):
        date, line = L.format_row((n, 0), DAY1, float(n))
        threaded.write("ola", COLS, date, line)
    assert threaded.status()["writer"] == "running"
    assert threaded.close()
    assert s.values(s.drive / s.files()[0]) == [str(n) for n in range(200)]
    assert threaded.status()["writer"] == "stopped" and threaded.status()["rows_written"] == {"ola": 200}


def test_hung_writer_never_blocks_the_caller_and_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "MAX_PENDING_ROWS", 100)
    monkeypatch.setattr(L, "STALLED_AFTER_S", 0.3)
    s = Store(tmp_path, flush_interval_s=0.01)
    release = threading.Event()
    real = s.store.tick
    monkeypatch.setattr(s.store, "tick", lambda *a, **k: (release.wait(20), real(*a, **k))[1])     # the drive hangs
    threaded = L.ThreadedLogStore(s.store, interval_s=0.01)
    started = time.monotonic()
    for n in range(300):
        date, line = L.format_row((n, 0), DAY1, float(n))
        threaded.write("ola", COLS, date, line)
        threaded.status()
    assert time.monotonic() - started < 2.0                 # the caller was never held up
    status = threaded.status()
    assert status["rows_dropped"] == 200 and status["rows_waiting"] == 100     # counted although the writer hangs
    time.sleep(0.4)
    status = threaded.status()
    assert status["writer"] == "stalled" and "no progress for" in status["reason"]
    release.set()
    assert threaded.close(timeout_s=10)
    assert s.values(s.drive / s.files()[0]) == [str(n) for n in range(100)]     # the rows that fitted, once each


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_dead_writer_is_reported(tmp_path, monkeypatch):
    s = Store(tmp_path)

    def broken(*a, **k):
        raise SystemError("a bug that ends the thread")
    monkeypatch.setattr(s.store, "tick", broken)
    threaded = L.ThreadedLogStore(s.store, interval_s=0.01)
    threaded._thread.join(5)
    status = threaded.status()
    assert status["writer"] == "dead" and "no logs are written" in status["reason"]
    date, line = L.format_row((1, 0), DAY1, 1.0)
    threaded.write("ola", COLS, date, line)                 # still does not block or raise
    assert threaded.status()["rows_waiting"] == 1


# ---- the boot counter

def test_boot_counter_goes_up_once_per_boot(tmp_path):
    d = str(tmp_path)
    assert B.boot_count(d, boot_id="boot-a") == (1, "")
    assert B.boot_count(d, boot_id="boot-a") == (1, "")                 # same boot, any service, any number of times
    assert B.boot_count(d, boot_id="boot-b") == (2, "")
    assert B.boot_count(d, boot_id="boot-b") == (2, "")
    assert B.boot_count(d, boot_id="boot-a") == (3, "")                 # an ID seen before the last one is a new boot too
    assert json.loads((tmp_path / "boot_count.json").read_text()) == {"count": 3, "boot_id": "boot-a"}
    assert not [n for n in os.listdir(d) if n.endswith(".tmp")]


def test_boot_counter_uses_the_kernel_boot_id(tmp_path):
    first = B.boot_count(str(tmp_path))
    assert first == (1, "") and B.boot_count(str(tmp_path)) == first
    assert json.loads((tmp_path / "boot_count.json").read_text())["boot_id"] == open("/proc/sys/kernel/random/boot_id").read().strip()


def test_boot_counter_floor_after_a_lost_or_rolled_back_counter(tmp_path):
    d = str(tmp_path)
    assert B.boot_count(d, boot_id="x", floor=41) == (42, "")           # new card, old data drive
    assert B.boot_count(d, boot_id="x", floor=99) == (42, "")           # already settled for this boot
    assert B.boot_count(d, boot_id="y", floor=10) == (43, "")           # a lower floor changes nothing


@pytest.mark.parametrize("content", [b"", b"{", b'{"count": "7"}', b'{"count": -1, "boot_id": "x"}', b"[1]", b"\xff\x00"])
def test_boot_counter_survives_a_damaged_file(tmp_path, content):
    (tmp_path / "boot_count.json").write_bytes(content)
    assert B.boot_count(str(tmp_path), boot_id="x", floor=5) == (6, "")
    assert B.boot_count(str(tmp_path), boot_id="x") == (6, "")


def test_boot_counter_without_a_writable_directory(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    count, problem = B.boot_count(str(tmp_path / "missing"), boot_id="x", floor=4)
    assert count == 4 and "cannot keep the boot counter" in problem
    d = tmp_path / "ro"
    d.mkdir()
    assert B.boot_count(str(d), boot_id="a") == (1, "")
    d.chmod(0o555)
    try:
        assert B.boot_count(str(d), boot_id="a") == (1, "")             # reading still works
        count, problem = B.boot_count(str(d), boot_id="b")
        assert count == 2 and "cannot keep the boot counter" in problem
    finally:
        d.chmod(0o755)


def test_boot_counter_from_several_processes_at_once(tmp_path):
    results = []

    def worker():
        results.append(B.boot_count(str(tmp_path), boot_id="same-boot"))
    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results) == {(1, "")}


def test_boot_counter_can_be_raised_when_the_number_was_in_use(tmp_path):
    d = str(tmp_path)
    assert B.boot_count(d, boot_id="x") == (1, "")
    assert B.raise_boot_count(d, 9, boot_id="x") == (9, "")
    assert B.boot_count(d, boot_id="x") == (9, "")                      # every later caller in this boot gets it
    assert B.raise_boot_count(d, 4, boot_id="x") == (9, "")             # never lowered
    assert B.boot_count(d, boot_id="y") == (10, "")
    count, problem = B.raise_boot_count(str(tmp_path / "missing"), 5, boot_id="x")
    assert count == 5 and "cannot keep the boot counter" in problem
