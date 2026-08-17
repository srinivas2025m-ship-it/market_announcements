"""
db.py — SQLite data layer for the Research/Article Planner.

Schema overview
----------------
articles
    One row per source item (article or YouTube link) the user wants to
    read/watch, track, and eventually act on.
    - category: Economy | Industry | Regulatory | Company
    - status: Not Started | In Progress | Completed
    - planned_date: target completion date -> drives reminders
    - is_recurring / recurrence: for reports that repeat Monthly/Yearly
    - next_due_date: when a recurring item's *next edition* is expected
    - notes: short trace/review note

derived_articles
    Articles the user writes based on research gathered from `articles`.

derived_sources
    Many-to-many link: which source articles fed into a derived article.
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "planner.db"

CATEGORIES = ["Economy", "Industry", "Regulatory", "Company"]
SOURCE_TYPES = ["Article", "YouTube"]
STATUSES = ["Not Started", "In Progress", "Completed"]
RECURRENCE = ["None", "Monthly", "Yearly"]


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK(source_type IN ('Article','YouTube')),
            category TEXT NOT NULL CHECK(category IN ('Economy','Industry','Regulatory','Company')),
            tags TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Not Started' CHECK(status IN ('Not Started','In Progress','Completed')),
            planned_date TEXT,
            completed_date TEXT,
            is_recurring INTEGER NOT NULL DEFAULT 0,
            recurrence TEXT NOT NULL DEFAULT 'None' CHECK(recurrence IN ('None','Monthly','Yearly')),
            next_due_date TEXT,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS derived_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT,
            content TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS derived_sources (
            derived_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            FOREIGN KEY(derived_id) REFERENCES derived_articles(id) ON DELETE CASCADE,
            FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE,
            PRIMARY KEY (derived_id, article_id)
        )
    """)
    conn.commit()
    conn.close()


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- articles

def add_article(title, url, source_type, category, tags, planned_date,
                 is_recurring, recurrence, notes):
    conn = get_conn()
    conn.execute("""
        INSERT INTO articles
            (title, url, source_type, category, tags, status, planned_date,
             is_recurring, recurrence, next_due_date, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'Not Started', ?, ?, ?, ?, ?, ?, ?)
    """, (title, url, source_type, category, tags, planned_date,
          int(is_recurring), recurrence,
          planned_date if is_recurring else None,
          notes, _now(), _now()))
    conn.commit()
    conn.close()


def get_articles(category=None, status=None, search=None, source_type=None):
    conn = get_conn()
    q = "SELECT * FROM articles WHERE 1=1"
    params = []
    if category and category != "All":
        q += " AND category = ?"
        params.append(category)
    if status and status != "All":
        q += " AND status = ?"
        params.append(status)
    if source_type and source_type != "All":
        q += " AND source_type = ?"
        params.append(source_type)
    if search:
        q += " AND (title LIKE ? OR tags LIKE ? OR notes LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like]
    q += " ORDER BY (planned_date IS NULL), planned_date ASC, id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_article(article_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_article(article_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [article_id]
    conn = get_conn()
    conn.execute(f"UPDATE articles SET {cols} WHERE id = ?", params)
    conn.commit()
    conn.close()


def delete_article(article_id):
    conn = get_conn()
    conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    conn.execute("DELETE FROM derived_sources WHERE article_id = ?", (article_id,))
    conn.commit()
    conn.close()


def _advance_date(d: date, recurrence: str) -> date:
    if recurrence == "Monthly":
        month = d.month + 1
        year = d.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(d.day, 28)  # safe day to avoid month-length issues
        return date(year, month, day)
    if recurrence == "Yearly":
        try:
            return d.replace(year=d.year + 1)
        except ValueError:
            return d.replace(year=d.year + 1, day=28)
    return d


def mark_complete(article_id):
    art = get_article(article_id)
    if not art:
        return
    today = date.today()
    updates = {"status": "Completed", "completed_date": today.isoformat()}
    if art["is_recurring"]:
        base = date.fromisoformat(art["planned_date"]) if art["planned_date"] else today
        base = max(base, today)
        next_due = _advance_date(base, art["recurrence"])
        updates["next_due_date"] = next_due.isoformat()
        # Recurring items reopen automatically for their next edition
        updates["status"] = "Not Started"
        updates["planned_date"] = next_due.isoformat()
    update_article(article_id, **updates)


def get_overdue():
    today = date.today().isoformat()
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM articles
        WHERE status != 'Completed' AND planned_date IS NOT NULL AND planned_date < ?
        ORDER BY planned_date ASC
    """, (today,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_due_soon(days=3):
    today = date.today()
    limit = (today + timedelta(days=days)).isoformat()
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM articles
        WHERE status != 'Completed' AND planned_date IS NOT NULL
              AND planned_date >= ? AND planned_date <= ?
        ORDER BY planned_date ASC
    """, (today.isoformat(), limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recurring():
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM articles WHERE is_recurring = 1 ORDER BY next_due_date ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
    completed = conn.execute("SELECT COUNT(*) c FROM articles WHERE status='Completed'").fetchone()["c"]
    by_cat = conn.execute("""
        SELECT category, COUNT(*) c, SUM(status='Completed') done
        FROM articles GROUP BY category
    """).fetchall()
    conn.close()
    return {
        "total": total,
        "completed": completed,
        "by_category": [dict(r) for r in by_cat],
    }


# --------------------------------------------------------- derived articles

def add_derived_article(title, category, content, source_ids):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO derived_articles (title, category, content, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
    """, (title, category, content, _now(), _now()))
    derived_id = cur.lastrowid
    for sid in source_ids:
        cur.execute("INSERT OR IGNORE INTO derived_sources (derived_id, article_id) VALUES (?, ?)",
                    (derived_id, sid))
    conn.commit()
    conn.close()
    return derived_id


def get_derived_articles():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM derived_articles ORDER BY id DESC").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        srcs = conn.execute("""
            SELECT a.id, a.title FROM derived_sources ds
            JOIN articles a ON a.id = ds.article_id
            WHERE ds.derived_id = ?
        """, (d["id"],)).fetchall()
        d["sources"] = [dict(s) for s in srcs]
        result.append(d)
    conn.close()
    return result


def update_derived_article(derived_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [derived_id]
    conn = get_conn()
    conn.execute(f"UPDATE derived_articles SET {cols} WHERE id = ?", params)
    conn.commit()
    conn.close()


def delete_derived_article(derived_id):
    conn = get_conn()
    conn.execute("DELETE FROM derived_articles WHERE id = ?", (derived_id,))
    conn.execute("DELETE FROM derived_sources WHERE derived_id = ?", (derived_id,))
    conn.commit()
    conn.close()
