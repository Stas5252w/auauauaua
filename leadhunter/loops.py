"""Фоновые циклы: SLA (лид не взят N минут), follow-up (клиент не ответил), дайджесты."""
import asyncio
import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Database
from .bot.handlers import build_digest, build_funnel
from .bot.notify import TelegramNotifier, followup_keyboard
from .bot.workspace import message_link

log = logging.getLogger(__name__)


async def sla_loop(db: Database, notifier: TelegramNotifier, minutes: int, min_score: int) -> None:
    while True:
        try:
            for r in await db.sla_overdue(minutes, min_score):
                age = int((datetime.now().astimezone() - datetime.fromisoformat(r["created_at"])).total_seconds() // 60)
                link = message_link(r["msg_chat_id"], r["thread_id"], r["msg_id"]) if r["msg_chat_id"] and r["msg_id"] else r["url"]
                head = html.escape((r["title"] or r["text"] or "")[:80])
                text = (f"⏰ <b>Лид #{r['id']} никто не взял {age} мин</b> ({r['score']}/10)\n{head}\n"
                        + (f'<a href="{html.escape(link)}">Открыть</a>' if link else ""))
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👋 Беру", callback_data=f"take:{r['id']}")]])
                await notifier.send(text, topic="hot", silent=False, reply_markup=kb)
                await db.mark_sla_pinged(r["id"])
        except Exception:
            log.exception("sla loop")
        await asyncio.sleep(60)


async def followup_loop(db: Database, notifier: TelegramNotifier) -> None:
    while True:
        try:
            for r in await db.followups_due():
                head = html.escape((r["title"] or r["text"] or "")[:80])
                who = f" ({html.escape(r['assigned_name'])})" if r["assigned_name"] else ""
                contact = f" · {html.escape(r['contact'])}" if r["contact"] else ""
                text = (f"🔔 <b>Follow-up #{r['id']}</b>{who}: писали {r['followup_count']} раз, ответа нет\n"
                        f"{head}{contact}")
                await db.clear_followup(r["id"])
                await notifier.send(text, topic="hot", silent=False, reply_markup=followup_keyboard(r["id"]))
        except Exception:
            log.exception("followup loop")
        await asyncio.sleep(300)


async def digest_loop(db: Database, notifier: TelegramNotifier, hour: int, tz_name: str) -> None:
    tz = ZoneInfo(tz_name)
    while True:
        now = datetime.now(tz)
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await notifier.send(await build_digest(db), topic="reports")
            if datetime.now(tz).weekday() == 0:  # понедельник — недельная воронка
                await notifier.send(await build_funnel(db, 7, "source"), topic="reports")
        except Exception:
            log.exception("дайджест не отправился")


async def purge_loop(db: Database, days: int) -> None:
    """Раз в сутки чистит старые записи дедупа (первый проход — сразу при старте)."""
    while True:
        try:
            n = await db.purge_old(days)
            if n:
                log.info("purge: удалено %d старых записей дедупа", n)
        except Exception:
            log.exception("purge loop")
        await asyncio.sleep(24 * 3600)
