"""FastAPI-сервер: віддає одну HTML-сторінку і JSON-API поверх движка."""

from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .engine import Monitor
from .tz import KYIV

STATIC_DIR = Path(__file__).resolve().parent / "static"

monitor: Monitor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global monitor
    monitor = Monitor(Config.load())
    await monitor.start()
    yield
    await monitor.stop()


app = FastAPI(title="Binance Market Monitor", lifespan=lifespan)


@app.middleware("http")
async def revalidate_assets(request, call_next):
    """no-cache для сторінки та статики: браузер завжди перевіряє свіжість JS/CSS.

    StaticFiles віддає ETag/Last-Modified, тож незмінене повертається як 304 —
    трафік мінімальний, але оновлений код підхоплюється без ручного очищення кешу.
    """
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


def _mon() -> Monitor:
    if monitor is None:
        raise HTTPException(503, "Движок ще не готовий")
    return monitor


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config():
    cfg = _mon().cfg
    return {
        "notional_usdt": cfg.notional_usdt,
        "hold_seconds": cfg.hold_seconds,
        "calm_minutes": cfg.calm_minutes,
        "color_green_max_bps": cfg.color_green_max_bps,
        "color_yellow_max_bps": cfg.color_yellow_max_bps,
    }


@app.get("/api/limits")
async def get_limits():
    return _mon().limits()


@app.get("/api/search")
async def search(q: str = ""):
    return await _mon().search(q)


@app.get("/api/watchlist")
async def watchlist():
    return _mon().watchlist_view()


@app.post("/api/watchlist")
async def watchlist_add(payload: dict):
    symbol = str(payload.get("symbol", "")).strip()
    if not symbol:
        raise HTTPException(400, "Порожній символ")
    return await _mon().add_symbol(symbol)


@app.delete("/api/watchlist/{symbol}")
async def watchlist_remove(symbol: str):
    await _mon().remove_symbol(symbol)
    return {"ok": True}


@app.post("/api/watchlist/{symbol}/notify")
async def watchlist_notify(symbol: str, payload: dict):
    _mon().set_notify(symbol, bool(payload.get("on", True)))
    return {"ok": True}


@app.post("/api/notify/test")
async def notify_test():
    return await _mon().notify_test()


@app.get("/api/symbol/{symbol}")
async def symbol_detail(symbol: str):
    return _mon().symbol_detail(symbol)


@app.get("/api/symbol/{symbol}/roundtrip")
async def symbol_roundtrip(symbol: str):
    return await _mon().live_roundtrip(symbol)


@app.get("/api/journal")
async def journal(symbol: str | None = None):
    return _mon().journal(symbol)


@app.get("/api/journal.csv")
async def journal_csv(symbol: str | None = None):
    episodes = _mon().journal(symbol)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["symbol", "kind", "ts_utc", "ts_kyiv", "duration_min"])
    for e in episodes:
        ts = e.get("ts")
        dur = e.get("duration_ms")
        writer.writerow([
            e.get("symbol"), e.get("kind"),
            _iso(ts, timezone.utc), _iso(ts, KYIV),
            f"{dur / 60000:.1f}" if dur else "",
        ])
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=journal.csv"},
    )


def _iso(ms, tz) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# Статика (JS/CSS) — монтуємо в кінці, щоб не перехопити /api
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
