Records from bench collections under `/mnt/nvme/radar-runner/collections/` (records 2 and 3, or
record 2 alone; each record 2352 complex float32, 1000 pulses summed; 300 MHz, 30 MHz chirp,
13 us pulse, 56 MS/s). `chirp.bin` is the chirp of all of them.

| File | Collection | tx_gain | Arrangement |
|---|---|---|---|
| `loopback_30ft_tx70.bin` | b00017_s003_20261120T085831Z | 70 | portable loopback (`HARDWARE_CONFIG.md`): one combined peak at sample 78, -5.9 dB |
| `loopback_50m_tx70.bin` | b00011_s001_20261120T032640Z | 70 | the earlier 50 m cable: leakage at 75 (-35 dB), loopback at 90 (-10.6 dB) |
| `loopback_50m_tx45.bin`, `_tx72.bin` | b00009_s002, b00009_s005 (2026-11-20) | 45, 72 | 50 m cable, the gain tuning (VVA potentiometer) |
| `no_signal_tx0.bin` | b00004_s008_20261119T184531Z | 0 | no loopback signal (noise, a trace of leakage at 76) |
| `amplifier_off_tx70.bin` | b00013_s003_20261120T053228Z, its last two records | 70 | portable loopback after a power cut: the 28 V gone, only the leakage left (sample 75.07, -33.5 dB) |
