"""HTTP-сервер Mini App: статика + JSON API. Авторизация — подпись initData от Telegram."""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web

from ..db import Database
from ..filters import parse_rule
from ..models import Lead
from .auth import validate_init_data
from .landing import render_landing, render_notice, render_thanks

log = logging.getLogger(__name__)
STATIC = os.path.join(os.path.dirname(__file__), "static")
STATUSES = ("new", "taken", "contacted", "replied", "deal", "lost", "spam", "hot")


def _row(r) -> dict:
    return dict(r) if r is not None else None


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return fwd or request.remote or "?"


class SubmitLimiter:
    """Скользящие окна в памяти: заявки с одного IP и на одну страницу (защита лендинга от спама)."""

    def __init__(self, per_ip: int = 5, ip_window: float = 600, per_slug: int = 30, slug_window: float = 3600):
        self.rules = ((per_ip, ip_window, "ip"), (per_slug, slug_window, "slug"))
        self.hits: dict[tuple[str, str], list[float]] = {}

    def allow(self, ip: str, slug: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self.hits) > 10_000:  # не даём словарю расти бесконечно
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 3600}
        keys = {"ip": ("ip", ip), "slug": ("slug", slug)}
        for limit, window, kind in self.rules:
            hits = [t for t in self.hits.get(keys[kind], []) if now - t < window]
            self.hits[keys[kind]] = hits
            if len(hits) >= limit:
                return False
        for kind in keys.values():
            self.hits[kind].append(now)
        return True


class MiniAppAPI:
    def __init__(self, db: Database, bot_token: str, guard, deps, sources, dev_user_id: int | None = None):
        self.limiter = SubmitLimiter()
        self.db = db
        self.bot_token = bot_token
        self.guard = guard
        self.deps = deps
        self.sources = sources
        self.dev_user_id = dev_user_id

    # --- auth ---
    async def user(self, request: web.Request) -> dict:
        init_data = request.headers.get("X-Init-Data", "")  # только заголовок: query попадает в логи и историю
        user = validate_init_data(init_data, self.bot_token) if init_data else None
        if user is None and self.dev_user_id and not init_data:
            user = {"id": self.dev_user_id, "first_name": "dev"}
        if not user or "id" not in user:
            raise web.HTTPUnauthorized(text="bad initData")
        uid = int(user["id"])
        if not (await self.guard.is_owner(uid) or await self.db.is_member(uid)):
            raise web.HTTPForbidden(text="not a team member")
        user["is_owner"] = await self.guard.is_owner(uid)
        return user

    @staticmethod
    def _name(user: dict) -> str:
        return " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username") or str(user["id"])

    # --- handlers ---
    async def me(self, request):
        user = await self.user(request)
        return web.json_response({"user": user, "sources": [s.status_line() for s in self.sources]})

    async def leads(self, request):
        await self.user(request)
        q = request.query
        where, params = ["1=1"], []
        if q.get("status"):
            if q["status"] == "open":
                where.append("status IN ('new','taken','contacted','replied')")
            else:
                where.append("status = ?"); params.append(q["status"])
        if q.get("source"):
            where.append("source = ?"); params.append(q["source"])
        if q.get("service"):
            where.append("service = ?"); params.append(q["service"])
        if q.get("q"):
            where.append("(text LIKE ? OR title LIKE ? OR author LIKE ?)")
            params += [f"%{q['q']}%"] * 3
        if q.get("mine") and q.get("mine") != "0":
            user = await self.user(request)
            where.append("assigned_to = ?"); params.append(int(user["id"]))
        limit = min(int(q.get("limit", 50)), 200)
        offset = int(q.get("offset", 0))
        cur = await self.db.conn.execute(
            f"SELECT id, source, score, status, service, urgency, budget, title, text, author, contact, url, summary,"
            f" assigned_name, created_at, chat_title FROM leads WHERE {' AND '.join(where)}"
            f" ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            r["text"] = (r["text"] or "")[:300]
        return web.json_response({"leads": rows})

    async def lead(self, request):
        await self.user(request)
        row = await self.db.get_lead(int(request.match_info["id"]))
        if not row:
            raise web.HTTPNotFound()
        cur = await self.db.conn.execute("SELECT status, user_name, at FROM lead_events WHERE lead_id = ? ORDER BY id",
                                         (row["id"],))
        events = [dict(r) for r in await cur.fetchall()]
        data = dict(row)
        if not data.get("draft") and data.get("service"):
            data["draft"] = await self.db.get_template(data["service"]) or ""
        return web.json_response({"lead": data, "events": events})

    async def set_status(self, request):
        user = await self.user(request)
        lead_id = int(request.match_info["id"])
        body = await request.json()
        status = body.get("status")
        if status not in STATUSES:
            raise web.HTTPBadRequest(text="bad status")
        row = await self.db.get_lead(lead_id)
        if not row:
            raise web.HTTPNotFound()
        name = self._name(user)
        if status in ("taken", "contacted", "replied", "deal", "lost") and not row["assigned_to"]:
            await self.db.assign(lead_id, int(user["id"]), name)
        if status == "taken":
            if not await self.db.assign(lead_id, int(user["id"]), name):
                raise web.HTTPConflict(text=f"уже взял {row['assigned_name']}")
        else:
            await self.db.set_status(lead_id, status, int(user["id"]), name)
        if status == "contacted":
            await self.db.schedule_followup(lead_id, self.deps.followup_hours)
        elif status != "hot":
            await self.db.clear_followup(lead_id)
        return web.json_response({"lead": _row(await self.db.get_lead(lead_id))})

    async def stats(self, request):
        await self.user(request)
        days = int(request.query.get("days", 30))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.db.conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n, SUM(status='deal') AS deals FROM leads"
            " WHERE created_at >= ? GROUP BY day ORDER BY day", (since,))
        per_day = [dict(r) for r in await cur.fetchall()]
        cur = await self.db.conn.execute("SELECT status, COUNT(*) AS n FROM leads WHERE created_at >= ? GROUP BY status", (since,))
        by_status = {r["status"]: r["n"] for r in await cur.fetchall()}
        times = await self.db.response_times(days)
        times.sort()
        return web.json_response({
            "days": days,
            "per_day": per_day,
            "by_status": by_status,
            "funnel_source": [dict(r) for r in await self.db.funnel(days, "source")],
            "funnel_service": [dict(r) for r in await self.db.funnel(days, "service")],
            "funnel_phrase": [dict(r) for r in await self.db.funnel(days, "phrase")][:15],
            "funnel_chat": [dict(r) for r in await self.db.funnel(days, "chat")][:15],
            "response": {"n": len(times), "median_min": times[len(times) // 2] if times else None,
                         "under5_pct": (sum(1 for t in times if t <= 5) * 100 // len(times)) if times else None},
            "team": [dict(r) for r in await self.db.team_stats(days)],
            "sources": [s.status_line() for s in self.sources],
        })

    async def settings_get(self, request):
        await self.user(request)
        kw = await self.db.get_keywords()
        from ..freshness import Freshness
        from ..niches import NICHES
        from ..content.factory import brand_from_setting
        fr = Freshness.from_settings({"board_max_age_hours": await self.db.get_setting("board_max_age_hours"),
                                      "max_competitors": await self.db.get_setting("max_competitors")})
        brand = brand_from_setting(await self.db.get_setting("brand"))
        factory = self.deps.extra.get("factory")
        channels = await factory.store.channels() if factory else {}
        integ = self.deps.extra.get("integrations")
        return web.json_response({
            "threshold": await self.db.get_setting("threshold", str(self.deps.extra.get("threshold", 5))),
            "profile": await self.db.get_setting("profile") or self.deps.default_profile,
            "geo": await self.db.get_setting("geo", ""),
            "paused": (await self.db.get_setting("paused")) == "1",
            "phrases": await self.deps.phrases.get(),
            "rules": [dict(r) for r in await self.db.rules()],
            "keywords": {"topic": kw.topic, "intent": kw.intent, "negative": kw.negative},
            "templates": await self.db.templates(),
            "ignored": await self.db.get_ignored(),
            "team": [dict(r) for r in await self.db.members()],
            "workspace": await self.db.get_setting("workspace_chat_id", ""),
            "niche": await self.db.get_setting("niche", ""),
            "niches": [{"key": k, "title": n.title} for k, n in NICHES.items()],
            "fresh": {"board_max_age_hours": fr.board_max_age_hours, "max_competitors": fr.max_competitors},
            "voice": await self.db.get_setting("voice", ""),
            "brand": {"name": brand.name, "handle": brand.handle, "primary": brand.primary, "accent": brand.accent},
            "autopilot": (await self.db.get_setting("autopilot")) == "1",
            "pillars": await self.db.get_setting("pillars", ""),
            "channels": channels,
            "integrations": await integ.status() if integ else [],
            "summary": integ.summary() if integ else {},
            "miniapp_url": self.deps.extra.get("miniapp_url"),
        })

    async def settings_set(self, request):
        user = await self.user(request)
        body = await request.json()
        factory = self.deps.extra.get("factory")
        for key, value in body.items():
            if key == "threshold":
                await self.db.set_setting("threshold", str(max(0, min(10, int(value)))))
            elif key in ("profile", "geo", "voice", "pillars"):
                await self.db.set_setting(key, str(value).strip())
            elif key in ("paused", "autopilot"):
                await self.db.set_setting(key, "1" if value else "0")
            elif key == "phrases":
                await self.deps.phrases.set(list(value))
            elif key == "templates":
                for service, text in dict(value).items():
                    await self.db.set_template(service, text)
            elif key == "niche":
                from ..niches import NICHES, apply
                if value not in NICHES:
                    raise web.HTTPBadRequest(text="unknown niche")
                await apply(self.db, self.deps.phrases, NICHES[value])
            elif key == "fresh":
                v = dict(value)
                if "board_max_age_hours" in v:
                    await self.db.set_setting("board_max_age_hours", str(max(0.1, min(72.0, float(v["board_max_age_hours"])))))
                if "max_competitors" in v:
                    await self.db.set_setting("max_competitors", str(max(0, min(500, int(v["max_competitors"])))))
            elif key == "brand":
                v = {k: str(dict(value).get(k, "")).strip() for k in ("name", "handle", "primary", "accent")}
                for c in ("primary", "accent"):
                    if v[c] and not (v[c].startswith("#") and len(v[c]) in (4, 7)):
                        raise web.HTTPBadRequest(text=f"{c}: цвет в формате #RRGGBB")
                await self.db.set_setting("brand", json.dumps({k: x for k, x in v.items() if x}, ensure_ascii=False))
            elif key == "channels":
                if not factory:
                    raise web.HTTPBadRequest(text="контент-фабрика выключена")
                for platform, target in dict(value).items():
                    if platform not in ("telegram", "vk"):
                        raise web.HTTPBadRequest(text="канал задаётся только для telegram и vk")
                    target = str(target or "").strip()
                    if not target:
                        await factory.store.remove_channel(platform)
                    elif platform == "vk" and not target.lstrip("-").isdigit():
                        raise web.HTTPBadRequest(text="VK: числовой id, для группы со знаком минус")
                    else:
                        await factory.store.set_channel(platform, target)
            elif key == "team_remove":
                await self.db.remove_member(int(value))
            else:
                raise web.HTTPBadRequest(text=f"unknown setting {key}")
        log.info("settings changed by %s: %s", user["id"], list(body))
        return await self.settings_get(request)

    async def integration_set(self, request):
        """Ключи API: проверка живым запросом, сохранение в базе, применение без перезапуска. Только владелец."""
        user = await self.user(request)
        if not user.get("is_owner"):
            raise web.HTTPForbidden(text="только владелец")
        integ = self.deps.extra.get("integrations")
        if not integ:
            raise web.HTTPServiceUnavailable(text="недоступно")
        body = await request.json()
        key, value = body.get("key"), body.get("value")
        if value in (None, ""):
            await integ.unset(key)
        else:
            err = await integ.set(key, str(value))
            if err:
                raise web.HTTPBadRequest(text=err)
        return web.json_response({"integrations": await integ.status(), "summary": integ.summary()})

    async def rule_add(self, request):
        await self.user(request)
        expr = (await request.json()).get("expr", "")
        try:
            parse_rule(expr)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        await self.db.add_rule(expr)
        return web.json_response({"rules": [dict(r) for r in await self.db.rules()]})

    async def rule_del(self, request):
        await self.user(request)
        await self.db.remove_rule(int(request.match_info["id"]))
        return web.json_response({"rules": [dict(r) for r in await self.db.rules()]})

    async def keyword_add(self, request):
        await self.user(request)
        body = await request.json()
        if body.get("kind") not in ("topic", "intent", "negative") or not body.get("word"):
            raise web.HTTPBadRequest(text="kind/word")
        await self.db.add_keyword(body["kind"], body["word"], body.get("category"))
        return await self.settings_get(request)

    async def keyword_del(self, request):
        await self.user(request)
        await self.db.remove_keyword(request.match_info["word"])
        return await self.settings_get(request)

    async def ignored_del(self, request):
        await self.user(request)
        await self.db.remove_ignored(request.match_info["key"])
        return await self.settings_get(request)

    async def index(self, request):
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    # --- контент-фабрика ---
    @property
    def factory(self):
        f = self.deps.extra.get("factory")
        if not f:
            raise web.HTTPServiceUnavailable(text="контент-фабрика не инициализирована")
        return f

    def _content_row(self, r) -> dict:
        from ..content.factory import compose_text
        from ..content.store import media_json

        d = dict(r)
        media = media_json(r)
        store = self.factory.media
        d["images"] = [store.url(n) or f"/media/{n}" for n in media.get("images") or []]
        d["video"] = (store.url(media["video"]) or f"/media/{media['video']}") if media.get("video") else None
        d["text"] = compose_text(r)
        d["published"] = json.loads(r["published"] or "[]")
        return d

    async def content_list(self, request):
        await self.user(request)
        q = request.query
        rows = await self.factory.store.recent(min(int(q.get("limit", 50)), 200), status=q.get("status") or None,
                                               kind=q.get("kind") or None)
        return web.json_response({"items": [self._content_row(r) for r in rows if r["kind"] != "idea" or q.get("kind") == "idea"]})

    async def content_item(self, request):
        await self.user(request)
        row = await self.factory.store.get(int(request.match_info["id"]))
        if not row:
            raise web.HTTPNotFound()
        return web.json_response({"item": self._content_row(row)})

    async def content_generate(self, request):
        user = await self.user(request)
        body = await request.json()
        kind, topic = body.get("kind", "post"), (body.get("topic") or "").strip()
        if kind not in ("post", "carousel", "reel", "magnet") or not topic:
            raise web.HTTPBadRequest(text="нужны kind и topic")
        try:
            row = await self.factory.create(kind, topic, created_by=self._name(user))
        except Exception as e:
            raise web.HTTPBadRequest(text=str(e)[:300])
        return web.json_response({"item": self._content_row(row)})

    async def content_action(self, request):
        """action: publish | schedule (at: ISO) | draft | reject | edit (body/caption/hashtags)."""
        await self.user(request)
        item_id = int(request.match_info["id"])
        row = await self.factory.store.get(item_id)
        if not row:
            raise web.HTTPNotFound()
        body = await request.json()
        action = body.get("action")
        if action == "publish":
            pubs = await self.deps.extra["publishers"]() if self.deps.extra.get("publishers") else []
            if not pubs:
                raise web.HTTPBadRequest(text="площадки не настроены")
            out = await self.factory.publish(row, pubs)
            return web.json_response({"item": self._content_row(await self.factory.store.get(item_id)),
                                      "results": [{"platform": r.platform, "url": r.url} for r in out.results],
                                      "errors": out.errors})
        if action == "schedule":
            try:
                at = datetime.fromisoformat(body["at"])
            except (KeyError, ValueError, TypeError):
                raise web.HTTPBadRequest(text="нужно время at в ISO")
            if at.tzinfo is None:
                from zoneinfo import ZoneInfo
                at = at.replace(tzinfo=ZoneInfo(self.deps.tz))
            await self.factory.store.schedule(item_id, at)
        elif action == "draft":
            await self.factory.store.schedule(item_id, None)
        elif action == "reject":
            await self.factory.store.update(item_id, status="rejected")
        elif action == "edit":
            fields = {}
            for k in ("body", "caption"):
                if isinstance(body.get(k), str):
                    await self.factory.store.add_edit(item_id, row["kind"], row[k] or "", body[k])
                    fields[k] = body[k]
            if "hashtags" in body:
                fields["hashtags"] = body["hashtags"]
            await self.factory.store.update(item_id, **fields)
        else:
            raise web.HTTPBadRequest(text="bad action")
        return web.json_response({"item": self._content_row(await self.factory.store.get(item_id))})

    async def content_channels(self, request):
        await self.user(request)
        pubs = await self.deps.extra["publishers"]() if self.deps.extra.get("publishers") else []
        return web.json_response({"channels": [{"platform": p.platform, "label": p.label} for p in pubs]})

    # --- публичный лендинг лид-магнита: заявка превращается в горячий лид ---
    async def landing(self, request):
        row = await self.factory.store.by_slug(request.match_info["slug"])
        if not row or row["status"] == "rejected":
            raise web.HTTPNotFound(text="страница не найдена")
        from ..content.store import body_json
        return web.Response(text=render_landing(row, body_json(row), await self.factory.brand()), content_type="text/html")

    async def _too_many(self, request, row):
        headers = {"Retry-After": "600"}
        if "application/json" in request.headers.get("Accept", ""):
            return web.json_response({"error": "Слишком много заявок, попробуйте позже"}, status=429, headers=headers)
        return web.Response(text=render_notice("Слишком много заявок", "Вы отправили слишком много заявок подряд. "
                                               "Попробуйте через несколько минут.", await self.factory.brand()),
                            status=429, content_type="text/html", headers=headers)

    async def landing_submit(self, request):
        slug = request.match_info["slug"]
        row = await self.factory.store.by_slug(slug)
        if not row or row["status"] == "rejected":
            raise web.HTTPNotFound()
        if not self.limiter.allow(_client_ip(request), slug):
            return await self._too_many(request, row)
        form = await request.post()
        name = str(form.get("name", "")).strip()[:200]
        contact = str(form.get("contact", "")).strip()[:200]
        message = str(form.get("message", "")).strip()[:2000]
        if not contact or str(form.get("website", "")):  # website — скрытое поле-ловушка для ботов
            raise web.HTTPBadRequest(text="укажите контакт")
        n = await self.factory.store.add_landing_lead(slug, name, contact, message, None)
        pipeline = self.deps.extra.get("pipeline")
        if pipeline:
            text = (f"Заявка с лендинга «{row['title'] or row['topic']}»" + (f": {message}" if message else "")
                    + f"\nИмя: {name or '—'}. Хочет получить материал и консультацию.")
            await pipeline.handle(Lead(source="landing", external_id=f"{slug}:{n}", text=text, title=row["title"] or "",
                                       author=name, contact=contact, preset_score=9))
        return web.Response(text=render_thanks(row, await self.factory.brand()), content_type="text/html")


def build_app(api: MiniAppAPI) -> web.Application:
    app = web.Application()
    app.router.add_get("/", api.index)
    app.router.add_get("/app", api.index)
    app.router.add_get("/app/", api.index)
    app.router.add_static("/static/", STATIC, show_index=False)
    r = app.router
    r.add_get("/api/me", api.me)
    r.add_get("/api/leads", api.leads)
    r.add_get("/api/leads/{id:\\d+}", api.lead)
    r.add_post("/api/leads/{id:\\d+}/status", api.set_status)
    r.add_get("/api/stats", api.stats)
    r.add_get("/api/settings", api.settings_get)
    r.add_post("/api/settings", api.settings_set)
    r.add_post("/api/integrations", api.integration_set)
    r.add_post("/api/rules", api.rule_add)
    r.add_delete("/api/rules/{id:\\d+}", api.rule_del)
    r.add_post("/api/keywords", api.keyword_add)
    r.add_delete("/api/keywords/{word}", api.keyword_del)
    r.add_delete("/api/ignored/{key}", api.ignored_del)
    r.add_get("/api/content", api.content_list)
    r.add_post("/api/content", api.content_generate)
    r.add_get("/api/content/channels", api.content_channels)
    r.add_get(r"/api/content/{id:\d+}", api.content_item)
    r.add_post(r"/api/content/{id:\d+}", api.content_action)
    r.add_get("/lp/{slug}", api.landing)
    r.add_post("/lp/{slug}", api.landing_submit)
    if api.deps.extra.get("factory"):
        app.router.add_static("/media/", api.deps.extra["factory"].media.root, show_index=False)

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException as e:
            if e.status >= 400 and request.path.startswith("/api/"):
                return web.json_response({"error": e.text or e.reason}, status=e.status)
            raise
    app.middlewares.append(errors)
    return app


async def start_miniapp(s, db: Database, bot, deps) -> None:
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    api = MiniAppAPI(db, s.bot_token, deps.guard, deps, deps.sources, dev_user_id=None)
    deps.extra["threshold"] = s.score_threshold
    app = build_app(api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", s.miniapp_port)
    await site.start()
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=s.miniapp_url)))
    except Exception:
        log.exception("не удалось поставить кнопку меню Mini App")
    log.info("Mini App: http://0.0.0.0:%s → %s", s.miniapp_port, s.miniapp_url)
    try:
        while True:
            await __import__("asyncio").sleep(3600)
    finally:
        await runner.cleanup()
