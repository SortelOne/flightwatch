"""Command line: `flightwatch scan | status | check-config | test-discord`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import discord
from .config import ConfigError, describe_trip, estimate_requests, load_config
from .store import Store


def _cmd_check(config, _args) -> int:
    print(f"Config:       {config.path}")
    print(f"Origins:      {', '.join(config.origins)}")
    print(f"Currency:     {config.currency}")
    print(f"Database:     {config.database}")
    print(f"Discord:      {'webhook set' if config.discord_webhook else 'NOT SET (alerts will only be logged)'}")
    if config.scanner.paused:
        print("Scanning:     PAUSED")
    print("Destinations:")
    for d in config.destinations:
        print(f"  {d.code}  max {discord.money(d.max_price, config.currency)}")
        print("        " + describe_trip(d.trip).replace("\n", "\n        "))
    print(f"About {estimate_requests(config)} Google page loads per scan.")
    return 0


def status_rows(config) -> list[tuple[str, ...]]:
    """Destination, target, latest, lowest, lowest dates, last alert."""
    store = Store(config.database)
    rows = []
    try:
        for d in config.destinations:
            s = store.stats(d.code)
            a = store.last_alert(d.code)
            fmt = lambda v: discord.money(v, config.currency) if v is not None else "-"  # noqa: E731
            dates = f"{s.lowest_ever_dates[0]:%b %-d}-{s.lowest_ever_dates[1]:%b %-d}" if s.lowest_ever_dates else "-"
            alert = f"{fmt(a.price)} {a.sent_at:%b %-d}" if a else "-"
            rows.append((d.code, fmt(d.max_price), fmt(s.latest_price), fmt(s.lowest_ever), dates, alert))
    finally:
        store.close()
    return rows


def format_status(config) -> str:
    header = ("DEST", "TARGET", "LATEST", "LOWEST", "LOWEST DATES", "LAST ALERT")
    rows = [header] + status_rows(config)
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def _cmd_status(config, _args) -> int:
    print(format_status(config))
    return 0


def _cmd_test_discord(config, _args) -> int:
    if not config.discord_webhook:
        print("FLIGHTWATCH_DISCORD_WEBHOOK is not set.", file=sys.stderr)
        return 1
    ok = discord.send(config.discord_webhook, [discord.info_embed(
        "Flightwatch is connected 👋",
        f"Watching {', '.join(d.code for d in config.destinations)} from {'/'.join(config.origins)}.")])
    print("Sent!" if ok else "Sending failed; see the log above.")
    return 0 if ok else 1


def _cmd_scan(config, args) -> int:
    from .scan import ScanBusy, failures, run_scan, scan_lock

    only = {c.upper() for c in args.only} if args.only else None
    if only:
        unknown = sorted(only - {d.code for d in config.destinations})
        if unknown:
            print(f"Not in [destinations]: {', '.join(unknown)}", file=sys.stderr)
            return 2
    try:
        with scan_lock(config.database):
            store = Store(config.database)
            try:
                results = run_scan(config, store, dry_run=args.dry_run, only=only, today=date.today(),
                                   force=bool(only or args.dry_run))
            finally:
                store.close()
    except ScanBusy:
        logging.getLogger("flightwatch").info("another scan is already running; skipping this one")
        return 0
    errors, all_failed = failures(results)
    alerts = sum(r.alerted for r in results)
    logging.getLogger("flightwatch").info("scan done: %d destinations, %d alerts, %d errors",
                                          len(results), alerts, len(errors))
    return 1 if all_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flightwatch", description="Watch Google Flights for cheap fares.")
    parser.add_argument("-c", "--config", help="config file (default: $FLIGHTWATCH_CONFIG or "
                                               "/etc/flightwatch/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="search every destination once and send alerts")
    scan.add_argument("--dry-run", action="store_true", help="search and log, but don't send to Discord")
    scan.add_argument("--only", nargs="+", metavar="CODE", help="only scan these destinations")
    sub.add_parser("status", help="show prices seen so far")
    sub.add_parser("check-config", help="validate the config and show what will be searched")
    sub.add_parser("test-discord", help="send a test message to the Discord webhook")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("fli").setLevel(logging.ERROR)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config problem: {exc}", file=sys.stderr)
        return 2

    return {"scan": _cmd_scan, "status": _cmd_status, "check-config": _cmd_check,
            "test-discord": _cmd_test_discord}[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
