from storyindex import db
from storyindex.app import app


def _client(db_path):
    app.config["DB_PATH"] = db_path
    return app.test_client()


def _seed(conn, make_sig):
    now = "2026-01-01T00:00:00Z"
    db.upsert_story(conn, make_sig("s1", title="Story One"))
    db.upsert_story(conn, make_sig("s2", title="Story Two"))
    db.upsert_story(conn, make_sig("s3", title="Story Three"))
    job1 = db.create_job(conn, "cluster", now)
    job2 = db.create_job(conn, "cluster", now)
    common = db.get_or_create_tag(conn, "common tag", now)
    rare = db.get_or_create_tag(conn, "rare tag", now)
    db.link_story_tag(conn, "s1", common, source="model", job_id=job1)
    db.link_story_tag(conn, "s2", common, source="model", job_id=job1)
    db.link_story_tag(conn, "s3", rare, source="model", job_id=job2)
    conn.commit()
    return job1, job2, common, rare


def test_pending_review_tags_orders_by_story_count_then_name(conn, make_sig):
    _seed(conn, make_sig)
    rows = db.pending_review_tags(conn)
    assert [(r["name"], r["story_count"]) for r in rows] == [("common tag", 2), ("rare tag", 1)]
    assert db.count_pending_review_tags(conn) == 2


def test_pending_review_tags_filters_by_job(conn, make_sig):
    job1, job2, common, rare = _seed(conn, make_sig)
    rows = db.pending_review_tags(conn, job_id=job2)
    assert [r["name"] for r in rows] == ["rare tag"]
    assert db.count_pending_review_tags(conn, job_id=job2) == 1


def test_pending_review_stories_for_tags_batches_in_one_call(conn, make_sig):
    job1, job2, common, rare = _seed(conn, make_sig)
    by_tag = db.pending_review_stories_for_tags(conn, [common, rare])
    assert {s["id"] for s in by_tag[common]} == {"s1", "s2"}
    assert {s["id"] for s in by_tag[rare]} == {"s3"}


def test_pending_review_stories_for_tags_empty_list_returns_empty(conn):
    assert db.pending_review_stories_for_tags(conn, []) == {}


def test_approve_tag_pending_flips_source_for_all_pending_links(conn, make_sig):
    job1, job2, common, rare = _seed(conn, make_sig)
    db.approve_tag_pending(conn, common)
    conn.commit()
    rows = conn.execute("SELECT story_id, source FROM story_tags WHERE tag_id = ?", (common,)).fetchall()
    assert {(r[0], r[1]) for r in rows} == {("s1", "human"), ("s2", "human")}
    # untouched tag stays pending
    assert conn.execute("SELECT source FROM story_tags WHERE tag_id = ?", (rare,)).fetchone()[0] == "model"


def test_approve_tag_pending_scoped_to_job_leaves_other_jobs_alone(conn, make_sig):
    now = "2026-01-01T00:00:00Z"
    db.upsert_story(conn, make_sig("s1"))
    db.upsert_story(conn, make_sig("s2"))
    job1 = db.create_job(conn, "cluster", now)
    job2 = db.create_job(conn, "cluster", now)
    tag = db.get_or_create_tag(conn, "shared", now)
    db.link_story_tag(conn, "s1", tag, source="model", job_id=job1)
    db.link_story_tag(conn, "s2", tag, source="model", job_id=job2)
    conn.commit()

    db.approve_tag_pending(conn, tag, job_id=job1)
    conn.commit()

    rows = {r[0]: r[1] for r in conn.execute("SELECT story_id, source FROM story_tags WHERE tag_id = ?", (tag,))}
    assert rows == {"s1": "human", "s2": "model"}


def test_reject_tag_pending_deletes_matching_links_only(conn, make_sig):
    job1, job2, common, rare = _seed(conn, make_sig)
    db.reject_tag_pending(conn, common)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM story_tags WHERE tag_id = ?", (common,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM story_tags WHERE tag_id = ?", (rare,)).fetchone()[0] == 1


def test_review_page_shows_tags_with_spot_check_stories(tmp_path, make_sig):
    conn = db.connect(tmp_path / "t.sqlite")
    _seed(conn, make_sig)
    conn.close()

    client = _client(tmp_path / "t.sqlite")
    r = client.get("/review")
    body = r.get_data(as_text=True)
    assert "common tag" in body
    assert "rare tag" in body
    assert "Story One" in body and "Story Two" in body and "Story Three" in body


def test_approve_pending_tag_route_redirects_and_commits(tmp_path, make_sig):
    conn = db.connect(tmp_path / "t.sqlite")
    job1, job2, common, rare = _seed(conn, make_sig)
    conn.close()

    client = _client(tmp_path / "t.sqlite")
    r = client.post(f"/review/tags/{common}/approve", data={})
    assert r.status_code == 302

    conn = db.connect(tmp_path / "t.sqlite")
    assert db.count_pending_review_tags(conn) == 1
    conn.close()


def test_reject_pending_tag_route_redirects_and_commits(tmp_path, make_sig):
    conn = db.connect(tmp_path / "t.sqlite")
    job1, job2, common, rare = _seed(conn, make_sig)
    conn.close()

    client = _client(tmp_path / "t.sqlite")
    r = client.post(f"/review/tags/{rare}/reject", data={})
    assert r.status_code == 302

    conn = db.connect(tmp_path / "t.sqlite")
    assert db.count_pending_review_tags(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM story_tags WHERE tag_id = ?", (rare,)).fetchone()[0] == 0
    conn.close()


def test_review_stories_view_still_reachable(tmp_path, make_sig):
    conn = db.connect(tmp_path / "t.sqlite")
    _seed(conn, make_sig)
    conn.close()

    client = _client(tmp_path / "t.sqlite")
    r = client.get("/review/stories")
    assert r.status_code == 200
    assert "Story One" in r.get_data(as_text=True)
