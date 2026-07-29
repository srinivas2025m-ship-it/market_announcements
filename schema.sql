BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS "announcement_idea_scores" (
	"id"	INTEGER,
	"announcement_id"	INTEGER NOT NULL,
	"idea_type_id"	INTEGER NOT NULL,
	"score"	REAL NOT NULL,
	"matched_keywords"	TEXT,
	"scored_at"	TEXT DEFAULT (datetime('now', 'localtime')),
	UNIQUE("announcement_id","idea_type_id"),
	PRIMARY KEY("id" AUTOINCREMENT),
	FOREIGN KEY("announcement_id") REFERENCES "announcements"("id"),
	FOREIGN KEY("idea_type_id") REFERENCES "idea_types"("id")
);
CREATE TABLE IF NOT EXISTS "announcements" (
	"id"	INTEGER,
	"script_id"	TEXT,
	"scrip_code"	TEXT,
	"symbol"	TEXT,
	"company_name"	TEXT,
	"category_id"	INTEGER,
	"subcategory_id"	INTEGER,
	"subject"	TEXT,
	"file_name"	TEXT,
	"input_timestamp"	TEXT,
	"attachment_url"	TEXT,
	"fetched_at"	TEXT DEFAULT (datetime('now', 'localtime')),
	"raw_json"	TEXT,
	PRIMARY KEY("id" AUTOINCREMENT),
	UNIQUE("script_id","file_name","input_timestamp"),
	FOREIGN KEY("category_id") REFERENCES "categories"("id"),
	FOREIGN KEY("subcategory_id") REFERENCES "subcategories"("id")
);
CREATE TABLE IF NOT EXISTS "categories" (
	"id"	INTEGER,
	"name"	TEXT NOT NULL UNIQUE,
	PRIMARY KEY("id" AUTOINCREMENT)
);
CREATE TABLE IF NOT EXISTS "idea_groups" (
	"id"	INTEGER,
	"name"	TEXT NOT NULL UNIQUE,
	"sort_order"	INTEGER,
	PRIMARY KEY("id" AUTOINCREMENT)
);
CREATE TABLE IF NOT EXISTS "idea_keyword_rules" (
	"id"	INTEGER,
	"idea_type_id"	INTEGER NOT NULL,
	"phrase"	TEXT NOT NULL,
	"weight"	REAL NOT NULL DEFAULT 1.0,
	"is_negative"	INTEGER NOT NULL DEFAULT 0,
	"is_category_hint"	INTEGER NOT NULL DEFAULT 0,
	PRIMARY KEY("id" AUTOINCREMENT),
	UNIQUE("idea_type_id","phrase","is_category_hint"),
	FOREIGN KEY("idea_type_id") REFERENCES "idea_types"("id")
);
CREATE TABLE IF NOT EXISTS "idea_types" (
	"id"	INTEGER,
	"group_id"	INTEGER NOT NULL,
	"name"	TEXT NOT NULL,
	"description"	TEXT,
	"sort_order"	INTEGER,
	UNIQUE("group_id","name"),
	PRIMARY KEY("id" AUTOINCREMENT),
	FOREIGN KEY("group_id") REFERENCES "idea_groups"("id")
);
CREATE TABLE IF NOT EXISTS "run_log" (
	"id"	INTEGER,
	"started_at"	TEXT NOT NULL,
	"finished_at"	TEXT,
	"status"	TEXT,
	"records_fetched"	INTEGER DEFAULT 0,
	"records_inserted"	INTEGER DEFAULT 0,
	"error_message"	TEXT,
	PRIMARY KEY("id" AUTOINCREMENT)
);
CREATE TABLE IF NOT EXISTS "subcategories" (
	"id"	INTEGER,
	"category_id"	INTEGER NOT NULL,
	"name"	TEXT NOT NULL,
	UNIQUE("category_id","name"),
	PRIMARY KEY("id" AUTOINCREMENT),
	FOREIGN KEY("category_id") REFERENCES "categories"("id")
);
CREATE VIEW IF NOT EXISTS v_announcement_ideas AS
SELECT
    s.id AS score_id,
    a.id AS announcement_id,
    a.company_name, a.symbol, a.scrip_code,
    a.subject, a.input_timestamp, a.attachment_url,
    g.name  AS idea_group,
    it.name AS idea_type,
    s.score, s.matched_keywords
FROM announcement_idea_scores s
JOIN announcements a ON a.id = s.announcement_id
JOIN idea_types    it ON it.id = s.idea_type_id
JOIN idea_groups    g ON g.id = it.group_id;
CREATE VIEW IF NOT EXISTS v_announcements AS
            SELECT
                a.id, a.script_id, a.scrip_code, a.symbol, a.company_name,
                c.name  AS category,
                sc.name AS subcategory,
                a.subject, a.file_name, a.input_timestamp, a.attachment_url,
                a.fetched_at, a.raw_json
            FROM announcements a
            LEFT JOIN categories    c  ON c.id  = a.category_id
            LEFT JOIN subcategories sc ON sc.id = a.subcategory_id;
CREATE INDEX IF NOT EXISTS "idx_bse_eq_code" ON "announcements" (
	"scrip_code"
);
CREATE INDEX IF NOT EXISTS "idx_bse_eq_subcat_category" ON "subcategories" (
	"category_id"
);
CREATE INDEX IF NOT EXISTS "idx_bse_eq_symbol" ON "announcements" (
	"symbol"
);
CREATE INDEX IF NOT EXISTS "idx_bse_eq_timestamp" ON "announcements" (
	"input_timestamp"
);
CREATE UNIQUE INDEX IF NOT EXISTS "idx_bse_eq_unique" ON "announcements" (
	"script_id",
	"file_name",
	"input_timestamp"
);
CREATE INDEX IF NOT EXISTS "idx_idea_scores_ann" ON "announcement_idea_scores" (
	"announcement_id"
);
CREATE INDEX IF NOT EXISTS "idx_idea_scores_score" ON "announcement_idea_scores" (
	"score"
);
CREATE INDEX IF NOT EXISTS "idx_idea_scores_type" ON "announcement_idea_scores" (
	"idea_type_id"
);
CREATE INDEX IF NOT EXISTS "idx_idea_scores_type_score" ON "announcement_idea_scores" (
	"idea_type_id",
	"score"	DESC
);
COMMIT;
