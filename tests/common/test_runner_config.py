"""The YAML config layers (radar_runner/common/config.py)."""
import copy
import os

import pytest
import yaml

from radar_runner.common import config as C


def write(path, text):
    path.write_text(text)
    return str(path)


def load(tmp_path, repo=None, local=None, secrets=None, mode=0o640, **kw):
    """Load with the given text for each layer; None means the file does not exist."""
    paths = {}
    for name, text in (("repo", repo), ("local", local), ("secrets", secrets)):
        path = tmp_path / f"{name}.yaml"
        if text is not None:
            path.write_text(text)
            if name == "secrets":
                path.chmod(mode)
        paths[name] = str(path)
    return C.load(paths["repo"], paths["local"], paths["secrets"], **kw)


def test_example_file_matches_the_built_in_defaults():
    # config/radar-runner.yaml documents every setting with its default.
    with open(C.REPO_FILE) as f:
        assert yaml.safe_load(f) == C.DEFAULTS
    config, warnings = C.load(local_file=None, secrets_file=None)
    assert config == C.DEFAULTS and warnings == []


def test_stage_0_settings():
    # Services run as englacial write only inside the "user" directories; the
    # parents belong to root and the power service.
    assert C.DEFAULTS["paths"]["run_dir"] == "/run/radar-runner/user"
    assert C.DEFAULTS["paths"]["state_dir"] == "/var/lib/radar-runner/user"
    assert C.DEFAULTS["paths"]["power_status_file"] == "/run/radar-runner/power.json"
    assert C.LOCAL_FILE == "/etc/radar-runner/local.yaml"
    assert C.SECRETS_FILE == "/etc/radar-runner/secrets.yaml"


def test_missing_repo_file_gives_defaults_and_a_warning(tmp_path):
    config, warnings = load(tmp_path)
    assert config == C.DEFAULTS
    assert len(warnings) == 1 and "repo.yaml: not found" in warnings[0]    # the other two are optional


def test_layers_override_in_order(tmp_path):
    defaults = {"a": {"x": 1, "y": 1, "z": 1, "w": 1}}
    config, warnings = load(tmp_path, defaults=defaults,
                            repo="a: {y: 2, z: 2, w: 2}", local="a: {z: 3, w: 3}", secrets="a: {w: 4}")
    assert config == {"a": {"x": 1, "y": 2, "z": 3, "w": 4}} and warnings == []


def test_load_does_not_change_the_defaults(tmp_path):
    before = copy.deepcopy(C.DEFAULTS)
    config, _ = load(tmp_path, repo="paths: {run_dir: /tmp/x}")
    assert config["paths"]["run_dir"] == "/tmp/x"
    config["paths"]["state_dir"] = "changed"
    assert C.DEFAULTS == before


def test_unknown_settings_are_ignored_with_a_warning(tmp_path):
    config, warnings = load(tmp_path, repo="paths: {run_dir: /a, colour: red}\nnonsense: {x: 1}\n7: x\n")
    assert config["paths"]["run_dir"] == "/a" and "colour" not in config["paths"]
    assert "nonsense" not in config
    assert [w.split(": ", 1)[1] for w in warnings] == [
        "unknown setting paths.colour: ignored", "unknown setting nonsense: ignored",
        "unknown setting 7: ignored"]


@pytest.mark.parametrize("text, expected", [
    ("status: {stale_after_s: fast}", "status.stale_after_s must be a number, not text"),
    ("status: {stale_after_s: true}", "status.stale_after_s must be a number, not true or false"),
    ("status: {stale_after_s: .nan}", "status.stale_after_s must be a number, not an infinite or undefined number"),
    ("status: {stale_after_s: .inf}", "status.stale_after_s must be a number, not an infinite or undefined number"),
    ("status: {stale_after_s: [1]}", "status.stale_after_s must be a number, not a list"),
    ("status: {stale_after_s: }", "status.stale_after_s must be a number, not nothing"),
    ("paths: {run_dir: 5}", "paths.run_dir must be text, not a whole number"),
    ("paths: {run_dir: ''}", "paths.run_dir must be text, not text"),
    ("paths: {run_dir: {a: 1}}", "paths.run_dir must be text, not a section"),
    ("paths: /run", "paths must be a section, not text"),
    ("logging: {level: no}", "logging.level must be text, not true or false"),
])
def test_wrong_types_keep_the_earlier_value(tmp_path, text, expected):
    config, warnings = load(tmp_path, repo=text)
    assert config == C.DEFAULTS
    assert len(warnings) == 1 and warnings[0].endswith(expected + ": ignored")


def test_other_types(tmp_path):
    defaults = {"s": {"flag": False, "count": 3, "ratio": 0.5, "items": ["a"]}}
    config, warnings = load(tmp_path, defaults=defaults,
                            repo="s: {flag: true, count: 4, ratio: 2, items: [b, c]}")
    assert config == {"s": {"flag": True, "count": 4, "ratio": 2.0, "items": ["b", "c"]}}
    assert type(config["s"]["ratio"]) is float and warnings == []
    config, warnings = load(tmp_path, defaults=defaults,
                            repo="s: {flag: 1, count: 4.5, ratio: '2', items: b}")
    assert config == defaults and len(warnings) == 4
    config, warnings = load(tmp_path, defaults=defaults, repo="s: {count: true}")
    assert config == defaults and len(warnings) == 1


def test_empty_receiver_command_switches_it_off(tmp_path):
    config, warnings = load(tmp_path, repo="", local="telemetry: {gps: {receiver_fix_command: ''}}")
    assert config["telemetry"]["gps"]["receiver_fix_command"] == "" and warnings == []
    config, warnings = load(tmp_path, repo="", local="paths: {run_dir: ''}")      # elsewhere "" is still refused
    assert config["paths"] == C.DEFAULTS["paths"] and len(warnings) == 1


def test_receiver_command_intervals_have_a_floor(tmp_path):
    config, warnings = load(tmp_path, repo="", local="telemetry: {gps: {no_position_retry_s: 0, "
                                                     "no_position_retry_max_s: 5, receiver_fix_timeout_s: 1, "
                                                     "no_position_warn_s: 0}}")
    gps = config["telemetry"]["gps"]
    assert (gps["no_position_retry_s"], gps["no_position_retry_max_s"], gps["receiver_fix_timeout_s"]) == (30.0, 30.0, 30.0)
    assert gps["no_position_warn_s"] == 0.0 and len(warnings) == 3
    assert warnings[0].endswith("telemetry.gps.no_position_retry_s must be at least 30: 30 used")


def test_a_bad_value_in_a_later_layer_keeps_the_earlier_layer(tmp_path):
    config, warnings = load(tmp_path, repo="status: {stale_after_s: 9}", local="status: {stale_after_s: soon}")
    assert config["status"]["stale_after_s"] == 9.0 and len(warnings) == 1
    assert "local.yaml" in warnings[0]


@pytest.mark.parametrize("text", ["paths: {run_dir: /a", "paths:\n\trun_dir: /a", "- a\n- b", "just text",
                                  "42", "? [a, b]\n: c", "!!python/object/apply:os.system ['true']"])
def test_broken_file_is_skipped_as_a_whole(tmp_path, text):
    config, warnings = load(tmp_path, repo="status: {stale_after_s: 9}", local=text)
    assert config["status"]["stale_after_s"] == 9.0 and config["paths"] == C.DEFAULTS["paths"]
    assert len(warnings) == 1 and "local.yaml" in warnings[0] and "skipped" in warnings[0]


def test_empty_file_and_empty_section_are_fine(tmp_path):
    config, warnings = load(tmp_path, repo="", local="# only a comment\n", secrets="paths:\n")
    assert config == C.DEFAULTS and warnings == []


def test_binary_file_is_skipped(tmp_path):
    (tmp_path / "local.yaml").write_bytes(b"\xff\xfe\x00\x01paths")
    config, warnings = C.load(str(tmp_path / "none.yaml"), str(tmp_path / "local.yaml"), None)
    assert config == C.DEFAULTS and len(warnings) == 2 and "not valid YAML" in warnings[1]


def test_unreadable_file_is_skipped(tmp_path):
    config, warnings = C.load(str(tmp_path), None, None)           # a directory
    assert config == C.DEFAULTS and "cannot read" in warnings[0]
    if os.geteuid() != 0:
        config, warnings = load(tmp_path, repo="", secrets="paths: {run_dir: /s}", mode=0)
        assert config == C.DEFAULTS and len(warnings) == 1 and "Permission denied" in warnings[0]


def test_secrets_file_readable_by_everyone_is_reported(tmp_path):
    config, warnings = load(tmp_path, repo="", secrets="paths: {run_dir: /s}", mode=0o644)
    assert config["paths"]["run_dir"] == "/s"                      # still used
    assert len(warnings) == 1 and "readable by every user" in warnings[0]
    for mode in (0o640, 0o600):
        assert load(tmp_path, repo="", secrets="paths: {run_dir: /s}", mode=mode)[1] == []


def test_warnings_never_contain_values(tmp_path):
    defaults = {"alerts": {"slack_webhook": "", "retries": 3}}
    _, warnings = load(tmp_path, defaults=defaults, repo="",
                       secrets="alerts: {slack_webhook: [hunter2-TOKEN], retries: hunter2-TOKEN, "
                               "hunter2-KEY: 1}\n")
    assert len(warnings) == 3
    assert not any("hunter2-TOKEN" in w for w in warnings)


def test_command_line_shows_the_merged_config():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-B", "-m", "radar_runner.common.config"],
                         capture_output=True, text=True, timeout=60, cwd=C.REPO)
    assert out.returncode == 0, out.stderr
    shown = yaml.safe_load(out.stdout.split("WARNING:")[0])     # /etc may override values here
    assert {k: sorted(v) for k, v in shown.items()} == {k: sorted(v) for k, v in C.DEFAULTS.items()}
