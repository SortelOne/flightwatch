"""Discord webhook messages."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date

from .search import Direction, Itinerary

log = logging.getLogger(__name__)

GREEN = 0x2ECC71
ORANGE = 0xE67E22
BLUE = 0x3498DB


def money(amount: float, currency: str) -> str:
    if currency == "USD":
        return f"${amount:,.0f}"
    return f"{amount:,.0f} {currency}"


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def describe(direction: Direction) -> str:
    first, last = direction.legs[0], direction.legs[-1]
    flights = ", ".join(f"{leg.airline} {leg.flight_number}" for leg in direction.legs)
    stops = "nonstop" if direction.stops == 0 else (
        "1 stop" if direction.stops == 1 else f"{direction.stops} stops")
    lines = [
        f"{first.origin} {first.departs:%a %b %-d %-I:%M %p} → {last.destination} {last.arrives:%a %b %-d %-I:%M %p}",
        f"{flights} · {stops} · {_hm(direction.duration_minutes)}",
    ]
    for leg, wait in zip(direction.legs, direction.layover_minutes):
        lines.append(f"Layover in {leg.destination}: {_hm(wait)}")
    return "\n".join(lines)


def deal_embed(*, origins: tuple[str, ...], dest: str, adults: int, depart: date, ret: date, price: float,
               target: float, currency: str, previous_low: float | None, itinerary: Itinerary | None,
               link: str) -> dict:
    nights = (ret - depart).days
    fields = [
        {"name": "Dates", "value": f"{depart:%a %b %-d} → {ret:%a %b %-d, %Y} ({nights} nights)", "inline": False},
        {"name": "Price", "value": (
            f"**{money(price, currency)}** round trip for {adults}"
            + (f" (about {money(price / adults, currency)} each)" if adults > 1 else "")
            + f"\nYour target: {money(target, currency)}"
            + (f"\nLowest seen before: {money(previous_low, currency)}" if previous_low is not None else "")
        ), "inline": False},
    ]
    if itinerary:
        fields.append({"name": "Outbound", "value": describe(itinerary.outbound), "inline": False})
        fields.append({"name": "Return", "value": describe(itinerary.inbound), "inline": False})
    else:
        fields.append({"name": "Flights", "value": "Open the link to see the flights for these dates.",
                       "inline": False})
    return {
        "title": f"✈️ {'/'.join(origins)} → {dest} for {money(price, currency)}",
        "url": link,
        "color": GREEN,
        "fields": fields,
        "footer": {"text": "Prices change fast. Check the link before booking."},
    }


def warning_embed(title: str, text: str) -> dict:
    return {"title": f"⚠️ {title}", "description": text[:4000], "color": ORANGE}


def info_embed(title: str, text: str) -> dict:
    return {"title": title, "description": text[:4000], "color": BLUE}


def send(webhook_url: str | None, embeds: list[dict], *, retries: int = 3) -> bool:
    """Post embeds to a Discord webhook. Returns False if it couldn't be sent."""
    if not webhook_url:
        log.info("no Discord webhook set; would have sent: %s", json.dumps(embeds, default=str)[:2000])
        return False
    body = json.dumps({"username": "Flightwatch", "embeds": embeds[:10]}).encode()
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "flightwatch (self-hosted, 1.0)"},
        method="POST",
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=20):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                try:
                    wait = float(json.loads(exc.read() or b"{}").get("retry_after", 2))
                except (ValueError, AttributeError):
                    wait = 2.0
                time.sleep(min(wait, 30))
                continue
            log.error("Discord rejected the message: HTTP %s", exc.code)
            return False
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            log.error("could not reach Discord: %s", exc)
            return False
    return False
