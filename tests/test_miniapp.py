import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from leadhunter.bot.guard import Guard
from leadhunter.bot.handlers import BotDeps
from leadhunter.bot.workspace import Workspace
from leadhunter.db import Database
from leadhunter.miniapp.auth import sign_init_data, validate_init_data
from leadhunter.miniapp.server import MiniAppAPI, build_app
from leadhunter.models import Lead
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Pipeline

TOKEN = "123456:TEST-TOKEN"
OWNER = 777


class FakeNotifier:
    async def notify(self, lead_id, lead, info):
        pass


@pytest.fixture
async def client(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    p = Pipeline(db, FakeNotifier(), default_threshold=5)
    await p.handle(Lead(source="kwork", external_id="1", title="Нужен сайт для салона", text="Бюджет 40 000 руб, срочно", intent_implied=True))
    await p.handle(Lead(source="telegram", external_id="2", text="Ищу разработчика телеграм бота для записи клиентов",
                        contact="@anna", extra={"chat_key": "c1", "chat_title": "Бизнес чат"}))
    guard = Guard(None, db, [OWNER])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), Workspace(None, db), None, "профиль")
    deps.extra["threshold"] = 5
    api = MiniAppAPI(db, TOKEN, guard, deps, [])
    async with TestClient(TestServer(build_app(api))) as c:
        yield c
    await db.close()


def headers(user_id=OWNER, name="Стас"):
    return {"X-Init-Data": sign_init_data({"auth_date": int(time.time()), "query_id": "q",
                                           "user": {"id": user_id, "first_name": name}}, TOKEN)}


def test_validate_init_data_roundtrip_and_tamper():
    data = sign_init_data({"auth_date": int(time.time()), "user": {"id": 1, "first_name": "A"}}, TOKEN)
    assert validate_init_data(data, TOKEN)["id"] == 1
    assert validate_init_data(data, "other:token") is None
    assert validate_init_data(data.replace("first_name", "last_name"), TOKEN) is None
    old = sign_init_data({"auth_date": int(time.time()) - 100_000, "user": {"id": 1}}, TOKEN)
    assert validate_init_data(old, TOKEN) is None


async def test_auth_required_and_team_only(client):
    assert (await client.get("/api/leads")).status == 401
    assert (await client.get("/api/leads", headers=headers(user_id=999))).status == 403
    r = await client.get("/api/me", headers=headers())
    assert r.status == 200 and (await r.json())["user"]["is_owner"] is True


async def test_leads_list_filters_and_detail(client):
    r = await client.get("/api/leads?status=open", headers=headers())
    leads = (await r.json())["leads"]
    assert len(leads) == 2 and leads[0]["id"] == 2
    r = await client.get("/api/leads?service=бот", headers=headers())
    assert [l["id"] for l in (await r.json())["leads"]] == [2]
    r = await client.get("/api/leads?q=салона", headers=headers())
    assert [l["id"] for l in (await r.json())["leads"]] == [1]
    r = await client.get("/api/leads/2", headers=headers())
    data = await r.json()
    assert data["lead"]["contact"] == "@anna" and data["events"][0]["status"] == "new"


async def test_status_flow_take_conflict_and_funnel(client):
    r = await client.post("/api/leads/1/status", json={"status": "taken"}, headers=headers())
    assert r.status == 200 and (await r.json())["lead"]["assigned_name"] == "Стас"
    # второй менеджер не может перехватить
    from leadhunter.db import Database  # noqa: F401
    r = await client.post("/api/leads/1/status", json={"status": "taken"}, headers=headers(user_id=OWNER))
    assert r.status == 200
    r = await client.post("/api/leads/1/status", json={"status": "contacted"}, headers=headers())
    assert (await r.json())["lead"]["status"] == "contacted"
    assert (await r.json())["lead"]["followup_at"] is not None
    r = await client.post("/api/leads/1/status", json={"status": "deal"}, headers=headers())
    assert (await r.json())["lead"]["status"] == "deal"
    assert (await client.post("/api/leads/1/status", json={"status": "bogus"}, headers=headers())).status == 400

    r = await client.get("/api/stats?days=7", headers=headers())
    s = await r.json()
    assert s["by_status"] == {"deal": 1, "new": 1}
    src = {row["k"]: row for row in s["funnel_source"]}
    assert src["kwork"]["deal"] == 1 and src["telegram"]["found"] == 1
    assert s["response"]["n"] == 1 and s["response"]["under5_pct"] == 100
    assert s["per_day"][0]["n"] == 2


async def test_settings_rules_keywords(client):
    r = await client.post("/api/settings", json={"threshold": 7, "geo": "Москва", "paused": True,
                                                  "phrases": ["нужен бот", "нужен бот", " "]}, headers=headers())
    s = await r.json()
    assert s["threshold"] == "7" and s["geo"] == "Москва" and s["paused"] is True and s["phrases"] == ["нужен бот"]
    assert (await client.post("/api/settings", json={"evil": 1}, headers=headers())).status == 400

    r = await client.post("/api/rules", json={"expr": "+сайт -курс"}, headers=headers())
    rules = (await r.json())["rules"]
    assert rules[0]["expr"] == "+сайт -курс"
    assert (await client.post("/api/rules", json={"expr": "   "}, headers=headers())).status == 400
    r = await client.delete(f"/api/rules/{rules[0]['id']}", headers=headers())
    assert (await r.json())["rules"] == []

    r = await client.post("/api/keywords", json={"kind": "topic", "word": "Квиз", "category": "сайт"}, headers=headers())
    assert (await r.json())["keywords"]["topic"]["квиз"] == "сайт"
    r = await client.delete("/api/keywords/квиз", headers=headers())
    assert "квиз" not in (await r.json())["keywords"]["topic"]


async def test_static_served(client):
    r = await client.get("/app")
    assert r.status == 200 and "LeadHunter" in await r.text()
    assert (await client.get("/static/app.js")).status == 200


async def test_settings_full_surface(client):
    """Ниша, свежесть, бренд, голос, автопилот, каналы, подключения — всё через один endpoint."""
    r = await client.get("/api/settings", headers=headers())
    s = await r.json()
    assert "niches" in s and "fresh" in s and "brand" in s and "integrations" in s
    r = await client.post("/api/settings", headers=headers(), json={
        "niche": "realtor", "fresh": {"board_max_age_hours": 2, "max_competitors": 8}, "voice": "на ты",
        "brand": {"name": "Пиксель", "handle": "@px", "primary": "#000000", "accent": "#ffffff"}, "autopilot": True, "pillars": "а; б"})
    s = await r.json()
    assert s["niche"] == "realtor" and "ищу квартиру" in s["phrases"] and s["fresh"] == {"board_max_age_hours": 2.0, "max_competitors": 8}
    assert s["brand"]["name"] == "Пиксель" and s["voice"] == "на ты" and s["autopilot"] is True and s["pillars"] == "а; б"
    assert (await client.post("/api/settings", headers=headers(), json={"brand": {"primary": "red"}})).status == 400
    assert (await client.post("/api/settings", headers=headers(), json={"niche": "nope"})).status == 400


# --- лендинг: лимиты и защита от спама ---

@pytest.fixture
async def landing(tmp_path):
    from leadhunter.content.render import Brand
    from leadhunter.content.store import ContentStore

    db = Database(str(tmp_path / "lp.db"))
    await db.connect()
    store = ContentStore(db)
    await store.init()
    slug_ids = {}
    for kind_slug in ("a", "b"):
        item_id = await store.add("magnet", kind_slug, title=f"Чек-лист {kind_slug}", body="{}")
        slug_ids[kind_slug] = item_id

    class Factory:
        media = type("M", (), {"root": str(tmp_path)})()

        async def brand(self):
            return Brand()

    Factory.store = store
    guard = Guard(None, db, [OWNER])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), Workspace(None, db), None, "профиль")
    deps.extra["factory"] = Factory()
    api = MiniAppAPI(db, TOKEN, guard, deps, [])
    slugs = {k: (await store.get(i))["slug"] for k, i in slug_ids.items()}
    async with TestClient(TestServer(build_app(api))) as c:
        c.slugs, c.store, c.api = slugs, store, api
        yield c
    await db.close()


def form(**kw):
    return {"name": "Оля", "contact": "@olya", **kw}


async def test_landing_ip_limit_5_per_10_min_via_forwarded_for(landing):
    slug = landing.slugs["a"]
    for _ in range(5):
        assert (await landing.post(f"/lp/{slug}", data=form(), headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"})).status == 200
    r = await landing.post(f"/lp/{slug}", data=form(), headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.status == 429 and "Слишком много" in await r.text()
    r = await landing.post(f"/lp/{slug}", data=form(), headers={"X-Forwarded-For": "1.2.3.4", "Accept": "application/json"})
    assert r.status == 429 and "error" in await r.json()
    # другой IP не задет
    assert (await landing.post(f"/lp/{slug}", data=form(), headers={"X-Forwarded-For": "5.6.7.8"})).status == 200


def test_submit_limiter_windows_and_slug_cap():
    from leadhunter.miniapp.server import SubmitLimiter

    lim = SubmitLimiter()
    assert all(lim.allow("ip", "s", now=t) for t in range(5))
    assert not lim.allow("ip", "s", now=6)
    assert lim.allow("ip", "s", now=601 + 4)  # окно 10 минут прошло
    lim = SubmitLimiter()
    assert all(lim.allow(f"ip{i}", "s", now=i) for i in range(30))
    assert not lim.allow("new-ip", "s", now=31)  # 30 в час на страницу
    assert lim.allow("new-ip", "other-slug", now=31)
    assert lim.allow("new-ip", "s", now=3601 + 30)


async def test_landing_rejected_status_blocks_post_and_get(landing):
    slug = landing.slugs["b"]
    item = await landing.store.by_slug(slug)
    await landing.store.update(item["id"], status="rejected")
    assert (await landing.get(f"/lp/{slug}")).status == 404
    assert (await landing.post(f"/lp/{slug}", data=form())).status == 404


async def test_landing_caps_lengths_and_keeps_honeypot(landing):
    slug = landing.slugs["a"]
    r = await landing.post(f"/lp/{slug}", data=form(name="н" * 500, contact="@" + "k" * 500, message="м" * 5000))
    assert r.status == 200
    cur = await landing.store.conn.execute("SELECT name, contact, message FROM landing_leads")
    row = await cur.fetchone()
    assert (len(row["name"]), len(row["contact"]), len(row["message"])) == (200, 200, 2000)
    assert (await landing.post(f"/lp/{slug}", data=form(website="spam"))).status == 400


async def test_init_data_only_from_header_not_query(client):
    h = headers()["X-Init-Data"]
    assert (await client.get("/api/me", params={"initData": h})).status == 401
    assert (await client.get("/api/me", headers=headers())).status == 200
