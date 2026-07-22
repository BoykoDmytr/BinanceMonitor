"""Крок 0 — розвідувальний спайк перед проєктуванням.

Що робить:
  1. Тягне GET /api/v3/exchangeInfo і зберігає сиру відповідь локально.
  2. Рахує символи зі status == "TRADING" і скільки з них котируються в USDT.
  3. Перевіряє наявність символів bStocks зі списку.
  4. Для знайдених — по одному запиту klines (1m, limit=5) та depth (limit=100),
     сирі відповіді зберігає у файли й друкує.
  5. Друкує X-MBX-USED-WEIGHT-1M з кожної відповіді.

Жодних ключів, жодного запису — тільки публічні read-only ендпоінти.
Запуск:  python spike/step0_spike.py
"""

import json
import os
import sys
from pathlib import Path

import httpx

PRIMARY = "https://api.binance.com"
MIRROR = "https://data-api.binance.vision"  # фолбек при 4xx від основного хоста

BSTOCKS = ["CBRSBUSDT", "NVDABUSDT", "TSLABUSDT", "MUBUSDT", "CRCLBUSDT", "SNDKBUSDT"]

OUT_DIR = Path(__file__).parent / "raw"


def make_client() -> httpx.Client:
    # У середовищі з проксі TLS перевіряється через локальний CA-бандл,
    # шлях до якого передається через SSL_CERT_FILE / REQUESTS_CA_BUNDLE.
    verify = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True
    return httpx.Client(verify=verify, timeout=30.0)


def get_with_fallback(client: httpx.Client, path: str, params: dict | None = None) -> tuple[httpx.Response, str]:
    """GET з основного хоста; при 4xx пробуємо дзеркало data-api.binance.vision."""
    resp = client.get(PRIMARY + path, params=params)
    host = PRIMARY
    if 400 <= resp.status_code < 500 and resp.status_code != 429:
        print(f"  [!] {PRIMARY}{path} -> HTTP {resp.status_code}, пробую дзеркало {MIRROR}")
        resp = client.get(MIRROR + path, params=params)
        host = MIRROR
    resp.raise_for_status()
    return resp, host


def used_weight(resp: httpx.Response) -> str:
    return resp.headers.get("X-MBX-USED-WEIGHT-1M", "<заголовок відсутній>")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = make_client()

    # ---- 1. exchangeInfo ----
    print("=== 1. exchangeInfo ===")
    resp, host = get_with_fallback(client, "/api/v3/exchangeInfo")
    info_path = OUT_DIR / "exchangeInfo.json"
    info_path.write_bytes(resp.content)
    print(f"Хост: {host}")
    print(f"Збережено: {info_path} ({len(resp.content):,} байт)")
    print(f"X-MBX-USED-WEIGHT-1M: {used_weight(resp)}")

    info = resp.json()
    symbols = info["symbols"]

    # ---- 2. Підрахунок ----
    trading = [s for s in symbols if s["status"] == "TRADING"]
    trading_usdt = [s for s in trading if s["quoteAsset"] == "USDT"]
    print("\n=== 2. Підрахунок символів ===")
    print(f"Усього символів у exchangeInfo: {len(symbols)}")
    print(f'Зі status == "TRADING": {len(trading)}')
    print(f"З них до USDT (quoteAsset == USDT): {len(trading_usdt)}")

    # ---- 3. Перевірка bStocks ----
    print("\n=== 3. Перевірка символів bStocks ===")
    by_name = {s["symbol"]: s for s in symbols}
    found: list[str] = []
    for sym in BSTOCKS:
        s = by_name.get(sym)
        if s is None:
            print(f"  {sym}: ВІДСУТНІЙ в exchangeInfo")
        else:
            found.append(sym)
            print(f"  {sym}: є, status={s['status']}, base={s['baseAsset']}, quote={s['quoteAsset']}")

    # Додатково: чи є взагалі щось схоже на токенізовані акції (суфікси/бази)
    hints = [s["symbol"] for s in symbols
             if any(t in s["symbol"] for t in ("NVDA", "TSLA", "CRCL", "SNDK", "CBRS", "MU"))]
    print(f"  Схожі тикери за підрядками (NVDA/TSLA/CRCL/SNDK/CBRS/MU): {hints or 'нічого'}")

    # ---- 4. klines + depth для знайдених ----
    print("\n=== 4. klines та depth для знайдених символів ===")
    if not found:
        print("  Жодного символу bStocks не знайдено — запити klines/depth не робимо.")
    for sym in found:
        print(f"\n--- {sym}: klines 1m limit=5 ---")
        resp, host = get_with_fallback(client, "/api/v3/klines",
                                       {"symbol": sym, "interval": "1m", "limit": 5})
        (OUT_DIR / f"klines_{sym}.json").write_bytes(resp.content)
        print(resp.text)
        print(f"X-MBX-USED-WEIGHT-1M: {used_weight(resp)}  (хост: {host})")

        print(f"\n--- {sym}: depth limit=100 ---")
        resp, host = get_with_fallback(client, "/api/v3/depth",
                                       {"symbol": sym, "limit": 100})
        (OUT_DIR / f"depth_{sym}.json").write_bytes(resp.content)
        print(resp.text)
        print(f"X-MBX-USED-WEIGHT-1M: {used_weight(resp)}  (хост: {host})")

    print("\n=== Готово ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
