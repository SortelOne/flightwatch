"""Load and validate config.toml.

The config is re-read at the start of every scan, so editing the destination
list takes effect on the next run with no restart.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

DEFAULT_CONFIG_PATH = "/etc/flightwatch/config.toml"
DEFAULT_DB_PATH = "/var/lib/flightwatch/flightwatch.db"
WEBHOOK_ENV = "FLIGHTWATCH_DISCORD_WEBHOOK"

CABINS = ("economy", "premium_economy", "business", "first")
_CODE_RE = re.compile(r"^[A-Z]{3}$")
_WEBHOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")


class ConfigError(ValueError):
    """The config file is missing, unreadable or has a bad value."""


@dataclass(frozen=True)
class Trip:
    adults: int
    cabin: str
    max_stops: int
    max_layover_minutes: int | None
    earliest_departure: date
    latest_departure: date
    nights: tuple[int, ...]


@dataclass(frozen=True)
class Destination:
    code: str
    max_price: float
    trip: Trip


@dataclass(frozen=True)
class Alerts:
    realert_after_days: float = 3
    min_drop_to_realert: float = 25
    notify_on_errors: bool = True


@dataclass(frozen=True)
class Scanner:
    requests_per_second: int = 2
    pause_between_destinations: tuple[float, float] = (5, 20)
    detail_candidates: int = 5


@dataclass(frozen=True)
class Config:
    origins: tuple[str, ...]
    currency: str
    destinations: tuple[Destination, ...]
    alerts: Alerts
    scanner: Scanner
    database: Path
    discord_webhook: str | None = field(default=None, repr=False)


def _code(value: object, where: str) -> str:
    if not isinstance(value, str) or not _CODE_RE.match(value.strip().upper()):
        raise ConfigError(f"{where}: {value!r} is not a 3-letter airport code")
    code = value.strip().upper()
    # Imported lazily so `check-config` errors stay readable if fli is missing.
    from fli.models import Airport

    if code not in Airport.__members__:
        raise ConfigError(f"{where}: {code} is not an airport Google Flights knows")
    return code


def _date(value: object, where: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError(f"{where}: {value!r} is not a date like 2027-03-15")


def _pos_int(value: object, where: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ConfigError(f"{where}: {value!r} must be a whole number{' >= 0' if allow_zero else ' > 0'}")
    return value


def _price(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where}: max price {value!r} must be a positive number")
    return float(value)


def _trip(raw: dict, where: str, base: Trip | None = None) -> Trip:
    def get(key, default):
        return raw[key] if key in raw else default

    adults = _pos_int(get("adults", base.adults if base else 1), f"{where}.adults")
    if adults > 9:
        raise ConfigError(f"{where}.adults: Google Flights allows at most 9 travelers")

    cabin = str(get("cabin", base.cabin if base else "economy")).lower()
    if cabin not in CABINS:
        raise ConfigError(f"{where}.cabin: must be one of {', '.join(CABINS)}")

    max_stops = _pos_int(get("max_stops", base.max_stops if base else 1), f"{where}.max_stops", allow_zero=True)
    if max_stops > 2:
        raise ConfigError(f"{where}.max_stops: must be 0, 1 or 2")

    layover = get("max_layover_minutes", base.max_layover_minutes if base else None)
    if layover is not None:
        layover = _pos_int(layover, f"{where}.max_layover_minutes")

    if base is None and ("earliest_departure" not in raw or "latest_departure" not in raw):
        raise ConfigError(f"{where}: earliest_departure and latest_departure are required")
    earliest = _date(get("earliest_departure", base.earliest_departure if base else None), f"{where}.earliest_departure")
    latest = _date(get("latest_departure", base.latest_departure if base else None), f"{where}.latest_departure")
    if latest < earliest:
        raise ConfigError(f"{where}: latest_departure is before earliest_departure")

    nights_raw = get("nights", list(base.nights) if base else None)
    if isinstance(nights_raw, int):
        nights_raw = [nights_raw]
    if not isinstance(nights_raw, list) or not nights_raw:
        raise ConfigError(f"{where}.nights: give a list of trip lengths, like [6, 7, 8]")
    nights = tuple(sorted({_pos_int(n, f"{where}.nights") for n in nights_raw}))

    return Trip(adults, cabin, max_stops, layover, earliest, latest, nights)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("FLIGHTWATCH_CONFIG", DEFAULT_CONFIG_PATH))
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None

    origins_raw = raw.get("origins")
    if isinstance(origins_raw, str):
        origins_raw = [origins_raw]
    if not isinstance(origins_raw, list) or not origins_raw:
        raise ConfigError('origins: list your home airports, like origins = ["ORD", "MDW"]')
    origins = tuple(dict.fromkeys(_code(o, "origins") for o in origins_raw))

    currency = str(raw.get("currency", "USD")).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ConfigError("currency: must be a 3-letter code like USD")

    trip_raw = raw.get("trip")
    if not isinstance(trip_raw, dict):
        raise ConfigError("[trip] section is missing")
    trip = _trip(trip_raw, "trip")

    dest_raw = raw.get("destinations")
    if not isinstance(dest_raw, dict) or not dest_raw:
        raise ConfigError("[destinations] is empty: add lines like LIS = 1400")
    destinations = []
    for key, value in dest_raw.items():
        code = _code(key, f"destinations.{key}")
        if code in origins:
            raise ConfigError(f"destinations.{code}: that is one of your origins")
        where = f"destinations.{code}"
        if isinstance(value, dict):
            if "max_price" not in value:
                raise ConfigError(f"{where}: needs max_price, like {code} = {{ max_price = 1400 }}")
            overrides = {k: v for k, v in value.items() if k != "max_price"}
            destinations.append(Destination(code, _price(value["max_price"], where), _trip(overrides, where, trip)))
        else:
            destinations.append(Destination(code, _price(value, where), trip))

    alerts_raw = raw.get("alerts", {})
    alerts = Alerts(
        realert_after_days=float(alerts_raw.get("realert_after_days", Alerts.realert_after_days)),
        min_drop_to_realert=float(alerts_raw.get("min_drop_to_realert", Alerts.min_drop_to_realert)),
        notify_on_errors=bool(alerts_raw.get("notify_on_errors", Alerts.notify_on_errors)),
    )

    scan_raw = raw.get("scanner", {})
    pause = scan_raw.get("pause_between_destinations", list(Scanner.pause_between_destinations))
    if not (isinstance(pause, list) and len(pause) == 2 and 0 <= pause[0] <= pause[1]):
        raise ConfigError("scanner.pause_between_destinations: give [min_seconds, max_seconds]")
    scanner = Scanner(
        requests_per_second=_pos_int(scan_raw.get("requests_per_second", 2), "scanner.requests_per_second"),
        pause_between_destinations=(float(pause[0]), float(pause[1])),
        detail_candidates=min(10, _pos_int(scan_raw.get("detail_candidates", 5), "scanner.detail_candidates")),
    )

    database = Path(os.environ.get("FLIGHTWATCH_DB") or raw.get("database", DEFAULT_DB_PATH))

    webhook = os.environ.get(WEBHOOK_ENV, "").strip() or None
    if webhook and not _WEBHOOK_RE.match(webhook):
        raise ConfigError(f"{WEBHOOK_ENV} does not look like a Discord webhook URL")

    return Config(origins, currency, tuple(destinations), alerts, scanner, database, webhook)


def departure_dates(trip: Trip, today: date | None = None) -> list[date]:
    """Departure dates still worth searching: tomorrow onward, inside the window."""
    start = max(trip.earliest_departure, (today or date.today()) + timedelta(days=1))
    return [start + timedelta(days=i) for i in range((trip.latest_departure - start).days + 1)]


def estimate_requests(config: Config, today: date | None = None) -> int:
    """Rough count of Google page loads one full scan makes."""
    total = 0
    for dest in config.destinations:
        total += len(departure_dates(dest.trip, today)) * len(dest.trip.nights)
        total += 1 + config.scanner.detail_candidates  # detail lookup if it alerts
    return total
