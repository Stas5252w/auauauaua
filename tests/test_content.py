"""Контент-фабрика сквозь бота: команды, кнопки, правки с обучением, публикация, планировщик, Mini App и лендинг."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, User
from aiohttp.test_utils import TestClient, TestServer

from leadhunter.bot.callbacks import build_callbacks
from leadhunter.bot.content import build_content_router, format_card
from leadhunter.bot.guard import Guard
from leadhunter.bot.handlers import BotDeps, build_router
from leadhunter.bot.notify import TelegramNotifier
from leadhunter.config import Settings
from leadhunter.content.publish import PublishResult
from leadhunter.content.runtime import build_content
from leadhunter.content.scheduler import autopilot_once, publish_due
from leadhunter.content.video import ffmpeg_available
from leadhunter.miniapp.auth import sign_init_data
from leadhunter.miniapp.server import MiniAppAPI, build_app
from leadhunter.models import Lead
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Pipeline
from test_bot_e2e import OWNER, Env, FakeSession

POST = {"title": "Сколько стоит сайт", "body": "Хук.\n\nСайт стоит от 30 тысяч.\n\nНапишите — посчитаем.",
        "hashtags": ["#сайт", "#бизнес"], "image_prompt": "laptop on desk"}
CAROUSEL = {"title": "5 ошибок на сайте", "slides": [{"title": "5 ошибок", "text": "которые сливают клиентов"}]
            + [{"title": f"Ошибка {i}", "text": "Описание ошибки и как исправить"} for i in range(1, 5)]
            + [{"title": "Хотите аудит?", "text": "Напишите «аудит»"}],
            "caption": "Разбираем ошибки сайтов", "hashtags": ["#сайт"], "image_prompt": "x"}
REEL = {"title": "Бот за день", "hook": "Бот окупается за неделю", "scenes": [
    {"text": "Бот окупается за неделю", "voice": "Расскажу как", "duration": 2},
    {"text": "Запись клиентов 24/7", "voice": "", "duration": 2}], "caption": "Как бот экономит время", "hashtags": ["#бот"]}
MAGNET = {"title": "Чек-лист: сайт, который продаёт", "subtitle": "10 пунктов перед запуском",
          "items": [{"title": f"Пункт {i}", "text": "Что проверить"} for i in range(1, 4)], "cta": "Оставьте контакт",
          "post": "Забирайте чек-лист — напишите «чек-лист»", "hashtags": ["#чеклист"], "image_prompt": "x"}
IDEAS = {"ideas": [{"topic": "Почему сайт не приносит заявки", "kind": "post", "hook": "Хук 1", "why": "боль"},
                   {"topic": "Бот для записи", "kind": "reel", "hook": "Хук 2", "why": "спрос"}]}


class FakeLLM:
    """Отвечает по типу запроса; запоминает системные промпты, чтобы проверить обучение на правках."""

    def __init__(self):
        self.systems: list[str] = []

    async def complete_json(self, system, user, **kw):
        self.systems.append(system)
        if user.startswith("Ниже материал"):
            data = json.loads(user.split("\n\n", 1)[1])
            data["body"] = "ПЕРЕПИСАНО"
            return data
        if "идей" in user:
            return json.loads(json.dumps(IDEAS))
        if "карусель" in user:
            return json.loads(json.dumps(CAROUSEL))
        if "сценарий" in user:
            return json.loads(json.dumps(REEL))
        if "лид-магнит" in user:
            return json.loads(json.dumps(MAGNET))
        return json.loads(json.dumps(POST))


class MediaSession(FakeSession):
    async def make_request(self, bot, method, timeout=None):
        res = await self._request(bot, method, timeout)
        return res.as_(bot) if hasattr(res, "as_") else res  # как настоящая сессия: объекты привязаны к боту

    async def _request(self, bot, method, timeout=None):
        name = type(method).__name__
        if name in ("SendPhoto", "SendVideo", "SendDocument"):
            data = method.model_dump(exclude_none=True)
            data.pop(name[4:].lower(), None)  # сами байты не храним
            self.calls.append((name, data))
            self._msg_id += 1
            return Message(message_id=self._msg_id, date=datetime.now(timezone.utc),
                           chat=Chat(id=data["chat_id"] if isinstance(data["chat_id"], int) else -1001, type="channel"),
                           caption=data.get("caption"))
        if name == "SendMediaGroup":
            data = method.model_dump(exclude_none=True)
            self.calls.append((name, {"chat_id": data["chat_id"], "n": len(data["media"])}))
            self._msg_id += 1
            return [Message(message_id=self._msg_id, date=datetime.now(timezone.utc), chat=Chat(id=-1001, type="channel"))]
        if name == "DeleteMessage":
            self.calls.append((name, {}))
            return True
        if name == "GetChat" and isinstance(method.chat_id, str):
            from aiogram.types import AcceptedGiftTypes, ChatFullInfo
            self.calls.append((name, {"chat_id": method.chat_id}))
            return ChatFullInfo(id=-1009, type="channel", title="Канал", username=method.chat_id.lstrip("@"),
                                accent_color_id=0, max_reaction_count=0,
                                accepted_gift_types=AcceptedGiftTypes(unlimited_gifts=False, limited_gifts=False,
                                                                      unique_gifts=False, premium_subscription=False,
                                                                      gifts_from_channels=False))
        return await super().make_request(bot, method, timeout)

    def names(self):
        return [n for n, _ in self.calls]


class ContentEnv(Env):
    def __init__(self, *a, factory, llm, deps):
        super().__init__(*a)
        self.factory, self.llm, self.deps = factory, llm, deps

    async def text(self, text: str, user_id: int = OWNER) -> list[str]:
        return await self.cmd(text, user_id)

    def answers(self, calls):
        return [d.get("text", "") for n, d in calls if n == "AnswerCallbackQuery"]


async def make_env(tmp_path, llm):
    from leadhunter.db import Database
    db = Database(str(tmp_path / "c.db"))
    await db.connect()
    session = MediaSession()
    bot = Bot("1:TEST", session=session)
    notifier = TelegramNotifier(bot, db, [OWNER], hot_score=8)
    pipeline = Pipeline(db, notifier, default_threshold=5)
    guard = Guard(bot, db, [OWNER])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), notifier.workspace, None, "Студия делает сайты", 0, 24)
    deps.extra["pipeline"] = pipeline
    s = Settings(bot_token="1:TEST", content_dir=str(tmp_path / "content"), _env_file=None)
    factory = await build_content(s, db, bot, llm, deps, notifier)
    dp = Dispatcher()
    dp.include_router(build_router(deps))
    dp.include_router(build_callbacks(deps))
    dp.include_router(build_content_router(deps))
    return ContentEnv(bot, dp, session, db, notifier, pipeline, factory=factory, llm=llm, deps=deps)


@pytest.fixture
async def env(tmp_path):
    e = await make_env(tmp_path, FakeLLM())
    yield e
    await e.db.close()


@pytest.fixture
async def env_noai(tmp_path):
    e = await make_env(tmp_path, None)
    yield e
    await e.db.close()


async def test_post_card_edit_learning_and_variant(env):
    texts = await env.text("/post сколько стоит сайт")
    assert "Сколько стоит сайт" in texts[-1] and "#сайт" in texts[-1]
    assert "SendPhoto" in env.session.names()  # обложка по шаблону — ИИ-картинки нет
    row = await env.factory.store.get(1)
    assert row["kind"] == "post" and row["status"] == "draft" and json.loads(row["media"])["images"]
    assert os.path.exists(env.factory.media.path(json.loads(row["media"])["images"][0]))
    # правка текста → запоминается как пример «было → стало»
    calls = await env.tap("ce:1")
    assert any("Пришлите исправленный" in d.get("text", "") for n, d in calls if n == "SendMessage")
    texts = await env.text("Новый текст поста, написанный владельцем")
    assert "Запомнил" in texts[0]
    assert (await env.factory.store.get(1))["body"] == "Новый текст поста, написанный владельцем"
    assert await env.factory.store.edits("post") == [(POST["body"], "Новый текст поста, написанный владельцем")]
    # следующая генерация видит правку в системном промпте
    await env.text("/post ещё тема")
    assert "БЫЛО:" in env.llm.systems[-1] and "написанный владельцем" in env.llm.systems[-1]
    # переписать по указанию
    await env.tap("cr:1")
    texts = await env.text("короче")
    assert (await env.factory.store.get(1))["body"] == "ПЕРЕПИСАНО" and "ПЕРЕПИСАНО" in texts[-1]
    # другой вариант — новая карточка с parent_id
    await env.tap("cv:1")
    rows = await env.factory.store.recent(10)
    assert any(r["parent_id"] == 1 for r in rows)
    # удалить
    calls = await env.tap("cx:1")
    assert "Удалил" in env.answers(calls)[0] and (await env.factory.store.get(1))["status"] == "rejected"


async def test_publish_schedule_and_package(env):
    await env.text("/post тема")
    calls = await env.tap("cp:1")
    assert "Площадки не настроены" in env.answers(calls)[0]
    texts = await env.text("/channel_set telegram @mychannel")
    assert "Буду публиковать" in texts[0]
    texts = await env.text("/channels")
    assert "✅ Telegram" in texts[0] and "TikTok" in texts[0]
    calls = await env.tap("cp:1")
    sent = [d for n, d in calls if n == "SendPhoto"]
    assert sent and sent[0]["chat_id"] == "@mychannel" and "#сайт" in sent[0]["caption"]
    row = await env.factory.store.get(1)
    assert row["status"] == "published" and json.loads(row["published"])[0]["url"].startswith("https://t.me/mychannel/")
    assert any("Опубликовано" in d.get("text", "") for n, d in calls if n == "SendMessage")
    calls = await env.tap("cp:1")  # двойной клик: повторно не постим
    assert not [d for n, d in calls if n in ("SendPhoto", "SendMessage")] and "Уже опубликовано" in env.answers(calls)[0]
    # расписание: меню → слот → снять
    await env.text("/post вторая")
    calls = await env.tap("cs:2")
    assert "Точное время" in env.answers(calls)[0]
    calls = await env.tap("cs:2:1:10")
    assert "Выйдет" in env.answers(calls)[0]
    row = await env.factory.store.get(2)
    assert row["status"] == "scheduled"
    at = datetime.fromisoformat(row["scheduled_at"]).astimezone(ZoneInfo("Europe/Moscow"))
    assert at.hour == 10 and at > datetime.now(ZoneInfo("Europe/Moscow"))
    texts = await env.text("/queue")
    assert "#2" in texts[0] and "🗓" in texts[0]
    calls = await env.tap("cu:2")
    assert (await env.factory.store.get(2))["status"] == "draft"
    texts = await env.text("/schedule 2 2030-01-15 10:00")
    assert "15.01 10:00" in texts[0] and (await env.factory.store.get(2))["status"] == "scheduled"
    assert "Формат" in (await env.text("/schedule x"))[0]
    # пакет для ручной выкладки
    calls = await env.tap("cpk:2")
    names = [n for n, _ in calls]
    assert "SendDocument" in names and any("<code>" in d.get("text", "") for n, d in calls if n == "SendMessage")
    # статистика и справка
    texts = await env.text("/content")
    assert "Контент-фабрика" in texts[0] and "опубликовано 1" in texts[0]
    assert "Убрал" in (await env.text("/channel_del telegram"))[0]


async def test_carousel_reel_magnet_ideas(env):
    texts = await env.text("/carousel 5 ошибок на сайте")
    assert "6 слайдов" in texts[-1] and ("SendMediaGroup", {"chat_id": OWNER, "n": 6}) in env.session.calls
    if ffmpeg_available():
        texts = await env.text("/reel бот за день")
        assert "видео собрано" in texts[-1] and "SendVideo" in env.session.names()
        row = (await env.factory.store.recent(1))[0]
        assert os.path.getsize(env.factory.media.path(json.loads(row["media"])["video"])) > 0
    texts = await env.text("/magnet сайт который продаёт")
    row = (await env.factory.store.recent(1))[0]
    assert row["kind"] == "magnet" and row["slug"] and f"/lp/{row['slug']}" in texts[-1]
    assert "Пункт 1" in texts[-1] and "Анонс" in texts[-1]
    # идеи из болей клиентов → кнопка формата → материал
    await env.pipeline.handle(Lead(source="kwork", external_id="k1", title="Нужен сайт для стоматологии", text="Бюджет 50 тыс", intent_implied=True))
    await env.text("/ideas 2")
    edited = [d["text"] for n, d in env.session.calls if n == "EditMessageText"]
    assert "Почему сайт не приносит заявки" in edited[-1] and "стоматологии" in env.llm.systems[-1]
    idea = next(r for r in await env.factory.store.recent(10, kind="idea"))
    calls = await env.tap(f"ci:{idea['id']}:post")
    assert "пишу" in env.answers(calls)[0]
    made = (await env.factory.store.recent(1))[0]
    assert made["kind"] == "post" and made["parent_id"] == idea["id"] and made["topic"] == idea["topic"]
    assert (await env.factory.store.get(idea["id"]))["status"] == "published"
    # /plan раскладывает идеи по дням
    await env.text("/plan")
    assert "план на неделю" in [d["text"] for n, d in env.session.calls if n == "EditMessageText"][-1]
    planned = [r for r in await env.factory.store.recent(10, kind="idea") if r["scheduled_at"]]
    assert len(planned) == 2
    # тема вопросом, если не указана
    assert "о чём" in (await env.text("/post"))[0]
    texts = await env.text("тема вторым сообщением")
    assert "Сколько стоит сайт" in texts[-1]


async def test_voice_brand_autopilot_settings(env):
    assert "не задан" in (await env.text("/voice"))[0]
    assert "Запомнил" in (await env.text("/voice на ты, коротко"))[0]
    await env.text("/post тема")
    assert "на ты, коротко" in env.llm.systems[-1]
    assert "Сохранил" in (await env.text("/brand Пиксель | @pixel | #101010 | #FF0000"))[0]
    b = await env.factory.brand()
    assert b.name == "Пиксель" and b.handle == "@pixel" and b.accent == "#FF0000"
    assert "Пиксель" in (await env.text("/brand"))[0]
    assert "включён" in (await env.text("/autopilot on"))[0] and await env.db.get_setting("autopilot") == "1"
    assert "выключен" in (await env.text("/autopilot off"))[0]
    assert "числовой id" in (await env.text("/channel_set vk abc"))[0]
    assert "Буду публиковать" in (await env.text("/channel_set vk -123"))[0]


async def test_without_ai(env_noai):
    env = env_noai
    assert "ИИ не подключён" in (await env.text("/post тема"))[0]
    assert "Идеи генерирует ИИ" in (await env.text("/ideas"))[0]
    texts = await env.text("/carousel Три совета | Совет раз | Совет два")
    assert "3 слайдов" in texts[-1]
    texts = await env.text("/publish Мой собственный пост #свой")
    assert "Мой собственный пост" in texts[-1] and "#свой" in texts[-1]
    calls = await env.tap("cr:2")
    assert "Нужен ИИ" in env.answers(calls)[0]


async def test_scheduler_and_autopilot(env):
    class Pub:
        platform, label = "telegram", "Telegram @t"

        def __init__(self):
            self.calls = []

        async def publish(self, text, images=(), video_path=None):
            self.calls.append((text, len(images), video_path))
            return PublishResult("telegram", "https://t.me/t/1")

    class Broken(Pub):
        platform, label = "vk", "VK"

        async def publish(self, *a, **k):
            raise RuntimeError("токен протух")

    notes = []

    async def notify(t):
        notes.append(t)

    await env.text("/post тема")
    await env.factory.store.schedule(1, datetime.now(timezone.utc) - timedelta(minutes=1))
    pub = Pub()
    assert await publish_due(env.factory, [pub, Broken()], notify) == 1
    assert pub.calls[0][1] == 1 and "#сайт" in pub.calls[0][0]
    row = await env.factory.store.get(1)
    # VK упал — это не теряется: частично, ошибка в карточке и в сообщении
    assert row["status"] == "partial" and "частично" in notes[0] and "VK: токен протух" in notes[0]
    assert json.loads(row["publish_errors"])[0]["platform"] == "vk" and "❌ VK" in format_card(row)
    # без площадок — возвращаем в черновики и предупреждаем
    await env.text("/post вторая")
    await env.factory.store.schedule(2, datetime.now(timezone.utc) - timedelta(minutes=1))
    await publish_due(env.factory, [], notify)
    assert (await env.factory.store.get(2))["status"] == "draft" and "площадки не настроены" in notes[-1]
    # все площадки упали — тоже в черновики
    await env.factory.store.schedule(2, datetime.now(timezone.utc) - timedelta(minutes=1))
    await publish_due(env.factory, [Broken()], notify)
    assert (await env.factory.store.get(2))["status"] == "draft" and "токен протух" in notes[-1]

    # автопилот: выключен → ничего; включён → раз в день приносит черновик по плану
    delivered = []

    async def deliver(row):
        delivered.append(row["id"])

    tz = "Europe/Moscow"
    assert await autopilot_once(env.factory, deliver, tz, 10) is None
    await env.db.set_setting("autopilot", "1")
    now = datetime.now(ZoneInfo(tz)).replace(hour=9)
    assert await autopilot_once(env.factory, deliver, tz, 10, now=now) is None  # рано
    await env.text("/plan")
    ideas = await env.factory.store.recent(10, kind="idea")
    await env.factory.store.update(ideas[-1]["id"], scheduled_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    now = now.replace(hour=11)
    made = await autopilot_once(env.factory, deliver, tz, 10, now=now)
    assert made and delivered == [made]
    row = await env.factory.store.get(made)
    assert row["created_by"] == "автопилот" and row["topic"] == ideas[-1]["topic"]
    assert await autopilot_once(env.factory, deliver, tz, 10, now=now) is None  # сегодня уже приносил
    # доставка в команду идёт через тему «Контент» рабочей группы
    group = -1001234567890
    await env.cmd("/setup_group", chat_id=group)
    env.session.reset()
    await env.deps.extra["deliver_to_team"](row)
    sends = [d for n, d in env.session.calls if n == "SendMessage"]
    assert sends[-1]["chat_id"] == group and sends[-1]["message_thread_id"] == int(await env.db.get_setting("topic:content"))


# --- Mini App ---
TOKEN = "1:TEST"


def headers(user_id=OWNER):
    return {"X-Init-Data": sign_init_data({"auth_date": int(time.time()), "query_id": "q",
                                           "user": {"id": user_id, "first_name": "Стас"}}, TOKEN)}


@pytest.fixture
async def client(env):
    api = MiniAppAPI(env.db, TOKEN, env.deps.guard, env.deps, [])
    async with TestClient(TestServer(build_app(api))) as c:
        yield c


async def test_miniapp_content_api_and_landing(client, env):
    assert (await client.get("/api/content")).status == 401
    r = await client.post("/api/content", json={"kind": "magnet", "topic": "чек-лист"}, headers=headers())
    assert r.status == 200
    item = (await r.json())["item"]
    assert item["kind"] == "magnet" and item["images"][0].startswith("/media/") and item["slug"]
    assert (await client.get(item["images"][0])).status == 200
    r = await client.get("/api/content?status=draft", headers=headers())
    assert [i["id"] for i in (await r.json())["items"]] == [item["id"]]
    # правка через API — тоже обучение
    r = await client.post(f"/api/content/{item['id']}", json={"action": "edit", "caption": "новая подпись"}, headers=headers())
    assert (await r.json())["item"]["caption"] == "новая подпись"
    assert await env.factory.store.edits("magnet") == [("", "новая подпись")]
    r = await client.post(f"/api/content/{item['id']}", json={"action": "schedule", "at": "2030-01-01T10:00"}, headers=headers())
    assert (await r.json())["item"]["status"] == "scheduled"
    r = await client.post(f"/api/content/{item['id']}", json={"action": "publish"}, headers=headers())
    assert r.status == 400 and "площадки" in (await r.json())["error"]
    await env.factory.store.set_channel("telegram", "@chan")
    r = await client.post(f"/api/content/{item['id']}", json={"action": "publish"}, headers=headers())
    body = await r.json()
    assert body["item"]["status"] == "published" and body["results"][0]["platform"] == "telegram"
    r = await client.get("/api/content/channels", headers=headers())
    assert (await r.json())["channels"][0]["platform"] == "telegram"
    # публичный лендинг: открывается без авторизации, заявка становится горячим лидом
    r = await client.get(f"/lp/{item['slug']}")
    page = await r.text()
    assert r.status == 200 and "Чек-лист" in page and "Пункт 2" in page and 'name="contact"' in page
    assert (await client.get("/lp/nope")).status == 404
    assert (await client.post(f"/lp/{item['slug']}", data={"name": "Оля", "contact": ""})).status == 400
    assert (await client.post(f"/lp/{item['slug']}", data={"contact": "@bot", "website": "spam"})).status == 400
    env.session.reset()
    r = await client.post(f"/lp/{item['slug']}", data={"name": "Оля", "contact": "@olya", "message": "хочу сайт для салона"})
    assert r.status == 200 and "Спасибо" in await r.text()
    lead = (await env.db.recent_leads(1))[0]
    assert lead["source"] == "landing" and lead["score"] == 9 and lead["contact"] == "@olya" and "салона" in lead["text"]
    sent = [d for n, d in env.session.calls if n == "SendMessage"]
    assert sent and sent[0]["chat_id"] == OWNER and "landing" in sent[0]["text"]
    assert (await env.factory.store.stats())["landing_leads"] == 1
    r = await client.post(f"/api/content/{item['id']}", json={"action": "reject"}, headers=headers())
    assert (await r.json())["item"]["status"] == "rejected"
    assert (await client.get(f"/lp/{item['slug']}")).status == 404


def test_format_card_truncates():
    class Row(dict):
        def __getitem__(self, k):
            return self.get(k)

    row = Row(id=1, kind="post", status="draft", topic="t", title="T", body="x" * 5000, hashtags="", media="", scheduled_at=None,
              published=None, slug=None, caption="")
    assert len(format_card(row)) <= 4010 and format_card(row).endswith("…")


async def test_toon_and_niche(env_noai):
    env = env_noai
    if ffmpeg_available():
        texts = await env.text("/toon Где заявки | Директор: Где заявки?! | Разработчик: Сайт готов, но рекламы нет")
        assert "видео собрано" in texts[-1] and "Директор" in texts[-1] and "SendVideo" in env.session.names()
        row = (await env.factory.store.recent(1))[0]
        assert row["kind"] == "toon" and os.path.getsize(env.factory.media.path(json.loads(row["media"])["video"])) > 0
    assert "GEMINI_API_KEY" in (await env.text("/aivideo тема"))[0]
    texts = await env.text("/niche")
    assert "/niche realtor" in texts[0] and "▶️" in texts[0]
    texts = await env.text("/niche realtor")
    assert "Переключил нишу" in texts[0] and "Недвижимость" in texts[0]
    assert "Риелтор" in await env.db.get_setting("profile") and "ипотека" in await env.db.get_setting("pillars")
    assert "ищу квартиру" in await env.deps.phrases.get()
    kw = await env.db.get_keywords()
    assert kw.topic.get("ипотек") == "ипотека" and "сайт" not in kw.topic and "ищу" in kw.intent
    assert "Нет такой" in (await env.text("/niche xyz"))[0]
    assert "не включён" in (await env.text("/app"))[0]
    env.deps.extra["miniapp_url"] = "https://x.trycloudflare.com/app"
    assert "Кабинет" in (await env.text("/app"))[0]


@pytest.mark.skipif(not ffmpeg_available(), reason="нет ffmpeg")
async def test_aivideo_assemble(tmp_path):
    import subprocess
    from leadhunter.content.aivideo import assemble
    from leadhunter.content.video import probe_duration

    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True)
    mute = tmp_path / "mute.mp4"  # клип без звуковой дорожки
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=360x640:d=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(mute)], check=True)
    voice = tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "1", str(voice)], check=True)
    out = str(tmp_path / "out.mp4")
    await assemble([(clip.read_bytes(), voice.read_bytes()), (mute.read_bytes(), None)], out, size=(360, 640))
    assert os.path.getsize(out) > 0 and abs(await probe_duration(out) - 4.0) < 0.6


# --- идемпотентность публикации ---
class FakePub:
    def __init__(self, platform="telegram", label=None, fail=None, delay=0.0):
        self.platform, self.label = platform, label or platform.upper()
        self.fail, self.delay, self.calls = fail, delay, 0

    async def publish(self, text, images=(), video_path=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError(self.fail)
        return PublishResult(self.platform, f"https://x/{self.platform}")


async def test_duplicate_publish_is_noop(env):
    await env.text("/post тема")
    row = await env.factory.store.get(1)
    pub = FakePub(delay=0.2)
    a, b = await asyncio.gather(env.factory.publish(row, [pub]), env.factory.publish(row, [pub]))
    assert pub.calls == 1
    busy = next(o for o in (a, b) if o.status == "busy")
    assert "уже публикуется" in busy.errors[0]
    assert (await env.factory.store.get(1))["status"] == "published"
    again = await env.factory.publish(row, [pub])  # устаревшая строка тоже не пройдёт
    assert pub.calls == 1 and again.status == "busy" and "уже опубликовано" in again.errors[0]


async def test_restart_mid_publish_retries_only_missing(env):
    await env.text("/post тема")
    store = env.factory.store
    tg, vk = FakePub("telegram"), FakePub("vk")
    # процесс упал в publishing: telegram уже записан, vk нет
    await store.update(1, status="publishing", published=[{"platform": "telegram", "url": "https://x/telegram"}],
                       publishing_at=(datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(), publish_attempts=1)
    notes = []

    async def notify(t):
        notes.append(t)

    assert await publish_due(env.factory, [tg, vk], notify) == 1
    assert tg.calls == 0 and vk.calls == 1 and "прервалась" in notes[0]
    row = await store.get(1)
    assert row["status"] == "published" and {p["platform"] for p in json.loads(row["published"])} == {"telegram", "vk"}


async def test_fresh_publishing_not_requeued(env):
    await env.text("/post тема")
    store = env.factory.store
    await store.update(1, status="publishing", publishing_at=datetime.now(timezone.utc).isoformat(), publish_attempts=1)
    assert await store.requeue_stuck(10) == [] and (await store.get(1))["status"] == "publishing"
    await store.update(1, publishing_at=(datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(), publish_attempts=3)
    assert await store.requeue_stuck(10, exclude={1}) == []  # публикует этот же процесс
    assert await store.requeue_stuck(10) == [(1, False)]  # попытки исчерпаны: автоповтора нет
    assert (await store.get(1))["status"] == "draft"


async def test_partial_failure_and_retry_only_failed(env):
    await env.text("/post тема")
    tg, vk = FakePub("telegram"), FakePub("vk", "VK", fail="токен протух")
    out = await env.factory.publish(await env.factory.store.get(1), [tg, vk])
    row = await env.factory.store.get(1)
    assert out.status == "partial" and row["status"] == "partial" and out.errors == ["VK: токен протух"]
    assert [p["platform"] for p in json.loads(row["published"])] == ["telegram"]
    assert "❌ VK: токен протух" in format_card(row)
    vk.fail = None
    out = await env.factory.publish(row, [tg, vk])
    row = await env.factory.store.get(1)
    assert tg.calls == 1 and vk.calls == 2 and out.status == "published" and row["status"] == "published"
    assert row["publish_errors"] is None and len(json.loads(row["published"])) == 2


async def test_all_failed_returns_to_draft(env):
    await env.text("/post тема")
    out = await env.factory.publish(await env.factory.store.get(1), [FakePub(fail="нет")])
    row = await env.factory.store.get(1)
    assert out.status == "draft" and row["status"] == "draft" and not json.loads(row["published"] or "[]")


async def test_cancelled_platform_not_marked_done(env):
    await env.text("/post тема")
    tg, slow = FakePub("telegram"), FakePub("vk", delay=30)
    task = asyncio.create_task(env.factory.publish(await env.factory.store.get(1), [tg, slow]))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await env.factory.store.get(1)
    assert row["status"] == "partial" and [p["platform"] for p in json.loads(row["published"])] == ["telegram"]
    assert env.factory.active == set()


async def test_platform_timeout_is_error(env, monkeypatch):
    from leadhunter.content import factory as fmod
    monkeypatch.setattr(fmod, "PLATFORM_TIMEOUT", 0.05)
    await env.text("/post тема")
    out = await env.factory.publish(await env.factory.store.get(1), [FakePub(delay=5)])
    row = await env.factory.store.get(1)
    assert out.status == "draft" and "таймаут" in out.errors[0] and row["status"] == "draft"


async def test_failed_render_leaves_no_orphan_file(env, monkeypatch):
    from leadhunter.content import video as vmod

    async def boom(scenes, brand, out_path, **kw):
        with open(out_path, "wb") as f:
            f.write(b"partial")
        raise RuntimeError("ffmpeg упал")

    monkeypatch.setattr(vmod, "build_video", boom)
    before = set(os.listdir(env.factory.media.root))
    with pytest.raises(RuntimeError):
        await env.factory._video({"scenes": [{"text": "a", "voice": "", "duration": 1}]}, await env.factory.brand())
    assert set(os.listdir(env.factory.media.root)) == before


async def test_ffmpeg_timeout_kills_process(monkeypatch):
    from leadhunter.content import aivideo, video

    with pytest.raises(RuntimeError, match="не уложился"):
        await video._run("sleep", "5", timeout=0.2)

    real = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kw):  # вместо ffmpeg — долгий sleep
        return await real("sleep", "5", **kw)

    monkeypatch.setattr(aivideo.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="не уложился"):
        await aivideo._run("-i", "x", timeout=0.2)


async def test_store_migrates_old_content_items(tmp_path):
    from leadhunter.content.store import ContentStore
    from leadhunter.db import Database

    db = Database(str(tmp_path / "old.db"))
    await db.connect()
    await db.conn.execute("CREATE TABLE content_items (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, topic TEXT NOT NULL,"
                          " title TEXT, body TEXT, caption TEXT, hashtags TEXT, image_prompt TEXT, media TEXT,"
                          " status TEXT NOT NULL DEFAULT 'draft', scheduled_at TEXT, published_at TEXT, published TEXT, platforms TEXT,"
                          " lead_id INTEGER, parent_id INTEGER, slug TEXT UNIQUE, msg_chat_id INTEGER, msg_id INTEGER,"
                          " created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    store = ContentStore(db)
    await store.init()
    await store.init()
    item = await store.add("post", "t")
    assert await store.claim_publish(item) and not await store.claim_publish(item)
    await db.close()
