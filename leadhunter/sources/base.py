import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from email.utils import parsedate_to_datetime

from ..http import CaptchaError, NeedLoginError, in_work_hours
from ..models import Lead

Emit = Callable[..., Awaitable[bool]]
Alert = Callable[[str], Awaitable[None]]
log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"[ \t]+", " ", text).strip()


def watch(task: asyncio.Task) -> asyncio.Task:
    """Фоновая задача не должна умирать молча — исключение пишем в лог."""
    def _done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.error("фоновая задача %s упала", t.get_name(), exc_info=t.exception())
    task.add_done_callback(_done)
    return task


def _http_status(e: Exception) -> tuple[int | None, float | None]:
    """HTTP-статус и Retry-After (секунды) из исключения raise_for_status(), если они есть."""
    r = getattr(e, "response", None)
    status = getattr(r, "status_code", None)
    if status != 429 or r is None:
        return status, None
    raw = (getattr(r, "headers", None) or {}).get("Retry-After")
    try:
        return status, float(raw)
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(raw)
        return status, max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())
    except (TypeError, ValueError):
        return status, None


class Source:
    name: str = "base"
    interval: int = 600
    # Источник ходит по сайтам «как человек»: только в рабочие часы, с паузами
    human: bool = False
    work_hours: str = "0-24"
    tz: str = "Europe/Moscow"
    alert: Alert | None = None
    # Состояние для /sources: когда последний раз получилось и что сломалось
    last_ok: float | None = None
    last_error: str | None = None
    found_total: int = 0
    burst_limit: int = 5  # максимум уведомлений за один проход источника

    async def poll(self) -> list[Lead]:
        raise NotImplementedError

    async def run(self, emit: Emit) -> None:
        """По умолчанию — опрос раз в interval секунд с бэкоффом при ошибках. Realtime-источники переопределяют run()."""
        failures = 0
        # Разводим старт источников, чтобы они не били по сети одновременно
        await asyncio.sleep(random.uniform(0, min(60, self.interval)))
        while True:
            if self.human and not in_work_hours(self.work_hours, self.tz):
                await asyncio.sleep(600)
                continue
            delay = None
            try:
                leads = await self.poll()
                # Первый проход после запуска: страница биржи — это десятки старых заказов. Шлём не больше
                # burst_limit самых свежих, остальные уходят в базу молча (есть в /leads и кабинете).
                leads.sort(key=lambda l: l.found_at, reverse=True)
                found = 0
                for lead in leads:
                    if await emit(lead, quiet=found >= self.burst_limit):
                        found += 1
                self.found_total += found
                self.last_ok, self.last_error, failures = time.time(), None, 0
                log.info("%s: %d записей, %d новых лидов", self.name, len(leads), found)
            except (CaptchaError, NeedLoginError) as e:
                failures += 1
                self.last_error = str(e)[:200]
                log.warning("%s: %s", self.name, e)
                await self._alert(f"⚠️ {self.name}: {e}. Пауза {self._backoff(failures) // 60} мин.")
            except Exception as e:
                failures += 1
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.exception("%s: ошибка опроса", self.name)
                status, retry_after = _http_status(e)
                if status in (403, 429):
                    # Блокировка/лимит — не ждём третьего раза, предупреждаем сразу (один раз за серию)
                    if failures == 1:
                        await self._alert(f"⚠️ {self.name}: HTTP {status}, площадка ограничивает запросы. Пауза.")
                    if retry_after:
                        delay = min(retry_after, 3600)
                elif failures == 3:
                    await self._alert(f"⚠️ {self.name} не работает третий раз подряд: {self.last_error}")
            if delay is None:
                delay = self._backoff(failures) if failures else self.interval
            await asyncio.sleep(delay * random.uniform(0.85, 1.15))  # ±15%, чтобы не бить по сети в такт

    def _backoff(self, failures: int) -> int:
        return min(self.interval * (2 ** min(failures, 4)), 6 * 3600)

    async def _alert(self, text: str) -> None:
        if self.alert:
            try:
                await self.alert(text)
            except Exception:
                log.exception("alert failed")

    def status_line(self) -> str:
        if self.last_error:
            state = f"⚠️ {self.last_error}"
        elif self.last_ok:
            state = f"✅ {int((time.time() - self.last_ok) / 60)} мин назад"
        else:
            state = "⏳ ещё не опрашивался"
        return f"{self.name} — каждые {self.interval // 60} мин, лидов: {self.found_total}, {state}"
