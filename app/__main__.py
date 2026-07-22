"""Точка входу: `python -m app` піднімає сервер і відкриває браузер.

Читає лише публічні маркет-дані. Жодних ключів, жодних ордерів.
"""

from __future__ import annotations

import threading
import webbrowser

import uvicorn

from .config import Config


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    cfg = Config.load()
    url = f"http://{cfg.host}:{cfg.port}"
    print(f"Binance Market Monitor → {url}")
    print("Публічні маркет-дані, лише читання. Ctrl+C — вихід.")
    if cfg.open_browser:
        threading.Timer(1.2, _open_browser, args=[url]).start()
    uvicorn.run("app.server:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
