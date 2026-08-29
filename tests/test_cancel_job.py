from storyindex import db


def test_cancel_job_marks_running_job_failed_and_returns_pid(conn):
    now = "2026-01-01T00:00:00Z"
    job_id = db.create_job(conn, "extract", now)
    db.mark_job_running(conn, job_id, now, pid=12345)
    conn.commit()

    pid = db.cancel_job(conn, job_id, "2026-01-01T00:01:00Z")
    conn.commit()

    assert pid == 12345
    job = db.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["error"] == "cancelled by user"


def test_cancel_job_is_noop_for_already_done_job(conn):
    now = "2026-01-01T00:00:00Z"
    job_id = db.create_job(conn, "extract", now)
    db.mark_job_running(conn, job_id, now, pid=1)
    db.mark_job_done(conn, job_id, now)
    conn.commit()

    pid = db.cancel_job(conn, job_id, "2026-01-01T00:01:00Z")
    conn.commit()

    assert pid is None
    job = db.get_job(conn, job_id)
    assert job["status"] == "done"
