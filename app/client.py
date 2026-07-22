"""Асинхронний клієнт публічних маркет-даних Binance.

Прямі HTTP-виклики через httpx — без обгорток (python-binance / ccxt).
Ключова політика лімітів:
  • читаємо X-MBX-USED-WEIGHT-1M з КОЖНОЇ відповіді (вагу не хардкодимо);
  • тримаємо використання нижче `weight_soft_ratio` × ліміту;
  • на 429 — експоненційний бекоф із урахуванням Retry-After;
  • на 418 — негайно зупиняємось (IP-бан), UI має показати помилку;
  • на 4xx від основного хоста «липко» перемикаємось на дзеркало.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .config import Config


class IPBannedError(RuntimeError):
    """418 — IP заблоковано. Треба негайно зупинити всі запити."""


class BinanceClient:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config.load()
        self._active_base = self.cfg.hosts.rest_primary
        self._client = httpx.AsyncClient(
            timeout=self.cfg.http_timeout,
            headers={"Accept": "application/json"},
        )
        # Стан лімітів (публічно читається UI)
        self.used_weight: int = 0
        self.weight_limit: int = self.cfg.request_weight_limit
        self.banned_until_ms: int = 0
        self.last_error: str = ""

    async def aclose(self) -> None:
        await self._client.aclose()

    # ─────────────────── низькорівневий запит ───────────────────

    async def _request(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET із контролем ваги, фолбеком на дзеркало та обробкою 429/418."""
        await self._respect_weight()

        for attempt in range(6):
            base = self._active_base
            try:
                resp = await self._client.get(base + path, params=params)
            except httpx.HTTPError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 5:
                    raise
                await asyncio.sleep(min(2 ** attempt, 16))
                continue

            self._read_weight(resp)

            if resp.status_code == 418:
                retry_after = int(resp.headers.get("Retry-After", "0") or 0)
                self.banned_until_ms = int(time.time() * 1000) + retry_after * 1000
                self.last_error = "418 IP banned"
                raise IPBannedError(
                    f"418: IP заблоковано, Retry-After={retry_after}s. "
                    "Усі запити зупинено."
                )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "0") or 0)
                wait = retry_after if retry_after > 0 else min(2 ** attempt, 16)
                self.last_error = f"429 rate limit, чекаю {wait}s"
                await asyncio.sleep(wait)
                continue

            # 4xx від основного хоста (напр. 451 гео-блок) → липко на дзеркало
            if 400 <= resp.status_code < 500 and base == self.cfg.hosts.rest_primary:
                self._active_base = self.cfg.hosts.rest_mirror
                self.last_error = f"{resp.status_code} від основного хоста → дзеркало"
                continue

            resp.raise_for_status()
            self.last_error = ""
            return resp

        raise RuntimeError(f"Не вдалося виконати запит {path} після кількох спроб")

    def _read_weight(self, resp: httpx.Response) -> None:
        weight = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        if weight is not None:
            try:
                self.used_weight = int(weight)
            except ValueError:
                pass

    async def _respect_weight(self) -> None:
        """Якщо вага вже вище м'якого порога — чекаємо скидання вікна (хвилина)."""
        soft = self.weight_limit * self.cfg.weight_soft_ratio
        if self.used_weight >= soft:
            # Вікно ваги — 1 хвилина; чекаємо до її межі
            now = time.time()
            sleep_s = 60 - (now % 60) + 0.5
            await asyncio.sleep(sleep_s)
            self.used_weight = 0

    async def pace(self, min_pause: float = 0.2) -> None:
        """Пауза між сторінками backfill з огляду на вагу."""
        soft = self.weight_limit * self.cfg.weight_soft_ratio
        if self.used_weight >= soft:
            await self._respect_weight()
        else:
            await asyncio.sleep(min_pause)

    # ─────────────────── публічні ендпоінти ───────────────────

    async def exchange_info(self) -> dict:
        resp = await self._request("/api/v3/exchangeInfo")
        data = resp.json()
        # Прочитати реальний ліміт ваги замість хардкоду
        for rl in data.get("rateLimits", []):
            if rl.get("rateLimitType") == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE":
                self.weight_limit = int(rl["limit"])
        return data

    async def klines(self, symbol: str, interval: str = "1m",
                     start_time: int | None = None, limit: int = 1000) -> list[list]:
        params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        resp = await self._request("/api/v3/klines", params)
        return resp.json()

    async def depth(self, symbol: str, limit: int = 100) -> dict:
        resp = await self._request("/api/v3/depth", {"symbol": symbol, "limit": limit})
        return resp.json()

    async def ticker_24hr(self, symbol: str | None = None) -> dict | list:
        params = {"symbol": symbol} if symbol else None
        resp = await self._request("/api/v3/ticker/24hr", params)
        return resp.json()
