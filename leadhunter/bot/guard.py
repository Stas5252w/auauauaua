"""Проверка доступа: владельцы из .env/по коду привязки + команда.
В рабочей группе её участники действуют без постоянного членства — только пока состоят в группе."""
import logging
import secrets
import time

from aiogram import Bot
from aiogram.types import User

from ..db import Database
from .workspace import WORKSPACE_KEY

log = logging.getLogger(__name__)

CLAIM_KEY = "owner_claim_code"
CLAIM_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MEMBER_TTL = 300  # сек: как долго помнить результат get_chat_member
MAX_CLAIM_FAILS = 10  # после стольких неверных кодов выпускаем новый


class Guard:
    def __init__(self, bot: Bot, db: Database, static_owners: list[int]):
        self.bot = bot
        self.db = db
        self.static_owners = static_owners
        self._member_cache: dict[int, tuple[float, str | None]] = {}
        self._claim_fails = 0

    async def owners(self) -> list[int]:
        stored = await self.db.get_setting("owner_ids", "")
        return self.static_owners or [int(x) for x in stored.split(",") if x]

    async def is_owner(self, user_id: int) -> bool:
        return user_id in await self.owners()

    async def has_owner(self) -> bool:
        return bool(await self.owners())

    async def ensure_claim_code(self, announce: bool = False) -> str | None:
        """Одноразовый код привязки владельца; None, если владелец уже есть."""
        if await self.has_owner():
            return None
        code = await self.db.get_setting(CLAIM_KEY)
        if not code:
            code = "".join(secrets.choice(CLAIM_ALPHABET) for _ in range(8))
            await self.db.set_setting(CLAIM_KEY, code)
            self._claim_fails = 0
            announce = True
        if announce:
            log.warning("Владелец не задан. Напишите боту: /start %s (одноразовый код; лучше задать OWNER_IDS в .env)", code)
        return code

    async def claim_owner(self, user_id: int, code: str) -> bool:
        expected = await self.ensure_claim_code()
        if not expected or not code or not secrets.compare_digest(code.strip().upper().encode(), expected.encode()):
            if expected:
                self._claim_fails += 1
                if self._claim_fails >= MAX_CLAIM_FAILS:  # перебор — сжигаем код и выпускаем новый
                    await self.db.set_setting(CLAIM_KEY, "")
                    await self.ensure_claim_code()
            return False
        await self.db.set_setting("owner_ids", str(user_id))
        await self.db.set_setting(CLAIM_KEY, "")
        return True

    async def _in_workspace(self, chat_id: int, user: User) -> str | None:
        """Статус пользователя в рабочей группе (кэш ~5 минут) или None, если его там нет."""
        now = time.monotonic()
        hit = self._member_cache.get(user.id)
        if hit and now - hit[0] < MEMBER_TTL:
            return hit[1]
        try:
            m = await self.bot.get_chat_member(chat_id, user.id)
            status = m.status if m.status in ("member", "administrator", "creator") else None
        except Exception:
            return None
        self._member_cache[user.id] = (now, status)
        return status

    async def allowed(self, user: User, chat_id: int | None = None) -> bool:
        if await self.is_owner(user.id) or await self.db.is_member(user.id):
            return True
        ws = await self.db.get_setting(WORKSPACE_KEY)
        if ws and chat_id is not None and str(chat_id) == ws:
            status = await self._in_workspace(chat_id, user)
            if status in ("administrator", "creator"):  # админам группы доверяем — вносим в команду
                await self.db.add_member(user.id, user.full_name)
            return status is not None
        return False

    @staticmethod
    def name(user: User) -> str:
        return user.full_name or (f"@{user.username}" if user.username else str(user.id))
