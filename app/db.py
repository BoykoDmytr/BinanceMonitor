"""Сховище SQLite через stdlib sqlite3.

Час скрізь у UTC мілісекундах. Конвертація в київський час — тільки в UI.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .metrics import (
    K_OPEN_TIME, K_OPEN, K_HIGH, K_LOW, K_CLOSE,
    K_VOLUME, K_QUOTE_VOLUME, K_TRADES,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
  symbol TEXT, open_time INTEGER, open REAL, high REAL, low REAL, close REAL,
  volume REAL, quote_volume REAL, trades INTEGER,
  PRIMARY KEY (symbol, open_time)
);
CREATE TABLE IF NOT EXISTS metrics (
  symbol TEXT, ts INTEGER, sigma_bps REAL, sigma_pct REAL, range_bps REAL,
  range_pct REAL, atr_bps REAL, vol_ratio REAL, dead_market INTEGER,
  half_spread_bps REAL, roundtrip_bps REAL, drift_bps REAL, total_bps REAL,
  calm INTEGER, PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, symbol TEXT, ts INTEGER, kind TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS watchlist (
  symbol TEXT PRIMARY KEY, added_ms INTEGER
);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Відкриває з'єднання та вмикає WAL для файлових БД."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ─────────────────────────── klines ───────────────────────────

def upsert_klines(conn: sqlite3.Connection, symbol: str, rows: list[list]) -> int:
    """Записує сирі масиви klines. INSERT OR IGNORE — не перезаписує наявне.

    Саме тому повторний backfill не «перезаливає» те, що вже є в БД.
    Повертає кількість реально доданих рядків.
    """
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO klines "
        "(symbol, open_time, open, high, low, close, volume, quote_volume, trades) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                symbol,
                int(r[K_OPEN_TIME]),
                float(r[K_OPEN]), float(r[K_HIGH]), float(r[K_LOW]), float(r[K_CLOSE]),
                float(r[K_VOLUME]), float(r[K_QUOTE_VOLUME]), int(r[K_TRADES]),
            )
            for r in rows
        ],
    )
    return conn.total_changes - before


def count_klines(conn: sqlite3.Connection, symbol: str, start_ms: int, end_ms: int) -> int:
    """Кількість свічок символу з open_time у [start_ms, end_ms)."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM klines WHERE symbol=? AND open_time>=? AND open_time<?",
        (symbol, start_ms, end_ms),
    )
    return int(cur.fetchone()[0])


def kline_range(conn: sqlite3.Connection, symbol: str) -> tuple[int | None, int | None, int]:
    """(min_open_time, max_open_time, count) для символу."""
    cur = conn.execute(
        "SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM klines WHERE symbol=?",
        (symbol,),
    )
    row = cur.fetchone()
    return row[0], row[1], int(row[2])


def get_klines(conn: sqlite3.Connection, symbol: str, limit: int | None = None) -> list[sqlite3.Row]:
    """Свічки символу за зростанням часу (за потреби — останні `limit`)."""
    if limit is not None:
        cur = conn.execute(
            "SELECT * FROM (SELECT * FROM klines WHERE symbol=? "
            "ORDER BY open_time DESC LIMIT ?) ORDER BY open_time ASC",
            (symbol, limit),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM klines WHERE symbol=? ORDER BY open_time ASC", (symbol,)
        )
    return cur.fetchall()


# ─────────────────────────── metrics ───────────────────────────

def upsert_metrics(conn: sqlite3.Connection, symbol: str, ts: int, m: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO metrics (symbol, ts, sigma_bps, sigma_pct, range_bps,"
        " range_pct, atr_bps, vol_ratio, dead_market, half_spread_bps, roundtrip_bps,"
        " drift_bps, total_bps, calm) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            symbol, ts,
            m.get("sigma_bps"), m.get("sigma_pct"), m.get("range_bps"),
            m.get("range_pct"), m.get("atr_bps"), m.get("vol_ratio"),
            int(bool(m.get("dead_market"))), m.get("half_spread_bps"),
            m.get("roundtrip_bps"), m.get("drift_bps"), m.get("total_bps"),
            int(bool(m.get("calm"))),
        ),
    )
    conn.commit()


def get_metrics_history(conn: sqlite3.Connection, symbol: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM metrics WHERE symbol=? ORDER BY ts ASC", (symbol,)
    )
    return cur.fetchall()


# ─────────────────────────── alerts ───────────────────────────

def add_alert(conn: sqlite3.Connection, symbol: str, ts: int, kind: str, payload: str) -> None:
    conn.execute(
        "INSERT INTO alerts (symbol, ts, kind, payload) VALUES (?,?,?,?)",
        (symbol, ts, kind, payload),
    )
    conn.commit()


def get_alerts(conn: sqlite3.Connection, symbol: str | None = None) -> list[sqlite3.Row]:
    if symbol:
        cur = conn.execute(
            "SELECT * FROM alerts WHERE symbol=? ORDER BY ts DESC", (symbol,)
        )
    else:
        cur = conn.execute("SELECT * FROM alerts ORDER BY ts DESC")
    return cur.fetchall()


# ─────────────────────────── watchlist ───────────────────────────

def add_to_watchlist(conn: sqlite3.Connection, symbol: str, added_ms: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (symbol, added_ms) VALUES (?,?)",
        (symbol, added_ms),
    )
    conn.commit()


def remove_from_watchlist(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
    conn.commit()


def get_watchlist(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT symbol FROM watchlist ORDER BY added_ms ASC")
    return [r[0] for r in cur.fetchall()]
