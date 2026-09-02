import os

from storyindex import db


def test_reap_stale_jobs_marks_running_as_failed(conn):
    # sync jobs are still each their own independent subprocess (unlike
    # extract/cluster, which now share the scheduler's pid and are never
    # reaped for a merely-dead pid - see reap_dead_pid_jobs).
    now = "2026-01-01T00:00:00Z"
    job_id = db.create_job(conn, "sync", now)
    db.mark_job_running(conn, job_id, now, pid=999999)
    conn.commit()

    reaped = db.reap_stale_jobs(conn, now)
    conn.commit()

    assert reaped == [job_id]
    job = db.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert "interrupted" in job["error"]


def test_reap_stale_jobs_leaves_done_and_queued_alone(conn):
    now = "2026-01-01T00:00:00Z"
    done_id = db.create_job(conn, "extract", now)
    db.mark_job_running(conn, done_id, now, pid=1)
    db.mark_job_done(conn, done_id, now)
    queued_id = db.create_job(conn, "extract", now)
    conn.commit()

    reaped = db.reap_stale_jobs(conn, now)
    conn.commit()

    assert reaped == []
    assert db.get_job(conn, done_id)["status"] == "done"
    assert db.get_job(conn, queued_id)["status"] == "queued"


def test_reap_stale_jobs_leaves_a_still_alive_detached_job_running(conn):
    # Job subprocesses are detached (start_new_session=True) precisely so
    # they survive an app restart - reap_stale_jobs must not treat "the
    # app just started" as proof the job died, or a real multi-hour
    # extraction run gets wrongly marked failed every time the app
    # restarts while it's still working.
    now = "2026-01-01T00:00:00Z"
    job_id = db.create_job(conn, "extract", now)
    db.mark_job_running(conn, job_id, now, pid=os.getpid())
    conn.commit()

    reaped = db.reap_stale_jobs(conn, now)
    conn.commit()

    assert reaped == []
    assert db.get_job(conn, job_id)["status"] == "running"


def test_reap_dead_pid_jobs_only_touches_dead_pids(conn):
    now = "2026-01-01T00:00:00Z"
    # sync jobs are still each their own independent _spawn_job subprocess
    # (unlike extract/cluster, which now share the scheduler's pid), so a
    # dead pid here really does mean the job died.
    alive_job = db.create_job(conn, "sync", now)
    db.mark_job_running(conn, alive_job, now, pid=os.getpid())  # our own pid: alive

    dead_job = db.create_job(conn, "sync", now)
    # a pid essentially guaranteed not to exist
    db.mark_job_running(conn, dead_job, now, pid=2**30)
    conn.commit()

    reaped = db.reap_dead_pid_jobs(conn, now)
    conn.commit()

    assert reaped == [dead_job]
    assert db.get_job(conn, alive_job)["status"] == "running"
    assert db.get_job(conn, dead_job)["status"] == "failed"


def test_reap_dead_pid_jobs_never_fails_extract_jobs(conn):
    # Every running extract/cluster job's pid column holds the *shared*
    # scheduler process's pid (see scheduler.py), not an independent
    # per-job process - so a dead pid here means "the scheduler needs a
    # respawn" (app._ensure_scheduler_if_jobs_pending), not "this job
    # crashed". Mass-failing every extract/cluster job sharing that pid
    # would discard their resumability.
    now = "2026-01-01T00:00:00Z"
    job_id = db.create_job(conn, "extract", now)
    db.mark_job_running(conn, job_id, now, pid=2**30)  # essentially guaranteed dead
    conn.commit()

    reaped = db.reap_dead_pid_jobs(conn, now)
    conn.commit()

    assert reaped == []
    assert db.get_job(conn, job_id)["status"] == "running"


def test_reap_dead_pid_jobs_never_fails_cluster_jobs(conn):
    # Same reasoning as the extract case above: cluster jobs now also
    # share the scheduler's pid (see scheduler.py generalizing to drive
    # both job types), so a dead scheduler pid must not mass-fail them.
    now = "2026-01-01T00:00:00Z"
    job_id = db.create_job(conn, "cluster", now)
    db.mark_job_running(conn, job_id, now, pid=2**30)  # essentially guaranteed dead
    conn.commit()

    reaped = db.reap_dead_pid_jobs(conn, now)
    conn.commit()

    assert reaped == []
    assert db.get_job(conn, job_id)["status"] == "running"


def test_reconnect_survives_a_stuck_running_job(tmp_path):
    """Simulates a crash: job left 'running' from a prior process, then a
    fresh connect() (as main() does on restart) + reap should leave the DB
    usable, not corrupted, and the job clearly marked failed."""
    path = tmp_path / "crash.sqlite"
    conn1 = db.connect(path)
    now = "2026-01-01T00:00:00Z"
    job_id = db.create_job(conn1, "sync", now)
    db.mark_job_running(conn1, job_id, now, pid=999998)
    conn1.commit()
    conn1.close()  # simulate the process dying without a clean shutdown

    conn2 = db.connect(path)  # fresh process reconnecting
    reaped = db.reap_stale_jobs(conn2, now)
    conn2.commit()
    assert reaped == [job_id]

    # DB is still fully usable afterward
    db.create_job(conn2, "sync", now)
    conn2.commit()
    assert len(db.list_jobs(conn2)) == 2
    conn2.close()
