"""Load and validate config.toml.

The config is re-read at the start of every scan, so edits (by hand or from
Discord) take effect on the next run with no restart.
"""

from __future__ import annotations

import difflib
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
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_CODE_RE = re.compile(r"^[A-Z]{3}$")
_WEBHOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")


TOP_KEYS = ("origins", "currency", "trip", "destinations", "alerts", "scanner", "database")
TRIP_KEYS = ("adults", "cabin", "max_stops", "min_layover_minutes", "max_layover_minutes", "earliest_departure",
             "latest_departure", "latest_return", "depart_weekdays", "nights")
ALERT_KEYS = ("realert_after_days", "min_drop_to_realert", "notify_on_errors")
SCANNER_KEYS = ("paused", "requests_per_second", "pause_between_destinations", "detail_candidates")


class ConfigError(ValueError):
    """The config file is missing, unreadable or has a bad value."""


def _table(raw: object, allowed: tuple[str, ...], where: str) -> dict:
    """A TOML table with only known keys, so a typo is an error instead of being silently ignored."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table")
    for key in raw:
        if key not in allowed:
            hint = difflib.get_close_matches(key, allowed, n=1)
            raise ConfigError(f"{where}: unknown setting {key!r}" + (f" (did you mean {hint[0]!r}?)" if hint else ""))
    return raw


def _bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {value!r} must be true or false")
    return value


@dataclass(frozen=True)
class Trip:
    adults: int
    cabin: str
    max_stops: int
    min_layover_minutes: int | None
    max_layover_minutes: int | None
    earliest_departure: date
    latest_departure: date | None
    latest_return: date | None
    depart_weekdays: tuple[int, ...]  # 0 = Monday; empty = any day
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
    paused: bool = False
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
    path: Path | None = None
    discord_webhook: str | None = field(default=None, repr=False)
    trip: Trip | None = None  # the shared [trip] settings, before per-destination overrides


def airport_code(value: object, where: str = "airport") -> str:
    if not isinstance(value, str) or not _CODE_RE.match(value.strip().upper()):
        raise ConfigError(f"{where}: {value!r} is not a 3-letter airport code")
    code = value.strip().upper()
    from fli.models import Airport

    if code not in Airport.__members__:
        raise ConfigError(f"{where}: {code} is not an airport Google Flights knows")
    return code


def weekday(value: object, where: str = "weekday") -> int:
    if isinstance(value, str):
        key = value.strip().lower()[:3]
        if key in WEEKDAYS and WEEKDAY_NAMES[WEEKDAYS.index(key)].lower().startswith(value.strip().lower()):
            return WEEKDAYS.index(key)
    raise ConfigError(f"{where}: {value!r} is not a day of the week (use mon, tue, wed, thu, fri, sat, sun)")


def _date(value: object, where: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
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


_MISSING = object()


def _trip(raw: dict, where: str, base: Trip | None = None) -> Trip:
    _table(raw, TRIP_KEYS, where)

    def get(key, inherited):
        return raw[key] if key in raw else (inherited if base else _MISSING)

    def opt_int(key, inherited):
        v = get(key, inherited)
        return None if v in (_MISSING, None, 0) else _pos_int(v, f"{where}.{key}")

    adults = _pos_int(raw.get("adults", base.adults if base else 1), f"{where}.adults")
    if adults > 9:
        raise ConfigError(f"{where}.adults: Google Flights allows at most 9 travelers")

    cabin = str(raw.get("cabin", base.cabin if base else "economy")).lower()
    if cabin not in CABINS:
        raise ConfigError(f"{where}.cabin: must be one of {', '.join(CABINS)}")

    max_stops = _pos_int(raw.get("max_stops", base.max_stops if base else 1), f"{where}.max_stops", allow_zero=True)
    if max_stops > 2:
        raise ConfigError(f"{where}.max_stops: must be 0, 1 or 2")

    min_lay = opt_int("min_layover_minutes", base.min_layover_minutes if base else None)
    max_lay = opt_int("max_layover_minutes", base.max_layover_minutes if base else None)
    if min_lay and max_lay and min_lay > max_lay:
        raise ConfigError(f"{where}: min_layover_minutes is longer than max_layover_minutes")

    earliest = get("earliest_departure", base.earliest_departure if base else None)
    if earliest is _MISSING:
        raise ConfigError(f"{where}.earliest_departure is required")
    earliest = _date(earliest, f"{where}.earliest_departure")

    latest_dep = get("latest_departure", base.latest_departure if base else None)
    latest_dep = None if latest_dep in (_MISSING, None) else _date(latest_dep, f"{where}.latest_departure")
    latest_ret = get("latest_return", base.latest_return if base else None)
    latest_ret = None if latest_ret in (_MISSING, None) else _date(latest_ret, f"{where}.latest_return")
    if latest_dep is None and latest_ret is None:
        raise ConfigError(f"{where}: set latest_return (when you need to be home) or latest_departure")
    if latest_dep and latest_dep < earliest:
        raise ConfigError(f"{where}: latest_departure is before earliest_departure")
    if latest_ret and latest_ret <= earliest:
        raise ConfigError(f"{where}: latest_return must be after earliest_departure")

    days_raw = raw.get("depart_weekdays", list(WEEKDAYS[d] for d in base.depart_weekdays) if base else [])
    if isinstance(days_raw, str):
        days_raw = [days_raw]
    if not isinstance(days_raw, list):
        raise ConfigError(f'{where}.depart_weekdays: give a list like ["thu"], or [] for any day')
    depart_weekdays = tuple(sorted({weekday(d, f"{where}.depart_weekdays") for d in days_raw}))

    nights_raw = raw.get("nights", list(base.nights) if base else None)
    if isinstance(nights_raw, int):
        nights_raw = [nights_raw]
    if not isinstance(nights_raw, list) or not nights_raw:
        raise ConfigError(f"{where}.nights: give a list of trip lengths, like [6, 7, 8]")
    nights = tuple(sorted({_pos_int(n, f"{where}.nights") for n in nights_raw}))

    return Trip(adults, cabin, max_stops, min_lay, max_lay, earliest, latest_dep, latest_ret,
                depart_weekdays, nights)


def parse_config(raw: dict, path: Path | None = None) -> Config:
    _table(raw, TOP_KEYS, "config")
    origins_raw = raw.get("origins")
    if isinstance(origins_raw, str):
        origins_raw = [origins_raw]
    if not isinstance(origins_raw, list) or not origins_raw:
        raise ConfigError('origins: list your home airports, like origins = ["ORD", "MDW"]')
    origins = tuple(dict.fromkeys(airport_code(o, "origins") for o in origins_raw))

    currency = str(raw.get("currency", "USD")).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ConfigError("currency: must be a 3-letter code like USD")

    trip_raw = raw.get("trip")
    if not isinstance(trip_raw, dict):
        raise ConfigError("[trip] section is missing")
    trip = _trip(trip_raw, "trip")

    dest_raw = raw.get("destinations")
    if not isinstance(dest_raw, dict):
        raise ConfigError("[destinations] section is missing")
    destinations = []
    for key, value in dest_raw.items():
        code = airport_code(key, f"destinations.{key}")
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

    alerts_raw = _table(raw.get("alerts", {}), ALERT_KEYS, "[alerts]")
    alerts = Alerts(
        realert_after_days=float(alerts_raw.get("realert_after_days", Alerts.realert_after_days)),
        min_drop_to_realert=float(alerts_raw.get("min_drop_to_realert", Alerts.min_drop_to_realert)),
        notify_on_errors=_bool(alerts_raw.get("notify_on_errors", Alerts.notify_on_errors), "alerts.notify_on_errors"),
    )

    scan_raw = _table(raw.get("scanner", {}), SCANNER_KEYS, "[scanner]")
    pause = scan_raw.get("pause_between_destinations", list(Scanner.pause_between_destinations))
    if not (isinstance(pause, list) and len(pause) == 2 and 0 <= pause[0] <= pause[1]):
        raise ConfigError("scanner.pause_between_destinations: give [min_seconds, max_seconds]")
    scanner = Scanner(
        paused=_bool(scan_raw.get("paused", False), "scanner.paused"),
        requests_per_second=_pos_int(scan_raw.get("requests_per_second", 2), "scanner.requests_per_second"),
        pause_between_destinations=(float(pause[0]), float(pause[1])),
        detail_candidates=min(10, _pos_int(scan_raw.get("detail_candidates", 5), "scanner.detail_candidates")),
    )

    database = Path(os.environ.get("FLIGHTWATCH_DB") or raw.get("database", DEFAULT_DB_PATH))

    webhook = os.environ.get(WEBHOOK_ENV, "").strip() or None
    if webhook and not _WEBHOOK_RE.match(webhook):
        raise ConfigError(f"{WEBHOOK_ENV} does not look like a Discord webhook URL")

    return Config(origins, currency, tuple(destinations), alerts, scanner, database, path, webhook, trip)


def config_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("FLIGHTWATCH_CONFIG", DEFAULT_CONFIG_PATH))


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = config_path(path)
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    return parse_config(raw, path)


def search_plan(trip: Trip, today: date | None = None) -> dict[int, list[date]]:
    """For each trip length, the departure dates still worth searching."""
    start = max(trip.earliest_departure, (today or date.today()) + timedelta(days=1))
    plan: dict[int, list[date]] = {}
    for nights in trip.nights:
        last = trip.latest_departure or date.max
        if trip.latest_return:
            last = min(last, trip.latest_return - timedelta(days=nights))
        days = [start + timedelta(days=i) for i in range(max(0, (last - start).days + 1))]
        if trip.depart_weekdays:
            days = [d for d in days if d.weekday() in trip.depart_weekdays]
        plan[nights] = days
    return plan


def describe_trip(trip: Trip) -> str:
    """One readable paragraph describing what gets searched."""
    window = f"leave {trip.earliest_departure:%b %-d, %Y} or later"
    if trip.latest_return:
        window += f", home by {trip.latest_return:%b %-d, %Y}"
    if trip.latest_departure:
        window += f", leave by {trip.latest_departure:%b %-d, %Y}"
    nights = "/".join(str(n) for n in trip.nights)
    if trip.depart_weekdays:
        days = " or ".join(WEEKDAY_NAMES[d] for d in trip.depart_weekdays)
        if len(trip.depart_weekdays) == 1 and len(trip.nights) == 1:
            back = WEEKDAY_NAMES[(trip.depart_weekdays[0] + trip.nights[0]) % 7]
            pattern = f"{days} to {back} ({nights} nights)"
        else:
            pattern = f"leave on {days}, {nights} nights"
    else:
        pattern = f"any day, {nights} nights"
    stops = "nonstop only" if trip.max_stops == 0 else f"up to {trip.max_stops} stop{'s' if trip.max_stops > 1 else ''}"
    lay = []
    if trip.min_layover_minutes:
        lay.append(f"at least {fmt_minutes(trip.min_layover_minutes)}")
    if trip.max_layover_minutes:
        lay.append(f"at most {fmt_minutes(trip.max_layover_minutes)}")
    layovers = f", layovers {' and '.join(lay)}" if lay and trip.max_stops else ""
    travelers = f"{trip.adults} traveler{'s' if trip.adults > 1 else ''}"
    cabin = trip.cabin.replace("_", " ")
    return f"{window}\n{pattern}\n{travelers}, {cabin}, {stops}{layovers}"


def fmt_minutes(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m}m"
    return f"{h}h" if not m else f"{h}h {m:02d}m"


def estimate_requests(config: Config, today: date | None = None) -> int:
    """Rough count of Google page loads one full scan makes."""
    total = 0
    for dest in config.destinations:
        total += sum(len(days) for days in search_plan(dest.trip, today).values())
        total += 1 + config.scanner.detail_candidates  # detail lookup if it alerts
    return total
