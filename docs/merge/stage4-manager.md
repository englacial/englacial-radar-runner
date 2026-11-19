# Stage 4 (radar manager): what to add to the shared files at merge

The manager's branch does not touch the files other stages were editing. This
is the list of what belongs in them. Delete this file once it is done.

## radar_runner/common/config.py and config/radar-runner.yaml

* Move `MANAGER_DEFAULTS` (radar_runner/manager/config.py) into `DEFAULTS` as
  the section `"manager"`; keep `manager/config.py` only if something still
  imports it (`defaults()` and `load()` then become one-liners over the shared
  loader), and replace `radar_runner/manager/__main__.py` by
  `sys.exit(run_service(Manager))`.
* Append `docs/merge/manager-config-section.yaml` to `config/radar-runner.yaml`.
  `tests/manager/test_manager_control.py::test_config_section_for_the_merge_matches_the_built_in_defaults`
  checks the two against each other; point it at the merged file.

## radar_runner/common/bootcount.py

* Already used if present: `radar_runner/manager/collection.py` `boot_number()`
  imports `radar_runner.common.bootcount.boot_count` and falls back to
  `_placeholder_boot_count` only when the import fails. At merge delete the
  placeholder, the fallback, and its test
  (`test_placeholder_boot_counter_counts_boots_not_starts`).

## setup/setup.sh (needs root; each is a system change to record in SYSTEM_CHANGES.md)

1. Install the unit: copy `systemd/radar-manager.service` to
   `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable radar-manager`.
   Do not start it before the stage 3 tool and the watcher exist and the first
   real start has been reviewed.
2. `ln -s /home/englacial/englacial-radar-runner/bin/radarctl /usr/local/bin/radarctl`.
3. The power service's hook, in `/etc/radar-runner/power.toml`:
   `stop_radar = ["/home/englacial/englacial-radar-runner/bin/radarctl", "stop", "--interrupt", "--wait", "--timeout", "4"]`
   and review `stop_radar_timeout_s` (5 s): the wait covers the radar's exit
   (up to `orca.stop_timeout_s` = 3 s) **and** the amplifier step. With the
   default policy that step is a wait for an amplifier reading taken after the
   exit (about 1 s, `amp_safe.confirm_settle_s`; up to `confirm_timeout_s`,
   4 s, if telemetry is not fresh) and, only when the pin is then not known to
   be safe, the tool itself (up to 3 x 20 s). Either raise the hook's
   timeout, or accept that the hook can return before the amplifier step has
   finished (the tool serialises concurrent callers).
   The power service's own `amp_safe` hook can test the manager's record
   before it runs the tool:
   `[ "$(head -n1 /var/lib/radar-runner/user/sdr-state 2>/dev/null)" = safe ]`.
4. The data root exists once the drive is mounted: the manager creates
   `/mnt/nvme/radar-runner/collections` itself; `/mnt/nvme/radar-runner` must
   be writable by `englacial` (the telemetry stage already needs that).
5. `/etc/radar-runner/radar.yaml`: the requested ORCA config, readable by
   `englacial`. Not created by the manager.

No new package and no new Python dependency: PyYAML, gpiozero and lgpio are in
`uv.lock` already; `setpriv` is part of util-linux (installed).

## setup/check.py

* `radar-manager.service` installed, enabled, and identical to `systemd/radar-manager.service`.
* `/usr/local/bin/radarctl` resolves to the checkout's `bin/radarctl`; `radarctl status` exits 0 (3 = manager not running).
* `/usr/bin/setpriv` exists and knows `--pdeathsig` (`setpriv --pdeathsig KILL true` exits 0).
* User `englacial` may open `/dev/gpiochip4` (group `dialout` on this image) and has `rtprio 99` (`/etc/security/limits.conf`; the unit sets `LimitRTPRIO=99`).
* `manager.amp_safe.command[0]` exists and is executable, and so is `bin/radar-manager-stop-hook` (the unit's `ExecStopPost`).
* `manager.radar_config` exists and passes the limits:
  `.venv/bin/python -c "from radar_runner.manager import limits, config; c,_=config.load(); print(limits.check(limits.load_file(c['manager']['radar_config']), c['manager']['limits']))"` prints `[]`.
* The data drive: `manager.gates.data.mount_point` is a mount point and the root is writable by `englacial`.
* ORCA: `orca.dir` at the pinned commit; `sdr/build/radar` exists.
* `manager.limits.max_tx_gain` and `max_enable_duty` are set in `/etc/radar-runner/local.yaml` (a reminder while the defaults are the conservative ones).

## README.md (layout table)

| Path | What |
|---|---|
| `radar_runner/manager/`, `systemd/radar-manager.service`, `bin/radarctl` | Radar manager (stage 4): state machine, gates, ORCA runner, button and LEDs, `radarctl`. See `radar_runner/manager/README.md` |
| `tests/manager/` | Its tests (pytest), with a stand-in ORCA tree in `tests/manager/fake_orca` |
| `docs/orca-proposals/` | What works with ORCA unchanged, and patches proposed for ORCA |

## Tests

`tests/manager/` has no `__init__.py` on purpose: `/usr/bin/python3 -m unittest`
(the power service's suite) must not discover it, because the system Python
has no pytest.
