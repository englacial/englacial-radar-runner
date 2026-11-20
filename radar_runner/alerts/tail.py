"""Following another service's event log (one JSON object per line) without ever writing to it.

The watcher and the manager append to <file> and, at their size limit, rename it
to <file>.1 and start a new <file>. The position kept here is the file (its
inode and its first 64 bytes, since a deleted file's inode is soon reused), the
offset in it, the fingerprint of the line that ends at the offset, the
fingerprints of the last 50 lines taken, and the identity and size of the last
three files read to their end. A power cut can undo any of the writers' recent
steps (a line, a rename, the new file), so:

* that file at <file>: read on from the offset, if the line ending there is the
  last one taken. If it is not, or the file is shorter than the offset (its
  unsynced end lost, and perhaps new lines written since), go on after the last
  line already taken that is still in it, or from its end; counted in `lost`;
* that file is now <file>.1: the same check, the rest of <file>.1, then <file>;
* a file read to its end before is never read from its start again: at <file>
  (a rename that was undone) it is read on from where it ended;
* neither, with a <file>.1 that was never read: rotated twice while not
  followed: the end of the old file is gone (`lost`); all of <file>.1, then
  <file> from 0;
* otherwise <file> from 0, counted in `lost`;
* no position yet (the very first start): begin at the end; history is not sent.

Only whole lines are taken: a last line without its newline (being written, or
cut off by a power cut) waits. A line that is not a JSON object (a line cut off
by a power cut and then continued by the next writer, or NUL bytes the file
system left after a cut) is skipped and counted. A line longer than
`max_line` is skipped. At most `max_read` bytes are read per call.
"""
import collections
import hashlib
import json
import os

MAX_LINE = 65536
HEAD = 64
RECENT_LINES = 50           # fingerprints of the last lines taken, to find the place again after a cut
DONE_FILES = 3              # files read to their end, never to be read from their start again
RESYNC_MAX = 16 << 20       # a log is at most this large (the writers rotate at 5 MB)


def _fingerprint(raw):
    return hashlib.blake2b(raw.replace(b"\0", b"").strip(), digest_size=8).hexdigest()


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
        self.end = None         # fingerprint of the line that ends at the offset (None: not known)
        self.done = []          # [{"inode", "head", "size"}] of files read to their end
        self.started = False    # False until a position is known (restored or begun at the end)
        self.lines = 0
        self.skipped = 0
        self.lost = 0
        self.error = ""
        self._skip_rest = False     # inside a line that was too long
        self.recent = collections.deque(maxlen=RECENT_LINES)

    # ---- the position, for the state file

    def position(self):
        if not self.started:
            return None
        return {"inode": self.inode, "offset": self.offset, "head": self.head.hex(), "end": self.end,
                "recent": list(self.recent), "done": list(self.done)}

    def restore(self, pos):
        if isinstance(pos, dict) and type(pos.get("offset")) is int and pos["offset"] >= 0 \
                and (pos.get("inode") is None or type(pos.get("inode")) is int):
            try:
                self.head = bytes.fromhex(str(pos.get("head") or ""))[:HEAD]
            except ValueError:
                self.head = b""
            self.inode, self.offset, self.started = pos["inode"], pos["offset"], True
            self.end = pos.get("end") if isinstance(pos.get("end"), str) else None
            recent = pos.get("recent") if isinstance(pos.get("recent"), list) else []
            self.recent.extend(r for r in recent[-RECENT_LINES:] if isinstance(r, str))
            done = pos.get("done") if isinstance(pos.get("done"), list) else []
            self.done = [d for d in done[-DONE_FILES:] if isinstance(d, dict) and type(d.get("inode")) is int
                         and type(d.get("size")) is int and isinstance(d.get("head"), str)]

    def begin_at_end(self):
        try:
            st = os.stat(self.path)
            self.inode, self.offset = st.st_ino, st.st_size
            self.head = _head_of(self.path)[1]
        except OSError:
            self.inode, self.offset, self.head = None, 0, b""
        self.end = None
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

    def _done_entry(self, st, path):
        """The record of a file read to its end that is the file at `path`, or None."""
        if st is None:
            return None
        for d in self.done:
            if d["inode"] == st.st_ino:
                inode, head = _head_of(path)
                try:
                    known = bytes.fromhex(d["head"])
                except ValueError:
                    continue
                if inode == st.st_ino and head[:len(known)] == known and known:
                    return d
        return None

    def _finish(self, st, path):
        """The file the position is in was read to its end: remember it, wait for the next one."""
        if self.inode is not None and self.head:
            self.done = [d for d in self.done if d["inode"] != self.inode][-(DONE_FILES - 1):]
            self.done.append({"inode": self.inode, "head": self.head.hex(), "size": self.offset})
        self.inode, self.offset, self.head, self.end, self._skip_rest = None, 0, b"", None, False

    def _take_up(self, st, path, offset):
        self.inode, self.offset, self.head, self._skip_rest = st.st_ino, offset, _head_of(path)[1], False
        self.end = None

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
            if current is not None and self.inode is None:
                done = self._done_entry(current, self.path)
                self._take_up(current, self.path, done["size"] if done else 0)
                self._check(self.path, current.st_size)
                self._read_from(self.path, current.st_ino, events, budget)
            elif current is not None and self._is_ours(current, self.path):
                self._check(self.path, current.st_size)
                self._read_from(self.path, current.st_ino, events, budget)
            elif self.inode is None:
                pass                        # waiting for the next <file>; nothing there yet
            else:
                self._moved(current, events, budget)
            self.error = ""
        except OSError as e:
            self.error = f"{e.strerror or e}"
        return events

    def _moved(self, current, events, budget):
        """The file of the position is not at <file> any more."""
        try:
            old = os.stat(self.path + ".1")
        except OSError:
            old = None
        if self._is_ours(old, self.path + ".1"):
            self._check(self.path + ".1", old.st_size)
            budget -= self._read_from(self.path + ".1", old.st_ino, events, budget, final=True)
            if self.offset < old.st_size:
                return                      # more of .1 next time
            self._finish(old, self.path + ".1")
        elif (old is not None and (current is None or old.st_ino != current.st_ino)
              and self._done_entry(old, self.path + ".1") is None):
            # Rotated twice while not followed: the end of the file that held the position is gone,
            # but .1 came after it and was never read: all of it, then <file>.
            self.lost += 1
            self._take_up(old, self.path + ".1", 0)
            budget -= self._read_from(self.path + ".1", old.st_ino, events, budget, final=True)
            if self.offset < old.st_size:
                return
            self._finish(old, self.path + ".1")
        elif current is None:
            return                          # nothing there yet
        else:
            done = self._done_entry(current, self.path)
            if done is None:
                self.lost += 1              # replaced while not followed
            self.inode, self.offset = None, 0
        if current is not None and budget > 0:
            done = self._done_entry(current, self.path)
            self._take_up(current, self.path, done["size"] if done else 0)
            self._check(self.path, current.st_size)
            self._read_from(self.path, current.st_ino, events, budget)

    def _check(self, path, size):
        """The line that ends at the offset must be the last one taken; else the file was cut short."""
        if self.offset == 0:
            return
        if size >= self.offset and self._ends_with_last(path):
            return
        self.offset = self._resync(path, size)
        self.lost += 1

    def _ends_with_last(self, path):
        if self.end is None:
            return True                     # not known (begun at the end, or a line too long)
        window = min(self.offset, self.max_line + 2)
        with open(path, "rb") as f:
            f.seek(self.offset - window)
            data = f.read(window)
        if not data.endswith(b"\n"):
            return False
        body = data[:-1]
        if b"\n" not in body and window < self.offset:
            return True                     # a line too long to look at: no check
        return _fingerprint(body.rsplit(b"\n", 1)[-1]) == self.end

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
            self.end = None
            return len(data)
        for raw in data[:end].split(b"\n")[:-1]:
            if self._skip_rest:
                self._skip_rest = False
                self.end = None
                continue
            self.end = self._take(raw, events)
        consumed = end
        if final and end < len(data):
            consumed = len(data)
            self.skipped += 1
            self.end = None
        self.offset += consumed
        return consumed

    def _resync(self, path, size):
        """Just after the last line already taken that is still in the file, or its end."""
        with open(path, "rb") as f:
            data = f.read(min(size, RESYNC_MAX))
        known, pos, found, found_fp = set(self.recent), 0, None, None
        for raw in data.split(b"\n")[:-1]:
            pos += len(raw) + 1
            fp = _fingerprint(raw)
            if fp in known:
                found, found_fp = pos, fp
        self.end = found_fp
        return size if found is None else found

    def _take(self, raw, events):
        """Take one line; returns its fingerprint."""
        fp = _fingerprint(raw)
        raw = raw.replace(b"\0", b"").strip()
        if not raw:
            return fp
        self.recent.append(fp)
        if len(raw) > self.max_line:
            self.skipped += 1
            return fp
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            event = None
        if isinstance(event, dict):
            self.lines += 1
            events.append(event)
        else:
            self.skipped += 1
        return fp
