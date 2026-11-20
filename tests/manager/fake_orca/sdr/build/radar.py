"""Stand-in for ORCA's `radar` binary (sdr/main.cpp). Opens no device.

What it copies from ORCA, because the manager depends on it:
  * the config is read from "../../" + argv[1], relative to the working directory;
  * the burst sizes come from RF1's rates, as in sdr.cpp / main.cpp:
        num_tx_samps = int(RF1.tx_rate * CHIRP.tx_duration)
        num_rx_samps = int(RF1.rx_rate * CHIRP.rx_duration)
    and one record is num_rx_samps complex float32;
  * SIGINT has its default action (the program dies) until the device set-up is over;
    only then is the handler installed, as main.cpp does after createUsrp/setupUsrp.
    "[VERSION]" is the first line printed after that;
  * UHD's own messages ("[INFO] [B200] Operating over USB 3.") go to stderr;
  * the data file is FILES.output_dir + "/" + FILES.save_loc, with "../../" in front unless
    it starts with "/"; with FILES.max_chirps_per_file > 0 the files are <save_loc>.0, .1, ...,
    a new one every max_chirps_per_file good pulses; one record per CHIRP.num_presums pulses;
  * the chirp is read from "../../" + FILES.output_dir + "/" + FILES.chirp_loc (always
    with "../../" in front), and a missing chirp ends the program with status 1;
  * the GPS file FILES.output_dir + "/" + FILES.gps_loc is created empty;
  * the stdout lines "[VERSION]", "[OPEN FILE] <path>", "[START] Beginning main loop",
    "[CLOSE FILE] <path>", "[ERROR] (Chirp N) Receiver error: ...", and the "[RX]"/"[TX]"
    lines of a stop; error lines are not flushed (the real ones end in "\\n", not endl);
  * SIGINT (once the handler is there) ends the main loop; exit status 0.

Time runs fast: FAKE_RADAR_SPEEDUP (default 1) multiplies the pulse rate.

Control file (JSON, named by FAKE_ORCA_CONTROL):
  "coerce_rate"  true: the SDR runs at half the requested rates (prints ORCA's "[WARNING]: Requested
              ... rate" lines; they are also printed whenever RF0's rates do not divide DEVICE.clk_rate)
  "init_s"    how long the device set-up takes (default 0.1 s); SIGINT during it kills the program
  "radar"     "fail_start"     exit 1 at the end of the set-up, before the start line
              "no_start"       never print the start line (the handler is installed)
              "crash"          exit 3 one second after the start line
              "exit0"          exit 0 by itself one second after the start line
              "ignore_sigint"  keep running after SIGINT
              "slow_exit"      take 2 s to exit after SIGINT
              "flood"          print 200 error lines per loop round (about 20 000 lines a second)
              "stderr"         write more UHD-style noise to stderr
              "hang"           ignore the pulse count: run until SIGINT
              "no_data"        write no records; "short_data": only the first half of them
  "radar_check"  as "radar", for the turn-on check's low-power run only (FILES.save_loc low_power.bin);
              without it, "radar" applies to that run too
  "records_from" a file of records (complex float32) recorded at tx_gain "records_gain": written in turn,
              scaled by 1 dB per step of RF0.tx_gain, instead of the constant records
  "tx_file"   this file exists while pulses go out (for a stand-in telemetry: the amplifier current)
  "pulse_delay_s"  wait this long after the start line before the first pulse (ORCA: CHIRP.time_offset)
"""
import array
import json
import os
import signal
import sys
import time

import yaml

control = {}
if os.environ.get("FAKE_ORCA_CONTROL"):
    try:
        with open(os.environ["FAKE_ORCA_CONTROL"]) as f:
            control = json.load(f)
    except OSError:
        pass
mode = control.get("radar", "")
speedup = float(os.environ.get("FAKE_RADAR_SPEEDUP", "1"))

stop = False


def on_sigint(*_):
    global stop
    stop = True


def say(text, flush=True):
    sys.stdout.write(text + "\n")
    if flush:
        sys.stdout.flush()


def uhd(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


signal.signal(signal.SIGINT, signal.SIG_DFL)        # Python would raise KeyboardInterrupt; C dies
yaml_filename = "../../" + sys.argv[1] if len(sys.argv) >= 2 else "../../config/default.yaml"
say("Reading from config file: " + yaml_filename)
with open(yaml_filename) as f:
    config = yaml.safe_load(f)
if str((config.get("FILES") or {}).get("save_loc")) == "low_power.bin":
    mode = control.get("radar_check", mode)
say("")
say("Creating the usrp device with: " + str(config["DEVICE"]["device_args"]) + "...")
uhd("[INFO] [UHD] linux; GNU C++ version 13.3.0; Boost_109000; UHD_4.11.0.0-0-c87abfbe")
uhd("[INFO] [B200] Detected Device: B206mini")
uhd("[INFO] [B200] Operating over USB 3.")
if mode == "stderr":
    sys.stderr.write("[WARNING] [B200] something UHD says on stderr\nLLLL")
    sys.stderr.flush()
time.sleep(float(control.get("init_s", 0.1)))      # createUsrp, setupUsrp: firmware, FPGA, tuning
if mode == "fail_start":
    say("Error: LookupError: KeyError: No devices found for ----->")
    sys.exit(1)
# As UHD: the SDR runs at its clock divided by a whole number; ORCA (rf_settings.cpp) only warns.
clk_rate = float(config["DEVICE"]["clk_rate"])
for name, key in (("RX", "rx_rate"), ("TX", "tx_rate")):
    requested = float(config["RF0"][key])
    actual = clk_rate / max(1, round(clk_rate / requested))
    if control.get("coerce_rate"):
        actual = requested / 2
    if actual != requested:
        say(f"[WARNING]: Requested {name} rate (CH0): {requested:f} Hz. Actual {name} rate: {actual:f} Hz.")

files = config["FILES"]
chirp = config["CHIRP"]
output_dir = str(files["output_dir"])
save_loc = output_dir + "/" + str(files["save_loc"])
gps_save_loc = output_dir + "/" + str(files["gps_loc"])
max_per_file = int(files["max_chirps_per_file"])
num_pulses = int(chirp["num_pulses"])
num_presums = int(chirp.get("num_presums", 1))
pri = float(chirp["pulse_rep_int"])
# As ORCA: the Sdr class takes its rates from RF1.
num_tx_samps = int(float(config["RF1"]["tx_rate"]) * float(chirp["tx_duration"]))
num_rx_samps = int(float(config["RF1"]["rx_rate"]) * float(chirp["rx_duration"]))

signal.signal(signal.SIGINT, signal.SIG_IGN if mode == "ignore_sigint" else on_sigint)

say("[VERSION] 0.0.2")
say("Note: Phase inversion is performed in this code.")
say(f"INFO: Number of TX samples: {num_tx_samps}")
say(f"INFO: Number of RX samples: {num_rx_samps}")
say("Starting GPS code...")

chirp_path = "../../" + output_dir + "/" + str(files["chirp_loc"])
if not os.path.isfile(chirp_path):
    say("")
    say("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    say("ERROR! Failed to open chirp.bin input file")
    say("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)

if not save_loc.startswith("/"):
    save_loc = "../../" + save_loc
if not gps_save_loc.startswith("/"):
    gps_save_loc = "../../" + gps_save_loc
os.close(os.open(gps_save_loc, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o700))

save_file_index = 0
current_filename = save_loc + (f".{save_file_index}" if max_per_file > 0 else "")
say("[OPEN FILE] " + current_filename)
outfile = open(current_filename, "wb")

if mode == "no_start":
    while not stop:
        time.sleep(0.05)
    sys.exit(0)

if num_pulses < 0:
    say("num_pulses is < 0. Will continue to send chirps until stopped with Ctrl-C.")
say("[START] Beginning main loop")

records = [b"\x01" * (num_rx_samps * 8)]
if control.get("records_from"):
    with open(control["records_from"], "rb") as f:
        raw = f.read()
    scale = 10 ** ((float(config["RF0"]["tx_gain"]) - float(control.get("records_gain", 0))) / 20)
    records = []
    for i in range(len(raw) // (num_rx_samps * 8)):
        values = array.array("f", raw[i * num_rx_samps * 8:(i + 1) * num_rx_samps * 8])
        records.append(array.array("f", (v * scale for v in values)).tobytes())
written_records = 0
wanted_records = num_pulses // num_presums if num_pulses > 0 else -1
if mode == "hang":
    num_pulses = -1
time.sleep(float(control.get("pulse_delay_s", 0)))
if control.get("tx_file"):
    open(control["tx_file"], "w").close()
t0 = time.monotonic()
pulses_received = 0
error_count = 0
last_pulse_num_written = -1
while num_pulses < 0 or last_pulse_num_written < num_pulses:
    time.sleep(0.01)
    due = int((time.monotonic() - t0) * speedup / pri)
    if mode == "flood":
        for _ in range(200):
            say(f"[ERROR] (Chirp {pulses_received}) Receiver error: ERROR_CODE_LATE_COMMAND", flush=False)
            pulses_received += 1
            error_count += 1
        say(f"[TX] (Chirp {pulses_received}) time_offset increased by 0.0004")
    while pulses_received - error_count < due and (num_pulses < 0 or last_pulse_num_written < num_pulses):
        pulses_received += 1
        good = pulses_received - error_count
        if good % num_presums == 0:
            if not (mode == "no_data" or (mode == "short_data" and 2 * written_records >= wanted_records)):
                outfile.write(records[written_records % len(records)])
            written_records += 1
            last_pulse_num_written = good
        if max_per_file > 0 and last_pulse_num_written // max_per_file > save_file_index:
            outfile.close()
            say("[CLOSE FILE] " + current_filename)
            save_file_index += 1
            current_filename = save_loc + "." + str(save_file_index)
            say("[OPEN FILE] " + current_filename)
            outfile = open(current_filename, "wb")
    if mode == "crash" and time.monotonic() - t0 > 1.0:
        say("terminate called after throwing an instance of 'uhd::io_error'")
        os._exit(3)
    if mode == "exit0" and time.monotonic() - t0 > 1.0:
        say("[RX] TX finished and no pulses in flight; stopping with 0 pulses written")
        break
    if stop:
        say("[RX] Reached stop signal handling for outer RX loop -> break")
        break

if control.get("tx_file"):
    try:
        os.unlink(control["tx_file"])
    except OSError:
        pass
if mode == "slow_exit":
    time.sleep(2.0)
say("[TX] stop signal called -> break")
say("[TX] Closing file")
say("[TX] Done.")
say("[RX] Closing output file.")
outfile.close()
say("[CLOSE FILE] " + current_filename)
say(f"[RX] Error count: {error_count}")
say(f"[RX] Total pulses written: {last_pulse_num_written}")
say(f"[RX] Total pulses attempted: {pulses_received}")
say("[RX] Done. Calling join_all() on transmit thread group.")
say("[RX] transmit_thread.join_all() complete.")
sys.exit(0)
