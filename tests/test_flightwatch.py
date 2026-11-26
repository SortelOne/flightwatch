from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from flightwatch import discord, scan, search
from flightwatch.config import ConfigError, departure_dates, estimate_requests, load_config
from flightwatch.store import SentAlert, Store

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


BASE = """
origins = ["ord", "MDW"]
[trip]
adults = 2
max_stops = 1
max_layover_minutes = 180
earliest_departure = "2027-03-01"
latest_departure = "2027-03-10"
nights = [7, 6, 7]
[destinations]
LIS = 1400
NRT = { max_price = 1800, nights = 12, max_stops = 0 }
"""


# --- config -----------------------------------------------------------------

def test_example_config_loads(monkeypatch):
    monkeypatch.delenv("FLIGHTWATCH_DISCORD_WEBHOOK", raising=False)
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.origins == ("ORD", "MDW")
    assert [d.code for d in cfg.destinations] == ["LIS", "CUN", "HNL"]
    assert cfg.destinations[0].trip.max_layover_minutes == 180


def test_overrides_and_normalising(tmp_path, monkeypatch):
    monkeypatch.delenv("FLIGHTWATCH_DISCORD_WEBHOOK", raising=False)
    cfg = load_config(write(tmp_path, BASE))
    lis, nrt = cfg.destinations
    assert cfg.origins == ("ORD", "MDW")
    assert lis.trip.nights == (6, 7)
    assert nrt.max_price == 1800 and nrt.trip.nights == (12,) and nrt.trip.max_stops == 0
    assert nrt.trip.adults == 2 and nrt.trip.max_layover_minutes == 180  # inherited


@pytest.mark.parametrize("bad, message", [
    ('[destinations]\nXXX = 500', "not an airport"),
    ('[destinations]\nLIS = -5', "positive"),
    ('[destinations]\nLIS = { nights = 5 }', "max_price"),
    ('[destinations]\nORD = 500', "one of your origins"),
])
def test_bad_destinations(tmp_path, bad, message):
    text = BASE.split("[destinations]")[0] + bad
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_dates_must_be_in_order(tmp_path):
    text = BASE.replace('latest_departure = "2027-03-10"', 'latest_departure = "2027-02-01"')
    with pytest.raises(ConfigError, match="before"):
        load_config(write(tmp_path, text))


def test_webhook_is_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHTWATCH_DISCORD_WEBHOOK", "https://evil.example.com/hook")
    with pytest.raises(ConfigError, match="Discord webhook"):
        load_config(write(tmp_path, BASE))
    monkeypatch.setenv("FLIGHTWATCH_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/123/abc-DEF_9")
    assert load_config(write(tmp_path, BASE)).discord_webhook.endswith("abc-DEF_9")


def test_departure_dates_skip_the_past(tmp_path, monkeypatch):
    monkeypatch.delenv("FLIGHTWATCH_DISCORD_WEBHOOK", raising=False)
    trip = load_config(write(tmp_path, BASE)).destinations[0].trip
    assert len(departure_dates(trip, today=date(2027, 1, 1))) == 10
    assert departure_dates(trip, today=date(2027, 3, 8)) == [date(2027, 3, 9), date(2027, 3, 10)]
    assert departure_dates(trip, today=date(2027, 4, 1)) == []


def test_estimate_requests(tmp_path, monkeypatch):
    monkeypatch.delenv("FLIGHTWATCH_DISCORD_WEBHOOK", raising=False)
    cfg = load_config(write(tmp_path, BASE))
    # LIS: 10 dates x 2 lengths + 6 detail; NRT: 10 x 1 + 6
    assert estimate_requests(cfg, today=date(2027, 1, 1)) == 20 + 6 + 10 + 6


# --- alert rules ------------------------------------------------------------

def alert(price, days_ago):
    return SentAlert("LIS", price, date(2027, 3, 1), date(2027, 3, 8), NOW - timedelta(days=days_ago))


@pytest.mark.parametrize("price, last, expected", [
    (1500, None, False),            # over target
    (1400, None, True),             # exactly at target
    (1200, None, True),
    (1190, alert(1200, 1), False),  # only $10 cheaper than the last alert, too soon
    (1175, alert(1200, 1), True),   # $25 cheaper
    (1250, alert(1200, 1), False),  # pricier than last alert
    (1250, alert(1200, 3), True),   # reminder after 3 days
])
def test_should_alert(price, last, expected):
    assert scan.should_alert(price, 1400, last, NOW, realert_after_days=3, min_drop=25) is expected


# --- itinerary checks -------------------------------------------------------

def direction(*layovers):
    t = datetime(2027, 3, 1, 8)
    legs = tuple(search.Leg("DL", str(100 + i), "AAA", "BBB", t, t) for i in range(len(layovers) + 1))
    return search.Direction(legs, tuple(layovers), 300)


def test_fits(tmp_path, monkeypatch):
    monkeypatch.delenv("FLIGHTWATCH_DISCORD_WEBHOOK", raising=False)
    trip = load_config(write(tmp_path, BASE)).destinations[0].trip
    assert search.fits(direction(), trip)
    assert search.fits(direction(180), trip)
    assert not search.fits(direction(181), trip)
    assert not search.fits(direction(60, 60), trip)


# --- store ------------------------------------------------------------------

def test_store_roundtrip():
    s = Store(":memory:")
    s.record_prices("LIS", [(date(2027, 3, 1), date(2027, 3, 8), 1300.0),
                            (date(2027, 3, 2), date(2027, 3, 9), 1250.0)], "USD", NOW - timedelta(days=1))
    s.record_prices("LIS", [(date(2027, 3, 1), date(2027, 3, 8), 1350.0)], "USD", NOW)
    st = s.stats("LIS")
    assert st.lowest_ever == 1250 and st.lowest_ever_dates == (date(2027, 3, 2), date(2027, 3, 9))
    assert st.latest_price == 1350
    assert s.lowest_before("LIS", NOW) == 1250
    assert s.last_alert("LIS") is None
    s.save_alert("LIS", 1250, date(2027, 3, 2), date(2027, 3, 9), NOW)
    s.save_alert("LIS", 1200, date(2027, 3, 2), date(2027, 3, 9), NOW)
    assert s.last_alert("LIS").price == 1200


# --- scan orchestration (search layer stubbed; this tests our logic, not Google) ---

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHTWATCH_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1/x")
    return load_config(write(tmp_path, BASE))


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(discord, "send", lambda url, embeds, **kw: out.append(embeds) or True)
    return out


def stub_search(monkeypatch, quotes, itinerary=None):
    monkeypatch.setattr(search, "sweep_dates", lambda c, d, today=None: quotes)
    monkeypatch.setattr(search, "cheapest_itinerary", lambda c, d, a, b: itinerary)
    monkeypatch.setattr(search, "google_flights_link", lambda c, d, a, b: "https://www.google.com/travel/flights")


def test_scan_alerts_once_then_stays_quiet(cfg, sent, monkeypatch):
    store = Store(":memory:")
    lis = cfg.destinations[0]
    stub_search(monkeypatch, [search.FareQuote(date(2027, 3, 3), date(2027, 3, 10), 1350),
                              search.FareQuote(date(2027, 3, 4), date(2027, 3, 11), 1399)])
    today = date(2027, 1, 1)
    assert scan.scan_destination(cfg, store, lis, today=today).alerted
    assert "$1,350" in sent[0][0]["title"]
    assert not scan.scan_destination(cfg, store, lis, today=today).alerted  # same price again
    assert len(sent) == 1


def test_scan_skips_when_details_are_over_target(cfg, sent, monkeypatch):
    store = Store(":memory:")
    out = direction(90)
    stub_search(monkeypatch, [search.FareQuote(date(2027, 3, 3), date(2027, 3, 10), 1350)],
                search.Itinerary(1500, out, out))
    assert not scan.scan_destination(cfg, store, cfg.destinations[0], today=date(2027, 1, 1)).alerted
    assert sent == []


def test_dry_run_sends_nothing(cfg, sent, monkeypatch):
    store = Store(":memory:")
    stub_search(monkeypatch, [search.FareQuote(date(2027, 3, 3), date(2027, 3, 10), 900)])
    scan.scan_destination(cfg, store, cfg.destinations[0], dry_run=True, today=date(2027, 1, 1))
    assert sent == [] and store.last_alert("LIS") is None


def test_all_failures_warn_once_a_day(cfg, sent, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("blocked")

    monkeypatch.setattr(search, "sweep_dates", boom)
    monkeypatch.setattr(search, "configure_politeness", lambda n: None)
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    store = Store(":memory:")
    scan.run_scan(cfg, store, today=date(2027, 1, 1))
    scan.run_scan(cfg, store, today=date(2027, 1, 1))
    assert len(sent) == 1 and "failed" in sent[0][0]["title"]


# --- discord formatting -----------------------------------------------------

def test_deal_embed_reads_well():
    out = search.Direction(
        (search.Leg("DL", "1234", "ORD", "JFK", datetime(2027, 3, 3, 7, 5), datetime(2027, 3, 3, 13, 20)),
         search.Leg("TP", "208", "JFK", "LIS", datetime(2027, 3, 3, 15, 0), datetime(2027, 3, 4, 3, 0))),
        (100,), 900)
    e = discord.deal_embed(origins=("ORD", "MDW"), dest="LIS", adults=2, depart=date(2027, 3, 3),
                           ret=date(2027, 3, 10), price=1280, target=1400, currency="USD", previous_low=1310,
                           itinerary=search.Itinerary(1280, out, out), link="https://example.com")
    text = str(e)
    assert e["title"] == "✈️ ORD/MDW → LIS for $1,280"
    assert "about $640 each" in text and "Lowest seen before: $1,310" in text
    assert "DL 1234, TP 208 · 1 stop · 15h 00m" in text and "Layover in JFK: 1h 40m" in text
