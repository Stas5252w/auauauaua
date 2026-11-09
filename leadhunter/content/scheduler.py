"""Фоновые циклы контент-фабрики: публикация по расписанию и автопилот (черновик каждый день)."""
import asyncio
import html
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .factory import API_PLATFORMS, ContentFactory

log = logging.getLogger(__name__)

STUCK_MINUTES = 10  # столько publishing считается зависшим


async def publish_due(factory: ContentFactory, publishers: list, notify) -> int:
    """Один проход: всё, чьё время пришло. notify(text) — сообщить владельцу о результате."""
    n = 0
    # зависшие после рестарта: вернуть в план (вышедшие площадки пропустятся) или сообщить, что прервано
    for item_id, requeued in await factory.store.requeue_stuck(STUCK_MINUTES, exclude=factory.active):
        await notify(f"♻️ #{item_id}: публикация прервалась, повторю только недоставленные площадки." if requeued
                     else f"⚠️ #{item_id}: публикация несколько раз прерывалась, автоповтор остановлен — проверьте площадки и нажмите «Опубликовать».")
    e = html.escape
    for row in await factory.store.due():
        name = f"#{row['id']} «{e(row['title'] or row['topic'])}»"
        if not publishers:
            await factory.store.schedule(row["id"], None)
            await notify(f"⚠️ {name} не вышел: площадки не настроены (/channels). Вернул в черновики.")
            continue
        out = await factory.publish(row, publishers)
        if out.status == "busy":
            continue  # уже публикуется или вышло: второй раз не постим
        n += 1
        fresh = await factory.store.get(row["id"])
        done = json.loads(fresh["published"] or "[]") if fresh else []
        links = ", ".join(API_PLATFORMS.get(d["platform"], d["platform"]) + (f' <a href="{e(d["url"])}">↗</a>' if d.get("url") else "")
                          for d in done)
        notes = "".join(f"\n⚠️ {e(r.note)}" for r in out.results if getattr(r, "note", ""))
        if out.errors and done:
            await notify(f"⚠️ {name} опубликован частично: {links}\nНе вышло:\n" + "\n".join(e(x) for x in out.errors)
                         + f"{notes}\nПовтор — «Опубликовать» в карточке (уйдёт только на недостающие площадки).")
        elif out.errors:
            await notify(f"⚠️ {name} не опубликовался, вернул в черновики:\n" + "\n".join(e(x) for x in out.errors))
        elif done:
            await notify(f"✅ {name} опубликован: {links}{notes}")
    return n


async def content_loop(factory: ContentFactory, get_publishers, notify) -> None:
    while True:
        try:
            await publish_due(factory, await get_publishers(), notify)
        except Exception:
            log.exception("content loop")
        await asyncio.sleep(60)


async def autopilot_once(factory: ContentFactory, deliver, tz_name: str, hour: int, *, now: datetime | None = None) -> int | None:
    """Если автопилот включён и сегодня ещё не приносили: взять идею из плана на сегодня (или придумать) → черновик."""
    db = factory.db
    if await db.get_setting("autopilot", "0") != "1":
        return None
    now = now or datetime.now(ZoneInfo(tz_name))
    today = now.strftime("%Y-%m-%d")
    if now.hour < hour or await db.get_setting("autopilot_last") == today:
        return None
    await db.set_setting("autopilot_last", today)  # сначала отметка — чтобы ошибка ИИ не зациклила попытки
    kind, topic, parent = "post", "", None
    for r in await factory.store.recent(50, status="draft", kind="idea"):
        if r["scheduled_at"] and r["scheduled_at"] <= datetime.now(timezone.utc).isoformat():
            kind, topic, parent = (r["platforms"] or "post"), r["topic"], r["id"]
            break
    if not topic:
        ideas = (await factory.gen.generate("ideas", "", n=1))["ideas"]
        if not ideas:
            return None
        kind, topic = ideas[0].get("kind", "post"), ideas[0]["topic"]
    row = await factory.create(kind, topic, created_by="автопилот", parent_id=parent)
    if parent:
        await factory.store.update(parent, status="published")
    await deliver(row)
    return row["id"]


async def autopilot_loop(factory: ContentFactory, deliver, tz_name: str, hour: int) -> None:
    while True:
        try:
            await autopilot_once(factory, deliver, tz_name, hour)
        except Exception:
            log.exception("autopilot")
        await asyncio.sleep(600)
