import logging
import time
from datetime import datetime, timezone

import httpx

from ..config import Settings
from ..models import Lead
from ..phrases import PhraseProvider
from .base import Source

log = logging.getLogger(__name__)
API = "https://api.vk.com/method/newsfeed.search"


def parse_posts(data: dict) -> list[Lead]:
    leads = []
    for p in (data.get("response") or {}).get("items", []):
        owner, pid = p.get("owner_id"), p.get("id")
        if owner is None or pid is None or not p.get("text"):
            continue
        profile = f"https://vk.com/id{owner}" if owner > 0 else f"https://vk.com/club{-owner}"
        leads.append(Lead(
            source="vk",
            external_id=f"{owner}_{pid}",
            text=p["text"],
            url=f"https://vk.com/wall{owner}_{pid}",
            author=profile,
            contact=profile,
            found_at=datetime.fromtimestamp(p.get("date", time.time()), tz=timezone.utc),
        ))
    return leads


class VKSource(Source):
    name = "vk"

    def __init__(self, s: Settings, phrases: PhraseProvider):
        self.token = s.vk_token
        self.phrases = phrases
        self.interval = s.vk_interval
        self.since = int(time.time()) - 24 * 3600

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        error: Exception | None = None
        now = int(time.time())
        async with httpx.AsyncClient(timeout=30) as c:
            for q in await self.phrases.get():
                try:
                    r = await c.get(API, params={"q": q, "count": 200, "start_time": self.since,
                                                 "access_token": self.token, "v": "5.199"})
                    r.raise_for_status()
                    data = r.json()
                    if "error" in data:
                        raise RuntimeError(f"VK API: {data['error'].get('error_msg')}")
                    leads += parse_posts(data)
                except Exception as e:
                    log.warning("vk %r: %s", q, e)
                    error = e
        if error and not leads:
            raise error
        if not error:  # при сбое окно не двигаем — упавший запрос повторится со старым since
            self.since = now - 300  # небольшой нахлёст, дедуп отсеет повторы
        return leads
