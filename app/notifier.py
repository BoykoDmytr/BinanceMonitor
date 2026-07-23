"""Сповіщення в Telegram через Bot API (пряме HTTP, без обгорток).

Токен і чат читаються з оточення (не комітяться):
  BMM_TG_TOKEN — токен бота від @BotFather
  BMM_TG_CHAT  — куди слати: @назва_каналу або числовий id (напр. -100...)

Якщо змінні не задані — сповіщення просто вимкнені (застосунок працює як є).
"""

from __future__ import annotations

import os

import httpx


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("BMM_TG_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("BMM_TG_CHAT", "")
        self.last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str) -> bool:
        """Надсилає повідомлення. Помилки не кидає — лише пише в last_error."""
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
            if resp.status_code != 200:
                self.last_error = f"{resp.status_code}: {resp.text[:200]}"
                return False
            self.last_error = ""
            return True
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    async def test(self) -> dict:
        """Перевірка зв'язку: getMe + тестове повідомлення."""
        if not self.enabled:
            return {"ok": False, "error": "BMM_TG_TOKEN / BMM_TG_CHAT не задані"}
        ok = await self.send("✅ Binance Market Monitor: тестове сповіщення. Зв'язок є.")
        return {"ok": ok, "error": self.last_error}
