"""Following another service's event log (one JSON object per line) without ever writing to it.

The watcher and the manager append to <file> and, at their size limit, rename it
to <file>.1 and start a new <file>. The position kept here is the file (its
inode and its first 64 bytes, since a deleted file's inode is soon reused) and
the offset in it:

* that file at <file>: read on from the offset (from 0 if the file got shorter);
* that file is now <file>.1: read the rest of <file>.1, then <file> from 0;
* neither: the file was rotated twice, or replaced, while it was not followed:
  read <file> from 0 and count it in `lost` (events may have been missed);
* no position yet (the very first start): begin at the end; history is not sent.

Only whole lines are taken: a last line without its newline (being written, or
cut off by a power cut) waits. A line that is not a JSON object (a line cut off
by a power cut and then continued by the next writer, or NUL bytes the file
system left after a cut) is skipped and counted. A line longer than
`max_line` is skipped. At most `max_read` bytes are read per call.
"""
import json
import os

MAX_LINE = 65536
HEAD = 64


def _head_of(path):
    try:
        with open(path, "rb") as f:
            return os.fstat(f.fileno()).st_ino, f.read(HEAD)
    except OSError:
        return None, b""


class EventTail:
    def __init__(self, path, max_read=256 * 1024, max_line=MAX_LINE):
        self.path = path
        self.max_line = max_line
        self.max_read = max(int(max_read), max_line)    # a line of the largest allowed size always fits
        self.inode = None       # None with offset 0: the next file that appears, from its start
        self.offset = 0
        self.head = b""         # the first bytes of that file, as far as they were there
        self.started = False    # False until a position is known (restored or begun at the end)
        self.lines = 0
        self.skipped = 0
        self.lost = 0
        self.error = ""
        self._skip_rest = False     # inside a line that was too long

    # ---- the position, for the state file

    def position(self):
        if not self.started:
            return None
        return {"inode": self.inode, "offset": self.offset, "head": self.head.hex()}

    def restore(self, pos):
        if isinstance(pos, dict) and type(pos.get("offset")) is int and pos["offset"] >= 0 \
                and (pos.get("inode") is None or type(pos.get("inode")) is int):
            try:
                self.head = bytes.fromhex(str(pos.get("head") or ""))[:HEAD]
            except ValueError:
                self.head = b""
            self.inode, self.offset, self.started = pos["inode"], pos["offset"], True

    def begin_at_end(self):
        try:
            st = os.stat(self.path)
            self.inode, self.offset = st.st_ino, st.st_size
            self.head = _head_of(self.path)[1]
        except OSError:
            self.inode, self.offset, self.head = None, 0, b""
        self.started = True

    def _is_ours(self, st, path):
        """True if the file at `path` (its stat `st`) is the one the position is in."""
        if st is None or self.inode is None or st.st_ino != self.inode:
            return False
        inode, head = _head_of(path)
        if inode != self.inode or head[:len(self.head)] != self.head:
            return False        # the inode of a deleted file, reused
        self.head = head
        return True

    # ---- reading

    def read(self):
        """The new events (dicts), in order. Never raises."""
        if not self.started:
            self.begin_at_end()
            return []
        events, budget = [], self.max_read
        try:
            current = os.stat(self.path)
        except FileNotFoundError:
            current = None
        except OSError as e:
            self.error = f"{e.strerror or e}"
            return events
        try:
            if current is not None and (self.inode is None or self._is_ours(current, self.path)):
                if self.inode is None:
                    self.inode, self.offset, self.head = current.st_ino, 0, b""
                elif current.st_size < self.offset:
                    self.offset = 0         # truncated or rewritten in place
                budget -= self._read_from(self.path, current.st_ino, events, budget)
            else:
                try:
                    old = os.stat(self.path + ".1")
                except OSError:
                    old = None
                if self._is_ours(old, self.path + ".1"):
                    used = self._read_from(self.path + ".1", old.st_ino, events, budget, final=True)
                    budget -= used
                    if self.offset < old.st_size:
                        return events       # more of .1 next time
                elif current is None:
                    return events           # nothing there yet
                else:
                    self.lost += 1
                self.inode, self.offset, self.head, self._skip_rest = None, 0, b"", False
                if current is not None and budget > 0:
                    self.inode = current.st_ino
                    self._read_from(self.path, current.st_ino, events, budget)
            self.error = ""
        except OSError as e:
            self.error = f"{e.strerror or e}"
        return events

    def _read_from(self, path, inode, events, budget, final=False):
        """Read whole lines of `path` from the offset; returns the number of bytes consumed."""
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_ino != inode:
                return 0            # renamed between stat and open: next time
            f.seek(self.offset)
            data = f.read(budget)
        # The cut-off last line of a rotated file, which will not grow again, is dropped.
        final = final and self.offset + len(data) >= st.st_size
        end = data.rfind(b"\n") + 1
        if end == 0:
            if len(data) >= self.max_line:
                self._skip_rest = True      # a line longer than allowed: drop it up to its newline
            elif not (final and data):
                return 0                    # a line still being written, or no room left this time
            self.offset += len(data)
            self.skipped += 1
            return len(data)
        for raw in data[:end].split(b"\n")[:-1]:
            if self._skip_rest:
                self._skip_rest = False
                continue
            self._take(raw, events)
        consumed = end
        if final and end < len(data):
            consumed = len(data)
            self.skipped += 1
        self.offset += consumed
        return consumed

    def _take(self, raw, events):
        raw = raw.replace(b"\0", b"").strip()
        if not raw:
            return
        if len(raw) > self.max_line:
            self.skipped += 1
            return
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            event = None
        if isinstance(event, dict):
            self.lines += 1
            events.append(event)
        else:
            self.skipped += 1
