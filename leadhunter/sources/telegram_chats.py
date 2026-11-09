import asyncio
import logging
import random
import re
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User

from ..config import Settings
from ..models import Lead
from .base import Emit, Source, watch

log = logging.getLogger(__name__)
INVITE_RE = re.compile(r"(?:t\.me/\+|t\.me/joinchat/|^\+)([\w-]+)")


def message_url(chat, msg_id: int) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if isinstance(chat, Channel):
        return f"https://t.me/c/{chat.id}/{msg_id}"
    return ""


def sender_info(sender) -> tuple[str, str]:
    if isinstance(sender, User):
        name = " ".join(filter(None, [sender.first_name, sender.last_name])) or str(sender.id)
        contact = f"@{sender.username}" if sender.username else f"tg://user?id={sender.id}"
        return name, contact
    if sender is not None:
        return getattr(sender, "title", "") or str(sender.id), ""
    return "", ""


class TelegramChatsSource(Source):
    """Читает группы через обычный аккаунт (Telethon): realtime + периодический глобальный поиск.
    Умеет искать и вступать в чаты по команде из бота."""

    name = "telegram"

    def __init__(self, s: Settings):
        self.client = TelegramClient(s.tg_session, s.tg_api_id, s.tg_api_hash)
        self.chats = s.tg_chats
        self.join_chats = s.tg_join_chats
        self.search_words = s.tg_search_words
        self.interval = s.tg_search_interval
        self.me_id: int | None = None
        # Хуки: статистика сообщений по чатам (chat_key, title, messages, leads)
        self.on_message_stats = None
        self._bio_cache: dict[int, str] = {}

    async def get_bio(self, user_id: int) -> str:
        """Bio автора — ИИ по нему понимает, бизнес это или частник."""
        if user_id in self._bio_cache:
            return self._bio_cache[user_id]
        try:
            full = await self.client(functions.users.GetFullUserRequest(user_id))
            bio = (full.full_user.about or "").strip()
        except Exception:
            bio = ""
        if len(self._bio_cache) > 5000:
            self._bio_cache.clear()
        self._bio_cache[user_id] = bio
        return bio

    async def recent_messages(self, chat, limit: int = 50) -> list[str]:
        """Последние сообщения чата — для оценки, есть ли там заказчики."""
        out = []
        async for m in self.client.iter_messages(chat, limit=limit):
            if m.raw_text:
                out.append(m.raw_text)
        return out

    def _to_lead(self, message, chat, sender) -> Lead:
        author, contact = sender_info(sender)
        chat_title = getattr(chat, "title", "") or getattr(chat, "username", "") or str(getattr(chat, "id", ""))
        return Lead(
            source="telegram",
            external_id=f"{getattr(chat, 'id', 0)}:{message.id}",
            text=message.raw_text or "",
            title=f"Чат: {chat_title}",
            url=message_url(chat, message.id),
            author=author,
            contact=contact,
            found_at=message.date or datetime.now(timezone.utc),
            extra={"chat_key": str(getattr(chat, "id", "")), "chat_title": chat_title,
                   "sender_id": getattr(sender, "id", None)},
        )

    # --- управление из бота ---
    async def find_chats(self, query: str, limit: int = 15) -> list[dict]:
        r = await self.client(functions.contacts.SearchRequest(q=query, limit=limit))
        out = []
        for c in r.chats:
            if isinstance(c, Channel) and c.username:
                out.append({"title": c.title, "username": c.username, "members": getattr(c, "participants_count", None),
                            "is_group": bool(c.megagroup)})
        return out

    async def join(self, target: str) -> str:
        m = INVITE_RE.search(target)
        if m:
            r = await self.client(functions.messages.ImportChatInviteRequest(m.group(1)))
        else:
            r = await self.client(functions.channels.JoinChannelRequest(target.lstrip("@").split("/")[-1]))
        chat = r.chats[0] if r.chats else None
        return getattr(chat, "title", target)

    async def list_chats(self) -> list[dict]:
        out = []
        async for d in self.client.iter_dialogs():
            if d.is_group or d.is_channel:
                out.append({"title": d.title, "id": d.entity.id, "username": getattr(d.entity, "username", None)})
        return out

    async def send_dm(self, contact: str, text: str) -> None:
        """Личное сообщение клиенту с вашего аккаунта. Лимит в день задаётся в настройках — спам = бан."""
        if contact.startswith("tg://user?id="):
            entity = int(contact.split("=")[1])
        else:
            entity = contact.lstrip("@")
        await self.client.send_message(entity, text)

    # --- основной цикл ---
    async def run(self, emit: Emit) -> None:
        # client.start() без сессии просит код через input() — в Docker это EOFError/зависание
        await self.client.connect()
        if not await self.client.is_user_authorized():
            self.last_error = "аккаунт не авторизован: python -m leadhunter.tg_login"
            log.error("Telegram-аккаунт не авторизован: выполните python -m leadhunter.tg_login")
            await self._alert("⚠️ telegram: аккаунт не авторизован, источник выключен. "
                              "Выполните: python -m leadhunter.tg_login")
            await self.client.disconnect()
            return
        me = await self.client.get_me()
        self.me_id = me.id
        log.info("telegram: вошли как %s", me.username or me.id)
        for i, target in enumerate(self.join_chats):
            if i:
                await asyncio.sleep(random.uniform(5, 15))  # вступление пачкой = FloodWait/бан
            for attempt in range(2):
                try:
                    log.info("telegram: вступили в %s", await self.join(target))
                    break
                except FloodWaitError as e:
                    log.warning("telegram: FloodWait %s с, вступление в %s", e.seconds, target)
                    await asyncio.sleep(min(e.seconds + 1, 600))
                except Exception as e:
                    log.warning("telegram: не удалось вступить в %s: %s", target, e)
                    break

        chats = self.chats or None

        @self.client.on(events.NewMessage(chats=chats, incoming=True))
        async def on_message(event):
            try:
                if not (event.is_group or event.is_channel) or not event.raw_text:
                    return
                sender = await event.get_sender()
                if isinstance(sender, User) and (sender.bot or sender.id == self.me_id):
                    return
                chat = await event.get_chat()
                if self.on_message_stats:
                    try:
                        await self.on_message_stats(str(chat.id), getattr(chat, "title", "") or "", 1, 0)
                    except Exception:
                        log.debug("chat stats failed", exc_info=True)
                await emit(self._to_lead(event.message, chat, sender))
            except Exception:
                log.exception("telegram: ошибка обработки сообщения")

        watch(asyncio.create_task(self._search_loop(emit), name="telegram-search"))
        await self.client.run_until_disconnected()

    async def _search_loop(self, emit: Emit) -> None:
        # Подстраховка от пропусков realtime: глобальный поиск по словам во всех диалогах аккаунта
        since = datetime.now(timezone.utc) - timedelta(hours=6)
        while True:
            started = datetime.now(timezone.utc)
            try:
                for word in self.search_words:
                    async for m in self.client.iter_messages(None, search=word, limit=50):
                        if m.date and m.date < since:
                            break
                        chat = await m.get_chat()
                        if isinstance(chat, User) or not (m.raw_text or ""):
                            continue
                        if self.chats and not isinstance(chat, (Channel, Chat)):
                            continue
                        await emit(self._to_lead(m, chat, await m.get_sender()))
                    await asyncio.sleep(2)
                self.last_ok, self.last_error = started.timestamp(), None
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.exception("telegram: ошибка глобального поиска")
            since = started - timedelta(minutes=5)
            await asyncio.sleep(self.interval)
