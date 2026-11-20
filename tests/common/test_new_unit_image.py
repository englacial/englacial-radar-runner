"""setup/new_unit_image.py: the core, on a fake image tree built in a temporary directory.

Not checked here, because it needs root: the image wrapper (losetup, mount, umount), and that
the files the tool writes get root's ownership (the tests run with --skip-ownership; modes are
checked). The trees are built in /dev/shm because the tool refuses any directory on the file
system or disk that this computer runs from, which is where pytest's tmp_path is on the Pi.
"""
import ast
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "setup"))
sys.path.insert(0, REPO)
import new_unit_image as nui  # noqa: E402
from radar_runner.common import config as C  # noqa: E402
from radar_runner.manager import limits  # noqa: E402

TOOL = os.path.join(REPO, "setup", "new_unit_image.py")
OLD, NEW = "englacial-radar-1", "englacial-radar-2"
OLD_ID = "2055a6d999f94c11bac49c72c2dd5240"
IID = "rpi-imager-1790188189686"
TOKEN = "tk_NOTAREALTOKEN0123456789"
SECRETS = f"""alerts:
  ntfy:
    server: https://ntfy.example.invalid
    topic: {OLD}-alerts
    token: {TOKEN}
offload:
  bucket: example
"""
USER_DATA = f"""#cloud-config
manage_resolv_conf: false
hostname: {OLD}
manage_etc_hosts: true
users:
- name: englacial
  passwd: $6$NOTAREALHASH
enable_ssh: true
"""
HOSTS = f"""# Your system has configured 'manage_etc_hosts' as True.
#
127.0.1.1 {OLD} {OLD}
127.0.0.1 localhost

::1 localhost ip6-localhost ip6-loopback
"""
FSTAB = """LABEL=writable\t/\text4\tdefaults\t0\t1
LABEL=system-boot\t/boot/firmware\tvfat\tdefaults\t0\t1
UUID=4803c5eb-0612-4afa-a67b-1ad14c085f79 /mnt/nvme ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2
"""
LOCAL = """# Per-unit settings for this bench unit (layer 3; see config/radar-runner.yaml).
manager:
  limits:
    # measured on the first unit
    max_tx_gain: 70.0
"""
RADAR = """# Radar config (bench).
RF0:
  rx_gain: 40
  tx_gain: 70                # about 9 W with this unit's divider; the cap
  transmit: true

RF1:
  tx_gain: 10
"""


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.lexists(path):
        os.unlink(path)
    with open(path, "wb") as f:
        f.write(text.encode() if isinstance(text, str) else text)
    os.chmod(path, mode)


def keygen(path, kind):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", kind, "-N", "", "-C", f"root@{OLD}", "-f", path], check=True)


def make_unit(top, key_types=("ed25519",)):
    """A tree shaped like englacial-radar-1's card (2026-11-20). Returns (root, boot)."""
    root, boot = os.path.join(top, "root"), os.path.join(top, "boot")
    e = lambda p: os.path.join(root, p.lstrip("/"))      # noqa: E731
    write(e("/etc/hostname"), f"{OLD}\n")
    write(e("/etc/hosts"), HOSTS)
    write(e("/etc/machine-id"), OLD_ID + "\n", 0o444)
    write(e("/etc/fstab"), FSTAB)
    write(e("/etc/default/tailscaled"), 'PORT="41641"\nFLAGS=""\n')
    for kind in key_types:
        keygen(e(f"/etc/ssh/ssh_host_{kind}_key"), kind)
    write(e("/etc/ssh/sshd_config"), "Include /etc/ssh/sshd_config.d/*.conf\n")
    write(e("/etc/radar-runner/local.yaml"), LOCAL)
    shutil.copy(os.path.join(REPO, "tests/manager/fixtures/orca_configs/run300_d05_soak.yaml"),
                e("/etc/radar-runner/radar.yaml"))
    shutil.copy(os.path.join(REPO, "config/power.toml"), e("/etc/radar-runner/power.toml"))
    with open(e("/etc/radar-runner/power.toml")) as f:
        armed = f.read().replace("armed = false", "armed = true")
    write(e("/etc/radar-runner/power.toml"), armed)
    write(e("/etc/radar-runner/secrets.yaml"), SECRETS, 0o640)
    os.makedirs(e("/home/englacial/englacial-radar-runner"))
    write(e("/home/englacial/.bash_history"), "ls\n", 0o600)
    os.makedirs(e("/var/lib/dbus"))
    os.symlink("/etc/machine-id", e("/var/lib/dbus/machine-id"))
    inst = e(f"/var/lib/cloud/instances/{IID}")
    write(f"{inst}/obj.pkl", b"\x80\x04pickled old user-data", 0o400)
    write(f"{inst}/user-data.txt", USER_DATA, 0o600)
    write(f"{inst}/cloud-config.txt", f"#cloud-config\nhostname: {OLD}\nmanage_etc_hosts: true\n", 0o600)
    write(f"{inst}/sem/config_ssh", "done\n")
    os.symlink(f"/var/lib/cloud/instances/{IID}", e("/var/lib/cloud/instance"))
    write(e("/var/lib/cloud/data/previous-hostname"), f"{OLD}\n")
    write(e("/var/lib/cloud/data/set-hostname"), json.dumps({"fqdn": OLD, "hostname": OLD}, indent=1))
    write(e("/var/lib/cloud/data/instance-id"), IID + "\n")
    write(e("/var/lib/tailscale/tailscaled.state"), '{"_machinekey": "privkey:NOTREAL"}', 0o600)
    write(e("/var/lib/tailscale/tailscaled.log.conf"), "{}", 0o600)
    os.chmod(e("/var/lib/tailscale"), 0o700)
    s = e("/var/lib/radar-runner")
    write(f"{s}/power_guard.json", '{"version": 1, "count": 2, "passive": false, "last_shutdown": null}\n')
    write(f"{s}/user/boot_count.json", '{"count": 17, "boot_id": "x"}\n')
    write(f"{s}/user/boot_count.json.lock", "")
    write(f"{s}/user/sdr-state", "safe\n2026-11-20T09:03:49Z telemetry: amplifier current 0.16 A\n")
    write(f"{s}/user/.sdr-state.123.abcd.tmp", "unknown\n")
    write(f"{s}/user/manager-events.jsonl", '{"event": "run_end"}\n')
    write(f"{s}/user/watcher-events.jsonl", '{"event": "trip"}\n')
    write(f"{s}/user/alerts-state.json", '{"version": 1}\n')
    write(f"{s}/user/alerts-queue/000001.json", "{}")
    os.makedirs(f"{s}/user/alerts-inbox")
    write(f"{s}/user/override-no-amp-telemetry", "set by hand\n")
    write(f"{s}/user/auto-blocked", "manual stop\n")
    write(f"{s}/user/telemetry-fallback/b00003/amp.csv", "t,v\n")
    write(e(f"/var/log/journal/{OLD_ID}/system.journal"), b"LPKSHHRH")
    write(e("/var/lib/systemd/random-seed"), os.urandom(32), 0o600)
    write(os.path.join(boot, "config.txt"), "dtparam=i2c_arm=on\n")
    write(os.path.join(boot, "cmdline.txt"), "console=tty1 root=LABEL=writable\n")
    write(os.path.join(boot, "user-data"), USER_DATA)
    write(os.path.join(boot, "meta-data"), f"instance-id: {IID}\n")
    write(os.path.join(boot, "network-config"), "network: {config: disabled}\n")
    return root, boot


@pytest.fixture
def shm():
    if shutil.which("ssh-keygen") is None:
        pytest.skip("no ssh-keygen")
    if not os.path.isdir("/dev/shm"):
        pytest.skip("no /dev/shm")
    top = tempfile.mkdtemp(prefix="test-new-unit.", dir="/dev/shm")
    yield top
    shutil.rmtree(top, ignore_errors=True)


@pytest.fixture
def unit(shm):
    return make_unit(shm)


def run(root, boot, *extra, hostname=NEW):
    return nui.main(["tree", "--root", root, "--boot", boot, "--hostname", hostname, "--skip-ownership", *extra])


def read(root, path):
    with open(os.path.join(root, path.lstrip("/"))) as f:
        return f.read()


def snapshot(*tops):
    """Everything about every entry under the trees: type, mode, size, mtime, content, link target."""
    out = {}
    for top in tops:
        for dirpath, dirnames, filenames in os.walk(top):
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                st = os.lstat(p)
                item = (st.st_mode, st.st_size, st.st_mtime_ns)
                if stat.S_ISLNK(st.st_mode):
                    item += (os.readlink(p),)
                elif stat.S_ISREG(st.st_mode):
                    with open(p, "rb") as f:
                        item += (hashlib.sha256(f.read()).hexdigest(),)
                out[p] = item
    return out


# ------------------------------------------------------------------ one full run, each area

def test_full_run(unit, capsys):
    root, boot = unit
    old_key = read(root, "/etc/ssh/ssh_host_ed25519_key.pub")
    radar_before = read(root, "/etc/radar-runner/radar.yaml")
    power_before = read(root, "/etc/radar-runner/power.toml")
    assert run(root, boot) == 0
    out = capsys.readouterr()
    text = out.out + out.err

    # 1. identity: hostname everywhere cloud-init would take it from, without a new instance
    assert read(root, "/etc/hostname") == f"{NEW}\n"
    assert f"127.0.1.1 {NEW} {NEW}\n" in read(root, "/etc/hosts")
    assert OLD not in read(root, "/etc/hosts")
    assert read(boot, "/user-data") == USER_DATA.replace(f"hostname: {OLD}", f"hostname: {NEW}")
    assert read(boot, "/meta-data") == f"instance-id: {IID}\n"
    assert read(root, "/var/lib/cloud/data/previous-hostname") == f"{NEW}\n"
    assert json.loads(read(root, "/var/lib/cloud/data/set-hostname")) == {"fqdn": NEW, "hostname": NEW}
    inst = f"/var/lib/cloud/instances/{IID}"
    assert not os.path.exists(os.path.join(root, inst.lstrip("/"), "obj.pkl"))
    assert f"hostname: {NEW}\n" in read(root, f"{inst}/user-data.txt")
    assert f"hostname: {NEW}\n" in read(root, f"{inst}/cloud-config.txt")
    assert read(root, f"{inst}/sem/config_ssh") == "done\n"
    assert os.readlink(os.path.join(root, "var/lib/cloud/instance")) == inst
    # machine ID: a new one, never empty; the D-Bus link kept; the old journal and seed gone
    new_id = read(root, "/etc/machine-id").strip()
    assert re.fullmatch(r"[0-9a-f]{32}", new_id) and new_id != OLD_ID
    assert stat.S_IMODE(os.stat(os.path.join(root, "etc/machine-id")).st_mode) == 0o444
    assert os.readlink(os.path.join(root, "var/lib/dbus/machine-id")) == "/etc/machine-id"
    assert not os.path.exists(os.path.join(root, f"var/log/journal/{OLD_ID}"))
    assert not os.path.exists(os.path.join(root, "var/lib/systemd/random-seed"))
    # SSH host keys: new, with modes, fingerprints printed
    new_key = read(root, "/etc/ssh/ssh_host_ed25519_key.pub")
    assert new_key != old_key and new_key.strip().endswith(f"root@{NEW}")
    assert stat.S_IMODE(os.stat(os.path.join(root, "etc/ssh/ssh_host_ed25519_key")).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.join(root, "etc/ssh/ssh_host_ed25519_key.pub")).st_mode) == 0o644
    assert nui.fingerprint(new_key.encode()) in text
    assert f"hostname: {OLD} -> {NEW}" in text and f"machine-id: {OLD_ID} -> {new_id}" in text

    # 2. Tailscale: logged out, and never a suggestion to log out
    assert os.listdir(os.path.join(root, "var/lib/tailscale")) == []
    assert "tailscale logout" not in text.lower()

    # 3. per-unit settings: the template loads cleanly and carries nothing over
    cfg, warnings = C.load(local_file=os.path.join(root, "etc/radar-runner/local.yaml"), secrets_file=None)
    assert warnings == []
    assert cfg["manager"]["limits"]["max_tx_gain"] == C.DEFAULTS["manager"]["limits"]["max_tx_gain"] == 43.0
    assert cfg["manager"]["turn_on_check"]["blocking"] is False
    assert cfg["manager"]["turn_on_check"]["expected"]["verified"] is False
    import yaml
    assert yaml.safe_load(read(root, "/etc/radar-runner/local.yaml")) == {
        "manager": {"limits": {"max_tx_gain": 43}, "turn_on_check": {"blocking": False,
                                                                      "expected": {"verified": False}}}}
    radar = read(root, "/etc/radar-runner/radar.yaml")
    changed = [(a, b) for a, b in zip(radar_before.split("\n"), radar.split("\n")) if a != b]
    assert len(changed) == 1 and changed[0][0] == "  tx_gain: 72.0"
    assert changed[0][1].startswith("  tx_gain: 43 ") and "lowered from 72.0" in changed[0][1]
    assert limits.check(limits.load(radar), cfg["manager"]["limits"]) == []
    assert read(root, "/etc/radar-runner/power.toml") == power_before

    # 4. state: only the pessimistic SDR record and the empty alert directories remain
    user = os.path.join(root, "var/lib/radar-runner/user")
    assert sorted(os.listdir(user)) == ["alerts-inbox", "alerts-queue", "sdr-state"]
    assert os.listdir(os.path.join(user, "alerts-queue")) == []
    assert read(root, "/var/lib/radar-runner/user/sdr-state").split("\n")[0] == "unknown"
    assert os.listdir(os.path.join(root, "var/lib/radar-runner")) == ["user"]

    # 5. the data drive by label, options kept
    assert read(root, "/etc/fstab") == FSTAB.replace("UUID=4803c5eb-0612-4afa-a67b-1ad14c085f79",
                                                     "LABEL=radar-data")

    # 6. alerts: kept, with the warning; the file never printed
    assert read(root, "/etc/radar-runner/secrets.yaml") == SECRETS
    assert "same topic" in text and "secrets.yaml contains the old hostname" in text
    assert TOKEN not in text and "ntfy.example.invalid" not in text

    # 7. the report: marker, left-overs and the checklist
    marker = json.loads(read(root, nui.MARKER))
    assert marker["hostname"] == NEW and marker["old_hostname"] == OLD and marker["state"] == "done"
    assert "/home/englacial/.bash_history" in text
    for words in ("sudo tailscale up", "key expiry", "identity lines", "e2label", "gps_receiver.py --apply",
                  "bootloader", "amplifier's gain", "Turn-on check", "Power service bench tests",
                  "HARDWARE_CONFIG.md"):
        assert words in text, words
    # what still names the old hostname is listed (the secrets file, whose topic was kept)
    assert "root:/etc/radar-runner/secrets.yaml" in text


def test_all_three_key_types(shm, capsys):
    root, boot = make_unit(shm, key_types=("rsa", "ecdsa", "ed25519"))
    old = {k: read(root, f"/etc/ssh/ssh_host_{k}_key") for k in ("rsa", "ecdsa", "ed25519")}
    assert run(root, boot) == 0
    text = capsys.readouterr().out
    for kind, private in old.items():
        assert read(root, f"/etc/ssh/ssh_host_{kind}_key") != private
        pub = read(root, f"/etc/ssh/ssh_host_{kind}_key.pub")
        assert nui.fingerprint(pub.encode()) in text
    assert sorted(os.listdir(os.path.join(root, "etc/ssh"))) == sorted(
        ["sshd_config"] + [f"ssh_host_{k}_key{s}" for k in old for s in ("", ".pub")])


def test_dry_run_changes_nothing(unit, capsys):
    root, boot = unit
    before = snapshot(root, boot)
    assert run(root, boot, "--dry-run") == 0
    assert snapshot(root, boot) == before
    out = capsys.readouterr().out
    assert "would: /etc/hostname" in out and "would: /etc/fstab" in out and "nothing was written" in out
    assert TOKEN not in out


def test_second_run_needs_force(unit, capsys):
    root, boot = unit
    assert run(root, boot) == 0
    first_id = read(root, "/etc/machine-id")
    capsys.readouterr()
    before = snapshot(root, boot)
    assert run(root, boot, hostname="englacial-radar-3") == 2
    err = capsys.readouterr().err
    assert f"hostname {NEW}" in err and "--force" in err and re.search(r"\d{4}-\d\d-\d\dT", err)
    assert snapshot(root, boot) == before
    assert run(root, boot, "--force", hostname="englacial-radar-3") == 0
    assert read(root, "/etc/hostname") == "englacial-radar-3\n"
    assert read(root, "/etc/machine-id") != first_id
    assert "hostname: englacial-radar-3" in read(boot, "/user-data")
    assert json.loads(read(root, nui.MARKER))["hostname"] == "englacial-radar-3"
    assert "tx_gain: 43 " in read(root, "/etc/radar-runner/radar.yaml")


# ------------------------------------------------------------------ symlinks never lead out of the image

def test_tree_resolves_symlinks_inside_the_image(shm):
    top = os.path.join(shm, "t")
    os.makedirs(os.path.join(top, "var/lib/cloud/instances/x"))
    os.symlink("/var/lib/cloud/instances/x", os.path.join(top, "var/lib/cloud/instance"))
    os.symlink("../../../../../../../../etc", os.path.join(top, "var/escape"))
    os.symlink("/", os.path.join(top, "var/rootlink"))
    os.symlink("loop2", os.path.join(top, "loop1"))
    os.symlink("loop1", os.path.join(top, "loop2"))
    t = nui.Tree(top, "root")
    real = os.path.realpath(top)
    assert t.path("/var/lib/cloud/instance/obj.pkl") == f"{real}/var/lib/cloud/instances/x/obj.pkl"
    assert t.path("/var/lib/cloud/instance") == f"{real}/var/lib/cloud/instance"     # not followed
    assert t.path("/var/lib/cloud/instance", follow_last=True) == f"{real}/var/lib/cloud/instances/x"
    assert t.path("/var/escape/hosts") == f"{real}/etc/hosts"
    assert t.path("/var/rootlink/etc/passwd") == f"{real}/etc/passwd"
    assert t.path("/../../etc/passwd") == f"{real}/etc/passwd"
    assert t.path("/missing/../../../etc") == f"{real}/etc"
    with pytest.raises(nui.Stop):
        t.path("/loop1/x")


@pytest.mark.parametrize("link", ["/etc/hosts", "/etc/radar-runner/local.yaml", "/var/lib/dbus/machine-id",
                                  "/etc/radar-runner/secrets.yaml", "/var/lib/radar-runner/user/sdr-state"])
def test_absolute_symlink_to_this_computers_file_stops_the_run(shm, link, capsys):
    root, boot = make_unit(os.path.join(shm, "unit"))
    host = os.path.join(shm, "host-file")       # stands for a file of the imaging computer
    write(host, "the imaging computer's own file\n")
    inside = os.path.join(root, link.lstrip("/"))
    os.unlink(inside)
    os.symlink(host, inside)
    before = snapshot(root, boot)
    assert run(root, boot, "--no-alerts" if "secrets" in link else "--disarm-power") == 2
    with open(host) as f:
        assert f.read() == "the imaging computer's own file\n"
    assert snapshot(root, boot) == before
    assert "STOPPED" in capsys.readouterr().err


def test_absolute_symlinked_directory_is_resolved_inside_the_image(shm, capsys):
    root, boot = make_unit(os.path.join(shm, "unit"))
    host = os.path.join(shm, "host-tailscale")
    write(os.path.join(host, "tailscaled.state"), "the imaging computer's tailscale state")
    shutil.rmtree(os.path.join(root, "var/lib/tailscale"))
    os.symlink(host, os.path.join(root, "var/lib/tailscale"))     # absolute: means <image>/<host path>
    jhost = os.path.join(shm, "host-journal")
    write(os.path.join(jhost, OLD_ID, "system.journal"), "the imaging computer's journal")
    shutil.rmtree(os.path.join(root, "var/log/journal"))
    os.symlink(jhost, os.path.join(root, "var/log/journal"))
    assert run(root, boot) == 0
    assert os.listdir(host) == ["tailscaled.state"]
    assert os.listdir(os.path.join(jhost, OLD_ID)) == ["system.journal"]
    assert "no Tailscale state directory" in capsys.readouterr().out


# ------------------------------------------------------------------ refusals

def test_refuses_the_running_system(unit, capsys):
    root, boot = unit
    assert nui.main(["tree", "--root", "/", "--boot", boot, "--hostname", NEW, "--dry-run"]) == 2
    assert "own root file system" in capsys.readouterr().err


def test_refuses_a_tree_on_the_disk_this_computer_runs_from(tmp_path, unit, capsys):
    if os.stat(tmp_path).st_dev != os.stat("/").st_dev:
        pytest.skip("tmp_path is not on the root file system here")
    root, boot = make_unit(str(tmp_path / "unit"))
    before = snapshot(root, boot)
    assert run(root, boot) == 2
    assert "this computer runs from" in capsys.readouterr().err
    assert snapshot(root, boot) == before


def test_refuses_a_tree_on_the_boot_disk(unit, monkeypatch):
    root, boot = unit
    dev = os.stat(root).st_dev
    with pytest.raises(nui.Stop, match="runs from"):
        nui.refuse_host_tree(root, "--root", host=({dev}, set()))
    monkeypatch.setattr(nui, "_disks", lambda d: {"mmcblk0"} if d == dev else set())
    with pytest.raises(nui.Stop, match="booted from"):
        nui.refuse_host_tree(root, "--root", host=(set(), {"mmcblk0"}))
    nui.refuse_host_tree(root, "--root", host=(set(), {"sda"}))         # another disk is fine


@pytest.mark.parametrize("remove, message", [
    ("etc/radar-runner", "not the root file system of a radar unit"),
    ("home/englacial/englacial-radar-runner", "not the root file system of a radar unit"),
    ("BOOT/config.txt", "not the boot partition"),
])
def test_refuses_a_tree_that_is_not_this_projects_image(unit, remove, message, capsys):
    root, boot = unit
    path = os.path.join(boot, remove[5:]) if remove.startswith("BOOT/") else os.path.join(root, remove)
    shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
    before = snapshot(root, boot)
    assert run(root, boot) == 2
    assert message in capsys.readouterr().err
    assert snapshot(root, boot) == before


@pytest.mark.parametrize("edit, message", [
    (lambda r: write(f"{r}/etc/fstab", FSTAB + FSTAB.split("\n")[2] + "\n"), "exactly one line for /mnt/nvme"),
    (lambda r: write(f"{r}/etc/fstab", FSTAB.replace("nofail,", "")), "no 'nofail'"),
    (lambda r: write(f"{r}/etc/fstab", FSTAB.replace("LABEL=writable", "LABEL=radar-data")), "already mounts"),
    (lambda r: write(f"{r}/var/lib/radar-runner/user/mystery.json", "{}"), "not known to this tool"),
    (lambda r: write(f"{r}/etc/machine-id", ""), "not a machine ID"),
    (lambda r: write(f"{r}/etc/hostname", f"{NEW}\n"), "already"),
    (lambda r: write(f"{r}/etc/hosts", "127.0.0.1 localhost\n"), "one 127.0.1.1 line"),
    (lambda r: write(f"{r}/etc/ssh/ssh_host_ed25519_key-cert.pub", "x"), "not an SSH host key"),
    (lambda r: write(f"{r}/etc/radar-runner/radar.yaml", RADAR + RADAR), "one 'RF0:' section"),
    (lambda r: write(f"{r}/etc/default/tailscaled", 'FLAGS="--statedir=/data"\n'), "--statedir"),
])
def test_stops_on_unexpected_state_before_changing_anything(unit, edit, message, capsys):
    root, boot = unit
    edit(root)
    before = snapshot(root, boot)
    assert run(root, boot) == 2
    assert message in capsys.readouterr().err
    assert snapshot(root, boot) == before


def test_user_data_with_another_hostname_stops(unit, capsys):
    root, boot = unit
    write(os.path.join(boot, "user-data"), USER_DATA.replace(f"hostname: {OLD}", "hostname: somebody-else"))
    assert run(root, boot) == 2
    assert "neither" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--hostname", "Bad_Name"], ["--hostname", "-x"], ["--label", "a" * 17],
                                  ["--label", "a b"], ["--max-tx-gain", "80"], ["--ntfy-topic", "a/b"],
                                  ["--ntfy-topic", "x", "--no-alerts"]])
def test_refuses_bad_arguments(unit, argv):
    root, boot = unit
    with pytest.raises(SystemExit) as e:
        nui.main(["tree", "--root", root, "--boot", boot, "--hostname", NEW, "--skip-ownership", *argv])
    assert e.value.code == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the tool sets owners")
def test_not_root_needs_skip_ownership(unit, capsys):
    root, boot = unit
    assert nui.main(["tree", "--root", root, "--boot", boot, "--hostname", NEW]) == 2
    assert "run as root" in capsys.readouterr().err
    with pytest.raises(nui.Stop, match="run as root"):
        nui.with_image(nui.parse(["image", os.path.join(boot, "config.txt"), "--hostname", NEW]))


def test_image_mode_needs_an_image_file(unit):
    root, boot = unit
    with pytest.raises(nui.Stop, match="not an image file"):
        nui.with_image(nui.parse(["image", root, "--hostname", NEW]))


# ------------------------------------------------------------------ options

def test_cap_option_and_a_radar_config_within_it(unit):
    root, boot = unit
    assert run(root, boot, "--max-tx-gain", "30") == 0
    assert "max_tx_gain: 30\n" in read(root, "/etc/radar-runner/local.yaml")
    assert "  tx_gain: 30 " in read(root, "/etc/radar-runner/radar.yaml")


def test_radar_config_within_the_cap_is_unchanged(unit, capsys):
    root, boot = unit
    write(os.path.join(root, "etc/radar-runner/radar.yaml"), RADAR.replace("tx_gain: 70 ", "tx_gain: 40 "))
    assert run(root, boot) == 0
    assert read(root, "/etc/radar-runner/radar.yaml") == RADAR.replace("tx_gain: 70 ", "tx_gain: 40 ")
    assert "within the cap" in capsys.readouterr().out


def test_radar_config_keeps_its_comments(unit):
    root, boot = unit
    write(os.path.join(root, "etc/radar-runner/radar.yaml"), RADAR)
    assert run(root, boot) == 0
    lines = read(root, "/etc/radar-runner/radar.yaml").split("\n")
    assert lines[0] == "# Radar config (bench)." and lines[-2] == "  tx_gain: 10"
    assert lines[3].startswith("  tx_gain: 43                # lowered from 70")


def test_disarm_power(unit):
    root, boot = unit
    before = read(root, "/etc/radar-runner/power.toml")
    assert run(root, boot, "--disarm-power") == 0
    assert read(root, "/etc/radar-runner/power.toml") == before.replace("\narmed = true", "\narmed = false")


def test_new_ntfy_topic(unit, capsys):
    root, boot = unit
    assert run(root, boot, "--ntfy-topic", "radar-2-alerts") == 0
    assert read(root, "/etc/radar-runner/secrets.yaml") == SECRETS.replace(f"topic: {OLD}-alerts",
                                                                           'topic: "radar-2-alerts"')
    assert stat.S_IMODE(os.stat(os.path.join(root, "etc/radar-runner/secrets.yaml")).st_mode) == 0o640
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_no_alerts(unit, capsys):
    root, boot = unit
    assert run(root, boot, "--no-alerts") == 0
    path = os.path.join(root, "etc/radar-runner/secrets.yaml")
    assert read(root, "/etc/radar-runner/secrets.yaml") == "offload:\n  bucket: example\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    cfg, _ = C.load(local_file=None, secrets_file=path)
    assert cfg["alerts"]["ntfy"]["server"] == "-" and cfg["alerts"]["ntfy"]["token"] == "-"
    assert TOKEN not in capsys.readouterr().out


def test_secrets_in_another_layout_stop_without_printing_them(unit, capsys):
    root, boot = unit
    flow = f"alerts: {{ntfy: {{server: https://x.invalid, topic: t, token: {TOKEN}}}}}\n"
    write(os.path.join(root, "etc/radar-runner/secrets.yaml"), flow, 0o640)
    assert run(root, boot, "--ntfy-topic", "radar-2") == 2
    out = capsys.readouterr()
    assert "edit it by hand" in out.err and TOKEN not in out.out + out.err


def test_label_option(unit):
    root, boot = unit
    assert run(root, boot, "--label", "lab-data") == 0
    assert "LABEL=lab-data /mnt/nvme ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2" in \
        read(root, "/etc/fstab")


# ------------------------------------------------------------------ the file itself

def test_standard_library_only():
    with open(TOOL) as f:
        tree = ast.parse(f.read())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


def test_runs_alone_with_the_system_python(unit):
    root, boot = unit
    copy = os.path.join(os.path.dirname(root), "new_unit_image.py")      # away from the repository
    shutil.copy(TOOL, copy)
    out = subprocess.run(["/usr/bin/python3", "-I", "-B", copy, "tree", "--root", root, "--boot", boot,
                          "--hostname", NEW, "--dry-run"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "would: /etc/hostname" in out.stdout


def test_default_cap_is_the_repositorys():
    assert nui.DEFAULT_MAX_TX_GAIN == C.DEFAULTS["manager"]["limits"]["max_tx_gain"]


def test_documentation_never_suggests_logging_out():
    with open(os.path.join(REPO, "setup", "NEW_UNIT.md")) as f:
        doc = f.read()
    with open(TOOL) as f:
        source = f.read()
    assert "tailscale logout" not in doc.lower() and "tailscale logout" not in source.lower()


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root: file owners")
def test_owners_as_root(unit):
    root, boot = unit
    assert nui.main(["tree", "--root", root, "--boot", boot, "--hostname", NEW]) == 0
    for p in ("etc/machine-id", "etc/ssh/ssh_host_ed25519_key", "etc/radar-runner/local.yaml", nui.MARKER[1:]):
        st = os.stat(os.path.join(root, p))
        assert (st.st_uid, st.st_gid) == (0, 0), p


# ------------------------------------------------------------------ the image wrapper, with fake commands

class FakeSystem:
    """losetup, mount, umount and sync as a record of calls and a model of what is attached and
    mounted: their real use needs root. `trouble` picks where things go wrong."""

    def __init__(self, image, trouble=""):
        self.image, self.trouble = image, trouble
        self.calls, self.attached, self.mounted = [], [], set()

    def handle(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["losetup", "-j"]:
            return 0, "\n".join(f"{d}: []: ({self.image})" for d in self.attached)
        if argv[0] == "losetup" and "--find" in argv:
            self.attached.append("/dev/loop9")
            if self.trouble == "interrupt_in_losetup":      # attached, but --show never returned
                raise KeyboardInterrupt
            return 0, "/dev/loop9"
        if argv[:2] == ["losetup", "-d"]:
            if self.trouble != "busy_loop":                 # a busy device only gets autoclear, status 0
                self.attached.remove(argv[2])
            return 0, ""
        if argv[0] == "mount":
            if self.trouble == "mount_fails" and argv[-2].endswith("p1"):
                return 32, ""
            self.mounted.add(argv[-1])
            if self.trouble == "interrupt_in_mount" and argv[-2].endswith("p1"):
                raise KeyboardInterrupt                     # mounted, but mount never returned
            return 0, ""
        if argv[0] == "umount":
            if self.trouble == "umount_fails":
                return 32, ""
            self.mounted.discard(argv[-1])
            return 0, ""
        return 0, ""

    def run_cmd(self, argv, **kw):
        rc, out = self.handle(argv)
        if rc:
            raise nui.Stop(f"{' '.join(argv)}: failed")
        return out

    def run(self, argv, **kw):
        rc, out = self.handle(argv)
        return subprocess.CompletedProcess(argv, rc, out, "")


@pytest.fixture
def fake_system(shm, monkeypatch):
    image = os.path.join(shm, "unit.img")
    write(image, b"\0" * 1024, 0o600)
    holder = {}

    def make(trouble="", core=""):
        fake = FakeSystem(image, trouble)
        holder["fake"] = fake

        def fake_core(root, boot, args, ownership):
            assert ownership and os.path.isdir(root) and os.path.isdir(boot)
            if core == "stop":
                raise nui.Stop("unexpected state")
            if core == "interrupt":
                raise KeyboardInterrupt
        real_listdir, real_exists, real_ismount = os.listdir, os.path.exists, os.path.ismount
        monkeypatch.setattr(nui.os, "geteuid", lambda: 0)
        monkeypatch.setattr(nui.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(nui, "run_cmd", fake.run_cmd)
        monkeypatch.setattr(nui.subprocess, "run", fake.run)
        monkeypatch.setattr(nui.time, "sleep", lambda s: None)
        monkeypatch.setattr(nui, "probe", lambda dev: {"TYPE": "vfat", "LABEL": "system-boot"}
                            if dev.endswith("p1") else {"TYPE": "ext4", "LABEL": "writable"})
        monkeypatch.setattr(nui, "run_tree", fake_core)
        monkeypatch.setattr(nui.os, "listdir", lambda p: ["loop9p1", "loop9p2", "queue"]
                            if p == "/sys/block/loop9" else real_listdir(p))
        monkeypatch.setattr(nui.os.path, "exists", lambda p: True if p.startswith("/dev/loop9")
                            else real_exists(p))
        monkeypatch.setattr(nui.os.path, "ismount", lambda p: p in fake.mounted or real_ismount(p))
        return fake
    yield image, make
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


@pytest.mark.parametrize("trouble, core, status", [
    ("", "", 0), ("", "stop", 2), ("", "interrupt", 130), ("mount_fails", "", 2),
    ("interrupt_in_losetup", "", 130), ("interrupt_in_mount", "", 130)])
def test_wrapper_always_unmounts_and_detaches(fake_system, capsys, trouble, core, status):
    image, make = fake_system
    fake = make(trouble, core)
    assert nui.main(["image", image, "--hostname", NEW]) == status
    err = capsys.readouterr().err
    assert fake.mounted == set() and fake.attached == []
    assert ["sync"] in fake.calls
    mounts = [c[-1] for c in fake.calls if c[0] == "mount"]
    assert all(not os.path.exists(m) for m in mounts)       # the empty mount points are gone
    assert "image unmounted and detached" in err and "INCOMPLETE" not in err


@pytest.mark.parametrize("trouble, left", [("umount_fails", "still mounted"), ("busy_loop", "/dev/loop9")])
def test_wrapper_reports_an_incomplete_clean_up_with_a_failing_status(fake_system, capsys, trouble, left):
    image, make = fake_system
    fake = make(trouble)
    status = nui.main(["image", image, "--hostname", NEW])
    err = capsys.readouterr().err
    assert status not in (0, 2, 130)                         # never mistaken for success, nor a plain stop
    assert "CLEAN-UP INCOMPLETE" in err and left in err
    assert "image unmounted and detached" not in err
    if trouble == "umount_fails":
        assert not [c for c in fake.calls if c[:2] == ["losetup", "-d"]]     # never detached under a mount


def test_wrapper_dry_run_is_read_only(fake_system):
    image, make = fake_system
    fake = make()
    assert nui.main(["image", image, "--hostname", NEW, "--dry-run"]) == 0
    attach = next(c for c in fake.calls if c[0] == "losetup" and "--find" in c)
    assert "--read-only" in attach
    mounts = [c for c in fake.calls if c[0] == "mount"]
    assert len(mounts) == 2
    for c in mounts:
        options = c[c.index("-o") + 1].split(",")
        assert "ro" in options and "nodev" in options and "nosuid" in options
    assert "noload" in mounts[0][mounts[0].index("-o") + 1].split(",")


def test_wrapper_refuses_an_image_attached_already(fake_system, capsys):
    image, make = fake_system
    fake = make()
    fake.attached.append("/dev/loop3")
    assert nui.main(["image", image, "--hostname", NEW]) == 2
    assert "attached to a loop device already" in capsys.readouterr().err
    assert not [c for c in fake.calls if c[0] == "mount"]


def test_wrapper_missing_image_is_a_clean_stop(shm, capsys):
    assert nui.main(["image", os.path.join(shm, "nothing.img"), "--hostname", NEW]) == 2
    assert "STOPPED" in capsys.readouterr().err


class BrokenAfter:
    """A stdout whose reader (| tee) goes away after `n` writes."""

    def __init__(self, n):
        self.n = n

    def write(self, text):
        self.n -= 1
        if self.n < 0:
            raise BrokenPipeError(32, "Broken pipe")
        return len(text)

    def flush(self):
        pass

    def fileno(self):
        raise OSError("no file descriptor")


def test_broken_pipe_in_the_core_says_partly_changed(unit, monkeypatch, capsys):
    root, boot = unit
    monkeypatch.setattr(sys, "stdout", BrokenAfter(5))
    assert run(root, boot) == 4
    assert "partly changed" in capsys.readouterr().err


def test_broken_pipe_in_the_wrapper_still_cleans_up(fake_system, monkeypatch, capsys):
    image, make = fake_system
    fake = make()
    monkeypatch.setattr(nui, "run_tree", lambda *a, **k: (_ for _ in ()).throw(BrokenPipeError(32, "Broken pipe")))
    assert nui.main(["image", image, "--hostname", NEW]) == 4
    assert fake.mounted == set() and fake.attached == []
    err = capsys.readouterr().err
    assert "partly changed" in err and "image unmounted and detached" in err
