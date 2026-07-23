"""Конфігурація застосунку.

Значення за замовчуванням можна перевизначити файлом ``config.json``
у корені репозиторію. Абсолютні пороги волатильності ніде не
хардкодяться — тут лише параметри метрик і кольорові пороги UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
ALIASES_PATH = ROOT / "aliases.json"
# Шлях БД можна винести на постійний том (напр. /data на Fly) через env
DB_PATH = Path(os.environ.get("BMM_DB_PATH", str(ROOT / "data" / "monitor.sqlite3")))


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Hosts:
    """Хости даних. Дзеркало — фолбек при 4xx від основного (напр. 451 гео-блок).

    Зі спайку (Крок 0): у цьому середовищі api.binance.com віддає 451,
    а data-api.binance.vision і wss://data-stream.binance.vision працюють.
    Клієнт «липко» перемикається на дзеркало після першого 4xx.
    """

    rest_primary: str = "https://api.binance.com"
    rest_mirror: str = "https://data-api.binance.vision"
    ws_primary: str = "wss://stream.binance.com:9443"
    ws_mirror: str = "wss://data-stream.binance.vision"


@dataclass
class Config:
    # Розмір круговороту в котирувальній валюті (USDT)
    notional_usdt: float = 256.0
    # Горизонт утримання для ризику дрейфу, секунди
    hold_seconds: float = 30.0
    # Скільки хвилин поспіль мають виконуватись умови для стану «спокійно»
    calm_minutes: int = 5
    # Вікно для σ / діапазону / обсягу, у закритих 1-хв свічках
    window: int = 60
    # Період ATR
    atr_period: int = 14
    # Глибина backfill, днів
    backfill_days: int = 30

    # Пороги стану «спокійно» (перцентилі всередині символу + абсолютний roundtrip)
    calm_sigma_pct: float = 20.0
    calm_range_pct: float = 20.0
    calm_roundtrip_bps: float = 25.0
    # Перцентиль обсягу, нижче якого ринок вважається неліквідним
    dead_market_pct: float = 5.0

    # Кольорові пороги вотчліста за total_bps (три стани світлофора)
    color_green_max_bps: float = 20.0
    color_yellow_max_bps: float = 50.0

    # Сповіщення в Telegram (для обраних монет; токен/чат — через env)
    notify_calm_events: bool = True          # вхід/вихід зі стану «спокійно»
    notify_low_total_bps: float = 5.0        # алерт коли total падає нижче (0 = вимкнено)

    # Мережа
    request_weight_limit: int = 1200   # фолбек, якщо не прочитали з exchangeInfo
    weight_soft_ratio: float = 0.80    # тримати використання нижче 80% ліміту
    http_timeout: float = 30.0

    # Сервер
    host: str = "127.0.0.1"
    port: int = 8787
    open_browser: bool = True

    hosts: Hosts = field(default_factory=Hosts)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            hosts = data.pop("hosts", None)
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
            if hosts:
                cfg.hosts = Hosts(**{**asdict(cfg.hosts), **hosts})
        # Env-оверрайди — для сервера (Fly), де немає config.json
        cfg.host = os.environ.get("BMM_HOST", cfg.host)
        cfg.port = int(os.environ.get("BMM_PORT", cfg.port))
        cfg.open_browser = _env_bool("BMM_OPEN_BROWSER", cfg.open_browser)
        cfg.notify_calm_events = _env_bool("BMM_NOTIFY_CALM", cfg.notify_calm_events)
        if os.environ.get("BMM_NOTIFY_LOW_TOTAL_BPS"):
            cfg.notify_low_total_bps = float(os.environ["BMM_NOTIFY_LOW_TOTAL_BPS"])
        return cfg


def load_aliases() -> dict[str, str]:
    """Читає aliases.json (назва → тикер). Користувач редагує його руками."""
    if ALIASES_PATH.exists():
        raw = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
        return {str(k).lower(): str(v).upper() for k, v in raw.items()}
    return {}
