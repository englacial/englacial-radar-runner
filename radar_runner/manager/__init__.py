"""Radar manager (stage 4): starts and stops the radar on triggers, behind safety gates.

    logic.py     the decisions: a pure state machine, no I/O and no clock
    gates.py     the requirements to start and to keep running (status files, drive, SDR)
    limits.py    the fixed limits every radar config must pass, and the per-collection config
    runner.py    runs ORCA: chirp, build, radar process, log, stop, amplifier made safe
    panel.py     front-panel button and LEDs
    control.py   the Unix socket that `radarctl` talks to
    service.py   ties them together in the shared service loop

See README.md in this directory.
"""
