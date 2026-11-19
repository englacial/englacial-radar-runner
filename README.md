# englacial-radar-runner

The radar manager for the truck-towed radio echo sounder: everything on the Pi
except the radar acquisition code itself (`~/orca`). Plan and stages:
`20261118-radar-runner-implementation-plan.md`. Current hardware: `HARDWARE_CONFIG.md`.

## Layout

| Path | What |
|---|---|
| `power/`, `config/power.toml`, `systemd/radar-power.service` | Power service (stage 1). Standard library only, run as root by `/usr/bin/python3`; independent of everything below. See `power/README.md` |
| `radar_runner/common/` | Shared code for all other services: status files, config, logging, systemd notification, the service loop |
| `radar_runner/example.py` | A minimal service to copy. Not installed |
| `config/radar-runner.yaml` | Settings of those services, with every default |
| `setup/` | System setup: `setup.sh`, `check.py`, `SYSTEM_CHANGES.md`, the files it installs (`files/`), version pins (`pins.toml`), and the Python environment tools |
| `tests/power/` | Power service tests (unittest) |
| `tests/common/` | Tests of the shared code and of `setup.sh` (pytest) |
| `.venv/`, `wheelhouse/` | Python environment and the wheels it is built from. Not in git |

## Python environment

All services except the power service run on `.venv`, built by
[uv](https://docs.astral.sh/uv/) from `uv.lock` on the system's Python 3.12.
The code runs from the checkout; nothing is installed for the repository itself.

```
setup/build_venv.sh        # build or repair .venv from wheelhouse/; no internet, no root
```

Do not use `uv sync` or `uv run`: they fetch from the internet. To change a
dependency (needs internet): edit `pyproject.toml`, then

```
uv lock --python /usr/bin/python3
setup/fetch_wheels.py      # download the locked wheels, verified against uv.lock
setup/build_venv.sh
```

and commit `pyproject.toml` and `uv.lock`. `wheelhouse/` is not in git: copy it
between units, or run `setup/fetch_wheels.py` while online.

## Tests

```
/usr/bin/python3 -B -m unittest       # power service only, as it runs in the field
.venv/bin/python -B -m pytest         # everything
```

No test touches the hardware, the installed services or the system
configuration; none can power the Pi off.

## System setup

```
setup/setup.sh --dry-run    # what would change; no root needed
tmux                        # so that a dropped SSH session does not interrupt the next command
sudo setup/setup.sh         # apply; safe to run again, also after an interruption
sudo reboot
setup/check.py              # PASS/FAIL per item; also after every update, and to compare two systems
```

What each change is, why, and how to revert it: `setup/SYSTEM_CHANGES.md`,
which also lists the three bench tests that must follow enabling the hardware
watchdog. The script installs no packages; chrony and rclone come with the
stages that configure them (2 and 9).

## Writing a service

See the top of `radar_runner/common/service.py` and `radar_runner/example.py`.
Add its settings as a new section in `radar_runner/common/config.py` and
`config/radar-runner.yaml`. Each service publishes
`/run/radar-runner/user/<name>.json` and reads the others with
`radar_runner.common.status.read_status`, which reports a file that is old,
missing or damaged without raising. Services run as user `englacial` and write
only inside `/run/radar-runner/user` and `/var/lib/radar-runner/user`; the
parent directories belong to root and hold only the power service's files
(`/run/radar-runner/power.json`, readable by everyone).
