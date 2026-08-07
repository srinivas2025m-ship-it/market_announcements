#!/usr/bin/env python3
"""
Announcement Ideas pipeline
============================
Layers an "idea board" taxonomy + rule-based scoring engine on top of
the existing bse_equity.db schema, without touching the original tables.

The database is the source of truth: `run` migrates the schema, seeds
the taxonomy/rules, and scores announcements straight into
announcement_idea_scores. The Streamlit report (idea_board_streamlit.py)
reads directly from this DB — no JSON export is required in normal use.

Usage:
    python3 announcement_ideas_pipeline.py --db bse_equity.db run
    # rescore everything from scratch (e.g. after editing idea_rules.py):
    python3 announcement_ideas_pipeline.py --db bse_equity.db run --recompute

    # only process/export announcements within a date range (inclusive,
    # matched against input_timestamp):
    python3 announcement_ideas_pipeline.py --db bse_equity.db run --from 2026-06-01 --to 2026-06-30

    # individual steps, if you want them separately:
    python3 announcement_ideas_pipeline.py --db bse_equity.db migrate
    python3 announcement_ideas_pipeline.py --db bse_equity.db seed
    python3 announcement_ideas_pipeline.py --db bse_equity.db classify

    # optional JSON export (only needed for the standalone idea_board.html artifact):
    python3 announcement_ideas_pipeline.py --db bse_equity.db --out ideas_export.json export
    python3 announcement_ideas_pipeline.py --db bse_equity.db --out ideas_export.json run --export

    # build+load a small synthetic demo database (for trying the pipeline
    # without real BSE data):
    python3 announcement_ideas_pipeline.py --db demo.db demo
"""
import argparse
import json
import re
import sqlite3
from datetime import datetime

from idea_rules import IDEA_TYPES, GROUPS, CATEGORY_BONUS_WEIGHT, MIN_SCORE_THRESHOLD

# ─────────────────────────────────────────────────────────────────────────
# SOURCE NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────
# The four market_announcements.py databases (bse_equity.db / bse_sme.db /
# nse_equity.db / nse_sme.db) each store `announcements` with a DIFFERENT
# native schema:
#
#   BSE Equity : company_name, symbol, scrip_code, subject, input_timestamp,
#                attachment_url/file_name, category_id/subcategory_id
#                (exposed as a ready-made view `v_announcements`)
#   BSE SME    : scrip_name, scrip_code, category, grp, purpose, announce_date,
#                attachment_url   (no v_announcements view)
#   NSE Eq/SME : symbol, company_name, subject, ann_date ("28-Jun-2026 23:26:37"),
#                attachment_url   (no v_announcements view)
#
# Everything downstream (classify/export, and the Idea Board + Tracker in
# market_suite.py) is written once against a single canonical shape. This
# view maps whichever native schema is present onto that shape so the SAME
# pipeline invocation (`--db <any of the 4 dbs> run`) works everywhere.
SOURCE_VIEW = "v_idea_source"

_NSE_DATE_TO_ISO = """
    (CASE
       WHEN length(ann_date) >= 11 THEN
         substr(ann_date,8,4)||'-'||
         CASE substr(ann_date,4,3)
           WHEN 'Jan' THEN '01' WHEN 'Feb' THEN '02' WHEN 'Mar' THEN '03'
           WHEN 'Apr' THEN '04' WHEN 'May' THEN '05' WHEN 'Jun' THEN '06'
           WHEN 'Jul' THEN '07' WHEN 'Aug' THEN '08' WHEN 'Sep' THEN '09'
           WHEN 'Oct' THEN '10' WHEN 'Nov' THEN '11' WHEN 'Dec' THEN '12'
           ELSE '00' END||'-'||
         substr(ann_date,1,2)||substr(ann_date,12)
       ELSE ann_date
     END)
"""


def _table_cols(conn, table):
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def ensure_source_view(conn):
    """(Re)creates SOURCE_VIEW so it always reflects the current underlying
    schema, mapping it onto the canonical column set:
        id, company_name, symbol, scrip_code, subject,
        input_timestamp, attachment_url, category, subcategory
    Safe to call repeatedly (e.g. once per command)."""
    cur = conn.cursor()
    ann_cols = _table_cols(conn, "announcements")
    if not ann_cols:
        raise sqlite3.OperationalError(
            "no `announcements` table found in this database — run market_announcements.py first"
        )
    view_cols = _table_cols(conn, "v_announcements")
    id_col = "id" if "id" in ann_cols else "rowid"

    if view_cols and {"company_name", "symbol", "subject", "input_timestamp"}.issubset(view_cols):
        # BSE Equity-style: a ready-made view already exposes the canonical shape.
        has_cat = {"category", "subcategory"}.issubset(view_cols)
        select_sql = f"""
            SELECT id AS id, company_name, symbol, scrip_code, subject,
                   input_timestamp, attachment_url,
                   {"category" if has_cat else "NULL"} AS category,
                   {"subcategory" if has_cat else "NULL"} AS subcategory
            FROM v_announcements
        """
    elif {"scrip_name", "purpose", "announce_date"}.issubset(ann_cols):
        # BSE SME-style
        select_sql = f"""
            SELECT {id_col} AS id, scrip_name AS company_name, scrip_code AS symbol,
                   scrip_code AS scrip_code, purpose AS subject,
                   announce_date AS input_timestamp,
                   {"attachment_url" if "attachment_url" in ann_cols else "NULL"} AS attachment_url,
                   {"category" if "category" in ann_cols else "NULL"} AS category,
                   {"grp" if "grp" in ann_cols else "NULL"} AS subcategory
            FROM announcements
        """
    elif {"symbol", "company_name", "subject", "ann_date"}.issubset(ann_cols):
        # NSE Equity / NSE SME-style (same schema for both)
        select_sql = f"""
            SELECT {id_col} AS id, company_name, symbol, NULL AS scrip_code, subject,
                   {_NSE_DATE_TO_ISO} AS input_timestamp,
                   {"attachment_url" if "attachment_url" in ann_cols else "NULL"} AS attachment_url,
                   NULL AS category, NULL AS subcategory
            FROM announcements
        """
    elif {"company_name", "symbol", "subject", "input_timestamp"}.issubset(ann_cols):
        # Canonical shape already present directly on the table.
        select_sql = f"""
            SELECT {id_col} AS id, company_name, symbol,
                   {"scrip_code" if "scrip_code" in ann_cols else "NULL"} AS scrip_code,
                   subject, input_timestamp,
                   {"attachment_url" if "attachment_url" in ann_cols else "NULL"} AS attachment_url,
                   {"category" if "category" in ann_cols else "NULL"} AS category,
                   {"subcategory" if "subcategory" in ann_cols else "NULL"} AS subcategory
            FROM announcements
        """
    else:
        raise sqlite3.OperationalError(
            f"don't recognize this database's `announcements` schema (columns: {sorted(ann_cols)}); "
            f"can't build {SOURCE_VIEW}"
        )

    cur.execute(f"DROP VIEW IF EXISTS {SOURCE_VIEW}")
    cur.execute(f"CREATE VIEW {SOURCE_VIEW} AS {select_sql}")
    conn.commit()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS idea_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sort_order INTEGER
);

CREATE TABLE IF NOT EXISTS idea_types (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES idea_groups(id),
    name        TEXT NOT NULL,
    description TEXT,
    sort_order  INTEGER,
    UNIQUE (group_id, name)
);

CREATE TABLE IF NOT EXISTS idea_keyword_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_type_id INTEGER NOT NULL REFERENCES idea_types(id),
    phrase       TEXT NOT NULL,
    weight       REAL NOT NULL DEFAULT 1.0,
    is_negative  INTEGER NOT NULL DEFAULT 0,
    is_category_hint INTEGER NOT NULL DEFAULT 0,
    UNIQUE (idea_type_id, phrase, is_category_hint)
);

CREATE TABLE IF NOT EXISTS announcement_idea_scores (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id  INTEGER NOT NULL REFERENCES announcements(id),
    idea_type_id     INTEGER NOT NULL REFERENCES idea_types(id),
    score            REAL NOT NULL,
    matched_keywords TEXT,
    scored_at        TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (announcement_id, idea_type_id)
);
CREATE INDEX IF NOT EXISTS idx_idea_scores_type  ON announcement_idea_scores(idea_type_id);
CREATE INDEX IF NOT EXISTS idx_idea_scores_score ON announcement_idea_scores(score);
CREATE INDEX IF NOT EXISTS idx_idea_scores_type_score ON announcement_idea_scores(idea_type_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_idea_scores_ann   ON announcement_idea_scores(announcement_id);

"""

# v_announcement_ideas joins against SOURCE_VIEW (not the raw `announcements`
# table) so it works regardless of which of the 4 DBs this is run against —
# built separately from SCHEMA_SQL because it depends on ensure_source_view()
# having run first.
V_ANNOUNCEMENT_IDEAS_SQL = f"""
CREATE VIEW v_announcement_ideas AS
SELECT
    s.id AS score_id,
    a.id AS announcement_id,
    a.company_name, a.symbol, a.scrip_code,
    a.subject, a.input_timestamp, a.attachment_url,
    g.name  AS idea_group,
    it.name AS idea_type,
    s.score, s.matched_keywords
FROM announcement_idea_scores s
JOIN {SOURCE_VIEW} a ON a.id = s.announcement_id
JOIN idea_types    it ON it.id = s.idea_type_id
JOIN idea_groups    g ON g.id = it.group_id;
"""


def migrate(conn):
    ensure_source_view(conn)
    conn.executescript(SCHEMA_SQL)
    conn.execute("DROP VIEW IF EXISTS v_announcement_ideas")
    conn.execute(V_ANNOUNCEMENT_IDEAS_SQL)
    conn.commit()
    print(f"[migrate] schema ready ({SOURCE_VIEW} + idea tables).")


def seed(conn):
    cur = conn.cursor()

    # Check-then-insert/update rather than ON CONFLICT: a DB whose
    # idea_groups/idea_types tables were created earlier by something else
    # (e.g. market_suite.py's Guided Activity, which only uniques idea_types
    # on `name`, not on (group_id, name)) won't have the exact constraint
    # ON CONFLICT needs to target, and CREATE TABLE IF NOT EXISTS above
    # doesn't retrofit one onto an existing table. This works regardless.
    group_id = {}
    for i, gname in enumerate(GROUPS):
        row = cur.execute("SELECT id FROM idea_groups WHERE name = ?", (gname,)).fetchone()
        if row:
            gid = row[0]
            cur.execute("UPDATE idea_groups SET sort_order = ? WHERE id = ?", (i, gid))
        else:
            cur.execute("INSERT INTO idea_groups (name, sort_order) VALUES (?, ?)", (gname, i))
            gid = cur.lastrowid
        group_id[gname] = gid
    conn.commit()

    # idea_type names are unique across the whole taxonomy (see idea_rules.py),
    # so key off name alone rather than (group_id, name).
    type_id = {}
    for j, (type_name, cfg) in enumerate(IDEA_TYPES.items()):
        gid = group_id[cfg["group"]]
        row = cur.execute("SELECT id FROM idea_types WHERE name = ?", (type_name,)).fetchone()
        if row:
            tid = row[0]
            cur.execute(
                "UPDATE idea_types SET group_id = ?, description = ?, sort_order = ? WHERE id = ?",
                (gid, cfg["description"], j, tid),
            )
        else:
            cur.execute(
                "INSERT INTO idea_types (group_id, name, description, sort_order) VALUES (?,?,?,?)",
                (gid, type_name, cfg["description"], j),
            )
            tid = cur.lastrowid
        type_id[type_name] = tid
    conn.commit()

    # Best-effort: add real UNIQUE indexes going forward, if the data
    # already in the tables allows it (silently skip if it doesn't — e.g.
    # pre-existing duplicate names from before this fix).
    for stmt in (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_idea_groups_name ON idea_groups(name)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_idea_types_name ON idea_types(name)",
    ):
        try:
            cur.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass

    cur.execute("DELETE FROM idea_keyword_rules")  # rules always re-seeded from idea_rules.py
    for type_name, cfg in IDEA_TYPES.items():
        tid = type_id[type_name]
        for phrase, weight in cfg["keywords"]:
            cur.execute(
                "INSERT INTO idea_keyword_rules (idea_type_id, phrase, weight, is_negative, is_category_hint) "
                "VALUES (?,?,?,0,0)",
                (tid, phrase.strip().lower(), weight),
            )
        for phrase in cfg.get("negative", []):
            cur.execute(
                "INSERT INTO idea_keyword_rules (idea_type_id, phrase, weight, is_negative, is_category_hint) "
                "VALUES (?,?,?,1,0)",
                (tid, phrase.strip().lower(), 0),
            )
        for phrase in cfg.get("category_hints", []):
            cur.execute(
                "INSERT INTO idea_keyword_rules (idea_type_id, phrase, weight, is_negative, is_category_hint) "
                "VALUES (?,?,?,0,1)",
                (tid, phrase.strip().lower(), CATEGORY_BONUS_WEIGHT),
            )
    conn.commit()
    print(f"[seed] {len(GROUPS)} groups, {len(IDEA_TYPES)} idea types, rules loaded.")


def _apply_date_filter(base_sql, params, date_from=None, date_to=None):
    """Append a WHERE (or AND) clause filtering input_timestamp to [date_from, date_to].

    date_from/date_to are 'YYYY-MM-DD' strings (inclusive on both ends).
    Compares only the first 10 characters of input_timestamp (the date part),
    so it's robust to whether the stored value uses a space or 'T' separator,
    includes seconds, etc. Returns (sql, params).
    """
    clauses = []
    if date_from:
        clauses.append("substr(input_timestamp, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("substr(input_timestamp, 1, 10) <= ?")
        params.append(date_to)
    if not clauses:
        return base_sql, params
    joiner = "WHERE" if " WHERE " not in base_sql.upper() else "AND"
    sql = f"{base_sql} {joiner} " + " AND ".join(clauses)
    return sql, params


def _cap_for(cfg):
    weights = sorted([w for _, w in cfg["keywords"]], reverse=True)
    top2 = sum(weights[:2]) if weights else 2.0
    if cfg.get("category_hints"):
        top2 += CATEGORY_BONUS_WEIGHT
    return max(top2, 2.0)


def classify(conn, recompute=False, date_from=None, date_to=None):
    cur = conn.cursor()
    ensure_source_view(conn)
    source = SOURCE_VIEW

    type_id = {r[0]: r[1] for r in cur.execute("SELECT name, id FROM idea_types")}
    caps = {name: _cap_for(cfg) for name, cfg in IDEA_TYPES.items()}

    if recompute:
        if date_from or date_to:
            sub_sql, sub_params = _apply_date_filter(f"SELECT id FROM {source}", [], date_from, date_to)
            del_sql = f"DELETE FROM announcement_idea_scores WHERE announcement_id IN ({sub_sql})"
            cur.execute(del_sql, sub_params)
        else:
            cur.execute("DELETE FROM announcement_idea_scores")

    base_sql = f"SELECT id, subject, category, subcategory FROM {source}"
    sql, params = _apply_date_filter(base_sql, [], date_from, date_to)
    rows = cur.execute(sql, params).fetchall()

    already_scored = set()
    if not recompute:
        already_scored = {r[0] for r in cur.execute("SELECT DISTINCT announcement_id FROM announcement_idea_scores")}

    inserted = 0
    for ann_id, subject, category, subcategory in rows:
        if ann_id in already_scored:
            continue
        subj = f" {(subject or '').lower()} "
        cat_text = f"{(category or '')} {(subcategory or '')}".lower()

        for type_name, cfg in IDEA_TYPES.items():
            raw = 0.0
            matched = []
            disqualified = False
            for phrase in cfg.get("negative", []):
                if phrase in subj:
                    disqualified = True
                    break
            if disqualified:
                continue

            for phrase, weight in cfg["keywords"]:
                if phrase in subj:
                    raw += weight
                    matched.append(phrase.strip())

            for phrase in cfg.get("category_hints", []):
                if phrase in cat_text:
                    raw += CATEGORY_BONUS_WEIGHT
                    matched.append(f"[category] {phrase}")
                    break  # only count the bonus once

            if raw <= 0:
                continue

            score = round(min(100.0, raw / caps[type_name] * 100.0), 1)
            if score < MIN_SCORE_THRESHOLD:
                continue

            cur.execute(
                "INSERT INTO announcement_idea_scores (announcement_id, idea_type_id, score, matched_keywords) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(announcement_id, idea_type_id) DO UPDATE SET "
                "score=excluded.score, matched_keywords=excluded.matched_keywords, "
                "scored_at=datetime('now','localtime')",
                (ann_id, type_id[type_name], score, json.dumps(matched)),
            )
            inserted += 1
    conn.commit()
    print(f"[classify] scored {len(rows)} announcements, {inserted} idea matches recorded.")


def export(conn, out_path, date_from=None, date_to=None):
    cur = conn.cursor()
    ensure_source_view(conn)
    groups = cur.execute("SELECT id, name FROM idea_groups ORDER BY sort_order").fetchall()
    result = {"generated_at": datetime.now().isoformat(timespec="seconds"), "groups": []}

    for gid, gname in groups:
        types = cur.execute(
            "SELECT id, name, description FROM idea_types WHERE group_id=? ORDER BY sort_order", (gid,)
        ).fetchall()
        group_entry = {"name": gname, "types": []}
        for tid, tname, tdesc in types:
            items_sql = (
                f"""SELECT a.id, a.company_name, a.symbol, a.scrip_code, a.subject,
                          a.input_timestamp, a.attachment_url, s.score, s.matched_keywords
                   FROM announcement_idea_scores s
                   JOIN {SOURCE_VIEW} a ON a.id = s.announcement_id
                   WHERE s.idea_type_id = ?"""
            )
            items_params = [tid]
            items_sql, items_params = _apply_date_filter(items_sql, items_params, date_from, date_to)
            items_sql += " ORDER BY s.score DESC, a.input_timestamp DESC"
            items = cur.execute(items_sql, items_params).fetchall()
            item_list = [
                {
                    "id": r[0], "company": r[1], "symbol": r[2], "scrip_code": r[3],
                    "subject": r[4], "timestamp": r[5], "url": r[6],
                    "score": r[7], "matched": json.loads(r[8]) if r[8] else [],
                }
                for r in items
            ]
            avg_score = round(sum(i["score"] for i in item_list) / len(item_list), 1) if item_list else 0
            group_entry["types"].append(
                {"name": tname, "description": tdesc, "count": len(item_list),
                 "avg_score": avg_score, "items": item_list}
            )
        result["groups"].append(group_entry)

    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    total_items = sum(t["count"] for g in result["groups"] for t in g["types"])
    print(f"[export] wrote {out_path} ({total_items} idea matches across {len(IDEA_TYPES)} idea types).")


def build_demo_db(path):
    """Creates a small synthetic bse_equity.db with sample announcements, for demoing the pipeline."""
    import os
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
    CREATE TABLE subcategories (id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL, name TEXT NOT NULL, UNIQUE(category_id,name));
    CREATE TABLE announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, script_id TEXT, scrip_code TEXT, symbol TEXT,
        company_name TEXT, category_id INTEGER, subcategory_id INTEGER, subject TEXT,
        file_name TEXT, input_timestamp TEXT, attachment_url TEXT,
        fetched_at TEXT DEFAULT (datetime('now','localtime')), raw_json TEXT
    );
    CREATE VIEW v_announcements AS
    SELECT a.id, a.script_id, a.scrip_code, a.symbol, a.company_name,
           c.name AS category, sc.name AS subcategory, a.subject, a.file_name,
           a.input_timestamp, a.attachment_url, a.fetched_at, a.raw_json
    FROM announcements a
    LEFT JOIN categories c ON c.id = a.category_id
    LEFT JOIN subcategories sc ON sc.id = a.subcategory_id;
    CREATE INDEX IF NOT EXISTS idx_bse_eq_code      ON announcements(scrip_code);
    CREATE INDEX IF NOT EXISTS idx_bse_eq_symbol    ON announcements(symbol);
    CREATE INDEX IF NOT EXISTS idx_bse_eq_timestamp ON announcements(input_timestamp);
    """)
    cur = conn.cursor()
    cats = ["Company Update", "Board Meeting", "Corporate Action", "Result", "Regulatory"]
    subcats = {
        "Company Update": ["Award of Order / Receipt of Order", "Capital Expenditure", "Commissioning",
                            "New Product", "Incorporation / Subsidiary", "Acquisition", "Joint Venture",
                            "Business Update", "Litigation", "Clarification", "Regulatory Approval"],
        "Board Meeting": ["Outcome of Board Meeting", "Intimation"],
        "Corporate Action": ["Dividend", "Buyback", "Bonus"],
        "Result": ["Financial Results"],
        "Regulatory": ["Credit Rating", "Shareholding Pattern"],
    }
    cat_id = {}
    for c in cats:
        cur.execute("INSERT INTO categories (name) VALUES (?)", (c,))
        cat_id[c] = cur.lastrowid
    sub_id = {}
    for c, subs in subcats.items():
        for s in subs:
            cur.execute("INSERT INTO subcategories (category_id, name) VALUES (?,?)", (cat_id[c], s))
            sub_id[(c, s)] = cur.lastrowid

    samples = [
        ("RELIND", "500325", "Reliance Industries", "Company Update", "Award of Order / Receipt of Order",
         "Receipt of Letter of Award for new EPC contract worth Rs. 1,200 crore", "2026-06-02 10:15:00"),
        ("TATASTL", "500470", "Tata Steel", "Company Update", "Capital Expenditure",
         "Board approves capital expenditure of Rs. 4,500 crore for capacity expansion at Kalinganagar",
         "2026-05-14 09:30:00"),
        ("ADANIPORT", "532921", "Adani Ports", "Company Update", "Commissioning",
         "Commissioning of new container terminal; company commences commercial operations",
         "2026-04-22 11:00:00"),
        ("SUNPHARMA", "524715", "Sun Pharmaceutical", "Company Update", "New Product",
         "Launch of new specialty product Ilumya in the US market", "2026-03-18 08:45:00"),
        ("INFY", "500209", "Infosys", "Company Update", "Incorporation / Subsidiary",
         "Incorporation of wholly owned subsidiary in Singapore for new business vertical",
         "2026-06-10 12:00:00"),
        ("HDFCBANK", "500180", "HDFC Bank", "Company Update", "Acquisition",
         "Agreement to acquire 100% stake in a fintech company for Rs. 800 crore", "2026-02-27 14:20:00"),
        ("LT", "500510", "Larsen & Toubro", "Company Update", "Joint Venture",
         "Signing of Memorandum of Understanding for strategic partnership with a global EPC major",
         "2026-05-30 10:00:00"),
        ("WIPRO", "507685", "Wipro", "Company Update", "Business Update",
         "Monthly business update: provisional sales numbers for the quarter", "2026-06-05 09:00:00"),
        ("ITC", "500875", "ITC Limited", "Corporate Action", "Dividend",
         "Board recommends dividend of Rs. 6.75 per equity share; record date announced", "2026-05-20 16:00:00"),
        ("BAJFINANCE", "500034", "Bajaj Finance", "Corporate Action", "Buyback",
         "Board approves buyback of equity shares up to Rs. 2,000 crore", "2026-04-11 15:30:00"),
        ("MARUTI", "532500", "Maruti Suzuki", "Regulatory", "Credit Rating",
         "CRISIL reaffirms credit rating at AAA/Stable with rating outlook unchanged", "2026-03-02 09:15:00"),
        ("DRREDDY", "500124", "Dr. Reddy's Labs", "Company Update", "Regulatory Approval",
         "Receipt of USFDA approval for generic oncology drug", "2026-01-25 10:40:00"),
        ("CIPLA", "500087", "Cipla", "Company Update", "Litigation",
         "Update on litigation - court case regarding patent dispute in the US", "2026-02-14 11:10:00"),
        ("ZOMATO", "543320", "Eternal (Zomato)", "Company Update", "Clarification",
         "Clarification on media report regarding potential merger speculation", "2026-06-08 13:00:00"),
        ("PAYTM", "543396", "One97 Communications", "Board Meeting", "Outcome of Board Meeting",
         "Resignation of Chief Financial Officer with immediate effect", "2026-05-05 17:00:00"),
        ("NYKAA", "543384", "FSN E-Commerce (Nykaa)", "Board Meeting", "Outcome of Board Meeting",
         "Appointment of Ms. Anjali Rao as Independent Director on the Board", "2026-04-30 16:45:00"),
        ("IRCTC", "542830", "IRCTC", "Board Meeting", "Outcome of Board Meeting",
         "Appointment of Company Secretary and Compliance Officer", "2026-03-27 12:30:00"),
        ("ZOMATO2", "543320", "Eternal (Zomato)", "Board Meeting", "Outcome of Board Meeting",
         "Resignation of Statutory Auditor and appointment of new statutory auditor",
         "2026-02-19 10:05:00"),
        ("IRFC", "543257", "IRFC", "Company Update", "Award of Order / Receipt of Order",
         "Company bags order worth Rs. 650 crore from Ministry of Railways", "2026-01-30 09:50:00"),
        ("ADANIGREEN", "541450", "Adani Green Energy", "Company Update", "Capital Expenditure",
         "Setting up a new greenfield solar manufacturing plant with capex plan of Rs. 3,000 crore",
         "2025-12-20 08:20:00"),
    ]
    for symbol, code, name, cat, sub, subject, ts in samples:
        cur.execute(
            "INSERT INTO announcements (scrip_code, symbol, company_name, category_id, subcategory_id, "
            "subject, file_name, input_timestamp, attachment_url) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, symbol, name, cat_id[cat], sub_id[(cat, sub)], subject,
             f"{symbol}_{ts[:10]}.pdf", ts, f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/demo_{code}.pdf"),
        )
    conn.commit()
    conn.close()
    print(f"[demo] built synthetic database at {path} with {len(samples)} announcements.")


def main():
    ap = argparse.ArgumentParser(description="Announcement Ideas pipeline")
    ap.add_argument("--db", default="bse_equity.db", help="path to sqlite db (this IS the data store)")
    ap.add_argument("--out", default="ideas_export.json", help="path to export JSON (only used with --export or the export command)")
    ap.add_argument("--recompute", action="store_true", help="rescore all announcements, not just new ones")
    ap.add_argument("--export", action="store_true", help="also write a JSON export after 'run' (optional; the DB itself is the source of truth for Streamlit/reporting)")
    ap.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", default=None,
                     help="only process announcements with input_timestamp >= this date (inclusive)")
    ap.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", default=None,
                     help="only process announcements with input_timestamp <= this date (inclusive)")
    ap.add_argument("command", choices=["migrate", "seed", "classify", "export", "run", "demo"])
    args = ap.parse_args()

    for label, value in (("--from", args.date_from), ("--to", args.date_to)):
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                ap.error(f"{label} must be in YYYY-MM-DD format, got {value!r}")

    if args.command == "demo":
        build_demo_db(args.db)
        return

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")

    if args.command == "migrate":
        migrate(conn)
    elif args.command == "seed":
        seed(conn)
    elif args.command == "classify":
        classify(conn, recompute=args.recompute, date_from=args.date_from, date_to=args.date_to)
    elif args.command == "export":
        export(conn, args.out, date_from=args.date_from, date_to=args.date_to)
    elif args.command == "run":
        # migrate + seed + classify write everything straight into --db.
        # No JSON file is produced unless --export is passed: the DB is
        # the single source of truth that the Streamlit report reads from.
        migrate(conn)
        seed(conn)
        classify(conn, recompute=args.recompute, date_from=args.date_from, date_to=args.date_to)
        if args.export:
            export(conn, args.out, date_from=args.date_from, date_to=args.date_to)

    conn.close()


if __name__ == "__main__":
    main()
