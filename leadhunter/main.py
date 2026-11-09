import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from .bot.callbacks import build_callbacks
from .bot.content import build_content_router
from .bot.setup import build_onboarding_callbacks, build_setup_router
from .bot.guard import Guard
from .bot.handlers import BotDeps, build_router
from .bot.notify import TelegramNotifier
from .bot.workspace import Workspace
from .config import Settings
from .content.runtime import build_content, build_llm, reconfigure_ai
from .content.scheduler import autopilot_loop, content_loop
from .db import Database
from .integrations import Integrations, load_overrides
from .loops import digest_loop, followup_loop, purge_loop, sla_loop
from .phrases import PhraseProvider, default_phrases
from .pipeline import Pipeline
from .sources import build_sources
from .sources.base import watch
from .sources.telegram_chats import TelegramChatsSource
from .webhook import MultiNotifier, WebhookNotifier

log = logging.getLogger("leadhunter")

BOT_COMMANDS = [
    ("start", "Начать работу"), ("setup", "Подключения и ключи"), ("status", "Состояние бота"),
    ("niche", "Выбрать нишу"), ("app", "Открыть кабинет"), ("fresh", "Самые свежие лиды"),
    ("leads", "Последние лиды"), ("stats", "Статистика"), ("digest", "Дайджест за день"),
    ("funnel", "Воронка по лидам"), ("export", "Экспорт лидов в CSV"), ("sources", "Состояние источников"),
    ("post", "Сгенерировать пост"), ("toon", "Сгенерировать мультик"), ("ideas", "Идеи для контента"),
    ("queue", "Очередь публикаций"), ("autopilot", "Автопилот контента"), ("pause", "Поставить поиск на паузу"),
    ("resume", "Возобновить поиск"), ("help", "Список команд"),
]


def spawn(coro, name: str) -> asyncio.Task:
    return watch(asyncio.create_task(coro, name=name))


async def run() -> None:
    s = Settings()
    db = Database(s.db_path)
    await db.connect()
    applied = await load_overrides(db, s)  # ключи, введённые через /setup или кабинет
    if applied:
        log.info("подключения из базы: %s", ", ".join(applied))

    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    tg_notifier = TelegramNotifier(bot, db, s.owner_ids, outreach_enabled=s.tg_outreach_limit > 0,
                                   hot_score=s.hot_score, deliver_dm=s.deliver_dm)
    notifier = MultiNotifier(tg_notifier, WebhookNotifier(s.webhook_url) if s.webhook_url else None)
    llm = build_llm(s)
    pipeline = Pipeline(db, notifier, s.score_threshold, llm, max_age_hours=s.max_age_hours,
                        llm_min_score=s.llm_min_score, profile=s.profile, spam_chats=s.spam_chats_per_hour)

    phrases = PhraseProvider(db, default_phrases(s))
    sources = build_sources(s, db, phrases)
    for src in sources:
        src.alert = tg_notifier.send
    tg = next((x for x in sources if isinstance(x, TelegramChatsSource)), None)
    if tg:
        tg.on_message_stats = db.bump_chat_stats
        pipeline.author_bio = tg.get_bio
    log.info("источники: %s; ИИ: %s", ", ".join(x.name for x in sources) or "нет", llm.model if llm else "выкл")

    guard = Guard(bot, db, s.owner_ids)
    deps = BotDeps(db, sources, guard, phrases, tg_notifier.workspace, llm, s.profile, s.tg_outreach_limit,
                   s.followup_hours, s.digest_tz)
    deps.extra["redeliver"] = tg_notifier.notify_row
    deps.extra["threshold"] = s.score_threshold
    if tg:
        from .discovery import ChatDiscovery
        deps.discover = ChatDiscovery(tg, db, llm, s.discover_join_per_day)

    dp = Dispatcher()
    dp.include_router(build_router(deps))
    dp.include_router(build_callbacks(deps))
    dp.include_router(build_content_router(deps))
    dp.include_router(build_setup_router(deps))
    dp.include_router(build_onboarding_callbacks(deps))
    deps.extra["pipeline"] = pipeline

    tunnel = None
    if s.miniapp_tunnel == "cloudflare" and not s.miniapp_url:
        from .tunnel import CloudflareTunnel
        tunnel = CloudflareTunnel(s.miniapp_port)
        try:
            s.miniapp_url = await tunnel.start() + "/app"
        except Exception as e:
            log.error("туннель не поднялся, Mini App выключен: %s", e)
            tunnel = None
    deps.extra["miniapp_url"] = s.miniapp_url
    factory = await build_content(s, db, bot, llm, deps, tg_notifier)

    async def on_change():
        reconfigure_ai(s, deps, factory, pipeline)

    deps.extra["integrations"] = Integrations(db, s, on_change)
    deps.extra["settings"] = s

    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    except Exception as e:
        log.warning("не удалось зарегистрировать команды бота: %s", e)

    tasks = [spawn(src.run(pipeline.handle), src.name) for src in sources]
    tasks.append(spawn(sla_loop(db, tg_notifier, s.sla_minutes, s.sla_min_score), "sla"))
    tasks.append(spawn(followup_loop(db, tg_notifier), "followup"))
    tasks.append(spawn(content_loop(factory, deps.extra["publishers"], deps.extra["content_notify"]), "content"))
    tasks.append(spawn(autopilot_loop(factory, deps.extra["deliver_to_team"], s.digest_tz, s.content_hour), "autopilot"))
    tasks.append(spawn(purge_loop(db, s.seen_ttl_days), "purge"))
    if s.digest_hour is not None:
        tasks.append(spawn(digest_loop(db, tg_notifier, s.digest_hour, s.digest_tz), "digest"))
    if s.miniapp_url:
        from .miniapp.server import start_miniapp
        tasks.append(spawn(start_miniapp(s, db, bot, deps), "miniapp"))
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        # Дожидаемся задач до закрытия базы, иначе они пишут в закрытое соединение
        await asyncio.gather(*tasks, return_exceptions=True)
        if tunnel:
            await tunnel.stop()
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
