"""Raw GPS log for PPK: the logger, and a check of a log. See README.md in this directory.

    python -m radar_runner.gpsraw log --out FILE [--keep NAMES] [--host H] [--port P] [--fsync-s S]
                                      [--parent-pid PID]
    python -m radar_runner.gpsraw check FILE [--json]

`check` reads a log and counts its messages (with checksums), the RXM-RAWX
epochs, the measurements and those with a valid carrier phase, per system,
and the receiver time of the first and last epoch. A cut-off last message is
reported and otherwise ignored. Exit status 0 if the log has RXM-RAWX epochs
with carrier phase, 1 if not, 2 if it cannot be read.
"""
import argparse
import json
import sys

from . import logger, ubx


def check(args):
    try:
        result = ubx.scan(args.file)
    except OSError as e:
        print(f"cannot read {args.file}: {e.strerror}", file=sys.stderr)
        return 2
    ok = result["rawx_epochs"] > 0 and result["carrier_phase_valid"] > 0
    if args.json:
        print(json.dumps(dict(result, carrier_phase=ok), indent=1))
        return 0 if ok else 1
    print(f"{args.file}:")
    print("  messages: " + (", ".join(f"{k} {v}" for k, v in sorted(result["messages"].items())) or "none"))
    print(f"  RXM-RAWX epochs: {result['rawx_epochs']}, measurements: {result['measurements']}, "
          f"with carrier phase: {result['carrier_phase_valid']}")
    for system, n in result["by_system"].items():
        print(f"    {system}: {n['measurements']} measurements, {n['carrier_phase_valid']} with carrier phase")
    for label in ("first_epoch", "last_epoch"):
        e = result[label]
        if e:
            print(f"  {label.replace('_', ' ')}: GPS week {e['week']} tow {e['tow_s']} s"
                  + (f" ({e['utc']})" if e.get("utc") else ""))
    if result["bad_checksums"] or result["skipped_bytes"]:
        print(f"  bad checksums: {result['bad_checksums']}, bytes outside messages: {result['skipped_bytes']}")
    if result["partial_last_bytes"]:
        print(f"  cut-off last message: {result['partial_last_bytes']} bytes, ignored")
    print("  carrier phase: " + ("yes" if ok else "NO (no RXM-RAWX epoch with a valid carrier phase)"))
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m radar_runner.gpsraw", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    log = sub.add_parser("log", help="log the receiver's UBX messages from gpsd to a file")
    log.add_argument("--out", required=True)
    log.add_argument("--keep", default="", help="comma-separated message names to keep (default: all UBX)")
    log.add_argument("--host", default="127.0.0.1")
    log.add_argument("--port", type=int, default=2947)
    log.add_argument("--fsync-s", type=float, default=10.0)
    log.add_argument("--reconnect-s", type=float, default=1.0)
    log.add_argument("--max-s", type=float, default=0.0, help="stop after this long (0: until signalled)")
    log.add_argument("--parent-pid", type=int, default=0, help="the process that started it: exit when it is gone")
    chk = sub.add_parser("check", help="count what a log holds; exit status 0 if it has carrier phase")
    chk.add_argument("file")
    chk.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return logger.main(args) if args.cmd == "log" else check(args)


if __name__ == "__main__":
    sys.exit(main())
