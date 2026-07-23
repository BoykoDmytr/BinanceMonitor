"""Логіка сповіщень (calm + низький total) без мережі й без реального Telegram."""

import asyncio
import os

from app import db
from app.config import Config


def _monitor(cfg):
    os.environ["BMM_TG_TOKEN"] = "123:DUMMY"
    os.environ["BMM_TG_CHAT"] = "@dummy"
    from app.engine import Monitor
    conn = db.connect(":memory:")
    return Monitor(cfg, conn=conn), conn


METR = {
    "sigma_bps": 4.0, "sigma_pct": 10, "roundtrip_bps": 1.0, "total_bps": 2.0,
    "total_usd": 0.05, "drift_bps": 1.0, "price": 100.0,
}


def _mbps(bps):
    """Метрики з заданим total_bps (і похідним total_usd на 256 USDT)."""
    return {"total_bps": bps, "total_usd": bps / 10000 * 256}


def _musd(usd):
    return {"total_usd": usd, "total_bps": usd / 256 * 10000}


def test_low_total_edge_trigger_with_hysteresis():
    cfg = Config()
    cfg.notify_low_total_bps = 5.0
    m, conn = _monitor(cfg)
    sym = "TESTUSDT"

    # Перше падіння нижче порога → подія
    ev = m._update_low_total(sym, _mbps(2.0))
    assert ev and ev["kind"] == "low_total" and ev["unit"] == "bps"
    # Лишається низьким → без повторної події (не спамимо)
    assert m._update_low_total(sym, _mbps(2.0)) is None
    # Піднявся вище гістерезису (5 × 1.5 = 7.5) → переозброєння, без події
    assert m._update_low_total(sym, _mbps(8.0)) is None
    # Знову падіння → нова подія
    ev2 = m._update_low_total(sym, _mbps(1.0))
    assert ev2 and ev2["kind"] == "low_total"


def test_low_total_usd_threshold_takes_priority():
    cfg = Config()
    cfg.notify_low_total_bps = 5.0     # має ігноруватись, бо задано $-поріг
    cfg.notify_low_total_usd = 0.035
    m, _ = _monitor(cfg)
    sym = "TESTUSDT"

    # Вартість $0.11 (total 4.3 bps) — за bps було б «низько», а за $ — ні
    assert m._update_low_total(sym, _mbps(4.3)) is None
    # Вартість $0.03 (≤ 0.035) → подія у $-режимі
    ev = m._update_low_total(sym, _musd(0.030))
    assert ev and ev["unit"] == "usd" and ev["threshold"] == 0.035


def test_low_total_disabled_when_thresholds_zero():
    cfg = Config()
    cfg.notify_low_total_bps = 0.0
    cfg.notify_low_total_usd = 0.0
    m, _ = _monitor(cfg)
    assert m._update_low_total("TESTUSDT", _mbps(0.1)) is None


def test_calm_enter_after_k_minutes():
    cfg = Config()
    cfg.calm_minutes = 3
    m, _ = _monitor(cfg)
    sym = "TESTUSDT"
    active = False
    event = None
    for i in range(3):
        active, event = m._update_calm(sym, 1_700_000_000_000 + i * 60_000, True)
    assert active is True
    assert event and event["kind"] == "calm_enter"


def test_notify_respects_per_symbol_flag():
    cfg = Config()
    m, conn = _monitor(cfg)
    sym = "TESTUSDT"
    db.add_to_watchlist(conn, sym, 0)

    sent = []
    async def fake_send(text):
        sent.append(text)
        return True
    m.notifier.send = fake_send

    ev = {"kind": "low_total", "total_bps": 2.0, "threshold": 5.0}
    # Дзвіночок увімкнено (за замовчуванням) → повідомлення йде
    asyncio.run(m._notify(sym, [ev], METR, 1_700_000_000_000))
    assert len(sent) == 1 and "TESTUSDT" in sent[0]

    # Вимкнули сповіщення для символу → нічого не шлемо
    sent.clear()
    m.set_notify(sym, False)
    asyncio.run(m._notify(sym, [ev], METR, 1_700_000_000_000))
    assert sent == []


def test_event_formatting():
    cfg = Config()
    m, _ = _monitor(cfg)
    sym = "TESTUSDT"
    enter = m._format_event(sym, {"kind": "calm_enter"}, METR, 1_700_000_000_000)
    assert "спокій" in enter.lower() and "TESTUSDT" in enter
    exit_ = m._format_event(sym, {"kind": "calm_exit", "duration_ms": 600_000}, METR, 1_700_000_000_000)
    assert "вийшов" in exit_ and "10 хв" in exit_
    low = m._format_event(sym, {"kind": "low_total", "total_bps": 2.0, "threshold": 5.0},
                          METR, 1_700_000_000_000)
    assert "total" in low.lower()
