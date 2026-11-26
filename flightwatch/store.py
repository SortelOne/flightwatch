"""SQLite storage for price history and alerts already sent."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY,
    scanned_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prices_dest ON prices (destination, scanned_at);

CREATE TABLE IF NOT EXISTS alerts (
    destination TEXT PRIMARY KEY,
    price REAL NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SentAlert:
    destination: str
    price: float
    depart_date: date
    return_date: date
    sent_at: datetime


@dataclass(frozen=True)
class DestinationStats:
    destination: str
    lowest_ever: float | None
    lowest_ever_dates: tuple[date, date] | None
    latest_price: float | None
    latest_scan: datetime | None


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def record_prices(self, destination: str, rows: list[tuple[date, date, float]], currency: str,
                      when: datetime | None = None) -> None:
        when_s = (when or utcnow()).isoformat()
        with self.db:
            self.db.executemany(
                "INSERT INTO prices (scanned_at, destination, depart_date, return_date, price, currency)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(when_s, destination, d.isoformat(), r.isoformat(), p, currency) for d, r, p in rows],
            )

    def lowest_before(self, destination: str, before: datetime) -> float | None:
        """Lowest price seen for a destination in scans before `before`."""
        row = self.db.execute(
            "SELECT MIN(price) FROM prices WHERE destination = ? AND scanned_at < ?",
            (destination, before.isoformat()),
        ).fetchone()
        return row[0]

    def last_alert(self, destination: str) -> SentAlert | None:
        row = self.db.execute(
            "SELECT destination, price, depart_date, return_date, sent_at FROM alerts WHERE destination = ?",
            (destination,),
        ).fetchone()
        if not row:
            return None
        return SentAlert(row[0], row[1], date.fromisoformat(row[2]), date.fromisoformat(row[3]),
                         datetime.fromisoformat(row[4]))

    def save_alert(self, destination: str, price: float, depart: date, ret: date,
                   when: datetime | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO alerts (destination, price, depart_date, return_date, sent_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(destination) DO UPDATE SET price = excluded.price,"
                " depart_date = excluded.depart_date, return_date = excluded.return_date, sent_at = excluded.sent_at",
                (destination, price, depart.isoformat(), ret.isoformat(), (when or utcnow()).isoformat()),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def stats(self, destination: str) -> DestinationStats:
        low = self.db.execute(
            "SELECT price, depart_date, return_date FROM prices WHERE destination = ?"
            " ORDER BY price ASC, scanned_at DESC LIMIT 1",
            (destination,),
        ).fetchone()
        latest = self.db.execute(
            "SELECT MAX(scanned_at) FROM prices WHERE destination = ?", (destination,)
        ).fetchone()[0]
        latest_price = None
        if latest:
            latest_price = self.db.execute(
                "SELECT MIN(price) FROM prices WHERE destination = ? AND scanned_at = ?", (destination, latest)
            ).fetchone()[0]
        return DestinationStats(
            destination,
            low[0] if low else None,
            (date.fromisoformat(low[1]), date.fromisoformat(low[2])) if low else None,
            latest_price,
            datetime.fromisoformat(latest) if latest else None,
        )
