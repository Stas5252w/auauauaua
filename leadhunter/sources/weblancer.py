import json
import logging
import re
from datetime import datetime, timezone

from ..http import CaptchaError, Fetcher, NeedLoginError
from ..models import Lead
from .base import Source, strip_html

log = logging.getLogger(__name__)
BASE = "https://www.weblancer.net"
CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
REF_RE = re.compile(r"^\$[0-9a-f]+$")


def parse_weblancer_html(body: str) -> list[Lead]:
    """Weblancer на Next.js: список заказов лежит в RSC-payload, разбитом на self.__next_f.push(...)."""
    payload = "".join(json.loads('"' + c + '"') for c in CHUNK_RE.findall(body))
    k = payload.find('"jobs":[')
    if k < 0:
        return []
    try:
        jobs, _ = json.JSONDecoder().raw_decode(payload[k + len('"jobs":'):])
    except json.JSONDecodeError:
        return []
    leads = []
    for j in jobs:
        if not j.get("id") or j.get("status") not in (None, "opened"):
            continue
        # Длинные описания RSC выносит в отдельный чанк и оставляет ссылку вида "$3f" — берём сниппет
        desc = j.get("description") or ""
        if REF_RE.match(desc):
            desc = j.get("snippet") or j.get("seo_snippet") or j.get("localizedSnippet") or ""
        text = strip_html(desc)
        if j.get("budget") and str(j["budget"]) not in ("0", "None"):
            text += f"\nБюджет: {j['budget']} $"
        ts = j.get("bidding_time")
        found = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts and str(ts).isdigit() else datetime.now(timezone.utc)
        user = j.get("user") if isinstance(j.get("user"), dict) else {}
        leads.append(Lead(
            source="weblancer",
            external_id=str(j["id"]),
            title=(j.get("name") or "").strip(),
            text=text,
            url=BASE + j["url"] if j.get("url") else "",
            author=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])),
            found_at=found,
            intent_implied=True,
            competitors=int(j["total_bids_count"]) if str(j.get("total_bids_count", "")).isdigit() else None,
        ))
    return leads


class WeblancerSource(Source):
    name = "weblancer"
    human = True

    def __init__(self, urls: list[str], interval: int, proxy: str | None):
        self.urls = urls
        self.interval = interval
        self.fetcher = Fetcher(proxy, pause=(4, 10))

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        error: Exception | None = None
        for url in self.urls:
            try:
                r = await self.fetcher.get(url)
                r.raise_for_status()
                leads += parse_weblancer_html(r.text)
            except (CaptchaError, NeedLoginError):
                raise
            except Exception as e:
                log.warning("weblancer %s: %s", url, e)
                error = e
        if error and not leads:
            raise error
        return leads
