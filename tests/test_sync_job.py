import json
import sqlite3

from storyindex import db, jobs
from storyindex.adapters.example_adapter import _SAMPLE_CHAPTER_HTML, _SAMPLE_INDEX_HTML


def _make_archive(tmp_path):
    root = tmp_path / "archive"
    story_dir = root / "the-example"
    story_dir.mkdir(parents=True)
    (story_dir / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
    (story_dir / "chapter-1.html").write_text(_SAMPLE_CHAPTER_HTML, encoding="utf-8")
    (story_dir / "chapter-2.html").write_text(_SAMPLE_CHAPTER_HTML, encoding="utf-8")
    return root


def test_sync_job_ingests_stories(tmp_path):
    archive_root = _make_archive(tmp_path)
    db_path = tmp_path / "sync.sqlite"
    conn = db.connect(db_path)
    scope = json.dumps({
        "adapter": "storyindex.adapters.example_adapter:ExampleAdapter",
        "archive_root": str(archive_root),
    })
    job_id = db.create_job(conn, "sync", "2026-01-01T00:00:00Z", scope=scope)
    conn.commit()
    conn.close()

    jobs.run_sync_job(db_path, job_id)

    conn = db.connect(db_path)
    job = db.get_job(conn, job_id)
    conn.row_factory = sqlite3.Row
    assert job["status"] == "done"
    assert job["total"] == 2
    assert job["done"] == 2

    stories = conn.execute("SELECT title, author, part_index FROM stories ORDER BY part_index").fetchall()
    assert len(stories) == 2
    assert stories[0]["title"] == "The Example"
    assert stories[0]["author"] == "Jane Doe"
    assert stories[0]["part_index"] == 0
    assert stories[1]["part_index"] == 1
    conn.close()


def test_sync_job_is_idempotent_on_rerun(tmp_path):
    archive_root = _make_archive(tmp_path)
    db_path = tmp_path / "sync.sqlite"
    scope = json.dumps({
        "adapter": "storyindex.adapters.example_adapter:ExampleAdapter",
        "archive_root": str(archive_root),
    })

    conn = db.connect(db_path)
    job1 = db.create_job(conn, "sync", "2026-01-01T00:00:00Z", scope=scope)
    conn.commit()
    conn.close()
    jobs.run_sync_job(db_path, job1)

    conn = db.connect(db_path)
    job2 = db.create_job(conn, "sync", "2026-01-01T00:00:00Z", scope=scope)
    conn.commit()
    conn.close()
    jobs.run_sync_job(db_path, job2)

    conn = db.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    assert count == 2  # re-sync upserts, doesn't duplicate
    conn.close()


def test_sync_job_missing_params_fails_cleanly(tmp_path):
    db_path = tmp_path / "sync.sqlite"
    conn = db.connect(db_path)
    job_id = db.create_job(conn, "sync", "2026-01-01T00:00:00Z", scope=json.dumps({}))
    conn.commit()
    conn.close()

    jobs.run_sync_job(db_path, job_id)

    conn = db.connect(db_path)
    job = db.get_job(conn, job_id)
    assert job["status"] == "failed"
    conn.close()
