import csv
import io
from datetime import datetime, timedelta, timezone

from leadhunter.db import Database
from leadhunter.models import Lead
from leadhunter.scoring import ScoreResult


async def make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    return db


async def test_wal_and_schema_version(tmp_path):
    db = await make_db(tmp_path)
    cur = await db.conn.execute("PRAGMA journal_mode")
    assert (await cur.fetchone())[0] == "wal"
    cur = await db.conn.execute("PRAGMA busy_timeout")
    assert (await cur.fetchone())[0] == 5000
    cur = await db.conn.execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == 1
    await db.close()
    # повторное подключение не ломает уже мигрированную базу
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    cur = await db.conn.execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_purge_old_removes_only_stale(tmp_path):
    db = await make_db(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    await db.conn.execute("INSERT INTO seen VALUES ('kwork', 'old', ?)", (old,))
    await db.conn.execute("INSERT INTO seen_hashes VALUES ('oldhash', ?)", (old,))
    await db.conn.execute("INSERT INTO author_events VALUES ('a', 'c', ?)", (old,))
    await db.conn.commit()
    assert await db.mark_seen("kwork", "fresh")
    assert await db.mark_hash_seen("свежий текст запроса")
    await db.author_seen("b", "c2")

    removed = await db.purge_old(30)
    assert removed >= 3
    # старая запись забыта — тот же запрос снова считается новым, свежая по-прежнему дубль
    assert await db.mark_seen("kwork", "old") is True
    assert await db.mark_seen("kwork", "fresh") is False
    assert await db.mark_hash_seen("свежий текст запроса") is False
    cur = await db.conn.execute("SELECT COUNT(*) FROM author_events WHERE author = 'a'")
    assert (await cur.fetchone())[0] == 0
    await db.close()


async def test_csv_export_escapes_formulas(tmp_path):
    db = await make_db(tmp_path)
    lead = Lead(source="kwork", external_id="1", title="=HYPERLINK(\"http://evil\")", text="+1 нужен сайт",
                author="@admin", contact="-cmd", url="https://x")
    await db.save_lead(lead, 7, ScoreResult(7, [], [], []), summary="\tтаб")
    data = (await db.export_csv()).decode("utf-8-sig")
    row = next(csv.DictReader(io.StringIO(data)))
    assert row["title"].startswith("'=")
    assert row["text"] == "'+1 нужен сайт"
    assert row["author"] == "'@admin"
    assert row["contact"] == "'-cmd"
    assert row["summary"] == "'\tтаб"
    assert row["url"] == "https://x"
    await db.close()
