"""Движок моніторингу: WS-стрім, перерахунок метрик, стан «спокійно», журнал.

Тримає одне WS-з'єднання до дзеркала стрімів і динамічно
SUBSCRIBE/UNSUBSCRIBE символи. Закриті свічки (k.x == true) пишуться в БД і
за ними рахуються метрики; незакрита свічка лише віддається на графік.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import websockets

from . import analytics, backfill, db
from .client import BinanceClient, IPBannedError
from .config import Config
from .notifier import TelegramNotifier
from .tz import KYIV

MINUTE_MS = 60_000


class Monitor:
    def __init__(self, cfg: Config | None = None, conn=None):
        self.cfg = cfg or Config.load()
        if conn is not None:
            self.conn = conn                      # для тестів: in-memory БД
        else:
            from .config import DB_PATH
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.conn = db.connect(DB_PATH)
        db.init_db(self.conn)
        self.client = BinanceClient(self.cfg)

        self.exchange_symbols: dict[str, dict] = {}   # symbol → {base, quote, status}
        self.state: dict[str, dict] = {}              # символ → живий стан для UI
        self.backfill_progress: dict[str, dict] = {}

        self._calm_streak: dict[str, int] = {}
        self._calm_active: dict[str, tuple[bool, int | None]] = {}
        self._low_total: dict[str, bool] = {}   # символ → чи total зараз «низький»

        self.notifier = TelegramNotifier()

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._ws_task: asyncio.Task | None = None
        self._sub_id = 0
        self._recompute_locks: dict[str, asyncio.Lock] = {}
        self.banned = False
        self.status_msg = "Запуск…"

    # ─────────────────── життєвий цикл ───────────────────

    async def start(self) -> None:
        """Завантажує довідник символів, піднімає WS і відновлює вотчліст."""
        try:
            info = await self.client.exchange_info()
            for s in info.get("symbols", []):
                self.exchange_symbols[s["symbol"]] = {
                    "base": s["baseAsset"], "quote": s["quoteAsset"],
                    "status": s["status"],
                }
            self.status_msg = f"Символів у довіднику: {len(self.exchange_symbols)}"
        except IPBannedError as exc:
            self.banned = True
            self.status_msg = str(exc)
            return
        except Exception as exc:
            self.status_msg = f"Не вдалося завантажити exchangeInfo: {exc}"

        self._ws_task = asyncio.create_task(self._ws_loop())

        for symbol in db.get_watchlist(self.conn):
            self._init_state(symbol)
            asyncio.create_task(self._prepare_symbol(symbol))

    async def stop(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()
        await self.client.aclose()

    # ─────────────────── вотчліст ───────────────────

    async def add_symbol(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if symbol not in self.exchange_symbols:
            return {"ok": False, "error": f"Символ {symbol} відсутній у довіднику Binance."}
        db.add_to_watchlist(self.conn, symbol, int(time.time() * 1000))
        self._init_state(symbol)
        await self._subscribe(symbol)
        asyncio.create_task(self._prepare_symbol(symbol))
        return {"ok": True, "symbol": symbol}

    async def remove_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        db.remove_from_watchlist(self.conn, symbol)
        self.state.pop(symbol, None)
        self.backfill_progress.pop(symbol, None)
        await self._unsubscribe(symbol)

    def _init_state(self, symbol: str) -> None:
        meta = self.exchange_symbols.get(symbol, {})
        self.state.setdefault(symbol, {
            "symbol": symbol,
            "base": meta.get("base", ""),
            "quote": meta.get("quote", ""),
            "status": meta.get("status", ""),
            "price": None,
            "best_bid": None,
            "best_ask": None,
            "forming": None,       # незакрита свічка для графіка
            "metrics": None,       # останній знімок метрик
            "last_close_ms": None,
        })

    async def _prepare_symbol(self, symbol: str) -> None:
        """Backfill 30 днів, тоді перший перерахунок метрик."""
        self.backfill_progress[symbol] = {"done": 0, "total": 0, "running": True}

        def on_progress(done, total, fetched):
            self.backfill_progress[symbol] = {
                "done": done, "total": total, "fetched": fetched, "running": True,
            }

        try:
            await backfill.backfill_symbol(
                self.conn, self.client, symbol,
                days=self.cfg.backfill_days, on_progress=on_progress,
            )
            self.backfill_progress[symbol]["running"] = False
            await self._subscribe(symbol)
            await self._recompute(symbol)
        except IPBannedError as exc:
            self.banned = True
            self.status_msg = str(exc)
        except Exception as exc:
            self.backfill_progress[symbol] = {"running": False, "error": str(exc)}

    # ─────────────────── WebSocket ───────────────────

    def _streams_for(self, symbol: str) -> list[str]:
        low = symbol.lower()
        return [f"{low}@kline_1m", f"{low}@bookTicker"]

    async def _subscribe(self, symbol: str) -> None:
        if self._ws is None:
            return
        self._sub_id += 1
        await self._ws.send(json.dumps({
            "method": "SUBSCRIBE", "params": self._streams_for(symbol),
            "id": self._sub_id,
        }))

    async def _unsubscribe(self, symbol: str) -> None:
        if self._ws is None:
            return
        self._sub_id += 1
        await self._ws.send(json.dumps({
            "method": "UNSUBSCRIBE", "params": self._streams_for(symbol),
            "id": self._sub_id,
        }))

    async def _ws_loop(self) -> None:
        """Одне з'єднання зі стрімами; перепідключення з бекофом."""
        backoff = 1
        while not self.banned:
            try:
                url = self.cfg.hosts.ws_mirror + "/stream"
                async with websockets.connect(url, open_timeout=20,
                                              ping_interval=180) as ws:
                    self._ws = ws
                    backoff = 1
                    # Відновити підписки на весь вотчліст
                    for symbol in db.get_watchlist(self.conn):
                        await self._subscribe(symbol)
                    async for raw in ws:
                        await self._on_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status_msg = f"WS перепідключення: {exc}"
                self._ws = None
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2
        self._ws = None

    async def _on_message(self, raw: str) -> None:
        msg = json.loads(raw)
        if "stream" not in msg:
            return  # ack підписки
        stream = msg["stream"]
        data = msg["data"]
        if stream.endswith("@bookTicker"):
            self._handle_book(data)
        elif "@kline" in stream:
            await self._handle_kline(data)

    def _handle_book(self, data: dict) -> None:
        symbol = data["s"]
        st = self.state.get(symbol)
        if not st:
            return
        st["best_bid"] = float(data["b"])
        st["best_ask"] = float(data["a"])
        st["price"] = (st["best_bid"] + st["best_ask"]) / 2

    async def _handle_kline(self, data: dict) -> None:
        symbol = data["s"]
        k = data["k"]
        st = self.state.get(symbol)
        if not st:
            return
        candle = {
            "open_time": int(k["t"]), "open": float(k["o"]), "high": float(k["h"]),
            "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
            "quote_volume": float(k["q"]), "trades": int(k["n"]),
        }
        if k["x"]:  # ТІЛЬКИ закриті свічки пишемо в БД і рахуємо метрики
            db.upsert_klines(self.conn, symbol, [[
                candle["open_time"], candle["open"], candle["high"], candle["low"],
                candle["close"], candle["volume"], k["T"], candle["quote_volume"],
                candle["trades"], 0, 0, "0",
            ]])
            self.conn.commit()
            st["forming"] = None
            st["last_close_ms"] = candle["open_time"]
            await self._recompute(symbol)
        else:
            st["forming"] = candle  # незакрита — лише на графік

    # ─────────────────── перерахунок метрик + журнал ───────────────────

    async def _recompute(self, symbol: str) -> None:
        lock = self._recompute_locks.setdefault(symbol, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            rows = db.get_klines(self.conn, symbol)
            if len(rows) < 2:
                return
            open_times = [r["open_time"] for r in rows]
            closes = [r["close"] for r in rows]
            highs = [r["high"] for r in rows]
            lows = [r["low"] for r in rows]
            qv = [r["quote_volume"] for r in rows]

            try:
                depth = await self.client.depth(symbol, limit=100)
            except IPBannedError as exc:
                self.banned = True
                self.status_msg = str(exc)
                return
            except Exception:
                depth = {"asks": [], "bids": []}

            # Важку частину (numpy по 30 днях) виносимо в потік
            m = await asyncio.to_thread(
                analytics.snapshot, closes, highs, lows, qv, open_times,
                depth.get("asks", []), depth.get("bids", []), self.cfg,
            )

            ts = int(open_times[-1])
            active, calm_event = self._update_calm(symbol, ts, m["calm_now"])
            m["calm"] = active
            low_event = self._update_low_total(symbol, m.get("total_bps"))
            self.state[symbol]["metrics"] = m
            self.state[symbol]["last_close_ms"] = ts
            db.upsert_metrics(self.conn, symbol, ts, m)

            events = [e for e in (calm_event, low_event) if e]
            if events:
                await self._notify(symbol, events, m, ts)

    def _update_calm(self, symbol: str, ts: int, calm_now: bool) -> tuple[bool, dict | None]:
        """Стан «спокійно» = calm_now витримано K хвилин поспіль. Журналить входи/виходи.

        Повертає (активний_стан, подія|None) — подія йде в сповіщення.
        """
        streak = self._calm_streak.get(symbol, 0)
        streak = streak + 1 if calm_now else 0
        self._calm_streak[symbol] = streak

        event: dict | None = None
        active, since = self._calm_active.get(symbol, (False, None))
        if not active and streak >= self.cfg.calm_minutes:
            since = ts - (self.cfg.calm_minutes - 1) * MINUTE_MS
            active = True
            db.add_alert(self.conn, symbol, since, "calm_enter",
                         json.dumps({"since": since}))
            event = {"kind": "calm_enter", "since": since}
        elif active and not calm_now:
            duration = ts - (since or ts)
            db.add_alert(self.conn, symbol, ts, "calm_exit",
                         json.dumps({"since": since, "until": ts,
                                     "duration_ms": duration}))
            event = {"kind": "calm_exit", "duration_ms": duration}
            active = False
            since = None
        self._calm_active[symbol] = (active, since)
        return active, event

    def _update_low_total(self, symbol: str, total_bps: float | None) -> dict | None:
        """Подія, коли total падає нижче порога (з гістерезисом, щоб не блимало)."""
        thr = self.cfg.notify_low_total_bps
        if not thr or thr <= 0 or total_bps is None:
            return None
        was_low = self._low_total.get(symbol, False)
        event = None
        if not was_low and total_bps < thr:
            event = {"kind": "low_total", "total_bps": total_bps, "threshold": thr}
            was_low = True
        elif was_low and total_bps > thr * 1.5:
            was_low = False  # переозброїти, але без окремого сповіщення
        self._low_total[symbol] = was_low
        return event

    # ─────────────────── сповіщення Telegram ───────────────────

    async def _notify(self, symbol: str, events: list[dict], m: dict, ts: int) -> None:
        if not self.notifier.enabled:
            return
        if not db.get_notify_map(self.conn).get(symbol, True):
            return  # сповіщення лише для обраних монет
        for e in events:
            text = self._format_event(symbol, e, m, ts)
            if text:
                asyncio.create_task(self.notifier.send(text))

    def _format_event(self, symbol: str, e: dict, m: dict, ts: int) -> str | None:
        kind = e["kind"]
        hhmm = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(KYIV).strftime("%H:%M")
        n = int(self.cfg.notional_usdt)

        def f(x, d=2):
            return "—" if x is None else f"{x:.{d}f}"

        if kind in ("calm_enter", "calm_exit") and not self.cfg.notify_calm_events:
            return None
        if kind == "calm_enter":
            return (f"🟢 <b>{symbol}</b> — стан «спокійно» ({hhmm} Київ)\n"
                    f"σ {f(m.get('sigma_bps'))} bps ({f(m.get('sigma_pct'), 0)}%ᵖ) · "
                    f"RT {f(m.get('roundtrip_bps'))} bps · "
                    f"total {f(m.get('total_bps'))} bps ≈ {f(m.get('total_usd'), 3)}$ / {n} USDT")
        if kind == "calm_exit":
            return (f"⚪ <b>{symbol}</b> — вийшов зі «спокою» ({hhmm} Київ), "
                    f"тривав {e['duration_ms'] // 60000} хв")
        if kind == "low_total":
            return (f"💧 <b>{symbol}</b> — total ≤ {f(e['threshold'])} bps, дешево зайти/вийти ({hhmm} Київ)\n"
                    f"total {f(m.get('total_bps'))} bps ≈ {f(m.get('total_usd'), 3)}$ / {n} USDT "
                    f"(RT {f(m.get('roundtrip_bps'))} + дрейф {f(m.get('drift_bps'))})")
        return None

    # ─────────────────── пошук ───────────────────

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """Пошук за підрядком у symbol/baseAsset + аліаси. Без сторонніх сервісів."""
        from .config import load_aliases
        q = query.strip().lower()
        if not q:
            return []
        aliases = load_aliases()
        matched: list[str] = []
        if q in aliases and aliases[q] in self.exchange_symbols:
            matched.append(aliases[q])
        for sym, meta in self.exchange_symbols.items():
            if sym in matched:
                continue
            if q in sym.lower() or q in meta["base"].lower():
                matched.append(sym)
            if len(matched) >= limit:
                break

        if not matched:
            return []

        # 24-год обсяг у котирувальній валюті — одним батч-запитом
        vol_by_sym: dict[str, float] = {}
        try:
            tick = await self.client.ticker_24hr()  # для точності беремо всі, шукаємо потрібні
            if isinstance(tick, list):
                wanted = set(matched)
                for t in tick:
                    if t["symbol"] in wanted:
                        vol_by_sym[t["symbol"]] = float(t.get("quoteVolume", 0))
        except Exception:
            pass

        results = []
        for sym in matched:
            meta = self.exchange_symbols[sym]
            _, mx, _ = db.kline_range(self.conn, sym)
            results.append({
                "symbol": sym, "base": meta["base"], "quote": meta["quote"],
                "status": meta["status"],
                "quote_volume_24h": vol_by_sym.get(sym),
                "last_candle_ms": mx,
                "in_watchlist": sym in db.get_watchlist(self.conn),
            })
        return results

    # ─────────────────── зрізи для UI ───────────────────

    def watchlist_view(self) -> list[dict]:
        out = []
        notify_map = db.get_notify_map(self.conn)
        for symbol in db.get_watchlist(self.conn):
            st = self.state.get(symbol) or {}
            out.append({
                "symbol": symbol,
                "base": st.get("base"), "quote": st.get("quote"),
                "status": st.get("status"),
                "price": st.get("price"),
                "metrics": st.get("metrics"),
                "last_close_ms": st.get("last_close_ms"),
                "backfill": self.backfill_progress.get(symbol),
                "notify": notify_map.get(symbol, True),
            })
        return out

    def set_notify(self, symbol: str, on: bool) -> None:
        db.set_notify(self.conn, symbol.upper(), on)

    async def notify_test(self) -> dict:
        return await self.notifier.test()

    def symbol_detail(self, symbol: str, candles: int = 720) -> dict:
        symbol = symbol.upper()
        rows = db.get_klines(self.conn, symbol, limit=candles)
        klines = [{
            "time": r["open_time"] // 1000, "open": r["open"], "high": r["high"],
            "low": r["low"], "close": r["close"], "volume": r["quote_volume"],
        } for r in rows]
        metric_rows = db.get_metrics_history(self.conn, symbol)
        st = self.state.get(symbol) or {}
        return {
            "symbol": symbol,
            "klines": klines,
            "forming": st.get("forming"),
            "metrics": st.get("metrics"),
            "price": st.get("price"),
            "heatmap": analytics.heatmap_total_bps(metric_rows),
        }

    async def live_roundtrip(self, symbol: str) -> dict:
        """Свіжий стакан → миттєва вартість круговороту (для деталі символу)."""
        from . import metrics as M
        symbol = symbol.upper()
        try:
            depth = await self.client.depth(symbol, limit=100)
        except Exception as exc:
            return {"error": str(exc)}
        rt = M.roundtrip(depth.get("asks", []), depth.get("bids", []), self.cfg.notional_usdt)
        if rt.insufficient_depth:
            return {"insufficient_depth": True, "side": rt.side}
        st = self.state.get(symbol) or {}
        sig = (st.get("metrics") or {}).get("sigma")
        drift = M.drift_bps(sig, self.cfg.hold_seconds) if sig else float("nan")
        total = M.total_bps(rt.roundtrip_bps, drift)

        def num(x):
            xf = float(x)
            return None if xf != xf else xf   # NaN → None

        return {
            "insufficient_depth": False,
            "roundtrip_bps": num(rt.roundtrip_bps),
            "half_spread_bps": num(rt.half_spread_bps),
            "drift_bps": num(drift),
            "total_bps": num(total),
            "total_usd": num(M.bps_to_usd(total, self.cfg.notional_usdt)),
            "notional": self.cfg.notional_usdt,
        }

    def journal(self, symbol: str | None = None) -> list[dict]:
        """Епізоди «спокійно»: вхід, вихід, тривалість (з таблиці alerts)."""
        rows = db.get_alerts(self.conn, symbol)
        episodes = []
        for r in rows:
            payload = json.loads(r["payload"]) if r["payload"] else {}
            episodes.append({
                "symbol": r["symbol"], "ts": r["ts"], "kind": r["kind"],
                **payload,
            })
        return episodes

    def limits(self) -> dict:
        return {
            "used_weight": self.client.used_weight,
            "weight_limit": self.client.weight_limit,
            "banned": self.banned,
            "status": self.status_msg,
            "last_error": self.client.last_error,
            "tg_enabled": self.notifier.enabled,
            "tg_error": self.notifier.last_error,
        }
