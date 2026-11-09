import asyncio
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)


class CaptchaError(RuntimeError):
    """Площадка показала капчу — надо остановиться, а не долбить дальше."""


class NeedLoginError(RuntimeError):
    """Сессия протухла — нужен повторный вход через браузер."""


def in_work_hours(work_hours: str, tz_name: str) -> bool:
    """«8-23» — активно только днём: ночью живые люди не сидят на биржах, и антибот это знает."""
    try:
        start, end = (int(x) for x in work_hours.split("-"))
    except ValueError:
        return True
    hour = datetime.now(ZoneInfo(tz_name)).hour
    return start <= hour < end if start <= end else hour >= start or hour < end


async def human_pause(lo: float, hi: float) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


class Fetcher:
    """HTTP-клиент с TLS-отпечатком Chrome (curl_cffi) и паузами между запросами."""

    HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}

    def __init__(self, proxy: str | None = None, pause: tuple[float, float] = (3, 8)):
        self.proxy = proxy
        self.pause = pause
        self._first = True

    async def get(self, url: str, *, params: dict | None = None, headers: dict | None = None, timeout: int = 30):
        if not self._first:
            await human_pause(*self.pause)
        self._first = False
        async with AsyncSession(impersonate="chrome", proxy=self.proxy, timeout=timeout) as s:
            r = await s.get(url, params=params, headers={**self.HEADERS, **(headers or {})}, allow_redirects=True)
        final = str(r.url)
        if "showcaptcha" in final or "captcha" in final.split("?")[0]:
            raise CaptchaError(f"капча на {url.split('?')[0]}")
        if r.status_code in (200, 403, 503) and _is_challenge(r):
            raise CaptchaError(f"антибот-проверка на {url.split('?')[0]}")
        return r


# Маркеры страниц-заглушек DDoS-Guard / Cloudflare / Яндекса; настоящие страницы с ними маленькими не бывают
_CHALLENGE_MARKERS = ("ddos-guard", "cf-chl", "checking your browser", "smartcaptcha", "showcaptcha")


def _is_challenge(r) -> bool:
    try:
        body = r.text
    except Exception:
        return False
    if not body or len(body) > 100_000:
        return False
    low = body.lower()
    return any(m in low for m in _CHALLENGE_MARKERS)
