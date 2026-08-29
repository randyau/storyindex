import json

from storyindex import db, jobs as jobs_module
from storyindex.app import app


def _client(tmp_path, dbpath, monkeypatch):
    app.config["DB_PATH"] = dbpath
    app.config["LIBRARIES_PATH"] = tmp_path / "libs.json"
    monkeypatch.setattr("storyindex.app._spawn_job", lambda job_id: jobs_module.run_sync_job(dbpath, job_id))
    return app.test_client()


def test_sync_generic_mode_end_to_end(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "my_first_story.txt").write_text("Once upon a time.", encoding="utf-8")
    dbpath = tmp_path / "t.sqlite"
    client = _client(tmp_path, dbpath, monkeypatch)

    r = client.post("/jobs/sync", data={
        "mode": "generic",
        "archive_root": str(archive_root),
        "glob": "*.txt",
        "tags": "imported, batch1",
    }, follow_redirects=True)
    assert r.status_code == 200

    conn = db.connect(dbpath)
    story = conn.execute("SELECT title FROM stories").fetchone()
    assert story[0] == "My First Story"
    tags = {r[0] for r in conn.execute("SELECT code FROM site_tags")}
    assert tags == {"imported", "batch1"}
    conn.close()


def test_sync_custom_mode_uses_given_adapter(tmp_path, monkeypatch):
    from storyindex.adapters.example_adapter import _SAMPLE_CHAPTER_HTML, _SAMPLE_INDEX_HTML

    archive_root = tmp_path / "archive"
    story_dir = archive_root / "the-example"
    story_dir.mkdir(parents=True)
    (story_dir / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
    (story_dir / "chapter-1.html").write_text(_SAMPLE_CHAPTER_HTML, encoding="utf-8")
    dbpath = tmp_path / "t.sqlite"
    client = _client(tmp_path, dbpath, monkeypatch)

    r = client.post("/jobs/sync", data={
        "mode": "custom",
        "archive_root": str(archive_root),
        "adapter": "storyindex.adapters.example_adapter:ExampleAdapter",
    }, follow_redirects=True)
    assert r.status_code == 200

    conn = db.connect(dbpath)
    story = conn.execute("SELECT title, author FROM stories").fetchone()
    assert story[0] == "The Example"
    assert story[1] == "Jane Doe"
    conn.close()


def test_sync_missing_archive_root_redirects_without_job(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    client = _client(tmp_path, dbpath, monkeypatch)
    r = client.post("/jobs/sync", data={"mode": "generic"})
    assert r.status_code == 302
    conn = db.connect(dbpath)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.close()
