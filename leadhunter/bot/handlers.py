import html
from dataclasses import dataclass, field
from statistics import median

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from ..db import Database
from ..llm import LLMClassifier
from ..phrases import PhraseProvider
from ..sources.base import Source
from ..sources.telegram_chats import TelegramChatsSource
from .guard import Guard
from .notify import STATUS_LABELS
from .workspace import TOPICS, Workspace

HELP = """Я ищу лидов и делаю контент под вашу нишу. /niche — выбрать нишу одной командой, /app — кабинет.

<b>Рабочая группа</b>
/setup_group — выполнить в группе-форуме: бот создаст темы (Горячие, Сайты, Боты…) и будет слать лидов туда
/team — команда и её результаты за неделю · /team_del id — убрать

<b>Лиды</b>
/stats — за сутки по источникам · /leads [N] — последние · /digest — сводка
/funnel [days] [source|service|phrase|chat] — воронка: найдено → взяли → написали → ответил → сделка
/export — CSV

<b>Фильтры</b>
/keywords, /kw_add topic|intent|negative слово [услуга], /kw_del слово — словарь
/rules — правила вида <code>+сайт +(нужен|ищу) -предлагаю</code> (словоформы учитываются)
/rule_add выражение · /rule_del id
/geo Москва, Тула — только эти города (пусто = везде) · /geo off
/threshold N · /fresh часы откликов — какие заказы с бирж ещё реально забрать · /profile [текст] · /phrases · /phrases_set а; б · /phrases_gen
/block @user · /unblock ключ · /ignored · /pause · /resume
/template_set сайт текст — шаблон ответа по услуге, когда нет ИИ · /templates

<b>Telegram-чаты</b> (если подключён аккаунт)
/tg_find запрос · /tg_join @chat · /tg_chats · /chats_rating — какие чаты приносят лидов
/discover ниша — найти и оценить чаты по нише (ИИ смотрит последние сообщения)

<b>Контент-фабрика</b> — /content
/post, /carousel, /reel, /magnet тема · /ideas · /plan · /queue · /channels · /voice · /brand · /autopilot on

/sources — состояние источников"""


@dataclass
class BotDeps:
    db: Database
    sources: list[Source]
    guard: Guard
    phrases: PhraseProvider
    workspace: Workspace
    llm: LLMClassifier | None = None
    default_profile: str = ""
    outreach_limit: int = 0
    followup_hours: int = 24
    tz: str = "Europe/Moscow"
    discover: object = None  # ChatDiscovery, задаётся в main
    extra: dict = field(default_factory=dict)

    @property
    def tg(self) -> TelegramChatsSource | None:
        return next((s for s in self.sources if isinstance(s, TelegramChatsSource)), None)


async def onboarding_text(deps: "BotDeps", first: bool) -> str:
    """/start: что уже работает и какие 3 шага остались — вместо простыни команд (она в /help)."""
    integ = deps.extra.get("integrations")
    sm = integ.summary() if integ else {}
    niche = await deps.db.get_setting("niche")
    ws = await deps.db.get_setting("workspace_chat_id")
    hello = "Привет! Я ищу вам клиентов и делаю контент." if first else "Я ищу лидов и делаю контент под вашу нишу."
    steps = [
        f"{'✅' if niche else '1️⃣'} Ниша — /niche" + ("" if niche else " (одна команда настраивает всё под вашу сферу)"),
        f"{'✅' if sm.get('ai') else '2️⃣'} Ключ ИИ — /setup" + ("" if sm.get("ai") else " (бесплатный Gemini: 2 минуты)"),
        f"{'✅' if sm.get('miniapp') else '3️⃣'} Кабинет — /app" + ("" if sm.get("miniapp") else " (Mini App с лидами, отчётами и контентом)"),
        f"{'✅' if ws else '▫️'} Группа для команды — /setup_group (необязательно)",
    ]
    return (f"{hello} Лиды приходят сюда с кнопками «Беру / Горячий / Не лид», контент — по командам /post, /toon, /ideas.\n\n"
            + "\n".join(steps) + "\n\n/status — что работает · /help — все команды · /fresh — какие заказы с бирж присылать")


def onboarding_keyboard(deps: "BotDeps"):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    row = [InlineKeyboardButton(text="🧭 Ниша", callback_data="ob:niche"), InlineKeyboardButton(text="🔌 Подключения", callback_data="ob:setup")]
    url = deps.extra.get("miniapp_url")
    if url:
        row.append(InlineKeyboardButton(text="📱 Кабинет", web_app=WebAppInfo(url=url)))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def build_router(deps: BotDeps) -> Router:
    router = Router()
    db, guard, tg = deps.db, deps.guard, deps.tg

    async def allowed(m: Message) -> bool:
        return await guard.allowed(m.from_user, m.chat.id)

    @router.message(Command("start"))
    async def start(m: Message, command: CommandObject):
        first_owner = False
        if not await guard.has_owner():
            # Владельцем становится только тот, кто прислал одноразовый код из логов сервера
            await guard.ensure_claim_code()
            if m.chat.type != "private" or not await guard.claim_owner(m.from_user.id, command.args or ""):
                await m.answer("Бот ещё не привязан к владельцу. Код — в логах сервера (docker compose logs / journalctl).")
                return
            first_owner = True
        if not await allowed(m):
            return
        await m.answer(await onboarding_text(deps, first_owner), reply_markup=onboarding_keyboard(deps))
        # Лиды, собранные до появления владельца, досылаем — иначе они молча лежат в базе
        undelivered = await db.undelivered_leads(20)
        if undelivered and (first_owner or deps.extra.get("redeliver")):
            await m.answer(f"Пока вас не было, нашёл {len(undelivered)} лидов — присылаю лучшие:")
            for row in reversed(undelivered):
                await deps.extra["redeliver"](row)

    @router.message(Command("help"))
    async def help_cmd(m: Message):
        if await allowed(m):
            await m.answer(HELP)

    @router.message(Command("niche"))
    async def niche(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        from ..niches import NICHES, apply, describe
        key = (command.args or "").strip().lower()
        if not key:
            cur = await db.get_setting("niche", "web")
            lines = [f"{'▶️' if k == cur else '▫️'} <code>/niche {k}</code> — {n.title}" for k, n in NICHES.items()]
            await m.answer("<b>Ниши</b> — одна команда меняет профиль для ИИ, фразы поиска, словарь и рубрики контента:\n"
                           + "\n".join(lines) + "\n\nСвоя ниша: /profile текст + /phrases_set + /kw_add")
            return
        if key not in NICHES:
            await m.answer("Нет такой ниши. Список: /niche")
            return
        n = NICHES[key]
        await apply(db, deps.phrases, n)
        for src in deps.sources:  # Яндекс.Карты: кого искать без сайта
            if n.yandex_queries and hasattr(src, "queries") and getattr(src, "name", "") == "yandex_maps":
                src.queries = n.yandex_queries
        await m.answer("Переключил нишу:\n" + describe(n) + "\n\nИсточники подхватят фразы на следующем проходе. "
                       "Проверьте /profile, при желании уточните его под себя.")

    @router.message(Command("fresh"))
    async def fresh(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        from ..freshness import Freshness
        cur = Freshness.from_settings({"board_max_age_hours": await db.get_setting("board_max_age_hours"),
                                       "max_competitors": await db.get_setting("max_competitors")})
        args = (command.args or "").split()
        if len(args) == 2:
            try:
                hours, comp = float(args[0].replace(",", ".")), int(args[1])
            except ValueError:
                await m.answer("Формат: /fresh часы откликов — например /fresh 2 10")
                return
            await db.set_setting("board_max_age_hours", str(hours))
            await db.set_setting("max_competitors", str(comp))
            await m.answer(f"Биржи: беру заказы моложе {hours:g} ч и не больше {comp} откликов. Остальное не присылаю.")
            return
        await m.answer(f"<b>Свежесть заказов с бирж</b> (Kwork, Weblancer, Freelance.ru, FL, hh)\n"
                       f"Присылаю только моложе <b>{cur.board_max_age_hours:g} ч</b> и с не больше <b>{cur.max_competitors}</b> откликами. "
                       f"Моложе {cur.hot_age_minutes:g} мин и до {cur.hot_competitors} откликов — +2 к баллу.\n\n"
                       f"Изменить: /fresh 2 10 (часы, откликов). Чаты, карты и аудит это не трогает.")

    @router.message(Command("app"))
    async def app_cmd(m: Message):
        if not await allowed(m):
            return
        url = deps.extra.get("miniapp_url")
        if not url:
            await m.answer("Mini App не включён: боту нужен публичный HTTPS-адрес.\n"
                           "Быстро и бесплатно: <code>MINIAPP_TUNNEL=cloudflare</code> в .env — бот сам поднимет туннель и поставит "
                           "кнопку «Кабинет» в меню. Или свой домен: <code>MINIAPP_URL=https://ваш-домен/app</code>.")
            return
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть кабинет", web_app=WebAppInfo(url=url))]])
        await m.answer("Кабинет: лиды, отчёт, контент и настройки. Кнопка «Кабинет» есть и в меню рядом с полем ввода.", reply_markup=kb)

    # --- рабочая группа и команда ---
    @router.message(Command("setup_group"))
    async def setup_group(m: Message):
        if not await guard.is_owner(m.from_user.id):
            return
        if m.chat.type not in ("supergroup", "group"):
            await m.answer("Выполните эту команду внутри группы, где бот — администратор.")
            return
        problems = await deps.workspace.setup(m.chat.id)
        if problems:
            await m.answer("Не готово:\n" + "\n".join(f"• {html.escape(p)}" for p in problems))
            return
        await m.answer("Готово: темы созданы, лиды будут приходить сюда. Горячие — со звуком, остальные тихо.\n"
                       "Участники группы могут нажимать «Беру» — так лид закрепляется за человеком.")

    @router.message(Command("team"))
    async def team(m: Message):
        if not await allowed(m):
            return
        members = await db.members()
        stats = await db.team_stats(7)
        by_user: dict[str, dict[str, int]] = {}
        for r in stats:
            by_user.setdefault(r["user_name"], {})[r["status"]] = r["n"]
        lines = ["<b>Команда</b> (нажмите любую кнопку в группе, чтобы попасть в неё):"]
        names = {mem["name"]: mem["user_id"] for mem in members}
        if await guard.is_owner(m.from_user.id) and m.from_user.id not in names.values():
            names.setdefault(Guard.name(m.from_user) + " (владелец)", m.from_user.id)  # владелец виден, даже если ещё ничего не нажимал
        for name in by_user:  # владелец и те, кто действовал, но не в таблице команды
            names.setdefault(name, None)
        for name, uid in names.items():
            s = by_user.get(name, {})
            lines.append(f"• {html.escape(name)}{f' (id {uid})' if uid else ''}: взял {s.get('taken', 0)}, "
                         f"написал {s.get('contacted', 0)}, сделок {s.get('deal', 0)}")
        times = await db.response_times(7)
        if times:
            fast = sum(1 for t in times if t <= 5)
            lines.append(f"\n⏱ Скорость реакции за неделю: медиана {median(times):.0f} мин, "
                         f"в первые 5 минут — {fast * 100 // len(times)}%")
        await m.answer("\n".join(lines))

    @router.message(Command("team_del"))
    async def team_del(m: Message, command: CommandObject):
        if not await guard.is_owner(m.from_user.id):
            return
        if not command.args or not command.args.strip().isdigit():
            await m.answer("Формат: /team_del user_id (см. /team)")
            return
        n = await db.remove_member(int(command.args.strip()))
        await m.answer("Убрал" if n else "Нет такого")

    # --- лиды ---
    @router.message(Command("stats"))
    async def stats(m: Message):
        if not await allowed(m):
            return
        rows = await db.stats(24)
        if not rows:
            await m.answer("За сутки лидов нет.")
            return
        total = sum(c for _, c in rows)
        await m.answer(f"Лидов за 24ч: {total}\n" + "\n".join(f"• {s}: {c}" for s, c in rows))

    @router.message(Command("leads"))
    async def leads(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        n = int(command.args) if command.args and command.args.isdigit() else 10
        rows = await db.recent_leads(min(n, 30))
        await m.answer("\n".join(_lead_line(r) for r in rows) if rows else "Лидов пока нет.")

    @router.message(Command("digest"))
    async def digest(m: Message):
        if await allowed(m):
            await m.answer(await build_digest(db))

    @router.message(Command("funnel"))
    async def funnel(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        args = (command.args or "").split()
        days = int(args[0]) if args and args[0].isdigit() else 30
        by = next((a for a in args if a in ("source", "service", "phrase", "chat")), "source")
        await m.answer(await build_funnel(db, days, by))

    @router.message(Command("export"))
    async def export(m: Message):
        if not await allowed(m):
            return
        data = await db.export_csv()
        await m.answer_document(BufferedInputFile(data, filename="leads.csv"), caption="Последние 1000 лидов")

    @router.message(Command("sources"))
    async def sources_cmd(m: Message):
        if await allowed(m):
            await m.answer("Источники:\n" + "\n".join(f"• {html.escape(s.status_line())}" for s in deps.sources))

    # --- словарь и правила ---
    @router.message(Command("keywords"))
    async def keywords(m: Message):
        if not await allowed(m):
            return
        kw = await db.get_keywords()
        by_cat: dict[str, list[str]] = {}
        for w, cat in kw.topic.items():
            by_cat.setdefault(cat, []).append(w)
        topic = "\n".join(f"  {cat}: {', '.join(ws)}" for cat, ws in by_cat.items())
        await m.answer(f"<b>Тема</b>:\n{html.escape(topic)}\n\n<b>Намерение</b>: {html.escape(', '.join(kw.intent))}\n\n"
                       f"<b>Стоп-слова</b>: {html.escape(', '.join(kw.negative))}")

    @router.message(Command("kw_add"))
    async def kw_add(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        parts = (command.args or "").split()
        if len(parts) < 2 or parts[0] not in ("topic", "intent", "negative"):
            await m.answer("Формат: /kw_add topic|intent|negative слово [услуга]\nНапример: /kw_add topic квиз сайт")
            return
        kind, word = parts[0], parts[1]
        category = parts[2] if kind == "topic" and len(parts) > 2 else None
        await db.add_keyword(kind, word, category)
        await m.answer(f"Добавил «{word}» в {kind}" + (f" ({category})" if category else ""))

    @router.message(Command("kw_del"))
    async def kw_del(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not command.args:
            await m.answer("Формат: /kw_del слово")
            return
        n = await db.remove_keyword(command.args.strip())
        await m.answer("Удалил" if n else "Такого слова нет")

    @router.message(Command("rules"))
    async def rules(m: Message):
        if not await allowed(m):
            return
        rs = await db.rules()
        if not rs:
            await m.answer("Правил нет. Пример: /rule_add +сайт +(нужен|ищу|заказать) -предлагаю -курс\n"
                           "+слово — обязательно, (а|б) — любое из, -слово — запрет, \"точная форма\" в кавычках.\n"
                           "Правило только из -слов блокирует всё, что их содержит.")
            return
        await m.answer("Правила (лид, подходящий под правило, проходит порог автоматически):\n"
                       + "\n".join(f"{r['id']}. <code>{html.escape(r['expr'])}</code>" for r in rs)
                       + "\n\nУдалить: /rule_del id")

    @router.message(Command("rule_add"))
    async def rule_add(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        from ..filters import parse_rule
        if not command.args:
            await m.answer("Формат: /rule_add +сайт +(нужен|ищу) -предлагаю")
            return
        try:
            parse_rule(command.args)
        except ValueError as e:
            await m.answer(f"Ошибка в правиле: {html.escape(str(e))}")
            return
        await db.add_rule(command.args)
        await m.answer("Правило добавлено")

    @router.message(Command("rule_del"))
    async def rule_del(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not command.args or not command.args.strip().isdigit():
            await m.answer("Формат: /rule_del id")
            return
        await m.answer("Удалил" if await db.remove_rule(int(command.args)) else "Нет такого")

    @router.message(Command("geo"))
    async def geo(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not command.args:
            cur = await db.get_setting("geo", "")
            await m.answer(f"Гео-фильтр: {html.escape(cur) or 'выключен (все города)'}\n/geo Москва, Тула — включить; /geo off — выключить")
            return
        if command.args.strip().lower() in ("off", "выкл"):
            await db.set_setting("geo", "")
            await m.answer("Гео-фильтр выключен")
            return
        await db.set_setting("geo", command.args.strip())
        await m.answer(f"Оставляю лиды из: {html.escape(command.args.strip())} (и без указания города)")

    @router.message(Command("threshold"))
    async def threshold(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not command.args or not command.args.isdigit():
            await m.answer(f"Текущий порог: {await db.get_setting('threshold', 'по умолчанию')}\nФормат: /threshold N")
            return
        await db.set_setting("threshold", command.args)
        await m.answer(f"Порог: {command.args}")

    @router.message(Command("profile"))
    async def profile(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not command.args:
            cur = await db.get_setting("profile") or deps.default_profile
            await m.answer(f"Профиль студии для ИИ:\n{html.escape(cur)}\n\nИзменить: /profile текст")
            return
        await db.set_setting("profile", command.args.strip())
        await m.answer("Профиль обновлён — ИИ будет оценивать лидов с учётом него.")

    @router.message(Command("phrases"))
    async def phrases_show(m: Message):
        if await allowed(m):
            ps = await deps.phrases.get()
            await m.answer("Фразы для поиска:\n" + "\n".join(f"• {html.escape(p)}" for p in ps)
                           + "\n\nИзменить: /phrases_set фраза; фраза; … или /phrases_gen")

    @router.message(Command("phrases_set"))
    async def phrases_set(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not command.args:
            await m.answer("Формат: /phrases_set нужен сайт; ищу разработчика бота; …")
            return
        ps = [p for p in command.args.split(";") if p.strip()]
        await deps.phrases.set(ps)
        await m.answer(f"Сохранил {len(ps)} фраз — источники подхватят на следующем проходе.")

    @router.message(Command("phrases_gen"))
    async def phrases_gen(m: Message):
        if not await allowed(m):
            return
        if not deps.llm:
            await m.answer("ИИ не подключён (OPENAI_API_KEY).")
            return
        profile = await db.get_setting("profile", deps.default_profile) or deps.default_profile
        try:
            ps = await deps.phrases.generate(deps.llm, profile)
        except Exception as e:
            await m.answer(f"Не получилось: {html.escape(str(e))}")
            return
        await m.answer("ИИ предложил и сохранил:\n" + "\n".join(f"• {html.escape(p)}" for p in ps))

    @router.message(Command("template_set"))
    async def template_set(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.answer("Формат: /template_set сайт Здравствуйте! Увидел ваш запрос…")
            return
        await db.set_template(parts[0].lower(), parts[1])
        await m.answer(f"Шаблон для «{parts[0]}» сохранён")

    @router.message(Command("templates"))
    async def templates(m: Message):
        if await allowed(m):
            ts = await db.templates()
            await m.answer("\n\n".join(f"<b>{html.escape(k)}</b>: {html.escape(v)}" for k, v in ts.items()) or "Шаблонов нет")

    @router.message(Command("block"))
    async def block(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not command.args:
            await m.answer("Формат: /block @username")
            return
        await db.add_ignored("author", command.args.strip())
        await m.answer(f"Автор {html.escape(command.args.strip())} в игноре")

    @router.message(Command("unblock"))
    async def unblock(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        n = await db.remove_ignored((command.args or "").strip())
        await m.answer("Убрал из игнора" if n else "Такого в игноре нет")

    @router.message(Command("ignored"))
    async def ignored(m: Message):
        if not await allowed(m):
            return
        ig = await db.get_ignored()
        chats = "\n".join(f"  {html.escape(label or key)} ({key})" for key, label in ig["chat"].items()) or "  —"
        authors = ", ".join(html.escape(k) for k in ig["author"]) or "—"
        await m.answer(f"<b>Чаты</b>:\n{chats}\n<b>Авторы</b>: {authors}\n\nСнять: /unblock ключ")

    @router.message(Command("pause"))
    async def pause(m: Message):
        if await allowed(m):
            await db.set_setting("paused", "1")
            await m.answer("Уведомления на паузе, лиды копятся в базе.")

    @router.message(Command("resume"))
    async def resume(m: Message):
        if await allowed(m):
            await db.set_setting("paused", "0")
            await m.answer("Уведомления включены.")

    # --- Telegram-чаты через userbot ---
    @router.message(Command("tg_find"))
    async def tg_find(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not tg:
            await m.answer("Telegram-аккаунт не подключён (TG_API_ID / TG_API_HASH).")
            return
        if not command.args:
            await m.answer("Формат: /tg_find фриланс заказы")
            return
        found = await tg.find_chats(command.args)
        if not found:
            await m.answer("Ничего не нашёл.")
            return
        lines = [f"{'👥' if c['is_group'] else '📢'} {html.escape(c['title'])} — @{c['username']}"
                 + (f" ({c['members']})" if c["members"] else "") for c in found]
        await m.answer("\n".join(lines) + "\n\nВступить: /tg_join @username")

    @router.message(Command("tg_join"))
    async def tg_join(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not tg:
            await m.answer("Telegram-аккаунт не подключён.")
            return
        if not command.args:
            await m.answer("Формат: /tg_join @chat или ссылка-приглашение")
            return
        try:
            title = await tg.join(command.args.strip())
            await m.answer(f"Вступил: {html.escape(title)}. Новые сообщения уже отслеживаются.")
        except Exception as e:
            await m.answer(f"Не получилось: {html.escape(str(e))}")

    @router.message(Command("tg_chats"))
    async def tg_chats(m: Message):
        if not await allowed(m):
            return
        if not tg:
            await m.answer("Telegram-аккаунт не подключён.")
            return
        chats = await tg.list_chats()
        lines = [f"• {html.escape(c['title'])}" + (f" (@{c['username']})" if c["username"] else "") for c in chats[:80]]
        await m.answer(f"Чатов: {len(chats)}\n" + "\n".join(lines))

    @router.message(Command("chats_rating"))
    async def chats_rating(m: Message):
        if not await allowed(m):
            return
        rows = await db.chat_rating(30)
        if not rows:
            await m.answer("Пока нет данных — статистика копится по мере работы.")
            return
        lines = [f"• {html.escape(r['title'] or r['chat_key'])}: лидов {r['leads']} из {r['messages']} сообщений"
                 for r in rows]
        await m.answer("Рейтинг чатов (лиды / сообщения):\n" + "\n".join(lines)
                       + "\n\nШумные чаты: кнопка «🔇 Чат в игнор» под карточкой")

    @router.message(Command("discover"))
    async def discover(m: Message, command: CommandObject):
        if not await allowed(m):
            return
        if not tg or not deps.discover:
            await m.answer("Нужен подключённый Telegram-аккаунт.")
            return
        niche = (command.args or "").strip()
        if not niche:
            from ..chat_catalog import NICHES
            await m.answer("Ниши: " + ", ".join(NICHES) + "\nФормат: /discover предприниматели")
            return
        await m.answer("Ищу и оцениваю чаты, это займёт пару минут…")
        try:
            found = await deps.discover.run(niche)
        except Exception as e:
            await m.answer(f"Не получилось: {html.escape(str(e))}")
            return
        if not found:
            await m.answer("Подходящих чатов не нашёл.")
            return
        lines = [f"{c['score']:>3} · {html.escape(c['title'])} — @{c['username']}"
                 + (f" ({c['members']})" if c.get("members") else "") + (" ✅ вступил" if c.get("joined") else "")
                 for c in found]
        await m.answer("Чаты по нише (балл = насколько часто там ищут исполнителей):\n" + "\n".join(lines)
                       + "\n\nВступить: /tg_join @username")

    async def announce_claim_code() -> None:
        await guard.ensure_claim_code(announce=True)  # пишет код в лог, если владельца нет

    router.startup.register(announce_claim_code)
    return router


def _lead_line(r) -> str:
    head = html.escape((r["title"] or r["text"] or "")[:80])
    link = f' — <a href="{html.escape(r["url"])}">ссылка</a>' if r["url"] else ""
    service = f" 🎯{r['service']}" if r["service"] else ""
    who = f" 👤{html.escape(r['assigned_name'])}" if r["assigned_name"] else ""
    return f"#{r['id']} [{STATUS_LABELS.get(r['status'], r['status'])}] {r['score']}/10 {r['source']}{service}{who}: {head}{link}"


async def build_digest(db: Database, hours: int = 24) -> str:
    rows = await db.leads_since(hours)
    if not rows:
        return f"За {hours}ч лидов нет."
    by_source: dict[str, int] = {}
    by_service: dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        if r["service"]:
            by_service[r["service"]] = by_service.get(r["service"], 0) + 1
    untouched = [r for r in rows if r["status"] == "new"]
    top = sorted(untouched, key=lambda r: -r["score"])[:5]
    lines = [f"📊 <b>Дайджест за {hours}ч</b>: {len(rows)} лидов, не разобрано {len(untouched)}"]
    lines.append("По источникам: " + ", ".join(f"{s} {c}" for s, c in sorted(by_source.items(), key=lambda x: -x[1])))
    if by_service:
        lines.append("По услугам: " + ", ".join(f"{s} {c}" for s, c in sorted(by_service.items(), key=lambda x: -x[1])))
    if top:
        lines.append("\n🔥 <b>Топ неразобранных</b>:")
        lines += [_lead_line(r) for r in top]
    return "\n".join(lines)


async def build_funnel(db: Database, days: int = 30, by: str = "source") -> str:
    rows = await db.funnel(days, by)
    if not rows:
        return f"За {days} дн. лидов нет."
    label = {"source": "источникам", "service": "услугам", "phrase": "фразам", "chat": "чатам"}[by]
    lines = [f"📈 <b>Воронка за {days} дн. по {label}</b>\nнайдено → взяли → написали → ответил → сделка (спам)"]
    for r in rows[:25]:
        conv = f" · {r['deal'] * 100 // r['found']}%" if r["deal"] else ""
        lines.append(f"• {html.escape(str(r['k']))[:40]}: {r['found']} → {r['taken']} → {r['contacted']} → "
                     f"{r['replied']} → <b>{r['deal']}</b> ({r['spam']}){conv}")
    times = await db.response_times(days)
    if times:
        lines.append(f"\n⏱ Медиана «появился → взяли»: {median(times):.0f} мин; в первые 5 мин — "
                     f"{sum(1 for t in times if t <= 5) * 100 // len(times)}%")
    return "\n".join(lines)
