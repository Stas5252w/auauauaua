import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone

import aiosqlite

from .models import Lead
from .scoring import DEFAULT_INTENT, DEFAULT_NEGATIVE, DEFAULT_TOPIC, Keywords, ScoreResult, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL, external_id TEXT NOT NULL, seen_at TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);
CREATE TABLE IF NOT EXISTS seen_hashes (hash TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT, title TEXT, text TEXT,
    author TEXT, contact TEXT, score INTEGER NOT NULL, matched TEXT, summary TEXT,
    service TEXT, budget TEXT, urgency TEXT, draft TEXT, chat_key TEXT, chat_title TEXT,
    status TEXT NOT NULL DEFAULT 'new', found_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_created ON leads(created_at);
CREATE TABLE IF NOT EXISTS keywords (
    kind TEXT NOT NULL, word TEXT NOT NULL, category TEXT, PRIMARY KEY (kind, word)
);
CREATE TABLE IF NOT EXISTS ignored (kind TEXT NOT NULL, key TEXT NOT NULL, label TEXT, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lead_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, status TEXT NOT NULL,
    user_id INTEGER, user_name TEXT, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lead_events_lead ON lead_events(lead_id);
CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY AUTOINCREMENT, expr TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_stats (
    chat_key TEXT PRIMARY KEY, title TEXT, messages INTEGER NOT NULL DEFAULT 0, leads INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS author_events (author TEXT NOT NULL, chat_key TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS author_events_author ON author_events(author, at);
CREATE TABLE IF NOT EXISTS templates (service TEXT PRIMARY KEY, text TEXT NOT NULL);
"""

# Колонки, добавленные после первой версии: для существующих баз делаем ALTER TABLE
LEAD_COLUMNS = {
    "assigned_to": "INTEGER", "assigned_name": "TEXT", "assigned_at": "TEXT",
    "msg_chat_id": "INTEGER", "msg_id": "INTEGER", "thread_id": "INTEGER",
    "sla_pinged": "INTEGER NOT NULL DEFAULT 0", "followup_at": "TEXT", "followup_count": "INTEGER NOT NULL DEFAULT 0",
    "phrase": "TEXT", "rule": "TEXT", "author_bio": "TEXT", "freshness": "TEXT", "competitors": "INTEGER",
}
OPEN_STATUSES = ("new", "taken", "contacted", "replied")


def content_hash(text: str) -> str:
    # Один и тот же запрос часто постят в десяток чатов, меняя ссылки/эмодзи — сравниваем только буквы и цифры
    core = re.sub(r"[^\w]+", "", re.sub(r"https?://\S+", "", normalize(text)))
    return hashlib.sha1(core.encode()).hexdigest()


def _csv_safe(v):
    # Ячейка с = + - @ в Excel исполняется как формула — экранируем апострофом
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() не вызван"
        return self._conn

    async def connect(self) -> None:
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # WAL — читатели (кабинет, бот) не блокируют запись; busy_timeout — ждём, а не падаем с "database is locked"
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._seed_keywords()

    async def _migrate(self) -> None:
        """Версия схемы в PRAGMA user_version. Новый шаг — новая функция в конце списка."""
        steps = [self._migrate_v1, self._migrate_v2]
        cur = await self.conn.execute("PRAGMA user_version")
        version = (await cur.fetchone())[0]
        for i in range(version, len(steps)):
            await steps[i]()
            await self.conn.execute(f"PRAGMA user_version = {i + 1}")
        await self.conn.commit()

    async def _migrate_v1(self) -> None:
        # Базовая схема: недостающие колонки leads для баз старых версий
        cur = await self.conn.execute("PRAGMA table_info(leads)")
        existing = {r[1] for r in await cur.fetchall()}
        for col, typ in LEAD_COLUMNS.items():
            if col not in existing:
                await self.conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {typ}")

    async def _migrate_v2(self) -> None:
        # Словарь переработан: в старых базах досеваем новые слова и убираем негативы,
        # штрафовавшие заказчиков («сайт для онлайн-курса»). Пользовательские слова не трогаем.
        for word in ("курс", "обучени", "вебинар"):
            await self.conn.execute("DELETE FROM keywords WHERE kind = 'negative' AND word = ?", (word,))
        await self.conn.executemany("INSERT OR IGNORE INTO keywords VALUES (?, ?, ?)", self._default_keyword_rows())

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @staticmethod
    def _default_keyword_rows() -> list[tuple[str, str, str | None]]:
        rows: list[tuple[str, str, str | None]] = [("topic", w, cat) for w, cat in DEFAULT_TOPIC.items()]
        rows += [("intent", w, None) for w in DEFAULT_INTENT] + [("negative", w, None) for w in DEFAULT_NEGATIVE]
        return rows

    async def _seed_keywords(self) -> None:
        cur = await self.conn.execute("SELECT COUNT(*) FROM keywords")
        if (await cur.fetchone())[0]:
            return
        await self.conn.executemany("INSERT OR IGNORE INTO keywords VALUES (?, ?, ?)", self._default_keyword_rows())
        await self.conn.commit()

    # --- дедуп ---
    async def mark_seen(self, source: str, external_id: str) -> bool:
        """True, если запись новая."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (source, external_id, _now())
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def mark_hash_seen(self, text: str) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO seen_hashes VALUES (?, ?)", (content_hash(text), _now())
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def purge_old(self, days: int) -> int:
        """Чистит дедуп-таблицы старше days дней: иначе они растут вечно, а такой же запрос через полгода терялся."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        total = 0
        for table, col in (("seen", "seen_at"), ("seen_hashes", "seen_at"), ("author_events", "at")):
            cur = await self.conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
            total += cur.rowcount
        await self.conn.commit()
        return total

    # --- лиды ---
    async def save_lead(self, lead: Lead, score: int, res: ScoreResult, *, summary: str | None = None,
                        service: str = "", budget: str = "", urgency: str = "", draft: str = "",
                        rule: str = "", author_bio: str = "", freshness: str = "") -> int:
        matched = json.dumps({"topic": res.topic, "intent": res.intent, "negative": res.negative}, ensure_ascii=False)
        cur = await self.conn.execute(
            "INSERT INTO leads (source, external_id, url, title, text, author, contact, score, matched, summary,"
            " service, budget, urgency, draft, chat_key, chat_title, phrase, rule, author_bio, freshness, competitors, found_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lead.source, lead.external_id, lead.url, lead.title, lead.text, lead.author, lead.contact,
             score, matched, summary, service, budget, urgency, draft,
             lead.extra.get("chat_key"), lead.extra.get("chat_title"), lead.extra.get("phrase"), rule, author_bio,
             freshness, lead.competitors, lead.found_at.isoformat(), _now()),
        )
        await self.conn.commit()
        lead_id = cur.lastrowid
        await self.add_event(lead_id, "new")
        if lead.extra.get("chat_key"):
            await self.bump_chat_stats(lead.extra["chat_key"], lead.extra.get("chat_title", ""), leads=1)
        return lead_id

    async def get_lead(self, lead_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
        return await cur.fetchone()

    async def set_status(self, lead_id: int, status: str, user_id: int | None = None, user_name: str = "") -> None:
        await self.conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
        await self.conn.commit()
        await self.add_event(lead_id, status, user_id, user_name)

    async def add_event(self, lead_id: int, status: str, user_id: int | None = None, user_name: str = "") -> None:
        await self.conn.execute(
            "INSERT INTO lead_events (lead_id, status, user_id, user_name, at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, status, user_id, user_name, _now()),
        )
        await self.conn.commit()

    async def assign(self, lead_id: int, user_id: int, name: str) -> bool:
        """Закрепить лида за менеджером. False — уже взят кем-то другим."""
        row = await self.get_lead(lead_id)
        if row is None:
            return False
        if row["assigned_to"] == user_id:
            return True
        cur = await self.conn.execute(
            "UPDATE leads SET assigned_to = ?, assigned_name = ?, assigned_at = ?, status = 'taken'"
            " WHERE id = ? AND assigned_to IS NULL",
            (user_id, name, _now(), lead_id),
        )
        await self.conn.commit()
        if cur.rowcount:
            await self.add_event(lead_id, "taken", user_id, name)
        return cur.rowcount == 1

    async def set_lead_message(self, lead_id: int, chat_id: int, msg_id: int, thread_id: int | None) -> None:
        await self.conn.execute("UPDATE leads SET msg_chat_id = ?, msg_id = ?, thread_id = ? WHERE id = ?",
                                (chat_id, msg_id, thread_id, lead_id))
        await self.conn.commit()

    async def sla_overdue(self, minutes: int, min_score: int) -> list[aiosqlite.Row]:
        before = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE status = 'new' AND sla_pinged = 0 AND score >= ? AND created_at <= ?",
            (min_score, before),
        )
        return list(await cur.fetchall())

    async def mark_sla_pinged(self, lead_id: int) -> None:
        await self.conn.execute("UPDATE leads SET sla_pinged = 1 WHERE id = ?", (lead_id,))
        await self.conn.commit()

    # --- follow-up ---
    async def schedule_followup(self, lead_id: int, hours: int) -> None:
        at = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        await self.conn.execute("UPDATE leads SET followup_at = ?, followup_count = followup_count + 1 WHERE id = ?",
                                (at, lead_id))
        await self.conn.commit()

    async def clear_followup(self, lead_id: int) -> None:
        await self.conn.execute("UPDATE leads SET followup_at = NULL WHERE id = ?", (lead_id,))
        await self.conn.commit()

    async def followups_due(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE followup_at IS NOT NULL AND followup_at <= ? AND status = 'contacted'", (_now(),)
        )
        return list(await cur.fetchall())

    # --- команда ---
    async def add_member(self, user_id: int, name: str) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO team VALUES (?, ?, ?)", (user_id, name, _now()))
        await self.conn.commit()

    async def remove_member(self, user_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM team WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return cur.rowcount

    async def members(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM team ORDER BY added_at")
        return list(await cur.fetchall())

    async def is_member(self, user_id: int) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM team WHERE user_id = ?", (user_id,))
        return await cur.fetchone() is not None

    async def team_stats(self, days: int = 7) -> list[aiosqlite.Row]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.conn.execute(
            "SELECT user_name, status, COUNT(*) AS n FROM lead_events WHERE at >= ? AND user_id IS NOT NULL"
            " GROUP BY user_name, status", (since,)
        )
        return list(await cur.fetchall())

    # --- правила-фильтры ---
    async def add_rule(self, expr: str) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO rules (expr, created_at) VALUES (?, ?)", (expr.strip(), _now()))
        await self.conn.commit()

    async def remove_rule(self, rule_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        await self.conn.commit()
        return cur.rowcount

    async def rules(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM rules ORDER BY id")
        return list(await cur.fetchall())

    # --- статистика чатов и авторов ---
    async def bump_chat_stats(self, chat_key: str, title: str, messages: int = 0, leads: int = 0) -> None:
        await self.conn.execute(
            "INSERT INTO chat_stats (chat_key, title, messages, leads, last_seen) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(chat_key) DO UPDATE SET messages = messages + ?, leads = leads + ?, last_seen = ?,"
            " title = COALESCE(NULLIF(excluded.title, ''), chat_stats.title)",
            (chat_key, title, messages, leads, _now(), messages, leads, _now()),
        )
        await self.conn.commit()

    async def chat_rating(self, limit: int = 50) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM chat_stats ORDER BY leads DESC, messages DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    async def author_seen(self, author: str, chat_key: str, window_hours: int = 1) -> int:
        """Сколько разных чатов автор засветил за окно. ≥3 — это рассылка, а не клиент."""
        await self.conn.execute("INSERT INTO author_events VALUES (?, ?, ?)", (author.lower(), chat_key, _now()))
        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT COUNT(DISTINCT chat_key) FROM author_events WHERE author = ? AND at >= ?", (author.lower(), since)
        )
        await self.conn.commit()
        return (await cur.fetchone())[0]

    # --- шаблоны ответов ---
    async def set_template(self, service: str, text: str) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO templates VALUES (?, ?)", (service, text))
        await self.conn.commit()

    async def get_template(self, service: str) -> str | None:
        cur = await self.conn.execute("SELECT text FROM templates WHERE service = ?", (service,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def templates(self) -> dict[str, str]:
        cur = await self.conn.execute("SELECT service, text FROM templates")
        return {r[0]: r[1] for r in await cur.fetchall()}

    # --- аналитика ---
    async def funnel(self, days: int = 30, by: str = "source") -> list[aiosqlite.Row]:
        """Воронка: найдено → взято → написали → ответил → сделка, в разрезе source / phrase / chat_title."""
        col = {"source": "source", "phrase": "phrase", "chat": "chat_title", "service": "service"}[by]
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.conn.execute(
            f"SELECT COALESCE({col}, '—') AS k, COUNT(*) AS found,"
            " SUM(status IN ('taken','contacted','replied','deal','lost')) AS taken,"
            " SUM(status IN ('contacted','replied','deal','lost')) AS contacted,"
            " SUM(status IN ('replied','deal','lost')) AS replied,"
            " SUM(status = 'deal') AS deal, SUM(status = 'spam') AS spam"
            f" FROM leads WHERE created_at >= ? GROUP BY {col} ORDER BY deal DESC, replied DESC, found DESC", (since,)
        )
        return list(await cur.fetchall())

    async def response_times(self, days: int = 7) -> list[float]:
        """Минуты от появления лида до «взял» — метрика speed-to-lead."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.conn.execute(
            "SELECT created_at, assigned_at FROM leads WHERE assigned_at IS NOT NULL AND created_at >= ?", (since,)
        )
        out = []
        for created, assigned in await cur.fetchall():
            out.append((datetime.fromisoformat(assigned) - datetime.fromisoformat(created)).total_seconds() / 60)
        return out

    async def recent_leads(self, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    async def undelivered_leads(self, limit: int = 20, board_max_age_hours: float = 3.0) -> list[aiosqlite.Row]:
        """Недоставленные лиды; с бирж — только те, что ещё можно забрать (моложе N часов)."""
        since = (datetime.now(timezone.utc) - timedelta(hours=board_max_age_hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE msg_id IS NULL AND status = 'new'"
            " AND (source NOT IN ('kwork', 'weblancer', 'freelance.ru', 'hh.ru', 'rss') AND source NOT LIKE 'rss:%' OR found_at >= ?)"
            " ORDER BY score DESC, id DESC LIMIT ?", (since, limit)
        )
        return list(await cur.fetchall())

    async def leads_by_status(self, status: str, limit: int = 5) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        )
        return list(await cur.fetchall())

    async def leads_since(self, hours: int) -> list[aiosqlite.Row]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE created_at >= ? ORDER BY score DESC, id DESC", (since,)
        )
        return list(await cur.fetchall())

    async def stats(self, hours: int = 24) -> list[tuple[str, int]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT source, COUNT(*) FROM leads WHERE created_at >= ? GROUP BY source ORDER BY 2 DESC", (since,)
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]

    async def export_csv(self, limit: int = 1000) -> bytes:
        cur = await self.conn.execute(
            "SELECT id, created_at, source, score, status, service, budget, title, text, author, contact, url, summary"
            " FROM leads ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([d[0] for d in cur.description])
        w.writerows(tuple(_csv_safe(v) for v in r) for r in rows)
        return buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel открыл кириллицу

    # --- ключевые слова ---
    async def get_keywords(self) -> Keywords:
        cur = await self.conn.execute("SELECT kind, word, category FROM keywords")
        kw = Keywords(topic={}, intent=[], negative=[])
        for kind, word, category in await cur.fetchall():
            if kind == "topic":
                kw.topic[word] = category or "другое"
            else:
                getattr(kw, kind).append(word)
        return kw

    async def add_keyword(self, kind: str, word: str, category: str | None = None) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO keywords VALUES (?, ?, ?)",
            (kind, normalize(word), category if kind == "topic" else None),
        )
        await self.conn.commit()

    async def remove_keyword(self, word: str) -> int:
        cur = await self.conn.execute("DELETE FROM keywords WHERE word = ?", (normalize(word),))
        await self.conn.commit()
        return cur.rowcount

    # --- игнор-листы: шумные чаты и авторы-спамеры ---
    async def add_ignored(self, kind: str, key: str, label: str = "") -> None:
        await self.conn.execute("INSERT OR REPLACE INTO ignored VALUES (?, ?, ?)", (kind, key.lower(), label))
        await self.conn.commit()

    async def remove_ignored(self, key: str) -> int:
        cur = await self.conn.execute("DELETE FROM ignored WHERE key = ?", (key.lower(),))
        await self.conn.commit()
        return cur.rowcount

    async def get_ignored(self) -> dict[str, dict[str, str]]:
        cur = await self.conn.execute("SELECT kind, key, label FROM ignored")
        out: dict[str, dict[str, str]] = {"chat": {}, "author": {}}
        for kind, key, label in await cur.fetchall():
            out.setdefault(kind, {})[key] = label or ""
        return out

    # --- настройки ---
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        await self.conn.commit()
