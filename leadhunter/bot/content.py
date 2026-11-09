"""Команды и кнопки контент-фабрики: сгенерировать → посмотреть → поправить → опубликовать / запланировать."""
import html
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)

from ..content.factory import API_PLATFORMS, MANUAL_PLATFORMS, ContentFactory, compose_text
from ..content.store import KINDS, STATUSES, body_json, media_json
from .guard import Guard

log = logging.getLogger(__name__)

CONTENT_HELP = """<b>Контент-фабрика</b> — ИИ пишет под ваш профиль и учится на ваших правках.

/post тема · /carousel тема · /reel тема · /magnet тема — сделать пост / карусель / короткое видео / лид-магнит
/toon тема — мультик с персонажами, рендерится кодом (без нейросети) · /aivideo тема — ИИ-мультик нейросетью Gemini (платно ≈ 0,07 $/сек, биллинг в AI Studio)
/ideas [N] — идеи из реальных запросов клиентов · /plan — план на неделю (с автопилотом сам предложит черновики)
/publish текст — опубликовать свой текст (обложка подставится)
/queue — очередь · /schedule id 2025-01-15 10:00 — точное время · /content — статистика

<b>Куда публиковать</b>: /channels · /channel_set telegram @канал · /channel_set vk -123456 · /channel_del vk
Threads / Instagram / YouTube — ключи через /setup. TikTok и остальное — кнопка «📦 Пакет»: видео + подпись, выкладываете за минуту.

<b>Стиль</b>: /voice как писать (тон голоса) · /brand Студия | @handle | #141420 | #7C5CFF — подпись и цвета слайдов
/autopilot on|off — каждый день в заданный час бот сам приносит черновик по плану"""


class ContentState(StatesGroup):
    edit = State()
    rewrite = State()
    topic = State()


def kind_label(kind: str) -> str:
    return KINDS.get(kind, kind)


def keyboard(row) -> InlineKeyboardMarkup:
    i = row["id"]
    edit_row = [InlineKeyboardButton(text="✏️ Текст", callback_data=f"ce:{i}"),
                InlineKeyboardButton(text="🔄 Переписать", callback_data=f"cr:{i}"),
                InlineKeyboardButton(text="🎲 Вариант", callback_data=f"cv:{i}")]
    tail = [InlineKeyboardButton(text="📦 Пакет", callback_data=f"cpk:{i}")]
    if row["status"] == "draft":
        rows = [[InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"cp:{i}"),
                 InlineKeyboardButton(text="🗓 Когда", callback_data=f"cs:{i}")], edit_row,
                tail + [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cx:{i}")]]
    elif row["status"] == "scheduled":
        rows = [[InlineKeyboardButton(text="🚀 Сейчас", callback_data=f"cp:{i}"),
                 InlineKeyboardButton(text="❌ Снять с плана", callback_data=f"cu:{i}")], edit_row, tail]
    elif row["status"] == "partial":
        rows = [[InlineKeyboardButton(text="🔁 Повторить недостающие", callback_data=f"cp:{i}")], tail]
    else:
        rows = [tail]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня 18:00", callback_data=f"cs:{item_id}:0:18"),
         InlineKeyboardButton(text="Завтра 10:00", callback_data=f"cs:{item_id}:1:10")],
        [InlineKeyboardButton(text="Завтра 18:00", callback_data=f"cs:{item_id}:1:18"),
         InlineKeyboardButton(text="Через 2 дня 10:00", callback_data=f"cs:{item_id}:2:10")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"cb:{item_id}")],
    ])


def format_card(row, tz: str = "Europe/Moscow") -> str:
    e = html.escape
    head = f"{kind_label(row['kind'])} <b>#{row['id']}</b> · {STATUSES.get(row['status'], row['status'])} · {e(row['topic'][:80])}"
    lines = [head]
    data = body_json(row) if row["kind"] != "post" else {}
    if row["title"]:
        lines.append(f"<b>{e(row['title'])}</b>")
    if row["kind"] == "post":
        lines.append(e(row["body"] or ""))
    elif row["kind"] == "carousel":
        for n, s in enumerate(data.get("slides") or [], 1):
            lines.append(f"<b>{n}. {e(s.get('title', ''))}</b> — {e(s.get('text', ''))}")
        if row["caption"]:
            lines.append(f"\n📝 {e(row['caption'])}")
    elif row["kind"] == "toon":
        chars = data.get("characters") or {}
        names = {k: (v.get("name") or k) for k, v in chars.items()} if isinstance(chars, dict) else {}
        for n, s in enumerate(data.get("scenes") or [], 1):
            line = s.get("line") or {}
            who = names.get(line.get("speaker", ""), line.get("speaker", ""))
            lines.append(f"{n}. [{e(s.get('bg', ''))}] <b>{e(who)}</b>: {e(line.get('text', ''))}")
        if row["caption"]:
            lines.append(f"\n📝 {e(row['caption'])}")
    elif row["kind"] == "aivideo":
        for n, c in enumerate(data.get("clips") or [], 1):
            lines.append(f"{n}. 🎙 {e(c.get('voice', ''))}\n<i>{e(c.get('prompt', '')[:160])}</i>")
        if row["caption"]:
            lines.append(f"\n📝 {e(row['caption'])}")
    elif row["kind"] == "reel":
        if data.get("hook"):
            lines.append(f"🎣 {e(data['hook'])}")
        for n, s in enumerate(data.get("scenes") or [], 1):
            lines.append(f"{n}. [{e(s.get('text', ''))}] {e(s.get('voice', ''))} · {s.get('duration', 3):g}с")
        if row["caption"]:
            lines.append(f"\n📝 {e(row['caption'])}")
    elif row["kind"] == "magnet":
        if data.get("subtitle"):
            lines.append(e(data["subtitle"]))
        for n, it in enumerate(data.get("items") or [], 1):
            lines.append(f"{n}. <b>{e(it.get('title', ''))}</b> — {e(it.get('text', ''))}")
        if data.get("post"):
            lines.append(f"\n📣 Анонс:\n{e(data['post'])}")
    if row["hashtags"]:
        lines.append(f"\n{e(row['hashtags'])}")
    m = media_json(row)
    meta = []
    if m.get("video"):
        meta.append("🎬 видео собрано")
    elif m.get("error"):
        meta.append(f"⚠️ видео: {e(m['error'][:120])}")
    if m.get("images"):
        n_img = len(m["images"])
        meta.append("🖼 " + ("картинка ИИ" if m.get("ai_cover") else "обложка по шаблону" if n_img == 1 else f"{n_img} слайдов"))
    if row["scheduled_at"] and row["status"] == "scheduled":
        at = datetime.fromisoformat(row["scheduled_at"]).astimezone(ZoneInfo(tz))
        meta.append(f"🗓 {at:%d.%m %H:%M}")
    if row["published"]:
        for p in json.loads(row["published"]):
            meta.append(f"✅ {API_PLATFORMS.get(p['platform'], p['platform'])}" + (f': <a href="{e(p["url"])}">ссылка</a>' if p.get("url") else ""))
    if row["publish_errors"] and row["status"] != "published":
        for err in json.loads(row["publish_errors"]):
            meta.append(f"❌ {e(err.get('label') or API_PLATFORMS.get(err.get('platform'), err.get('platform') or ''))}: {e(err.get('error', '')[:120])}")
    if row["slug"]:
        meta.append(f"🔗 страница: /lp/{row['slug']}")
    if meta:
        lines.append("\n" + " · ".join(meta))
    text = "\n".join(lines)
    return text[:4000] + ("…" if len(text) > 4000 else "")


def build_content_router(deps) -> Router:
    router = Router()
    db, guard = deps.db, deps.guard

    def factory() -> ContentFactory | None:
        return deps.extra.get("factory")

    async def allowed(m: Message) -> bool:
        return await guard.allowed(m.from_user, m.chat.id)

    async def ready(m: Message) -> bool:
        if not await allowed(m):
            return False
        if not factory():
            await m.answer("Контент-фабрика не инициализирована.")
            return False
        return True

    async def deliver(row, chat_id: int, thread_id: int | None = None) -> Message | None:
        """Карточка: медиа как в публикации, затем текст с кнопками (у альбома кнопок не бывает)."""
        f = factory()
        images, video = f.files(row)
        try:
            if video:
                await deps.guard.bot.send_video(chat_id, FSInputFile(video), message_thread_id=thread_id)
            elif len(images) == 1:
                await deps.guard.bot.send_photo(chat_id, BufferedInputFile(images[0], "cover.png"), message_thread_id=thread_id)
            elif images:
                album = [InputMediaPhoto(media=BufferedInputFile(b, f"slide{n}.png")) for n, b in enumerate(images[:10], 1)]
                await deps.guard.bot.send_media_group(chat_id, album, message_thread_id=thread_id)
        except Exception:
            log.exception("медиа не отправилось")
        msg = await deps.guard.bot.send_message(chat_id, format_card(row, deps.tz), reply_markup=keyboard(row),
                                                message_thread_id=thread_id, disable_web_page_preview=True)
        await f.store.set_message(row["id"], msg.chat.id, msg.message_id)
        return msg

    deps.extra["deliver_content"] = deliver

    async def make(m: Message, kind: str, topic: str, *, data: dict | None = None):
        f = factory()
        wait = await m.answer(f"{kind_label(kind)}: пишу про «{html.escape(topic[:60])}»…")
        try:
            row = await f.create(kind, topic, created_by=Guard.name(m.from_user), data=data)
        except Exception as e:
            await wait.edit_text(f"Не получилось: {html.escape(str(e)[:300])}")
            return
        await wait.delete()
        await deliver(row, m.chat.id, m.message_thread_id)

    # --- генерация ---
    for _kind in ("post", "carousel", "reel", "toon", "aivideo", "magnet"):
        def _bind(kind: str):
            @router.message(Command(kind))
            async def gen_cmd(m: Message, command: CommandObject, state: FSMContext):
                if not await ready(m):
                    return
                topic = (command.args or "").strip()
                if not topic:
                    await state.set_state(ContentState.topic)
                    await state.update_data(kind=kind)
                    await m.answer(f"{kind_label(kind)} — о чём? Напишите тему одним сообщением.")
                    return
                await _run(m, kind, topic)
        _bind(_kind)

    async def _run(m: Message, kind: str, topic: str):
        f = factory()
        if kind == "aivideo" and not (f.video_ai and f.video_ai.video_enabled):
            await m.answer("ИИ-мультики генерирует нейросеть Gemini Omni — задайте GEMINI_API_KEY в .env (aistudio.google.com). "
                           "Без него есть /toon — мультик с персонажами, который рисуется кодом.")
            return
        if f.gen.available:
            await make(m, kind, topic)
            return
        # Без ИИ: слайды/сцены через «|», первый кусок — заголовок
        parts = [p.strip() for p in topic.split("|") if p.strip()]
        if kind == "carousel" and len(parts) >= 2:
            data = {"title": parts[0], "slides": [{"title": parts[0], "text": ""}] + [{"title": "", "text": p} for p in parts[1:]],
                    "caption": parts[0], "hashtags": []}
            await make(m, kind, parts[0], data=data)
        elif kind == "reel" and len(parts) >= 2:
            data = {"title": parts[0], "scenes": [{"text": p, "voice": "", "duration": 3} for p in parts], "caption": parts[0], "hashtags": []}
            await make(m, kind, parts[0], data=data)
        elif kind == "toon" and len(parts) >= 2:
            # Без ИИ: «Название | Имя: реплика | Имя: реплика» — два первых имени становятся персонажами слева и справа
            chars, scenes = {}, []
            for p in parts[1:]:
                who, _, text = p.partition(":")
                who, text = (who.strip() or "Герой"), (text.strip() or p.strip())
                chars.setdefault(who, {"name": who, "color": ["#F2C14E", "#6FB9F0", "#8EEE98"][len(chars) % 3], "shape": "round"})
                cid = who
                ids = list(chars)
                scenes.append({"bg": "office", "duration": 3.5,
                               "characters": [{"id": c, "pos": "left" if i == 0 else "right" if i == 1 else "center",
                                               "emotion": "happy" if c == cid else "neutral", "action": "talk" if c == cid else "idle"}
                                              for i, c in enumerate(ids[:3])],
                               "line": {"speaker": cid, "text": text}})
            data = {"title": parts[0], "style": "flat", "characters": chars, "scenes": scenes, "caption": parts[0], "hashtags": []}
            await make(m, kind, parts[0], data=data)
        else:
            await m.answer("ИИ не подключён (GEMINI_API_KEY или OPENAI_API_KEY). Без него можно: /publish текст, "
                           "/carousel Заголовок | слайд 1 | слайд 2, /reel фраза 1 | фраза 2, "
                           "/toon Название | Директор: реплика | Разработчик: реплика")

    @router.message(ContentState.topic, F.text, ~F.text.startswith("/"))
    async def topic_reply(m: Message, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        if await ready(m):
            await _run(m, data.get("kind", "post"), m.text.strip())

    @router.message(Command("publish"))
    async def publish_own(m: Message, command: CommandObject):
        if not await ready(m):
            return
        text = (command.args or "").strip()
        if not text:
            await m.answer("Формат: /publish текст поста")
            return
        title = text.split("\n")[0][:80]
        tags = [w for w in text.split() if w.startswith("#")]
        await make(m, "post", title, data={"title": title, "body": text, "hashtags": tags, "image_prompt": ""})

    @router.message(Command("ideas", "plan"))
    async def ideas(m: Message, command: CommandObject):
        if not await ready(m):
            return
        f = factory()
        if not f.gen.available:
            await m.answer("Идеи генерирует ИИ — задайте OPENAI_API_KEY.")
            return
        is_plan = m.text.lstrip("/").startswith("plan")
        n = int(command.args) if command.args and command.args.strip().isdigit() else 7
        wait = await m.answer("Смотрю, что спрашивали клиенты, и придумываю темы…")
        try:
            data = await f.gen.generate("ideas", "", n=min(n, 10))
        except Exception as e:
            await wait.edit_text(f"Не получилось: {html.escape(str(e)[:300])}")
            return
        tz = ZoneInfo(deps.tz)
        hour = deps.extra.get("content_hour", 10)
        lines, rows = ["💡 <b>Идеи</b>" + (" — план на неделю" if is_plan else "") + ":"], []
        for d, idea in enumerate(data["ideas"]):
            when = None
            if is_plan:
                when = (datetime.now(tz) + timedelta(days=d + 1)).replace(hour=hour, minute=0, second=0, microsecond=0)
            item_id = await f.store.add("idea", idea["topic"], title=idea.get("hook", ""), body=json.dumps(idea, ensure_ascii=False),
                                        created_by=Guard.name(m.from_user), platforms=idea.get("kind", "post"),
                                        scheduled_at=when.isoformat() if when else None)
            lines.append(f"\n<b>{d + 1}. {html.escape(idea['topic'])}</b> · {kind_label(idea.get('kind', 'post'))}"
                         + (f" · {when:%a %d.%m}" if when else "") + f"\n{html.escape(idea.get('hook', ''))}")
            rows.append([InlineKeyboardButton(text=f"{d + 1} ✍️", callback_data=f"ci:{item_id}:post"),
                         InlineKeyboardButton(text="🎠", callback_data=f"ci:{item_id}:carousel"),
                         InlineKeyboardButton(text="🎬", callback_data=f"ci:{item_id}:reel"),
                         InlineKeyboardButton(text="🧲", callback_data=f"ci:{item_id}:magnet")])
        tail = ("\n\nАвтопилот сам сделает черновик каждой идеи в её день (/autopilot on). Или нажмите формат сейчас."
                if is_plan else "\n\nНажмите формат — сделаю сразу.")
        await wait.edit_text("\n".join(lines)[:3900] + tail, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    # --- очередь, расписание, статистика ---
    @router.message(Command("queue"))
    async def queue(m: Message):
        if not await ready(m):
            return
        rows = await factory().store.queue()
        if not rows:
            await m.answer("Очередь пуста. /post тема — сделать что-нибудь.")
            return
        tz = ZoneInfo(deps.tz)
        lines = []
        for r in rows:
            when = f" 🗓 {datetime.fromisoformat(r['scheduled_at']).astimezone(tz):%d.%m %H:%M}" if r["scheduled_at"] and r["status"] == "scheduled" else ""
            lines.append(f"{kind_label(r['kind'])} #{r['id']} {html.escape((r['title'] or r['topic'])[:60])}{when}")
        await m.answer("<b>Очередь</b>:\n" + "\n".join(lines) + "\n\nОткрыть карточку: /show id")

    @router.message(Command("show"))
    async def show(m: Message, command: CommandObject):
        if not await ready(m):
            return
        if not command.args or not command.args.strip().isdigit():
            await m.answer("Формат: /show id")
            return
        row = await factory().store.get(int(command.args))
        if not row:
            await m.answer("Нет такого")
            return
        await deliver(row, m.chat.id, m.message_thread_id)

    @router.message(Command("schedule"))
    async def schedule_cmd(m: Message, command: CommandObject):
        if not await ready(m):
            return
        parts = (command.args or "").split()
        try:
            item_id = int(parts[0])
            at = datetime.strptime(" ".join(parts[1:3]), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(deps.tz))
        except (ValueError, IndexError):
            await m.answer("Формат: /schedule id 2025-01-15 10:00")
            return
        row = await factory().store.get(item_id)
        if not row:
            await m.answer("Нет такого")
            return
        await factory().store.schedule(item_id, at)
        await m.answer(f"#{item_id} выйдет {at:%d.%m %H:%M}")

    @router.message(Command("content"))
    async def content_stats(m: Message):
        if not await ready(m):
            return
        st = await factory().store.stats(30)
        lines = [CONTENT_HELP, "\n<b>За 30 дней</b>:"]
        for kind, by in st["by_kind"].items():
            lines.append(f"• {kind_label(kind)}: " + ", ".join(f"{STATUSES.get(s, s)} {n}" for s, n in by.items()))
        lines.append(f"• заявок с лендингов: {st['landing_leads']}")
        pubs = deps.extra.get("publishers")
        if pubs:
            labels = ", ".join(html.escape(p.label) for p in await pubs())
            lines.append("\n<b>Публикую в</b>: " + (labels or "никуда — настройте /channels"))
        await m.answer("\n".join(lines))

    # --- каналы, стиль, автопилот ---
    @router.message(Command("channels"))
    async def channels(m: Message):
        if not await ready(m):
            return
        pubs = await deps.extra["publishers"]() if deps.extra.get("publishers") else []
        lines = ["<b>Площадки</b>:"]
        for p in pubs:
            lines.append(f"✅ {html.escape(p.label)}")
        for key, name in API_PLATFORMS.items():
            if not any(p.platform == key for p in pubs):
                hint = {"telegram": "добавьте бота админом канала → /channel_set telegram @канал",
                        "vk": "токен VK в /setup → /channel_set vk -id_группы",
                        "threads": "токен и ID в /setup",
                        "instagram": "токен и ID в /setup (картинки — через кабинет, нужен /app)",
                        "youtube": "Client ID, secret и refresh token в /setup"}[key]
                lines.append(f"▫️ {name}: {hint}")
        lines.append("\n" + ", ".join(MANUAL_PLATFORMS.values()) + " — через «📦 Пакет» (у TikTok API только по одобрению приложения)")
        await m.answer("\n".join(lines))

    @router.message(Command("channel_set"))
    async def channel_set(m: Message, command: CommandObject):
        if not await ready(m):
            return
        parts = (command.args or "").split()
        if len(parts) != 2 or parts[0] not in ("telegram", "vk"):
            await m.answer("Формат: /channel_set telegram @канал  или  /channel_set vk -123456 (id группы со знаком минус)")
            return
        platform, target = parts
        if platform == "telegram":
            try:
                chat = await deps.guard.bot.get_chat(target)
                target = f"@{chat.username}" if chat.username else str(chat.id)
                member = await deps.guard.bot.get_chat_member(chat.id, (await deps.guard.bot.me()).id)
                if getattr(member, "status", "") not in ("administrator", "creator"):
                    await m.answer("Сначала добавьте бота администратором канала с правом публиковать.")
                    return
            except TelegramBadRequest as e:
                await m.answer(f"Канал не найден: {html.escape(e.message)}. Бот должен быть добавлен в канал.")
                return
        elif not target.lstrip("-").isdigit():
            await m.answer("Для VK укажите числовой id: группа — со знаком минус, например -123456")
            return
        await factory().store.set_channel(platform, target)
        await m.answer(f"Буду публиковать в {API_PLATFORMS[platform]} {html.escape(target)}")

    @router.message(Command("channel_del"))
    async def channel_del(m: Message, command: CommandObject):
        if not await ready(m):
            return
        n = await factory().store.remove_channel((command.args or "").strip())
        await m.answer("Убрал" if n else "Нет такой площадки (см. /channels)")

    @router.message(Command("voice"))
    async def voice(m: Message, command: CommandObject):
        if not await ready(m):
            return
        if not command.args:
            cur = await db.get_setting("voice", "")
            await m.answer(f"Тон голоса: {html.escape(cur) or 'не задан (ИИ пишет нейтрально-дружелюбно)'}\n\n"
                           "Задать: /voice на «ты», коротко, с юмором, без эмодзи, примеры из практики")
            return
        await db.set_setting("voice", command.args.strip())
        await m.answer("Запомнил — так и буду писать.")

    @router.message(Command("brand"))
    async def brand(m: Message, command: CommandObject):
        if not await ready(m):
            return
        if not command.args:
            b = await factory().brand()
            await m.answer(f"Бренд на слайдах: {html.escape(b.name or '—')} {html.escape(b.handle)} · фон {b.primary} · акцент {b.accent}\n"
                           "Задать: /brand Студия Пиксель | @pixel_studio | #141420 | #7C5CFF")
            return
        parts = [p.strip() for p in command.args.split("|")]
        data = {"name": parts[0]}
        if len(parts) > 1:
            data["handle"] = parts[1]
        if len(parts) > 2 and parts[2].startswith("#"):
            data["primary"] = parts[2]
        if len(parts) > 3 and parts[3].startswith("#"):
            data["accent"] = parts[3]
        await db.set_setting("brand", json.dumps(data, ensure_ascii=False))
        await m.answer("Сохранил — новые слайды и обложки будут с этим брендом.")

    @router.message(Command("autopilot"))
    async def autopilot(m: Message, command: CommandObject):
        if not await ready(m):
            return
        arg = (command.args or "").strip().lower()
        if arg in ("on", "вкл"):
            await db.set_setting("autopilot", "1")
            await m.answer(f"Автопилот включён: каждый день в {deps.extra.get('content_hour', 10)}:00 принесу черновик по плану "
                           "(/plan) или по свежим запросам клиентов. Публикую только после вашего ✅.")
        elif arg in ("off", "выкл"):
            await db.set_setting("autopilot", "0")
            await m.answer("Автопилот выключен.")
        else:
            cur = await db.get_setting("autopilot", "0") == "1"
            await m.answer(f"Автопилот: {'включён' if cur else 'выключен'}. /autopilot on|off")

    # --- правки (FSM) ---
    @router.message(ContentState.edit, F.text, ~F.text.startswith("/"))
    async def edit_reply(m: Message, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        f = factory()
        row = await f.store.get(data["item_id"]) if f else None
        if not row:
            return
        new = m.text.strip()
        if row["kind"] == "post":
            await f.store.add_edit(row["id"], "post", row["body"] or "", new)
            await f.store.update(row["id"], body=new)
        else:
            await f.store.add_edit(row["id"], row["kind"], row["caption"] or "", new)
            await f.store.update(row["id"], caption=new)
        await m.answer("Принял. Запомнил, как вы правите — дальше буду писать ближе к этому.")
        await deliver(await f.store.get(row["id"]), m.chat.id, m.message_thread_id)

    @router.message(ContentState.rewrite, F.text, ~F.text.startswith("/"))
    async def rewrite_reply(m: Message, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        f = factory()
        row = await f.store.get(data["item_id"]) if f else None
        if not row:
            return
        wait = await m.answer("Переписываю…")
        try:
            old = f.data_of(row)
            new = await f.gen.rewrite(row["kind"], old, m.text.strip())
            await f.store.add_edit(row["id"], row["kind"], old.get("body") or old.get("caption") or json.dumps(old, ensure_ascii=False)[:800],
                                   f"[указание: {m.text.strip()}] " + (new.get("body") or new.get("caption") or json.dumps(new, ensure_ascii=False)[:800]))
            await f.replace_data(row["id"], new)
        except Exception as e:
            await wait.edit_text(f"Не получилось: {html.escape(str(e)[:300])}")
            return
        await wait.delete()
        await deliver(await f.store.get(row["id"]), m.chat.id, m.message_thread_id)

    # --- кнопки ---
    async def ok(cq: CallbackQuery) -> bool:
        if await guard.allowed(cq.from_user, cq.message.chat.id if cq.message else None) and factory():
            return True
        await cq.answer("Нет доступа")
        return False

    async def refresh(cq: CallbackQuery, row) -> None:
        try:
            await cq.message.edit_text(format_card(row, deps.tz), reply_markup=keyboard(row), disable_web_page_preview=True)
        except TelegramBadRequest:
            pass

    async def get(cq: CallbackQuery):
        row = await factory().store.get(int(cq.data.split(":")[1]))
        if not row:
            await cq.answer("Уже удалено")
        return row

    @router.callback_query(F.data.startswith("cp:"))
    async def publish_now(cq: CallbackQuery):
        if not await ok(cq) or not (row := await get(cq)):
            return
        pubs = await deps.extra["publishers"]() if deps.extra.get("publishers") else []
        if not pubs:
            await cq.answer("Площадки не настроены: /channels", show_alert=True)
            return
        if row["status"] in ("publishing", "published"):  # двойной клик / уже вышло
            await cq.answer("Уже публикуется…" if row["status"] == "publishing" else "Уже опубликовано", show_alert=True)
            return
        await cq.answer("Публикую…")
        out = await factory().publish(row, pubs)
        if out.status == "busy":
            await cq.message.reply(html.escape("⚠️ " + "\n⚠️ ".join(out.errors)))
            return
        row = await factory().store.get(row["id"])
        await refresh(cq, row)
        msg = ("✅ Опубликовано: " + ", ".join(f"{API_PLATFORMS.get(r.platform, r.platform)} {r.url}".strip() for r in out.results)) if out.results else ""
        notes = [r.note for r in out.results if getattr(r, "note", "")]
        if out.errors or notes:
            msg += ("\n" if msg else "") + "⚠️ " + "\n⚠️ ".join(out.errors + notes)
        if out.status == "partial":
            msg += "\n🔁 Опубликовано частично — «Повторить недостающие» в карточке отправит только на площадки без успеха."
        await cq.message.reply(html.escape(msg) if msg else "Никуда не ушло", disable_web_page_preview=True)

    @router.callback_query(F.data.startswith("cs:"))
    async def schedule_cb(cq: CallbackQuery):
        if not await ok(cq) or not (row := await get(cq)):
            return
        parts = cq.data.split(":")
        if len(parts) == 2:
            try:
                await cq.message.edit_reply_markup(reply_markup=schedule_keyboard(row["id"]))
            except TelegramBadRequest:
                pass
            await cq.answer("Точное время: /schedule id 2025-01-15 10:00")
            return
        days, hour = int(parts[2]), int(parts[3])
        tz = ZoneInfo(deps.tz)
        at = (datetime.now(tz) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if at <= datetime.now(tz):
            at += timedelta(days=1)
        await factory().store.schedule(row["id"], at)
        await refresh(cq, await factory().store.get(row["id"]))
        await cq.answer(f"Выйдет {at:%d.%m %H:%M}")

    @router.callback_query(F.data.startswith("cb:"))
    async def back(cq: CallbackQuery):
        if await ok(cq) and (row := await get(cq)):
            await refresh(cq, row)
            await cq.answer()

    @router.callback_query(F.data.startswith("cu:"))
    async def unschedule(cq: CallbackQuery):
        if not await ok(cq) or not (row := await get(cq)):
            return
        await factory().store.schedule(row["id"], None)
        await refresh(cq, await factory().store.get(row["id"]))
        await cq.answer("Снял с плана — остаётся черновиком")

    @router.callback_query(F.data.startswith("cx:"))
    async def reject(cq: CallbackQuery):
        if not await ok(cq) or not (row := await get(cq)):
            return
        await factory().store.update(row["id"], status="rejected")
        try:
            await cq.message.edit_text(f"🗑 #{row['id']} удалено", reply_markup=None)
        except TelegramBadRequest:
            pass
        await cq.answer("Удалил")

    @router.callback_query(F.data.startswith("ce:"))
    async def edit_start(cq: CallbackQuery, state: FSMContext):
        if not await ok(cq) or not (row := await get(cq)):
            return
        await state.set_state(ContentState.edit)
        await state.update_data(item_id=row["id"])
        what = "текст поста" if row["kind"] == "post" else "подпись"
        await cq.message.reply(f"Пришлите исправленный {what} #{row['id']} одним сообщением — я заменю и запомню стиль правок.")
        await cq.answer()

    @router.callback_query(F.data.startswith("cr:"))
    async def rewrite_start(cq: CallbackQuery, state: FSMContext):
        if not await ok(cq) or not (row := await get(cq)):
            return
        if not factory().gen.available:
            await cq.answer("Нужен ИИ (OPENAI_API_KEY)", show_alert=True)
            return
        await state.set_state(ContentState.rewrite)
        await state.update_data(item_id=row["id"])
        await cq.message.reply(f"Что поменять в #{row['id']}? Например: «короче, на ты, добавь пример с ценой».")
        await cq.answer()

    @router.callback_query(F.data.startswith("cv:"))
    async def variant(cq: CallbackQuery):
        if not await ok(cq) or not (row := await get(cq)):
            return
        if not factory().gen.available:
            await cq.answer("Нужен ИИ (OPENAI_API_KEY)", show_alert=True)
            return
        await cq.answer("Пишу другой вариант…")
        try:
            new = await factory().create(row["kind"], row["topic"], created_by=Guard.name(cq.from_user), parent_id=row["id"])
        except Exception as e:
            await cq.message.reply(f"Не получилось: {html.escape(str(e)[:300])}")
            return
        await deliver(new, cq.message.chat.id, cq.message.message_thread_id)

    @router.callback_query(F.data.startswith("cpk:"))
    async def package(cq: CallbackQuery):
        """Пакет для ручной выкладки: файлы + подпись моноширинным, копируется одним тапом."""
        if not await ok(cq) or not (row := await get(cq)):
            return
        f = factory()
        images, video = f.files(row)
        chat_id, thread = cq.message.chat.id, cq.message.message_thread_id
        bot = deps.guard.bot
        if video:
            await bot.send_document(chat_id, FSInputFile(video, filename=f"reel_{row['id']}.mp4"), message_thread_id=thread,
                                    caption="Видео без сжатия — для TikTok / Reels / Shorts")
        for n, img in enumerate(images[:10], 1):
            await bot.send_document(chat_id, BufferedInputFile(img, f"slide_{row['id']}_{n}.png"), message_thread_id=thread)
        if row["kind"] == "magnet":
            await bot.send_document(chat_id, BufferedInputFile(f.magnet_text(row).encode(), f"magnet_{row['id']}.txt"),
                                    message_thread_id=thread, caption="Лид-магнит текстом")
        text = compose_text(row)
        await bot.send_message(chat_id, f"Подпись:\n<code>{text}</code>", message_thread_id=thread)
        await cq.answer("Отправил файлы и подпись")

    @router.callback_query(F.data.startswith("ci:"))
    async def from_idea(cq: CallbackQuery):
        if not await ok(cq) or not (row := await get(cq)):
            return
        kind = cq.data.split(":")[2]
        await cq.answer(f"{kind_label(kind)}: пишу…")
        try:
            new = await factory().create(kind, row["topic"], created_by=Guard.name(cq.from_user), parent_id=row["id"])
            await factory().store.update(row["id"], status="published")  # идея использована
        except Exception as e:
            await cq.message.reply(f"Не получилось: {html.escape(str(e)[:300])}")
            return
        await deliver(new, cq.message.chat.id, cq.message.message_thread_id)

    return router
