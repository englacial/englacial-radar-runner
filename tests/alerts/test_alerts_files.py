"""The queue on disk and the event log follower of the alerts service: persistence, damage, rotation."""
import json
import os

from radar_runner.alerts.queue import DiskQueue
from radar_runner.alerts.tail import EventTail


def item(n):
    return {"title": f"t{n}", "message": f"m{n}", "key": "k"}


# ---- the queue

def test_queued_alerts_survive_a_new_queue_object_and_come_out_by_priority_then_age(tmp_path):
    q = DiskQueue(str(tmp_path / "q"), 50)
    q.setup()
    for n, p in enumerate([3, 5, 2, 5, 3]):
        assert q.add(item(n), p)
    again = DiskQueue(str(tmp_path / "q"), 50)      # a restart, or a reboot
    again.setup()
    order = [again.load(name)["title"] for _, _, name in again.names()]
    assert order == ["t1", "t3", "t0", "t4", "t2"]
    assert again.add(item(9), 3).endswith("000000000006.json")      # numbers go on, never reused


def test_a_full_queue_drops_the_lowest_oldest_and_never_an_urgent_one_for_a_lower(tmp_path):
    q = DiskQueue(str(tmp_path / "q"), 4)
    q.setup()
    for n, p in enumerate([5, 5, 2, 3]):
        q.add(item(n), p)
    assert q.add(item(4), 3)                    # pushes out the priority-2 alert
    assert sorted(q.counts().items()) == [(3, 2), (5, 2)] and q.dropped == 1
    assert q.add(item(5), 5)                    # pushes out the oldest priority-3 alert
    assert [q.load(n)["title"] for _, _, n in q.names()] == ["t0", "t1", "t5", "t4"]
    assert q.add(item(6), 2) == ""              # lower than everything queued: itself dropped
    for n in range(7, 12):
        assert q.add(item(n), 5)                # urgent only: the oldest urgent one goes
    assert q.counts() == {5: 4} and q.dropped == 2 + 1 + 5
    assert len(os.listdir(tmp_path / "q")) == 4


def test_damaged_files_and_leftovers_of_a_cut_off_write_are_cleaned_up(tmp_path):
    d = tmp_path / "q"
    q = DiskQueue(str(d), 10)
    q.setup()
    good = q.add(item(1), 3)
    (d / "p5-000000000002.json").write_bytes(b'{"title": "cut of')            # truncated
    (d / "p4-000000000003.json").write_bytes(b"\0\0\0\0")                       # NULs after a power cut
    (d / "p4-000000000004.json").write_text(json.dumps([1, 2]))                 # not an alert
    (d / "p4-000000000005.json").write_bytes(b"x" * 20000)                      # too large
    (d / ".p3-000000000006.json.99.tmp").write_text("{}")                       # a write cut off before its rename
    (d / "unrelated.txt").write_text("left alone")
    q = DiskQueue(str(d), 10)
    q.setup()
    assert not (d / ".p3-000000000006.json.99.tmp").exists()
    assert [q.load(n) is not None for _, _, n in q.names()] == [False, False, False, False, True]
    assert q.damaged == 4 and [n for _, _, n in q.names()] == [good]
    assert (d / "unrelated.txt").exists() and q.seq == 5


def test_an_unwritable_queue_says_so_and_does_not_raise(tmp_path):
    (tmp_path / "file").write_text("")
    q = DiskQueue(str(tmp_path / "file" / "q"), 10)
    q.setup()
    assert q.error and q.add(item(1), 5) == "" and q.names() == []


# ---- following an event log

def append(path, *events, raw=b""):
    with open(path, "ab") as f:
        for e in events:
            f.write((json.dumps(e) + "\n").encode())
        f.write(raw)


def rotate(path):
    os.replace(path, str(path) + ".1")


def names(events):
    return [e["n"] for e in events]


def test_the_first_start_begins_at_the_end_and_then_follows_new_lines(tmp_path):
    log = tmp_path / "events.jsonl"
    append(log, {"n": 1}, {"n": 2})
    t = EventTail(str(log))
    assert t.read() == []                       # history is not sent
    append(log, {"n": 3})
    assert names(t.read()) == [3]
    assert t.read() == []


def test_a_restored_position_goes_on_where_it_was(tmp_path):
    log = tmp_path / "events.jsonl"
    t = EventTail(str(log))
    t.begin_at_end()
    append(log, {"n": 1})
    assert names(t.read()) == [1]
    saved = t.position()
    append(log, {"n": 2}, {"n": 3})
    t2 = EventTail(str(log))
    t2.restore(json.loads(json.dumps(saved)))
    assert names(t2.read()) == [2, 3]


def test_a_partial_last_line_waits_until_it_is_complete(tmp_path):
    log = tmp_path / "events.jsonl"
    t = EventTail(str(log))
    t.begin_at_end()
    append(log, {"n": 1}, raw=b'{"n": ')
    assert names(t.read()) == [1]
    assert t.read() == []
    append(log, raw=b'2}\n')
    assert names(t.read()) == [2] and t.skipped == 0


def test_a_line_cut_off_by_a_power_cut_and_nul_bytes_are_skipped(tmp_path):
    log = tmp_path / "events.jsonl"
    t = EventTail(str(log))
    t.begin_at_end()
    # The cut-off line is continued by the next writer's line; then the file system's NUL bytes.
    append(log, raw=b'{"n": 1, "te')
    append(log, {"n": 2})
    append(log, raw=b"\0" * 300)
    append(log, {"n": 3}, raw=b"not json\n[1, 2]\n\n")
    append(log, {"n": 4})
    assert names(t.read()) == [3, 4] and t.skipped == 3


def test_rotation_to_dot_one_is_followed_without_loss(tmp_path):
    log = tmp_path / "events.jsonl"
    t = EventTail(str(log))
    t.begin_at_end()
    append(log, {"n": 1})
    assert names(t.read()) == [1]
    append(log, {"n": 2}, {"n": 3})
    rotate(log)                                 # the writer renames at its size limit ...
    assert names(t.read()) == [2, 3]            # ... and before it writes the next line
    append(log, {"n": 4})
    assert names(t.read()) == [4]
    append(log, {"n": 5})
    rotate(log)
    append(log, {"n": 6})                       # rotated and written in between two reads
    assert names(t.read()) == [5, 6] and t.lost == 0


def test_two_rotations_while_not_followed_are_counted_as_lost(tmp_path):
    log = tmp_path / "events.jsonl"
    append(log, {"n": 0})
    t = EventTail(str(log))
    t.begin_at_end()
    append(log, {"n": 1})
    rotate(log)
    append(log, {"n": 2})
    rotate(log)                                 # replaces the .1 that held the position
    append(log, {"n": 3})
    # The end of the file that held the position (n 1) is gone; the .1 after it was never read.
    assert names(t.read()) == [2, 3] and t.lost == 1


def test_a_log_whose_unsynced_end_was_lost_in_a_power_cut_is_never_read_again_from_the_start(tmp_path):
    log = tmp_path / "events.jsonl"
    append(log, {"n": 0, "text": "x" * 80})
    t = EventTail(str(log))
    t.begin_at_end()
    for i in range(1, 13):
        append(log, {"n": i, "event": "fault"})
    synced = log.stat().st_size
    append(log, {"n": 13}, {"n": 14})
    assert names(t.read()) == list(range(1, 15))
    os.truncate(log, synced)                    # what the drive has after the cut: the last two are gone
    append(log, {"n": 15})                      # the next boot's first event
    assert names(t.read()) == [15] and t.lost == 1


def test_a_cut_off_last_line_of_the_rotated_file_is_dropped(tmp_path):
    log = tmp_path / "events.jsonl"
    append(log, {"n": 0})
    t = EventTail(str(log))
    t.begin_at_end()
    append(log, {"n": 1}, raw=b'{"n": 2, "cut')
    rotate(log)
    append(log, {"n": 3})
    assert names(t.read()) == [1, 3] and t.skipped == 1


def test_a_truncated_or_missing_file_and_a_huge_line(tmp_path):
    log = tmp_path / "events.jsonl"
    t = EventTail(str(log), max_read=4096, max_line=1000)
    assert t.read() == [] and t.started          # missing at the first start
    append(log, {"n": 1})
    assert names(t.read()) == [1]                # the file that appears is read from its start
    log.write_text("")                           # rewritten in place: noticed because it got shorter
    assert t.read() == []
    append(log, {"n": 2})
    assert names(t.read()) == [2]
    append(log, {"n": 3, "pad": "x" * 5000}, {"n": 4})
    got = []
    for _ in range(5):
        got += t.read()
    assert names(got) == [4] and t.skipped >= 1


def test_reading_is_bounded_per_call(tmp_path):
    log = tmp_path / "events.jsonl"
    t = EventTail(str(log), max_read=64 * 1024)
    t.begin_at_end()
    append(log, *({"n": i, "pad": "y" * 200} for i in range(2000)))       # about 450 kB
    first = t.read()
    assert 0 < len(first) < 2000
    rest = []
    while True:
        more = t.read()
        if not more:
            break
        rest += more
    assert names(first + rest) == list(range(2000))


def test_the_follower_never_writes(tmp_path):
    log = tmp_path / "events.jsonl"
    append(log, {"n": 1})
    before = (log.stat().st_mtime_ns, log.read_bytes(), sorted(os.listdir(tmp_path)))
    t = EventTail(str(log))
    for _ in range(3):
        t.read()
        append(log, {"n": 2})
    assert sorted(os.listdir(tmp_path)) == before[2]
    assert log.read_bytes().startswith(before[1])
