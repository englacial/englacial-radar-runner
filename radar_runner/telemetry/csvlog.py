"""Sensor log files: append-only CSV, one file per source, per boot and per day.

    <root>/b00042/ola_b00042_00001_20261119.csv
           |          |      |     |
           |          |      |     UTC date when the file was started (may be wrong: no clock battery)
           |          |      sequence number of this source's files within the boot
           |          boot counter (radar_runner/common/bootcount.py)
           one directory per boot, with the kernel's ID of that boot in the file .boot_id

`<root>` is `<data_root>/telemetry` on the NVMe drive, or the fallback directory
on the SD card while the drive is missing or not writable. Names sort in the
order in which the files were written, whatever the clock said.

Every file starts with a header line. Every row starts with `time_utc` (wall
clock) and `uptime_s` (seconds since boot, CLOCK_BOOTTIME), followed by the
source's columns. A new file is started when the boot, the UTC date or the set
of columns changes, when the storage location changes, after any write error,
and at every start of the service. A file is never opened twice and never
overwritten. A boot directory that belongs to another boot (the boot counter
was lost and restarted low) is never written into: the next free number is used.

What a hard power cut can lose. Rows are handed to the kernel in whole lines
every `flush_interval_s` (1 s) and each file is synced to the drive every
`fsync_interval_s` (30 s), one file per round so that the syncs are spread
out. With four sources that is at most about 35 s of rows. A normal shutdown
(SIGTERM) writes out and syncs everything. What is on the drive is readable;
only the last line of a file can be incomplete, and readers must drop a last
line that does not end with a newline.

    TO MEASURE IN STAGE 4: the cost of these syncs while the radar records. With ext4
    (data=ordered) a sync can force a journal commit that carries the radar's data too,
    at tens of MB per second. Idle, a sync took about 2 ms on either drive (2026-11-19).
    30 s is a cautious default until that is measured; lower it only with numbers.

A drive that fails. A location (drive or card) that fails a write, or a
write-and-sync probe before it is used, is left alone for `error_holdoff_s`,
doubling with every further failure up to an hour, and starting short again
only after ten minutes of good writes. So a drive that is mounted but broken,
read-only or full ends up left alone, with the logs on the card, and is tried
again once an hour. Rows are taken out of the buffer only when their bytes are in
a file: a failed write loses nothing, the rows go to the next file.

`LogStore.write` may be called from any thread and never waits: it appends to
one bounded buffer per source (MAX_PENDING_ROWS; when it is full, the newest
rows are dropped and counted). Everything else runs in the writer thread of
`ThreadedLogStore`, so that a slow or hung drive cannot hold up the service
loop that publishes the amplifier readings.
"""
import collections
import errno
import logging
import os
import re
import threading
import time

log = logging.getLogger("telemetry.log")

PRIMARY, FALLBACK, OFF = "primary", "fallback", "off"
TIME_COLUMNS = ("time_utc", "uptime_s")
MAX_PENDING_ROWS = 20000        # per source, kept in memory while no file can be written (30 min of OLA rows)
MAX_BATCH = 500                 # rows per write
GOOD_FOR_S = 600.0              # good writes for this long: the wait after an error starts short again
STALLED_AFTER_S = 10.0          # no round of the writer thread for this long: reported as stalled
MARKER = ".boot_id"
_NAME = re.compile(r"^(?P<source>[a-z0-9]+)_b(?P<boot>\d{5,})_(?P<seq>\d{3,})_(?P<date>\d{8})\.csv$")
_BOOT_DIR = re.compile(r"^b(\d{5,})$")


def utc_text(wall):
    """Wall-clock time as ISO 8601 with milliseconds: 2026-11-19T01:02:03.456Z."""
    whole = int(wall // 1)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + ".%03dZ" % min(999, int((wall - whole) * 1000))


def field(value):
    """One CSV field. Text never holds a separator: sensor values are numbers and plain words."""
    if value is None:
        return ""
    if value is True:
        return "1"
    if value is False:
        return "0"
    if isinstance(value, float):
        return repr(round(value, 6))
    text = str(value)
    if "," in text or "\n" in text or "\r" in text or '"' in text:
        text = text.replace(",", ";").replace("\n", " ").replace("\r", " ").replace('"', "'")
    return text


def format_row(values, wall, uptime):
    """Return (UTC date as YYYYMMDD, the complete line as bytes)."""
    stamp = utc_text(wall)
    line = ",".join([stamp, "%.3f" % uptime] + [field(v) for v in values]) + "\n"
    return stamp[0:4] + stamp[5:7] + stamp[8:10], line.encode("utf-8", "replace")


def highest_boot(roots):
    """Highest boot number used in directory names under the given roots (0 if none)."""
    best = 0
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            m = _BOOT_DIR.match(name)
            if m:
                best = max(best, int(m.group(1)))
    return best


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_marker(directory):
    """The boot ID a boot directory was made in, or None if it has no (complete) marker."""
    try:
        with open(os.path.join(directory, MARKER)) as f:
            return f.read(200).strip() or None
    except OSError:
        return None


class _Source:
    """One source's current file and the rows not yet written."""

    def __init__(self):
        self.columns = None     # of the open file
        self.date = None
        self.fd = None
        self.path = ""
        self.pending = collections.deque()      # (columns, date, line); appended by any thread, taken by the writer
        self.dirty = False                      # written but not yet synced
        self.last_sync = 0.0
        self.rows = 0
        self.seq = 0


class LogStore:
    def __init__(self, *, primary_root, fallback_root, boot, boot_id="", mount_point="", fallback_max_bytes=1 << 30,
                 flush_interval_s=1.0, fsync_interval_s=30.0, retry_s=10.0, error_holdoff_s=60.0,
                 error_holdoff_max_s=3600.0, monotonic=time.monotonic):
        self.roots = {PRIMARY: primary_root, FALLBACK: fallback_root}
        self.mount_point = mount_point
        self.boot = boot
        self.boot_id = boot_id
        self.fallback_max_bytes = fallback_max_bytes
        self.flush_interval_s = flush_interval_s
        self.fsync_interval_s = fsync_interval_s
        self.retry_s = retry_s
        self._holdoff_start, self._holdoff_max = error_holdoff_s, error_holdoff_max_s
        self._monotonic = monotonic
        self.location = OFF
        self.reason = "not started"
        self.dropped_rows = 0
        self.switches = 0
        self._lock = threading.Lock()           # for the table of sources and the drop counter
        self._sources = {}
        self._last_flush = None
        self._next_check = None
        self._chosen_once = False
        # Per location: left alone until, why, the wait to apply at the next error, good since.
        self._bad = {loc: {"until": 0.0, "why": "", "holdoff": error_holdoff_s, "good_since": None}
                     for loc in (PRIMARY, FALLBACK)}
        self._problems = {PRIMARY: "", FALLBACK: ""}
        self._claimed = set()                   # boot directories checked to be this boot's

    # ---- for the service: any thread

    def write(self, name, columns, date, line):
        """Queue one formatted row (see format_row) for the source `name`. Never waits."""
        source = self._sources.get(name)
        if source is None:
            with self._lock:
                source = self._sources.setdefault(name, _Source())
        if len(source.pending) >= MAX_PENDING_ROWS:
            with self._lock:
                self.dropped_rows += 1          # the newest row is dropped: the writer owns the old end
            return
        source.pending.append((columns, date, line))

    def _all_sources(self):
        with self._lock:
            return list(self._sources.items())

    # ---- where to write (writer thread from here on)

    def _boot_dir(self, location):
        return os.path.join(self.roots[location], "b%05d" % self.boot)

    def _claim(self, path):
        """Make the boot directory ours. False if it holds another boot's logs."""
        if path in self._claimed:
            return True
        try:
            os.makedirs(path)
            mark = True
        except FileExistsError:
            marker = _read_marker(path)
            if not self.boot_id or marker == self.boot_id:
                mark = False                    # ours already (or boots cannot be told apart)
            elif marker is None and not any(_NAME.match(n) for n in os.listdir(path)):
                mark = True                     # empty and unmarked: made and not yet used
            else:
                return False                    # another boot's logs
        if mark and self.boot_id:
            # Through a temporary name: a power cut or a failed write leaves no half-written marker.
            tmp = os.path.join(path, MARKER + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, (self.boot_id + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, os.path.join(path, MARKER))
            _sync_dir(path)
        self._claimed.add(path)
        return True

    def _probe(self, path):
        """Write and sync a few bytes: shows a read-only or failing drive before any log file is made on it."""
        probe = os.path.join(path, ".probe")
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, b"probe\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.unlink(probe)

    def _mark_bad(self, location, why, now):
        bad = self._bad[location]
        bad["until"], bad["why"], bad["good_since"] = now + bad["holdoff"], why, None
        log.error("sensor logs: %s (%s); leaving it alone for %.0f s", why, location, bad["holdoff"])
        bad["holdoff"] = min(bad["holdoff"] * 2, self._holdoff_max)

    def _usable(self, location, now):
        """Prepare a location. Returns "" or why it cannot be used."""
        root = self.roots[location]
        if not root:
            return "not configured"
        bad = self._bad[location]
        if now < bad["until"]:
            return f"{bad['why']}; next try in {bad['until'] - now:.0f} s"
        if location == PRIMARY and self.mount_point and not os.path.ismount(self.mount_point):
            return f"{self.mount_point} is not mounted"
        try:
            while not self._claim(self._boot_dir(location)):
                taken = self._boot_dir(location)
                self._next_boot_number()
                log.warning("sensor logs: %s belongs to another boot (the boot counter restarted low?); "
                            "using boot number %d", taken, self.boot)
            if location != self.location:       # before moving to it: can it be written?
                self._probe(self._boot_dir(location))
        except OSError as e:
            why = f"cannot write: {e.strerror}" + (f": {e.filename}" if e.filename else "")
            if e.errno not in (errno.EACCES, errno.EPERM, errno.ENOENT):
                # Not a matter of ownership or of a directory still to be made: the drive is failing.
                self._mark_bad(location, why, now)
            return why
        if location == FALLBACK:
            return self._limit_fallback()
        return ""

    def _next_boot_number(self):
        """Move to the next boot number whose directory is free or ours in both locations."""
        for _, source in self._all_sources():
            self._close(source)
            source.seq = 0
        self._claimed.clear()
        self.location = OFF
        while True:
            self.boot += 1
            for location in (PRIMARY, FALLBACK):
                path = self._boot_dir(location) if self.roots[location] else ""
                if path and os.path.isdir(path) and _read_marker(path) != self.boot_id \
                        and (_read_marker(path) is not None or any(_NAME.match(n) for n in os.listdir(path))):
                    break
            else:
                return

    def _choose(self, now):
        """Pick the location to write to; called at the start, after an error, and every retry_s."""
        self._next_check = now + self.retry_s
        chosen, reasons = OFF, []
        for location in (PRIMARY, FALLBACK):
            problem = self._usable(location, now)
            shown = problem.split("; next try in")[0]
            if shown != self._problems[location]:
                (log.warning if problem else log.info)(
                    "sensor logs: %s location %s", location, shown or "is usable again")
                self._problems[location] = shown
            if not problem:
                chosen = location
                break
            reasons.append(problem)
        if chosen != self.location:
            for _, source in self._all_sources():
                self._close(source)
            self.switches += 1 if self._chosen_once else 0
            self._chosen_once = True
            self.location = chosen
            log.log(logging.INFO if chosen == PRIMARY else logging.WARNING, "sensor logs go to %s",
                    self._boot_dir(chosen) if chosen != OFF else "nowhere: " + "; ".join(reasons))
        self.reason = "; ".join(reasons)

    def _limit_fallback(self):
        """Keep the fallback directory under its size limit by deleting its oldest closed files."""
        root = self.roots[FALLBACK]
        open_paths = {s.path for _, s in self._all_sources() if s.fd is not None}
        files, total = [], 0
        try:
            for name in sorted(os.listdir(root)):
                if not _BOOT_DIR.match(name):
                    continue
                directory = os.path.join(root, name)
                for entry in sorted(os.listdir(directory)):
                    if _NAME.match(entry):
                        path = os.path.join(directory, entry)
                        size = os.path.getsize(path)
                        total += size
                        files.append((path, size))
        except OSError as e:
            return f"{e.strerror}: {e.filename or root}"

        def age(item):          # oldest first: boot number, then sequence number, whatever the source
            m = _NAME.match(os.path.basename(item[0]))
            return int(m.group("boot")), int(m.group("seq"))
        files.sort(key=age)
        for path, size in files:
            if total <= self.fallback_max_bytes * 0.9:
                break
            if path in open_paths:
                continue
            try:
                os.unlink(path)
                total -= size
                log.warning("sensor logs: fallback directory over its limit, deleted %s", path)
            except OSError:
                pass
        if total > self.fallback_max_bytes:
            return f"fallback directory is full ({total >> 20} MB, limit {self.fallback_max_bytes >> 20} MB)"
        return ""

    # ---- files

    def _next_seq(self, name):
        """One more than the highest sequence number this source has used in this boot, anywhere."""
        best = 0
        for location in (PRIMARY, FALLBACK):
            path = self._boot_dir(location)
            try:
                if path not in self._claimed and self.boot_id and _read_marker(path) != self.boot_id:
                    continue                    # not this boot's directory (or not there)
                entries = os.listdir(path)
            except OSError:
                continue
            for entry in entries:
                m = _NAME.match(entry)
                if m and m.group("source") == name and int(m.group("boot")) == self.boot:
                    best = max(best, int(m.group("seq")))
        return best + 1

    def _open(self, name, source, columns, date, now):
        directory = self._boot_dir(self.location)
        source.seq = max(source.seq + 1, self._next_seq(name))
        for _ in range(1000):
            # Five digits, so that the names of one boot sort in the order written far beyond 999 files.
            path = os.path.join(directory, "%s_b%05d_%05d_%s.csv" % (name, self.boot, source.seq, date))
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o644)
                break
            except FileExistsError:
                source.seq += 1
        else:
            raise OSError(errno.EEXIST, "no free file name", directory)
        source.fd, source.path, source.columns, source.date = fd, path, columns, date
        source.last_sync = now
        os.write(fd, (",".join(TIME_COLUMNS + tuple(field(c) for c in columns)) + "\n").encode())
        os.fsync(fd)
        _sync_dir(directory)        # the new name itself must survive a power cut
        log.info("sensor logs: started %s", path)

    def _close(self, source):
        if source.fd is not None:
            try:
                if source.dirty:
                    os.fsync(source.fd)
            except OSError:
                pass
            try:
                os.close(source.fd)
            except OSError:
                pass
        source.fd, source.columns, source.date, source.dirty = None, None, None, False

    def _write_lines(self, source, lines):
        """Write rows to the source's file. Rows leave the buffer only once all their bytes are written."""
        data = b"".join(lines)
        view, done = memoryview(data), 0
        try:
            while done < len(data):
                done += os.write(source.fd, view[done:])
        finally:
            complete, total = 0, 0
            for line in lines:
                total += len(line)
                if total > done:
                    break
                complete += 1
            for _ in range(complete):
                source.pending.popleft()
            source.rows += complete
            source.dirty = source.dirty or complete > 0

    def _flush(self, name, source, now):
        """Write the pending rows of one source, starting a new file where the columns or date change."""
        while source.pending:
            columns, date, _ = source.pending[0]
            if source.fd is None or columns != source.columns or date != source.date:
                self._close(source)
                self._open(name, source, columns, date, now)
            lines = []
            for i in range(min(len(source.pending), MAX_BATCH)):    # by index: other threads append meanwhile
                item = source.pending[i]
                if item[0] != columns or item[1] != date:
                    break
                lines.append(item[2])
            self._write_lines(source, lines)

    def tick(self, now=None, force=False):
        """Flush and sync what is due. Called a few times per second by the writer thread."""
        now = self._monotonic() if now is None else now
        try:
            if self._next_check is None or now >= self._next_check:
                self._choose(now)
            if self.location == OFF:
                return
            if not force and self._last_flush is not None and now - self._last_flush < self.flush_interval_s:
                return
            self._last_flush = now
            sources = self._all_sources()
            for name, source in sources:
                self._flush(name, source, now)
            # Sync one file per round, the one that has waited longest, to spread the load.
            due = [s for _, s in sources
                   if s.fd is not None and s.dirty and (force or now - s.last_sync >= self.fsync_interval_s)]
            for source in sorted(due, key=lambda s: s.last_sync)[:len(due) if force else 1]:
                os.fsync(source.fd)
                source.dirty, source.last_sync = False, now
            bad = self._bad[self.location]
            if bad["good_since"] is None:
                bad["good_since"] = now
            elif now - bad["good_since"] >= GOOD_FOR_S:
                bad["holdoff"] = self._holdoff_start
        except OSError as e:
            # Never append to a file after a failed write: its last line may be cut off.
            why = f"write error: {e.strerror or e}"
            for _, source in self._all_sources():
                self._close(source)
            if self.location != OFF:
                self._mark_bad(self.location, why, now)
            self.location, self.reason = OFF, why
            self._next_check = now                  # choose again at the next call
        except Exception:                           # a logging bug must not stop the readings
            log.exception("sensor logs: internal error")

    def close(self):
        self.tick(force=True)
        for _, source in self._all_sources():
            self._close(source)

    def status(self):
        sources = self._all_sources()
        now = self._monotonic()
        waits = {loc: round(bad["until"] - now) for loc, bad in self._bad.items() if bad["until"] > now}
        return {
            "location": self.location,
            "directory": self._boot_dir(self.location) if self.location != OFF else "",
            "reason": self.reason,
            "boot": self.boot,
            "files": {name: os.path.basename(s.path) for name, s in sources if s.fd is not None},
            "rows_written": {name: s.rows for name, s in sources},
            "rows_waiting": sum(len(s.pending) for _, s in sources),
            "rows_dropped": self.dropped_rows,
            "location_changes": self.switches,
            "left_alone_for_s": waits,
        }


class ThreadedLogStore:
    """Runs a LogStore in a thread of its own. `write` never blocks; `status` says if the thread keeps up."""

    def __init__(self, store, *, interval_s=0.25):
        self._store = store
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._stopped = False
        self._snapshot = store.status()
        self._beat = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="sensor-logs", daemon=True)
        self._thread.start()

    def write(self, name, columns, date, line):
        self._store.write(name, columns, date, line)

    def _run(self):
        while not self._stop.is_set():
            self._store.tick()                  # catches its own errors
            self._beat = time.monotonic()
            self._snapshot = self._store.status()
            self._stop.wait(self._interval_s)
        self._store.close()
        self._snapshot = self._store.status()
        self._stopped = True

    def status(self):
        """The writer's last report, with `writer`: "running", "stopped", "stalled" (a drive holds it up) or "dead"."""
        snapshot = dict(self._snapshot)
        # Counted here, by the caller's thread: right even while the writer hangs.
        snapshot["rows_dropped"] = self._store.dropped_rows
        snapshot["rows_waiting"] = sum(len(s.pending) for _, s in self._store._all_sources())
        behind = time.monotonic() - self._beat
        if self._stopped:
            snapshot["writer"] = "stopped"
        elif not self._thread.is_alive():
            snapshot["writer"] = "dead"
            snapshot["reason"] = "the writer thread has ended: no logs are written"
        elif behind > STALLED_AFTER_S:
            snapshot["writer"] = "stalled"
            snapshot["reason"] = f"no progress for {behind:.0f} s (drive not answering?)"
        else:
            snapshot["writer"] = "running"
        return snapshot

    def close(self, timeout_s=5.0):
        """Write out what is waiting and stop. Gives up after timeout_s if the drive hangs."""
        self._stop.set()
        self._thread.join(timeout_s)
        return not self._thread.is_alive()
