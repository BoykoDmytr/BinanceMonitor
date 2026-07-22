"""Backfill 30 днів 1-хвилинних свічок.

Цикл по startTime з limit=1000, наступний старт = closeTime останньої + 1.
Повторний запуск НЕ перезавантажує наявне: сторінки, вже повністю присутні
в БД, пропускаються без жодного мережевого запиту (INSERT OR IGNORE + перевірка
покриття перед фетчем).
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable, Protocol

from . import db

MINUTE_MS = 60_000
DAY_MS = 86_400_000
PAGE_MINUTES = 1000  # limit=1000 свічок на сторінку


class KlineSource(Protocol):
    async def klines(self, symbol: str, interval: str,
                     start_time: int | None, limit: int) -> list[list]: ...
    async def pace(self, min_pause: float = ...) -> None: ...


def aligned_end_ms(now_ms: int) -> int:
    """Межа останньої закритої хвилини (open_time поточної хвилини)."""
    return (now_ms // MINUTE_MS) * MINUTE_MS


def pages_to_fetch(conn, symbol: str, start_ms: int, end_ms: int,
                   page_minutes: int = PAGE_MINUTES) -> list[tuple[int, int]]:
    """Список [start, end) сторінок, які ще НЕ повністю присутні в БД.

    Порожній список означає, що діапазон уже покрито — фетчити нема чого.
    """
    step = page_minutes * MINUTE_MS
    pages: list[tuple[int, int]] = []
    s = start_ms
    while s < end_ms:
        e = min(s + step, end_ms)
        expected = (e - s) // MINUTE_MS
        have = db.count_klines(conn, symbol, s, e)
        if have < expected:
            pages.append((s, e))
        s = e
    return pages


async def backfill_symbol(
    conn,
    client: KlineSource,
    symbol: str,
    *,
    days: int = 30,
    end_ms: int | None = None,
    now_ms: int | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> dict:
    """Дотягує `days` днів 1-хв свічок для символу.

    on_progress(done_pages, total_pages, fetched_candles) — прогрес для UI.
    Повертає підсумок {pages, fetched, start, end}.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if end_ms is None:
        end_ms = aligned_end_ms(now_ms)
    start_ms = end_ms - days * DAY_MS

    pages = pages_to_fetch(conn, symbol, start_ms, end_ms)
    total = len(pages)
    fetched = 0

    for i, (page_start, page_end) in enumerate(pages):
        rows = await client.klines(symbol, "1m", page_start, PAGE_MINUTES)
        if not rows:
            break
        # Не виходити за межу сторінки/діапазону
        rows = [r for r in rows if page_start <= int(r[0]) < end_ms]
        added = db.upsert_klines(conn, symbol, rows)
        conn.commit()
        fetched += added
        if on_progress:
            on_progress(i + 1, total, fetched)
        # Наступний старт визначається сіткою сторінок; пауза з огляду на вагу
        await client.pace()

    return {"pages": total, "fetched": fetched, "start": start_ms, "end": end_ms}
