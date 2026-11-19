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
FAKE_TOOLS = ("systemctl", "systemd-tmpfiles", "busctl", "dpkg-query", "apt-mark", "apt-get", "snap", "tailscale",
              "id", "findmnt", "lsblk", "ps")
REAL_TOOLS = ("cat", "cmp", "diff", "stat", "install", "ln", "mktemp", "sed", "awk", "grep", "cut", "tr", "head", "tail",
              "wc", "sync", "mv", "cp", "chmod", "rm", "dirname", "uname", "find", "readlink", "printf")
UPDATE_UNITS = ["apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service",
                "fwupd-refresh.timer", "rpi-eeprom-update.service"]
POWER = "radar-power.service"
TELEMETRY = "radar-telemetry.service"
GPSD_DROP_IN = "etc/systemd/system/gpsd.service.d/radar-runner.conf"
TIME_FILES = ["etc/chrony/conf.d/radar-runner.conf", "etc/systemd/system/chrony.service.d/radar-runner.conf",
              "etc/systemd/system/fake-hwclock-save.timer.d/radar-runner.conf"]
APT_INSTALL = "apt-get -y install --no-install-recommends chrony fake-hwclock"
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
        self.path("/etc/default").mkdir(parents=True)                 # the gpsd package's own settings
        self.path("/etc/default/gpsd").write_text('DEVICES=""\nGPSD_OPTIONS=""\nUSBAUTO="true"\n')
        self.state_path = tmp_path / "state.json"
        # As on the bench Pi before stage 2: gpsd started by the hotplug path (not enabled, no -n),
        # systemd-timesyncd as the time daemon, the data drive mounted.
        self.set(units=list(UPDATE_UNITS) + ["gpsd.service"], enabled=list(UPDATE_UNITS),
                 active=list(UPDATE_UNITS) + ["gpsd.service", "systemd-timesyncd.service"],
                 masked=[], installed=["gpsd", "systemd-timesyncd"], held=[], gpsd_args="/usr/sbin/gpsd",
                 snap_hold="", tailscale_auto_update=True,
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

    def run(self, *args, **extra_env):
        # The OLA's port: a path that does not exist, unless a test puts a file there and opens it.
        env = {"PATH": str(self.bin), "DESTDIR": str(self.root), "FAKE_STATE": str(self.state_path),
               "HOME": str(self.root), "OLA_PORT": str(self.root / "dev-ola-port"), **extra_env}
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
    for name in ("tailscale", "systemctl", "snap", "apt-mark", "apt-get"):
        assert os.path.realpath(system.bin / name) == FAKE
    for name in ("apt", "dpkg", "sudo", "reboot", "poweroff", "ssh", "ip", "netplan", "chown"):
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
    assert "WOULD    run: systemctl enable gpsd.service" in r.stdout
    assert "WOULD    run: systemctl restart gpsd.service" in r.stdout
    assert "WOULD    run: install -d -m 0755 " in r.stdout and "/mnt/nvme/radar-runner" in r.stdout
    assert "WOULD    run: systemctl enable --now radar-telemetry.service" in r.stdout
    assert "apt would remove systemd-timesyncd (replaced by chrony)" in r.stdout
    assert "WOULD    run: apt-get install --no-install-recommends chrony fake-hwclock" in r.stdout
    assert system.state["installed"] == ["gpsd", "systemd-timesyncd"]   # the plan was only simulated


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
    assert not any(word in c for c in log for word in ("reboot", "poweroff", "stop"))
    assert [c for c in log if "restart" in c] == ["systemctl restart gpsd.service"]       # nothing of this project
    assert [c for c in log if c.startswith("apt-get")][-1] == APT_INSTALL and log[-2] == APT_INSTALL     # the last step
    assert system.path("/mnt/nvme/radar-runner").is_dir()
    assert filecmp.cmp(os.path.join(REPO, "systemd", "radar-telemetry.service"),
                       system.path("/etc/systemd/system/radar-telemetry.service"), shallow=False)
    state = system.state
    assert {"gpsd.service", TELEMETRY, "chrony.service", "fake-hwclock-save.timer"} <= set(state["enabled"])
    assert {"gpsd.service", TELEMETRY, "chrony.service", "fake-hwclock-save.timer"} <= set(state["active"])
    assert "systemd-timesyncd" not in state["installed"] and {"chrony", "fake-hwclock"} <= set(state["installed"])
    assert " -n /dev/serial/by-id/usb-u-blox" in state["gpsd_args"]
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
    assert "chrony is not installed" in r.stdout            # the holds come before the step that installs it
    r = system.run("--hold-packages")
    assert {"chrony", "fake-hwclock"} <= set(system.state["held"])
    r = system.run("--hold-packages")
    assert "gpsd is held" in r.stdout and "apt-mark hold" not in r.stdout


# ---- gpsd

def test_gpsd_is_enabled_given_its_receiver_and_restarted_once(system):
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    log = system.log
    assert log.index("systemctl enable gpsd.service") < log.index("systemctl restart gpsd.service")
    assert filecmp.cmp(os.path.join(FILES, GPSD_DROP_IN), system.path(GPSD_DROP_IN), shallow=False)
    assert system.path("/etc/default/gpsd").read_text() == 'DEVICES=""\nGPSD_OPTIONS=""\nUSBAUTO="true"\n'   # untouched
    r = system.run()
    assert "gpsd.service is enabled (runs from boot)" in r.stdout and "gpsd is running with -n" in r.stdout
    assert system.log.count("systemctl restart gpsd.service") == 1


def test_gpsd_is_tied_to_the_receiver_and_the_hotplug_path_is_off(system):
    assert system.run().returncode == 0
    device = r"dev-serial-by\x2did-usb\x2du\x2dblox_AG_\x2d_www.u\x2dblox.com_u\x2dblox_GNSS_receiver\x2dif00.device"
    assert os.path.islink(system.path(f"/etc/systemd/system/{device}.wants/gpsd.service"))
    assert "ExecStart=/bin/true" in system.path("/etc/systemd/system/gpsdctl@.service.d/radar-runner.conf").read_text()
    assert "systemctl enable radar-gps-receiver.service" in system.log
    assert "radar-gps-receiver.service" in system.state["enabled"]
    before = len(system.log)
    r = system.run()                                   # a second run changes nothing
    assert r.returncode == 0 and "nothing to change" in r.stdout and len(system.log) == before + 1   # the daemon-reload


def test_gpsd_running_without_n_is_restarted_even_if_the_file_is_in_place(system):
    assert system.run().returncode == 0
    system.set(gpsd_args="/usr/sbin/gpsd", log=[])          # e.g. an earlier run was interrupted before the restart
    r = system.run()
    assert r.returncode == 0 and system.log == ["systemctl restart gpsd.service", "systemctl daemon-reload"]


def test_gpsd_not_installed_and_hotplug_switched_off_are_reported(system):
    system.set(installed=["systemd-timesyncd"])
    r = system.run()
    assert r.returncode == 0 and "gpsd is not installed: nothing done" in r.stdout
    assert not system.path(GPSD_DROP_IN).exists() and not [c for c in system.log if "gpsd.service" in c]
    system.set(installed=system.state["installed"] + ["gpsd"])
    system.path("/etc/default/gpsd").write_text('USBAUTO="false"\n')
    r = system.run()
    assert r.returncode == 0 and "USBAUTO=false: a receiver plugged in after gpsd started will not be" in r.stdout
    assert system.path("/etc/default/gpsd").read_text() == 'USBAUTO="false"\n'


# ---- the telemetry service

def test_telemetry_service_is_installed_enabled_and_started(system):
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "systemctl enable --now radar-telemetry.service" in system.log
    assert "the bench tools pa_guard.py and pa_run.py cannot run" in r.stdout
    assert system.mode("/etc/systemd/system/radar-telemetry.service") == 0o644
    r = system.run()
    assert "radar-telemetry.service is enabled and running (not restarted)" in r.stdout
    assert "/mnt/nvme/radar-runner belongs to englacial" in r.stdout


def test_running_telemetry_service_is_never_restarted_or_stopped(system):
    assert system.run().returncode == 0
    system.path("/etc/systemd/system/radar-telemetry.service").write_text("[Unit]\nDescription=old\n")
    system.set(log=[])
    r = system.run()
    assert r.returncode == 0 and "replace /etc/systemd/system/radar-telemetry.service" in r.stdout
    assert "keeps the old unit until it is restarted: systemctl restart radar-telemetry" in r.stdout
    assert system.log == ["systemctl daemon-reload"]
    # Stopped by hand for bench work: left stopped.
    system.set(active=[u for u in system.state["active"] if u != TELEMETRY], log=[])
    r = system.run()
    assert "radar-telemetry.service is enabled but inactive: left as it is" in r.stdout
    assert not [c for c in system.log if "telemetry" in c]


def test_refuses_to_start_the_telemetry_service_while_another_program_has_the_ola_port(system):
    port = system.root / "dev-ola-port"
    port.parent.mkdir(parents=True, exist_ok=True)
    port.write_text("")
    with open(port):                        # as a bench pa_guard.py run holds the serial port
        r = system.run()
        assert r.returncode == 1
        assert 'setup: FAILED in step "Telemetry service unit and data directory"' in r.stderr
        assert f"the OLA's serial port ({port}) is in use by process {os.getpid()} " in r.stderr
        assert "was not enabled or started" in r.stderr and "run setup again" in r.stderr
        assert not [c for c in system.log if "radar-telemetry" in c]
        assert TELEMETRY not in system.state["enabled"] and TELEMETRY not in system.state["active"]
        assert not [c for c in system.log if c.startswith("apt-get")]       # stopped there
        r = system.run("--dry-run")                                         # the dry run says so too
        assert r.returncode == 1 and "is in use by process" in r.stderr
    r = system.run()                        # the bench run has ended: run again
    assert r.returncode == 0, r.stdout + r.stderr
    assert "systemctl enable --now radar-telemetry.service" in system.log


def test_a_port_held_by_the_running_telemetry_service_itself_is_no_obstacle(system):
    assert system.run().returncode == 0
    port = system.root / "dev-ola-port"
    port.write_text("")
    with open(port):                        # the service is enabled and running: it is the holder
        r = system.run()
    assert r.returncode == 0 and "radar-telemetry.service is enabled and running (not restarted)" in r.stdout


def test_data_drive_not_mounted_is_reported_and_the_service_still_installed(system):
    system.set(nvme_device="")
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/mnt/nvme is not mounted: /mnt/nvme/radar-runner cannot be made now" in r.stdout
    assert not system.path("/mnt/nvme/radar-runner").exists()
    assert TELEMETRY in system.state["active"]


# ---- time: chrony and fake-hwclock in place of systemd-timesyncd

def test_time_packages_are_downloaded_first_then_installed(system):
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    apt = [c for c in system.log if c.startswith("apt-get")]
    assert apt == ["apt-get -y --download-only install --no-install-recommends chrony fake-hwclock",
                   "apt-get -y --download-only --reinstall install systemd-timesyncd",     # the way back, in the cache
                   APT_INSTALL]
    for rel in TIME_FILES:                      # in place before the packages start their services
        assert filecmp.cmp(os.path.join(FILES, rel), system.path(rel), shallow=False)
    assert "chrony.service is running" in r.stdout
    assert "fake-hwclock saves the clock every minute and at shutdown" in r.stdout
    r = system.run()
    assert "chrony and fake-hwclock are installed" in r.stdout and "setup: nothing to change." in r.stdout
    assert [c for c in system.log if c.startswith("apt-get")] == apt


@pytest.mark.parametrize("extra, message", [
    (["Remv tailscale [1.80.0]"], "remove:tailscale"),
    (["Remv openssh-server [1:9.6p1]"], "remove:openssh-server"),
    (["Inst libc6 [2.39-0ubuntu8.4] (2.39-0ubuntu8.6 Ubuntu:24.04/noble-updates [arm64])"], "upgrade:libc6"),
    (["Inst tzdata [2025b-0ubuntu0.24.04.1] (2026e-0ubuntu0.24.04.1 Ubuntu:24.04/noble-security [all])"], "upgrade:tzdata"),
])
def test_time_step_refuses_when_apt_would_remove_or_upgrade_anything_else(system, extra, message):
    system.set(apt_extra_plan=extra)
    r = system.run()
    assert r.returncode == 1 and message in r.stderr and "Nothing was changed" in r.stderr
    assert not [c for c in system.log if c.startswith("apt-get")]           # only the simulation ran
    assert "systemd-timesyncd" in system.state["installed"] and "systemd-timesyncd.service" in system.state["active"]
    assert TELEMETRY in system.state["active"]                              # every step before it was done


def test_time_step_without_internet_changes_nothing(system):
    system.set(offline=True)
    r = system.run()
    assert r.returncode == 1 and "cannot download chrony fake-hwclock" in r.stderr
    assert "systemd-timesyncd stays" in r.stderr
    assert system.state["installed"] == ["gpsd", "systemd-timesyncd"]
    assert "systemd-timesyncd.service" in system.state["active"]
    system.set(offline=False)                   # later, with internet: run again
    r = system.run()
    assert r.returncode == 0 and "chrony" in system.state["installed"]


def test_time_step_when_apt_cannot_plan(system):
    system.set(apt_broken=True)
    r = system.run()
    assert r.returncode == 1 and "apt cannot work out how to install chrony fake-hwclock" in r.stderr
    assert "Unable to locate package chrony" in r.stderr
    assert not [c for c in system.log if c.startswith("apt-get")]


def test_interrupted_dpkg_is_named_with_the_command_that_finishes_it(system):
    system.set(dpkg_interrupted=True)
    r = system.run()
    assert r.returncode == 1
    assert "an earlier package installation was interrupted" in r.stderr
    assert "sudo dpkg --configure -a" in r.stderr and "then run setup again" in r.stderr
    assert "apt-get update" not in r.stderr                                 # that would not help here
    assert not [c for c in system.log if c.startswith("apt-get")]
    system.set(dpkg_interrupted=False)
    assert system.run().returncode == 0


def test_failed_install_puts_systemd_timesyncd_back(system):
    system.set(apt_install_fails=True)
    r = system.run()
    assert r.returncode == 1 and "RECOVER  apt-get install chrony fake-hwclock failed" in r.stdout
    assert "systemd-timesyncd is installed and running again" in r.stderr
    assert system.log[-2:] == ["apt-get -y install systemd-timesyncd", "systemctl start systemd-timesyncd.service"]
    assert "systemd-timesyncd" in system.state["installed"] and "systemd-timesyncd.service" in system.state["active"]
    assert "chrony" not in system.state["installed"]


def test_chrony_that_does_not_start_puts_systemd_timesyncd_back(system):
    system.set(chrony_wont_start=True)
    r = system.run()
    assert r.returncode == 1 and "RECOVER  chrony does not start" in r.stdout
    assert "systemctl start chrony.service" in system.log                   # tried once
    assert "systemd-timesyncd.service" in system.state["active"] and "chrony" not in system.state["installed"]


def test_no_way_back_is_said_loudly(system):
    system.set(apt_install_fails=True, timesyncd_gone_for_good=True)
    r = system.run()
    assert r.returncode == 1 and "NO TIME DAEMON IS RUNNING" in r.stderr
    assert r.stderr.index("sudo dpkg --configure -a") < r.stderr.index("sudo apt-get install systemd-timesyncd")


def test_changed_time_settings_restart_chrony_and_the_timer(system):
    assert system.run().returncode == 0
    system.path(TIME_FILES[0]).write_text("refclock SHM 0\n")
    system.set(log=[])
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert system.log == ["systemctl daemon-reload", "systemctl restart chrony.service",
                          "systemctl restart fake-hwclock-save.timer", "systemctl daemon-reload"]
    assert filecmp.cmp(os.path.join(FILES, TIME_FILES[0]), system.path(TIME_FILES[0]), shallow=False)


def test_apt_runs_without_prompts_and_without_needrestart(system):
    # The stand-in refuses (status 98) unless DEBIAN_FRONTEND=noninteractive and NEEDRESTART_SUSPEND=1 are set.
    r = system.run()
    assert r.returncode == 0 and "would have been interactive" not in r.stdout + r.stderr
    text = open(SETUP).read()
    assert "DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1" in text
    assert "autoremove" not in text and "dist-upgrade" not in text and "apt-get upgrade" not in text


# ---- the radar manager (stage 4): installed, never enabled or started

MANAGER = "radar-manager.service"
RADARCTL_LINK = "/usr/local/bin/radarctl"
RADARCTL_TARGET = "/home/englacial/englacial-radar-runner/bin/radarctl"


def test_manager_unit_is_installed_but_not_enabled_or_started(system):
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert filecmp.cmp(os.path.join(REPO, "systemd", MANAGER), system.path(f"/etc/systemd/system/{MANAGER}"),
                       shallow=False)
    assert system.mode(f"/etc/systemd/system/{MANAGER}") == 0o644
    assert not [c for c in system.log if "radar-manager" in c]          # no enable, no start, nothing
    assert MANAGER not in system.state["enabled"] and MANAGER not in system.state["active"]
    assert "It is NOT enabled and NOT started by this script" in r.stdout
    assert "First start with the real radar" in r.stdout
    assert log_index(system, "systemctl daemon-reload") >= 0            # systemd is told about the new unit


def log_index(system, command):
    return system.log.index(command) if command in system.log else -1


def test_radarctl_is_linked_into_usr_local_bin(system):
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.readlink(system.path(RADARCTL_LINK)) == RADARCTL_TARGET
    assert f"CHANGE   link {RADARCTL_LINK} -> {RADARCTL_TARGET}" in r.stdout
    r = system.run()
    assert f"ok       {RADARCTL_LINK} points to {RADARCTL_TARGET}" in r.stdout
    assert "radar-manager.service is in place" in r.stdout


def test_radarctl_link_to_another_place_is_corrected_and_a_plain_file_is_refused(system):
    link = system.path(RADARCTL_LINK)
    link.parent.mkdir(parents=True)
    os.symlink("/somewhere/else/radarctl", link)
    r = system.run()
    assert r.returncode == 0 and "points to /somewhere/else/radarctl" in r.stdout
    assert os.readlink(link) == RADARCTL_TARGET
    link.unlink()
    link.write_text("#!/bin/sh\n")
    r = system.run()
    assert r.returncode == 1 and f"{RADARCTL_LINK} exists and is not a link" in r.stderr
    assert link.read_text() == "#!/bin/sh\n"


def test_manager_step_in_a_dry_run(system):
    r = system.run("--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"WOULD    install /etc/systemd/system/{MANAGER}" in r.stdout
    assert f"WOULD    link {RADARCTL_LINK} -> {RADARCTL_TARGET}" in r.stdout
    assert not system.path(RADARCTL_LINK).is_symlink() and not system.path(f"/etc/systemd/system/{MANAGER}").exists()


def test_running_manager_is_never_restarted_stopped_or_disabled(system):
    assert system.run().returncode == 0
    system.set(enabled=system.state["enabled"] + [MANAGER], active=system.state["active"] + [MANAGER])   # by hand
    system.path(f"/etc/systemd/system/{MANAGER}").write_text("[Unit]\nDescription=old\n")
    system.set(log=[])
    r = system.run()
    assert r.returncode == 0 and f"replace /etc/systemd/system/{MANAGER}" in r.stdout
    assert "the running manager keeps the old unit until it is restarted" in r.stdout
    assert "radar-manager.service is enabled and running (not restarted)" in r.stdout
    assert system.log == ["systemctl daemon-reload"]
    assert MANAGER in system.state["enabled"] and MANAGER in system.state["active"]


def test_power_service_hooks_are_printed_not_applied(system):
    r = system.run()
    assert r.returncode == 0, r.stdout + r.stderr
    out = " ".join(r.stdout.split())
    # Printed for the user to put into /etc/radar-runner/power.toml ...
    assert "The power service's hooks are not changed by this script" in out
    assert ('stop_radar = ["/usr/bin/setpriv", "--reuid=englacial", "--regid=englacial", "--init-groups", '
            '"--reset-env", "/usr/bin/python3", "-B", "-E", "-s", '
            '"/home/englacial/englacial-radar-runner/radar_runner/manager/radarctl.py", '
            '"stop", "--interrupt", "--wait", "--timeout", "4"]') in out
    assert "stop_radar_timeout_s = 5.0" in out
    assert ('amp_safe = ["/usr/bin/setpriv", "--reuid=englacial", "--regid=englacial", "--init-groups", '
            '"--reset-env", "/home/englacial/englacial-radar-runner/tools/sdr_gpio_safe/amp_safe", '
            '"--timeout", "20", "--state-file", "/var/lib/radar-runner/user/sdr-state"]') in out
    assert "amp_safe_timeout_s = 22.0" in out
    # ... and the installed config is the repository's, with no hook set.
    config = system.path("/etc/radar-runner/power.toml").read_text()
    assert "stop_radar = []" in config and "amp_safe = []" in config and "setpriv" not in config


def test_printed_hook_limits_are_longer_than_the_commands_own_timeouts(system):
    import re
    import tomllib
    out = system.run().stdout
    block = out[out.index("stop_radar = ["):out.index("(each hook's limit")]
    block = block[:block.rindex("\n")]
    hooks = tomllib.loads(block)
    for name in ("stop_radar", "amp_safe"):
        argv = hooks[name]
        own = float(argv[argv.index("--timeout") + 1])
        assert hooks[f"{name}_timeout_s"] > own, name
        assert argv[0] == "/usr/bin/setpriv" and "--reset-env" in argv      # root never runs a user's file directly
    assert re.search(r"amp_safe\b", hooks["amp_safe"][5])
    assert os.path.exists(os.path.join(REPO, "tools", "sdr_gpio_safe", "amp_safe"))
    assert os.path.exists(os.path.join(REPO, "radar_runner", "manager", "radarctl.py"))
