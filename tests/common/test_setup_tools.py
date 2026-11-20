"""The lock-file helpers (setup/uvlock.py), the drop-in files and a smoke test of setup/check.py."""
import hashlib
import json
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
               "etc/systemd/system.conf.d/", "etc/apt/apt.conf.d/", "etc/needrestart/conf.d/",
               # stage 2: drop-ins for gpsd, chrony and fake-hwclock only
               "etc/systemd/system/gpsd.service.d/", "etc/systemd/system/gpsdctl@.service.d/",
               "etc/systemd/system/chrony.service.d/",
               "etc/systemd/system/fake-hwclock-save.timer.d/", "etc/chrony/conf.d/")
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
    # Stage 2. gpsd: reads without clients, passive, the receiver by its stable name, restarted if it dies.
    device = r"dev-serial-by\x2did-usb\x2du\x2dblox_AG_\x2d_www.u\x2dblox.com_u\x2dblox_GNSS_receiver\x2dif00.device"
    # Ubuntu's hotplug path is off: with both, the receiver was opened twice at boot and gpsd delivered nothing.
    assert settings("etc/systemd/system/gpsdctl@.service.d/radar-runner.conf") == [
        "[Service]", "ExecStart=", "ExecStart=/bin/true", "ExecStop=", "ExecStop=/bin/true"]
    assert settings("etc/systemd/system/gpsd.service.d/radar-runner.conf") == [
        "[Unit]", "BindsTo=" + device, "After=" + device, "[Service]", "ExecStart=",
        "ExecStart=/usr/sbin/gpsd -n -p /dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00",
        "Restart=on-failure", "RestartSec=5"]
    # chrony: GPS from gpsd's shared memory; nothing else (the NTP pools stay Ubuntu's). No "makestep" here:
    # Ubuntu's chrony.conf would override it, so it is set by the service drop-in, and a failure there is ignored.
    assert settings("etc/chrony/conf.d/radar-runner.conf") == ["refclock SHM 0 refid GPS poll 3 offset 0.070 delay 0.2"]
    assert settings("etc/systemd/system/chrony.service.d/radar-runner.conf") == [
        "[Service]", "ExecStartPost=-/usr/bin/chronyc makestep 1 -1"]
    # fake-hwclock: every minute by monotonic timers (the hourly calendar timer is cleared).
    assert settings("etc/systemd/system/fake-hwclock-save.timer.d/radar-runner.conf") == [
        "[Timer]", "OnCalendar=", "OnBootSec=1min", "OnUnitActiveSec=1min", "AccuracySec=5s"]
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
    assert "rclone" not in r.stdout                                     # deferred to stage 9
    summary = re.search(r"check: (\d+) pass, (\d+) FAIL, (\d+) skip, (\d+) info", r.stdout)
    assert summary and (int(summary.group(2)) > 0) == (r.returncode == 1)
    for rel in installed_files():
        assert f"  /{rel}" in r.stdout
    # Without --receiver nothing is asked of the GPS receiver.
    assert "SKIP  GPS receiver settings (asked of the receiver): not asked" in r.stdout
    assert "NMEA output on UART1" not in r.stdout
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
                 "/var/lib/radar-runner/user", "held packages",
                 # stage 2
                 "gpsd.service enabled and running", "gpsd keeps reading without clients, passive", "gpsd hotplug",
                 "GPS receiver on USB", "GPS receiver known to gpsd, once", "GPS receiver identified by gpsd",
"radar-telemetry.service unit file",
                 "sensor log directory on the data drive", "telemetry status file", "chrony package",
                 "fake-hwclock package", "systemd-timesyncd removed", "chrony.service",
                 "chrony steps the clock at any time", "fake-hwclock units", "fake-hwclock saves",
                 # stage 3
                 "radar-watcher.service unit file", "SDR GPIO reset tool", "safety watcher status file",
                 # stage 4
                 "radar-manager.service unit file", "radarctl command", "radar manager answers",
                 "radar is killed if the manager dies", "front panel pins usable by", "real-time priority for the radar",
                 "make-amplifier-safe tool", "power service hook stop_radar", "power service hook amp_safe"):
        assert name in r.stdout, name


UBXTOOL_STAND_IN = """#!/usr/bin/python3
import json, sys
with open({log!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if "-g" in sys.argv:
    layer = int(sys.argv[-1].split(",")[1])
    if layer == 1 and {flash_nmea!r} == "not stored":
        print("UBX-ACK-NAK:\\n  NAK to Class x06 (CFG) ID x8b (VALGET)")
    else:
        print("UBX-CFG-VALGET:\\n version 1 layer %d position 0\\n  layers (...)" % layer)
        print("    item CFG-MSGOUT-UBX_NAV_PVT_USB/0x20910009 val 1")
        print("    item CFG-UART1OUTPROT-NMEA/0x10740002 val %s" % ({value!r} if layer < 2 else {flash_nmea!r}))
else:
    print("UBX-MON-COMMS:\\nversion 0 nPorts 3 txErrors x{errors} reserved1 0\\n  Port: 0x300 (USB)\\n"
          "   txPending 0 txBytes 26542809 txUsage 5 txPeakUsage 88")
"""


@pytest.mark.parametrize("value, flash_nmea, errors, expected, nmea, pvt", [
    ("0", "0", "10", "PASS", "RAM 0, battery-backed 0, flash 0", "RAM 1, battery-backed 1, flash 1"),
    ("1", "0", "90", "FAIL", "RAM 1, battery-backed 1, flash 0", "RAM 1, battery-backed 1, flash 1"),
    ("0", "1", "10", "FAIL", "RAM 0, battery-backed 0, flash 1", "RAM 1, battery-backed 1, flash 1"),
    ("0", "not stored", "10", "FAIL", "RAM 0, battery-backed not stored, flash not stored",
     "RAM 1, battery-backed not stored, flash 1")])
def test_check_script_asks_the_receiver_only_when_told_and_only_reads(tmp_path, value, flash_nmea, errors, expected,
                                                                      nmea, pvt):
    """With --receiver: four polls through (a stand-in for) ubxtool, and never an option that sets anything."""
    log = tmp_path / "ubxtool.log"
    tool = tmp_path / "ubxtool"
    tool.write_text(UBXTOOL_STAND_IN.format(log=str(log), value=value, flash_nmea=flash_nmea, errors=errors))
    tool.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    r = subprocess.run(["/usr/bin/python3", "-B", os.path.join(REPO, "setup", "check.py"), "--receiver"],
                       capture_output=True, text=True, timeout=300, cwd="/", env=env)
    assert r.returncode in (0, 1) and r.stderr == ""
    assert f"{expected}  GPS receiver: NMEA output on UART1 off: CFG-UART1OUTPROT-NMEA: {nmea}" in r.stdout
    pvt_status = "FAIL" if "not stored" in pvt else "PASS"
    assert f"{pvt_status}  GPS receiver: position message (NAV-PVT) on USB: CFG-MSGOUT-UBX_NAV_PVT_USB: {pvt}" in r.stdout
    errors_status = "FAIL" if errors == "90" else "PASS"
    assert f"{errors_status}  GPS receiver: no transmit buffer errors: txErrors 0x{errors}, USB buffer peak 88 %" in r.stdout
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [["-P", "27.50", "-w", "1.5", "-g", f"CFG-MSGOUT-UBX_NAV_PVT_USB,{n}", "-g", f"CFG-UART1OUTPROT-NMEA,{n}"]
                     for n in range(3)] + [["-P", "27.50", "-w", "1.5", "-p", "MON-COMMS"]]
    for call in calls:
        assert not {"-z", "-x", "-e", "-d", "-c", "-S", "-s"} & set(call)


def test_check_script_rejects_unknown_options():
    r = subprocess.run(["/usr/bin/python3", "-B", os.path.join(REPO, "setup", "check.py"), "--fix"],
                       capture_output=True, text=True, timeout=60, cwd="/")
    assert r.returncode != 0 and "usage: setup/check.py [--receiver]" in r.stderr and r.stdout == ""


# ---- check.py: gpsd's identification of the receiver (a pure function, taken from the source)

def check_function(name):
    import ast
    with open(os.path.join(REPO, "setup", "check.py")) as f:
        tree = ast.parse(f.read())
    wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(wanted) == 1
    scope = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "INFO": "INFO"}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "check.py", "exec"), scope)
    return scope[name]


def gps_identification(devices):
    return check_function("gps_identification")(devices)


def test_check_fails_fix_times_without_a_position_message():
    finding = check_function("gps_fix_finding")
    assert finding({"time": "2026-11-19T19:00:00.000Z", "mode": 0})[0] == "FAIL"
    assert finding({"time": "2026-11-19T19:00:00.000Z"})[0] == "FAIL"
    assert finding({"time": "2026-11-19T19:00:00.000Z", "mode": 1})[0] == "INFO"     # NAV-PVT without a fix
    assert finding({"mode": 3, "time": "x"}) == ("INFO", "3D fix")
    assert finding({})[0] == "INFO"                                                   # nothing from gpsd yet


def test_check_fails_a_receiver_that_gpsd_has_not_identified():
    device = {"class": "DEVICE", "path": "/dev/ttyACM0", "driver": "u-blox", "subtype": ""}
    status, observed = gps_identification([device])
    assert status == "FAIL" and "gpsd has not identified /dev/ttyACM0" in observed and "gps_receiver.py" in observed
    assert gps_identification([dict(device, subtype="SW EXT CORE 1.00", subtype1="PROTVER=27.50,MOD=ZED-F9P")]) == (
        "PASS", "PROTVER=27.50,MOD=ZED-F9P")
    assert gps_identification([dict(device, subtype="SW EXT CORE 1.00")]) == ("PASS", "SW EXT CORE 1.00")
    assert gps_identification([dict(device, subtype="Unknown")])[0] == "FAIL"     # gpsd's placeholder
    assert gps_identification([]) == ("SKIP", "gpsd has no receiver")             # cannot tell
    assert gps_identification(None)[0] == "SKIP"


# ---- check.py: the radar's hooks in the power service's config (a pure function, taken from the source)

def power_hook_findings(power, state_file="/var/lib/radar-runner/user/sdr-state"):
    import ast
    with open(os.path.join(REPO, "setup", "check.py")) as f:
        tree = ast.parse(f.read())
    wanted = [node for node in tree.body
              if (isinstance(node, ast.FunctionDef) and node.name == "power_hook_findings")
              or (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "POWER_OFF_BUDGET_S")]
    assert len(wanted) == 2
    scope = {"os": os}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "check.py", "exec"), scope)
    return {name: (ok, observed) for ok, name, observed, _ in scope["power_hook_findings"](power, state_file)}


SETPRIV = ["/usr/bin/setpriv", "--reuid=englacial", "--regid=englacial", "--init-groups", "--reset-env"]
STOP_RADAR = SETPRIV + ["/usr/bin/python3", "radarctl.py", "stop", "--interrupt", "--wait", "--timeout", "4"]
HOOK = "/home/englacial/englacial-radar-runner/bin/radar-manager-stop-hook"
STATE = "/var/lib/radar-runner/user/sdr-state"


def test_check_accepts_the_hook_lines_that_setup_prints():
    found = power_hook_findings({"hooks": {"stop_radar": STOP_RADAR, "stop_radar_timeout_s": 5.0,
                                           "amp_safe": SETPRIV + [HOOK, "--timeout", "10", STATE],
                                           "amp_safe_timeout_s": 12.0}})
    assert all(ok for ok, _ in found.values()), found
    assert found["power-off sequence fits the capacitor"][1] == (
        "worst case: hooks 19 s, power-off command 37.2 s after the loss, last resort at 47.2 s")


def test_check_reports_hooks_that_are_not_set_and_the_time_without_them():
    found = power_hook_findings({})
    assert found["power service hook stop_radar"][0] is None and found["power service hook amp_safe"][0] is None
    assert found["power-off sequence fits the capacitor"] == (
        True, "worst case: hooks 0 s, power-off command 18.2 s after the loss, last resort at 28.2 s")


def test_check_finds_a_hook_that_reads_another_record_or_always_runs_the_tool_or_takes_too_long():
    hooks = {"stop_radar": STOP_RADAR, "stop_radar_timeout_s": 5.0,
             "amp_safe": SETPRIV + [HOOK, "--timeout", "10", "/var/lib/radar-runner/sdr-state"],
             "amp_safe_timeout_s": 12.0}
    found = power_hook_findings({"hooks": hooks})
    assert found["power service hook amp_safe reads the manager's record"] == (False, "/var/lib/radar-runner/sdr-state")
    # A state directory moved in the manager's config must be followed by the hook.
    hooks["amp_safe"] = SETPRIV + [HOOK, "--timeout", "10", STATE]
    assert not power_hook_findings({"hooks": hooks}, "/srv/state/sdr-state")[
        "power service hook amp_safe reads the manager's record"][0]
    # The hook script without a path uses its default, which is the default state directory.
    hooks["amp_safe"] = SETPRIV + [HOOK, "--timeout", "10"]
    assert power_hook_findings({"hooks": hooks})["power service hook amp_safe reads the manager's record"][0]
    # The tool called directly (the earlier form): the record is found, but it runs every time, and 22 s is too long.
    hooks.update(amp_safe=SETPRIV + ["/x/tools/sdr_gpio_safe/amp_safe", "--timeout", "20", "--state-file", STATE],
                 amp_safe_timeout_s=22.0)
    found = power_hook_findings({"hooks": hooks})
    assert found["power service hook amp_safe reads the manager's record"][0]
    assert found["power service hook amp_safe runs the tool only when needed"] == (False, "calls the tool every time")
    assert found["power-off sequence fits the capacitor"][0] is False
    assert "last resort at 57.2 s" in found["power-off sequence fits the capacitor"][1]
    # A limit that is not longer than the command's own timeout.
    hooks.update(amp_safe=SETPRIV + [HOOK, "--timeout", "10", STATE], amp_safe_timeout_s=10.0)
    assert power_hook_findings({"hooks": hooks})["power service hook amp_safe"][0] is False
    # The power service's own settings count.
    hooks["amp_safe_timeout_s"] = 12.0
    slow = power_hook_findings({"hooks": hooks, "shutdown": {"ride_through_s": 20.0}})
    assert slow["power-off sequence fits the capacitor"][0] is False
