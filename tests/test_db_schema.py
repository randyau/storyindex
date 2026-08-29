from storyindex import db


def _cols(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_wal_mode_enabled(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_new_tables_exist(conn):
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"jobs", "prompts", "stories_fts"} <= names


def test_additive_columns_present(conn):
    assert "job_id" in _cols(conn, "tag_candidates")
    assert "job_id" in _cols(conn, "story_tags")
    assert "status" in _cols(conn, "stories")
    assert "removed_at" in _cols(conn, "stories")


def test_reconnect_is_idempotent(tmp_path):
    path = tmp_path / "reconnect.sqlite"
    db.connect(path).close()
    # second connect must not raise (ALTER TABLE / FTS bootstrap guarded)
    conn2 = db.connect(path)
    assert "job_id" in _cols(conn2, "story_tags")
    conn2.close()


def test_stories_default_status_active(conn, make_sig):
    db.upsert_story(conn, make_sig("s1"))
    row = conn.execute("SELECT status FROM stories WHERE id='s1'").fetchone()
    assert row[0] == "active"
