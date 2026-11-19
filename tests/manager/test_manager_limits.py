"""Radar config limits (radar_runner/manager/limits.py) against real ORCA config files.

The files in fixtures/orca_configs are copies, unchanged:
    orca_default.yaml       ~/orca/config/default.yaml (numbers as 56e6, anchors, pwr_amp_pin "-1")
    run300_d05_soak.yaml    ~/radar-testbench/pa_bringup: the 2 h soak at 300 MHz, tx_gain 72, written by ruamel
    pa_soak.yaml            the 10 W soak at 100 MHz, tx_gain 52 (VVA 0 V)
    pa_1w.yaml              100 MHz, tx_gain 0
"""
import copy
import os

import pytest

from radar_runner.manager import limits as L
from radar_runner.manager.config import MANAGER_DEFAULTS

HERE = os.path.dirname(os.path.abspath(__file__))
LIMITS = MANAGER_DEFAULTS["limits"]


def fixture(name):
    with open(os.path.join(HERE, "fixtures", "orca_configs", name + ".yaml")) as f:
        return f.read()


def good():
    """The bench's 300 MHz config with the TX gain under the default cap."""
    config = L.load(fixture("run300_d05_soak"))
    config["RF0"]["tx_gain"] = 40.0
    return config


def problems(change, limits=LIMITS):
    config = good()
    change(config)
    return L.check(config, limits)


def setter(section, key, value):
    def change(config):
        config[section][key] = value
    return change


# --------------------------------------------------------------------- reading YAML

def test_orca_number_formats_are_read_as_numbers():
    config = L.load(fixture("orca_default"))
    assert config["GENERATE"]["sample_rate"] == 56e6 and type(config["GENERATE"]["sample_rate"]) is float
    assert config["GENERATE"]["chirp_length"] == 20e-6
    assert config["GENERATE"]["lo_offset_sw"] == 0.0
    assert config["RF0"]["freq"] == 450e6
    assert config["CHIRP"]["pulse_rep_int"] == 200e-6
    for text, value in (("56e6", 56e6), ("1e-05", 1e-5), ("-2.5E+3", -2500.0), ("0e6", 0.0), ("1.3e-05", 1.3e-5)):
        assert L.load(f"a: {text}")["a"] == value and type(L.load(f"a: {text}")["a"]) is float
    # Things that are not numbers stay text.
    for text in ("e6", "1e", "abc", "1f6", "0x1e6g"):
        assert type(L.load(f"a: {text}")["a"]) is str


def test_anchors_and_aliases_are_resolved():
    config = L.load(fixture("orca_default"))
    assert config["RF0"]["rx_rate"] == 56e6                 # *s_rate
    assert config["CHIRP"]["tx_duration"] == 20e-6          # *pulse_len
    assert config["FILES"]["chirp_loc"] == "chirp.bin"      # *ch_sent
    assert config["PLOT"]["rx_samps"] == "rx_samps.bin"     # *save_loc


def test_quoted_values_as_orca_writes_them():
    config = L.load(fixture("orca_default"))
    assert config["GPIO"]["pwr_amp_pin"] == "-1" and config["DEVICE"]["tx_channels"] == "0"
    assert L.load(fixture("run300_d05_soak"))["GPIO"]["pwr_amp_pin"] == "2"


@pytest.mark.parametrize("text, message", [
    ("RF0: {tx_gain: 40, tx_gain: 80}", "appears twice"),
    ("RF0:\n  tx_gain: 40\nRF0:\n  tx_gain: 80\n", "appears twice"),
    ("- a\n- b\n", "not a mapping"),
    ("a: [1, 2", "not valid YAML"),
    ("", "not a mapping"),
    ("a: !!python/object/apply:os.system ['true']", "not valid YAML"),
])
def test_unusable_files_are_refused(text, message):
    with pytest.raises(L.ConfigError, match=message):
        L.load(text)


def test_load_file_errors(tmp_path):
    with pytest.raises(L.ConfigError, match="cannot read"):
        L.load_file(str(tmp_path / "missing.yaml"))
    (tmp_path / "bin.yaml").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(L.ConfigError, match="not UTF-8"):
        L.load_file(str(tmp_path / "bin.yaml"))
    (tmp_path / "big.yaml").write_text("a: 1\n" * 300000)
    with pytest.raises(L.ConfigError, match="larger than"):
        L.load_file(str(tmp_path / "big.yaml"))


# ------------------------------------------------------------------ the real files

def test_orca_default_config_is_refused_for_every_reason_that_applies():
    found = L.check(L.load(fixture("orca_default")), LIMITS)
    assert found == [
        "GPIO.pwr_amp_pin is -1, must be 2 (-1 leaves the amplifier enable unmanaged)",
        "GPIO.pwr_amp_active_low must be true",
        "RF0.tx_gain 65 is above the cap of 43",
        "chirp band 437.5-462.5 MHz is outside the allowed 225-400 MHz",
    ]


def test_bench_soak_config_passes_except_for_the_conservative_gain_cap():
    config = L.load(fixture("run300_d05_soak"))
    assert L.check(config, LIMITS) == ["RF0.tx_gain 72 is above the cap of 43"]
    assert L.check(config, dict(LIMITS, max_tx_gain=72.0)) == []        # the per-unit value for VVA 5 V


def test_100_mhz_bench_configs_are_outside_the_band():
    assert L.check(L.load(fixture("pa_soak")), LIMITS) == [
        "RF0.tx_gain 52 is above the cap of 43",
        "chirp band 87.5-112.5 MHz is outside the allowed 225-400 MHz"]
    assert L.check(L.load(fixture("pa_1w")), LIMITS) == [
        "chirp band 87.5-112.5 MHz is outside the allowed 225-400 MHz"]


def test_good_config_passes():
    assert L.check(good(), LIMITS) == []


# ------------------------------------------------------------------- each limit

@pytest.mark.parametrize("value", ["-1", -1, "3", 0, "2.5", "two", None, True])
def test_amplifier_pin_must_be_the_bench_pin(value):
    found = problems(setter("GPIO", "pwr_amp_pin", value))
    assert len(found) == 1 and "GPIO.pwr_amp_pin" in found[0]


@pytest.mark.parametrize("value", ["2", 2, 2.0])
def test_amplifier_pin_forms_that_the_radar_reads_as_2(value):
    assert problems(setter("GPIO", "pwr_amp_pin", value)) == []


@pytest.mark.parametrize("value, ok", [(True, True), ("true", True), ("yes", True), ("on", True),
                                       (False, False), ("false", False), ("no", False), (1, False), ("1", False)])
def test_amplifier_enable_must_be_active_low(value, ok):
    assert (problems(setter("GPIO", "pwr_amp_active_low", value)) == []) is ok


def test_missing_active_low_means_false_in_orca_and_is_refused():
    assert problems(lambda c: c["GPIO"].pop("pwr_amp_active_low")) == ["GPIO.pwr_amp_active_low must be true"]


def test_gpio_bank():
    assert problems(setter("GPIO", "gpio_bank", "RXA")) == ["GPIO.gpio_bank is RXA, must be FP0"]


@pytest.mark.parametrize("value, ok", [(43, True), (43.0, True), ("43", True), (43.5, False), ("44", False),
                                       (76, False), ("4e1", True), ("5e1", False), (-1, False),
                                       ("high", False), (None, False), (float("nan"), False), (float("inf"), False),
                                       ([40], False), (True, False)])
def test_tx_gain_cap(value, ok):
    assert (problems(setter("RF0", "tx_gain", value)) == []) is ok


def test_tx_gain_cap_is_a_setting():
    assert problems(setter("RF0", "tx_gain", 52), dict(LIMITS, max_tx_gain=52.0)) == []
    assert problems(setter("RF0", "tx_gain", 52.1), dict(LIMITS, max_tx_gain=52.0)) != []


@pytest.mark.parametrize("tx, pri, ok", [(13e-6, 200e-6, True), (30e-6, 200e-6, True), (30.1e-6, 200e-6, False),
                                         (13e-6, 80e-6, False), (13e-6, 0, False), (0, 200e-6, False)])
def test_enable_duty_cap(tx, pri, ok):
    def change(config):
        config["CHIRP"].update(tx_duration=tx, pulse_rep_int=pri, rx_duration=max(tx, 42e-6))
    found = problems(change)
    assert (found == []) is ok
    if not ok:
        assert "duty" in found[0] or "positive" in found[0]


def test_receive_window_must_cover_the_transmit_pulse():
    found = problems(setter("CHIRP", "rx_duration", 12e-6))
    assert len(found) == 1 and "rx_duration 12 us is shorter than CHIRP.tx_duration 13 us" in found[0]
    assert problems(setter("CHIRP", "rx_duration", 13e-6)) == []
    assert "tx_lead" in problems(setter("CHIRP", "tx_lead", 13e-6))[0]
    assert "tx_lead" in problems(setter("CHIRP", "tx_lead", -1e-6))[0]


@pytest.mark.parametrize("freq, offset, width, ok", [
    (290e6, 10e6, 30e6, True),          # 285-315 MHz, as on the bench
    (240e6, 0, 30e6, True),             # 225-255: on the edge
    (239e6, 0, 30e6, False),
    (385e6, 0, 30e6, True),             # 370-400
    (386e6, 0, 30e6, False),
    (290e6, 100e6, 30e6, False),        # the software offset moves the chirp out of the band
    (450e6, 0, 25e6, False),
    (300e6, 0, 0, False),
])
def test_chirp_band_must_lie_inside_the_allowed_band(freq, offset, width, ok):
    def change(config):
        config["RF0"]["freq"] = freq
        config["GENERATE"].update(lo_offset_sw=offset, chirp_bandwidth=width)
    assert (problems(change) == []) is ok


@pytest.mark.parametrize("value, ok", [(True, True), ("true", True), (False, False), ("false", False), ("maybe", False)])
def test_transmit_must_be_enabled(value, ok):
    assert (problems(setter("RF0", "transmit", value)) == []) is ok


def test_transmit_defaults_to_true_as_in_orca():
    assert problems(lambda c: c["RF0"].pop("transmit")) == []


def test_gpsdo_clock_is_refused_because_orca_would_open_the_gps():
    found = problems(setter("DEVICE", "clk_ref", "gpsdo"))
    assert len(found) == 1 and "gpsdo makes ORCA open and reconfigure the GPS receiver" in found[0]
    assert problems(setter("DEVICE", "clk_ref", "external")) == []


def test_only_one_channel_and_fc32():
    assert "tx_channels" in problems(setter("DEVICE", "tx_channels", "0,1"))[0]
    assert "cpu_format" in problems(setter("DEVICE", "cpu_format", "sc16"))[0]


def test_pulse_counts():
    assert problems(setter("CHIRP", "num_pulses", 0)) != []
    assert problems(setter("CHIRP", "num_pulses", -2)) != []
    assert problems(setter("CHIRP", "num_pulses", 10000)) == []
    assert problems(setter("CHIRP", "num_presums", 0)) != []


@pytest.mark.parametrize("section", ["GPIO", "RF0", "CHIRP", "GENERATE", "DEVICE", "FILES"])
def test_missing_section_is_refused(section):
    assert problems(lambda c: c.pop(section)) != []


def test_every_problem_is_reported_not_only_the_first():
    def change(config):
        config["RF0"].update(tx_gain=80, transmit=False)
        config["GPIO"]["pwr_amp_pin"] = "-1"
        config["CHIRP"]["rx_duration"] = 1e-6
    assert len(problems(change)) == 4


# ------------------------------------------------------------ the derived config

def test_derived_config_points_the_output_at_the_collection_and_chunks_it():
    config = L.load(fixture("orca_default"))
    derived = L.derive(config, "../../mnt/nvme/radar-runner/collections/b00001_s001_x", 5000)
    assert derived["FILES"] == {"chirp_loc": "chirp.bin", "output_dir": "../../mnt/nvme/radar-runner/collections/b00001_s001_x",
                                "save_loc": "rx_samps.bin", "gps_loc": "gps_log.txt", "max_chirps_per_file": 5000}
    assert derived["GENERATE"]["out_file"] == "chirp.bin" and derived["GENERATE"]["show_plot"] is False
    assert config["FILES"]["output_dir"] == "data"          # the input is not changed
    # Everything else is the same.
    for section in config:
        if section not in ("FILES", "GENERATE", "GPIO", "RF0", "CHIRP"):
            assert derived[section] == config[section]


def test_derived_config_holds_the_checked_values_in_canonical_form():
    config = good()
    config["RF0"]["tx_gain"] = "4e1"            # text that the radar would read as 40
    config["GPIO"]["pwr_amp_pin"] = "2"
    config["GPIO"]["pwr_amp_active_low"] = "yes"
    derived = L.derive(config, "x", 1000)
    assert derived["RF0"]["tx_gain"] == 40.0 and type(derived["RF0"]["tx_gain"]) is float
    assert derived["GPIO"]["pwr_amp_pin"] == 2 and derived["GPIO"]["pwr_amp_active_low"] is True
    text = L.dump(derived)
    assert "tx_gain: 40.0" in text and "pwr_amp_pin: 2\n" in text and "pwr_amp_active_low: true" in text
    assert L.load(text) == derived              # reads back as written
    assert L.check(L.load(text), LIMITS) == []


def test_dump_writes_exponents_that_every_reader_takes_as_numbers():
    text = L.dump(L.derive(L.load(fixture("orca_default")), "x", 1000))
    assert "&" not in text and "*" not in text              # no anchors
    assert "chirp_length: 2.0e-05" in text and "sample_rate: 56000000.0" in text
    import yaml
    plain = yaml.safe_load(text)                # also plain YAML 1.1 readers get numbers
    assert plain["GENERATE"]["chirp_length"] == 2e-5 and plain["CHIRP"]["pulse_rep_int"] == 2e-4


def test_derive_refuses_what_it_cannot_read():
    config = good()
    config["RF0"]["tx_gain"] = "loud"
    with pytest.raises(L.ConfigError, match="RF0.tx_gain is not a number"):
        L.derive(config, "x", 1000)


def test_output_check(tmp_path):
    orca = tmp_path / "home" / "orca"
    root = tmp_path / "nvme" / "collections"
    (root / "c1").mkdir(parents=True)
    orca.mkdir(parents=True)
    rel = os.path.relpath(str(root / "c1"), str(orca))
    derived = L.derive(good(), rel, 1000)
    assert L.check_output(derived, str(orca), str(root)) == []
    for change, text in (
            (setter("FILES", "output_dir", "data"), "not below the data root"),
            (setter("FILES", "output_dir", os.path.relpath(str(root), str(orca))), "not below the data root"),
            (setter("FILES", "output_dir", rel + "/../../elsewhere"), "not below the data root"),
            (setter("FILES", "save_loc", "../rx_samps.bin"), "FILES.save_loc must be a plain file name"),
            (setter("FILES", "chirp_loc", "other.bin"), "would not transmit the chirp that was generated"),
            (setter("FILES", "max_chirps_per_file", -1), "written in chunks")):
        bad = copy.deepcopy(derived)
        change(bad)
        found = L.check_output(bad, str(orca), str(root))
        assert found and text in found[0]


def test_output_check_follows_symlinks(tmp_path):
    orca, root, sd = tmp_path / "orca", tmp_path / "nvme", tmp_path / "sdcard"
    for d in (orca, root, sd):
        d.mkdir()
    (root / "sneaky").symlink_to(sd)            # a link out of the data drive
    derived = L.derive(good(), os.path.relpath(str(root / "sneaky"), str(orca)), 1000)
    assert "not below the data root" in L.check_output(derived, str(orca), str(root))[0]


# ----------------------------------------------------------------------- chunks

def test_record_size_follows_the_radar():
    assert L.record_bytes(L.load(fixture("run300_d05_soak"))) == 2352 * 8      # "Number of RX samples: 2352"
    assert L.record_bytes(L.load(fixture("orca_default"))) == 1120 * 8


@pytest.mark.parametrize("presums, chunk_mb, max_s, expected", [
    (1000, 128, 300, 1_500_000),        # 300 s / 0.2 s per record = 1500 records (28 MB): the time limit
    (1000, 10, 300, 531_000),           # 10 MB / 18816 B = 531 records: the size limit
    (10, 128, 300, 68_020),             # 128 MB / 18816 B = 6802 records, 13.6 s
    (1, 128, 300, 6_802),
    (1000, 128, 0.01, 1_000),           # never less than one record
])
def test_pulses_per_file(presums, chunk_mb, max_s, expected):
    config = L.load(fixture("run300_d05_soak"))
    config["CHIRP"]["num_presums"] = presums
    value = L.pulses_per_file(config, chunk_mb * 1e6, max_s)
    assert value == expected and value % presums == 0


def test_default_chunk_is_a_few_hundred_megabytes_at_most():
    config = L.load(fixture("run300_d05_soak"))
    for presums in (1, 10, 100, 1000):
        config["CHIRP"]["num_presums"] = presums
        n = L.pulses_per_file(config, MANAGER_DEFAULTS["data"]["chunk_mb"] * 1e6, MANAGER_DEFAULTS["data"]["chunk_max_s"])
        size = n / presums * L.record_bytes(config)
        seconds = n * 200e-6
        assert size <= 128e6 and seconds <= 300.0
