"""python -m app.selfcheck SYMBOL — реальна перевірка на живому символі.

Робить справжні запити, друкує розібраний символ, час останньої закритої
свічки, σ, вартість круговороту на N=256 USDT і поточну використану вагу.
Виходить із ненульовим кодом при будь-якій помилці.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .client import BinanceClient, IPBannedError
from .config import Config
from . import metrics

KYIV = ZoneInfo("Europe/Kyiv")


def _fmt_ms(ms: int) -> str:
    """UTC мілісекунди → рядок 'UTC | Київ' (київський час лише для показу)."""
    dt_utc = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    dt_kyiv = dt_utc.astimezone(KYIV)
    return (f"{dt_utc:%Y-%m-%d %H:%M:%S} UTC | "
            f"{dt_kyiv:%Y-%m-%d %H:%M:%S} Київ")


async def run(symbol: str) -> int:
    cfg = Config.load()
    client = BinanceClient(cfg)
    try:
        # 1. Розібраний символ
        info = await client.exchange_info(symbol)
        syms = info.get("symbols", [])
        if not syms:
            print(f"[selfcheck] Символ {symbol} не знайдено в exchangeInfo.")
            return 1
        s = syms[0]
        print(f"Символ:        {s['symbol']}")
        print(f"База / котир.: {s['baseAsset']} / {s['quoteAsset']}")
        print(f"Статус:        {s['status']}")

        # 2. Свічки: беремо із запасом, лишаємо тільки ЗАКРИТІ
        now_ms = int(time.time() * 1000)
        raw = await client.klines(symbol, "1m", None, cfg.window + 5)
        closed = [metrics.parse_kline(r) for r in raw if int(r[6]) < now_ms]
        if len(closed) < 2:
            print("[selfcheck] Недостатньо закритих свічок для розрахунку.")
            return 1
        closes = [k.close for k in closed]
        last = closed[-1]
        print(f"Остання свічка:{_fmt_ms(last.open_time)}")
        print(f"Ціна закриття: {last.close}")

        # 3. σ за останні `window` закритих свічок
        sig = metrics.sigma(closes, window=cfg.window)
        sig_bps = sig * metrics.BPS
        print(f"σ (60 свічок): {sig_bps:.2f} bps")

        # 4. Вартість круговороту на N=256 USDT зі стакана
        depth = await client.depth(symbol, limit=100)
        # depth: bids за спаданням, asks за зростанням; roundtrip(asks, bids, N)
        rt = metrics.roundtrip(depth["asks"], depth["bids"], cfg.notional_usdt)
        print(f"\nВартість круговороту (N={cfg.notional_usdt:.0f} USDT):")
        if rt.insufficient_depth:
            print(f"  insufficient_depth — не вистачає глибини ({rt.side})")
        else:
            drift = metrics.drift_bps(sig, cfg.hold_seconds)
            total = metrics.total_bps(rt.roundtrip_bps, drift)
            print(f"  куплено qty:     {rt.qty:.6f} {s['baseAsset']}")
            print(f"  виручка M:       {rt.proceeds:.4f} {s['quoteAsset']}")
            print(f"  half_spread:     {rt.half_spread_bps:.2f} bps")
            print(f"  roundtrip:       {rt.roundtrip_bps:.2f} bps")
            print(f"  drift ({cfg.hold_seconds:.0f}s):   {drift:.2f} bps")
            print(f"  total:           {total:.2f} bps "
                  f"= {metrics.bps_to_usd(total, cfg.notional_usdt):.4f} USD")

        # 5. Поточна використана вага
        print(f"\nX-MBX-USED-WEIGHT-1M: {client.used_weight} / {client.weight_limit}")
        return 0

    except IPBannedError as exc:
        print(f"[selfcheck] {exc}")
        return 1
    except Exception as exc:  # будь-яка помилка → ненульовий код
        print(f"[selfcheck] Помилка: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("Використання: python -m app.selfcheck SYMBOL")
        return 2
    return asyncio.run(run(argv[0].upper()))


if __name__ == "__main__":
    sys.exit(main())
