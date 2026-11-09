import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .db import Database
from .dedup import NearDuplicateIndex
from .extract import extract_budget, extract_contacts
from .filters import evaluate
from .freshness import Freshness, check as freshness_check, is_board
from .geo import geo_allows
from .llm import LLMClassifier
from .models import Lead
from .scoring import ScoreResult, score_text

log = logging.getLogger(__name__)


@dataclass
class Enriched:
    score: int
    res: ScoreResult
    summary: str | None = None
    service: str = ""
    budget: str = ""
    urgency: str = ""
    draft: str = ""
    rule: str = ""
    author_bio: str = ""
    freshness: str = ""  # «12 мин назад · 2 откл.» для бирж


class Notifier(Protocol):
    async def notify(self, lead_id: int, lead: Lead, info: Enriched) -> None: ...


class Pipeline:
    """Дедуп → игнор-листы → спам-детект → свежесть → гео → правила → контакты → словарь → ИИ → сохранение → уведомление."""

    def __init__(self, db: Database, notifier: Notifier, default_threshold: int, llm: LLMClassifier | None = None,
                 *, max_age_hours: int = 48, llm_min_score: int = 2, profile: str = "", spam_chats: int = 3):
        self.db = db
        self.notifier = notifier
        self.default_threshold = default_threshold
        self.llm = llm
        self.max_age = timedelta(hours=max_age_hours)
        self.llm_min_score = llm_min_score
        self.default_profile = profile
        self.spam_chats = spam_chats
        self.near_dups = NearDuplicateIndex()
        # Необязательный хук: по sender_id Telegram вернуть bio автора — идёт в ИИ и карточку
        self.author_bio: Callable[[int], Awaitable[str]] | None = None

    async def threshold(self) -> int:
        return int(await self.db.get_setting("threshold", str(self.default_threshold)))

    async def _is_ignored(self, lead: Lead) -> bool:
        ignored = await self.db.get_ignored()
        chat_key = str(lead.extra.get("chat_key", "")).lower()
        if chat_key and chat_key in ignored["chat"]:
            return True
        author_keys = {lead.author.lower(), lead.contact.lower()} - {""}
        return bool(author_keys & ignored["author"].keys())

    async def _is_spammer(self, lead: Lead) -> bool:
        # Один и тот же автор в нескольких чатах за час — это рассылка исполнителя, а не запрос клиента
        chat_key = lead.extra.get("chat_key")
        if not chat_key or not lead.contact or self.spam_chats <= 0:
            return False
        n = await self.db.author_seen(lead.contact, chat_key)
        if n >= self.spam_chats:
            await self.db.add_ignored("author", lead.contact, f"авто: {n} чатов за час")
            log.info("spammer %s: %d чатов за час", lead.contact, n)
            return True
        return False

    async def handle(self, lead: Lead, *, quiet: bool = False) -> bool:
        """quiet — сохранить без уведомления (лимит пачки у источника)."""
        if not await self.db.mark_seen(lead.source, lead.external_id):
            return False
        if len(lead.full_text) < 15:
            return False
        # Спам-детект раньше дедупа по хэшу: одинаковая рассылка в разных чатах должна считаться как активность автора
        if await self._is_ignored(lead) or await self._is_spammer(lead):
            return False
        if not await self.db.mark_hash_seen(lead.full_text):
            return False
        found = lead.found_at if lead.found_at.tzinfo else lead.found_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - found > self.max_age:
            return False
        geo = await self.db.get_setting("geo", "")
        if geo and not geo_allows(lead.full_text, [c.strip() for c in geo.split(",") if c.strip()]):
            return False

        rule, blocked = evaluate(lead.full_text, [r["expr"] for r in await self.db.rules()])
        if blocked:
            return False
        # Биржи: заказ старше N часов или с толпой откликов уже не забрать — не шлём
        fresh = Freshness.from_settings({"board_max_age_hours": await self.db.get_setting("board_max_age_hours"),
                                         "max_competitors": await self.db.get_setting("max_competitors")})
        ok, bonus, mark = freshness_check(lead, fresh)
        if not ok:
            log.debug("%s/%s пропущен: %s", lead.source, lead.external_id, mark)
            return False
        if not lead.contact:
            lead.contact = extract_contacts(lead.text)

        info = await self._enrich(lead, rule)
        if info is None:
            return False
        info.freshness = mark or ("дата неизвестна" if lead.extra.get("undated") else "")
        if bonus and lead.preset_score is None:
            info.score = min(10, info.score + bonus)
        if info.score < await self.threshold():
            return False
        if self.near_dups.is_duplicate(lead.full_text):
            return False

        lead_id = await self.db.save_lead(lead, info.score, info.res, summary=info.summary, service=info.service,
                                          budget=info.budget, urgency=info.urgency, draft=info.draft,
                                          rule=info.rule, author_bio=info.author_bio, freshness=info.freshness)
        if not quiet and await self.db.get_setting("paused") != "1":
            await self.notifier.notify(lead_id, lead, info)
        return True

    async def _enrich(self, lead: Lead, rule: str | None) -> Enriched | None:
        kw = await self.db.get_keywords()
        res = score_text(lead.full_text, kw, intent_implied=lead.intent_implied)
        service = res.services[0] if res.services else ""
        budget = extract_budget(lead.full_text)
        threshold = await self.threshold()

        if lead.preset_score is not None:
            return Enriched(lead.preset_score, res, service=service or "сайт", budget=budget)

        base = max(res.score, threshold) if rule else res.score
        if not self.llm:
            return Enriched(base, res, service=service, budget=budget, rule=rule or "",
                            draft=await self._template(service))

        # ИИ смотрит всё, где есть слово по теме, сработало правило, либо есть явная фраза-намерение без темы
        # («ищу исполнителя…» → словарный балл 0, но запрос может быть настоящим)
        if res.score < self.llm_min_score and not rule:
            if not (res.strong_intent and not res.topic and not res.negative):
                return None
        bio = ""
        if self.author_bio and lead.source == "telegram" and lead.extra.get("sender_id"):
            try:
                bio = await self.author_bio(int(lead.extra["sender_id"]))
            except Exception:
                log.debug("bio fetch failed", exc_info=True)
        profile = await self.db.get_setting("profile", self.default_profile) or self.default_profile
        good = [r["text"] for r in await self.db.leads_by_status("hot", 5)]
        bad = [r["text"] for r in await self.db.leads_by_status("spam", 5)]
        verdict = await self.llm.classify(lead.full_text, profile, good, bad, source=lead.source,
                                          is_board=is_board(lead), bio=bio)
        if verdict is None:
            return Enriched(base, res, service=service, budget=budget, rule=rule or "", author_bio=bio,
                            draft=await self._template(service))
        if not verdict.is_lead:
            log.info("LLM отклонил %s/%s: %s", lead.source, lead.external_id, verdict.summary)
            return None
        return Enriched(verdict.score, res, summary=verdict.summary, service=verdict.service or service,
                        budget=verdict.budget or budget, urgency=verdict.urgency, draft=verdict.reply_draft,
                        rule=rule or "", author_bio=bio)

    async def _template(self, service: str) -> str:
        return (await self.db.get_template(service) if service else None) or ""
