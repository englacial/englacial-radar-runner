Stand-in for the ORCA checkout (`~/orca`), for the manager's tests. Nothing here touches hardware.

    preprocessing/generate_chirp.py   same path rule as ORCA's: FILES.output_dir is taken relative to this tree
    sdr/build/fake_build.py           the "build"
    sdr/build/radar.py                the `radar` binary: the path rules and the stdout lines of sdr/main.cpp
    amp_safe.py                       the "make the amplifier safe" command
    fake_measure.py                   the turn-on check's analysis (tools/turn_on_check/measure.py)

Each reads the JSON file named by the environment variable FAKE_ORCA_CONTROL (if set) when it starts,
to be told to misbehave: see the top of each script.
