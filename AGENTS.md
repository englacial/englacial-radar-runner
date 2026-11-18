This machine runs a scientific radio echo sounder for making ice thickness measurements.

You may edit within this directory unless otherwise specified.

The primary radar code is in `~/orca`. Be mindful that this is a software tool used by others and do not clutter with notes. Keep edits concise and human-readable. Don't make your own commits to this repo without approval.

Test bench code is in `~/radar-testbench` and this directory should be checked for recent notes and context.

There is a mamba environment named `orca` with the required software installed.

`HARDWARE_CONFIG.md` contains documentaiton of the current hardware connected to this system. Note that this machine is connected to real hardware and running the SDR in the wrong configurations can cause damage. Do a safety review before running the radar code.