"""Command-line entry point: `uv run milemap ...`"""

from __future__ import annotations

import argparse
import logging
import sys


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

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every request URL

    if args.command == "probe" and args.probe == "history-depth":
        from milemap.sources.fuel_finder.probe import run_probe

        out = run_probe(quick=args.quick, max_batches_big=args.max_batches)
        print(out)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
