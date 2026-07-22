"""Метрики ринку — точні формули з ТЗ.

Усе рахується по **закритих** 1-хвилинних свічках. Функції чисті:
приймають масиви/списки, не роблять I/O — саме тому їх легко тестувати
без мережі. Усе відносне повертається у **базисних пунктах**
(bps = частка × 10000).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

BPS = 10000.0

# ─────────────────────────── Парсинг свічок ───────────────────────────

# Структура елемента klines (масив із 12 полів):
#  0 openTime(ms) 1 open 2 high 3 low 4 close 5 volume(base)
#  6 closeTime(ms) 7 quoteVolume 8 trades 9 takerBuyBase 10 takerBuyQuote 11 ignore
K_OPEN_TIME, K_OPEN, K_HIGH, K_LOW, K_CLOSE = 0, 1, 2, 3, 4
K_VOLUME, K_CLOSE_TIME, K_QUOTE_VOLUME, K_TRADES = 5, 6, 7, 8


@dataclass
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float          # обсяг у базовій валюті
    close_time: int
    quote_volume: float    # обсяг у котирувальній валюті — НЕ плутати з volume
    trades: int


def parse_kline(row: list) -> Kline:
    """Розкладає сирий масив klines. volume (idx 5) ≠ quote_volume (idx 7)."""
    return Kline(
        open_time=int(row[K_OPEN_TIME]),
        open=float(row[K_OPEN]),
        high=float(row[K_HIGH]),
        low=float(row[K_LOW]),
        close=float(row[K_CLOSE]),
        volume=float(row[K_VOLUME]),
        close_time=int(row[K_CLOSE_TIME]),
        quote_volume=float(row[K_QUOTE_VOLUME]),
        trades=int(row[K_TRADES]),
    )


# ─────────────────────────── Волатильність ───────────────────────────

def log_returns(closes) -> np.ndarray:
    """Логарифмічні повернення ln(close_t / close_{t-1})."""
    c = np.asarray(closes, dtype=float)
    return np.diff(np.log(c))


def sigma(closes, window: int = 60) -> float:
    """σ = вибіркове стд лог-повернень за останні `window` закритих свічок.

    `window` свічок дають `window − 1` повернень. Повертає σ як частку
    (для bps домнож на 10000). ddof=1 — вибіркове стандартне відхилення.
    """
    c = np.asarray(closes, dtype=float)
    seg = c[-window:] if window else c
    r = np.diff(np.log(seg))
    if r.size < 2:
        return float("nan")
    return float(np.std(r, ddof=1))


def rolling_sigma(closes, window: int = 60) -> np.ndarray:
    """σ ковзним вікном по всій історії — розподіл для перцентиля.

    Вікно з `window` свічок = `window − 1` послідовних повернень.
    Векторизовано через sliding_window_view (швидко навіть на 30 днях).
    """
    r = log_returns(closes)
    m = window - 1
    if r.size < m or m < 2:
        return np.array([])
    sw = np.lib.stride_tricks.sliding_window_view(r, m)
    return sw.std(axis=1, ddof=1)


def percentile_rank(value: float, distribution) -> float:
    """У якому перцентилі (0..100) `value` перебуває всередині розподілу.

    Частка історичних значень ≤ value. Абсолютні пороги не використовуються —
    лише перцентилі всередині одного символу.
    """
    d = np.asarray(distribution, dtype=float)
    d = d[~np.isnan(d)]
    if d.size == 0 or np.isnan(value):
        return float("nan")
    return float(np.searchsorted(np.sort(d), value, side="right") / d.size * 100.0)


# ─────────────────────────── Стиснення діапазону ───────────────────────────

def range_compression(highs, lows, closes, window: int = 60) -> float:
    """(max(high) − min(low)) / median(close) за `window` свічок, у bps."""
    h = np.asarray(highs, dtype=float)[-window:]
    lo = np.asarray(lows, dtype=float)[-window:]
    c = np.asarray(closes, dtype=float)[-window:]
    if c.size == 0:
        return float("nan")
    med = np.median(c)
    if med == 0:
        return float("nan")
    return float((h.max() - lo.min()) / med * BPS)


def rolling_range_compression(highs, lows, closes, window: int = 60) -> np.ndarray:
    """Стиснення діапазону ковзним вікном — розподіл для перцентиля."""
    h = np.asarray(highs, dtype=float)
    lo = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    if c.size < window:
        return np.array([])
    hi_max = np.lib.stride_tricks.sliding_window_view(h, window).max(axis=1)
    lo_min = np.lib.stride_tricks.sliding_window_view(lo, window).min(axis=1)
    med = np.median(np.lib.stride_tricks.sliding_window_view(c, window), axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(med != 0, (hi_max - lo_min) / med * BPS, np.nan)
    return out


# ─────────────────────────── ATR ───────────────────────────

def true_range(highs, lows, closes) -> np.ndarray:
    """Класичний True Range: max(H−L, |H−C_prev|, |L−C_prev|)."""
    h = np.asarray(highs, dtype=float)
    lo = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    prev_close = c[:-1]
    hl = h[1:] - lo[1:]
    hc = np.abs(h[1:] - prev_close)
    lc = np.abs(lo[1:] - prev_close)
    return np.maximum.reduce([hl, hc, lc])


def atr_bps(highs, lows, closes, period: int = 14) -> float:
    """ATR(period) за згладжуванням Уайлдера, поділений на останній close, у bps."""
    tr = true_range(highs, lows, closes)
    if tr.size < period:
        return float("nan")
    # Ініціалізація простим середнім перших `period` TR, далі RMA Уайлдера
    atr = float(np.mean(tr[:period]))
    for t in tr[period:]:
        atr = (atr * (period - 1) + t) / period
    last_close = float(np.asarray(closes, dtype=float)[-1])
    if last_close == 0:
        return float("nan")
    return atr / last_close * BPS


# ─────────────────────────── Обсяг ───────────────────────────

def volume_ratio(quote_volumes, window: int = 60) -> float:
    """Відношення quoteVolume останньої свічки до медіани за `window` свічок."""
    qv = np.asarray(quote_volumes, dtype=float)[-window:]
    if qv.size == 0:
        return float("nan")
    med = np.median(qv)
    if med == 0:
        return float("nan")
    return float(qv[-1] / med)


def is_dead_market(quote_volumes, current: float, pct: float = 5.0) -> bool:
    """Низький обсяг — це не спокій, а неліквідність.

    True, якщо `current` нижчий за `pct`-й перцентиль історії обсягу.
    """
    qv = np.asarray(quote_volumes, dtype=float)
    qv = qv[~np.isnan(qv)]
    if qv.size == 0 or np.isnan(current):
        return False
    threshold = float(np.percentile(qv, pct))
    return current < threshold


# ─────────────────────────── Ризик дрейфу ───────────────────────────

def drift_bps(sigma_fraction: float, hold_seconds: float) -> float:
    """drift_bps = σ · sqrt(hold/60) · 10000 · 0.8.

    0.8 ≈ sqrt(2/π) — матсподівання модуля нормального відхилення.
    σ тут — частка (не bps).
    """
    if np.isnan(sigma_fraction):
        return float("nan")
    return sigma_fraction * np.sqrt(hold_seconds / 60.0) * BPS * 0.8


# ─────────────────────────── Вартість круговороту зі стакана ───────────────────────────

@dataclass
class Roundtrip:
    insufficient_depth: bool
    qty: float = float("nan")           # куплена кількість базового активу
    proceeds: float = float("nan")      # виручка M від продажу назад, USDT
    roundtrip_bps: float = float("nan")
    half_spread_bps: float = float("nan")
    mid: float = float("nan")
    side: str = ""                       # де забракло глибини: "asks"/"bids"


def _to_levels(raw) -> list[tuple[float, float]]:
    """[["100","1"], ...] → [(100.0, 1.0), ...]. Рядки з depth стають float."""
    return [(float(p), float(q)) for p, q in raw]


def roundtrip(asks_raw, bids_raw, notional: float) -> Roundtrip:
    """Вартість круговороту N USDT: купити по asks, продати по bids.

    buy(N):   іти по asks, витрачаючи N USDT → отримана кількість qty
    sell(qty):іти по bids, продаючи qty → виручка M
    roundtrip_bps = (N − M) / N × 10000
    half_spread_bps = (ask[0] − bid[0]) / 2 / mid × 10000

    Якщо глибини не вистачає — НЕ екстраполюємо, повертаємо insufficient_depth.
    """
    asks = _to_levels(asks_raw)
    bids = _to_levels(bids_raw)
    if not asks or not bids:
        return Roundtrip(insufficient_depth=True, side="asks" if not asks else "bids")

    # buy(N): витрачаємо notional по asks (за зростанням ціни)
    remaining = float(notional)
    qty = 0.0
    for price, q in asks:
        level_value = price * q
        if level_value <= remaining:
            qty += q
            remaining -= level_value
        else:
            qty += remaining / price
            remaining = 0.0
            break
    if remaining > 1e-9:  # asks закінчились, а N не витрачено повністю
        return Roundtrip(insufficient_depth=True, side="asks")

    # sell(qty): продаємо qty по bids (за спаданням ціни)
    rem_qty = qty
    proceeds = 0.0
    for price, q in bids:
        if q <= rem_qty:
            proceeds += price * q
            rem_qty -= q
        else:
            proceeds += price * rem_qty
            rem_qty = 0.0
            break
    if rem_qty > 1e-9:  # bids закінчились, а qty не продано повністю
        return Roundtrip(insufficient_depth=True, side="bids")

    best_ask = asks[0][0]
    best_bid = bids[0][0]
    mid = (best_ask + best_bid) / 2.0
    return Roundtrip(
        insufficient_depth=False,
        qty=qty,
        proceeds=proceeds,
        roundtrip_bps=(notional - proceeds) / notional * BPS,
        half_spread_bps=(best_ask - best_bid) / 2.0 / mid * BPS,
        mid=mid,
    )


# ─────────────────────────── Підсумок ───────────────────────────

def total_bps(roundtrip_bps: float, drift: float) -> float:
    """total_bps = roundtrip_bps + drift_bps."""
    return roundtrip_bps + drift


def bps_to_usd(bps: float, notional: float) -> float:
    """Перевід bps у USD для заданого розміру круговороту."""
    return bps / BPS * notional
