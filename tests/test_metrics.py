"""Критерії приймання — математика без мережі, на синтетичних фікстурах."""

import math

import numpy as np

from app import metrics


# ── 1. Вартість круговороту ──────────────────────────────────────────
def test_roundtrip_reference_numbers():
    """Дано asks/bids і N=200 → qty≈1.909091, M≈180.818182, roundtrip≈959.09."""
    asks = [["100", "1"], ["110", "10"]]
    bids = [["99", "1"], ["90", "10"]]
    rt = metrics.roundtrip(asks, bids, notional=200)

    assert rt.insufficient_depth is False
    assert abs(rt.qty - 1.909091) < 0.01
    assert abs(rt.proceeds - 180.818182) < 0.01
    assert abs(rt.roundtrip_bps - 959.09) < 0.01

    # half_spread = (100 − 99)/2 / 99.5 × 10000
    expected_half = (100 - 99) / 2 / 99.5 * 10000
    assert abs(rt.half_spread_bps - expected_half) < 1e-6


# ── 2. insufficient_depth ────────────────────────────────────────────
def test_insufficient_depth_when_asks_shallow():
    """Сумарна глибина asks (100 USDT) менша за N=200 → insufficient_depth."""
    asks = [["100", "1"]]                 # усього 100 USDT глибини
    bids = [["99", "1"], ["90", "10"]]
    rt = metrics.roundtrip(asks, bids, notional=200)

    assert rt.insufficient_depth is True
    assert rt.side == "asks"
    assert math.isnan(rt.roundtrip_bps)


def test_insufficient_depth_when_bids_shallow():
    """Глибини bids не вистачає, щоб продати куплену кількість."""
    asks = [["100", "5"]]
    bids = [["99", "0.1"]]
    rt = metrics.roundtrip(asks, bids, notional=200)
    assert rt.insufficient_depth is True
    assert rt.side == "bids"


# ── 3. σ на синтетичному ряді ────────────────────────────────────────
def test_sigma_matches_known_stdev():
    """Ряд із відомим стд лог-повернень → σ збігається в межах 1e-9."""
    known_returns = np.linspace(-0.012, 0.017, 59)      # 59 повернень → 60 свічок
    closes = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(known_returns)]))

    got = metrics.sigma(closes, window=len(closes))
    expected = float(np.std(known_returns, ddof=1))

    assert abs(got - expected) < 1e-9


# ── 4. Парсер klines ─────────────────────────────────────────────────
def test_parse_kline_does_not_confuse_volume_and_quote_volume():
    """volume (idx 5) і quoteVolume (idx 7) не переплутані."""
    row = [
        1_784_736_180_000,   # 0 openTime
        "218.98",            # 1 open
        "219.79",            # 2 high
        "218.50",            # 3 low
        "219.41",            # 4 close
        "12.5",              # 5 volume (base)  ← НЕ quoteVolume
        1_784_736_239_999,   # 6 closeTime
        "2743.21",           # 7 quoteVolume    ← НЕ volume
        7,                   # 8 trades
        "6.0",               # 9 takerBuyBase
        "1300.0",            # 10 takerBuyQuote
        "0",                 # 11 ignore
    ]
    k = metrics.parse_kline(row)

    assert k.open_time == 1_784_736_180_000
    assert k.close_time == 1_784_736_239_999
    assert k.open == 218.98
    assert k.high == 219.79
    assert k.low == 218.50
    assert k.close == 219.41
    assert k.volume == 12.5           # base
    assert k.quote_volume == 2743.21  # quote
    assert k.volume != k.quote_volume
    assert k.trades == 7
