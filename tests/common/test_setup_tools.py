"""The lock-file helpers (setup/uvlock.py), the drop-in files and a smoke test of setup/check.py."""
import hashlib
import os
import re
import subprocess
import sys
import tomllib

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "setup"))
import uvlock  # noqa: E402

LOCK = os.path.join(REPO, "uv.lock")
FILES = os.path.join(REPO, "setup", "files")


def write_lock(tmp_path, wheels):
    """A lock file with one package per (name, version, wheel content) entry."""
    text = 'version = 1\n\n[[package]]\nname = "this-project"\nversion = "0.1"\nsource = { virtual = "." }\n'
    for name, version, content in wheels:
        digest = hashlib.sha256(content).hexdigest()
        text += (f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
                 'source = { registry = "https://pypi.org/simple" }\n'
                 f'wheels = [\n    {{ url = "https://example.invalid/x/{name}-{version}-py3-none-any.whl", '
                 f'hash = "sha256:{digest}", size = {len(content)} }},\n]\n')
    path = tmp_path / "uv.lock"
    path.write_text(text)
    return str(path)


def test_locked_packages_of_this_repository():
    packages = uvlock.locked_packages(LOCK)
    assert {"pyserial", "pyyaml", "gpiozero", "lgpio", "pytest"} <= set(packages)
    assert "radar-runner" not in packages                 # the project itself is not a wheel
    for name, p in packages.items():
        assert p["wheels"], name                          # nothing has to be built from source
        for filename, url, digest in p["wheels"]:
            assert filename.endswith(".whl") and url.startswith("https://") and len(digest) == 64


def test_lock_is_for_python_3_12_on_this_machine_type_only():
    with open(LOCK, "rb") as f:
        lock = tomllib.load(f)
    assert lock["requires-python"] == "==3.12.*"
    assert lock["supported-markers"] == ["platform_machine == 'aarch64' and sys_platform == 'linux'"]


def test_wheelhouse_problems(tmp_path):
    lock = write_lock(tmp_path, [("Alpha_Pkg", "1.0", b"alpha"), ("beta", "2.0", b"beta")])
    house = tmp_path / "wheelhouse"
    house.mkdir()
    assert uvlock.wheelhouse_problems(lock, str(house)) == [
        "Alpha_Pkg-1.0-py3-none-any.whl: missing", "beta-2.0-py3-none-any.whl: missing"]
    (house / "Alpha_Pkg-1.0-py3-none-any.whl").write_bytes(b"alpha")
    (house / "beta-2.0-py3-none-any.whl").write_bytes(b"tampered")
    assert uvlock.wheelhouse_problems(lock, str(house)) == ["beta-2.0-py3-none-any.whl: SHA-256 differs from uv.lock"]
    (house / "beta-2.0-py3-none-any.whl").write_bytes(b"beta")
    (house / "extra.whl").write_bytes(b"x")               # extra files do not matter
    assert uvlock.wheelhouse_problems(lock, str(house)) == []


def test_package_without_a_wheel_is_a_problem(tmp_path):
    lock = write_lock(tmp_path, [])
    with open(lock, "a") as f:
        f.write('\n[[package]]\nname = "sdist-only"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n')
    assert uvlock.wheelhouse_problems(lock, str(tmp_path)) == ["sdist-only 1: the lock lists no wheel"]


def test_venv_problems(tmp_path, monkeypatch):
    lock = write_lock(tmp_path, [("Alpha_Pkg", "1.0", b"a"), ("beta", "2.0", b"b")])
    installed = {"alpha-pkg": "1.0", "beta": "2.0"}
    monkeypatch.setattr(uvlock, "venv_packages", lambda python: installed)
    assert uvlock.venv_problems(lock, "python") == []      # names are compared in normal form
    installed.update(beta="2.1", gamma="3")
    del installed["alpha-pkg"]
    assert uvlock.venv_problems(lock, "python") == [
        "alpha-pkg: locked 1.0, installed no", "beta: locked 2.0, installed 2.1", "gamma: locked no, installed 3"]


def test_this_environment_matches_the_lock():
    if not os.path.samefile(sys.prefix, os.path.join(REPO, ".venv")):
        pytest.skip("not running in the repository's .venv")
    assert uvlock.venv_problems(LOCK, sys.executable) == []


@pytest.mark.skipif(not os.path.isdir(os.path.join(REPO, "wheelhouse")), reason="no wheelhouse here")
def test_wheelhouse_is_complete():
    assert uvlock.wheelhouse_problems(LOCK, os.path.join(REPO, "wheelhouse")) == []


# ---- the files that setup.sh installs

def installed_files():
    return sorted(os.path.relpath(os.path.join(root, n), FILES) for root, _, names in os.walk(FILES) for n in names)


def test_only_drop_in_files_owned_by_this_project():
    allowed = ("etc/tmpfiles.d/", "etc/systemd/logind.conf.d/", "etc/systemd/journald.conf.d/",
               "etc/systemd/system.conf.d/", "etc/apt/apt.conf.d/", "etc/needrestart/conf.d/")
    for rel in installed_files():
        assert rel.startswith(allowed), rel
        assert "radar-runner" in rel or rel == "etc/systemd/logind.conf.d/ups.conf", rel
    # Nothing that could affect remote access.
    assert not [rel for rel in installed_files() if re.search(r"ssh|network|netplan|tailscale|resolv", rel)]


def test_drop_in_file_contents():
    def settings(rel):
        with open(os.path.join(FILES, rel)) as f:
            return [line.strip() for line in f if line.strip() and not line.lstrip().startswith(("#", "//"))]

    assert settings("etc/systemd/logind.conf.d/ups.conf") == ["[Login]", "HandlePowerKey=ignore"]
    assert settings("etc/systemd/logind.conf.d/zz-radar-runner.conf") == ["[Login]", "InhibitDelayMaxSec=5"]
    assert settings("etc/systemd/journald.conf.d/radar-runner.conf") == [
        "[Journal]", "Storage=persistent", "SystemMaxUse=8G", "SystemMaxFiles=1000"]
    assert settings("etc/systemd/system.conf.d/radar-runner-watchdog.conf") == [
        "[Manager]", "RuntimeWatchdogSec=30s", "RebootWatchdogSec=10min"]
    assert 'APT::Periodic::Enable "0";' in settings("etc/apt/apt.conf.d/99zz-radar-runner-no-auto-updates")
    assert settings("etc/needrestart/conf.d/radar-runner.conf") == ["$nrconf{restart} = 'l';"]
    # Not a multiple of 16 s: the hardware counter of this watchdog wraps there.
    assert int(re.search(r"RuntimeWatchdogSec=(\d+)s", open(os.path.join(
        FILES, "etc/systemd/system.conf.d/radar-runner-watchdog.conf")).read()).group(1)) % 16 != 0
    # The logind file must sort after the one from unattended-upgrades to override it.
    assert "zz-radar-runner.conf" > "unattended-upgrades-logind-maxdelay.conf"


def test_directories_keep_root_and_user_files_apart():
    with open(os.path.join(FILES, "etc/tmpfiles.d/radar-runner.conf")) as f:
        entries = [line.split() for line in f if line.strip() and not line.startswith("#")]
    assert entries == [
        ["d", "/run/radar-runner", "0755", "root", "root", "-"],
        ["d", "/run/radar-runner/user", "0755", "englacial", "englacial", "-"],
        ["d", "/var/lib/radar-runner", "0755", "root", "root", "-"],
        ["d", "/var/lib/radar-runner/user", "0755", "englacial", "englacial", "-"],
        ["d", "/etc/radar-runner", "0755", "root", "root", "-"]]
    # Nothing is writable by a group or by everyone: a directory that both
    # root and englacial could write would let englacial block or redirect the
    # power service's files.
    assert all(int(mode, 8) & 0o022 == 0 for _, _, mode, *_ in entries)
    # What the shared code uses for englacial's services is inside "user";
    # what the power service uses (power/config.py) is not.
    from power.config import Config
    from radar_runner.common import config, status
    assert config.DEFAULTS["paths"]["run_dir"] == status.DEFAULT_RUN_DIR == "/run/radar-runner/user"
    assert config.DEFAULTS["paths"]["state_dir"] == "/var/lib/radar-runner/user"
    assert os.path.dirname(Config().status_file) == "/run/radar-runner"
    assert Config().state_dir == "/var/lib/radar-runner"
    assert status.POWER_STATUS_FILE == Config().status_file


def test_tmpfiles_accepts_a_directory_that_the_power_service_created_first(tmp_path):
    """The power service makes /run/radar-runner as 0755 if it starts before systemd-tmpfiles."""
    if not os.path.exists("/usr/bin/systemd-tmpfiles"):
        pytest.skip("no systemd-tmpfiles here")
    existing = tmp_path / "run"
    existing.mkdir(mode=0o755)
    (existing / "power.json").write_text("{}")
    group = os.getgid()
    conf = tmp_path / "t.conf"
    # The child line first: systemd-tmpfiles still makes the parent before the directory inside it.
    conf.write_text(f"d {existing}/user 0755 {os.getuid()} {group} -\nd {existing} 0755 {os.getuid()} {group} -\n"
                    f"d {tmp_path}/new/user 0755 {os.getuid()} {group} -\nd {tmp_path}/new 0755 {os.getuid()} {group} -\n")
    subprocess.run(["/usr/bin/systemd-tmpfiles", "--create", str(conf)], check=True, timeout=30)
    for directory in (existing, existing / "user", tmp_path / "new", tmp_path / "new" / "user"):
        assert directory.stat().st_mode & 0o7777 == 0o755
    assert (existing / "power.json").read_text() == "{}"         # contents are kept


def test_scripts_parse():
    for script in ("setup/setup.sh", "setup/build_venv.sh"):
        subprocess.run(["bash", "-n", os.path.join(REPO, script)], check=True, timeout=30)


# ---- check.py: read-only, so it is safe to run for real

def test_check_script_runs_and_reports_every_item():
    r = subprocess.run(["/usr/bin/python3", "-B", os.path.join(REPO, "setup", "check.py")],
                       capture_output=True, text=True, timeout=300, cwd="/")
    assert r.returncode in (0, 1), r.stderr
    assert r.stderr == ""
    lines = [line for line in r.stdout.split("\n") if line and not line.startswith("==")]
    assert all(re.match(r"(PASS|FAIL|SKIP|INFO)  \S|check: \d+ pass", line) for line in lines), lines
    assert "the check itself failed" not in r.stdout
    assert "chrony:" not in r.stdout and "rclone" not in r.stdout       # deferred to stages 2 and 9
    summary = re.search(r"check: (\d+) pass, (\d+) FAIL, (\d+) skip, (\d+) info", r.stdout)
    assert summary and (int(summary.group(2)) > 0) == (r.returncode == 1)
    for rel in installed_files():
        assert f"  /{rel}" in r.stdout
    for name in ("OS release", "kernel", "bootloader", "Tailscale", "SSH enabled", "NVMe mounted",
                 "orca mamba environment", "patched UHD", "ORCA checkout", "repository version",
                 "virtual environment matches uv.lock", "wheelhouse complete", "radar-power.service",
                 "power service status file", "RuntimeWatchdogSec", "watchdog device", "journal size limit",
                 "APT::Periodic::Enable", "apt-daily.timer", "rpi-eeprom-update.service",
                 "snap automatic refresh held forever", "Tailscale automatic self-update off",
                 "needrestart only lists services", "journald SystemMaxUse", "journald SystemMaxFiles",
                 "journald Storage", "systemd RebootWatchdogSec on disk", "watchdog settings in use",
                 "time daemon", "identity: machine-id", "identity: SSH host keys", "identity: Tailscale node",
                 "shutdown inhibitor delay", "power key ignored", "/run/radar-runner/user",
                 "/var/lib/radar-runner/user", "held packages"):
        assert name in r.stdout, name
