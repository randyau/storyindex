"""SQLite schema and access for the story index.

Kept deliberately minimal for the extraction-pass phase: stories and
tag_candidates are populated by the classifier. tags / story_tags are
defined now so the normalization + human-review pass (built next) has
somewhere to land without a schema migration.
"""

from __future__ import annotations

import html
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id              TEXT PRIMARY KEY,
    group_id        TEXT NOT NULL,
    part_index      INTEGER NOT NULL,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL,
    body_text       TEXT NOT NULL,
    source_relpath  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    ingested_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stories_group ON stories(group_id);

CREATE TABLE IF NOT EXISTS tag_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT NOT NULL REFERENCES stories(id),
    tag_text        TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    model           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'clustered', 'approved', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_tag_candidates_story ON tag_candidates(story_id);
CREATE INDEX IF NOT EXISTS idx_tag_candidates_status ON tag_candidates(status);

-- Canonical tag vocabulary. Populated by the normalization pass, not the
-- extraction pass.
CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS story_tags (
    story_id    TEXT NOT NULL REFERENCES stories(id),
    tag_id      INTEGER NOT NULL REFERENCES tags(id),
    confidence  REAL,
    source      TEXT NOT NULL CHECK (source IN ('model', 'human')),
    PRIMARY KEY (story_id, tag_id)
);

-- Site-provided tags: a separate storage system from tags/story_tags above.
-- Populated straight from StorySignature.tags at ingest time, treated as
-- read-only in the UI. See docs/crawler-parser-contract.md section 3a.
CREATE TABLE IF NOT EXISTS site_tags (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS story_site_tags (
    story_id    TEXT NOT NULL REFERENCES stories(id),
    code        TEXT NOT NULL REFERENCES site_tags(code),
    PRIMARY KEY (story_id, code)
);

-- Versioned, saved-for-reuse prompt library. Every save is a new row
-- (never edited in place) so a job run against prompt #7 stays
-- reproducible even after the text is tweaked into #8.
CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    text        TEXT NOT NULL,
    based_on_id INTEGER REFERENCES prompts(id),
    created_at  TEXT NOT NULL
);

-- A tagging/clustering/sync run. First-class so its output (tag_candidates
-- / story_tags rows tagged with this job_id) can be monitored while
-- running and reverted as a unit later.
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL CHECK (type IN ('extract', 'cluster', 'sync')),
    status      TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    prompt_id   INTEGER REFERENCES prompts(id),
    model       TEXT,
    scope       TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    pid         INTEGER,
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Per-item failure detail for a job (which story, why) — the jobs.failed
-- counter alone doesn't say which stories or what went wrong.
CREATE TABLE IF NOT EXISTS job_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    story_ref   TEXT NOT NULL,
    error       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_errors_job ON job_errors(job_id);
"""

# FTS5 external-content index over stories, created lazily (see _ensure_fts)
# so a freshly-added table on an existing DB gets backfilled once via
# 'rebuild' instead of only covering rows inserted from here on.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE stories_fts USING fts5(
    title, author, body_text, content='stories', content_rowid='rowid'
);

CREATE TRIGGER stories_ai AFTER INSERT ON stories BEGIN
    INSERT INTO stories_fts(rowid, title, author, body_text)
    VALUES (new.rowid, new.title, new.author, new.body_text);
END;

CREATE TRIGGER stories_ad AFTER DELETE ON stories BEGIN
    INSERT INTO stories_fts(stories_fts, rowid, title, author, body_text)
    VALUES ('delete', old.rowid, old.title, old.author, old.body_text);
END;

CREATE TRIGGER stories_au AFTER UPDATE ON stories BEGIN
    INSERT INTO stories_fts(stories_fts, rowid, title, author, body_text)
    VALUES ('delete', old.rowid, old.title, old.author, old.body_text);
    INSERT INTO stories_fts(rowid, title, author, body_text)
    VALUES (new.rowid, new.title, new.author, new.body_text);
END;
"""

# Additive columns on tables that predate this schema revision. No
# migration framework for a single local sqlite file — just guard each
# ALTER TABLE so re-running against an already-migrated DB is a no-op.
_ADDITIVE_COLUMNS = [
    ("tag_candidates", "job_id", "INTEGER REFERENCES jobs(id)"),
    ("story_tags", "job_id", "INTEGER REFERENCES jobs(id)"),
    ("stories", "status", "TEXT NOT NULL DEFAULT 'active'"),
    ("stories", "removed_at", "TEXT"),
    ("jobs", "reverted_at", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, coldef in _ADDITIVE_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")


def _ensure_fts(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='stories_fts'"
    ).fetchone()
    if row is None:
        conn.executescript(FTS_SCHEMA)
        conn.execute("INSERT INTO stories_fts(stories_fts) VALUES ('rebuild')")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # NORMAL is the documented-safe pairing with WAL: a crash or power loss
    # can lose the last not-yet-checkpointed transaction, but cannot corrupt
    # the database file itself (unlike journal_mode=DELETE, where a badly
    # timed power loss can). Combined with the job runner's periodic commits
    # (see jobs.COMMIT_EVERY), interruption loses at most one small batch of
    # in-flight work, never the DB as a whole.
    conn.execute("PRAGMA synchronous = NORMAL")
    # A job subprocess and the Flask reader can briefly contend for the
    # single WAL writer slot; wait instead of raising "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    _ensure_fts(conn)
    conn.commit()
    return conn


def upsert_story(conn: sqlite3.Connection, sig) -> None:
    conn.execute(
        """
        INSERT INTO stories
            (id, group_id, part_index, title, author, body_text,
             source_relpath, content_hash, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            group_id=excluded.group_id,
            part_index=excluded.part_index,
            title=excluded.title,
            author=excluded.author,
            body_text=excluded.body_text,
            source_relpath=excluded.source_relpath,
            content_hash=excluded.content_hash,
            ingested_at=excluded.ingested_at
        """,
        (
            sig.id,
            sig.group_id,
            sig.part_index,
            sig.title,
            sig.author,
            sig.body_text,
            sig.source_relpath,
            sig.content_hash,
            sig.ingested_at,
        ),
    )
    for code in getattr(sig, "tags", ()):
        upsert_site_tag(conn, code)
        link_story_site_tag(conn, sig.id, code)


def has_candidates(conn: sqlite3.Connection, story_id: str, prompt_version: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tag_candidates WHERE story_id = ? AND prompt_version = ? LIMIT 1",
        (story_id, prompt_version),
    ).fetchone()
    return row is not None


def insert_candidates(
    conn: sqlite3.Connection,
    story_id: str,
    tags: list[str],
    prompt_version: str,
    model: str,
    created_at: str,
    job_id: int | None = None,
) -> None:
    conn.executemany(
        """
        INSERT INTO tag_candidates (story_id, tag_text, prompt_version, model, created_at, job_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(story_id, tag, prompt_version, model, created_at, job_id) for tag in tags],
    )


# --- normalization / clustering pass -------------------------------------

def pending_candidate_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All tag_candidates rows still in status='candidate', i.e. not yet
    folded into the canonical tags table by the clustering pass."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, story_id, tag_text FROM tag_candidates WHERE status = 'candidate'"
    ).fetchall()
    conn.row_factory = None
    return rows


def get_or_create_tag(conn: sqlite3.Connection, name: str, created_at: str) -> int:
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row[0]
    cur = conn.execute(
        "INSERT INTO tags (name, description, created_at) VALUES (?, NULL, ?)",
        (name, created_at),
    )
    return cur.lastrowid


def link_story_tag(
    conn: sqlite3.Connection,
    story_id: str,
    tag_id: int,
    source: str,
    confidence: float | None = None,
    job_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO story_tags (story_id, tag_id, confidence, source, job_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(story_id, tag_id) DO NOTHING
        """,
        (story_id, tag_id, confidence, source, job_id),
    )


def mark_candidate_clustered(conn: sqlite3.Connection, candidate_id: int) -> None:
    conn.execute(
        "UPDATE tag_candidates SET status = 'clustered' WHERE id = ?",
        (candidate_id,),
    )


# --- site-provided tags (separate storage system, read-only in the UI) ---

def upsert_site_tag(conn: sqlite3.Connection, code: str, label: str | None = None) -> None:
    """Create the site_tag if missing. If a real label is given, it always
    wins over a previously auto-filled (code-as-label) placeholder."""
    conn.execute(
        """
        INSERT INTO site_tags (code, label) VALUES (?, ?)
        ON CONFLICT(code) DO UPDATE SET label = excluded.label
            WHERE excluded.label != site_tags.code OR site_tags.label = site_tags.code
        """,
        (code, label or code),
    )


def link_story_site_tag(conn: sqlite3.Connection, story_id: str, code: str) -> None:
    conn.execute(
        """
        INSERT INTO story_site_tags (story_id, code) VALUES (?, ?)
        ON CONFLICT(story_id, code) DO NOTHING
        """,
        (story_id, code),
    )


def load_site_tag_vocab(conn: sqlite3.Connection, vocab: dict[str, str]) -> None:
    """Backfill human-readable labels for site_tags from a code -> label
    vocabulary (e.g. parsed once from a source site's own category index)."""
    for code, label in vocab.items():
        upsert_site_tag(conn, code, label)


def site_tags_for_story(conn: sqlite3.Connection, story_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT st.code, t.label
        FROM story_site_tags st
        JOIN site_tags t ON t.code = st.code
        WHERE st.story_id = ?
        ORDER BY t.label ASC
        """,
        (story_id,),
    ).fetchall()
    conn.row_factory = None
    return rows


def list_site_tags_with_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT t.code, t.label, COUNT(st.story_id) AS story_count
        FROM site_tags t
        LEFT JOIN story_site_tags st ON st.code = t.code
        GROUP BY t.code
        ORDER BY story_count DESC, t.label ASC
        """
    ).fetchall()
    conn.row_factory = None
    return rows


def get_site_tag(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT code, label FROM site_tags WHERE code = ?", (code,)).fetchone()
    conn.row_factory = None
    return row


def stories_for_site_tag(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.id, s.group_id, s.part_index, s.title, s.author
        FROM stories s
        JOIN story_site_tags st ON st.story_id = s.id
        WHERE st.code = ?
        ORDER BY s.title ASC, s.part_index ASC
        """,
        (code,),
    ).fetchall()
    conn.row_factory = None
    return rows


# --- prompt library --------------------------------------------------------

def create_prompt(
    conn: sqlite3.Connection,
    name: str,
    text: str,
    created_at: str,
    based_on_id: int | None = None,
) -> int:
    """Every save is a new row, never an in-place edit, so a job that ran
    against this prompt stays reproducible even after later tweaks."""
    cur = conn.execute(
        "INSERT INTO prompts (name, text, based_on_id, created_at) VALUES (?, ?, ?, ?)",
        (name, text, based_on_id, created_at),
    )
    return cur.lastrowid


def get_prompt(conn: sqlite3.Connection, prompt_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    conn.row_factory = None
    return row


def list_prompts(
    conn: sqlite3.Connection, q: str | None = None, limit: int | None = None, offset: int = 0
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM prompts"
    params: list = []
    if q:
        sql += " WHERE name LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    conn.row_factory = None
    return rows


def count_prompts(conn: sqlite3.Connection, q: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM prompts"
    params: list = []
    if q:
        sql += " WHERE name LIKE ?"
        params.append(f"%{q}%")
    return conn.execute(sql, params).fetchone()[0]


def random_stories(conn: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM stories WHERE status = 'active' ORDER BY RANDOM() LIMIT ?", (n,)
    ).fetchall()
    conn.row_factory = None
    return rows


def ensure_seed_prompt(conn: sqlite3.Connection, name: str, text: str, created_at: str) -> int:
    """First-run bootstrap: if the prompt library is empty, seed it from a
    given default (e.g. prompts/extract_v1.md) so there's something to run
    a job against before the user has saved one of their own. No-op past
    the first call."""
    row = conn.execute("SELECT id FROM prompts LIMIT 1").fetchone()
    if row is not None:
        return row[0]
    return create_prompt(conn, name, text, created_at)


# --- tagging jobs ------------------------------------------------------

def create_job(
    conn: sqlite3.Connection,
    type: str,
    created_at: str,
    prompt_id: int | None = None,
    model: str | None = None,
    scope: str | None = None,
    total: int = 0,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO jobs (type, status, prompt_id, model, scope, total, created_at)
        VALUES (?, 'queued', ?, ?, ?, ?, ?)
        """,
        (type, prompt_id, model, scope, total, created_at),
    )
    return cur.lastrowid


def mark_job_running(conn: sqlite3.Connection, job_id: int, started_at: str, pid: int) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'running', started_at = ?, pid = ? WHERE id = ?",
        (started_at, pid, job_id),
    )


def set_job_total(conn: sqlite3.Connection, job_id: int, total: int) -> None:
    conn.execute("UPDATE jobs SET total = ? WHERE id = ?", (total, job_id))


def increment_job_progress(conn: sqlite3.Connection, job_id: int, done: int = 0, failed: int = 0) -> None:
    conn.execute(
        "UPDATE jobs SET done = done + ?, failed = failed + ? WHERE id = ?",
        (done, failed, job_id),
    )


def mark_job_done(conn: sqlite3.Connection, job_id: int, finished_at: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = ? WHERE id = ?",
        (finished_at, job_id),
    )


def mark_job_failed(conn: sqlite3.Connection, job_id: int, finished_at: str, error: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
        (finished_at, error, job_id),
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT j.*, p.name AS prompt_name FROM jobs j LEFT JOIN prompts p ON p.id = j.prompt_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    conn.row_factory = None
    return row


def revert_job(conn: sqlite3.Connection, job_id: int, reverted_at: str) -> None:
    """Undo everything a job produced: its story_tags links and
    tag_candidates rows, then any tag left with zero remaining links
    (created solely by this job, now orphaned). The job row itself stays
    (marked reverted_at) as a record that this run happened."""
    conn.execute("DELETE FROM story_tags WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM tag_candidates WHERE job_id = ?", (job_id,))
    conn.execute(
        """
        DELETE FROM tags WHERE id IN (
            SELECT t.id FROM tags t
            LEFT JOIN story_tags st ON st.tag_id = t.id
            WHERE st.tag_id IS NULL
        )
        """
    )
    conn.execute("UPDATE jobs SET reverted_at = ? WHERE id = ?", (reverted_at, job_id))


def reap_stale_jobs(conn: sqlite3.Connection, now: str) -> list[int]:
    """Called unconditionally at app startup: any job still 'running' at
    that point cannot actually be running (this app is a single
    supervising process; if it's starting fresh, whatever process was
    updating that job's progress is gone - crash, kill, power loss).
    Marks them failed with an explanatory error rather than leaving a
    phantom "running" job with a stale pid forever. The rows/commits that
    job already made before dying are untouched and remain valid (see
    connect()'s WAL/synchronous note) - only the job's own status reflects
    that it didn't finish. Returns the ids marked."""
    conn.row_factory = sqlite3.Row
    stale = conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
    conn.row_factory = None
    for row in stale:
        mark_job_failed(conn, row["id"], now, "interrupted (app restarted while this job was running)")
    return [row["id"] for row in stale]


def reap_dead_pid_jobs(conn: sqlite3.Connection, now: str) -> list[int]:
    """Lighter-weight check for the case where the app itself is still up
    but a job's subprocess died without updating its own row (killed,
    crashed). Safe to call often (e.g. on every /jobs view) - only touches
    jobs whose recorded pid is no longer alive."""
    import os

    conn.row_factory = sqlite3.Row
    running = conn.execute(
        "SELECT id, pid FROM jobs WHERE status = 'running' AND pid IS NOT NULL"
    ).fetchall()
    conn.row_factory = None
    reaped = []
    for row in running:
        if not _pid_alive(row["pid"]):
            mark_job_failed(conn, row["id"], now, "interrupted (job process is no longer running)")
            reaped.append(row["id"])
    return reaped


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else - treat as alive
    return True


def record_job_error(conn: sqlite3.Connection, job_id: int, story_ref: str, error: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO job_errors (job_id, story_ref, error, created_at) VALUES (?, ?, ?, ?)",
        (job_id, story_ref, error, created_at),
    )


def list_job_errors(conn: sqlite3.Connection, job_id: int, limit: int = 200) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT story_ref, error, created_at FROM job_errors WHERE job_id = ? ORDER BY id ASC LIMIT ?",
        (job_id, limit),
    ).fetchall()
    conn.row_factory = None
    return rows


def list_jobs(
    conn: sqlite3.Connection,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    type: str | None = None,
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    where = []
    params: list = []
    if status:
        where.append("j.status = ?")
        params.append(status)
    if type:
        where.append("j.type = ?")
        params.append(type)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT j.*, p.name AS prompt_name FROM jobs j
        LEFT JOIN prompts p ON p.id = j.prompt_id
        {where_sql}
        ORDER BY j.created_at DESC LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    conn.row_factory = None
    return rows


def count_jobs(conn: sqlite3.Connection, status: str | None = None, type: str | None = None) -> int:
    where = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if type:
        where.append("type = ?")
        params.append(type)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(f"SELECT COUNT(*) FROM jobs {where_sql}", params).fetchone()[0]


# --- browse / review app --------------------------------------------------

def list_tags_with_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT t.id, t.name, COUNT(st.story_id) AS story_count
        FROM tags t
        LEFT JOIN story_tags st ON st.tag_id = t.id
        GROUP BY t.id
        ORDER BY story_count DESC, t.name ASC
        """
    ).fetchall()
    conn.row_factory = None
    return rows


def stories_for_tag(conn: sqlite3.Connection, tag_id: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.id, s.group_id, s.part_index, s.title, s.author
        FROM stories s
        JOIN story_tags st ON st.story_id = s.id
        WHERE st.tag_id = ?
        ORDER BY s.title ASC, s.part_index ASC
        """,
        (tag_id,),
    ).fetchall()
    conn.row_factory = None
    return rows


def get_tag(conn: sqlite3.Connection, tag_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, name FROM tags WHERE id = ?", (tag_id,)).fetchone()
    conn.row_factory = None
    return row


def get_story(conn: sqlite3.Connection, story_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    conn.row_factory = None
    return row


def get_group_parts(conn: sqlite3.Connection, group_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, part_index, title FROM stories WHERE group_id = ? ORDER BY part_index ASC",
        (group_id,),
    ).fetchall()
    conn.row_factory = None
    return rows


def tags_for_story(conn: sqlite3.Connection, story_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT t.id, t.name, st.source, st.confidence
        FROM tags t
        JOIN story_tags st ON st.tag_id = t.id
        WHERE st.story_id = ?
        ORDER BY t.name ASC
        """,
        (story_id,),
    ).fetchall()
    conn.row_factory = None
    return rows


def search_stories(
    conn: sqlite3.Connection, query: str, limit: int = 100, offset: int = 0
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT id, group_id, part_index, title, author
        FROM stories
        WHERE (title LIKE ? OR author LIKE ?) AND status = 'active'
        ORDER BY title ASC
        LIMIT ? OFFSET ?
        """,
        (like, like, limit, offset),
    ).fetchall()
    conn.row_factory = None
    return rows


def list_stories(conn: sqlite3.Connection, limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, group_id, part_index, title, author FROM stories "
        "WHERE status = 'active' ORDER BY title ASC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    conn.row_factory = None
    return rows


def search_stories_fts(
    conn: sqlite3.Connection, query: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Full-text search over title/author/body via the stories_fts index.
    Returns plain dicts (not sqlite3.Row) since each includes a synthesized
    snippet column alongside the joined story fields."""
    # FTS5 query syntax treats bare punctuation specially; a plain keyword
    # search should never 500 on a stray quote/paren, so quote each term.
    terms = query.split()
    if not terms:
        return []
    match_expr = " ".join('"' + t.replace('"', '""') + '"' for t in terms)

    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.group_id, s.part_index, s.title, s.author,
                   snippet(stories_fts, 2, '\x01', '\x02', '…', 12) AS snippet
            FROM stories_fts
            JOIN stories s ON s.rowid = stories_fts.rowid
            WHERE stories_fts MATCH ? AND s.status = 'active'
            ORDER BY rank
            LIMIT ? OFFSET ?
            """,
            (match_expr, limit, offset),
        ).fetchall()
    except sqlite3.OperationalError:
        # malformed FTS5 query (e.g. a lone operator-like token) - no results
        # rather than a 500 for what's ultimately just a search box.
        rows = []
    conn.row_factory = None

    results = []
    for r in rows:
        d = dict(r)
        # snippet() interleaves our \x01/\x02 markers with raw story text;
        # escape everything first, then turn only our own markers into
        # <mark> tags, so raw story content can never inject HTML.
        escaped = html.escape(d["snippet"])
        d["snippet"] = escaped.replace("\x01", "<mark>").replace("\x02", "</mark>")
        results.append(d)
    return results


def search_stories(
    conn: sqlite3.Connection,
    query: str = "",
    include_tag_ids: list[int] | None = None,
    exclude_tag_ids: list[int] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Browse/search combining an optional keyword search with tag
    intersection (must have every include tag) and exclusion (must have
    none of the exclude tags) - e.g. "battle of wits" AND NOT "sherlock
    holmes". Superset of search_stories_fts/list_stories; those stay as
    simpler standalone helpers since they're independently useful/tested."""
    include_tag_ids = include_tag_ids or []
    exclude_tag_ids = exclude_tag_ids or []
    params: list = []

    if query.strip():
        terms = query.split()
        match_expr = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = """
            SELECT s.id, s.group_id, s.part_index, s.title, s.author,
                   snippet(stories_fts, 2, '\x01', '\x02', '…', 12) AS snippet
            FROM stories_fts
            JOIN stories s ON s.rowid = stories_fts.rowid
            WHERE stories_fts MATCH ? AND s.status = 'active'
        """
        params.append(match_expr)
        order_by = "ORDER BY rank"
    else:
        sql = "SELECT s.id, s.group_id, s.part_index, s.title, s.author, NULL AS snippet FROM stories s WHERE s.status = 'active'"
        order_by = "ORDER BY s.title ASC"

    for tag_id in include_tag_ids:
        sql += " AND s.id IN (SELECT story_id FROM story_tags WHERE tag_id = ?)"
        params.append(tag_id)
    if exclude_tag_ids:
        placeholders = ",".join("?" for _ in exclude_tag_ids)
        sql += f" AND s.id NOT IN (SELECT story_id FROM story_tags WHERE tag_id IN ({placeholders}))"
        params.extend(exclude_tag_ids)

    sql += f" {order_by} LIMIT ? OFFSET ?"
    params += [limit, offset]

    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.row_factory = None

    results = []
    for r in rows:
        d = dict(r)
        if d["snippet"]:
            escaped = html.escape(d["snippet"])
            d["snippet"] = escaped.replace("\x01", "<mark>").replace("\x02", "</mark>")
        results.append(d)
    return results


def set_story_status(conn: sqlite3.Connection, story_id: str, status: str) -> None:
    removed_at = _now_iso() if status == "removed" else None
    conn.execute(
        "UPDATE stories SET status = ?, removed_at = ? WHERE id = ?",
        (status, removed_at, story_id),
    )


def _now_iso() -> str:
    import datetime

    return datetime.datetime.utcnow().isoformat() + "Z"


def list_removed_stories(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, title, author, removed_at FROM stories "
        "WHERE status = 'removed' ORDER BY removed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.row_factory = None
    return rows


def stories_by_author(
    conn: sqlite3.Connection, author: str, exclude_group_id: str, limit: int = 10
) -> list[sqlite3.Row]:
    """Other works by the same author, one row per story group (not per
    chapter) - the "more like this" a reader most reliably wants."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, group_id, title, author, part_index
        FROM stories
        WHERE author = ? AND group_id != ? AND part_index = 0 AND status = 'active'
        ORDER BY title ASC
        LIMIT ?
        """,
        (author, exclude_group_id, limit),
    ).fetchall()
    conn.row_factory = None
    return rows


def create_manual_story(
    conn: sqlite3.Connection, story_id: str, title: str, author: str, body_text: str, ingested_at: str
) -> None:
    """A story typed/pasted straight into the UI, no parser involved.
    group_id/source_relpath/content_hash still need real values (other
    code assumes they exist) so we use the story's own id as a
    standalone group and 'manual-entry' as a recognizable relpath."""
    conn.execute(
        """
        INSERT INTO stories
            (id, group_id, part_index, title, author, body_text,
             source_relpath, content_hash, ingested_at)
        VALUES (?, ?, 0, ?, ?, ?, 'manual-entry', ?, ?)
        """,
        (story_id, story_id, title, author, body_text, _sha1(body_text), ingested_at),
    )


def _sha1(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def stories_for_author(conn: sqlite3.Connection, author: str) -> list[sqlite3.Row]:
    """Every story/part by this exact author name, for a dedicated browse
    page (unlike stories_by_author, this doesn't exclude any group)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, group_id, part_index, title, author
        FROM stories
        WHERE author = ? AND status = 'active'
        ORDER BY title ASC, part_index ASC
        """,
        (author,),
    ).fetchall()
    conn.row_factory = None
    return rows


def delete_tag(conn: sqlite3.Connection, tag_id: int) -> None:
    """Removes the tag and every story's link to it. Leaves tag_candidates
    (pre-clustering raw text, a separate pipeline stage) untouched, so a
    later clustering pass can still re-propose it if it's genuinely
    common — this only removes the curated tag, not the model's opinion."""
    conn.execute("DELETE FROM story_tags WHERE tag_id = ?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))


def rename_tag(conn: sqlite3.Connection, tag_id: int, new_name: str) -> None:
    conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, tag_id))


def merge_tags(conn: sqlite3.Connection, src_tag_id: int, dst_tag_id: int) -> None:
    """Repoint every story_tags row from src to dst, then drop src."""
    conn.execute(
        """
        UPDATE OR IGNORE story_tags SET tag_id = ? WHERE tag_id = ?
        """,
        (dst_tag_id, src_tag_id),
    )
    conn.execute("DELETE FROM story_tags WHERE tag_id = ?", (src_tag_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (src_tag_id,))


def delete_story_tag(conn: sqlite3.Connection, story_id: str, tag_id: int) -> None:
    conn.execute(
        "DELETE FROM story_tags WHERE story_id = ? AND tag_id = ?",
        (story_id, tag_id),
    )


def set_story_tag_source(conn: sqlite3.Connection, story_id: str, tag_id: int, source: str) -> None:
    conn.execute(
        "UPDATE story_tags SET source = ? WHERE story_id = ? AND tag_id = ?",
        (source, story_id, tag_id),
    )


def list_tag_names(conn: sqlite3.Connection) -> list[str]:
    """For the add-tag autocomplete — steers humans toward reusing an
    existing tag instead of minting near-duplicates the clustering pass
    would otherwise have to fold back together."""
    rows = conn.execute("SELECT name FROM tags ORDER BY name ASC").fetchall()
    return [r[0] for r in rows]


def count_pending_review(conn: sqlite3.Connection, job_id: int | None = None) -> int:
    if job_id is not None:
        row = conn.execute(
            "SELECT COUNT(DISTINCT story_id) FROM story_tags WHERE source = 'model' AND job_id = ?",
            (job_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(DISTINCT story_id) FROM story_tags WHERE source = 'model'"
        ).fetchone()
    return row[0]


def stories_pending_review(
    conn: sqlite3.Connection, limit: int = 25, offset: int = 0, job_id: int | None = None
) -> list[dict]:
    """Stories that have at least one model-proposed tag still awaiting
    human approval, each with just those pending tags attached — the
    review-queue workflow so a human doesn't have to hunt through browse/
    search to find what still needs a look. Filterable to one job's output
    via job_id, to review a single run in isolation."""
    conn.row_factory = sqlite3.Row
    job_clause = "AND st.job_id = ?" if job_id is not None else ""
    params = (job_id,) if job_id is not None else ()
    story_rows = conn.execute(
        f"""
        SELECT DISTINCT s.id, s.title, s.author
        FROM stories s
        JOIN story_tags st ON st.story_id = s.id AND st.source = 'model' {job_clause}
        ORDER BY s.title ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    result = []
    for s in story_rows:
        tag_rows = conn.execute(
            f"""
            SELECT t.id, t.name
            FROM tags t
            JOIN story_tags st ON st.tag_id = t.id
            WHERE st.story_id = ? AND st.source = 'model' {job_clause}
            ORDER BY t.name ASC
            """,
            (s["id"], *params),
        ).fetchall()
        result.append({"story": s, "tags": tag_rows})
    conn.row_factory = None
    return result


def approve_all_story_tags(conn: sqlite3.Connection, story_id: str) -> None:
    conn.execute(
        "UPDATE story_tags SET source = 'human' WHERE story_id = ? AND source = 'model'",
        (story_id,),
    )


def reject_all_story_tags(conn: sqlite3.Connection, story_id: str) -> None:
    conn.execute(
        "DELETE FROM story_tags WHERE story_id = ? AND source = 'model'",
        (story_id,),
    )


def add_story_tag_by_name(
    conn: sqlite3.Connection, story_id: str, tag_name: str, created_at: str, source: str = "human"
) -> None:
    tag_id = get_or_create_tag(conn, tag_name, created_at)
    link_story_tag(conn, story_id, tag_id, source=source)
