"""Рабочее пространство — форум-группа Telegram с темами под каждый тип лида.

Бот должен быть админом с правом «Управление темами». Команда /setup_group внутри группы создаёт темы
и запоминает их id; дальше каждый лид уходит в свою тему, горячие — со звуком."""
import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from ..db import Database
from ..models import Lead
from ..pipeline import Enriched

log = logging.getLogger(__name__)

# key -> (название темы, цвет иконки из допустимых Bot API)
TOPICS: dict[str, tuple[str, int]] = {
    "hot": ("🔥 Горячие", 0xFF93B2),
    "сайт": ("🌐 Сайты", 0x6FB9F0),
    "бот": ("🤖 Боты", 0x8EEE98),
    "автоматизация": ("⚙️ Автоматизация", 0xFFD67E),
    "мультик": ("🎬 Мультики и видео", 0xCB86DB),
    "другое": ("📌 Остальное", 0x6FB9F0),
    "business": ("🗺 Бизнес без сайта", 0xFB6F5F),
    "audit": ("🩺 Аудит сайтов", 0xFB6F5F),
    "boards": ("📋 Биржи", 0xFFD67E),
    "reports": ("📊 Отчёты", 0x8EEE98),
    "content": ("✍️ Контент", 0xCB86DB),
    "health": ("⚠️ Здоровье", 0xFF93B2),
}
BOARD_SOURCES = {"kwork", "weblancer", "freelance.ru", "hh.ru"}
WORKSPACE_KEY = "workspace_chat_id"


def route(lead: Lead, info: Enriched, hot_score: int) -> str:
    """В какую тему класть лида."""
    if lead.source == "yandex_maps":
        return "business"
    if lead.source == "site_audit":
        return "audit"
    if info.score >= hot_score or info.urgency == "высокая":
        return "hot"
    if lead.source in BOARD_SOURCES or lead.source.startswith("rss:"):
        return "boards"
    return info.service if info.service in TOPICS else "другое"


def message_link(chat_id: int, thread_id: int | None, msg_id: int) -> str:
    internal = str(chat_id).removeprefix("-100")
    return f"https://t.me/c/{internal}/{thread_id}/{msg_id}" if thread_id else f"https://t.me/c/{internal}/{msg_id}"


class Workspace:
    PACE = 0.3  # сек между сообщениями в группу

    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self._last_send = 0.0

    async def chat_id(self) -> int | None:
        raw = await self.db.get_setting(WORKSPACE_KEY)
        return int(raw) if raw else None

    async def topic(self, key: str) -> int | None:
        raw = await self.db.get_setting(f"topic:{key}")
        return int(raw) if raw else None

    async def setup(self, chat_id: int) -> list[str]:
        """Создаёт недостающие темы. Возвращает список проблем (пусто = всё готово)."""
        chat = await self.bot.get_chat(chat_id)
        if not chat.is_forum:
            return ["В настройках группы включите «Темы» (Topics), затем повторите /setup_group."]
        me = await self.bot.get_chat_member(chat_id, (await self.bot.me()).id)
        if getattr(me, "status", "") not in ("administrator", "creator") or not getattr(me, "can_manage_topics", False):
            return ["Сделайте бота администратором с правом «Управление темами»."]
        problems = []
        for key, (name, color) in TOPICS.items():
            if await self.db.get_setting(f"topic:{key}"):
                continue
            try:
                t = await self.bot.create_forum_topic(chat_id, name, icon_color=color)
                await self.db.set_setting(f"topic:{key}", str(t.message_thread_id))
            except TelegramBadRequest as e:
                problems.append(f"{name}: {e.message}")
        await self.db.set_setting(WORKSPACE_KEY, str(chat_id))
        return problems

    async def reset(self) -> None:
        for key in TOPICS:
            await self.db.set_setting(f"topic:{key}", "")
        await self.db.set_setting(WORKSPACE_KEY, "")

    async def send(self, key: str, text: str, *, silent: bool = True, **kwargs):
        """Сообщение в тему. Если темы нет (удалили руками) — в General."""
        chat_id = await self.chat_id()
        if not chat_id:
            return None
        thread_id = await self.topic(key)
        try:
            return await self._send_paced(chat_id, text, message_thread_id=thread_id,
                                          disable_notification=silent, disable_web_page_preview=True, **kwargs)
        except TelegramBadRequest as e:
            if "thread not found" in e.message.lower() or "TOPIC_DELETED" in e.message:
                await self.db.set_setting(f"topic:{key}", "")
                return await self._send_paced(chat_id, text, disable_notification=silent,
                                              disable_web_page_preview=True, **kwargs)
            raise

    async def _send_paced(self, chat_id: int, text: str, **kwargs):
        # Пауза между подряд идущими карточками + один повтор после 429, иначе лид уезжал бы в личку владельца
        wait = self.PACE - (time.monotonic() - self._last_send)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            try:
                return await self.bot.send_message(chat_id, text, **kwargs)
            except TelegramRetryAfter as e:
                await asyncio.sleep(min(e.retry_after, 60))
                return await self.bot.send_message(chat_id, text, **kwargs)
        finally:
            self._last_send = time.monotonic()
