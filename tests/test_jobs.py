import datetime
import sqlite3

import pytest

from storyindex import db, jobs
from storyindex.classify import ExtractionError
from storyindex.cluster import Cluster


def _now():
    return datetime.datetime.utcnow().isoformat() + "Z"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "jobs.sqlite"


def _seed_stories(conn, make_sig, n=3):
    for i in range(n):
        db.upsert_story(conn, make_sig(f"s{i}", title=f"Story {i}"))
    conn.commit()


def test_extract_job_happy_path(db_path, make_sig, monkeypatch):
    conn = db.connect(db_path)
    _seed_stories(conn, make_sig, n=3)
    prompt_id = db.create_prompt(conn, "default", "extract tags from {body_text}", _now())
    job_id = db.create_job(conn, "extract", _now(), prompt_id=prompt_id, model="fake-model", scope="all")
    conn.commit()
    conn.close()

    monkeypatch.setattr(jobs, "extract_tags", lambda sig, model, prompt_text, host=None: ["alpha", "beta"])

    jobs.run_extract_job(db_path, job_id)

    conn = db.connect(db_path)
    job = db.get_job(conn, job_id)
    conn.row_factory = sqlite3.Row
    assert job["status"] == "done"
    assert job["total"] == 3
    assert job["done"] == 3
    assert job["failed"] == 0

    candidates = conn.execute("SELECT * FROM tag_candidates").fetchall()
    assert len(candidates) == 6  # 3 stories x 2 tags
    assert all(c["job_id"] == job_id for c in candidates)
    conn.close()


def test_extract_job_no_prompt_marks_failed(db_path, make_sig):
    conn = db.connect(db_path)
    _seed_stories(conn, make_sig, n=1)
    job_id = db.create_job(conn, "extract", _now(), prompt_id=None, model="fake-model", scope="all")
    conn.commit()
    conn.close()

    jobs.run_extract_job(db_path, job_id)

    conn = db.connect(db_path)
    job = db.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["error"]
    conn.close()


def test_extract_job_partial_failures_still_completes(db_path, make_sig, monkeypatch):
    conn = db.connect(db_path)
    _seed_stories(conn, make_sig, n=3)
    prompt_id = db.create_prompt(conn, "default", "text", _now())
    job_id = db.create_job(conn, "extract", _now(), prompt_id=prompt_id, model="m", scope="all")
    conn.commit()
    conn.close()

    calls = {"n": 0}

    def flaky(sig, model, prompt_text, host=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ExtractionError("boom")
        return ["tag"]

    monkeypatch.setattr(jobs, "extract_tags", flaky)
    jobs.run_extract_job(db_path, job_id)

    conn = db.connect(db_path)
    job = db.get_job(conn, job_id)
    assert job["status"] == "done"
    assert job["total"] == 3
    assert job["done"] == 2
    assert job["failed"] == 1
    errors = db.list_job_errors(conn, job_id)
    assert len(errors) == 1
    assert "boom" in errors[0]["error"]
    conn.close()


def test_extract_job_scope_untagged_only(db_path, make_sig, monkeypatch):
    conn = db.connect(db_path)
    _seed_stories(conn, make_sig, n=2)
    now = _now()
    tag_id = db.get_or_create_tag(conn, "existing", now)
    db.link_story_tag(conn, "s0", tag_id, source="human")
    prompt_id = db.create_prompt(conn, "default", "text", now)
    job_id = db.create_job(conn, "extract", now, prompt_id=prompt_id, model="m", scope="untagged")
    conn.commit()
    conn.close()

    monkeypatch.setattr(jobs, "extract_tags", lambda sig, model, prompt_text, host=None: ["x"])
    jobs.run_extract_job(db_path, job_id)

    conn = db.connect(db_path)
    job = db.get_job(conn, job_id)
    conn.row_factory = sqlite3.Row
    assert job["total"] == 1  # only s1, s0 already tagged
    candidates = conn.execute("SELECT story_id FROM tag_candidates").fetchall()
    assert [c["story_id"] for c in candidates] == ["s1"]
    conn.close()


def test_cluster_job_folds_candidates_into_tags(db_path, make_sig, monkeypatch):
    conn = db.connect(db_path)
    _seed_stories(conn, make_sig, n=2)
    now = _now()
    db.insert_candidates(conn, "s0", ["scary story"], "p", "m", now)
    db.insert_candidates(conn, "s1", ["scary story"], "p", "m", now)
    job_id = db.create_job(conn, "cluster", now, model="fake-embed", scope=None)
    conn.commit()
    conn.close()

    def fake_cluster(texts, model, host=None):
        c = Cluster()
        for t in texts:
            c.add(t, [1.0])
        return [c]

    monkeypatch.setattr(jobs, "cluster_tag_texts", fake_cluster)
    jobs.run_cluster_job(db_path, job_id)

    conn = db.connect(db_path)
    job = db.get_job(conn, job_id)
    conn.row_factory = sqlite3.Row
    assert job["status"] == "done"

    tags = conn.execute("SELECT * FROM tags").fetchall()
    assert len(tags) == 1
    assert tags[0]["name"] == "scary story"

    links = conn.execute("SELECT * FROM story_tags").fetchall()
    assert len(links) == 2
    assert all(l["job_id"] == job_id for l in links)
    assert all(l["source"] == "model" for l in links)

    remaining = conn.execute(
        "SELECT COUNT(*) FROM tag_candidates WHERE status='candidate'"
    ).fetchone()[0]
    assert remaining == 0
    conn.close()
