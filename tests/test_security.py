"""Безопасность: привязка владельца по коду, членство команды в рабочей группе."""
from types import SimpleNamespace

import pytest
from aiogram.types import User

from leadhunter.bot import guard as guard_mod
from leadhunter.bot.guard import Guard
from leadhunter.db import Database
from leadhunter.models import Lead
from test_bot_e2e import OTHER, OWNER, env  # noqa: F401  (фикстура env)

NOT_BOUND = "Бот ещё не привязан к владельцу. Код — в логах сервера (docker compose logs / journalctl)."
GROUP = -1001234567890


# ---------- привязка владельца ----------

async def test_stranger_start_does_not_become_owner(env):  # noqa: F811
    assert await env.cmd("/start", user_id=OTHER) == [NOT_BOUND]
    assert await env.db.get_setting("owner_ids") in (None, "")
    assert not await env.bot_guard.is_owner(OTHER)
    assert await env.cmd("/stats", user_id=OTHER) == []


async def test_wrong_code_fails_and_correct_code_is_consumed(env, caplog):  # noqa: F811
    code = await env.bot_guard.ensure_claim_code()
    assert len(code) == 8
    assert await env.cmd("/start WRONG123", user_id=OTHER) == [NOT_BOUND]
    assert not await env.bot_guard.is_owner(OTHER)
    texts = await env.cmd(f"/start {code}", user_id=OWNER)
    assert "ищу вам клиентов" in texts[0]
    assert await env.db.get_setting("owner_ids") == str(OWNER)
    assert not await env.db.get_setting("owner_claim_code")  # код израсходован
    # повторное использование кода чужим — безрезультатно
    assert await env.cmd(f"/start {code}", user_id=OTHER) == []
    assert await env.db.get_setting("owner_ids") == str(OWNER)


async def test_claim_code_is_logged_and_stable_until_used(env, caplog):  # noqa: F811
    with caplog.at_level("WARNING"):
        code = await env.bot_guard.ensure_claim_code()
    assert f"/start {code}" in caplog.text and "Владелец не задан" in caplog.text
    assert await env.bot_guard.ensure_claim_code() == code


async def test_brute_force_rotates_code(env):  # noqa: F811
    code = await env.bot_guard.ensure_claim_code()
    for i in range(guard_mod.MAX_CLAIM_FAILS):
        assert not await env.bot_guard.claim_owner(OTHER, f"BAD{i}")
    assert await env.bot_guard.ensure_claim_code() != code
    assert not await env.bot_guard.claim_owner(OTHER, code)


async def test_no_claim_code_when_owner_configured(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    await db.connect()
    g = Guard(None, db, [OWNER])
    assert await g.ensure_claim_code() is None and not await g.claim_owner(OTHER, "ANY")
    await db.close()


# ---------- команда рабочей группы ----------

class FakeBot:
    def __init__(self, status="member"):
        self.status, self.calls = status, 0

    async def get_chat_member(self, chat_id, user_id):
        self.calls += 1
        if isinstance(self.status, Exception):
            raise self.status
        return SimpleNamespace(status=self.status)


@pytest.fixture
async def gdb(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    await db.connect()
    await db.set_setting("workspace_chat_id", str(GROUP))
    yield db
    await db.close()


def user(uid=OTHER):
    return User(id=uid, is_bot=False, first_name="Боря")


async def test_regular_member_allowed_in_group_but_not_persisted(gdb):
    bot = FakeBot("member")
    g = Guard(bot, gdb, [OWNER])
    assert await g.allowed(user(), GROUP)
    assert not await gdb.is_member(OTHER)  # доступа к личке/Mini App/экспорту нет
    assert not await g.allowed(user(), OTHER)  # вне группы — нет
    assert not await g.allowed(user(), -100999)


async def test_admin_is_persisted(gdb):
    g = Guard(FakeBot("administrator"), gdb, [OWNER])
    assert await g.allowed(user(), GROUP) and await gdb.is_member(OTHER)


async def test_left_member_denied_and_cache_ttl(gdb, monkeypatch):
    bot = FakeBot("member")
    g = Guard(bot, gdb, [OWNER])
    assert await g.allowed(user(), GROUP) and await g.allowed(user(), GROUP)
    assert bot.calls == 1  # второй раз — из кэша
    bot.status = "left"
    assert await g.allowed(user(), GROUP)  # кэш ещё живой
    now = guard_mod.time.monotonic()
    monkeypatch.setattr(guard_mod.time, "monotonic", lambda: now + guard_mod.MEMBER_TTL + 1)
    assert not await g.allowed(user(), GROUP) and bot.calls == 2
    assert not await gdb.is_member(OTHER)


async def test_api_error_denies(gdb):
    g = Guard(FakeBot(RuntimeError("boom")), gdb, [OWNER])
    assert not await g.allowed(user(), GROUP)


async def test_group_member_can_take_lead_but_miniapp_closed(env):  # noqa: F811
    await env.claim()
    await env.cmd("/setup_group", chat_id=GROUP)
    env.bot_guard.bot = FakeBot("member")
    await env.pipeline.handle(Lead(source="telegram", external_id="g:1", text="Срочно нужен сайт и бот для магазина, бюджет есть"))
    calls = await env.tap("take:1", user_id=OTHER, chat_id=GROUP)
    assert "Ваш лид" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    assert not await env.db.is_member(OTHER)
    # команды вне группы (в личке) обычному участнику недоступны
    assert await env.cmd("/export", user_id=OTHER) == []
