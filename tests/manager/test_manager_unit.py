"""The manager's systemd unit and the two commands in bin/. Nothing is installed or started."""
import os
import re
import shutil
import stat
import subprocess

import pytest

from radar_runner.common import config as common_config

REPO = common_config.REPO
UNIT = os.path.join(REPO, "systemd", "radar-manager.service")
INSTALLED_REPO = "/home/englacial/englacial-radar-runner"


def settings():
    """The unit's settings as {name: [values]} (a name can appear more than once)."""
    out = {}
    with open(UNIT) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith(("#", "[")) and "=" in line:
                name, _, value = line.partition("=")
                out.setdefault(name, []).append(value)
    return out


def test_unit_runs_the_manager_as_the_service_user_with_the_repository_environment():
    s = settings()
    assert s["User"] == ["englacial"] and s["Type"] == ["notify"] and s["NotifyAccess"] == ["main"]
    assert s["WorkingDirectory"] == [INSTALLED_REPO]
    assert s["ExecStart"] == [f"{INSTALLED_REPO}/.venv/bin/python -B -u -m radar_runner.manager"]
    assert s["Restart"] == ["always"] and s["StartLimitIntervalSec"] == ["0"]
    assert s["WatchdogSec"] == ["30"]
    assert s["WantedBy"] == ["multi-user.target"]           # used only once the user enables it by hand


def test_unit_gives_the_radar_real_time_priority_as_a_login_shell_has():
    assert settings()["LimitRTPRIO"] == ["99"]


def test_unit_never_leaves_the_radar_without_the_manager():
    s = settings()
    # Only the manager is signalled at a stop; it stops the radar itself, and the reset tool is
    # never interrupted half way. What is left after the time limit is killed.
    assert s["KillMode"] == ["mixed"] and "KillSignal" not in s
    assert s["ExecStopPost"] == [f"{INSTALLED_REPO}/bin/radar-manager-stop-hook"]
    # ... and the radar itself is started so that the kernel kills it if the manager dies.
    assert common_config.DEFAULTS["manager"]["orca"]["launch_prefix"] == ["/usr/bin/setpriv", "--pdeathsig", "KILL"]


def test_unit_stop_time_limit_covers_the_longest_clean_stop():
    m = common_config.DEFAULTS["manager"]
    longest = (m["orca"]["urgent_stop_budget_s"] + m["amp_safe"]["confirm_timeout_s"]
               + m["amp_safe"]["timeout_s"] * (1 + m["amp_safe"]["assume_enabled_retries"]) + 15.0)
    assert float(settings()["TimeoutStopSec"][0]) > longest


def test_unit_does_not_expand_shell_variables():
    # systemd replaces $NAME in command lines itself: a hook written as a shell one-liner
    # with variables would not do what it says. The hook is a script.
    for name in ("ExecStart", "ExecStopPost"):
        assert "$" not in settings()[name][0]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze is not installed")
def test_systemd_accepts_the_unit(tmp_path):
    # The unit names files in the installed checkout. Point them at this checkout, where they exist
    # whether or not it is the installed one, and let systemd check the rest.
    with open(UNIT) as f:
        text = f.read()
    python = shutil.which("python3")
    text = text.replace(f"{INSTALLED_REPO}/.venv/bin/python", python).replace(INSTALLED_REPO, REPO)
    copy = tmp_path / "radar-manager.service"
    copy.write_text(text)
    r = subprocess.run(["systemd-analyze", "verify", str(copy)], capture_output=True, text=True, timeout=60)
    about_this_unit = [line for line in (r.stdout + r.stderr).splitlines() if "radar-manager.service" in line]
    assert about_this_unit == [], about_this_unit
    assert r.returncode == 0, r.stderr


def test_commands_in_bin_are_executable_and_plain_shell():
    for name in ("radarctl", "radar-manager-stop-hook"):
        path = os.path.join(REPO, "bin", name)
        assert os.stat(path).st_mode & stat.S_IXUSR, name
        with open(path) as f:
            assert f.readline().strip() == "#!/bin/sh"
        assert subprocess.run(["/bin/sh", "-n", path]).returncode == 0     # syntax only; nothing is run


def test_stop_hook_calls_the_tool_with_its_mandatory_timeout_and_the_state_file():
    with open(os.path.join(REPO, "bin", "radar-manager-stop-hook")) as f:
        text = f.read()
    call = re.search(r'^exec "\$tool" (.*)$', text, re.M).group(1)
    assert call == '--timeout 30 --state-file "$state"'
    assert "/var/lib/radar-runner/user/sdr-state" in text and "tools/sdr_gpio_safe/amp_safe" in text
    assert os.path.exists(os.path.join(REPO, "tools", "sdr_gpio_safe", "amp_safe"))
    # The same state file as the manager uses.
    from radar_runner.manager import ampsafe
    assert ampsafe.SdrState(common_config.DEFAULTS["paths"]["state_dir"]).path == "/var/lib/radar-runner/user/sdr-state"


def test_real_tool_accepts_the_options_the_manager_and_the_hook_pass():
    # Read from the real wrapper's source: it is never run here.
    with open(os.path.join(REPO, "tools", "sdr_gpio_safe", "amp_safe")) as f:
        source = f.read()
    assert '"--timeout"' in source and '"--state-file"' in source
    command = common_config.DEFAULTS["manager"]["amp_safe"]["command"]
    assert command == [f"{INSTALLED_REPO}/tools/sdr_gpio_safe/amp_safe", "--timeout", "30"]
    assert common_config.DEFAULTS["manager"]["amp_safe"]["timeout_s"] > 30
