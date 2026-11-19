# ORCA: what the manager needs, and proposed changes

Written against `~/orca` at commit 6fd9448. Nothing in `~/orca` was changed by
the manager's work. Each patch here is a proposal for the user to review; none
was compiled or run by the manager's work (the stage rules forbid building or
running ORCA). Apply with `cd ~/orca && git apply <file>`.

| Patch | State (2026-11-19) |
|---|---|
| `0001-bounded-exit-after-sigint.patch` | approved; applied in `~/orca`'s working tree, not committed |
| `0002-absolute-config-and-chirp-paths.patch` | optional, not applied |
| `0003-sigint-handler-before-device-setup.patch` | proposed, not applied; drafted on top of 0001 |

## What works with ORCA unchanged

The manager makes today's ORCA write straight into the collection directory,
in chunks, with nothing moved or copied afterwards. Tested end to end against
a stand-in that copies the path rules, the burst arithmetic, the late SIGINT
handler and the output lines of `sdr/main.cpp`, and the path rule of
`preprocessing/generate_chirp.py` (`tests/manager/fake_orca`).

| Need | How, with ORCA as it is |
|---|---|
| Data written directly into the collection directory | `FILES.output_dir` in the derived config is the collection directory **as a path relative to the ORCA checkout** (e.g. `../../../mnt/nvme/radar-runner/collections/b00042_s003_...`). `main.cpp` puts `../../` in front of relative paths (its working directory is `sdr/build`), and `generate_chirp.py` joins the same value to the checkout's path, so both resolve to the collection directory. |
| Data in chunks | `FILES.max_chirps_per_file` > 0: `main.cpp` writes `rx_samps.bin.0`, `.1`, ... and starts a new file every that many good pulses. The manager computes the value from `manager.data.chunk_mb` and `chunk_max_s` (a multiple of `num_presums`, so files hold whole records). |
| Chirp file and config in the collection directory | The chirp generator writes `<output_dir>/chirp.bin` and the radar reads it from there. The manager writes `config.yaml` (derived) and `config_requested.yaml` (byte copy) there before the radar starts, and passes `config.yaml` to the radar as a path relative to the checkout. |
| Config and logs saved at the start, not at the end | `collection.json`, both configs and `prepare.log` exist before the radar is launched; the radar's output is written to `uhd_stdout.log` as it arrives. `run.py`'s `save_data.py` step (copy the config, move `rx_samps.bin`, copy the logs under a timestamp prefix) is not needed and not used. |
| A hard power cut loses at most the chunk being written | The radar does not sync its files. The manager syncs each data file when the radar reports `[CLOSE FILE]`, so every closed chunk is on the drive. The open chunk is written back by the kernel within about 30 s as usual; after a cut it may end in a partial record (a reader must drop a tail shorter than `record_bytes` in `collection.json`). |
| Data valid after SIGKILL | Same as after a power cut: closed chunks are complete; the open chunk loses at most the radar's small write buffer. |
| The output log on the data drive | Done in the manager: the log goes to the collection directory in parts of 32 MB. Nothing is cut or filtered (below). No ORCA change. |
| ORCA must not touch the GPS | `DEVICE.clk_ref: gpsdo` makes `main.cpp` open `/dev/ttyACM0` and reconfigure the receiver, which gpsd owns. The manager's config limits refuse it. |

Two paths cannot be absolute today: the config argument and the chirp (both
get `../../` in front unconditionally). The relative form works; its cost is
that the radar must be started from a directory exactly two levels below the
checkout, which the manager checks (`orca.radar_cwd`).

Not written into the collection directory: `ofstream gps_output("gps_log.txt")`
in `main.cpp` creates an empty `gps_log.txt` in the working directory
(`sdr/build`) at every start. Harmless.

**The radar's output is data, not only a log.** Post-processing needs the
`[ERROR] (Chirp N) Receiver error` lines (which pulses are missing from the
file) and the HIL timing analysis needs `[TX] (Chirp N) time_offset increased
by D` (when the later pulses were sent). The manager keeps every line, in
ORCA's `run.py` format (`[unix time] \t<line>`); the size is in
`collection.json`. UHD's own messages (stderr) go to `uhd_stderr.log`.

## Things in ORCA that the manager has to work around

* **Burst sizes come from `RF1`, the hardware settings from `RF0`.**
  `sdr.cpp` reads the Sdr class's rates from `RF1` (`rx_rate = rf1["rx_rate"]`
  and so on) and `main.cpp` sizes every burst from them (`num_tx_samps =
  sdr.getTxRate() * tx_duration`), while `set_rf_params_single` sets the device
  from `RF0`. With `RF1.tx_rate` four times `RF0.tx_rate` every transmit burst,
  and with it the amplifier's enable time, is four times as long as
  `tx_duration` says. The chirp file is generated at `GENERATE.sample_rate` and
  played at `RF0.tx_rate`. The manager's limits require all three rates to be
  equal and compute duty, window and band from the real burst lengths. An ORCA
  fix (use `RF0`'s rates, or `GENERATE.sample_rate`, as its own TODO comments
  say) would be the cleaner cure; no patch is drafted for it here.
* **No SIGINT handler until the device set-up is over** (`main.cpp` installs it
  after `createUsrp()`/`setupUsrp()`): see patch 0003. Until then the manager
  holds a stop back until the `[START]` line.
* **"Operating over USB 2/3" is printed by UHD on stderr**, and before its
  firmware is loaded the SDR is a USB 2 device anyway. The manager reads the
  link speed from sysfs after the start line instead.

## Proposed patches

### 0001-bounded-exit-after-sigint.patch (approved, applied uncommitted)

Problem: after the 2 h soak on 2026-11-18 the radar had not exited 5 s after
SIGINT. Cause, from the source and the log of that run:

* The RX loop checks the stop flag only after `recv()` returns, and `recv()`
  waits up to 60 s.
* Normally about 6 pulses are in flight, the next one arrives within a pulse
  period, and the radar exits in about 0.3 s.
* During a burst of late commands the device answers each command at once, so
  nothing is in flight. SIGINT landed in such a burst (the log's last lines are
  eight consecutive late-command errors, then `[TX] stop signal called`). RX
  had just consumed the answer to the last scheduled pulse, saw the flag still
  clear and `tx_done` still false, and went back into `recv()`. TX then saw
  the flag and exited without scheduling another pulse. RX was left waiting
  for a pulse that would never be sent.
* It would have exited cleanly by itself after the 60 s timeout (one timeout,
  not a chain; plus up to about 2 s of UHD teardown). The chance of hitting
  this grows with the late-command rate, not with the length of the run.

So without the patch a clean exit takes either about 0.3 s or about 60 s,
nothing in between. The manager allows 3 s and then kills the radar; the data
is valid either way.

The patch makes RX wait in steps of 0.5 s. After a step without data it leaves
the loop if a stop was requested, and otherwise goes round again, where the
existing check (`tx_done` and nothing in flight) also gets its turn. A timeout
is still reported after 60 s without data, as before. Longest exit after
SIGINT: about 0.5 s plus teardown.

### 0002-absolute-config-and-chirp-paths.patch (optional)

Lets the config argument and the chirp path be absolute, as the data file path
already may be. Not needed: the manager works with relative paths. It would
remove the dependence on the working directory being two levels below the
checkout. `generate_chirp.py` already accepts an absolute `output_dir`.

### 0003-sigint-handler-before-device-setup.patch (proposed)

Problem: `main.cpp` installs its SIGINT handler only after `createUsrp()` and
`setupUsrp()`. Until then SIGINT has its default action and kills the program
in the middle of the device set-up. UHD's own initialisation makes every front
panel pin an output at level 0, which for this amplifier means "enabled", and
ORCA's `setupGpio()` (the last step of `createUsrp()`) is what puts the pin
under ATR control again. A kill between the two leaves the amplifier enabled
(about 70 W of heat with no RF) until something resets the pin. The first
start after power-up spends about 14 s loading firmware and FPGA before that
point, later starts about 1 s; the window in which a stop is unsafe is the
whole set-up, about 5 s, since the handler comes only at its end.

The patch installs the handler at the top of `main`, before `createUsrp()`,
and checks the flag once after `setupUsrp()`: if a stop was asked for during
the set-up, the program prints `[RX] stop signal called during device setup ->
exiting` and returns, with the pin where `setupGpio()` put it (ATR idle =
shutdown). A stop during the set-up then takes until the set-up is over, and is
safe.

With ORCA as it is, the manager holds a requested stop until the `[START]`
line (up to the start timeout; 1 s for a stop caused by power loss or the
manager's own exit) and treats a radar that had to be signalled earlier as not
safe: the reset tool runs. With the patch the hold becomes unnecessary; the
manager's rule can stay as it is (it never signals before `[START]` unless it
must), and the "must" cases stop leaving the amplifier enabled.

## Not proposed, for the record

* Syncing data files inside the radar: the manager does it from outside, for
  closed chunks. Syncing the open chunk more often than the kernel does would
  reduce the loss at a hard cut from about 30 s to less, at the price of extra
  I/O during a capture; not worth it without a measurement.
* Fewer or shorter output lines: they are needed (above).
* PPS timing: stage 8.

## To check on the bench (cannot be checked without running ORCA)

* The build at every start: `cmake ..` then `make radar` in `sdr/build`, as
  the service's user, with no internet (the CMake file fetches googletest; the
  copy in `sdr/build/_deps` should be reused).
* The time from launch to `[START]`: about 5 s on the bench; the first start
  after power-up adds the firmware and FPGA load (14 s measured with the reset
  tool). `orca.start_timeout_s` is 60 s.
* That the radar gets real-time priority under the service (`LimitRTPRIO=99`
  in the unit), as it does from a login shell.
* The sysfs view of the SDR is as the manager expects: `2500:0023`, product
  "WestBridge" at 480 Mbit/s before the firmware load; speed 5000 and another
  product text after (measured with the reset tool on 2026-11-19).
