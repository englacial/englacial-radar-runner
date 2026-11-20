#!/usr/bin/env python3
"""Turn a copy of a radar unit's SD-card image into the card for a second, identical unit.

Run as root on another Linux computer, on a copy of the whole card image, before it is flashed:

    new_unit_image.py image COPY.img --hostname englacial-radar-2 --dry-run
    new_unit_image.py image COPY.img --hostname englacial-radar-2

or on a card whose two partitions are mounted already (the core alone):

    new_unit_image.py tree --root /media/x/writable --boot /media/x/system-boot --hostname ...

What it changes: a new identity (hostname, machine ID, SSH host keys; the Tailscale state is
removed), the settings measured on the first unit (local.yaml replaced by a conservative
template, the radar config held to its cap), the services' state under /var/lib/radar-runner,
the data drive's fstab line (by label), and optionally the ntfy alerts. It ends with a report and
the list of what is left to do on the new unit. Procedure: setup/NEW_UNIT.md.

Standard library only and no imports from the repository, so that the file can be copied alone.
Every path in the image is resolved under the image's root (an absolute symlink in the image
points into the image, never at this computer's files), nothing is written in a dry run, and the
first unexpected state stops the run.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

TOOL = "setup/new_unit_image.py"
MARKER = "/etc/radar-runner/new-unit-image.json"
DEFAULT_LABEL = "radar-data"
DEFAULT_MAX_TX_GAIN = 43.0      # manager.limits.max_tx_gain in radar_runner/common/config.py
HIGHEST_TX_GAIN = 76.0          # "never above 76" (config/radar-runner.yaml)
SSH_TYPES = {"rsa": ["-b", "3072"], "ecdsa": ["-b", "256"], "ed25519": []}
STATE = "/var/lib/radar-runner"
HOSTNAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")   # a letter first: YAML reads it as text
YAML_WORDS = {"y", "n", "yes", "no", "true", "false", "on", "off", "null"}   # ... unless it is one of these
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,16}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MACHINE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# What happens to each entry of the services' state directories, and why.
REMOVE, EMPTY, UNKNOWN_RECORD, DIRECTORY = "remove", "empty", "unknown", "directory"
STATE_RULES = {
    STATE: {
        "power_guard.json": (REMOVE, "the power service's boot-loop guard: the first unit's early-shutdown "
                                     "count, passive flag and last shutdown; missing means zero"),
        "user": (DIRECTORY, ""),
    },
    STATE + "/user": {
        "boot_count.json": (REMOVE, "the boot counter: starts again, above the names on the data drive"),
        "boot_count.json.lock": (REMOVE, "lock file of the boot counter"),
        "sdr-state": (UNKNOWN_RECORD, "the SDR-state record: not known to be safe on this unit"),
        "override-no-amp-telemetry": (REMOVE, "the user's 'run without amplifier telemetry' override"),
        "auto-blocked": (REMOVE, "the motion trigger's block after a manual stop"),
        "manager-events.jsonl": (REMOVE, "the manager's event log"),
        "manager-events.jsonl.1": (REMOVE, "the manager's older event log"),
        "watcher-events.jsonl": (REMOVE, "the safety watcher's event log"),
        "watcher-events.jsonl.1": (REMOVE, "the safety watcher's older event log"),
        "amp_safe.log": (REMOVE, "output of the make-amplifier-safe tool outside a collection"),
        "amp_safe.log.1": (REMOVE, "older output of the make-amplifier-safe tool"),
        "alerts-state.json": (REMOVE, "the alerts service's state: last boot, event-log positions, "
                                      "disk levels, storm limits"),
        "alerts-queue": (EMPTY, "alerts waiting to be sent"),
        "alerts-inbox": (EMPTY, "test alerts from alertctl"),
        "telemetry-fallback": (REMOVE, "sensor logs written to the SD card while the data drive was unusable"),
    },
}
ETC_FILES = {"local.yaml", "radar.yaml", "power.toml", "secrets.yaml", "power-disarm",
             os.path.basename(MARKER)}


class Stop(Exception):
    """An unexpected state or a refusal: nothing more is done."""


class PartlyChanged(Stop):
    """A stop after the changes to the image had begun."""


# How far the run got, for the message when it ends early: "plan" (nothing written yet),
# "dry" (a dry run: nothing is ever written), "apply" (changes being made), "done".
PROGRESS = {"phase": "plan"}


def what_was_changed():
    phase = PROGRESS["phase"]
    if phase == "apply":
        return "the image may be partly changed; start again from a fresh copy"
    if phase == "done":
        return f"all the changes were made ({MARKER} says done); only the rest of the report was lost"
    return "nothing in the image was changed"


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ paths inside the image

class Tree:
    """A mounted file system of the image. Every path is resolved under `top`, as if it were `/`."""

    def __init__(self, top, name, posix=True, ownership=True):
        self.top = os.path.realpath(top)
        self.name = name                # "root" or "boot", for messages
        self.posix = posix              # False for the FAT boot partition: no owners or modes
        self.ownership = ownership      # False: owners are left as they are (not root)

    def path(self, inside, follow_last=False):
        """The real path of `inside` (absolute in the image). Symlinks resolve inside the image;
        the last component is followed only with follow_last."""
        todo = [p for p in reversed(inside.split("/")) if p]
        done, links, missing = [], 0, False
        while todo:
            part = todo.pop()
            if part == ".":
                continue
            if part == "..":
                if done:
                    done.pop()
                continue
            here = os.path.join(self.top, *done, part)
            if missing:                         # below a missing directory nothing exists
                done.append(part)
                continue
            try:
                st = os.lstat(here)
            except FileNotFoundError:
                done.append(part)
                missing = True
                continue
            if stat.S_ISLNK(st.st_mode) and (todo or follow_last):
                links += 1
                if links > 40:
                    raise Stop(f"{self.name}:{inside}: too many levels of symlinks")
                target = os.readlink(here)
                if target.startswith("/"):
                    done = []                   # absolute: from the image's root, not this computer's
                todo.extend(reversed([p for p in target.split("/") if p]))
                continue
            if todo and not stat.S_ISDIR(st.st_mode):
                raise Stop(f"{self.name}:{inside}: {os.path.join('/', *done, part)} is not a directory")
            done.append(part)
        real = os.path.join(self.top, *done)
        parent = os.path.dirname(real)
        if os.path.commonpath([self.top, real]) != self.top or (
                os.path.isdir(parent) and os.path.realpath(parent) != parent):
            raise Stop(f"{self.name}:{inside}: resolves outside the image ({real}); not touched")
        return real

    def lstat(self, inside, follow_last=False):
        try:
            return os.lstat(self.path(inside, follow_last))
        except FileNotFoundError:
            return None

    def exists(self, inside):
        return self.lstat(inside) is not None

    def is_dir(self, inside, follow_last=False):
        st = self.lstat(inside, follow_last)
        return st is not None and stat.S_ISDIR(st.st_mode)

    def read(self, inside, limit=1 << 22):
        """The bytes of a regular file; Stop if it is anything else (a symlink included)."""
        real = self.path(inside)
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise Stop(f"{self.name}:{inside}: missing")
        except OSError as e:
            raise Stop(f"{self.name}:{inside}: cannot read ({e.strerror})")
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise Stop(f"{self.name}:{inside}: not a regular file")
            data = f.read(limit + 1)
        if len(data) > limit:
            raise Stop(f"{self.name}:{inside}: larger than expected")
        return data

    def text(self, inside):
        try:
            return self.read(inside).decode("utf-8")
        except UnicodeDecodeError:
            raise Stop(f"{self.name}:{inside}: not UTF-8 text")

    def listdir(self, inside):
        return sorted(os.listdir(self.path(inside, follow_last=True)))

    def write(self, inside, data, mode=None, uid=None, gid=None):
        """Replace (or create) a regular file atomically. Owner and mode stay unless given."""
        real = self.path(inside)
        parent, name = os.path.split(real)
        try:
            st = os.lstat(real)
        except FileNotFoundError:
            st = None
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise Stop(f"{self.name}:{inside}: exists and is not a regular file; not replaced")
        mode = mode if mode is not None else stat.S_IMODE(st.st_mode) if st else 0o644
        uid = uid if uid is not None else st.st_uid if st else 0
        gid = gid if gid is not None else st.st_gid if st else 0
        dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        tmp = f".{name}.new-unit-image.tmp"
        try:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    if self.posix:
                        if self.ownership:
                            os.fchown(f.fileno(), uid, gid)
                        os.fchmod(f.fileno(), mode)
                    os.fsync(f.fileno())
                os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass
                raise
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def remove(self, inside):
        """Delete a file, a symlink (not its target) or a whole directory. Missing is fine."""
        real = self.path(inside)
        try:
            st = os.lstat(real)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(real)         # does not follow symlinks inside
        else:
            os.unlink(real)

    def empty(self, inside):
        for name in self.listdir(inside):
            self.remove(f"{inside}/{name}")


# ------------------------------------------------------------------ safety checks

def _disks(dev):
    """Names of the whole disks under a device number (through partitions, device mapper, md)."""
    sys_path = f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}"
    if not os.path.exists(sys_path):
        return set()

    def walk(real):
        slaves = os.path.join(real, "slaves")
        names = os.listdir(slaves) if os.path.isdir(slaves) else []
        if names:
            out = set()
            for n in names:
                out |= walk(os.path.realpath(os.path.join(slaves, n)))
            return out
        if os.path.exists(os.path.join(real, "partition")):
            real = os.path.dirname(real)
        return {os.path.basename(real)}
    return walk(os.path.realpath(sys_path))


def host_system_devices():
    """(device numbers, disk names) that this computer runs from."""
    devs = set()
    for p in ("/", "/boot", "/boot/efi", "/boot/firmware", "/usr", "/var"):
        try:
            devs.add(os.stat(p).st_dev)
        except OSError:
            pass
    disks = set()
    for d in devs:
        disks |= _disks(d)
    return devs, disks


MOUNTINFO = "/proc/self/mountinfo"
HOST_MOUNTS = ("/", "/boot", "/boot/efi", "/boot/firmware", "/usr", "/var", "/home")
PSEUDO_SOURCES = {"none", "tmpfs", "overlay", "proc", "sysfs", "devtmpfs", "rootfs"}


def mount_table():
    """(mount point, fstype, source) of every mount, from the kernel's mountinfo."""
    unescape = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)     # noqa: E731
    table = []
    try:
        with open(MOUNTINFO) as f:
            for line in f:
                fields = line.split()
                if "-" in fields:
                    sep = fields.index("-")
                    table.append((unescape(fields[4]), fields[sep + 1], unescape(fields[sep + 2])))
    except OSError as e:
        raise Stop(f"cannot read {MOUNTINFO}: {e.strerror}")
    return table


def mount_of(path, table):
    """The mount that holds `path`: the longest mount point above it (the last one if stacked)."""
    best = None
    for mp, fstype, source in table:
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) >= len(best[0]):
                best = (mp, fstype, source)
    return best


def source_keys(fstype, source):
    """What makes two mounts the same storage: the source (one btrfs device for every subvolume),
    the ZFS pool, and the whole disks under a block device."""
    if source in PSEUDO_SOURCES:
        return set()
    keys = {f"source:{source}"}
    if fstype == "zfs":
        keys.add(f"zfs:{source.split('/')[0]}")
    if source.startswith("/dev/"):
        try:
            keys |= {f"disk:{d}" for d in _disks(os.stat(source).st_rdev)}
        except OSError:
            pass
    return keys


def refuse_host_tree(top, what, host=None):
    real = os.path.realpath(top)
    if real == "/":
        raise Stop(f"{what} {top} is this computer's own root file system")
    devs, disks = host if host is not None else host_system_devices()
    dev = os.stat(real).st_dev
    if dev in devs:
        raise Stop(f"{what} {top} is on the file system this computer runs from")
    shared = _disks(dev) & disks
    if shared:
        raise Stop(f"{what} {top} is on the disk this computer booted from ({', '.join(sorted(shared))})")
    # File systems with anonymous device numbers (btrfs subvolumes, ZFS, overlays): compare sources.
    table = mount_table()
    mine = mount_of(real, table)
    host_keys = set()
    for p in HOST_MOUNTS:
        m = mount_of(p, table)
        if m is not None and m != mine:
            host_keys |= source_keys(m[1], m[2])
    if mine is not None and mine[0] in HOST_MOUNTS:
        raise Stop(f"{what} {top} is on {mine[0]}, a file system this computer runs from")
    if mine is not None and source_keys(mine[1], mine[2]) & host_keys:
        raise Stop(f"{what} {top} is on {mine[2]}, the storage this computer runs from")


def check_looks_like_unit(root, boot):
    if root.top == boot.top:
        raise Stop("--root and --boot are the same directory")
    for p in ("/etc/radar-runner", "/home/englacial/englacial-radar-runner", "/var/lib", "/etc/ssh"):
        if not root.is_dir(p):
            raise Stop(f"{root.top}: no {p}: not the root file system of a radar unit's image")
    for p in ("/etc/hostname", "/etc/machine-id", "/etc/hosts", "/etc/fstab"):
        st = root.lstat(p)
        if st is None or not stat.S_ISREG(st.st_mode):
            raise Stop(f"{root.top}: {p} missing or not a regular file")
    for p in ("/config.txt", "/cmdline.txt"):
        if not boot.exists(p):
            raise Stop(f"{boot.top}: no {p[1:]}: not the boot partition of a Raspberry Pi image")


# ------------------------------------------------------------------ the plan

class Plan:
    def __init__(self):
        self.changes = []       # (text, function or None)
        self.notes = []         # warnings and decisions, for the report
        self.identity = []      # (item, old, new)
        self.after = []         # lines printed after the changes are made (fingerprints)
        self.touched = set()    # ("root"|"boot", path) written or removed by this run

    def add(self, text, fn, tree=None, path=None):
        self.changes.append((text, fn))
        if tree is not None:
            self.touched.add((tree.name, path))

    def note(self, text):
        self.notes.append(text)


class Unit:
    """The core: plans every change to a mounted root tree and boot tree, then makes them."""

    def __init__(self, root, boot, args):
        self.root, self.boot, self.args = root, boot, args
        self.plan = Plan()
        self.old_hostname = None
        self.keygen_dir = None
        self.new_prints = []

    # ---- before anything

    def check_marker(self):
        st = self.root.lstat(MARKER)
        if st is None:
            return
        try:
            m = json.loads(self.root.read(MARKER))
            when, host, state = m.get("time", "?"), m.get("hostname", "?"), m.get("state", "?")
        except (ValueError, AttributeError):
            when, host, state = "?", "?", "unreadable marker"
        text = (f"this image was changed by {TOOL} already: {when}, hostname {host} ({state})")
        if state != "done":
            raise Stop(text + ". That run is incomplete, so the image is partly changed: start again "
                       "from a fresh copy (--force does not apply)")
        if not self.args.force:
            raise Stop(text + "; run again with --force if that is intended")
        self.plan.note(text + "; running again (--force)")

    # ---- 1. identity

    def plan_hostname(self):
        r, new = self.root, self.args.hostname
        old = r.text("/etc/hostname").strip()
        if not re.match(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", old):
            raise Stop(f"/etc/hostname holds {old!r}, not a plain host name")
        self.old_hostname = old
        if old == new and not self.args.force:
            raise Stop(f"the image's hostname is already {new}")
        self.plan.identity.append(("hostname", old, new))
        if old != new:
            self.plan.add(f"/etc/hostname: {old} -> {new}",
                          lambda: r.write("/etc/hostname", f"{new}\n".encode()), r, "/etc/hostname")

        # /etc/hosts: the 127.0.1.1 line (cloud-init writes the same from its template at boot)
        lines = r.text("/etc/hosts").split("\n")
        idx = [i for i, line in enumerate(lines) if line.split()[:1] == ["127.0.1.1"]]
        if len(idx) != 1:
            raise Stop(f"/etc/hosts: expected one 127.0.1.1 line, found {len(idx)}")
        fields = lines[idx[0]].split()
        names = [new if f == old else new + f[len(old):] if f.startswith(old + ".") else f for f in fields[1:]]
        if new not in names:
            raise Stop(f"/etc/hosts: the 127.0.1.1 line names neither {old} nor {new}")
        line = "127.0.1.1 " + " ".join(names)
        if line != lines[idx[0]]:
            lines[idx[0]] = line
            body = "\n".join(lines)
            self._no_old_name("/etc/hosts", body)
            self.plan.add(f"/etc/hosts: the 127.0.1.1 line becomes '{line}'",
                          lambda: r.write("/etc/hosts", body.encode()), r, "/etc/hosts")
        self.plan_cloud_init(old, new)

    def _no_old_name(self, where, text):
        if self.old_hostname == self.args.hostname:
            return
        hits = [str(i + 1) for i, line in enumerate(text.split("\n"))
                if re.search(rf"(?<![A-Za-z0-9-]){re.escape(self.old_hostname)}(?![A-Za-z0-9-])", line)]
        if hits:
            raise Stop(f"{where}: the old hostname is still on line(s) {', '.join(hits)} after the edit; "
                       "change it by hand on the mounted image")

    def _edit_key(self, text, keys, old, new, where):
        """Replace `key: old` at the top level of a cloud-config text. Returns (text, found)."""
        lines, found = text.split("\n"), 0
        pattern = re.compile(rf"^({'|'.join(map(re.escape, keys))}):(\s*)([\"']?)([^\"'#\s]*)\3(\s*(#.*)?)$")
        for i, line in enumerate(lines):
            m = pattern.match(line)
            if not m:
                continue
            found += 1
            value = m.group(4)
            if value not in (old, new):
                raise Stop(f"{where} line {i + 1}: {m.group(1)} is {value!r}, neither {old} nor {new}")
            lines[i] = f"{m.group(1)}:{m.group(2) or ' '}{new}{m.group(5)}"
        if found > 1:
            raise Stop(f"{where}: {found} lines set {'/'.join(keys)}")
        return "\n".join(lines), found

    def plan_cloud_init(self, old, new):
        """The image got its hostname from cloud-init (`hostname:` in the boot partition's
        user-data, with manage_etc_hosts). At every boot (cloud-init 26.1, NoCloud) init-local
        reads the boot partition again, but first sets the hostname from the previous boot's
        cloud-config.txt in the instance directory; update_hostname and update_etc_hosts then run
        from the new user-data. So the boot partition's user-data, that cached cloud-config.txt
        (the run stops if it still names the old host after the edit) and cloud-init's records of
        the hostname (previous-hostname, set-hostname) all get the new name. The instance ID is
        never changed: a new one would run every first-boot module again."""
        b, r = self.boot, self.root
        for name, keys in (("/user-data", ["hostname"]), ("/meta-data", ["local-hostname", "local_hostname",
                                                                         "hostname"])):
            if not b.exists(name):
                self.plan.note(f"boot partition: no {name[1:]}")
                continue
            text = b.text(name)
            if name == "/user-data" and not text.startswith("#cloud-config"):
                if old in text and old != new:
                    raise Stop("boot partition: user-data is not a #cloud-config file and names the old "
                               "hostname; edit it by hand")
                continue
            if name == "/user-data" and re.search(r"^fqdn:", text, re.M):
                raise Stop("boot partition: user-data sets fqdn; not handled, edit it by hand")
            edited, found = self._edit_key(text, keys, old, new, f"boot:{name}")
            self._no_old_name(f"boot:{name}", edited)
            if edited != text:
                self.plan.add(f"boot partition {name[1:]}: {keys[0]}: {old} -> {new} "
                              "(cloud-init sets the hostname from it at every boot)",
                              lambda e=edited, n=name: b.write(n, e.encode()), b, name)
            elif not found:
                self.plan.note(f"boot partition {name[1:]} sets no hostname")
        if not r.is_dir("/var/lib/cloud"):
            self.plan.note("no /var/lib/cloud: cloud-init has never run on this image")
            return
        if r.exists("/etc/cloud/cloud-init.disabled"):
            self.plan.note("cloud-init is disabled in this image (/etc/cloud/cloud-init.disabled)")
        prev = "/var/lib/cloud/data/previous-hostname"
        if r.exists(prev):
            value = r.text(prev).strip()
            if value == old and old != new:
                self.plan.add(f"{prev}: {old} -> {new}",
                              lambda: r.write(prev, f"{new}\n".encode()), r, prev)
            elif value != new:
                self.plan.note(f"{prev} holds {value!r}; left as it is")
        seth = "/var/lib/cloud/data/set-hostname"
        if r.exists(seth):
            try:
                data = json.loads(r.text(seth))
            except ValueError:
                raise Stop(f"{seth}: not JSON")
            changed = {k: new if v == old else new + v[len(old):] if isinstance(v, str) and v.startswith(old + ".")
                       else v for k, v in data.items()}
            if changed != data:
                body = json.dumps(changed, indent=1, sort_keys=True, separators=(",", ": ")) + "\n"
                self.plan.add(f"{seth}: hostname and fqdn -> {new}",
                              lambda: r.write(seth, body.encode()), r, seth)
        instance = "/var/lib/cloud/instance"
        if not r.is_dir(instance, follow_last=True):
            self.plan.note("no current cloud-init instance directory; nothing cached")
            return
        real = os.path.relpath(r.path(instance, follow_last=True), r.top)
        base = "/" + real
        pkl = f"{base}/obj.pkl"
        if r.exists(pkl):
            self.plan.add(f"{pkl}: removed (a pickled copy of the old user-data; for NoCloud cloud-init "
                          "finds it invalid at boot and reads the boot partition anyway)",
                          lambda: r.remove(pkl), r, pkl)
        if old == new:
            return
        # Read at boot, before the new user-data: the hostname in these must be the new one.
        for name in ("cloud-config.txt", "vendor-cloud-config.txt", "vendor2-cloud-config.txt"):
            path = f"{base}/{name}"
            if not r.exists(path):
                continue
            text = r.text(path)
            edited, _ = self._edit_key(text, ["hostname"], old, new, path)
            self._no_old_name(f"{path} (cloud-init sets the hostname from it at boot)", edited)
            if edited != text:
                self.plan.add(f"{path}: hostname: {old} -> {new} (cloud-init sets the hostname from the "
                              "previous boot's copy before it reads the new user-data)",
                              lambda p=path, e=edited: r.write(p, e.encode()), r, path)
        # Raw copies of the user-data, written again from the boot partition at every boot.
        for name in ("user-data.txt", "user-data.txt.i"):
            path = f"{base}/{name}"
            if not r.exists(path):
                continue
            data = r.read(path)
            edited = re.sub(rb"(?m)^hostname:(\s*)" + re.escape(old.encode()) + rb"(\s*)$",
                            rb"hostname:\g<1>" + new.encode() + rb"\g<2>", data)
            if edited != data:
                self.plan.add(f"{path}: hostname: {old} -> {new} (a copy of the user-data; cloud-init writes "
                              "it again from the boot partition at boot)",
                              lambda p=path, e=edited: r.write(p, e), r, path)

    def plan_machine_id(self):
        r = self.root
        old = r.text("/etc/machine-id").strip()
        if not MACHINE_ID_RE.match(old):
            raise Stop(f"/etc/machine-id holds {old!r}, not a machine ID")
        new = uuid.uuid4().hex
        self.plan.identity.append(("machine-id", old, new))
        # Never empty: systemd would treat the next boot as the first one and apply unit presets.
        self.plan.add(f"/etc/machine-id: {old} -> {new}",
                      lambda: r.write("/etc/machine-id", f"{new}\n".encode(), 0o444, 0, 0), r, "/etc/machine-id")
        dbus = "/var/lib/dbus/machine-id"
        st = r.lstat(dbus)
        if st is None:
            pass
        elif stat.S_ISLNK(st.st_mode):
            target = os.readlink(r.path(dbus))
            if os.path.normpath(os.path.join("/var/lib/dbus", target)) != "/etc/machine-id":
                raise Stop(f"{dbus} is a symlink to {target}, not to /etc/machine-id")
            self.plan.note(f"{dbus} is a link to /etc/machine-id: left as it is")
        elif stat.S_ISREG(st.st_mode):
            self.plan.add(f"{dbus}: -> {new}", lambda: r.write(dbus, f"{new}\n".encode()), r, dbus)
        else:
            raise Stop(f"{dbus} is neither a file nor a symlink")
        journal = f"/var/log/journal/{old}"
        if r.is_dir(journal, follow_last=True):
            self.plan.add(f"{journal}: removed (the first unit's journal; journald starts one under the new ID)",
                          lambda: r.remove(journal), r, journal)
        if r.is_dir("/var/log/journal", follow_last=True):
            others = [n for n in r.listdir("/var/log/journal") if MACHINE_ID_RE.match(n) and n not in (old, new)]
            if others:
                self.plan.note(f"/var/log/journal also has {', '.join(others)}: left as it is")
        seed = "/var/lib/systemd/random-seed"
        if r.exists(seed):
            self.plan.add(f"{seed}: removed (the same seed must not start two units; systemd makes a new one)",
                          lambda: r.remove(seed), r, seed)

    def plan_ssh(self):
        r, new = self.root, self.args.hostname
        names = [n for n in r.listdir("/etc/ssh") if n.startswith("ssh_host_")]
        types = []
        for n in names:
            m = re.match(r"^ssh_host_([a-z0-9]+)_key(\.pub)?$", n)
            if not m or m.group(1) not in SSH_TYPES:
                raise Stop(f"/etc/ssh/{n}: not an SSH host key of a type this tool makes (rsa, ecdsa, ed25519)")
            st = r.lstat(f"/etc/ssh/{n}")
            if not stat.S_ISREG(st.st_mode):
                raise Stop(f"/etc/ssh/{n}: not a regular file")
            if m.group(1) not in types:
                types.append(m.group(1))
        if not types:
            types = list(SSH_TYPES)
            self.plan.note("no SSH host keys in the image: all three types are made")
        if shutil.which("ssh-keygen") is None:
            raise Stop("ssh-keygen is not installed on this computer")
        old_prints = []
        for t in types:
            pub = f"/etc/ssh/ssh_host_{t}_key.pub"
            if r.exists(pub):
                old_prints.append(fingerprint(r.read(pub)))
        self.plan.identity.append(("SSH host keys", "; ".join(old_prints) or "none", "(made by this run, below)"))
        for t in types:
            self.plan.add(f"/etc/ssh/ssh_host_{t}_key and .pub: a new {t} key, root@{new} "
                          "(root:root, 0600 and 0644)", lambda t=t: self._new_key(t), r, f"/etc/ssh/ssh_host_{t}_key")
            self.plan.touched.add(("root", f"/etc/ssh/ssh_host_{t}_key.pub"))

    def _new_key(self, kind):
        if self.keygen_dir is None:     # 0700, in memory where there is a tmpfs for it
            memory = next((d for d in ("/dev/shm", "/run") if os.path.isdir(d) and os.access(d, os.W_OK)), None)
            self.keygen_dir = tempfile.mkdtemp(prefix="new-unit-keys.", dir=memory)
        key = os.path.join(self.keygen_dir, f"ssh_host_{kind}_key")
        subprocess.run(["ssh-keygen", "-q", "-t", kind, *SSH_TYPES[kind], "-N", "", "-C",
                        f"root@{self.args.hostname}", "-f", key], check=True, stdin=subprocess.DEVNULL)
        with open(key, "rb") as f:
            private = f.read()
        with open(key + ".pub", "rb") as f:
            public = f.read()
        self.root.write(f"/etc/ssh/ssh_host_{kind}_key", private, 0o600, 0, 0)
        self.root.write(f"/etc/ssh/ssh_host_{kind}_key.pub", public, 0o644, 0, 0)
        self.new_prints.append(fingerprint(public))

    # ---- 2. Tailscale

    def plan_tailscale(self):
        r = self.root
        flags = r.text("/etc/default/tailscaled") if r.exists("/etc/default/tailscaled") else ""
        dropins = "/etc/systemd/system/tailscaled.service.d"
        if r.is_dir(dropins, follow_last=True):
            flags += "".join(r.text(f"{dropins}/{n}") for n in r.listdir(dropins) if n.endswith(".conf"))
        if "--state" in flags or "--statedir" in flags:
            raise Stop("tailscaled is configured with its own --state or --statedir; not handled")
        found = False
        for d in ("/var/lib/tailscale", "/var/cache/tailscale"):
            if not r.is_dir(d, follow_last=True):
                continue
            found = True
            entries = r.listdir(d)
            if entries:
                self.plan.add(f"{d}: its {len(entries)} entries removed (node key, login, preferences): "
                              "the unit starts logged out", lambda d=d: r.empty(d), r, d)
        if not found:
            self.plan.note("no Tailscale state directory in the image")
        self.plan.identity.append(("Tailscale node", "the first unit's node", "none until logged in by hand"))

    # ---- 3. per-unit settings

    def plan_unit_config(self):
        r, cap = self.root, self.args.max_tx_gain
        local = "/etc/radar-runner/local.yaml"
        old_cap = "not set"
        if r.exists(local):
            found = re.findall(r"^\s*max_tx_gain:\s*([0-9.eE+-]+)", r.text(local), re.M)
            old_cap = "/".join(found) or "not set"
        body = LOCAL_TEMPLATE.format(hostname=self.args.hostname, old=self.old_hostname, date=now_utc()[:10],
                                     cap=f"{cap:g}")
        self.plan.add(f"{local}: replaced by a template (max_tx_gain {old_cap} -> {cap:g}, turn-on check "
                      "not blocking, no expected values, nothing else carried over)",
                      lambda: r.write(local, body.encode(), 0o644, 0, 0), r, local)
        self.plan_radar_config(cap)
        power = "/etc/radar-runner/power.toml"
        if r.exists(power):
            text = r.text(power)
            lines = [i for i, line in enumerate(text.split("\n")) if re.match(r"^armed\s*=", line)]
            if len(lines) > 1:
                raise Stop(f"{power}: {len(lines)} 'armed' lines")
            armed = bool(lines) and re.match(r"^armed\s*=\s*true\b", text.split("\n")[lines[0]]) is not None
            if self.args.disarm_power and armed:
                out = text.split("\n")
                out[lines[0]] = re.sub(r"^armed\s*=\s*true", "armed = false", out[lines[0]])
                body2 = "\n".join(out)
                self.plan.add(f"{power}: armed = true -> false (monitor-only until the power tests on the new unit)",
                              lambda: r.write(power, body2.encode()), r, power)
            else:
                self.plan.note(f"{power}: kept as it is (armed = {'true' if armed else 'false'})"
                               + ("; --disarm-power is recommended until the power-service bench tests pass "
                                  "on the new unit" if armed else ""))
        if r.exists("/etc/radar-runner/power-disarm"):
            self.plan.note("/etc/radar-runner/power-disarm exists: the power service runs monitor-only; kept")
        unknown = [n for n in r.listdir("/etc/radar-runner") if n not in ETC_FILES]
        if unknown:
            self.plan.note(f"/etc/radar-runner also holds {', '.join(unknown)}: not read and left as it is; "
                           "check whether it belongs to the first unit")

    def plan_radar_config(self, cap):
        """A tx_gain above the cap would be refused by the manager at every start (a fatal config
        fault). It is lowered to the cap instead, so that the new unit has a config it may run, at
        the power that is safe for an amplifier that has not been measured."""
        r, path = self.root, "/etc/radar-runner/radar.yaml"
        if not r.exists(path):
            self.plan.note(f"{path}: not present")
            return
        lines = r.text(path).split("\n")
        heads = [i for i, line in enumerate(lines) if re.match(r"^RF0:\s*(#.*)?$", line)]
        if len(heads) != 1:
            raise Stop(f"{path}: expected one 'RF0:' section, found {len(heads)}")
        end = next((i for i in range(heads[0] + 1, len(lines)) if re.match(r"^[^\s#]", lines[i])), len(lines))
        pattern = re.compile(r"^(\s+)tx_gain:(\s*)([^\s#]+)(\s*)(#.*)?$")
        gains = [i for i in range(heads[0] + 1, end) if pattern.match(lines[i])]
        if len(gains) != 1:
            raise Stop(f"{path}: expected one tx_gain line in RF0, found {len(gains)}")
        m = pattern.match(lines[gains[0]])
        try:
            value = float(m.group(3))
        except ValueError:
            raise Stop(f"{path}: RF0.tx_gain is {m.group(3)!r}, not a number")
        if value <= cap:
            self.plan.note(f"{path}: RF0.tx_gain {value:g} is within the cap {cap:g}: unchanged")
            return
        head = f"{m.group(1)}tx_gain:{m.group(2) or ' '}{cap:g}"
        column = len(lines[gains[0]]) - len(m.group(5) or "") if m.group(5) else len(head) + 2
        line = head.ljust(max(column, len(head) + 1)) + (f"# lowered from {m.group(3)} by {TOOL}: the cap "
                                                         "for an amplifier not measured yet")
        lines[gains[0]] = line
        body = "\n".join(lines)
        self.plan.add(f"{path}: RF0.tx_gain {m.group(3)} -> {cap:g} (the cap; the rest of the file unchanged)",
                      lambda: r.write(path, body.encode()), r, path)

    # ---- 4. state

    def plan_state(self):
        r = self.root
        if not r.is_dir(STATE):
            self.plan.note(f"no {STATE}: the services start without state anyway")
        else:
            for directory, rules in STATE_RULES.items():
                if not r.is_dir(directory):
                    continue
                for name in r.listdir(directory):
                    self._plan_state_entry(directory, name, rules)
        drift = "/var/lib/chrony/chrony.drift"
        if r.exists(drift):
            self.plan.add(f"{drift}: removed (the first unit's clock drift; chrony measures the new one)",
                          lambda: r.remove(drift), r, drift)

    def _plan_state_entry(self, directory, name, rules):
        r, path = self.root, f"{directory}/{name}"
        st = r.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            raise Stop(f"{path} is a symlink: not expected in the state directory")
        if name.endswith(".tmp") and stat.S_ISREG(st.st_mode):
            self.plan.add(f"{path}: removed (left by an interrupted write)", lambda: r.remove(path), r, path)
            return
        if name not in rules:
            raise Stop(f"{path}: not known to this tool, so it cannot say whether the new unit should "
                       "keep it; remove it (or keep it) by hand on the mounted image, then run again")
        what, why = rules[name]
        is_dir = stat.S_ISDIR(st.st_mode)
        if what == DIRECTORY:
            if not is_dir:
                raise Stop(f"{path} is not a directory")
        elif what == REMOVE:
            self.plan.add(f"{path}: removed ({why})", lambda: r.remove(path), r, path)
        elif what == EMPTY:
            if not is_dir:
                raise Stop(f"{path} is not a directory")
            n = len(r.listdir(path))
            if n:
                self.plan.add(f"{path}: its {n} entries removed ({why})", lambda: r.empty(path), r, path)
        elif what == UNKNOWN_RECORD:
            if not stat.S_ISREG(st.st_mode):
                raise Stop(f"{path} is not a regular file")
            body = (f"unknown\nwritten {now_utc()} by {TOOL} for a new unit: nothing is known about "
                    "this unit's SDR yet\n").encode()
            self.plan.add(f"{path}: `unknown` ({why})", lambda: r.write(path, body, 0o644), r, path)

    # ---- 5. data drive

    def plan_fstab(self):
        r, label = self.root, self.args.label
        lines = r.text("/etc/fstab").split("\n")
        live = [i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")]
        mine = [i for i in live if lines[i].split()[1:2] == ["/mnt/nvme"]]
        if len(mine) != 1:
            raise Stop(f"/etc/fstab: expected exactly one line for /mnt/nvme, found {len(mine)}")
        others = [i for i in live if i != mine[0] and lines[i].split()[0] == f"LABEL={label}"]
        if others:
            raise Stop(f"/etc/fstab: line {others[0] + 1} already mounts LABEL={label}")
        m = re.match(r"^(\s*)(\S+)(\s+/mnt/nvme\s+\S+\s+(\S+).*)$", lines[mine[0]])
        if not m:
            raise Stop("/etc/fstab: the /mnt/nvme line has no options field")
        if "nofail" not in m.group(4).split(","):
            raise Stop("/etc/fstab: the /mnt/nvme line has no 'nofail'; not touching it (a missing drive "
                       "could stop the boot)")
        old = m.group(2)
        if old == f"LABEL={label}":
            self.plan.note(f"/etc/fstab: /mnt/nvme is mounted by LABEL={label} already")
            return
        lines[mine[0]] = f"{m.group(1)}LABEL={label}{m.group(3)}"
        body = "\n".join(lines)
        self.plan.add(f"/etc/fstab: /mnt/nvme {old} -> LABEL={label} (options kept: {m.group(4)})",
                      lambda: r.write("/etc/fstab", body.encode()), r, "/etc/fstab")
        self.plan.identity.append(("data drive (fstab)", old, f"LABEL={label}"))

    # ---- 6. alerts

    def plan_alerts(self):
        r, path = self.root, "/etc/radar-runner/secrets.yaml"
        if not r.exists(path):
            self.plan.note(f"{path}: not present; alerts are not configured")
            return
        st = r.lstat(path)
        if stat.S_IMODE(st.st_mode) != 0o640 or st.st_uid != 0:
            self.plan.note(f"{path}: mode {stat.S_IMODE(st.st_mode):04o}, uid {st.st_uid} "
                           "(expected 0640, root:englacial); kept as found")
        text = r.text(path)     # never printed
        if self.args.ntfy_topic is None and not self.args.no_alerts:
            self.plan.note("alerts: the ntfy settings are kept, so both units post to the same topic "
                           "(each alert names its unit's hostname). --ntfy-topic or --no-alerts to change that")
            if self.old_hostname and self.old_hostname != self.args.hostname and self.old_hostname in text:
                self.plan.note(f"{path} contains the old hostname (for example in the topic); not changed")
            return
        lines = text.split("\n")
        heads = [i for i, line in enumerate(lines) if re.match(r"^alerts\s*:", line)]
        if not heads:
            if self.args.no_alerts:
                self.plan.note(f"{path}: no alerts section; alerts are not configured already")
                return
            raise Stop(f"{path}: no top-level 'alerts:' section; set the topic by hand")
        if len(heads) > 1 or not re.match(r"^alerts:\s*(#.*)?$", lines[heads[0]]):
            raise Stop(f"{path} line {heads[0] + 1}: the alerts section is not in the plain block layout; "
                       "edit it by hand")
        end = next((i for i in range(heads[0] + 1, len(lines)) if re.match(r"^[^\s#]", lines[i])), len(lines))
        block = lines[heads[0] + 1:end]
        if any("\t" in line or re.search(r"(^|\s)[&*!]", line.split("#")[0]) for line in block):
            raise Stop(f"{path}: the alerts section uses tabs, anchors, aliases or tags; edit it by hand")
        if self.args.no_alerts:
            out = lines[:heads[0]] + lines[end:]
            if not any(line.strip() and not line.lstrip().startswith("#") for line in out):
                out = [f"# alerts removed by {TOOL} for a new unit", ""]
            body = "\n".join(out)
            self.plan.add(f"{path}: the alerts section removed (server, topic and token): alerts are only "
                          "queued on the new unit (other settings in the file kept; owner and mode kept)",
                          lambda: r.write(path, body.encode()), r, path)
            return
        ntfy = [i for i in range(heads[0] + 1, end) if re.match(r"^(\s+)ntfy\s*:", lines[i])]
        if len(ntfy) != 1 or not re.match(r"^(\s+)ntfy:\s*(#.*)?$", lines[ntfy[0]]):
            raise Stop(f"{path}: expected one 'ntfy:' block under alerts; set the topic by hand")
        indent = len(lines[ntfy[0]]) - len(lines[ntfy[0]].lstrip())
        sub_end = next((i for i in range(ntfy[0] + 1, end) if lines[i].strip() and not lines[i].lstrip()
                        .startswith("#") and len(lines[i]) - len(lines[i].lstrip()) <= indent), end)
        topics = [i for i in range(ntfy[0] + 1, sub_end) if re.match(r"^\s+topic\s*:", lines[i])]
        if len(topics) != 1:
            raise Stop(f"{path}: expected one topic line under alerts.ntfy, found {len(topics)}")
        lead = re.match(r"^(\s+)", lines[topics[0]]).group(1)
        lines[topics[0]] = f'{lead}topic: "{self.args.ntfy_topic}"'
        body = "\n".join(lines)
        self.plan.add(f"{path}: alerts.ntfy.topic set to {self.args.ntfy_topic} (server and token kept; owner "
                      "and mode kept)", lambda: r.write(path, body.encode()), r, path)
        self.plan.note("alerts: the ntfy token must be allowed to post to the new topic")

    # ---- run

    def leftovers(self):
        """Things in the image that this tool does not touch, for the report (names only)."""
        r, out = self.root, []
        home = "/home/englacial"
        for p, what in ((".bash_history", "shell history"), (".claude", "Claude Code data"),
                        (".claude.json", "Claude Code settings and login"), (".config/gh", "GitHub CLI login"),
                        (".ssh/id_ed25519", "the user's own SSH key (the same on both units)"),
                        (".ssh/id_rsa", "the user's own SSH key (the same on both units)"),
                        (".copilot", "Copilot data"), (".vscode-server", "VS Code server"),
                        ("20261114-network-backups", "network configuration backups"),
                        ("radar-testbench", "bench files"), ("radar-config-staging", "bench radar configs")):
            if r.exists(f"{home}/{p}"):
                out.append(f"{home}/{p}: {what}")
        for p, what in (("/var/log", "the system logs other than the journal (syslog, cloud-init.log, auth.log)"),
                        ("/var/lib/snapd/device", "snapd's device identity"),
                        ("/var/lib/ubuntu-advantage/private", "an Ubuntu Pro attachment, if any"),
                        ("/etc/netplan", "the network configuration (shared Wi-Fi settings, static addresses)")):
            if r.exists(p):
                out.append(f"{p}: {what}")
        return out

    def scan_old_name(self):
        """Files that still name the old hostname (in a dry run: that this run does not change)."""
        old = self.old_hostname
        if not old or old == self.args.hostname:
            return []
        hits = []
        for tree, top in ((self.root, "/etc"), (self.root, "/var/lib/cloud"), (self.boot, "/")):
            base = tree.path(top, follow_last=True)        # resolved inside the image
            st = os.lstat(base) if os.path.lexists(base) else None
            if st is None or not stat.S_ISDIR(st.st_mode):
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                if tree is self.boot:
                    dirnames[:] = []
                for name in filenames:
                    real = os.path.join(dirpath, name)
                    inside = "/" + os.path.relpath(real, tree.top)
                    if (tree.name, inside) in self.plan.touched or (tree is self.root and inside == MARKER):
                        continue        # written by this run, and checked where it matters
                    try:
                        st = os.lstat(real)
                        if not stat.S_ISREG(st.st_mode) or st.st_size > 1 << 20:
                            continue
                        with open(real, "rb") as f:
                            if old.encode() in f.read():
                                hits.append(f"{tree.name}:{inside}")
                    except OSError:
                        hits.append(f"{tree.name}:{inside} (unreadable)")
        return hits

    def make_plan(self):
        self.check_marker()
        self.plan_hostname()
        self.plan_machine_id()
        self.plan_ssh()
        self.plan_tailscale()
        self.plan_unit_config()
        self.plan_state()
        self.plan_fstab()
        self.plan_alerts()

    def apply(self):
        r = self.root
        marker = {"tool": TOOL, "time": now_utc(), "hostname": self.args.hostname,
                  "old_hostname": self.old_hostname, "state": "started"}
        r.write(MARKER, (json.dumps(marker, indent=1) + "\n").encode(), 0o644, 0, 0)
        try:
            for text, fn in self.plan.changes:
                try:
                    fn()
                except (Stop, OSError, subprocess.CalledProcessError) as e:
                    raise PartlyChanged(f"while making the change '{text}': {e}. The image is partly "
                                        "changed: start again from a fresh copy")
                print(f"done: {text}", flush=True)
        finally:
            if self.keygen_dir:
                wipe(self.keygen_dir)
        marker["state"] = "done"
        r.write(MARKER, (json.dumps(marker, indent=1) + "\n").encode(), 0o644, 0, 0)

    def run(self):
        dry = self.args.dry_run
        PROGRESS["phase"] = "dry" if dry else "plan"
        self.make_plan()
        print(f"\n== {'Changes this run would make (dry run: nothing is written)' if dry else 'Changes'} ==")
        if dry:
            for text, _ in self.plan.changes:
                print(f"would: {text}")
            print(f"would: {MARKER}: the record that this tool has run")
        else:
            PROGRESS["phase"] = "apply"
            self.apply()
            PROGRESS["phase"] = "done"
        self.report()

    def report(self):
        p, dry = self.plan, self.args.dry_run
        print("\n== Identity (old -> new) ==")
        for item, old, new in p.identity:
            print(f"{item}: {old} -> {new}")
        if not dry and getattr(self, "new_prints", None):
            print("new SSH host keys (compare at the first connection):")
            for line in self.new_prints:
                print(f"  {line}")
        if p.notes:
            print("\n== Notes ==")
            for n in p.notes:
                print(f"- {n}")
        if not self.root.ownership:
            print("- not run as root: file owners were left as they were (--skip-ownership)")
        hits = self.scan_old_name()
        if hits:
            print(f"\n== Files that {'this run leaves with' if dry else 'still name'} the old hostname "
                  f"{self.old_hostname} (not changed; check them) ==")
            for h in hits:
                print(f"- {h}")
        left = self.leftovers()
        if left:
            print("\n== Left in the image as it was (clear by hand if the new unit should not have it) ==")
            for line in left:
                print(f"- {line}")
        print("\n== On the new unit, after its first boot, in this order (setup/NEW_UNIT.md) ==")
        steps = AFTER_FIRST_BOOT.format(label=self.args.label, cap=f"{self.args.max_tx_gain:g}",
                                        old=self.old_hostname or "the first unit")
        for i, line in enumerate(steps.strip().split("\n"), 1):
            print(f"{i}. {line}")
        if dry:
            print("\nDry run: nothing was written.")


LOCAL_TEMPLATE = """\
# Per-unit settings for {hostname} (layer 3; see config/radar-runner.yaml).
# Written {date} by setup/new_unit_image.py from the image of {old}. Nothing was
# carried over from that unit: its values belong to its own amplifier board and gain divider.
manager:
  limits:
    # The repository's conservative default. Measure this unit's amplifier output
    # against tx_gain (with its own VVA setting) before raising it.
    max_tx_gain: {cap}
  turn_on_check:
    # Not blocking and no expected values until a start on this unit has been verified
    # (tools/turn_on_check/measure.py prints the expected block).
    blocking: false
    expected:
      verified: false
"""

AFTER_FIRST_BOOT = """
Log in to Tailscale as a new node: sudo tailscale up --ssh --advertise-tags=tag:radar and open the link it prints (Tailscale SSH and the tag were in the removed state; the tag for the lab unit is your choice; a tagged node has key expiry off already, an untagged one needs it disabled in the admin console). Then sudo tailscale set --auto-update=false (also in the removed state). Do not log either unit out of Tailscale: they shared one node key, and logging out expires it, which would cut off the field unit. For the same reason do not remove or expire {old} in the admin console.
setup/check.py: the identity lines (hostname, machine-id, SSH host keys, Tailscale node) must all differ from the field unit's.
The data drive: find it with lsblk (a unit may have two NVMe devices). A new drive needs a partition and sudo mkfs.ext4 -L {label} <partition>; one with a file system needs sudo e2label <partition> {label}. After relabelling a mounted drive the device cache is stale: check the label with setup/fs_label.py if that file exists, and see setup/SYSTEM_CHANGES.md, section --nvme-by-label, for the stale cache (this tool has already put LABEL= in /etc/fstab). Then sudo mount /mnt/nvme (or reboot) and sudo setup/setup.sh, which creates /mnt/nvme/radar-runner on it.
Provision the GPS receiver once: setup/gps_receiver.py --apply, sudo systemctl restart radar-telemetry, setup/gps_receiver.py, setup/check.py --receiver.
Compare the bootloader settings with the field unit's (setup/check.py, the bootloader line; rpi-eeprom-config).
Measure the amplifier's gain (output power against tx_gain, with this unit's VVA) before raising manager.limits.max_tx_gain above {cap} in /etc/radar-runner/local.yaml and RF0.tx_gain in /etc/radar-runner/radar.yaml.
Amplifier current calibration: telemetry.ola.current_cal in the repository's config/radar-runner.yaml was measured on the first unit and reaches this one through the checkout: measure it again (clamp meter on the amplifier's supply) and set this unit's value in local.yaml.
Turn-on check: once a start at low power is verified on this unit, put the expected values that tools/turn_on_check/measure.py prints in local.yaml.
Power service bench tests on the new hardware (power/README.md, and the three watchdog tests of SYSTEM_CHANGES.md section 8). Until they pass, keep the power service disarmed (--disarm-power when making the image, as SYSTEM_CHANGES.md says for a rebuild), then set armed = true in /etc/radar-runner/power.toml.
Alerts: send a test alert (alertctl test --wait 60) and decide this unit's ntfy topic.
Write a HARDWARE_CONFIG.md for this unit (its amplifier, VVA divider, drive, receiver, serial numbers).
"""


def wipe(directory):
    """Overwrite each file of a flat directory with zeros, then remove the directory."""
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
            try:
                os.write(fd, b"\0" * os.fstat(fd).st_size)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    shutil.rmtree(directory, ignore_errors=True)


def fingerprint(public):
    """`bits SHA256:... (TYPE)` of a public key, through ssh-keygen on this computer."""
    out = subprocess.run(["ssh-keygen", "-l", "-f", "-"], input=public, capture_output=True, check=False)
    if out.returncode != 0:
        return "unreadable key"
    fields = out.stdout.decode().split()
    return f"{fields[-1].strip('()')} {fields[1]}" if len(fields) >= 2 else "unreadable key"


# ------------------------------------------------------------------ the image wrapper

def run_cmd(argv, **kw):
    out = subprocess.run(argv, capture_output=True, text=True, **kw)
    if out.returncode != 0:
        raise Stop(f"{' '.join(argv)}: {(out.stderr or out.stdout).strip()}")
    return out.stdout.strip()


def probe(dev):
    out = run_cmd(["blkid", "-p", "-o", "export", dev])
    return dict(line.split("=", 1) for line in out.split("\n") if "=" in line)


class CleanupIncomplete(Exception):
    """The image is still mounted or attached: it must not be flashed or copied."""


def say(text):
    """A line on stderr that a broken pipe on stdout (| tee) cannot stop."""
    try:
        print(text, file=sys.stderr, flush=True)
    except OSError:
        pass


def attached_loops(image):
    """Every loop device attached to the image file, asked of the kernel (losetup -j)."""
    out = run_cmd(["losetup", "-j", image])
    return [line.split(":", 1)[0] for line in out.split("\n") if line.strip()]


def clean_up(image, loop, workdir, mount_points):
    """Unmount whatever is mounted at our mount points, detach the loop device this run attached
    (every one of the image only if the attach was interrupted before its name was known), then
    check both. Returns what is left (empty: clean)."""
    left = []
    os.sync()
    for d in reversed(mount_points):
        for _ in range(5):
            if not os.path.ismount(d):
                break
            subprocess.run(["umount", d], check=False)
            if not os.path.ismount(d):
                break
            time.sleep(1)
        if os.path.ismount(d):
            left.append(f"{d} still mounted")
    try:
        mine = lambda: [d for d in attached_loops(image) if loop is None or d == loop]   # noqa: E731
        if not left:            # never detach a device with a file system mounted from it
            for d in mine():
                subprocess.run(["losetup", "-d", d], check=False)
            for _ in range(10):         # a busy device is only marked for detaching: wait, then check
                if not mine():
                    break
                time.sleep(0.5)
        still = mine()
        if still:
            left.append(f"{', '.join(still)} still attached to {image}")
        others = [d for d in attached_loops(image) if d not in still]
        if others:
            left.append(f"{', '.join(others)} also attached to {image}, not by this run: left as it is")
    except Stop as e:
        left.append(f"cannot ask which loop devices the image has ({e})")
    for d in reversed(mount_points):
        if not os.path.ismount(d):
            try:
                os.rmdir(d)             # rmdir only: never deletes anything inside a mount
            except OSError:
                pass
    if workdir:
        try:
            os.rmdir(workdir)           # only if empty
        except OSError:
            pass
    return left


def with_image(args):
    """Attach the image with partition scanning, mount both partitions, run the core, then sync,
    unmount and detach, whatever happens (errors, Ctrl-C, SIGTERM, a broken pipe)."""
    image = os.path.realpath(args.image)
    try:
        st = os.stat(image)
    except OSError as e:
        raise Stop(f"{args.image}: {e.strerror}")
    if not stat.S_ISREG(st.st_mode):
        raise Stop(f"{args.image} is not an image file (for a card or its mounted partitions use 'tree')")
    if os.geteuid() != 0:
        raise Stop("run as root (losetup and mount)")
    for tool in ("losetup", "mount", "umount", "blkid", "ssh-keygen"):
        if shutil.which(tool) is None:
            raise Stop(f"{tool} is not installed on this computer")
    # One run per image at a time, held for the whole run (also keeps two runs from mounting it).
    lock = os.open(image, os.O_RDONLY | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop(f"another run of this tool holds {image}; wait for it to end")
        with_locked_image(args, image)
    finally:
        os.close(lock)


def with_locked_image(args, image):
    if attached_loops(image):
        raise Stop(f"{image} is attached to a loop device already (losetup -j): detach it first")

    def interrupted(signum, frame):
        raise KeyboardInterrupt
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, interrupted)
    mount_points, attaching, run = [], False, {"loop": None, "workdir": None}
    try:
        attaching = True        # from here the clean-up asks the kernel what is attached
        loop = run_cmd(["losetup", "--find", "--show", "--partscan"] + (["--read-only"] if args.dry_run else [])
                       + [image])
        run["loop"] = loop
        say(f"image attached as {loop}")
        if shutil.which("udevadm"):
            subprocess.run(["udevadm", "settle", "--timeout=10"], check=False)
        parts = {}
        for _ in range(50):
            parts = {n: f"/dev/{n}" for n in os.listdir(f"/sys/block/{os.path.basename(loop)}")
                     if n.startswith(os.path.basename(loop) + "p")}
            if len(parts) >= 2 and all(os.path.exists(d) for d in parts.values()):
                break
            time.sleep(0.2)
        boot_dev, root_dev = f"{loop}p1", f"{loop}p2"
        if sorted(parts.values()) != [boot_dev, root_dev]:
            raise Stop(f"expected exactly two partitions in the image, found {', '.join(sorted(parts)) or 'none'}")
        for dev, fstype, label in ((boot_dev, "vfat", "system-boot"), (root_dev, "ext4", "writable")):
            info = probe(dev)
            if info.get("TYPE") != fstype or info.get("LABEL") != label:
                raise Stop(f"{dev}: {info.get('TYPE')} labelled {info.get('LABEL')}; expected {fstype} "
                           f"labelled {label}")
        workdir = run["workdir"] = tempfile.mkdtemp(prefix="new-unit-image.")
        root_dir, boot_dir = os.path.join(workdir, "root"), os.path.join(workdir, "boot")
        ro = ["-o", "ro,noload,nodev,nosuid"] if args.dry_run else ["-o", "nodev,nosuid"]
        for d, mount in ((root_dir, ["mount", "-t", "ext4", *ro, root_dev, root_dir]),
                         (boot_dir, ["mount", "-t", "vfat", "-o", ("ro," if args.dry_run else "")
                                     + "nodev,nosuid,noexec", boot_dev, boot_dir])):
            os.mkdir(d)
            mount_points.append(d)      # before mount: an interrupt during it is cleaned up too
            run_cmd(mount)
        run_tree(root_dir, boot_dir, args, ownership=True)
    finally:
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)        # a second Ctrl-C must not stop the clean-up
        try:
            left = clean_up(image, run["loop"], run["workdir"], mount_points) if attaching else []
        finally:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        if left:
            say(f"CLEAN-UP INCOMPLETE: {'; '.join(left)}. Do not copy or flash the image until "
                f"'findmnt' shows nothing of it and 'losetup -j {image}' prints nothing: umount, then losetup -d.")
            raise CleanupIncomplete("; ".join(left))
        if attaching:
            say(f"image unmounted and detached: {image}")


def run_tree(root_dir, boot_dir, args, ownership, host=None):
    """The core on mounted trees, after the safety checks."""
    for top, what in ((root_dir, "--root"), (boot_dir, "--boot")):
        if not os.path.isdir(top):
            raise Stop(f"{what} {top} is not a directory")
        refuse_host_tree(top, what, host)
    root = Tree(root_dir, "root", ownership=ownership)
    boot = Tree(boot_dir, "boot", posix=False)
    check_looks_like_unit(root, boot)
    Unit(root, boot, args).run()


def parse(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Procedure and the checklist for the new unit: setup/NEW_UNIT.md")
    sub = p.add_subparsers(dest="mode", required=True)
    image = sub.add_parser("image", help="a copy of the whole card image (attached and mounted here)")
    image.add_argument("image", help="the image file (a copy: it is changed in place)")
    tree = sub.add_parser("tree", help="a card whose two partitions are mounted already")
    tree.add_argument("--root", required=True, help="the mounted root partition (label writable)")
    tree.add_argument("--boot", required=True, help="the mounted boot partition (label system-boot)")
    tree.add_argument("--skip-ownership", action="store_true",
                      help="leave file owners as they are (for tests without root)")
    for s in (image, tree):
        s.add_argument("--hostname", required=True, help="the new unit's host name, e.g. englacial-radar-2")
        s.add_argument("--label", default=DEFAULT_LABEL, help=f"data drive label for /etc/fstab ({DEFAULT_LABEL})")
        s.add_argument("--max-tx-gain", type=float, default=DEFAULT_MAX_TX_GAIN,
                       help=f"manager.limits.max_tx_gain in the new local.yaml ({DEFAULT_MAX_TX_GAIN:g}, "
                            "the repository's conservative default)")
        s.add_argument("--disarm-power", action="store_true", help="armed = false in power.toml")
        alerts = s.add_mutually_exclusive_group()
        alerts.add_argument("--ntfy-topic", help="a different ntfy topic for the new unit")
        alerts.add_argument("--no-alerts", action="store_true", help="remove the ntfy settings: alerts unconfigured")
        s.add_argument("--dry-run", action="store_true", help="print every change, make none")
        s.add_argument("--force", action="store_true", help="run on an image that this tool has changed before")
    args = p.parse_args(argv)
    if not HOSTNAME_RE.match(args.hostname) or args.hostname in YAML_WORDS:
        p.error("--hostname: lower-case letters, digits and '-', at most 63, starting with a letter, not "
                "ending with '-', and not a word that YAML reads as true, false or null")
    if not LABEL_RE.match(args.label):
        p.error("--label: at most 16 of A-Z a-z 0-9 _ . -")
    if not 0 <= args.max_tx_gain <= HIGHEST_TX_GAIN:
        p.error(f"--max-tx-gain: between 0 and {HIGHEST_TX_GAIN:g}")
    if args.ntfy_topic is not None and not TOPIC_RE.match(args.ntfy_topic):
        p.error("--ntfy-topic: at most 64 of A-Z a-z 0-9 _ -")
    return args


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    PROGRESS["phase"] = "plan"
    try:
        sys.stdout.reconfigure(line_buffering=True)     # each line reaches | tee as it is printed
    except (AttributeError, ValueError):
        pass
    try:
        if args.mode == "image":
            with_image(args)
        else:
            if os.geteuid() != 0 and not args.dry_run and not args.skip_ownership:
                raise Stop("run as root (or, for a test, with --skip-ownership)")
            run_tree(args.root, args.boot, args, ownership=not args.skip_ownership)
    except CleanupIncomplete as e:
        cause = e.__context__
        if isinstance(cause, Stop):
            say(f"STOPPED: {cause}")
        elif cause is not None:
            say(f"ENDED EARLY ({type(cause).__name__}): {what_was_changed()}")
        say(f"FAILED: the clean-up is incomplete ({e})")
        return 3
    except PartlyChanged as e:
        say(f"\nSTOPPED: {e}")
        return 5
    except Stop as e:
        say(f"\nSTOPPED: {e}")
        return 2
    except KeyboardInterrupt:
        say(f"\nINTERRUPTED: {what_was_changed()}")
        return 130
    except BrokenPipeError:
        try:        # nothing more can go to stdout; keep Python's own exit flush from failing too
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        say(f"\nOUTPUT LOST (broken pipe): {what_was_changed()}")
        return 4
    except Exception as e:      # a fault of this tool: say what it means for the image, then the details
        say(f"\nFAILED ({type(e).__name__}: {e}): {what_was_changed()}")
        say("details:\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
