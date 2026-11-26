"""Everything that talks to Google Flights, through the `fli` library.

`fli` reads Google Flights' own search pages. It is unofficial, so a change on
Google's side can break it until the library is updated (`pip install -U flights`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import Config, Destination, Trip, search_plan

log = logging.getLogger(__name__)

# fli caps one date sweep at 93 dates; stay a little under it.
MAX_DATES_PER_SWEEP = 90


@dataclass(frozen=True)
class FareQuote:
    """Cheapest round-trip price Google showed for one pair of dates."""

    depart: date
    ret: date
    price: float


@dataclass(frozen=True)
class Leg:
    airline: str
    flight_number: str
    origin: str
    destination: str
    departs: datetime
    arrives: datetime


@dataclass(frozen=True)
class Direction:
    """One direction of the trip (outbound or return)."""

    legs: tuple[Leg, ...]
    layover_minutes: tuple[int, ...]
    duration_minutes: int

    @property
    def stops(self) -> int:
        return len(self.legs) - 1


@dataclass(frozen=True)
class Itinerary:
    price: float
    outbound: Direction
    inbound: Direction


def configure_politeness(requests_per_second: int) -> None:
    """Slow fli down from its default 10 requests/second burst."""
    try:
        import fli.search.client as fli_client
        from fli.search._concurrency import configure_concurrency

        fli_client.client = fli_client.Client(calls_per_second=requests_per_second)
        configure_concurrency(max(1, requests_per_second))
    except (ImportError, AttributeError, TypeError) as exc:  # internals moved in a newer fli
        log.warning("could not lower fli's request rate (%s); using its defaults", exc)


def _fli_filters(origins: tuple[str, ...], dest: str, trip: Trip, depart: date, ret: date):
    from fli.models import (
        Airport,
        FlightSegment,
        LayoverRestrictions,
        MaxStops,
        PassengerInfo,
        SeatType,
    )

    here = [[Airport[o], 0] for o in origins]
    there = [[Airport[dest], 0]]
    segments = [
        FlightSegment(departure_airport=here, arrival_airport=there, travel_date=depart.isoformat()),
        FlightSegment(departure_airport=there, arrival_airport=here, travel_date=ret.isoformat()),
    ]
    stops = {0: MaxStops.NON_STOP, 1: MaxStops.ONE_STOP_OR_FEWER, 2: MaxStops.TWO_OR_FEWER_STOPS}[trip.max_stops]
    layovers = None
    if trip.max_stops and (trip.min_layover_minutes or trip.max_layover_minutes):
        layovers = LayoverRestrictions(min_duration=trip.min_layover_minutes, max_duration=trip.max_layover_minutes)
    return dict(
        passenger_info=PassengerInfo(adults=trip.adults),
        flight_segments=segments,
        stops=stops,
        seat_type=SeatType[trip.cabin.upper()],
        layover_restrictions=layovers,
    )


def _runs(days: list[date]) -> list[list[date]]:
    """Split dates into runs of consecutive days, each short enough for one sweep."""
    runs: list[list[date]] = []
    for day in days:
        if runs and day - runs[-1][-1] == timedelta(days=1) and len(runs[-1]) < MAX_DATES_PER_SWEEP:
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def sweep_dates(config: Config, dest: Destination, today: date | None = None) -> list[FareQuote]:
    """Cheapest price for every departure date x trip length in the plan."""
    from fli.models import DateSearchFilters, TripType
    from fli.search import SearchDates

    quotes: list[FareQuote] = []
    errors: list[Exception] = []
    for nights, days in search_plan(dest.trip, today).items():
        for run in _runs(days):
            first, last = run[0], run[-1]
            filters = DateSearchFilters(
                trip_type=TripType.ROUND_TRIP,
                from_date=first.isoformat(),
                to_date=last.isoformat(),
                duration=nights,
                **_fli_filters(config.origins, dest.code, dest.trip, first, first + timedelta(days=nights)),
            )
            try:
                results = SearchDates().search(filters, currency=config.currency) or []
            except Exception as exc:  # noqa: BLE001
                if not quotes and not errors:
                    # The very first search failed, so Google is likely unreachable or blocking
                    # us. Stop here instead of retrying every other date for this destination.
                    raise
                log.warning("%s, %d nights, %s..%s failed: %s", dest.code, nights, first, last, exc)
                errors.append(exc)
                continue
            for r in results:
                dep = r.date[0].date()
                ret = r.date[1].date() if len(r.date) > 1 else dep + timedelta(days=nights)
                quotes.append(FareQuote(dep, ret, float(r.price)))
    if not quotes and errors:
        raise errors[-1]
    return quotes


def _direction(flight) -> Direction:
    legs = tuple(
        Leg(
            airline=getattr(leg.airline, "name", str(leg.airline)).removeprefix("_"),
            flight_number=str(leg.flight_number),
            origin=leg.departure_airport.name,
            destination=leg.arrival_airport.name,
            departs=leg.departure_datetime,
            arrives=leg.arrival_datetime,
        )
        for leg in flight.legs
    )
    if flight.layovers:
        layovers = tuple(int(lay.duration) for lay in flight.layovers)
    else:
        layovers = tuple(
            int((b.departs - a.arrives).total_seconds() // 60) for a, b in zip(legs, legs[1:])
        )
    return Direction(legs, layovers, int(flight.duration))


def fits(direction: Direction, trip: Trip) -> bool:
    """Double-check Google's own stop and layover filtering."""
    if direction.stops > trip.max_stops:
        return False
    if trip.max_layover_minutes and any(m > trip.max_layover_minutes for m in direction.layover_minutes):
        return False
    if trip.min_layover_minutes and any(m < trip.min_layover_minutes for m in direction.layover_minutes):
        return False
    return True


def cheapest_itinerary(config: Config, dest: Destination, depart: date, ret: date) -> Itinerary | None:
    """Look up actual flights for the winning dates, cheapest first."""
    from fli.models import FlightSearchFilters, SortBy, TripType
    from fli.search import SearchFlights

    filters = FlightSearchFilters(
        trip_type=TripType.ROUND_TRIP,
        sort_by=SortBy.CHEAPEST,
        **_fli_filters(config.origins, dest.code, dest.trip, depart, ret),
    )
    combos = SearchFlights().search(filters, top_n=config.scanner.detail_candidates, currency=config.currency) or []
    best: Itinerary | None = None
    for combo in combos:
        if not isinstance(combo, tuple) or len(combo) != 2:
            continue
        out_f, back_f = combo
        # Google puts the whole round-trip price on the outbound row.
        price = out_f.price if out_f.price is not None else back_f.price
        if price is None:
            continue
        it = Itinerary(float(price), _direction(out_f), _direction(back_f))
        if fits(it.outbound, dest.trip) and fits(it.inbound, dest.trip) and (best is None or it.price < best.price):
            best = it
    return best


def google_flights_link(config: Config, dest: Destination, depart: date, ret: date) -> str:
    """A link that opens this exact search (travelers, stops, layovers) on Google Flights."""
    try:
        from fli.models import FlightSearchFilters, TripType
        from fli.search._tfs import build_tfs, page_url

        filters = FlightSearchFilters(
            trip_type=TripType.ROUND_TRIP, **_fli_filters(config.origins, dest.code, dest.trip, depart, ret)
        )
        return page_url(build_tfs(filters), currency=config.currency)
    except Exception:  # noqa: BLE001 - fall back to the simple public link builder
        from fli.core.links import google_flights_url

        return google_flights_url(config.origins[0], dest.code, depart.isoformat(), ret.isoformat(),
                                  currency=config.currency)
