"""One full pass over every destination: search, record, alert."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import discord, search
from .config import Config, Destination
from .store import SentAlert, Store, utcnow

log = logging.getLogger(__name__)

ERROR_NOTICE_EVERY = timedelta(hours=24)


def should_alert(price: float, target: float, last: SentAlert | None, now: datetime,
                 realert_after_days: float, min_drop: float) -> bool:
    """Alert when under target, unless we already told you about this price recently."""
    if price > target:
        return False
    if last is None:
        return True
    if price <= last.price - min_drop:
        return True  # meaningfully cheaper than the last alert
    return now - last.sent_at >= timedelta(days=realert_after_days)


@dataclass
class DestinationResult:
    code: str
    best: search.FareQuote | None = None
    alerted: bool = False
    error: str | None = None


def scan_destination(config: Config, store: Store, dest: Destination, *, dry_run: bool = False,
                     today: date | None = None) -> DestinationResult:
    result = DestinationResult(dest.code)
    started = utcnow()

    if not search.departure_dates(dest.trip, today):
        result.error = "travel window is in the past"
        log.info("%s: skipped, its travel window is in the past", dest.code)
        return result

    quotes = search.sweep_dates(config, dest, today)
    if not quotes:
        log.info("%s: no flights matched your filters", dest.code)
        return result

    store.record_prices(dest.code, [(q.depart, q.ret, q.price) for q in quotes], config.currency, started)
    best = min(quotes, key=lambda q: (q.price, q.depart))
    result.best = best
    log.info("%s: cheapest %s on %s -> %s (target %s)", dest.code, discord.money(best.price, config.currency),
             best.depart, best.ret, discord.money(dest.max_price, config.currency))

    last = store.last_alert(dest.code)
    if not should_alert(best.price, dest.max_price, last, started, config.alerts.realert_after_days,
                        config.alerts.min_drop_to_realert):
        return result

    itinerary = None
    try:
        itinerary = search.cheapest_itinerary(config, dest, best.depart, best.ret)
    except Exception as exc:  # noqa: BLE001 - still alert with the price we have
        log.warning("%s: flight details lookup failed: %s", dest.code, exc)

    price = best.price
    if itinerary is not None:
        price = itinerary.price
        if price > dest.max_price:
            log.info("%s: details came back at %s, over target; not alerting", dest.code,
                     discord.money(price, config.currency))
            return result
        if not should_alert(price, dest.max_price, last, started, config.alerts.realert_after_days,
                            config.alerts.min_drop_to_realert):
            return result

    embed = discord.deal_embed(
        origins=config.origins, dest=dest.code, adults=dest.trip.adults, depart=best.depart, ret=best.ret,
        price=price, target=dest.max_price, currency=config.currency,
        previous_low=store.lowest_before(dest.code, started), itinerary=itinerary,
        link=search.google_flights_link(config, dest, best.depart, best.ret),
    )
    if dry_run:
        log.info("%s: DRY RUN, would alert:\n%s", dest.code, embed)
        return result
    if discord.send(config.discord_webhook, [embed]):
        store.save_alert(dest.code, price, best.depart, best.ret, started)
        result.alerted = True
    return result


def run_scan(config: Config, store: Store, *, dry_run: bool = False, only: set[str] | None = None,
             today: date | None = None) -> list[DestinationResult]:
    search.configure_politeness(config.scanner.requests_per_second)
    destinations = [d for d in config.destinations if not only or d.code in only]
    results = []
    for i, dest in enumerate(destinations):
        if i:
            time.sleep(random.uniform(*config.scanner.pause_between_destinations))
        try:
            results.append(scan_destination(config, store, dest, dry_run=dry_run, today=today))
        except Exception as exc:  # noqa: BLE001 - keep going with the next destination
            log.error("%s: search failed: %s", dest.code, exc)
            results.append(DestinationResult(dest.code, error=f"{type(exc).__name__}: {exc}"))

    failed = [r for r in results if r.error and r.error != "travel window is in the past"]
    if failed and len(failed) == len(results) and config.alerts.notify_on_errors and not dry_run:
        _maybe_warn(config, store, failed)
    elif not failed:
        store.set_meta("last_error_notice", "")
    return results


def _maybe_warn(config: Config, store: Store, failed: list[DestinationResult]) -> None:
    """Tell Discord once a day if every search is failing (e.g. Google started blocking)."""
    last = store.get_meta("last_error_notice")
    now = utcnow()
    if last and now - datetime.fromisoformat(last) < ERROR_NOTICE_EVERY:
        return
    lines = "\n".join(f"• {r.code}: {r.error[:200]}" for r in failed[:10])
    text = (f"Every search in the last scan failed:\n{lines}\n\n"
            "If this keeps happening, try `pip install -U flights` in the Flightwatch venv. "
            "Google may have changed something, or be rate-limiting this IP.")
    if discord.send(config.discord_webhook, [discord.warning_embed("Flightwatch scan failed", text)]):
        store.set_meta("last_error_notice", now.isoformat())
