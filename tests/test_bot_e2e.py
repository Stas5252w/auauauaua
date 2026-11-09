"""Сквозной прогон всех команд и кнопок бота через aiogram с подменённым Bot API."""
import json
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from leadhunter.bot.callbacks import build_callbacks
from leadhunter.bot.guard import Guard
from leadhunter.bot.handlers import BotDeps, build_router
from leadhunter.bot.notify import TelegramNotifier
from leadhunter.bot.workspace import Workspace
from leadhunter.db import Database
from leadhunter.models import Lead
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Pipeline

OWNER = 761405171
OTHER = 555


class FakeSession(BaseSession):
    """Перехватывает вызовы Bot API, отвечает правдоподобными объектами, запоминает всё отправленное."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self._msg_id = 100

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout=None):
        name = type(method).__name__
        data = method.model_dump(exclude_none=True)
        self.calls.append((name, data))
        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="bot", username="testbot")
        if name in ("SendMessage", "SendDocument"):
            self._msg_id += 1
            chat_id = data["chat_id"]
            return Message(message_id=self._msg_id, date=datetime.now(timezone.utc),
                           chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
                           text=data.get("text", ""), message_thread_id=data.get("message_thread_id"))
        if name in ("EditMessageText", "EditMessageReplyMarkup"):
            return True
        if name == "AnswerCallbackQuery":
            return True
        if name == "GetChat":
            from aiogram.types import AcceptedGiftTypes, ChatFullInfo
            return ChatFullInfo(id=data["chat_id"], type="supergroup", title="Лиды", is_forum=True,
                                accent_color_id=0, max_reaction_count=0,
                                accepted_gift_types=AcceptedGiftTypes(unlimited_gifts=False, limited_gifts=False,
                                                                      unique_gifts=False, premium_subscription=False, gifts_from_channels=False))
        if name == "GetChatMember":
            from aiogram.types import ChatMemberAdministrator
            return ChatMemberAdministrator(user=User(id=data["user_id"], is_bot=data["user_id"] == 1, first_name="x"),
                                           can_be_edited=False, is_anonymous=False, can_manage_chat=True,
                                           can_delete_messages=True, can_manage_video_chats=True, can_restrict_members=True,
                                           can_promote_members=False, can_change_info=True, can_invite_users=True,
                                           can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
                                           can_manage_topics=True, can_send_welcome_messages=False)
        if name == "CreateForumTopic":
            from aiogram.types import ForumTopic
            self._msg_id += 1
            return ForumTopic(message_thread_id=self._msg_id, name=data["name"], icon_color=data.get("icon_color", 0))
        raise AssertionError(f"unmocked method {name}")

    def sent_texts(self, chat_id=None) -> list[str]:
        return [d.get("text", "") for n, d in self.calls if n == "SendMessage" and (chat_id is None or d["chat_id"] == chat_id)]

    def reset(self):
        self.calls.clear()


class Env:
    def __init__(self, bot, dp, session, db, notifier, pipeline):
        self.bot, self.dp, self.session, self.db, self.notifier, self.pipeline = bot, dp, session, db, notifier, pipeline
        self._uid = 0

    def _update(self, **kw) -> Update:
        self._uid += 1
        return Update(update_id=self._uid, **kw)

    async def cmd(self, text: str, user_id: int = OWNER, chat_id: int | None = None) -> list[str]:
        chat_id = chat_id or user_id
        self.session.reset()
        msg = Message(message_id=self._uid + 1000, date=datetime.now(timezone.utc),
                      chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
                      from_user=User(id=user_id, is_bot=False, first_name="Стас", username="Butov52"), text=text,
                      entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None)
        await self.dp.feed_update(self.bot, self._update(message=msg))
        return self.session.sent_texts()

    async def claim(self, user_id: int = OWNER) -> list[str]:
        """Привязка владельца по одноразовому коду (как его берут из логов)."""
        code = await self.bot_guard.ensure_claim_code()
        return await self.cmd(f"/start {code}", user_id=user_id)

    async def tap(self, data: str, user_id: int = OWNER, chat_id: int | None = None, msg_text: str = "карточка") -> list[tuple[str, dict]]:
        chat_id = chat_id or user_id
        self.session.reset()
        msg = Message(message_id=5, date=datetime.now(timezone.utc), chat=Chat(id=chat_id, type="private"),
                      from_user=User(id=1, is_bot=True, first_name="bot"), text=msg_text)
        cq = CallbackQuery(id="cq1", from_user=User(id=user_id, is_bot=False, first_name="Стас"), chat_instance="ci",
                           data=data, message=msg)
        await self.dp.feed_update(self.bot, self._update(callback_query=cq))
        return list(self.session.calls)


@pytest.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    await db.connect()
    session = FakeSession()
    bot = Bot("1:TEST", session=session)
    notifier = TelegramNotifier(bot, db, [], hot_score=8)
    pipeline = Pipeline(db, notifier, default_threshold=5)
    guard = Guard(bot, db, [])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), Workspace(bot, db), None, "профиль", 0, 24)
    deps.extra["redeliver"] = notifier.notify_row
    dp = Dispatcher()
    dp.include_router(build_router(deps))
    dp.include_router(build_callbacks(deps))
    e = Env(bot, dp, session, db, notifier, pipeline)
    e.bot_guard = guard
    yield e
    await db.close()
    await session.close()


async def seed(env, n=3):
    texts = ["Ищу кто сделает сайт для салона, бюджет 40 000 руб", "Нужен телеграм бот для записи клиентов срочно",
             "Посоветуйте кто делает мультики для детского центра"]
    for i, t in enumerate(texts[:n]):
        await env.pipeline.handle(Lead(source="telegram", external_id=f"c:{i}", text=t, contact=f"@user{i}",
                                       extra={"chat_key": "chat1", "chat_title": "Бизнес чат"}))


async def test_start_makes_owner_and_redelivers_backlog(env):
    await seed(env)  # владельца ещё нет — лиды легли в базу без доставки
    assert len(await env.db.undelivered_leads()) == 3
    texts = await env.claim()
    assert "ищу вам клиентов" in texts[0] and "нашёл 3 лидов" in texts[1]
    assert sum("Открыть источник" in t or "#" in t for t in texts[2:]) == 3
    assert await env.db.get_setting("owner_ids") == str(OWNER)
    assert await env.db.undelivered_leads() == []
    # чужой — молчим
    assert await env.cmd("/start", user_id=OTHER) == []
    assert await env.cmd("/start " + "ZZZZZZZZ", user_id=OTHER) == []
    assert await env.cmd("/stats", user_id=OTHER) == []


async def test_every_command_answers(env):
    await env.claim()
    await seed(env)
    expectations = [
        ("/help", "Я ищу лидов"), ("/stats", "Лидов за 24ч: 3"), ("/leads 5", "#3"), ("/digest", "Дайджест"),
        ("/funnel 7 source", "Воронка"), ("/sources", "Источники"), ("/keywords", "Тема"), ("/rules", "Правил нет"),
        ("/rule_add +сайт -курс", "добавлено"), ("/rules", "+сайт -курс"), ("/rule_del 1", "Удалил"), ("/rule_add (((", "Ошибка"),
        ("/geo", "выключен"), ("/geo Москва, Тула", "Москва"), ("/geo off", "выключен"),
        ("/threshold", "Текущий порог"), ("/threshold 6", "Порог: 6"), ("/profile", "профиль"), ("/profile Делаем сайты", "обновлён"),
        ("/phrases", "нужен сайт"), ("/phrases_set а; б", "Сохранил 2"), ("/phrases_gen", "ИИ не подключён"),
        ("/kw_add topic квиз сайт", "Добавил"), ("/kw_del квиз", "Удалил"), ("/kw_add bad x", "Формат"),
        ("/template_set сайт Привет!", "сохранён"), ("/templates", "Привет!"), ("/block @spam", "в игноре"),
        ("/ignored", "@spam"), ("/unblock @spam", "Убрал"), ("/pause", "паузе"), ("/resume", "включены"),
        ("/team", "Команда"), ("/team_del 1", "Нет такого"), ("/tg_find x", "не подключён"), ("/tg_join @x", "не подключён"),
        ("/tg_chats", "не подключён"), ("/chats_rating", "Бизнес чат"), ("/discover", "Telegram-аккаунт"),
        ("/setup_group", "внутри группы"),
    ]
    for cmd, expected in expectations:
        texts = await env.cmd(cmd)
        assert texts and expected in texts[-1], f"{cmd!r}: ожидали {expected!r}, получили {texts!r}"
    env.session.reset()
    await env.cmd("/export")
    assert any(n == "SendDocument" for n, _ in env.session.calls)


async def test_buttons_full_funnel(env):
    await env.claim()
    await seed(env, 1)
    calls = await env.tap("take:1")
    answers = [d for n, d in calls if n == "AnswerCallbackQuery"]
    assert "Ваш лид" in answers[0]["text"]
    row = await env.db.get_lead(1)
    assert row["status"] == "taken" and row["assigned_to"] == OWNER
    # второй не может перехватить
    calls = await env.tap("take:1", user_id=OTHER)
    assert "Нет доступа" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    await env.db.add_member(OTHER, "Боря")
    calls = await env.tap("take:1", user_id=OTHER)
    assert "Уже взял" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    calls = await env.tap("st:1:contacted")
    assert "Напомню" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    assert (await env.db.get_lead(1))["followup_at"] is not None
    await env.tap("fu:1:72")
    await env.tap("st:1:replied")
    calls = await env.tap("st:1:deal")
    assert "Поздравляю" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    assert (await env.db.get_lead(1))["status"] == "deal"
    calls = await env.tap("dr:1")
    assert "Черновика нет" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    await env.db.set_template("сайт", "Здравствуйте!")
    calls = await env.tap("dr:1")
    assert any("Здравствуйте!" in d.get("text", "") for n, d in calls if n == "SendMessage")
    calls = await env.tap("mute:1")
    assert "в игноре" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    assert "chat1" in (await env.db.get_ignored())["chat"]
    calls = await env.tap("send:1")
    assert "Нечего отправлять" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    texts = await env.cmd("/team")
    assert "Стас" in texts[0] and "сделок 1" in texts[0]


async def test_setup_group_creates_topics_and_routes_leads(env):
    await env.claim()
    group = -1001234567890
    texts = await env.cmd("/setup_group", chat_id=group)
    assert "Готово" in texts[-1]
    created = [d["name"] for n, d in env.session.calls if n == "CreateForumTopic"]
    assert "🔥 Горячие" in created and len(created) == 12
    assert await env.db.get_setting("workspace_chat_id") == str(group)
    # лид уходит в тему, а не в личку
    env.session.reset()
    await env.pipeline.handle(Lead(source="telegram", external_id="g:1", text="Срочно нужен сайт и бот для магазина, бюджет есть"))
    sends = [d for n, d in env.session.calls if n == "SendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == group and sends[0]["message_thread_id"] == int(await env.db.get_setting("topic:hot"))
    assert sends[0]["disable_notification"] is False  # горячий — со звуком
    env.session.reset()
    await env.pipeline.handle(Lead(source="kwork", external_id="k:1", title="Нужен лендинг", text="Лендинг для студии йоги", intent_implied=True))
    sends = [d for n, d in env.session.calls if n == "SendMessage"]
    assert sends[0]["message_thread_id"] == int(await env.db.get_setting("topic:boards")) and sends[0]["disable_notification"] is True
    # участник группы становится членом команды при первом нажатии
    row = await env.db.get_lead(1)
    calls = await env.tap(f"take:{row['id']}", user_id=OTHER, chat_id=group)
    assert "Ваш лид" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]
    assert await env.db.is_member(OTHER)
