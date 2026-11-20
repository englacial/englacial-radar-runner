#!/usr/bin/python3 -B
"""Check this system against the setup that the radar manager expects.

    setup/check.py               everything that can be checked by looking
    setup/check.py --receiver    also ask the GPS receiver for four settings in all three layers and its
                                 port statistics (four read-only polls through gpsd, each asked once
                                 more after a lost reply; nothing is ever set)

Prints one line per item:

    PASS  as expected
    FAIL  not as expected (the exit status is then 1)
    SKIP  could not be checked here, with the reason (for example: needs root)
    INFO  observed value of something that has no expected value yet (setup/pins.toml)

Run it on the original Pi, on a clone, on a rebuilt system and after every
field update: a difference between two systems is a line that differs. It
changes nothing, needs no root for almost everything, and uses the standard
library only (/usr/bin/python3).

What each setup item is and how to apply or revert it: setup/SYSTEM_CHANGES.md.
"""
import grp
import json
import os
import pwd
import re
import subprocess
import sys
import tomllib

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(REPO, "setup"), REPO]
import uvlock  # noqa: E402
from fs_label import fs_label  # noqa: E402
from gps_receiver import RAWX, SFRBX, gpsd_args, parse_comms, parse_valget, rawx_every  # noqa: E402
from radar_runner.common import status as status_files  # noqa: E402  (standard library only)

FILES = os.path.join(REPO, "setup", "files")
OWNER = pwd.getpwuid(os.stat(REPO).st_uid)       # the user that owns the checkout
HOME = OWNER.pw_dir
IS_ROOT = os.geteuid() == 0
if set(sys.argv[1:]) - {"--receiver"}:
    sys.exit("usage: setup/check.py [--receiver]")
ASK_RECEIVER = "--receiver" in sys.argv[1:]
PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"

results = []
with open(os.path.join(REPO, "setup", "pins.toml"), "rb") as _f:
    PINS = tomllib.load(_f)


class Skip(Exception):
    pass


def report(status, name, observed=""):
    results.append(status)
    text = " ".join(str(observed).split())
    print(f"{status}  {name}" + (f": {text}" if text else ""), flush=True)


def section(title):
    print(f"\n== {title}", flush=True)


def check(name, ok, observed, expected=None):
    """PASS or FAIL with the observed value; a FAIL also shows what was expected."""
    if ok:
        report(PASS, name, observed)
    else:
        report(FAIL, name, f"{observed}" + (f" (expected: {expected})" if expected else ""))


def pinned(name, observed, key):
    """Compare with setup/pins.toml; no pin means INFO."""
    want = PINS.get(key, "")
    if not observed:
        report(FAIL, name, "could not be determined")
    elif not want:
        report(INFO, name, f"{observed} (no pin: {key} in setup/pins.toml)")
    else:
        check(name, observed == want, observed, f"{want}, from setup/pins.toml")


def run(*cmd, timeout=30):
    """Return (exit status, stdout, stderr). The status is None if the command cannot be run."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, LC_ALL="C"))
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return None, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return None, "", f"{cmd[0]}: no answer within {timeout} s"
    except OSError as e:
        return None, "", f"{cmd[0]}: {e}"


def out(*cmd, **kw):
    return run(*cmd, **kw)[1]


def read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def item(name):
    """Decorator: run a check now; a Skip becomes SKIP, any other exception a FAIL."""
    def wrap(fn):
        try:
            fn(name)
        except Skip as e:
            report(SKIP, name, str(e))
        except Exception as e:      # a broken check must not hide the others
            report(FAIL, name, f"the check itself failed: {type(e).__name__}: {e}")
        return fn
    return wrap


def unit_state(unit):
    return out("systemctl", "is-enabled", unit) or "not-found", out("systemctl", "is-active", unit) or "unknown"


def package_version(package):
    rc, text, _ = run("dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", package)
    return text.split(" ", 1)[1] if rc == 0 and text.startswith("installed ") else ""


def git(path, *args):
    return out("git", "-c", f"safe.directory={path}", "-C", path, *args)


# The one UHD build that every program here uses: it leaves the SDR's front-panel pins as inputs
# at initialisation, so opening the SDR does not enable the amplifier (docs/uhd-proposals).
_uhd = PINS.get("uhd_prefix") or "~/uhd-patched-gpio"
UHD_PREFIX = os.path.join(HOME, _uhd[2:]) if _uhd.startswith("~/") else _uhd     # the owner's home, also under sudo


def loaded_libuhd(binary, extra_env=()):
    """The libuhd file that `binary` loads when started with `extra_env` ("NAME=value" items) and
    no LD_LIBRARY_PATH of the caller's; "" if that cannot be told."""
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
    env.update(str(item).split("=", 1) for item in extra_env if "=" in str(item))
    try:
        text = subprocess.run(["ldd", binary], capture_output=True, text=True, timeout=30, env=env,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"libuhd\.so\S*\s+=>\s+(/\S+)", text)
    return os.path.realpath(m.group(1)) if m else ""


def check_loads_patched_uhd(name, binary, extra_env=()):
    lib = loaded_libuhd(binary, extra_env)
    want = os.path.realpath(os.path.join(UHD_PREFIX, "lib"))
    check(name, os.path.dirname(lib) == want, lib or "no libuhd found by ldd",
          f"a library in {want}: any other UHD drives the amplifier's enable pin low for about half a second "
          "each time the SDR is opened")


def logind(prop):
    rc, text, err = run("busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
                        "org.freedesktop.login1.Manager", prop)
    if rc != 0:
        raise Skip(f"cannot ask logind: {err or text}")
    return text.split(" ", 1)[1]


def merged_settings(config, section):
    """Settings of one section as systemd will read them: the main file and all drop-ins, in order."""
    rc, text, err = run("systemd-analyze", "cat-config", config)
    if rc != 0:
        raise Skip(f"systemd-analyze cat-config {config}: {err or 'failed'}")
    values, current = {}, None
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("["):
            current = line
        elif current == f"[{section}]" and "=" in line and not line.startswith(("#", ";")):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def file_settings(rel):
    """Settings in one of this project's drop-in files (setup/files/<rel>)."""
    text = read(os.path.join(FILES, rel)) or ""
    return dict(line.strip().split("=", 1) for line in text.split("\n")
                if "=" in line and not line.lstrip().startswith(("#", ";", "[")))


# ============================================================ the system

section("System")


@item("OS release")
def _(name):
    m = re.search(r'^VERSION_ID="?([^"\n]+)', read("/etc/os-release") or "", re.M)
    pinned(name, m.group(1) if m else "", "os_version")


@item("kernel")
def _(name):
    pinned(name, os.uname().release, "kernel")


@item("identity: hostname")
def _(name):
    # Identity must differ between two units. The same value on two systems means a card was copied.
    report(INFO, name, os.uname().nodename)
    report(INFO, "identity: machine-id", (read("/etc/machine-id") or "unreadable").strip())
    keys = []
    for pub in sorted(p for p in os.listdir("/etc/ssh") if p.startswith("ssh_host_") and p.endswith(".pub")):
        fields = out("ssh-keygen", "-lf", os.path.join("/etc/ssh", pub)).split()
        if len(fields) >= 2:
            keys.append(f"{fields[-1].strip('()')} {fields[1]}")
    report(INFO, "identity: SSH host keys", ", ".join(keys) or "none readable")


@item("systemd state")
def _(name):
    state = out("systemctl", "is-system-running")
    failed = out("systemctl", "list-units", "--failed", "--no-legend", "--plain").split("\n")
    failed = ", ".join(line.split()[0] for line in failed if line.strip())
    check(name, state == "running", state + (f"; failed units: {failed}" if failed else ""), "running")


section("Boot and hardware prerequisites (not automated by setup.sh)")


@item("bootloader version")
def _(name):
    rc, text, err = run("vcgencmd", "bootloader_version")
    if rc != 0:
        raise Skip(f"vcgencmd: {err or text or 'failed'}" + ("" if IS_ROOT else " (may need root or group video)"))
    pinned(name, text.split("\n")[0].strip(), "bootloader")


@item("bootloader config")
def _(name):
    rc, text, err = run("rpi-eeprom-config")
    if rc != 0:
        raise Skip(f"rpi-eeprom-config: {err or text or 'failed'}" + ("" if IS_ROOT else " (may need root)"))
    pinned(name, " ".join(text.split()), "bootloader_config")


@item("bootloader update pending")
def _(name):
    rc, text, err = run("rpi-eeprom-update")
    if rc is None:
        raise Skip(err)
    first = text.split("\n")[0].strip()
    check(name, "up to date" in first, first or err, "BOOTLOADER: up to date (else it is flashed at the next boot)")


@item("I2C enabled in /boot/firmware/config.txt")
def _(name):
    text = read("/boot/firmware/config.txt")
    if text is None:
        raise Skip("cannot read /boot/firmware/config.txt")
    found = re.findall(r"^\s*dtparam=i2c_arm=(\w+)", text, re.M)
    check(name, bool(found) and found[-1] == "on", f"dtparam=i2c_arm={found[-1]}" if found else "no dtparam=i2c_arm line",
          "dtparam=i2c_arm=on")
    check("I2C bus 1 device", os.path.exists("/dev/i2c-1"), "/dev/i2c-1 " + ("exists" if os.path.exists("/dev/i2c-1") else "missing"))


@item(f"user {OWNER.pw_name} in groups dialout, i2c")
def _(name):
    groups = {g.gr_name for g in grp.getgrall() if OWNER.pw_name in g.gr_mem}
    groups.add(grp.getgrgid(OWNER.pw_gid).gr_name)
    missing = {"dialout", "i2c"} - groups
    check(name, not missing, "missing: " + ", ".join(sorted(missing)) if missing else "yes",
          "serial ports, GPIO and I2C without root")
    report(INFO, f"all groups of {OWNER.pw_name}", ", ".join(sorted(groups)))


@item("realtime priority limit for the radar (limits.conf)")
def _(name):
    lines = [" ".join(line.split()) for line in (read("/etc/security/limits.conf") or "").split("\n")]
    want = f"{OWNER.pw_name} - rtprio 99"
    check(name, want in lines, want if want in lines else "no such line", f"'{want}' in /etc/security/limits.conf")


@item("UHD udev rules")
def _(name):
    path = "/etc/udev/rules.d/uhd-usrp.rules"
    check(name, os.path.isfile(path), path + (" exists" if os.path.isfile(path) else " missing"))


section("Remote access (setup.sh changes nothing here except Tailscale's self-update)")


@item("Tailscale")
def _(name):
    rc, text, err = run("tailscale", "status", "--json")
    if rc is None:
        report(FAIL, name, err)
        return
    if rc != 0:
        raise Skip(f"tailscale status: {err or text}" + ("" if IS_ROOT else " (may need root)"))
    data = json.loads(text)
    me = data.get("Self") or {}
    check(name, data.get("BackendState") == "Running", f"{data.get('BackendState')}, version {data.get('Version')}", "Running")
    report(INFO, "identity: Tailscale node", f"{me.get('DNSName') or me.get('HostName')} ({me.get('ID')})")
    expiry = me.get("KeyExpiry")
    check("Tailscale key expiry disabled", not expiry, f"key expires {expiry}" if expiry else "no expiry")
    enabled, active = unit_state("tailscaled.service")
    check("tailscaled.service", (enabled, active) == ("enabled", "active"), f"{enabled}, {active}", "enabled, active")


@item("SSH enabled")
def _(name):
    states = {u: unit_state(u) for u in ("ssh.socket", "ssh.service")}
    text = ", ".join(f"{u} {e}/{a}" for u, (e, a) in states.items())
    check(name, any(e == "enabled" for e, _ in states.values()), text, "ssh.socket or ssh.service enabled")
    listening = [line.split()[3] for line in out("ss", "-ltnH").split("\n") if line.split()[3:4]]
    port22 = [a for a in listening if a.endswith(":22")]
    check("SSH listening on port 22", bool(port22), ", ".join(port22) or "nothing listens on port 22")


section("Storage")


def nvme_fstab_findings(spec, device, uuid, label, cached_label):
    """Would the /etc/fstab line's source `spec` mount the drive that is at /mnt/nvme now? `label` is the
    filesystem's own label (None if unreadable), `cached_label` udev's, which e2label on a mounted drive
    does not update. Returns (status, name, observed) items."""
    name = "NVMe fstab line names the mounted drive"
    by, _, value = spec.partition("=")
    if by == "LABEL" and label is None:
        found = [(SKIP, name, f"{spec}: the label of the filesystem at /mnt/nvme cannot be read")]
    elif by in ("LABEL", "UUID"):
        has = label if by == "LABEL" else uuid
        fix = "sudo setup/setup.sh --nvme-by-label corrects it" if by == "LABEL" else "correct the line by hand"
        found = [(PASS, name, spec) if value == has else
                 (FAIL, name, f"the line names {spec}, the drive mounted at /mnt/nvme ({device}) has {by}={has}: "
                              f"it would be missing after the next boot ({fix})")]
    else:
        found = [(INFO, name, f"{spec}: neither LABEL= nor UUID=, not compared")]
    if label is not None and label != cached_label:
        # INFO, not FAIL: udev reads the label again at the next boot; until then lsblk, findmnt and
        # /dev/disk/by-label show the old one. The fstab finding above covers what matters for the boot.
        found.append((INFO, "NVMe label in udev's cache",
                      f"udev has '{cached_label}', the filesystem has '{label}' (relabelled while mounted); refresh: "
                      f"sudo udevadm trigger --action=change {device} && sudo udevadm settle"))
    elif label is not None:
        found.append((PASS, "NVMe label in udev's cache", f"'{label}', as the filesystem has it"))
    return found


@item("NVMe mounted at /mnt/nvme")
def _(name):
    rc, text, _ = run("findmnt", "-n", "-o", "SOURCE,FSTYPE,LABEL,UUID", "/mnt/nvme")
    check(name, rc == 0 and bool(text), text or "not mounted")
    if rc == 0 and text:
        report(INFO, "NVMe free space", out("df", "-h", "--output=avail,pcent", "/mnt/nvme").split("\n")[-1].strip())
    lines = [line.split() for line in (read("/etc/fstab") or "").split("\n")
             if line.split()[1:2] == ["/mnt/nvme"] and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        report(FAIL, "NVMe line in /etc/fstab", f"{len(lines)} lines for /mnt/nvme")
        return
    spec, options = lines[0][0], lines[0][3].split(",")
    check("NVMe fstab line has nofail", "nofail" in options, ",".join(options),
          "nofail, so that a missing drive cannot stop the boot")
    by = spec.split("=")[0]
    report(INFO, "NVMe mounted by", f"{spec} ({'any drive with this label works' if by == 'LABEL' else 'only this one drive; optional: setup.sh --nvme-by-label'})")
    if not (rc == 0 and text):
        report(SKIP, "NVMe fstab line names the mounted drive", "nothing is mounted at /mnt/nvme")
        return
    try:
        label = fs_label("/mnt/nvme")
    except OSError:
        label = None
    for finding in nvme_fstab_findings(spec, out("findmnt", "-n", "-o", "SOURCE", "/mnt/nvme"),
                                       out("findmnt", "-n", "-o", "UUID", "/mnt/nvme"), label,
                                       out("findmnt", "-n", "-o", "LABEL", "/mnt/nvme")):
        report(*finding)


section("Radar software (not automated by setup.sh)")


@item("orca mamba environment")
def _(name):
    python = os.path.join(HOME, "miniforge3", "envs", "orca", "bin", "python")
    if not os.path.exists(python):
        report(FAIL, name, f"{python} missing")
        return
    rc, text, err = run(python, "--version")
    check(name, rc == 0, f"{text or err} at {os.path.dirname(os.path.dirname(python))}")


@item("patched UHD build")
def _(name):
    path = os.path.join(UHD_PREFIX, "ORCA_UHD_BUILD.yaml")
    text = read(path)
    if text is None:
        report(FAIL, name, f"{path} missing")
        return
    fields = dict(re.findall(r'^(\w+):\s*"?([^"\n]*)"?\s*$', text, re.M))
    lib = os.path.join(UHD_PREFIX, "lib", "libuhd.so")
    check(name, os.path.exists(lib) and fields.get("dirty") == "false",
          f"{fields.get('uhd_version')}, built {fields.get('built_utc')}, dirty: {fields.get('dirty')}, "
          f"libuhd.so {'present' if os.path.exists(lib) else 'MISSING'}", "library present, built from a clean tree")
    pinned("patched UHD commit", fields.get("commit", ""), "uhd_commit")


@item("ORCA checkout")
def _(name):
    path = os.path.join(HOME, "orca")
    commit = git(path, "rev-parse", "HEAD")
    pinned(name + " commit", commit, "orca_commit")
    dirty = git(path, "status", "--porcelain")
    report(INFO, name + " state", f"branch {git(path, 'branch', '--show-current') or '(detached)'}, "
           + (f"{len(dirty.splitlines())} changed or untracked file(s)" if dirty else "clean"))


@item("gpsd package")
def _(name):
    pinned(name, package_version("gpsd"), "gpsd_version")


@item("time daemon")
def _(name):
    # chrony (with GPS as a time source) replaces systemd-timesyncd from stage 2; details further down.
    daemons = [f"{unit} ({package_version(package)})" for unit, package in
               (("systemd-timesyncd.service", "systemd-timesyncd"), ("chrony.service", "chrony"))
               if unit_state(unit)[1] == "active"]
    synced = out("timedatectl", "show", "-p", "NTPSynchronized", "--value")
    report(INFO, name, f"{', '.join(daemons) or 'none running'}; clock synchronised: {synced or 'unknown'}")


section("This repository and its Python environment")


@item("repository version")
def _(name):
    describe = git(REPO, "describe", "--tags", "--always", "--dirty")
    tag = git(REPO, "describe", "--tags", "--exact-match")
    changed = len(git(REPO, "status", "--porcelain").splitlines())
    report(INFO, name, f"{describe or 'unknown'} at {REPO}" + (f", {changed} changed or untracked file(s)" if changed else ""))
    if PINS.get("repo_tag"):
        check("repository release tag", tag == PINS["repo_tag"] and not changed, tag or "not on a tag",
              f"{PINS['repo_tag']} with no local changes, from setup/pins.toml")


@item("uv")
def _(name):
    uv = os.path.join(HOME, ".local", "bin", "uv")
    rc, text, err = run(uv, "--version")
    if rc != 0:
        report(FAIL, name, f"{uv}: {err or 'failed'}")
        return
    pinned(name, text.split()[1] if len(text.split()) > 1 else text, "uv_version")


VENV_PYTHON = os.path.join(REPO, ".venv", "bin", "python")


@item("virtual environment interpreter")
def _(name):
    cfg = read(os.path.join(REPO, ".venv", "pyvenv.cfg"))
    if cfg is None:
        report(FAIL, name, ".venv missing: run setup/build_venv.sh")
        return
    home = re.search(r"^home\s*=\s*(.+)$", cfg, re.M)
    version = out(VENV_PYTHON, "--version")
    check(name, bool(home) and home.group(1).strip() == "/usr/bin" and version.startswith("Python 3.12."),
          f"{version}, base {home.group(1).strip() if home else 'unknown'}", "Python 3.12 from /usr/bin")


@item("virtual environment matches uv.lock")
def _(name):
    if not os.path.exists(VENV_PYTHON):
        report(FAIL, name, ".venv missing: run setup/build_venv.sh")
        return
    problems = uvlock.venv_problems(os.path.join(REPO, "uv.lock"), VENV_PYTHON)
    count = len(uvlock.locked_packages(os.path.join(REPO, "uv.lock")))
    check(name, not problems, "; ".join(problems) if problems else f"{count} packages, all at the locked versions",
          "run setup/build_venv.sh")


@item("wheelhouse complete")
def _(name):
    problems = uvlock.wheelhouse_problems(os.path.join(REPO, "uv.lock"), os.path.join(REPO, "wheelhouse"))
    count = sum(len(p["wheels"]) for p in uvlock.locked_packages(os.path.join(REPO, "uv.lock")).values())
    check(name, not problems, "; ".join(problems) if problems else f"{count} wheels, SHA-256 as in uv.lock",
          "run setup/fetch_wheels.py (needs internet)")


@item("config files load without warnings")
def _(name):
    rc, text, err = run(VENV_PYTHON, "-B", "-m", "radar_runner.common.config", timeout=60)
    if rc != 0:
        report(FAIL, name, err.split("\n")[-1] if err else "could not run")
        return
    warnings = [line for line in text.split("\n") if line.startswith("WARNING:")]
    check(name, not warnings, " | ".join(warnings) if warnings else "no warnings")
    for label, path in (("per-unit config", "/etc/radar-runner/local.yaml"), ("secrets file", "/etc/radar-runner/secrets.yaml")):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            report(INFO, label, f"{path} not present (optional)")
            continue
        mode = st.st_mode & 0o777
        if label == "secrets file":
            check(label, not mode & 0o007, f"{path} mode {mode:04o}", "not readable by other users (0640 or 0600)")
        else:
            report(INFO, label, f"{path} present")


# ============================================================ setup.sh steps

section("setup.sh: files installed (setup/files/)")

for _root, _dirs, _names in sorted(os.walk(FILES)):
    for _n in sorted(_names):
        _src = os.path.join(_root, _n)
        _dst = "/" + os.path.relpath(_src, FILES)

        @item(_dst)
        def _(name, src=_src, dst=_dst):
            mine, theirs = read(src), read(dst)
            if theirs is None:
                report(FAIL, name, "not installed" if not os.path.exists(dst) else "cannot be read")
                return
            st = os.stat(dst)
            owner = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name} {st.st_mode & 0o7777:04o}"
            check(name, mine == theirs and owner == "root:root 0644",
                  ("identical" if mine == theirs else "differs from setup/files") + f", {owner}",
                  "identical, root:root 0644")

section("setup.sh: directories")

for _line in (read(os.path.join(FILES, "etc/tmpfiles.d/radar-runner.conf")) or "").split("\n"):
    _f = _line.split()
    if len(_f) >= 5 and _f[0] == "d":
        @item(_f[1])
        def _(name, mode=_f[2], user=_f[3], group=_f[4]):
            want = f"{user}:{group} {int(mode, 8):04o}"
            try:
                st = os.stat(name)
            except OSError as e:
                report(FAIL, name, f"{e.strerror} (expected: {want})")
                return
            have = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name} {st.st_mode & 0o7777:04o}"
            check(name, have == want, have, want)

section("setup.sh: shutdown path (logind)")


@item("power key ignored (UPS wake-up contact)")
def _(name):
    value = logind("HandlePowerKey")
    check(name, value == '"ignore"', f"HandlePowerKey={value}", 'HandlePowerKey="ignore"; after installing the file: reboot')


@item("shutdown inhibitor delay")
def _(name):
    usec = int(logind("InhibitDelayMaxUSec"))
    check(name, usec == 5_000_000, f"InhibitDelayMaxSec={usec / 1e6:g}", "5; after installing the file: reboot")


@item("no shutdown delay lock held by unattended-upgrades")
def _(name):
    rc, text, err = run("systemd-inhibit", "--list", "--no-legend", "--no-pager")
    if rc != 0:
        raise Skip(err or "systemd-inhibit failed")
    held = [line for line in text.split("\n") if "nattended" in line]
    check(name, not held, held[0] if held else "none")


section("setup.sh: automatic updates off")


@item("apt periodic jobs")
def _(name):
    rc, text, err = run("apt-config", "dump")
    if rc != 0:
        raise Skip(err or "apt-config failed")
    values = dict(re.findall(r'^APT::Periodic::([\w-]+) "(.*)";$', text, re.M))
    for key in ("Enable", "Update-Package-Lists", "Unattended-Upgrade"):
        value = values.get(key, "unset (default 1)" if key == "Enable" else "unset")
        check(f"APT::Periodic::{key}", value == "0", f'"{value}"', '"0"')


for _unit in ("apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service", "fwupd-refresh.timer",
              "rpi-eeprom-update.service"):
    @item(_unit)
    def _(name):
        enabled, active = unit_state(name)
        if enabled == "not-found":
            report(PASS, name, "not on this system")
        else:
            check(name, enabled == "masked" and active != "active", f"{enabled}, {active}", "masked, inactive")


@item("snap automatic refresh held forever")
def _(name):
    rc, text, err = run("snap", "refresh", "--time")
    if rc is None:
        report(PASS, name, "snapd is not installed")
        return
    if rc != 0:
        raise Skip(err or text)
    hold = re.search(r"^hold:\s*(.+)$", text, re.M)
    check(name, bool(hold) and hold.group(1).strip() == "forever",
          f"hold: {hold.group(1)}" if hold else "not held; " + ", ".join(text.split("\n")),
          "'hold: forever' in 'snap refresh --time'")


@item("Tailscale automatic self-update off")
def _(name):
    rc, text, err = run("tailscale", "debug", "prefs")
    if rc is None:
        report(PASS, name, "Tailscale is not installed")
        return
    if rc != 0:
        raise Skip(f"tailscale debug prefs: {err or text}" + ("" if IS_ROOT else " (may need root)"))
    auto = json.loads(text).get("AutoUpdate") or {}
    check(name, auto.get("Apply") is not True, f"AutoUpdate Check: {auto.get('Check')}, Apply: {auto.get('Apply')}",
          "Apply not true; fix: sudo tailscale set --auto-update=false")


@item("needrestart only lists services")
def _(name):
    conf = "/etc/needrestart/needrestart.conf"
    if not os.path.exists(conf):
        report(PASS, name, "needrestart is not installed")
        return
    # needrestart's config is Perl; evaluate it the way needrestart does, drop-ins included.
    rc, text, err = run("perl", "-e", 'our (%nrconf, $LOGPREF); %nrconf = (verbosity => 0); do $ARGV[0]; '
                        'die $@ if $@; print defined $nrconf{restart} ? $nrconf{restart} : "unset";', conf)
    if rc != 0:
        raise Skip(f"cannot evaluate {conf}: {err}")
    check(name, text == "l", f"restart mode '{text}'" + (" (Ubuntu default: restart services automatically after apt)"
                                                          if text == "unset" else ""), "'l' (list only)")


section("setup.sh: journal")


@item("journal is persistent")
def _(name):
    check(name, os.path.isdir("/var/log/journal"), "/var/log/journal " + ("exists" if os.path.isdir("/var/log/journal") else "missing"))
    usage = re.search(r"take up (\S+)", out("journalctl", "--disk-usage"))
    report(INFO, "journal size now", usage.group(1) if usage else "unknown")


@item("journal settings on disk (all files merged)")
def _(name):
    want = file_settings("etc/systemd/journald.conf.d/radar-runner.conf")
    have = merged_settings("systemd/journald.conf", "Journal")
    for key, value in want.items():
        check(f"journald {key}", have.get(key) == value, f"{key}={have.get(key, '(default)')}", f"{key}={value}")


@item("journal size limit in effect")
def _(name):
    size = re.fullmatch(r"(\d+)([KMGT])", file_settings("etc/systemd/journald.conf.d/radar-runner.conf")["SystemMaxUse"])
    want = f"{float(size.group(1)):.1f}{size.group(2)}"
    text = out("journalctl", "-b", "-t", "systemd-journald", "-o", "cat", "--no-pager")
    found = re.findall(r"System Journal \(.*\) is \S+, max (\S+),", text)
    if not found:
        raise Skip("journald's start-up message is not readable" + ("" if IS_ROOT else " (needs root or group adm)"))
    check(name, found[-1] == want, f"max {found[-1]}", f"max {want}; after installing the file: reboot")


section("setup.sh: hardware watchdog")


WATCHDOG = file_settings("etc/systemd/system.conf.d/radar-runner-watchdog.conf")


@item("watchdog settings on disk (all files merged)")
def _(name):
    have = merged_settings("systemd/system.conf", "Manager")
    for key, value in WATCHDOG.items():
        check(f"systemd {key} on disk", have.get(key) == value, f"{key}={have.get(key, '(default)')}", f"{key}={value}")


@item("watchdog settings in use")
def _(name):
    runtime = out("systemctl", "show", "-p", "RuntimeWatchdogUSec", "--value")
    reboot = out("systemctl", "show", "-p", "RebootWatchdogUSec", "--value")
    check(name, runtime == WATCHDOG["RuntimeWatchdogSec"] and reboot == WATCHDOG["RebootWatchdogSec"],
          f"runtime {runtime or 'unknown'}, reboot {reboot or 'unknown'}",
          f"runtime {WATCHDOG['RuntimeWatchdogSec']}, reboot {WATCHDOG['RebootWatchdogSec']}")


@item("watchdog device")
def _(name):
    base = "/sys/class/watchdog/watchdog0/"
    if not os.path.isdir(base):
        report(FAIL, name, "no /sys/class/watchdog/watchdog0")
        return
    want = WATCHDOG["RuntimeWatchdogSec"].rstrip("s")
    state, timeout, left = ((read(base + f) or "").strip() for f in ("state", "timeout", "timeleft"))
    check(name, state == "active" and timeout == want,
          f"{(read(base + 'identity') or '').strip()}: {state}, timeout {timeout} s", f"active, timeout {want} s")
    if state == "active":
        # Near zero would mean the kernel is not keeping the short hardware timer fed.
        check("watchdog time left", left.isdigit() and int(left) >= 3, f"{left} s", "3 s or more at any moment")


section("setup.sh: power service")


@item("radar-power.service unit file")
def _(name):
    mine = read(os.path.join(REPO, "systemd", "radar-power.service"))
    theirs = read("/etc/systemd/system/radar-power.service")
    check(name, theirs is not None and mine == theirs,
          "not installed" if theirs is None else "identical to systemd/radar-power.service" if mine == theirs
          else "differs from systemd/radar-power.service")
    enabled, active = unit_state("radar-power.service")
    check("radar-power.service", (enabled, active) == ("enabled", "active"), f"{enabled}, {active}", "enabled, active")


@item("power service config")
def _(name):
    text = read("/etc/radar-runner/power.toml")
    if text is None:
        report(FAIL, name, "/etc/radar-runner/power.toml missing or unreadable")
        return
    armed = tomllib.loads(text).get("armed", False)
    report(INFO, name, f"/etc/radar-runner/power.toml: armed = {str(armed).lower()}")


@item("power service status file")
def _(name):
    r = status_files.read_status(status_files.POWER_STATUS_FILE, 5.0)
    if not r.ok:
        report(FAIL, name, f"{r.state}: {r.error}")
        return
    d = r.data
    check(name, True, f"fresh ({r.age_s:.1f} s old): state {d.get('state')}, mode {d.get('mode')}, "
          f"power_present {d.get('power_present')}, capacitor {d.get('capacitor_v')} V")
    warnings = d.get("warnings") or {}
    check("power service warnings", not warnings, json.dumps(warnings) if warnings else "none")


section("setup.sh: gpsd")

GPS_DEVICE = "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00"


@item("gpsd.service enabled and running")
def _(name):
    enabled, active = unit_state("gpsd.service")
    check(name, (enabled, active) == ("enabled", "active"), f"{enabled}, {active}", "enabled, active")


@item("gpsd keeps reading without clients, passive, with the receiver named")
def _(name):
    # -p: gpsd leaves the receiver's settings alone (setup/SYSTEM_CHANGES.md, section 11). From /proc:
    # ps cuts the line to $COLUMNS when that is set.
    args = " ".join(gpsd_args())
    check(name, " -n " in args + " " and " -p " in args + " " and GPS_DEVICE in args, args or "gpsd is not running",
          f"gpsd -n -p {GPS_DEVICE}")


@item("gpsd started with the receiver, hotplug path off")
def _(name):
    device = next((l.split("=", 1)[1] for l in (read(os.path.join(FILES, "etc/systemd/system/gpsd.service.d/radar-runner.conf")) or "").splitlines()
                   if l.startswith("BindsTo=")), "")
    link = f"/etc/systemd/system/{device}.wants/gpsd.service"
    check("the receiver's device unit wants gpsd.service", os.path.islink(link), "link present" if os.path.islink(link) else "no link",
          "link present (setup.sh creates it)")
    exec_start = out("systemctl", "show", "gpsdctl@ttyACM0.service", "-p", "ExecStart", "--value")
    check("Ubuntu's gpsdctl hotplug path switched off", "/bin/true" in exec_start and "gpsdctl add" not in exec_start,
          "no-op" if "/bin/true" in exec_start else "still runs gpsdctl add", "no-op (the receiver was opened twice at boot otherwise)")
    enabled, _active = unit_state("radar-gps-receiver.service")
    check("radar-gps-receiver.service (receiver check after every start of gpsd)", enabled == "enabled", enabled, "enabled")


@item("gpsd hotplug (USBAUTO in /etc/default/gpsd)")
def _(name):
    found = re.findall(r'^USBAUTO="?(\w+)"?', read("/etc/default/gpsd") or "", re.M)
    check(name, bool(found) and found[-1] == "true", f"USBAUTO={found[-1]}" if found else "not set",
          'USBAUTO="true": a replugged receiver is added by udev and gpsdctl')


def gps_identification(devices):
    """(status, observed) for gpsd's identification of the receiver, from its DEVICES report.

    `subtype` comes from the receiver's version reply. On 2026-11-19, after a power cut, it was
    empty, the receiver sent gpsd no position message, and gpsd delivered no position for 77
    minutes (setup/gps_receiver.py has what gpsd does and what is inferred).
    """
    if devices is None:
        return SKIP, "gpsd did not send its list of devices"
    if not devices:
        return SKIP, "gpsd has no receiver"
    missing = [d.get("path") for d in devices
               if not (d.get("subtype1") or d.get("subtype") not in (None, "", "Unknown"))]
    if missing:
        return FAIL, (f"gpsd has not identified {', '.join(map(str, missing))}: no version reply seen "
                      "(setup/gps_receiver.py --apply --ram asks the receiver for its version)")
    return PASS, "; ".join(str(d.get("subtype1") or d.get("subtype")) for d in devices)


def gps_fix_finding(tpv):
    """(status, observed) for gpsd's latest fix. gpsd clears the fix at every cycle, so a fix time
    with mode 0 (or none) means that no position message (NAV-PVT) came from the receiver."""
    if tpv.get("time") and not tpv.get("mode"):
        return FAIL, ("fix time but mode 0: the receiver sends gpsd no position message "
                      "(setup/gps_receiver.py --apply --ram switches NAV-PVT on)")
    return INFO, {2: "2D fix", 3: "3D fix"}.get(tpv.get("mode"), "no position fix (chrony gets GPS time only with a fix)")


@item("GPS receiver")
def _(name):
    present = os.path.exists(GPS_DEVICE)
    check(name + " on USB", present, GPS_DEVICE + (" exists" if present else " missing"))
    import socket
    devices, reply = None, {}
    try:
        with socket.create_connection(("127.0.0.1", 2947), timeout=3) as sock:
            f = sock.makefile("rwb")
            f.readline()
            f.write(b'?WATCH={"enable":true};\n?DEVICES;\n?POLL;\n')     # questions to gpsd, not to the receiver
            f.flush()
            for _ in range(12):
                line = f.readline()
                if line.startswith(b'{"class":"DEVICES"'):
                    devices = json.loads(line).get("devices") or []
                elif line.startswith(b'{"class":"POLL"'):
                    reply = json.loads(line)
                    break
    except OSError as e:
        report(FAIL, name + " known to gpsd", f"cannot ask gpsd: {e}")
        report(SKIP, name + " identified by gpsd", "cannot ask gpsd")
        return
    paths = [d.get("path") for d in devices or []]
    tpv = (reply.get("tpv") or [{}])[0]
    # Exactly one: two entries would mean that the receiver was added twice (by its stable name
    # on gpsd's command line and again by the hotplug path) and is being read through both.
    check(name + " known to gpsd, once", len(paths) == 1 and reply.get("active") == 1,
          f"{len(paths)} device(s) {paths}, {reply.get('active', 0)} active; fix mode {tpv.get('mode')}, time {tpv.get('time')}",
          "exactly 1 device, active")
    status, observed = gps_identification(devices)
    report(status, name + " identified by gpsd", observed)
    status, observed = gps_fix_finding(tpv)
    report(status, name + " fix", observed)


@item("GPS receiver settings (asked of the receiver)")
def _(name):
    if not ASK_RECEIVER:
        raise Skip("not asked: setup/check.py --receiver reads them (four read-only polls through gpsd, nothing is set)")
    every, why = rawx_every()
    # Read only: -g reads configuration items from one layer (0 RAM, 1 battery-backed, 2 flash), -p MON-COMMS
    # polls the port statistics. No -z, -x, -e, -d here, ever: this script changes nothing, least of all the receiver.
    # The stored layers are what the receiver starts from at the next power-up.
    ubxtool = ("ubxtool", "-P", "27.50", "-w", "1.5")
    keys = ("CFG-MSGOUT-UBX_NAV_PVT_USB", "CFG-UART1OUTPROT-NMEA", RAWX, SFRBX)
    values = {key: [] for key in keys}
    for layer in range(3):
        for _ in range(2):          # asked once more if the reply was lost, as gps_receiver.py does
            rc, text, err = run(*ubxtool, *[a for key in keys for a in ("-g", f"{key},{layer}")], timeout=30)
            if rc is None:
                report(FAIL, name, err)
                return
            got = parse_valget(text, layer)
            if got is not None:
                break
        for key in keys:
            values[key].append("no answer" if got is None else got.get(key, "not stored"))

    def shown(key):
        return f"{key}: " + ", ".join(f"{where} {v}" for v, where in zip(values[key], ("RAM", "battery-backed", "flash")))
    check("GPS receiver: NMEA output on UART1 off", values[keys[1]] == [0, 0, 0], shown(keys[1]),
          "0 in all three (with 1 and the OLA's 10 Hz rate the receiver's transmit buffer overflows; "
          "setup/SYSTEM_CHANGES.md, section 11)")
    check("GPS receiver: position message (NAV-PVT) on USB", values[keys[0]] == [1, 1, 1], shown(keys[0]),
          "1 in all three (setup/gps_receiver.py --apply)")
    if why:
        report(FAIL, "GPS receiver: raw messages for PPK (RXM-RAWX) on USB", why)
    else:
        check("GPS receiver: raw messages for PPK (RXM-RAWX) on USB", values[keys[2]] == [every] * 3, shown(keys[2]),
              f"{every} in all three (manager.gps_log.rawx_every; setup/gps_receiver.py --apply)")
    sfrbx = 0 if every == 0 else 1
    check("GPS receiver: navigation data for PPK (RXM-SFRBX) on USB", values[keys[3]] == [sfrbx] * 3, shown(keys[3]),
          f"{sfrbx} in all three (setup/gps_receiver.py --apply)")
    for _ in range(2):              # asked once more if the reply was lost
        rc, text, err = run(*ubxtool, "-p", "MON-COMMS", timeout=30)
        errors, ports = parse_comms(text)
        if errors is not None:
            break
    usb = ports.get("USB")
    check("GPS receiver: no transmit buffer errors", errors is not None and not errors & 0xC0,
          (f"txErrors 0x{errors:02x}" + (f"; USB buffer {usb[1]} % (last period), peak {usb[2]} % (since the "
                                          "receiver started)" if usb else ""))
          if errors is not None else "no answer from the receiver", "memory (0x40) and allocation (0x80) flags clear")


section("setup.sh: telemetry service")


@item("radar-telemetry.service unit file")
def _(name):
    mine = read(os.path.join(REPO, "systemd", "radar-telemetry.service"))
    theirs = read("/etc/systemd/system/radar-telemetry.service")
    check(name, theirs is not None and mine == theirs,
          "not installed" if theirs is None else "identical to systemd/radar-telemetry.service" if mine == theirs
          else "differs from systemd/radar-telemetry.service")
    enabled, active = unit_state("radar-telemetry.service")
    check("radar-telemetry.service", (enabled, active) == ("enabled", "active"), f"{enabled}, {active}",
          "enabled, active (stopped by hand for bench work? see radar_runner/telemetry/README.md)")


@item("sensor log directory on the data drive")
def _(name):
    path = "/mnt/nvme/radar-runner"
    try:
        st = os.stat(path)
    except OSError as e:
        report(FAIL, name, f"{path}: {e.strerror} (the logs then go to the SD card)")
        return
    owner = pwd.getpwuid(st.st_uid).pw_name
    check(name, owner == OWNER.pw_name and os.path.ismount("/mnt/nvme"),
          f"{path} belongs to {owner}; /mnt/nvme {'is' if os.path.ismount('/mnt/nvme') else 'is NOT'} mounted",
          f"owner {OWNER.pw_name}, on the mounted drive")


@item("telemetry status file")
def _(name):
    r = status_files.read_status(status_files.status_path("telemetry"), 5.0)
    if not r.ok:
        report(FAIL, name, f"{r.state}: {r.error}")
        return
    d = r.data
    check(name, True, f"fresh ({r.age_s:.1f} s old): state {d.get('state')}, boot {d.get('boot_count')}")
    amp = d.get("amp") or {}
    check("amplifier readings fresh", amp.get("fresh") is True,
          f"{amp.get('temp_c')} C, {amp.get('current_a')} A, {amp.get('voltage_v')} V, {amp.get('age_s')} s old"
          + (f"; {amp.get('reason')}" if amp.get("reason") else ""), "fresh: true")
    for source, entry in sorted((d.get("sources") or {}).items()):
        check(f"telemetry source {source}", entry.get("health") == "ok",
              entry.get("health", "?") + (f": {entry.get('reason')}" if entry.get("reason") else ""), "ok")
    logs = d.get("logging") or {}
    check("sensor logs written to the data drive", logs.get("location") == "primary" and logs.get("writer") == "running",
          f"{logs.get('location')}, writer {logs.get('writer')}: {logs.get('directory')}"
          + (f" ({logs.get('reason')})" if logs.get("reason") else "") + f", {logs.get('rows_dropped')} rows dropped",
          "primary, writer running")
    loop = d.get("loop") or {}
    check("telemetry loop runs evenly", loop.get("recent_longest_gap_s", 99) <= 1.0,
          f"longest gap between rounds: {loop.get('recent_longest_gap_s')} s lately, {loop.get('longest_gap_s')} s since it started",
          "1 s or less in the last minutes")
    retry = (d.get("sources") or {}).get("ola", {}).get("retry") or {}
    check("OLA not in a failure back-off", not retry.get("failures_in_a_row"),
          f"{retry.get('failures_in_a_row')} failures in a row; waiting for: {retry.get('waiting_for')}", "0 failures")
    check("boot counter", not d.get("boot_count_problem") and d.get("boot_count", 0) > 0,
          d.get("boot_count_problem") or f"boot {d.get('boot_count')}")


section("setup.sh: safety watcher")


@item("radar-watcher.service unit file")
def _(name):
    mine = read(os.path.join(REPO, "systemd", "radar-watcher.service"))
    theirs = read("/etc/systemd/system/radar-watcher.service")
    check(name, theirs is not None and mine == theirs,
          "not installed" if theirs is None else "identical to systemd/radar-watcher.service" if mine == theirs
          else "differs from systemd/radar-watcher.service")
    enabled, active = unit_state("radar-watcher.service")
    check("radar-watcher.service", (enabled, active) == ("enabled", "active"), f"{enabled}, {active}",
          "enabled, active (setup.sh does not start it: sudo systemctl start radar-watcher; "
          "stopped by hand for bench work? see radar_runner/watcher/README.md)")


@item("SDR GPIO reset tool")
def _(name):
    tool_dir = os.path.join(REPO, "tools", "sdr_gpio_safe")
    missing = [rel for rel in ("amp_safe", os.path.join("build", "sdr_gpio_safe"))
               if not os.access(os.path.join(tool_dir, rel), os.X_OK)]
    check(name, not missing, f"missing or not executable in tools/sdr_gpio_safe: {', '.join(missing)}" if missing
          else "tools/sdr_gpio_safe/amp_safe and build/sdr_gpio_safe are there",
          "built (tools/sdr_gpio_safe/README.md): without it an amplifier left enabled cannot be shut down")


@item("safety watcher status file")
def _(name):
    r = status_files.read_status(status_files.status_path("watcher"), 5.0)
    if not r.ok:
        report(FAIL, name, f"{r.state}: {r.error}")
        return
    d = r.data
    amp = d.get("amp") or {}
    check(name, True, f"fresh ({r.age_s:.1f} s old): amplifier {amp.get('temp_c')} C, case {amp.get('case_temp_c')} C, "
          f"{amp.get('current_a')} A, {amp.get('voltage_v')} V")
    check("safety watcher says the radar may run", d.get("ok") is True and d.get("tripped") is False,
          "ok" if d.get("ok") is True else f"NOT OK{' (TRIPPED)' if d.get('tripped') else ''}: {d.get('reason')}",
          "ok: true, tripped: false")
    warnings = [w.get("text", "?") for w in d.get("warnings") or []]
    check("safety watcher warnings", not warnings, "; ".join(warnings) or "none")
    for problem in d.get("config_warnings") or []:
        if "watcher" in problem:        # other sections' problems are not this service's
            report(FAIL, "safety watcher settings", problem)
    tool = d.get("reset_tool") or {}
    check("reset tool as the watcher will run it", tool.get("runnable") is True, f"{tool.get('command')}: "
          + ("can be run" if tool.get("runnable") is True else "missing or not executable"))
    last = tool.get("last")
    report(INFO, "last reset tool run by the watcher", "none since the watcher started" if not last else
           f"{last.get('time_utc')}: {last.get('name')} (exit {last.get('code')})")
    events = d.get("events") or {}
    check("safety watcher event log", not events.get("error") and not events.get("dropped"),
          f"{events.get('file')}: " + (events.get("error") or f"{events.get('written')} written, {events.get('dropped')} dropped"))
    loop = d.get("loop") or {}
    check("safety watcher loop runs evenly", loop.get("recent_longest_gap_s", 99) <= 2.0,
          f"longest gap between rounds: {loop.get('recent_longest_gap_s')} s lately, {loop.get('longest_gap_s')} s "
          "since it started", "2 s or less in the last minutes")
    pin = (d.get("conditions") or {}).get("pin") or {}
    report(INFO, "amplifier pin record", f"record says safe: {pin.get('record_safe')}; "
           f"the watcher signalled a radar since: {pin.get('signalled_by_watcher')}")
    override = (d.get("override") or {}).get("no_amp_telemetry")
    report(INFO, "override: run without amplifier telemetry", "SET" if override else "not set")
    counters = d.get("counters") or {}
    report(INFO, "safety watcher since it started", ", ".join(f"{k} {counters.get(k)}" for k in (
        "trips", "telemetry_losses", "tool_runs", "radar_sigint", "radar_sigkill", "stalls")))


section("setup.sh: radar manager")

MANAGER_STATE_DIR = "/var/lib/radar-runner/user"
POWER_OFF_BUDGET_S = 48.0       # the last resort of the power-off sequence must come within this of the loss
AMP_SAFE = os.path.join(REPO, "tools", "sdr_gpio_safe", "amp_safe")


def manager_settings():
    """The manager's merged settings and what it finds wrong with the radar config, read with the
    repository's own Python environment (PyYAML). Returns a dict, or raises Skip."""
    code = ("import json, os; from radar_runner.common import config as C; from radar_runner.manager import limits\n"
            "c, w = C.load(secrets_file=None); m = c['manager']\n"
            "out = {'manager': m, 'warnings': w, 'paths': c['paths']}\n"
            "p = m['radar_config']\n"
            "out['radar_config_exists'] = os.path.exists(p)\n"
            "try:\n"
            "    out['problems'] = limits.check(limits.load_file(p), m['limits']) if os.path.exists(p) else None\n"
            "except limits.ConfigError as e:\n"
            "    out['problems'] = [str(e)]\n"
            "print(json.dumps(out))")
    rc, text, err = run(os.path.join(REPO, ".venv", "bin", "python"), "-B", "-c", code)
    if rc != 0:
        raise Skip(f"the manager's settings could not be read: {(err or text)[-200:]}")
    return json.loads(text)


@item("radar-manager.service unit file")
def _(name):
    mine = read(os.path.join(REPO, "systemd", "radar-manager.service"))
    theirs = read("/etc/systemd/system/radar-manager.service")
    check(name, theirs is not None and mine == theirs,
          "not installed" if theirs is None else "identical to systemd/radar-manager.service" if mine == theirs
          else "differs from systemd/radar-manager.service")
    enabled, active = unit_state("radar-manager.service")
    # Not enabled is the expected state until the supervised first start has been done by hand.
    report(PASS if (enabled, active) == ("enabled", "active") else INFO, "radar-manager.service",
           f"{enabled}, {active}" + ("" if active == "active" else
                                     " (setup.sh never enables it: see \"First start with the real radar\" "
                                     "in radar_runner/manager/README.md)"))


@item("radarctl command")
def _(name):
    link, target = "/usr/local/bin/radarctl", os.path.join(REPO, "bin", "radarctl")
    real = os.path.realpath(link) if os.path.lexists(link) else None
    check(name, real == os.path.realpath(target), f"{link} -> {real}" if real else f"{link} missing", target)
    rc, text, err = run(target, "status")
    if rc == 0:
        check("radar manager answers", True, text.split("\n")[0])
    elif rc == 3:
        report(INFO, "radar manager answers", "the manager is not running")
    else:
        report(FAIL, "radar manager answers", f"radarctl status: exit {rc}: {(err or text)[:200]}")


@item("radar is killed if the manager dies (setpriv --pdeathsig)")
def _(name):
    rc, _text, err = run("/usr/bin/setpriv", "--pdeathsig", "KILL", "/bin/true")
    check(name, rc == 0, "setpriv --pdeathsig works" if rc == 0 else f"exit {rc}: {err}")


@item(f"front panel pins usable by {OWNER.pw_name}")
def _(name):
    chips = [f"/dev/{n}" for n in sorted(os.listdir("/dev")) if n.startswith("gpiochip")]
    groups = {g.gr_gid for g in grp.getgrall() if OWNER.pw_name in g.gr_mem} | {OWNER.pw_gid}
    usable = [c for c in chips if (os.stat(c).st_mode & 0o060) == 0o060 and os.stat(c).st_gid in groups]
    check(name, bool(usable), ", ".join(usable) or "no GPIO chip is readable and writable by the user's groups",
          "the chip of the 40-pin header (pinctrl-rp1) usable through a group")


@item("real-time priority for the radar")
def _(name):
    unit = read("/etc/systemd/system/radar-manager.service") or ""
    limits = read("/etc/security/limits.conf") or ""
    shell = re.search(rf"^{re.escape(OWNER.pw_name)}\s+\S+\s+rtprio\s+(\d+)", limits, re.M)
    check(name, "LimitRTPRIO=99" in unit,
          ("unit: LimitRTPRIO=99" if "LimitRTPRIO=99" in unit else "unit: no LimitRTPRIO=99 (or not installed)")
          + f"; login shells: rtprio {shell.group(1) if shell else 'not set'} (as the bench runs were made)")


@item("make-amplifier-safe tool")
def _(name):
    binary = os.path.join(os.path.dirname(AMP_SAFE), "build", "sdr_gpio_safe")
    hook = os.path.join(REPO, "bin", "radar-manager-stop-hook")
    ok = all(os.access(p, os.X_OK) for p in (AMP_SAFE, binary, hook))
    check(name, ok, ", ".join(f"{os.path.relpath(p, REPO)} {'ok' if os.access(p, os.X_OK) else 'MISSING'}"
                              for p in (AMP_SAFE, binary, hook)), "all three executable")
    m = manager_settings()["manager"]["amp_safe"]
    command = [str(a) for a in m["command"]]
    try:
        own = float(command[command.index("--timeout") + 1])
    except (ValueError, IndexError):
        own = None
    check("manager's limit for the tool is longer than the tool's own", own is not None and m["timeout_s"] > own,
          f"manager.amp_safe.timeout_s {m['timeout_s']:g} s, tool --timeout {own if own is None else format(own, 'g')} s")
    record = read(os.path.join(MANAGER_STATE_DIR, "sdr-state"))
    report(INFO, "SDR amplifier pin record", " ".join(record.split("\n")[:2]) if record else
           "no record yet (treated as unknown)")


@item("reset tool loads the patched UHD")
def _(name):
    binary = os.path.join(os.path.dirname(AMP_SAFE), "build", "sdr_gpio_safe")
    if not os.access(binary, os.X_OK):
        raise Skip("the tool is not built")
    check_loads_patched_uhd(name, binary)


@item("radar binary loads the patched UHD")
def _(name):
    orca = manager_settings()["manager"]["orca"]
    radar = os.path.join(orca["dir"], orca["radar_cwd"], orca["radar_command"][0])
    if os.path.exists(radar):       # as last built; the manager rebuilds it at every start
        check_loads_patched_uhd(name, radar, orca["env"])
    else:
        report(INFO, name, f"{radar} is not built yet (the manager builds it at the first start; "
               "run this check again after that)")


@item("radar config and its limits")
def _(name):
    s = manager_settings()
    m = s["manager"]
    for w in s["warnings"]:
        report(FAIL, "config warning", w)
    if not s["radar_config_exists"]:
        report(INFO, name, f"{m['radar_config']} does not exist yet: the manager stays \"not ready\" until it does")
    else:
        check(name, not s["problems"], f"{m['radar_config']}: " + ("passes the limits" if not s["problems"]
                                                                    else "; ".join(s["problems"])))
    conservative = m["limits"]["max_tx_gain"] == 43.0
    report(INFO, "per-unit limits", f"max_tx_gain {m['limits']['max_tx_gain']:g}, max_enable_duty "
           f"{m['limits']['max_enable_duty']:g}" + (" (the conservative defaults: set the unit's values in "
                                                    "/etc/radar-runner/local.yaml)" if conservative else ""))
    root, mount = m["gates"]["data"]["root"], m["gates"]["data"]["mount_point"]
    parent = os.path.dirname(root)
    try:
        owner = pwd.getpwuid(os.stat(root if os.path.isdir(root) else parent).st_uid).pw_name
    except OSError:
        owner = None
    check("collection directory on the data drive", os.path.ismount(mount) and owner == OWNER.pw_name,
          f"{root if os.path.isdir(root) else parent} belongs to {owner}; {mount} "
          f"{'is' if os.path.ismount(mount) else 'is NOT'} mounted", f"owner {OWNER.pw_name}, on the mounted drive")


@item("turn-on check")
def _(name):
    t = manager_settings()["manager"]["turn_on_check"]
    e = t["expected"]
    report(INFO, name, ("enabled" if t["enabled"] else "off") + f"; blocking {str(t['blocking']).lower()}; "
           + (f"expected peak verified (leakage {e['leakage_sample']:g}, delay {e['delay_samples']:g} samples, "
              f"at least {e['min_level_db']:g} dB at tx_gain {e['level_tx_gain']:g})" if e["verified"] else
              "expected peak not set: the check only records (radar_runner/manager/README.md, \"Setting the "
              "expected values\")") + f"; low-power tx_gain {t['max_tx_gain']:g}, {t['pulses']} pulses")
    if not t["enabled"]:
        return
    command = [str(a) for a in t["analysis_command"]]
    script = next((a for a in command[1:] if a.endswith(".py")), "")
    rc, _text, err = run(command[0], "-I", "-c", "import numpy") if command else (1, "", "no command")
    check("turn-on check analysis can run", rc == 0 and os.path.isfile(script),
          f"{command[0] if command else '(none)'}: numpy {'imports' if rc == 0 else 'MISSING: ' + err[-100:]}; "
          f"{script or '(no script)'} {'found' if os.path.isfile(script) else 'MISSING'}")


def power_hook_findings(power, state_file):
    """What is right and wrong with the radar's hooks in the power service's config.

    `power` is power.toml as a dict, `state_file` the record that the manager keeps.
    Returns a list of (ok, name, observed, expected); ok None = information only.
    No I/O: tests/common/test_setup_tools.py runs it on made-up configs.
    """
    out = []
    hooks = power.get("hooks", {})
    hook_time = 0.0
    for hook in ("stop_radar", "amp_safe"):
        argv = [str(a) for a in hooks.get(hook) or []]
        if not argv:
            out.append((None, f"power service hook {hook}",
                        "not set (setup.sh prints the lines to add after the manager's first start)", ""))
            continue
        limit = hooks.get(f"{hook}_timeout_s", 5.0)
        hook_time += limit + 1.0            # the power service gives a hook 1 s more to die
        try:
            own = float(argv[argv.index("--timeout") + 1])
        except (ValueError, IndexError):
            own = None
        out.append((own is not None and limit > own and argv[0] == "/usr/bin/setpriv", f"power service hook {hook}",
                    f"limit {limit:g} s, command's own --timeout {own if own is None else format(own, 'g')} s, "
                    f"run through {os.path.basename(argv[0])}",
                    "through setpriv (never a user's file as root), and the limit longer than --timeout"))
        if hook == "amp_safe":
            # The record that the hook reads must be the one the manager keeps.
            through_script = any(a.endswith("/bin/radar-manager-stop-hook") for a in argv)
            if "--state-file" in argv:                                  # the tool called directly
                where = argv.index("--state-file") + 1
            elif "--timeout" in argv:                                   # bin/radar-manager-stop-hook
                where = argv.index("--timeout") + 2
            else:
                where = len(argv)
            given = argv[where] if where < len(argv) else "/var/lib/radar-runner/user/sdr-state"
            out.append((given == state_file, "power service hook amp_safe reads the manager's record",
                        given, state_file))
            out.append((through_script, "power service hook amp_safe runs the tool only when needed",
                        "through bin/radar-manager-stop-hook" if through_script else "calls the tool every time",
                        "bin/radar-manager-stop-hook (every run of the tool enables the amplifier for half a second)"))
    # Worst case of the power-off sequence, every step at its limit (power/README.md): the signals
    # agree 1.8 s after the loss, ride-through, the hooks, sync, guard record 3 s, 0.4 s.
    shutdown = power.get("shutdown", {})
    command_at = (1.8 + shutdown.get("ride_through_s", 8.0) + hook_time + shutdown.get("sync_timeout_s", 5.0)
                  + 3.0 + 0.4)
    last_resort = command_at + shutdown.get("last_resort_after_s", 10.0)
    out.append((last_resort <= POWER_OFF_BUDGET_S, "power-off sequence fits the capacitor",
                f"worst case: hooks {hook_time:g} s, power-off command {command_at:.1f} s after the loss, "
                f"last resort at {last_resort:.1f} s",
                f"last resort within {POWER_OFF_BUDGET_S:g} s (the capacitor lasted about 58 s at room temperature)"))
    return out


@item("power service hooks for the radar")
def _(name):
    text = read("/etc/radar-runner/power.toml")
    if text is None:
        raise Skip("/etc/radar-runner/power.toml missing or unreadable")
    try:
        state_dir = manager_settings()["paths"]["state_dir"]
    except Skip:
        state_dir = MANAGER_STATE_DIR
    for ok, what, observed, expected in power_hook_findings(tomllib.loads(text), os.path.join(state_dir, "sdr-state")):
        if ok is None:
            report(INFO, what, observed)
        else:
            check(what, ok, observed, expected)


section("setup.sh: alerts")


@item("radar-alerts.service unit file")
def _(name):
    mine = read(os.path.join(REPO, "systemd", "radar-alerts.service"))
    theirs = read("/etc/systemd/system/radar-alerts.service")
    check(name, theirs is not None and mine == theirs,
          "not installed" if theirs is None else "identical to systemd/radar-alerts.service" if mine == theirs
          else "differs from systemd/radar-alerts.service")
    enabled, active = unit_state("radar-alerts.service")
    check("radar-alerts.service", (enabled, active) == ("enabled", "active"), f"{enabled}, {active}", "enabled, active")
    # An observer: no other unit may need it, so that its failure changes nothing else.
    rc, text, err = run("systemctl", "show", "-p", "RequiredBy,RequisiteOf,BoundBy,WantedBy", "radar-alerts.service")
    if rc != 0:
        raise Skip(f"systemctl show: {err or text}")
    props = dict(line.split("=", 1) for line in text.split("\n") if "=" in line)
    needed_by = [u for key in ("RequiredBy", "RequisiteOf", "BoundBy") for u in props.get(key, "").split()]
    wanted_by = [u for u in props.get("WantedBy", "").split() if u != "multi-user.target"]
    check("no other unit depends on radar-alerts.service", not needed_by and not wanted_by,
          ", ".join(needed_by + wanted_by) or "none", "none (multi-user.target only wants it)")


@item("alertctl command")
def _(name):
    link, target = "/usr/local/bin/alertctl", os.path.join(REPO, "bin", "alertctl")
    real = os.path.realpath(link) if os.path.lexists(link) else None
    check(name, real == os.path.realpath(target), f"{link} -> {real}" if real else f"{link} missing", target)


@item("alerts status file")
def _(name):
    r = status_files.read_status(status_files.status_path("alerts"), 10.0)
    if not r.ok:
        report(FAIL, name, f"{r.state}: {r.error}")
        return
    d = r.data
    check(name, True, f"fresh ({r.age_s:.1f} s old)")
    ntfy = d.get("ntfy") or {}
    check("ntfy settings", ntfy.get("configured") is True, "configured" if ntfy.get("configured") else ntfy.get("problem"),
          "alerts: ntfy: {server, topic, token} in /etc/radar-runner/secrets.yaml")
    link = d.get("link") or {}
    check("alerts reach ntfy", link.get("state") in ("ok", "idle") and not link.get("failures_in_a_row"),
          f"{link.get('state')}: {link.get('last_result') or 'nothing sent yet'}",
          "ok or idle (failing is expected while the Pi is offline; the alerts wait in the queue)")
    q = d.get("queue") or {}
    check("alert queue", not q.get("error"), f"{q.get('length')} waiting, {q.get('dropped')} dropped"
          + (f"; {q['error']}" if q.get("error") else ""))
    errors = d.get("errors") or {}
    check("alerts service errors", not errors, "; ".join(f"{k}: {v}" for k, v in errors.items()) or "none")
    for source, e in sorted((d.get("events") or {}).items()):
        check(f"alerts follows the {source} event log", not e.get("error"),
              f"{e.get('file')}: {e.get('lines')} lines" + (f"; {e['error']}" if e.get("error") else ""))
    report(INFO, "boot summary", "queued in this boot" if d.get("summary_sent") else "not yet (at 3 min of uptime)")
    for problem in d.get("config_warnings") or []:
        if "alerts" in problem:
            report(FAIL, "alerts settings", problem)


section("setup.sh: time (chrony, fake-hwclock)")


@item("time packages")
def _(name):
    pinned("chrony package", package_version("chrony"), "chrony_version")
    pinned("fake-hwclock package", package_version("fake-hwclock"), "fake_hwclock_version")
    old = package_version("systemd-timesyncd")
    check("systemd-timesyncd removed", not old, f"installed ({old})" if old else "not installed",
          "replaced by chrony")


@item("chrony.service")
def _(name):
    enabled, active = unit_state("chrony.service")
    check(name, (enabled, active) == ("enabled", "active"), f"{enabled}, {active}", "enabled, active")


@item("chrony steps the clock at any time (makestep 1 -1)")
def _(name):
    if not package_version("chrony"):
        report(FAIL, name, "chrony is not installed")
        return
    text = out("journalctl", "-b", "-u", "chrony.service", "-o", "cat", "--no-pager")
    if not text:
        raise Skip("chrony's log is not readable" + ("" if IS_ROOT else " (needs root or group adm)"))
    lines = text.split("\n")
    started = max((i for i, line in enumerate(lines) if "chronyd version" in line and "starting" in line), default=-1)
    found = [line for line in lines[started + 1:] if "New makestep" in line]
    check(name, bool(found) and found[-1].endswith("New makestep 1.000000 -1"),
          found[-1].strip() if found else "not set since chronyd started (Ubuntu's 'makestep 1 3' is in effect)",
          "'New makestep 1.000000 -1' in chrony's log after its start")


@item("chrony time sources")
def _(name):
    rc, text, err = run("chronyc", "-c", "sources")
    if rc != 0:
        report(FAIL, name, err or text or "chronyc failed")
        return
    rows = [line.split(",") for line in text.split("\n") if line.count(",") >= 9]
    gps = [r for r in rows if r[2] == "GPS"]
    reach = int(gps[0][5], 8) if gps else 0
    check("chrony GPS source (gpsd)", bool(gps) and reach != 0,
          f"reach {gps[0][5]} (octal), last offset {float(gps[0][7]) * 1000:+.1f} ms" if gps else "no GPS source configured",
          "samples arriving (needs gpsd -n and a position fix)")
    ntp = [r for r in rows if r[0] == "^"]
    report(INFO, "chrony NTP sources", f"{len(ntp)} configured, {sum(1 for r in ntp if int(r[5], 8))} reachable")
    selected = [r[2] for r in rows if r[1] == "*"]
    rc, text, _ = run("chronyc", "-c", "tracking")
    fields = text.split(",")
    synced = rc == 0 and len(fields) >= 14 and fields[13] != "Not synchronised"
    check("clock synchronised by chrony", synced,
          f"source {', '.join(selected) or 'none'}; system clock off by {float(fields[4]) * 1000:+.2f} ms"
          if len(fields) >= 14 else "no answer", "a selected source")


@item("fake-hwclock saves the clock")
def _(name):
    states = {u: unit_state(u) for u in ("fake-hwclock-load.service", "fake-hwclock-save.service", "fake-hwclock-save.timer")}
    ok = all(e == "enabled" for e, _ in states.values()) and states["fake-hwclock-save.timer"][1] == "active"
    check("fake-hwclock units", ok, ", ".join(f"{u} {e}/{a}" for u, (e, a) in states.items()),
          "all enabled, the timer active")
    timers = out("systemctl", "show", "fake-hwclock-save.timer", "-p", "TimersMonotonic", "-p", "TimersCalendar")
    check("fake-hwclock saves every minute", "OnUnitActiveUSec=1min" in timers and "OnCalendar" not in timers,
          " ".join(timers.split()) or "no timers", "OnUnitActiveUSec=1min and no hourly calendar timer")
    saved = (read("/etc/fake-hwclock.data") or "").strip()
    try:
        import calendar
        import time
        age = time.time() - calendar.timegm(time.strptime(saved, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        report(FAIL, name, f"/etc/fake-hwclock.data: {saved or 'missing or empty'}")
        return
    check(name, -5 <= age <= 180, f"last saved {saved} UTC, {age:.0f} s ago", "saved within the last 3 minutes")


section("Optional setup.sh steps (not applied by default)")


@item("held packages")
def _(name):
    rc, text, err = run("apt-mark", "showhold")
    if rc != 0:
        raise Skip(err or "apt-mark failed")
    report(INFO, name, ", ".join(text.split()) or "none (optional: setup.sh --hold-packages)")


# ============================================================ summary

counts = {s: results.count(s) for s in (PASS, FAIL, SKIP, INFO)}
print(f"\ncheck: {counts[PASS]} pass, {counts[FAIL]} FAIL, {counts[SKIP]} skip, {counts[INFO]} info"
      + ("" if IS_ROOT else "  (not run as root)"))
sys.exit(1 if counts[FAIL] else 0)
