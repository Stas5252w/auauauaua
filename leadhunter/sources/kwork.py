import html
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..http import CaptchaError, Fetcher, NeedLoginError
from ..models import Lead
from .base import Source, strip_html

log = logging.getLogger(__name__)
URL = "https://kwork.ru/projects"
MSK = ZoneInfo("Europe/Moscow")


def parse_kwork_html(body: str) -> list[Lead]:
    """Биржа запросов Kwork: список лежит в window.stateData.wantsListData.pagination.data."""
    i = body.find("window.stateData")
    if i < 0:
        return []
    data, _ = json.JSONDecoder().raw_decode(body[body.find("{", i):])
    wants = ((data.get("wantsListData") or {}).get("pagination") or {}).get("data") or []
    leads = []
    for w in wants:
        if not w.get("id") or not w.get("isWantActive", True):
            continue
        text = strip_html(html.unescape(w.get("description") or "")).replace("\r", "")
        price = w.get("priceLimit") or w.get("possiblePriceLimit")
        if price:
            text += f"\nБюджет: до {str(price).split('.')[0]} ₽"
        found = datetime.now(timezone.utc)
        if w.get("date_create"):
            try:
                found = datetime.strptime(w["date_create"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
            except ValueError:
                pass
        leads.append(Lead(
            source="kwork",
            external_id=str(w["id"]),
            title=html.unescape(w.get("name") or ""),
            text=text,
            url=f"{URL}/{w['id']}",
            author=(w.get("user") or {}).get("username") or "",
            contact=w.get("wantUserGetProfileUrl") or "",
            found_at=found,
            intent_implied=True,
            competitors=int(w["kwork_count"]) if str(w.get("kwork_count", "")).isdigit() else None,
        ))
    return leads


class KworkSource(Source):
    """Kwork «Запросы» — покупатели сами описывают, что им нужно. Без логина, 12 запросов на страницу."""

    name = "kwork"
    human = True

    def __init__(self, categories: list[str], pages: int, interval: int, proxy: str | None):
        self.categories = categories
        self.pages = pages
        self.interval = interval
        self.fetcher = Fetcher(proxy, pause=(4, 10))

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        error: Exception | None = None
        for cat in self.categories:
            for page in range(1, self.pages + 1):
                params = {"page": page}
                if cat:
                    params["c"] = cat
                try:
                    r = await self.fetcher.get(URL, params=params)
                    r.raise_for_status()
                    batch = parse_kwork_html(r.text)
                except (CaptchaError, NeedLoginError):
                    raise
                except Exception as e:
                    # Упала страница — отдаём собранное, а не теряем всё
                    log.warning("kwork cat=%r page=%d: %s", cat, page, e)
                    error = e
                    break
                leads += batch
                if len(batch) < 12:
                    break
        if error and not leads:
            raise error
        return leads
