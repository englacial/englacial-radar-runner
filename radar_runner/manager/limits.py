"""The fixed limits every radar config must pass, and the per-collection config.

    config = load(text)                         # an ORCA YAML file as a dict
    problems = check(config, limits)            # [] = may run
    derived = derive(config, output_dir, max_chirps_per_file)
    text = dump(derived)                        # what the radar is given

How ORCA's YAML is read here
  * ORCA's own files write numbers as `56e6` and `20e-6`. PyYAML follows YAML
    1.1, where a float needs a dot, and would read these as text; yaml-cpp (the
    radar) and ruamel (the chirp generator) read them as numbers. `load` reads
    them as numbers too.
  * Anchors and aliases (`&s_rate`, `*s_rate`) are resolved by the loader;
    `dump` writes plain values, never an anchor.
  * A key that appears twice in one section is refused: yaml-cpp uses the
    first, PyYAML the last, so the check and the radar could disagree.
  * `derive` writes every checked value back as a plain number or true/false,
    and the manager checks the derived text once more after writing it, so
    the limits apply to the file that the radar reads, not to a file that only
    means the same.

The limits come from the bench (`~/radar-testbench/pa_bringup/pa_run.py`
`check_config`, the HIL fixtures and STATUS.md); the numbers are in
config.py under "limits".

Which field ORCA really uses (checked against sdr/sdr.cpp, sdr/rf_settings.cpp,
sdr/main.cpp and preprocessing/generate_chirp.py at 6fd9448). The checks follow
this arithmetic, not the names of the fields:

  * The hardware is set from RF0: rx_rate, tx_rate, freq, lo_offset, gains,
    bw, antennas (`set_rf_params_single`), and RF0.transmit.
  * The length of every burst is computed from **RF1**:
        num_tx_samps = size_t(RF1.tx_rate * CHIRP.tx_duration)
        num_rx_samps = size_t(RF1.rx_rate * CHIRP.rx_duration)
    (the Sdr class reads its rates from RF1; main.cpp sizes the buffers from
    them). On the air the transmit burst therefore lasts
    num_tx_samps / RF0.tx_rate, and the receive window num_rx_samps /
    RF0.rx_rate. A record in the data file is num_rx_samps complex float32.
  * The chirp file is generated at GENERATE.sample_rate and played at
    RF0.tx_rate: its bandwidth and offset on the air are scaled by
    RF0.tx_rate / GENERATE.sample_rate. The radar sends the first num_tx_samps
    samples of the file: a file longer than that is cut off.
  * The amplifier is enabled while transmit and receive are both active.

  * ORCA sets the SDR's master clock to DEVICE.clk_rate. The SDR can only
    run at the clock divided by a whole number, and UHD quietly picks the
    nearest such rate; ORCA prints "[WARNING]: Requested TX rate ... Actual
    ..." and carries on, with every burst and the chirp's band scaled by
    requested / actual. So the clock must be one the SDR has, and every rate
    the clock divided by a whole number (the runner also treats that warning
    line as a failed start).
  * The mixer is tuned to RF0.freq + RF0.lo_offset; its leakage is on the air
    there, and the chirp must lie within half the clock rate of it.

So the three rates must be equal (RF1 = RF0, GENERATE.sample_rate =
RF0.tx_rate), and the chirp must fit in tx_duration; the duty, window and band
checks are computed from the real burst lengths all the same.
"""
import copy
import math
import os
import re

import yaml


# The USRP B2xx: range of the master clock, and the largest whole-number divider
# from the clock to a sample rate up to which every value is supported.
MIN_CLK_RATE_HZ, MAX_CLK_RATE_HZ, MAX_RATE_DIVIDER = 5.0e6, 61.44e6, 128


class ConfigError(Exception):
    """The radar config cannot be read or is not a mapping of sections."""


class _Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            try:
                if key in seen:
                    raise ConfigError(f"key {key!r} appears twice (line {key_node.start_mark.line + 1})")
                seen.add(key)
            except TypeError:
                raise ConfigError(f"unusable key at line {key_node.start_mark.line + 1}") from None
        return super().construct_mapping(node, deep)


# Floats as YAML 1.2 (and ORCA) write them: the dot is optional when there is an exponent.
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:[0-9][0-9_]*)(?:\.[0-9_]*)?[eE][-+]?[0-9]+$"),
    list("-+0123456789"))


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def load(text):
    """Parse an ORCA config. Raises ConfigError."""
    try:
        data = yaml.load(text, Loader=_Loader)
    except ConfigError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, UnicodeDecodeError) as e:
        problem = getattr(e, "problem", None) or type(e).__name__
        mark = getattr(e, "problem_mark", None)
        raise ConfigError(f"not valid YAML ({problem}" + (f", line {mark.line + 1})" if mark else ")")) from None
    if not isinstance(data, dict):
        raise ConfigError("the top level is not a mapping of sections")
    return data


def load_file(path, max_bytes=1 << 20):
    try:
        with open(path, "rb") as f:
            raw = f.read(max_bytes + 1)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    if len(raw) > max_bytes:
        raise ConfigError(f"{path} is larger than {max_bytes} bytes")
    try:
        return load(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ConfigError(f"{path} is not UTF-8 text") from None


def dump(config):
    return yaml.dump(config, Dumper=_Dumper, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ------------------------------------------------------------------ value readers

class _Bad(Exception):
    pass


def _get(config, section, key):
    sec = config.get(section)
    if not isinstance(sec, dict):
        raise _Bad(f"section {section} is missing")
    if key not in sec:
        raise _Bad(f"{section}.{key} is missing")
    return sec[key]


def _number(config, section, key):
    """A finite number. Text is accepted if it is a plain decimal number, as yaml-cpp reads it."""
    value = _get(config, section, key)
    if type(value) in (int, float):
        out = float(value)
    elif isinstance(value, str) and re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", value):
        out = float(value)
    else:
        raise _Bad(f"{section}.{key} is not a number")
    if not math.isfinite(out):
        raise _Bad(f"{section}.{key} is not a finite number")
    return out


def _integer(config, section, key):
    value = _number(config, section, key)
    if value != int(value):
        raise _Bad(f"{section}.{key} is not a whole number")
    return int(value)


_TRUE, _FALSE = ("y", "yes", "true", "on"), ("n", "no", "false", "off")     # as yaml-cpp reads them


def _boolean(config, section, key, default=None):
    sec = config.get(section)
    if default is not None and isinstance(sec, dict) and key not in sec:
        return default
    value = _get(config, section, key)
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower() in _TRUE + _FALSE:
        return value.lower() in _TRUE
    raise _Bad(f"{section}.{key} is not true or false")


def _text(config, section, key):
    value = _get(config, section, key)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _Bad(f"{section}.{key} is not a plain value")
    return str(value)


# ------------------------------------------------------------------------- limits

# (section, key, reader): every value that a limit depends on. `derive` writes
# each back in canonical form.
_CHECKED = (
    ("GPIO", "pwr_amp_pin", _integer), ("GPIO", "pwr_amp_active_low", _boolean),
    ("RF0", "tx_gain", _number), ("RF0", "freq", _number), ("RF0", "transmit", _boolean),
    ("RF0", "tx_rate", _number), ("RF0", "rx_rate", _number),
    ("RF1", "tx_rate", _number), ("RF1", "rx_rate", _number),
    ("DEVICE", "clk_rate", _number),
    ("GENERATE", "sample_rate", _number), ("GENERATE", "chirp_length", _number),
    ("GENERATE", "chirp_bandwidth", _number), ("GENERATE", "lo_offset_sw", _number),
    ("CHIRP", "tx_duration", _number), ("CHIRP", "rx_duration", _number),
    ("CHIRP", "pulse_rep_int", _number), ("CHIRP", "tx_lead", _number),
)


def burst(config):
    """The real burst lengths, as ORCA computes them. Returns a dict; raises _Bad.

    tx_samples, rx_samples   samples per pulse (sized from RF1's rates)
    tx_s, rx_s               their duration on the air (clocked at RF0's rates)
    """
    rf0_tx, rf0_rx = _number(config, "RF0", "tx_rate"), _number(config, "RF0", "rx_rate")
    rf1_tx, rf1_rx = _number(config, "RF1", "tx_rate"), _number(config, "RF1", "rx_rate")
    tx, rx = _number(config, "CHIRP", "tx_duration"), _number(config, "CHIRP", "rx_duration")
    if min(rf0_tx, rf0_rx, rf1_tx, rf1_rx) <= 0:
        raise _Bad("the sample rates in RF0 and RF1 must be positive")
    if tx <= 0 or rx <= 0:
        raise _Bad("CHIRP.tx_duration and CHIRP.rx_duration must be positive")
    tx_samples, rx_samples = int(rf1_tx * tx), int(rf1_rx * rx)     # size_t(double * double), as in main.cpp
    return {"tx_samples": tx_samples, "rx_samples": rx_samples,
            "tx_s": tx_samples / rf0_tx, "rx_s": rx_samples / rf0_rx}


def check(config, limits):
    """Return the list of problems that forbid running this config. [] = allowed.

    Every limit is evaluated, so the list names everything that is wrong.
    A value that cannot be read counts as a problem (fail closed).
    """
    problems = []

    def rule(fn):
        try:
            message = fn()
        except _Bad as e:
            message = str(e)
        if message:
            problems.append(message)

    def amp_pin():
        pin = _integer(config, "GPIO", "pwr_amp_pin")
        if pin != limits["amp_pin"]:
            return (f"GPIO.pwr_amp_pin is {pin}, must be {limits['amp_pin']}"
                    + (" (-1 leaves the amplifier enable unmanaged)" if pin < 2 else ""))

    def amp_low():
        if _boolean(config, "GPIO", "pwr_amp_active_low", default=False) != limits["amp_active_low"]:
            return f"GPIO.pwr_amp_active_low must be {str(limits['amp_active_low']).lower()}"

    def bank():
        value = _text(config, "GPIO", "gpio_bank")
        if value != limits["gpio_bank"]:
            return f"GPIO.gpio_bank is {value}, must be {limits['gpio_bank']}"

    def gain():
        value = _number(config, "RF0", "tx_gain")
        if value > limits["max_tx_gain"]:
            return f"RF0.tx_gain {value:g} is above the cap of {limits['max_tx_gain']:g}"
        if value < 0:
            return f"RF0.tx_gain {value:g} is negative"

    def channels():
        for key in ("tx_channels", "rx_channels"):
            if _text(config, "DEVICE", key).strip() != "0":
                return f"DEVICE.{key} must be \"0\" (one channel; only RF0 is checked)"

    def transmit():
        if not _boolean(config, "RF0", "transmit", default=True):
            return "RF0.transmit is false: nothing would be transmitted"

    def rates():
        # ORCA sets the hardware from RF0 but sizes every burst from RF1, and
        # plays the chirp, generated at GENERATE.sample_rate, at RF0.tx_rate.
        rf0_tx, rf0_rx = _number(config, "RF0", "tx_rate"), _number(config, "RF0", "rx_rate")
        for name, value, want in (("RF1.tx_rate", _number(config, "RF1", "tx_rate"), rf0_tx),
                                  ("RF1.rx_rate", _number(config, "RF1", "rx_rate"), rf0_rx)):
            if value != want:
                return (f"{name} ({value:g}) differs from RF0's ({want:g}): the radar sizes its bursts "
                        "from RF1 and clocks them at RF0's rate")
        generated = _number(config, "GENERATE", "sample_rate")
        if generated != rf0_tx:
            return (f"GENERATE.sample_rate ({generated:g}) differs from RF0.tx_rate ({rf0_tx:g}): the chirp "
                    "would be played at another speed and its band would change")

    def clock_rate():
        clk = _number(config, "DEVICE", "clk_rate")
        if not MIN_CLK_RATE_HZ <= clk <= MAX_CLK_RATE_HZ:
            return (f"DEVICE.clk_rate {clk:g} is outside what the SDR supports "
                    f"({MIN_CLK_RATE_HZ:g} to {MAX_CLK_RATE_HZ:g})")
        for key in ("tx_rate", "rx_rate"):
            rate = _number(config, "RF0", key)
            if rate <= 0:
                return f"RF0.{key} must be positive"
            divider = clk / rate
            if rate > clk or divider != int(divider) or divider > MAX_RATE_DIVIDER:
                return (f"RF0.{key} ({rate:g}) is not DEVICE.clk_rate ({clk:g}) divided by a whole number up to "
                        f"{MAX_RATE_DIVIDER}: the SDR would run at another rate, and every burst and the "
                        "chirp's band would change with it")

    def chirp_fits():
        tx = _number(config, "CHIRP", "tx_duration")
        chirp = _number(config, "GENERATE", "chirp_length")
        gen = config.get("GENERATE")
        pulse = _number(config, "GENERATE", "pulse_length") if "pulse_length" in gen else chirp
        if chirp <= 0:
            return "GENERATE.chirp_length must be positive"
        if chirp > pulse:
            return f"GENERATE.chirp_length {chirp * 1e6:g} us is longer than GENERATE.pulse_length {pulse * 1e6:g} us"
        if pulse > tx:
            return (f"GENERATE.pulse_length {pulse * 1e6:g} us is longer than CHIRP.tx_duration {tx * 1e6:g} us: "
                    "the radar would cut the chirp off")

    def duty():
        on_air = burst(config)
        pri = _number(config, "CHIRP", "pulse_rep_int")
        if pri <= 0:
            return "CHIRP.pulse_rep_int must be positive"
        value = on_air["tx_s"] / pri
        if value > limits["max_enable_duty"]:
            return (f"amplifier enable duty {value:.3f} (transmit burst of {on_air['tx_s'] * 1e6:g} us every "
                    f"{pri * 1e6:g} us) is above the cap of {limits['max_enable_duty']:g}")

    def window():
        on_air = burst(config)
        lead = _number(config, "CHIRP", "tx_lead")
        if on_air["rx_s"] < on_air["tx_s"]:
            return (f"the receive window ({on_air['rx_s'] * 1e6:g} us) is shorter than the transmit burst "
                    f"({on_air['tx_s'] * 1e6:g} us): the amplifier is enabled only while both are active")
        if lead < 0 or lead >= on_air["tx_s"]:
            return f"CHIRP.tx_lead {lead * 1e6:g} us must be between 0 and the transmit burst length"

    def band():
        generated, played = _number(config, "GENERATE", "sample_rate"), _number(config, "RF0", "tx_rate")
        width = _number(config, "GENERATE", "chirp_bandwidth")
        offset = _number(config, "GENERATE", "lo_offset_sw")
        if width <= 0 or generated <= 0 or played <= 0:
            return "GENERATE.chirp_bandwidth and the sample rates must be positive"
        if abs(offset) + width / 2 > generated / 2:
            return (f"the chirp ({(offset - width / 2) / 1e6:g} to {(offset + width / 2) / 1e6:g} MHz at baseband) "
                    f"does not fit in the sample rate of {generated / 1e6:g} MHz: it would alias")
        scale = played / generated              # the chirp file is played at RF0.tx_rate
        centre = _number(config, "RF0", "freq") + offset * scale
        low, high = centre - width * scale / 2, centre + width * scale / 2
        allowed = f"{limits['min_freq_hz'] / 1e6:g}-{limits['max_freq_hz'] / 1e6:g} MHz"
        if low < limits["min_freq_hz"] or high > limits["max_freq_hz"]:
            return f"chirp band {low / 1e6:g}-{high / 1e6:g} MHz is outside the allowed {allowed}"
        # The mixer runs at freq + lo_offset (ORCA: tune_request_t(freq, lo_offset); 0 if not given).
        rf0 = config.get("RF0")
        lo_offset = _number(config, "RF0", "lo_offset") if "lo_offset" in rf0 else 0.0
        mixer = _number(config, "RF0", "freq") + lo_offset
        if not limits["min_freq_hz"] <= mixer <= limits["max_freq_hz"]:
            return (f"the mixer frequency {mixer / 1e6:g} MHz (RF0.freq + RF0.lo_offset) is outside the "
                    f"allowed {allowed}: its leakage is transmitted")
        clk = _number(config, "DEVICE", "clk_rate")
        if max(abs(low - mixer), abs(high - mixer)) > clk / 2:
            return (f"chirp band {low / 1e6:g}-{high / 1e6:g} MHz is more than half the clock rate "
                    f"({clk / 2e6:g} MHz) from the mixer at {mixer / 1e6:g} MHz (RF0.lo_offset): "
                    "the SDR cannot shift it there")

    def clock():
        value = _text(config, "DEVICE", "clk_ref")
        if value not in limits["clk_refs"]:
            return (f"DEVICE.clk_ref is {value}, must be one of {', '.join(map(str, limits['clk_refs']))}"
                    + (" (gpsdo makes ORCA open and reconfigure the GPS receiver)" if value == "gpsdo" else ""))

    def formats():
        sec = config.get("DEVICE")
        value = sec.get("cpu_format", "fc32") if isinstance(sec, dict) else "fc32"
        if value != "fc32":
            return f"DEVICE.cpu_format is {value}: the radar supports only fc32"

    def pulses():
        n = _integer(config, "CHIRP", "num_pulses")
        presums = _integer(config, "CHIRP", "num_presums") if "num_presums" in config.get("CHIRP", {}) else 1
        if n == 0 or n < -1:
            return "CHIRP.num_pulses must be -1 (until stopped) or positive"
        if presums < 1:
            return "CHIRP.num_presums must be at least 1"

    def files():
        for section, key in (("FILES", "chirp_loc"), ("FILES", "save_loc"), ("FILES", "gps_loc"),
                             ("GENERATE", "out_file")):
            _text(config, section, key)

    for fn in (amp_pin, amp_low, bank, gain, channels, transmit, rates, clock_rate, chirp_fits, duty, window, band, clock,
               formats, pulses, files):
        rule(fn)
    return problems


def check_output(config, orca_dir, data_root):
    """The derived config must write below the data root, with plain file names. Returns problems."""
    problems = []
    try:
        out = _text(config, "FILES", "output_dir")
        target = os.path.realpath(os.path.join(os.path.realpath(orca_dir), out))
        root = os.path.realpath(data_root)
        if os.path.commonpath([target, root]) != root or target == root:
            problems.append(f"FILES.output_dir resolves to {target}, which is not below the data root {root}")
        for section, key in (("FILES", "chirp_loc"), ("FILES", "save_loc"), ("FILES", "gps_loc"),
                             ("GENERATE", "out_file")):
            name = _text(config, section, key)
            if name != os.path.basename(name) or name in ("", ".", ".."):
                problems.append(f"{section}.{key} must be a plain file name")
        if _text(config, "FILES", "chirp_loc") != _text(config, "GENERATE", "out_file"):
            problems.append("FILES.chirp_loc differs from GENERATE.out_file: the radar would not "
                            "transmit the chirp that was generated")
        if _integer(config, "FILES", "max_chirps_per_file") < 1:
            problems.append("FILES.max_chirps_per_file must be positive: data is written in chunks")
    except _Bad as e:
        problems.append(str(e))
    return problems


# ------------------------------------------------------------ per-collection config

CHIRP_FILE, DATA_FILE, GPS_FILE = "chirp.bin", "rx_samps.bin", "gps_log.txt"


def record_bytes(config):
    """Size of one record in the data file: num_rx_samps complex float32, sized as the radar sizes it."""
    return burst(config)["rx_samples"] * 8


def continuous(config):
    """True if the radar runs until it is stopped (CHIRP.num_pulses < 0)."""
    return _integer(config, "CHIRP", "num_pulses") < 0


def pulses_per_file(config, chunk_bytes, chunk_max_s):
    """FILES.max_chirps_per_file for a data file of about `chunk_bytes`, at most `chunk_max_s` long.

    The radar starts a new file every max_chirps_per_file good pulses and
    writes one record per num_presums good pulses, so the value is a multiple
    of num_presums, and at least one record.
    """
    presums = _integer(config, "CHIRP", "num_presums") if "num_presums" in config.get("CHIRP", {}) else 1
    presums = max(1, presums)
    pri = _number(config, "CHIRP", "pulse_rep_int")
    by_size = chunk_bytes / max(1, record_bytes(config))                # records
    by_time = chunk_max_s / (pri * presums) if pri > 0 else by_size     # records
    records = max(1, int(min(by_size, by_time)))
    return min(records * presums, 2**31 - 1 - presums)                  # the radar reads it as an int


def derive(config, output_dir, max_chirps_per_file):
    """The config for one collection: the requested one, writing in chunks into `output_dir`.

    `output_dir` must be the collection directory as a path relative to the
    ORCA checkout: both the radar and the chirp generator resolve it from
    there. Raises ConfigError if a checked value cannot be read.
    """
    out = copy.deepcopy(config)
    try:
        for section, key, reader in _CHECKED:
            if reader is _boolean and key not in out.get(section, {}):
                continue                        # optional in ORCA; `check` knows its default
            out[section][key] = reader(out, section, key)
        if "pulse_length" in out["GENERATE"]:
            out["GENERATE"]["pulse_length"] = _number(out, "GENERATE", "pulse_length")
        if "lo_offset" in out["RF0"]:
            out["RF0"]["lo_offset"] = _number(out, "RF0", "lo_offset")
        _get(out, "FILES", "save_loc")
        _get(out, "GENERATE", "out_file")
    except _Bad as e:
        raise ConfigError(str(e)) from None
    out["FILES"]["output_dir"] = output_dir
    out["FILES"]["chirp_loc"] = CHIRP_FILE
    out["GENERATE"]["out_file"] = CHIRP_FILE
    out["FILES"]["save_loc"] = DATA_FILE
    out["FILES"]["gps_loc"] = GPS_FILE
    out["FILES"]["max_chirps_per_file"] = int(max_chirps_per_file)
    if isinstance(out.get("GENERATE"), dict) and "show_plot" in out["GENERATE"]:
        out["GENERATE"]["show_plot"] = False    # a plot window would block the chirp generator
    return out
