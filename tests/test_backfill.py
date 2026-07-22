"""Критерій 5: повторний backfill на заповненій БД не робить мережевих запитів."""

import asyncio

from app import backfill, db

MINUTE_MS = 60_000
DAY_MS = 86_400_000


class NoNetworkClient:
    """Клієнт, який падає при будь-якій спробі мережевого запиту."""

    def __init__(self):
        self.klines_calls = 0

    async def klines(self, symbol, interval, start_time, limit):
        self.klines_calls += 1
        raise AssertionError("Мережевий запит під час backfill на повній БД!")

    async def pace(self, min_pause=0.2):
        return None


def _fill_full_day(conn, symbol, start_ms, end_ms):
    """Заповнює кожну хвилину [start_ms, end_ms) синтетичними свічками."""
    rows = []
    t = start_ms
    price = 100.0
    while t < end_ms:
        rows.append([t, price, price, price, price, 1.0, t + MINUTE_MS - 1, 100.0, 1,
                     0.0, 0.0, "0"])
        t += MINUTE_MS
    db.upsert_klines(conn, symbol, rows)
    conn.commit()


def test_repeat_backfill_makes_no_network_calls():
    conn = db.connect(":memory:")
    db.init_db(conn)

    symbol = "BTCUSDT"
    end_ms = 1_700_000_400_000            # фіксована межа, кратна хвилині
    start_ms = end_ms - DAY_MS
    _fill_full_day(conn, symbol, start_ms, end_ms)

    # БД уже покриває весь діапазон → жодної сторінки для фетчу
    pages = backfill.pages_to_fetch(conn, symbol, start_ms, end_ms)
    assert pages == []

    client = NoNetworkClient()
    result = asyncio.run(
        backfill.backfill_symbol(conn, client, symbol, days=1, end_ms=end_ms)
    )

    assert client.klines_calls == 0
    assert result["pages"] == 0
    assert result["fetched"] == 0


def test_partial_db_requests_only_missing_pages():
    """Часткова БД: фетчаться лише сторінки з дірками (перевірка на sync-логіці)."""
    conn = db.connect(":memory:")
    db.init_db(conn)

    symbol = "ETHUSDT"
    end_ms = 1_700_000_400_000
    start_ms = end_ms - DAY_MS

    # Заповнюємо лише другу «сторінку» (після перших 1000 хвилин)
    page2_start = start_ms + 1000 * MINUTE_MS
    _fill_partial = []
    t = page2_start
    while t < end_ms:
        _fill_partial.append([t, 1, 1, 1, 1, 1.0, t + MINUTE_MS - 1, 1.0, 1, 0, 0, "0"])
        t += MINUTE_MS
    db.upsert_klines(conn, symbol, _fill_partial)
    conn.commit()

    pages = backfill.pages_to_fetch(conn, symbol, start_ms, end_ms)
    # Перша сторінка [start, start+1000хв) порожня → має бути в списку; друга — ні
    assert len(pages) == 1
    assert pages[0][0] == start_ms
