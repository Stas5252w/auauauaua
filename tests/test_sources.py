from leadhunter.sources.hh import parse_vacancies
from leadhunter.sources.instagram import parse_media
from leadhunter.sources.rss_boards import parse_feed
from leadhunter.sources.threads import parse_threads
from leadhunter.sources.vk import parse_posts
from leadhunter.sources.yandex_maps import parse_features

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>FL</title>
<item><title><![CDATA[Нужен Telegram-бот для записи (Бюджет: 20 000 ₽)]]></title>
<link>https://www.fl.ru/projects/1/bot.html</link>
<description><![CDATA[<p>Бюджет: 20 000.</p>Нужен <b>бот</b> для записи клиентов]]></description>
<pubDate>Mon, 09 Nov 2026 11:20:09 GMT</pubDate></item>
<item><title>Без ссылки</title></item>
</channel></rss>""".encode()


def test_parse_rss_feed():
    leads = parse_feed(RSS, "https://www.fl.ru/rss/all.xml")
    assert len(leads) == 1
    lead = leads[0]
    assert lead.source == "rss:fl.ru"
    assert lead.external_id == "https://www.fl.ru/projects/1/bot.html"
    assert lead.text == "Бюджет: 20 000. Нужен бот для записи клиентов"
    assert lead.intent_implied and lead.found_at.year == 2026


def test_parse_hh_vacancies():
    data = {"items": [{
        "id": "123", "name": "Разработчик Telegram-бота (проект)", "alternate_url": "https://hh.ru/vacancy/123",
        "employer": {"name": "ООО Ромашка"}, "published_at": "2026-11-09T10:00:00+0300",
        "snippet": {"requirement": "Опыт с <highlighttext>aiogram</highlighttext>", "responsibility": None},
    }]}
    (lead,) = parse_vacancies(data)
    assert lead.source == "hh.ru" and lead.external_id == "123"
    assert lead.text == "Опыт с aiogram" and lead.author == "ООО Ромашка"


def test_parse_yandex_skips_businesses_with_site():
    data = {"features": [
        {"properties": {"CompanyMetaData": {"id": "1", "name": "С сайтом", "url": "https://x.ru"}}},
        {"properties": {"CompanyMetaData": {"id": "2", "name": "Без сайта", "address": "Москва, Тверская 1",
                                            "Phones": [{"formatted": "+7 (495) 000-00-00"}],
                                            "Categories": [{"name": "Кофейня"}]}}},
    ]}
    (lead,) = parse_features(data, "кофейня", "Москва")
    assert lead.external_id == "2" and lead.preset_score == 6
    assert lead.contact == "+7 (495) 000-00-00" and "Кофейня" in lead.text
    assert lead.url == "https://yandex.ru/maps/org/2"


def test_parse_vk_posts():
    data = {"response": {"items": [
        {"id": 5, "owner_id": 100, "text": "Нужен сайт", "date": 1_700_000_000},
        {"id": 6, "owner_id": -200, "text": "Группа ищет бота", "date": 1_700_000_000},
        {"id": 7, "owner_id": 100, "text": ""},
    ]}}
    leads = parse_posts(data)
    assert [l.external_id for l in leads] == ["100_5", "-200_6"]
    assert leads[0].contact == "https://vk.com/id100"
    assert leads[1].contact == "https://vk.com/club200"
    assert leads[0].url == "https://vk.com/wall100_5"


def test_parse_threads_skips_replies():
    data = {"data": [
        {"id": "1", "text": "Нужен сайт для салона", "permalink": "https://www.threads.net/@anna/post/x",
         "timestamp": "2026-11-09T05:42:03+0000", "username": "anna", "is_reply": False},
        {"id": "2", "text": "ответ", "username": "bob", "is_reply": True},
    ]}
    (lead,) = parse_threads(data)
    assert lead.source == "threads" and lead.external_id == "1"
    assert lead.author == "@anna" and lead.contact == "https://www.threads.net/@anna"
    assert lead.found_at.year == 2026 and lead.found_at.tzinfo is not None


def test_parse_instagram_media():
    data = {"data": [
        {"id": "9", "caption": "Ищу кто сделает сайт #нуженсайт", "permalink": "https://www.instagram.com/p/abc/",
         "timestamp": "2026-11-09T05:42:03+0000"},
        {"id": "10", "permalink": "https://www.instagram.com/p/def/"},
    ]}
    (lead,) = parse_media(data, "нуженсайт")
    assert lead.source == "instagram" and lead.title == "#нуженсайт" and lead.external_id == "9"


class _Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


async def test_kwork_poll_keeps_pages_before_failure():
    from leadhunter.sources.kwork import KworkSource

    page = '<script>window.stateData={"wantsListData":{"pagination":{"data":[%s]}}}</script>' % ",".join(
        '{"id": %d, "name": "Нужен бот", "description": "текст"}' % i for i in range(1, 13))

    class F:
        n = 0

        async def get(self, url, params=None):
            self.n += 1
            return _Resp(page) if self.n == 1 else _Resp(status=500)

    src = KworkSource([""], 3, 600, None)
    src.fetcher = F()
    assert len(await src.poll()) == 12


async def test_kwork_poll_raises_when_nothing_collected():
    import pytest
    from leadhunter.sources.kwork import KworkSource

    class F:
        async def get(self, url, params=None):
            return _Resp(status=500)

    src = KworkSource([""], 2, 600, None)
    src.fetcher = F()
    with pytest.raises(RuntimeError):
        await src.poll()


async def test_vk_poll_keeps_results_when_one_query_fails(monkeypatch):
    import httpx
    from types import SimpleNamespace
    from leadhunter.sources import vk
    from leadhunter.sources.vk import VKSource

    class Phrases:
        async def get(self):
            return ["нужен сайт", "нужен бот"]

    def handler(req):
        if "бот" in req.url.params["q"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"response": {"items": [{"owner_id": 1, "id": 2, "text": "Нужен сайт", "date": 1}]}})

    real = httpx.AsyncClient
    monkeypatch.setattr(vk.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    src = VKSource(SimpleNamespace(vk_token="t", vk_interval=60), Phrases())
    before = src.since
    assert len(await src.poll()) == 1
    assert src.since == before  # окно не сдвинулось — упавший запрос повторится
