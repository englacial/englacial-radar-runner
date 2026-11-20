# Loopback reference before transport

`20261120-portable-loopback.json` is the reference made on 2026-11-20 with the portable loopback
of `HARDWARE_CONFIG.md` (amplifier -> 20 dB -> 30 dB -> splitter A; loopback branch 30 dB and a
30 ft cable into the receive front end), `tx_gain` 70, VVA 3.452 V, on a quiet Pi. Its data is
collection `b00013_s002_20261120T051050Z` under `/mnt/nvme/radar-runner/collections/`.

To compare a run made after transport (same hardware, same `/etc/radar-runner/radar.yaml`):

    radarctl start        # let it run for two minutes
    radarctl stop --wait
    ~/miniforge3/envs/orca/bin/python tools/transit_check/transit_check.py \
        "$(ls -dt /mnt/nvme/radar-runner/collections/*/ | head -1)" \
        docs/transit-reference/20261120-portable-loopback.json

What the numbers mean:

- The peak is at sample 78: with the short cable the loopback arrives about 3 samples after the
  internal leakage, and the two form one peak. Its level therefore depends on the leakage path
  inside the case as well as on the cable and attenuators; the shape around the peak says which
  moved. Level -5.98 dB, steady to 0.06 dB over two minutes.
- Scope side in the same run (eleven captures, all with full pulse trains): power detector 1.471 V
  (+/-0.002) during the pulse, against 2.06-2.10 V with no signal; scope RF channel -21.40 dBm
  (+/-0.07) at the scope. With the nominal 56.6 dB between amplifier and detector that is +39.3 dBm
  (about 8.4 W, +/-1.5 to 2 dB: the new attenuators are not measured). The raw readings repeat to
  about 0.1 dB and are what to compare. 12 V input: +0.42 A while transmitting.
- Pulses lost 2.4 % on a quiet Pi. A first attempt with the Pi's processor busy (load 2-4) lost
  32 % of the time to dropped pulses and schedule slips with the same recorded level: compare
  runs only when nothing else is running.

`20261120-portable-loopback-all-runs.json` holds the same numbers for every run made with this
arrangement on 2026-11-20 (the reference, the first attempt under load, and the short runs before
power cuts 3 to 5, each after a cold boot): it shows how much the numbers move from run to run
and across power cycles with nothing changed, which is the yardstick for a difference after
transport.
