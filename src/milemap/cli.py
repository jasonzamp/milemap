"""Command-line entry point: `uv run milemap ...`"""

from __future__ import annotations

import argparse
import logging
import sys


def _rotate_log(max_bytes: int = 5_000_000) -> None:
    """Keep data/logs/collector.log small (launchd reopens it on every run)."""
    from milemap.config import data_dir

    log_file = data_dir() / "logs" / "collector.log"
    try:
        if log_file.stat().st_size > max_bytes:
            log_file.replace(log_file.with_suffix(".log.1"))
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="milemap")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="One-off investigations")
    probe_sub = probe.add_subparsers(dest="probe", required=True)
    hd = probe_sub.add_parser("history-depth", help="Fuel Finder history-depth test")
    hd.add_argument("--quick", action="store_true",
                    help="Auth + format tests + first batch only (a few requests)")
    hd.add_argument("--max-batches", type=int, default=6000,
                    help="Cap for the since-launch run (500 records per batch)")
    hd.add_argument("--part", choices=("prices", "stations"), default="prices",
                    help="Which endpoints to probe (stations also runs the node_id join check)")

    collect = sub.add_parser("collect", help="The Fuel Finder collector")
    collect_sub = collect.add_subparsers(dest="collect", required=True)
    t = collect_sub.add_parser("tick", help="One collector run (scheduled every 5 minutes)")
    t.add_argument("--runner", default=None,
                   help="Label for the run record (default: $MILEMAP_RUNNER or 'manual')")
    g = collect_sub.add_parser("gaps", help="Coverage and gaps over recent days")
    g.add_argument("--days", type=int, default=7)

    r2 = sub.add_parser("r2", help="Cloudflare R2 raw store")
    r2_sub = r2.add_subparsers(dest="r2", required=True)
    up = r2_sub.add_parser("upload", help="Upload a local directory to R2 (write-once)")
    up.add_argument("--from", dest="src", default=None,
                    help="Directory to upload (default: data/outbox)")
    up.add_argument("--keep", action="store_true",
                    help="Leave files in place instead of moving them to data/raw")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every request URL

    if args.command == "probe" and args.probe == "history-depth":
        from milemap.sources.fuel_finder.probe import run_probe, run_station_probe

        if args.part == "stations":
            out = run_station_probe()
        else:
            out = run_probe(quick=args.quick, max_batches_big=args.max_batches)
        print(out)
        return 0

    if args.command == "collect":
        import json

        from milemap.collector.fuel_finder import gaps_report, tick

        if args.collect == "tick":
            _rotate_log()
            rec = tick(runner=args.runner)
            print(json.dumps({k: rec.get(k) for k in ("run_id", "status", "seconds", "gap")},
                             default=str))
            return 0 if rec.get("status") in ("ok", "partial", "skipped_cooldown",
                                               "skipped_locked") else 1
        print(json.dumps(gaps_report(args.days), indent=2))
        return 0

    if args.command == "r2" and args.r2 == "upload":
        from pathlib import Path

        from milemap.config import data_dir
        from milemap.storage.r2 import r2_client, upload_dir

        src = Path(args.src) if args.src else data_dir() / "outbox"
        client, bucket = r2_client()
        res = upload_dir(src.resolve(), client=client, bucket=bucket,
                         move_to=None if args.keep else data_dir() / "raw")
        print(res)
        return 0 if not res.conflicts and not res.errors else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
