"""Київський часовий пояс — в одному місці, з людяною помилкою.

На Windows у Python немає системної бази часових поясів: її дає пакет
`tzdata` (у requirements.txt). Якщо бази немає — кажемо прямо, що робити.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    KYIV = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:
    try:
        # Старіші бази поясів знають лише давню назву
        KYIV = ZoneInfo("Europe/Kiev")
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            "Не знайдено базу часових поясів для Europe/Kyiv. "
            "Встановіть залежності: pip install -r requirements.txt "
            "(на Windows обовʼязково потрібен пакет tzdata)."
        ) from exc
