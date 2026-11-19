"""Radar manager (stage 4): starts and stops the radar on triggers, behind safety gates.

    logic.py       the decisions: a pure state machine, no I/O and no clock
    gates.py       the requirements to start and to keep running (status files, drive, SDR)
    limits.py      the fixed limits every radar config must pass, and the per-collection config
    runner.py      runs ORCA: chirp, build, radar process, output log, stop
    ampsafe.py     when the reset tool runs, and the record of whether the amplifier pin is safe
    collection.py  collection directories, names and the metadata file
    panel.py       front-panel button and LEDs
    control.py     the Unix socket that `radarctl` talks to, and the event log
    radarctl.py    the command-line tool (standard library only)
    service.py     ties them together in the shared service loop

Settings: the section `manager` of config/radar-runner.yaml. See README.md in this directory.
"""
