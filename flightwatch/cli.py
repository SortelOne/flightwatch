"""Command line: `flightwatch scan | status | check-config | test-discord`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import discord
from .config import ConfigError, estimate_requests, load_config
from .search import departure_dates
from .store import Store


def _cmd_check(config, _args) -> int:
    print(f"Origins:      {', '.join(config.origins)}")
    print(f"Currency:     {config.currency}")
    print(f"Database:     {config.database}")
    print(f"Discord:      {'webhook set' if config.discord_webhook else 'NOT SET (alerts will only be logged)'}")
    print("Destinations:")
    for d in config.destinations:
        t = d.trip
        days = len(departure_dates(t))
        print(f"  {d.code}  max {discord.money(d.max_price, config.currency)} for {t.adults}, "
              f"leave {t.earliest_departure}..{t.latest_departure} ({days} dates left), "
              f"nights {list(t.nights)}, <= {t.max_stops} stop(s)"
              + (f", layovers <= {t.max_layover_minutes} min" if t.max_layover_minutes else ""))
    print(f"About {estimate_requests(config)} Google page loads per scan.")
    return 0


def _cmd_status(config, _args) -> int:
    store = Store(config.database)
    try:
        print(f"{'DEST':<5} {'TARGET':>9} {'LATEST':>9} {'LOWEST':>9}  LOWEST DATES             LAST ALERT")
        for d in config.destinations:
            s = store.stats(d.code)
            a = store.last_alert(d.code)
            fmt = lambda v: discord.money(v, config.currency) if v is not None else "-"  # noqa: E731
            dates = f"{s.lowest_ever_dates[0]} -> {s.lowest_ever_dates[1]}" if s.lowest_ever_dates else "-"
            alert = f"{fmt(a.price)} on {a.sent_at:%Y-%m-%d}" if a else "-"
            print(f"{d.code:<5} {fmt(d.max_price):>9} {fmt(s.latest_price):>9} {fmt(s.lowest_ever):>9}  "
                  f"{dates:<24} {alert}")
    finally:
        store.close()
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
    from .scan import run_scan

    only = {c.upper() for c in args.only} if args.only else None
    store = Store(config.database)
    try:
        results = run_scan(config, store, dry_run=args.dry_run, only=only, today=date.today())
    finally:
        store.close()
    errors = [r for r in results if r.error and r.error != "travel window is in the past"]
    alerts = sum(r.alerted for r in results)
    logging.getLogger("flightwatch").info("scan done: %d destinations, %d alerts, %d errors",
                                          len(results), alerts, len(errors))
    return 1 if errors and len(errors) == len(results) else 0


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
