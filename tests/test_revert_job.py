from storyindex import db


def test_revert_job_removes_story_tags_and_candidates(conn, make_sig):
    db.upsert_story(conn, make_sig("s1"))
    now = "2026-01-01T00:00:00Z"
    job_id = db.create_job(conn, "cluster", now)
    db.insert_candidates(conn, "s1", ["orphan-tag"], "p", "m", now, job_id=job_id)
    tag_id = db.get_or_create_tag(conn, "orphan-tag", now)
    db.link_story_tag(conn, "s1", tag_id, source="model", job_id=job_id)
    conn.commit()

    db.revert_job(conn, job_id, now)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM story_tags WHERE job_id=?", (job_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tag_candidates WHERE job_id=?", (job_id,)).fetchone()[0] == 0
    # tag was created solely by this job and now has zero links -> cleaned up
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE name='orphan-tag'").fetchone()[0] == 0

    job = db.get_job(conn, job_id)
    assert job["reverted_at"] == now


def test_revert_job_keeps_tag_still_used_by_another_job(conn, make_sig):
    db.upsert_story(conn, make_sig("s1"))
    db.upsert_story(conn, make_sig("s2"))
    now = "2026-01-01T00:00:00Z"
    job1 = db.create_job(conn, "cluster", now)
    job2 = db.create_job(conn, "cluster", now)
    tag_id = db.get_or_create_tag(conn, "shared-tag", now)
    db.link_story_tag(conn, "s1", tag_id, source="model", job_id=job1)
    db.link_story_tag(conn, "s2", tag_id, source="model", job_id=job2)
    conn.commit()

    db.revert_job(conn, job1, now)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM tags WHERE name='shared-tag'").fetchone()[0] == 1
    remaining = conn.execute("SELECT story_id FROM story_tags WHERE tag_id=?", (tag_id,)).fetchall()
    assert [r[0] for r in remaining] == ["s2"]


def test_revert_job_does_not_touch_human_added_tags(conn, make_sig):
    db.upsert_story(conn, make_sig("s1"))
    now = "2026-01-01T00:00:00Z"
    tag_id = db.get_or_create_tag(conn, "human-tag", now)
    db.link_story_tag(conn, "s1", tag_id, source="human", job_id=None)
    conn.commit()

    job_id = db.create_job(conn, "cluster", now)
    conn.commit()
    db.revert_job(conn, job_id, now)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM story_tags WHERE tag_id=?", (tag_id,)).fetchone()[0] == 1


def test_stories_pending_review_filters_by_job(conn, make_sig):
    db.upsert_story(conn, make_sig("s1"))
    db.upsert_story(conn, make_sig("s2"))
    now = "2026-01-01T00:00:00Z"
    job1 = db.create_job(conn, "cluster", now)
    job2 = db.create_job(conn, "cluster", now)
    t1 = db.get_or_create_tag(conn, "t1", now)
    t2 = db.get_or_create_tag(conn, "t2", now)
    db.link_story_tag(conn, "s1", t1, source="model", job_id=job1)
    db.link_story_tag(conn, "s2", t2, source="model", job_id=job2)
    conn.commit()

    items = db.stories_pending_review(conn, job_id=job1)
    assert [i["story"]["id"] for i in items] == ["s1"]
    assert db.count_pending_review(conn, job_id=job1) == 1
    assert db.count_pending_review(conn) == 2
