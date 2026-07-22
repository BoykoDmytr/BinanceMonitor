"""Зведені метрики: знімок символу, погодинна сезонність, теплокарта.

Спирається на чисті формули з metrics.py. Київський час використовується
лише для групування сезонності/теплокарти — усе зберігання лишається в UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from . import metrics
from .config import Config
from .tz import KYIV


def kyiv_dow_hour(ms: int) -> tuple[int, int]:
    """UTC мілісекунди → (день тижня 0=Пн, година 0..23) за київським часом."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(KYIV)
    return dt.weekday(), dt.hour


def snapshot(closes, highs, lows, quote_volumes, open_times,
             asks, bids, cfg: Config) -> dict:
    """Повний знімок метрик символу з закритих свічок і поточного стакана.

    Повертає поля схеми metrics + допоміжні (для UI). Персистентність стану
    «спокійно» (K хвилин поспіль) рахує движок — тут лише миттєвий calm_now.
    """
    closes = np.asarray(closes, dtype=float)
    last_close = float(closes[-1]) if closes.size else float("nan")

    # 1. Волатильність + перцентиль
    sig = metrics.sigma(closes, window=cfg.window)
    sig_bps = sig * metrics.BPS
    sig_pct = metrics.percentile_rank(sig, metrics.rolling_sigma(closes, cfg.window))

    # 3. Стиснення діапазону + перцентиль
    range_bps = metrics.range_compression(highs, lows, closes, cfg.window)
    range_pct = metrics.percentile_rank(
        range_bps, metrics.rolling_range_compression(highs, lows, closes, cfg.window)
    )

    # 4. ATR
    atr = metrics.atr_bps(highs, lows, closes, cfg.atr_period)

    # 5. Обсяг + dead_market
    vol_ratio = metrics.volume_ratio(quote_volumes, cfg.window)
    last_qv = float(np.asarray(quote_volumes, dtype=float)[-1]) if len(quote_volumes) else float("nan")
    dead = metrics.is_dead_market(quote_volumes, last_qv, cfg.dead_market_pct)

    # 6. Погодинна сезонність
    seas_ratio = seasonality_ratio(open_times, closes, sig, cfg)

    # 7. Вартість круговороту зі стакана
    rt = metrics.roundtrip(asks, bids, cfg.notional_usdt)

    # 8. Ризик дрейфу
    drift = metrics.drift_bps(sig, cfg.hold_seconds)

    # 9. Підсумок
    if rt.insufficient_depth:
        total = float("nan")
    else:
        total = metrics.total_bps(rt.roundtrip_bps, drift)

    # 10. Миттєвий «спокій» (без витримки K хвилин — її додає движок)
    calm_now = bool(
        not np.isnan(sig_pct) and sig_pct < cfg.calm_sigma_pct
        and not np.isnan(range_pct) and range_pct < cfg.calm_range_pct
        and not dead
        and not rt.insufficient_depth
        and rt.roundtrip_bps < cfg.calm_roundtrip_bps
    )

    return {
        "price": last_close,
        "sigma": sig,
        "sigma_bps": _clean(sig_bps),
        "sigma_pct": _clean(sig_pct),
        "range_bps": _clean(range_bps),
        "range_pct": _clean(range_pct),
        "atr_bps": _clean(atr),
        "vol_ratio": _clean(vol_ratio),
        "dead_market": bool(dead),
        "half_spread_bps": _clean(rt.half_spread_bps),
        "roundtrip_bps": _clean(rt.roundtrip_bps),
        "drift_bps": _clean(drift),
        "total_bps": _clean(total),
        "total_usd": _clean(metrics.bps_to_usd(total, cfg.notional_usdt)),
        "insufficient_depth": bool(rt.insufficient_depth),
        "insufficient_side": rt.side,
        "seasonality_ratio": _clean(seas_ratio),
        "calm_now": calm_now,
        "qty": _clean(rt.qty),
        "proceeds": _clean(rt.proceeds),
    }


def seasonality_ratio(open_times, closes, current_sigma: float, cfg: Config) -> float:
    """σ_зараз / σ_медіанна для поточної (день, година) за київським часом.

    Без цього детектор спокою перетворюється на годинник. Повертає NaN,
    якщо історії для цього відрізка ще немає.
    """
    closes = np.asarray(closes, dtype=float)
    roll = metrics.rolling_sigma(closes, cfg.window)
    if roll.size == 0 or np.isnan(current_sigma):
        return float("nan")

    # Елемент roll[j] відповідає вікну closes[j : j+window]; остання свічка —
    # open_times[j + window - 1]
    offset = cfg.window - 1
    buckets: dict[tuple[int, int], list[float]] = {}
    for j, val in enumerate(roll):
        ot = open_times[offset + j]
        buckets.setdefault(kyiv_dow_hour(int(ot)), []).append(float(val))

    now_bucket = kyiv_dow_hour(int(open_times[-1]))
    vals = buckets.get(now_bucket)
    if not vals:
        return float("nan")
    med = float(np.median(vals))
    if med == 0:
        return float("nan")
    return current_sigma / med


def heatmap_total_bps(metric_rows) -> list[list[float | None]]:
    """Теплокарта: медіана total_bps × година київського часу × день тижня.

    Приймає рядки таблиці metrics (з полями ts, total_bps). Повертає сітку
    7×24 (день × година), None де даних немає.
    """
    grid: list[list[list[float]]] = [[[] for _ in range(24)] for _ in range(7)]
    for row in metric_rows:
        tb = row["total_bps"]
        if tb is None:
            continue
        dow, hour = kyiv_dow_hour(int(row["ts"]))
        grid[dow][hour].append(float(tb))
    return [
        [float(np.median(cell)) if cell else None for cell in day]
        for day in grid
    ]


def _clean(x):
    """NaN/inf → None для чистого JSON (щоб фронт не отримував NaN)."""
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if np.isnan(xf) or np.isinf(xf):
        return None
    return xf
