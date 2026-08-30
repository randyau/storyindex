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
    assert "last_block_seconds" in _cols(conn, "jobs")
    assert "last_block_items" in _cols(conn, "jobs")


def test_record_block_timing_updates_job_row(conn):
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z")
    db.record_block_timing(conn, job_id, 12.5, 5)
    job = db.get_job(conn, job_id)
    assert job["last_block_seconds"] == 12.5
    assert job["last_block_items"] == 5


def test_reconnect_is_idempotent(tmp_path):
    path = tmp_path / "reconnect.sqlite"
    db.connect(path).close()
    # second connect must not raise (ALTER TABLE / FTS bootstrap guarded)
    conn2 = db.connect(path)
    assert "job_id" in _cols(conn2, "story_tags")
    conn2.close()


def test_reconnect_skips_ddl_once_initialized(tmp_path, monkeypatch):
    path = tmp_path / "reconnect2.sqlite"
    db.connect(path).close()

    # If connect() still ran SCHEMA's DDL on this second call, this
    # deliberately-broken script would raise sqlite3.OperationalError.
    monkeypatch.setattr(db, "SCHEMA", "THIS IS NOT VALID SQL;")
    conn2 = db.connect(path)
    conn2.close()


def test_is_initialized_false_for_fresh_connection(tmp_path):
    import sqlite3

    path = tmp_path / "bare.sqlite"
    conn = sqlite3.connect(path)
    assert db._is_initialized(conn) is False
    conn.close()


def test_connect_creates_missing_parent_directory(tmp_path):
    # library/storyindex.sqlite is the default path for the web app and
    # every scripts/*.py tool - a fresh clone won't have library/ yet, so
    # connect() must create it rather than raising sqlite3.OperationalError.
    path = tmp_path / "library" / "storyindex.sqlite"
    assert not path.parent.exists()
    conn = db.connect(path)
    assert path.parent.is_dir()
    conn.close()


def test_stories_default_status_active(conn, make_sig):
    db.upsert_story(conn, make_sig("s1"))
    row = conn.execute("SELECT status FROM stories WHERE id='s1'").fetchone()
    assert row[0] == "active"
