"""setup/setup.sh, run for real against a scratch directory (DESTDIR) and stand-ins
for systemctl, snap, tailscale and the other system commands (fake_system_tool.py).

Nothing here can touch the real system: files go under DESTDIR, and PATH holds
only the stand-ins and a few harmless tools (cat, sed, stat, ...). The real
systemctl, tailscale, snap and apt cannot be reached, even by mistake.
"""
import filecmp
import json
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SETUP = os.path.join(REPO, "setup", "setup.sh")
FILES = os.path.join(REPO, "setup", "files")
FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_system_tool.py")
BASH = shutil.which("bash")
FAKE_TOOLS = ("systemctl", "systemd-tmpfiles", "busctl", "dpkg-query", "apt-mark", "snap", "tailscale", "id",
              "findmnt", "lsblk")
REAL_TOOLS = ("cat", "cmp", "diff", "stat", "install", "mktemp", "sed", "awk", "grep", "cut", "tr", "head",
              "wc", "sync", "mv", "cp", "chmod", "rm", "dirname", "uname")
UPDATE_UNITS = ["apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service",
                "fwupd-refresh.timer", "rpi-eeprom-update.service"]
POWER = "radar-power.service"
UUID = "4803c5eb-0612-4afa-a67b-1ad14c085f79"
FSTAB = ("LABEL=writable\t/\text4\tdefaults\t0\t1\n"
         "LABEL=system-boot\t/boot/firmware\tvfat\tdefaults\t0\t1\n"
         f"UUID={UUID} /mnt/nvme ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2\n")


def drop_in_files():
    return sorted(os.path.relpath(os.path.join(root, n), FILES)
                  for root, _, names in os.walk(FILES) for n in names)


class System:
    """A fresh Ubuntu image as setup.sh sees it, under tmp_path."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "root"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for tool in FAKE_TOOLS:
            os.symlink(FAKE, self.bin / tool)
        for tool in REAL_TOOLS:
            os.symlink(shutil.which(tool), self.bin / tool)
        self.path("/etc/needrestart").mkdir(parents=True)             # the needrestart package is installed
        self.path("/etc/needrestart/needrestart.conf").write_text("# stock\n")
        self.state_path = tmp_path / "state.json"
        self.set(units=list(UPDATE_UNITS), enabled=list(UPDATE_UNITS), active=list(UPDATE_UNITS),
                 masked=[], installed=[], held=[], snap_hold="", tailscale_auto_update=True,
                 watchdog="0", inhibit_usec=30000000, fail="", log=[],
                 nvme_device="/dev/nvme0n1p1", nvme_uuid=UUID, nvme_label="radar-data",
                 labels=["writable", "system-boot", "radar-data"], fstab_valid=True)

    @property
    def state(self):
        return json.loads(self.state_path.read_text())

    @property
    def log(self):
        return self.state["log"]

    def set(self, **changes):
        state = self.state if self.state_path.exists() else {}
        state.update(changes)
        self.state_path.write_text(json.dumps(state))

    def run(self, *args):
        env = {"PATH": str(self.bin), "DESTDIR": str(self.root), "FAKE_STATE": str(self.state_path),
               "HOME": str(self.root)}
        return subprocess.run([BASH, SETUP, *args], env=env, capture_output=True, text=True, timeout=120)

    def path(self, name):
        return self.root / name.lstrip("/")

    def tree(self):
        return sorted(os.path.relpath(os.path.join(r, n), self.root)
                      for r, dirs, names in os.walk(self.root) for n in names + dirs)

    def mode(self, name):
        return self.path(name).stat().st_mode & 0o7777

    def install_power_service(self, armed=True, unit_text=None, enabled=True, active=True):
        """As on the bench Pi: the power service is installed, enabled, armed and running."""
        self.path("/etc/radar-runner").mkdir(parents=True, exist_ok=True)
        config = open(os.path.join(REPO, "config", "power.toml")).read()
        self.path("/etc/radar-runner/power.toml").write_text(
            config.replace("armed = false", "armed = true") if armed else config)
        self.path("/etc/systemd/system").mkdir(parents=True, exist_ok=True)
        if unit_text is None:
            shutil.copy(os.path.join(REPO, "systemd", "radar-power.service"),
                        self.path("/etc/systemd/system/radar-power.service"))
        else:
            self.path("/etc/systemd/system/radar-power.service").write_text(unit_text)
        self.path("/etc/systemd/system/radar-power.service").chmod(0o644)
        self.path("/etc/radar-runner/power.toml").chmod(0o644)
        s = self.state
        self.set(units=s["units"] + [POWER], enabled=s["enabled"] + ([POWER] if enabled else []),
                 active=s["active"] + ([POWER] if active else []))


@pytest.fixture
def system(tmp_path):
    return System(tmp_path)


def same_unit_as_repository(system):
    return filecmp.cmp(os.path.join(REPO, "systemd", "radar-power.service"),
                       system.path("/etc/systemd/system/radar-power.service"), shallow=False)


def test_only_stand_ins_and_harmless_tools_are_reachable(system):
    for name in ("tailscale", "systemctl", "snap", "apt-mark"):
        assert os.path.realpath(system.bin / name) == FAKE
    for name in ("apt-get", "apt", "sudo", "reboot", "poweroff", "ssh", "ip", "netplan"):
        assert not (system.bin / name).exists()


def test_dry_run_changes_nothing(system):
    before = system.tree()
    r = system.run("--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert system.tree() == before and system.log == []
    assert "Nothing was changed" in r.stdout and "CHANGE" not in r.stdout
    for rel in drop_in_files():
        assert f"WOULD    install /{rel}" in r.stdout
    for unit in UPDATE_UNITS:
        assert f"WOULD    run: systemctl mask --now {unit}" in r.stdout
    assert "WOULD    run: snap refresh --hold" in r.stdout
    assert "WOULD    run: tailscale set --auto-update=false" in r.stdout
    assert "WOULD    run: systemctl enable --now radar-power.service" in r.stdout
    assert "apt-get" not in r.stdout and "chrony" not in r.stdout       # no package is installed or removed


def test_apply_on_a_fresh_system(system):
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    for rel in drop_in_files():
        assert filecmp.cmp(os.path.join(FILES, rel), system.path(rel), shallow=False)
        assert system.mode(rel) == 0o644
    for directory in ("/run/radar-runner", "/run/radar-runner/user", "/var/lib/radar-runner",
                      "/var/lib/radar-runner/user", "/etc/radar-runner"):
        assert system.mode(directory) == 0o755                     # nothing is group- or world-writable
    assert same_unit_as_repository(system)
    assert "armed = false" in system.path("/etc/radar-runner/power.toml").read_text()     # monitor-only
    log = system.log
    assert [c for c in log if c.startswith("systemctl mask")] == [f"systemctl mask --now {u}" for u in UPDATE_UNITS]
    assert "snap refresh --hold" in log and system.state["snap_hold"] == "forever"
    assert [c for c in log if c.startswith("tailscale")] == ["tailscale set --auto-update=false"]
    assert log.index("systemctl daemon-reload") < log.index("systemctl enable --now radar-power.service")
    assert not any(word in c for c in log for word in ("restart", "reboot", "poweroff", "stop", "apt-get"))
    assert not [p for p in system.tree() if ".radar-setup." in p]                         # no temporary files
    assert "a reboot is needed for:" in r.stdout
    for reason in ("logind: InhibitDelayMaxSec=5", "journald limits"):
        assert reason in r.stdout
    assert "systemd is using RuntimeWatchdogSec=30s" in r.stdout


def test_second_run_changes_nothing(system):
    assert system.run().returncode == 0
    system.set(inhibit_usec=5000000)              # as after the reboot
    before, log_before = system.tree(), system.log
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "setup: nothing to change." in r.stdout
    assert "CHANGE" not in r.stdout and "WOULD" not in r.stdout and "reboot is needed" not in r.stdout
    assert system.tree() == before
    assert system.log == log_before + ["systemctl daemon-reload"]      # the one command every run ends with
    r = system.run("--dry-run")
    assert "setup: nothing to change." in r.stdout and system.log == log_before + ["systemctl daemon-reload"]


# ---- the power service

def test_bench_pi_power_service_is_left_alone(system):
    system.install_power_service(armed=True)
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "armed = true" in system.path("/etc/radar-runner/power.toml").read_text()
    assert "radar-power.service is enabled and running (not restarted)" in r.stdout
    assert "/etc/systemd/system/radar-power.service is in place" in r.stdout
    assert "radar-power unit file changed" not in r.stdout
    assert not [c for c in system.log if "radar-power" in c]


def test_changed_unit_file_is_installed_but_the_service_is_not_restarted(system):
    system.install_power_service(unit_text="[Unit]\nDescription=old version\n")
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert same_unit_as_repository(system)
    assert "replace /etc/systemd/system/radar-power.service" in r.stdout
    assert "radar-power unit file changed" in r.stdout
    assert "systemctl daemon-reload" in system.log and not [c for c in system.log if "radar-power" in c]


def test_enabled_but_stopped_power_service_is_not_started(system):
    system.install_power_service(active=False)
    r = system.run()
    assert r.returncode == 0
    assert "radar-power.service is enabled but inactive: left as it is" in r.stdout
    assert not [c for c in system.log if "radar-power" in c]


def test_interrupted_between_installing_the_unit_and_enabling_it(system):
    # An earlier run copied the unit file and the config, then died.
    system.install_power_service(armed=False, enabled=False, active=False)
    system.set(units=[u for u in system.state["units"] if u != POWER])     # systemd has not seen it yet
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/etc/systemd/system/radar-power.service is in place" in r.stdout
    assert "systemctl enable --now radar-power.service" in system.log
    assert POWER in system.state["enabled"] and POWER in system.state["active"]
    r = system.run()
    assert "radar-power.service is enabled and running (not restarted)" in r.stdout


def test_running_but_not_enabled_power_service_is_enabled_without_a_restart(system):
    system.install_power_service(armed=True, enabled=False, active=True)
    r = system.run()
    assert r.returncode == 0
    assert [c for c in system.log if "radar-power" in c] == ["systemctl enable --now radar-power.service"]
    assert "armed = true" in system.path("/etc/radar-runner/power.toml").read_text()


def test_masked_power_service_is_left_alone(system):
    system.install_power_service(enabled=False, active=False)
    system.set(masked=[POWER])
    r = system.run()
    assert r.returncode == 0 and "radar-power.service is masked: left as it is" in r.stdout
    assert not [c for c in system.log if "radar-power" in c]


# ---- re-running after an interruption

def test_interrupted_after_the_watchdog_file_and_before_the_reload(system):
    assert system.run().returncode == 0
    system.set(watchdog="0", inhibit_usec=5000000, log=[])      # the file is there; systemd never read it
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/etc/systemd/system.conf.d/radar-runner-watchdog.conf is in place" in r.stdout
    assert system.log == ["systemctl daemon-reload"]
    assert system.state["watchdog"] == "30s" and "systemd is using RuntimeWatchdogSec=30s" in r.stdout
    assert "setup: nothing to change." in r.stdout


def test_stops_at_the_first_failure_and_can_be_run_again(system):
    system.set(fail="mask --now apt-daily-upgrade.timer")
    r = system.run()
    assert r.returncode == 1
    assert 'setup: FAILED in step "Automatic updates off"' in r.stderr
    assert "systemctl mask --now apt-daily-upgrade.timer" in r.stderr
    assert "systemctl mask --now apt-daily.timer" in system.log                 # the step before it ran
    assert "systemctl mask --now unattended-upgrades.service" not in system.log  # nothing after it
    assert not system.path("/etc/systemd/journald.conf.d").exists()
    assert not [c for c in system.log if c.startswith(("tailscale", "snap"))]
    assert not [p for p in system.tree() if ".radar-setup." in p]
    system.set(fail="")                                                          # fixed: run again
    r = system.run()
    assert r.returncode == 0 and system.path("/etc/systemd/journald.conf.d/radar-runner.conf").exists()
    assert sorted(system.state["masked"]) == sorted(UPDATE_UNITS)
    assert system.state["watchdog"] == "30s"


def test_running_apt_job_stops_the_update_step_before_any_change(system):
    system.set(active=system.state["active"] + ["apt-daily-upgrade.service"])
    r = system.run()
    assert r.returncode == 1 and "apt-daily-upgrade.service is running an automatic apt job" in r.stderr
    assert not [c for c in system.log if "mask" in c]
    assert not system.path("/etc/apt").exists()


# ---- files and directories

def test_a_changed_drop_in_is_replaced_and_shown(system):
    assert system.run().returncode == 0
    target = system.path("/etc/systemd/journald.conf.d/radar-runner.conf")
    target.write_text("[Journal]\nSystemMaxUse=1G\n")
    r = system.run("--dry-run")
    assert "WOULD    replace /etc/systemd/journald.conf.d/radar-runner.conf" in r.stdout
    assert "-SystemMaxUse=1G" in r.stdout and "+SystemMaxUse=8G" in r.stdout
    assert target.read_text() == "[Journal]\nSystemMaxUse=1G\n"
    assert system.run().returncode == 0
    assert filecmp.cmp(os.path.join(FILES, "etc/systemd/journald.conf.d/radar-runner.conf"), target, shallow=False)


def test_wrong_modes_are_corrected(system):
    assert system.run().returncode == 0
    system.path("/etc/tmpfiles.d/radar-runner.conf").chmod(0o600)
    system.path("/run/radar-runner/user").chmod(0o777)
    r = system.run()
    assert r.returncode == 0 and "owner and mode 600 -> 644" in r.stdout
    assert system.mode("/etc/tmpfiles.d/radar-runner.conf") == 0o644
    assert system.mode("/run/radar-runner/user") == 0o755


def test_directory_made_first_by_the_power_service(system):
    system.path("/run/radar-runner").mkdir(parents=True, mode=0o755)
    system.path("/run/radar-runner/power.json").write_text("{}")
    r = system.run()
    assert r.returncode == 0 and "ok       /run/radar-runner is 755" in r.stdout
    assert system.mode("/run/radar-runner") == 0o755 and system.mode("/run/radar-runner/user") == 0o755
    assert system.path("/run/radar-runner/power.json").read_text() == "{}"


# ---- snap, Tailscale, needrestart

@pytest.mark.parametrize("hold, expect_command", [("", True), ("in 30 days, at 12:00 UTC", True), ("forever", False)])
def test_snap_hold_must_be_forever(system, hold, expect_command):
    system.set(snap_hold=hold)
    r = system.run()
    assert r.returncode == 0
    assert ("snap refresh --hold" in system.log) == expect_command
    assert system.state["snap_hold"] == "forever"


def test_tailscale_auto_update_already_off(system):
    system.set(tailscale_auto_update=False)
    r = system.run()
    assert r.returncode == 0 and "Tailscale automatic update is off" in r.stdout
    assert not [c for c in system.log if c.startswith("tailscale")]


def test_tailscale_not_installed(system):
    os.remove(system.bin / "tailscale")
    r = system.run()
    assert r.returncode == 0 and "Tailscale is not installed" in r.stdout
    assert not [c for c in system.log if c.startswith("tailscale")]


def test_tailscale_not_running_is_reported_and_setup_continues(system):
    system.set(tailscale_auto_update=None)
    r = system.run()
    assert r.returncode == 0 and "cannot read Tailscale's preferences" in r.stdout
    assert not [c for c in system.log if c.startswith("tailscale")]
    assert system.path("/etc/systemd/journald.conf.d/radar-runner.conf").exists()


def test_tailscale_setting_that_does_not_take_is_a_failure(system):
    system.set(tailscale_set_ignored=True)
    r = system.run()
    assert r.returncode == 1 and "Tailscale still has" in r.stderr


def test_needrestart_not_installed(system):
    shutil.rmtree(system.path("/etc/needrestart"))
    r = system.run()
    assert r.returncode == 0 and "needrestart is not installed" in r.stdout
    assert not system.path("/etc/needrestart").exists()


# ---- command line

def test_unknown_option_and_help(system):
    before = system.tree()
    r = system.run("--force")
    assert r.returncode == 2 and "unknown option: --force" in r.stderr and system.tree() == before
    r = system.run("--help")
    assert r.returncode == 0 and "--dry-run" in r.stdout and "tmux" in r.stdout and system.tree() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="would really run as root")
def test_refuses_to_apply_without_root():
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}       # no DESTDIR: the real system
    r = subprocess.run([BASH, SETUP], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "must be run as root" in r.stderr
    assert "== Directories" not in r.stdout                                       # stopped before the first step


def test_optional_steps_do_not_run_by_default(system):
    system.path("/etc/fstab").write_text(FSTAB)
    r = system.run()
    assert r.returncode == 0 and "OPTIONAL" not in r.stdout
    assert system.path("/etc/fstab").read_text() == FSTAB and system.state["held"] == []


# ---- optional: /mnt/nvme by label

@pytest.fixture
def with_fstab(system):
    system.path("/etc/fstab").write_text(FSTAB)
    return system


def test_nvme_by_label(with_fstab):
    system = with_fstab
    r = system.run("--nvme-by-label", "--dry-run")
    assert r.returncode == 0 and f"/etc/fstab: UUID={UUID} -> LABEL=radar-data" in r.stdout
    assert system.path("/etc/fstab").read_text() == FSTAB
    r = system.run("--nvme-by-label")
    assert r.returncode == 0, r.stdout + r.stderr
    assert system.path("/etc/fstab").read_text() == FSTAB.replace(f"UUID={UUID}", "LABEL=radar-data")
    assert system.path("/etc/fstab.before-radar-runner").read_text() == FSTAB
    assert system.mode("/etc/fstab") == 0o644
    assert "fstab: /mnt/nvme by label" in r.stdout
    r = system.run("--nvme-by-label")                         # again: nothing to do
    assert r.returncode == 0 and "/mnt/nvme is already mounted by label (LABEL=radar-data)" in r.stdout
    assert system.path("/etc/fstab.before-radar-runner").read_text() == FSTAB


@pytest.mark.parametrize("change, message", [
    (dict(nvme_label=""), "has no filesystem label"),
    (dict(labels=["radar-data", "radar-data"]), "2 filesystems have the label"),
    (dict(nvme_label="my data"), "unsafe in /etc/fstab"),
    (dict(nvme_device=""), "/mnt/nvme is not mounted"),
    (dict(nvme_uuid="0000"), "is not the one in /etc/fstab"),
    (dict(fstab_valid=False), "does not pass 'findmnt --verify'"),
])
def test_nvme_by_label_refuses(with_fstab, change, message):
    with_fstab.set(**change)
    r = with_fstab.run("--nvme-by-label")
    assert r.returncode == 1 and message in r.stderr
    assert with_fstab.path("/etc/fstab").read_text() == FSTAB
    assert not [p for p in with_fstab.tree() if ".radar-setup." in p]


@pytest.mark.parametrize("fstab, message", [
    (FSTAB.replace(",nofail", ""), "has no 'nofail' option"),
    (FSTAB.replace(f"UUID={UUID}", "/dev/nvme0n1p1"), "expected UUID="),
    (FSTAB + f"UUID={UUID} /mnt/nvme ext4 defaults,nofail 0 2\n", "found 2"),
    (FSTAB.replace(f"UUID={UUID} /mnt/nvme", f"UUID={UUID} /mnt/other"), "found 0"),
])
def test_nvme_by_label_refuses_unexpected_fstab(with_fstab, fstab, message):
    with_fstab.path("/etc/fstab").write_text(fstab)
    r = with_fstab.run("--nvme-by-label")
    assert r.returncode == 1 and message in r.stderr
    assert with_fstab.path("/etc/fstab").read_text() == fstab


# ---- optional: package holds

def test_hold_packages(system):
    kernel = os.uname().release
    wanted = ["gpsd", "linux-image-raspi", f"linux-image-{kernel}", "rpi-eeprom", "flash-kernel",
              "linux-firmware-raspi", "tailscale"]
    system.set(installed=wanted + ["vim"])
    r = system.run("--hold-packages", "--dry-run")
    assert r.returncode == 0 and "WOULD    run: apt-mark hold gpsd" in r.stdout and system.state["held"] == []
    r = system.run("--hold-packages")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(system.state["held"]) == sorted(wanted)
    assert "chrony is not installed" in r.stdout
    r = system.run("--hold-packages")
    assert "gpsd is held" in r.stdout and "apt-mark hold" not in r.stdout
