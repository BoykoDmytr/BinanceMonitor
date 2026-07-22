"""Крок 0 — розвідка Binance API перед проєктуванням.

Завантажує exchangeInfo, рахує TRADING-символи, шукає bStocks-тикери,
для знайдених тягне klines та depth, друкує сирі відповіді та used weight.
Без API-ключів — лише публічні маркет-дані.
"""

import json
import sys
from pathlib import Path

import httpx

BASE = "https://api.binance.com"
MIRROR = "https://data-api.binance.vision"  # фолбек для маркет-даних при 4xx
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Токенізовані акції bStocks, які просив перевірити користувач
BSTOCK_SYMBOLS = [
    "CBRSBUSDT",   # Cerebras
    "NVDABUSDT",   # NVIDIA
    "TSLABUSDT",   # Tesla
    "MUBUSDT",     # Micron
    "CRCLBUSDT",   # Circle
    "SNDKBUSDT",   # Sandisk
]

last_used_weight = "?"


def get(client: httpx.Client, path: str, params: dict | None = None) -> httpx.Response:
    """GET з фолбеком на дзеркало при 4xx від основного хоста."""
    global last_used_weight
    resp = client.get(BASE + path, params=params)
    if 400 <= resp.status_code < 500 and resp.status_code != 429:
        print(f"  [!] {BASE}{path} -> {resp.status_code}, пробую дзеркало {MIRROR}")
        resp = client.get(MIRROR + path, params=params)
    weight = resp.headers.get("X-MBX-USED-WEIGHT-1M")
    if weight is not None:
        last_used_weight = weight
    resp.raise_for_status()
    return resp


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    with httpx.Client(timeout=30) as client:
        # 1. exchangeInfo — завантажити й зберегти локально
        print("=== 1. GET /api/v3/exchangeInfo ===")
        resp = get(client, "/api/v3/exchangeInfo")
        info = resp.json()
        out_path = DATA_DIR / "exchangeInfo.json"
        out_path.write_text(json.dumps(info, indent=1))
        print(f"Збережено: {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")
        print(f"X-MBX-USED-WEIGHT-1M: {resp.headers.get('X-MBX-USED-WEIGHT-1M')}")

        symbols = info["symbols"]
        trading = [s for s in symbols if s["status"] == "TRADING"]
        trading_usdt = [s for s in trading if s["quoteAsset"] == "USDT"]

        # 2. Підрахунки
        print("\n=== 2. Підрахунки ===")
        print(f"Всього символів у exchangeInfo: {len(symbols)}")
        print(f"Зі status == TRADING:           {len(trading)}")
        print(f"З них до USDT (quoteAsset):     {len(trading_usdt)}")

        # 3. Перевірка bStocks-символів
        print("\n=== 3. Перевірка символів bStocks ===")
        by_symbol = {s["symbol"]: s for s in symbols}
        found: list[str] = []
        for sym in BSTOCK_SYMBOLS:
            s = by_symbol.get(sym)
            if s:
                found.append(sym)
                print(f"  {sym}: ЗНАЙДЕНО, status={s['status']}, "
                      f"base={s['baseAsset']}, quote={s['quoteAsset']}")
            else:
                print(f"  {sym}: ВІДСУТНІЙ в exchangeInfo")

        # Додатковий пошук схожих тикерів, щоб не пропустити інше написання
        hints = ("CBRS", "NVDA", "TSLA", "CRCL", "SNDK", "MU")
        similar = sorted(
            s["symbol"] for s in symbols
            if any(s["baseAsset"].startswith(h) for h in hints)
        )
        print(f"  Схожі тикери (baseAsset починається з {'/'.join(hints)}): "
              f"{similar if similar else 'немає'}")

        # 4. klines + depth для знайдених
        print("\n=== 4. klines та depth для знайдених символів ===")
        if not found:
            print("  Жодного символу bStocks не знайдено — запити пропущено.")
        for sym in found:
            print(f"\n--- {sym}: GET /api/v3/klines?interval=1m&limit=5 ---")
            r = get(client, "/api/v3/klines",
                    {"symbol": sym, "interval": "1m", "limit": 5})
            print(json.dumps(r.json(), indent=1))
            print(f"\n--- {sym}: GET /api/v3/depth?limit=100 ---")
            r = get(client, "/api/v3/depth", {"symbol": sym, "limit": 100})
            d = r.json()
            print(f"lastUpdateId={d['lastUpdateId']}, "
                  f"bids={len(d['bids'])} рівнів, asks={len(d['asks'])} рівнів")
            print("top-5 bids:", json.dumps(d["bids"][:5]))
            print("top-5 asks:", json.dumps(d["asks"][:5]))

        # Контрольний запит по BTCUSDT, щоб показати живі klines/depth,
        # навіть якщо bStocks не знайдено
        if not found:
            print("\n--- Контроль на BTCUSDT (bStocks відсутні) ---")
            r = get(client, "/api/v3/klines",
                    {"symbol": "BTCUSDT", "interval": "1m", "limit": 5})
            print("klines BTCUSDT (5 останніх 1m):")
            print(json.dumps(r.json(), indent=1))
            r = get(client, "/api/v3/depth", {"symbol": "BTCUSDT", "limit": 100})
            d = r.json()
            print(f"depth BTCUSDT: bids={len(d['bids'])}, asks={len(d['asks'])}")
            print("top-3 bids:", json.dumps(d["bids"][:3]))
            print("top-3 asks:", json.dumps(d["asks"][:3]))

        # 5. Used weight
        print("\n=== 5. X-MBX-USED-WEIGHT-1M (остання відповідь) ===")
        print(f"Використана вага за хвилину: {last_used_weight}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
