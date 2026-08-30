import time

from storyindex import classify, db
from storyindex.app import app


def _client(db_path):
    app.config["DB_PATH"] = db_path
    return app.test_client()


def test_prompts_list_seeds_default_when_empty(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    client = _client(dbpath)
    r = client.get("/prompts")
    assert r.status_code == 200

    conn = db.connect(dbpath)
    assert len(db.list_prompts(conn)) == 1
    conn.close()


def test_create_prompt_then_appears_in_list(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    client = _client(dbpath)
    r = client.post("/prompts/new", data={"name": "pets tagger", "text": "find pets in {body_text}"})
    assert r.status_code == 302
    r = client.get("/prompts")
    assert "pets tagger" in r.get_data(as_text=True)


def test_prompts_list_filters_by_name(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.create_prompt(conn, "pets tagger", "find pets", "2026-01-01T00:00:00Z")
    db.create_prompt(conn, "sci-fi tagger", "find sci-fi", "2026-01-01T00:00:00Z")
    conn.commit(); conn.close()

    client = _client(dbpath)
    r = client.get("/prompts?q=pets")
    body = r.get_data(as_text=True)
    assert "pets tagger" in body
    assert "sci-fi tagger" not in body


def test_prompts_list_paginates(tmp_path, monkeypatch):
    from storyindex import app as app_module
    monkeypatch.setattr(app_module, "PROMPTS_PAGE_SIZE", 1)

    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.create_prompt(conn, "first", "text", "2026-01-01T00:00:00Z")
    db.create_prompt(conn, "second", "text", "2026-01-02T00:00:00Z")
    conn.commit(); conn.close()

    client = _client(dbpath)
    r = client.get("/prompts")
    body = r.get_data(as_text=True)
    assert "second" in body
    assert "first" not in body
    assert "next" in body

    r = client.get("/prompts?page=2")
    body = r.get_data(as_text=True)
    assert "first" in body
    assert "second" not in body


def test_preview_prompt_random_sample(tmp_path, make_sig, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="A Story"))
    prompt_id = db.create_prompt(conn, "p", "text", "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    monkeypatch.setattr(classify, "extract_tags", lambda sig, model, prompt_text, host=None: ["mystery"])
    client = _client(dbpath)
    r = client.post(f"/prompts/{prompt_id}/preview", data={"model": "m", "sample_size": "5"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "A Story" in body
    assert "mystery" in body


def test_preview_prompt_on_specific_story(tmp_path, make_sig, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="Target Story"))
    db.upsert_story(conn, make_sig("s2", title="Other Story"))
    prompt_id = db.create_prompt(conn, "p", "text", "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    monkeypatch.setattr(classify, "extract_tags", lambda sig, model, prompt_text, host=None: ["theme-x"])
    client = _client(dbpath)
    r = client.post(
        "/story/s1/prompts/preview",
        data={"prompt_id": str(prompt_id), "model": "m"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Target Story" in body
    assert "Other Story" not in body
    assert "theme-x" in body


def test_jobs_list_seeds_and_renders(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    client = _client(dbpath)
    r = client.get("/jobs")
    assert r.status_code == 200
    assert "no jobs yet" in r.get_data(as_text=True)


def test_jobs_list_filters_by_status_and_type(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    j1 = db.create_job(conn, "sync", "2026-01-01T00:00:00Z")
    j2 = db.create_job(conn, "extract", "2026-01-01T00:00:00Z")
    db.mark_job_done(conn, j2, "2026-01-01T00:01:00Z")
    conn.commit(); conn.close()

    client = _client(dbpath)
    r = client.get("/jobs?type=sync")
    body = r.get_data(as_text=True)
    assert f">{j1}<" in body
    assert f">{j2}<" not in body

    r = client.get("/jobs?status=done")
    body = r.get_data(as_text=True)
    assert f">{j2}<" in body
    assert f">{j1}<" not in body


def test_create_extract_job_runs_to_completion(tmp_path, make_sig, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1"))
    prompt_id = db.create_prompt(conn, "p", "text", "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    # Avoid spawning a real subprocess (no PYTHONPATH-visible package in a
    # bare test env, and no point testing subprocess mechanics here) - run
    # the job inline instead, monkeypatching the model call. extract jobs
    # go through _ensure_scheduler_running (not _spawn_job) since they're
    # picked up by the shared scheduler - stand in for it here by running
    # the just-created job directly.
    from storyindex import jobs as jobs_module

    def fake_ensure_scheduler_running():
        conn2 = db.connect(dbpath)
        row = conn2.execute(
            "SELECT id FROM jobs WHERE type='extract' AND status='queued' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn2.close()
        if row:
            jobs_module.run_extract_job(dbpath, row[0])

    monkeypatch.setattr(jobs_module, "extract_tags", lambda sig, model, prompt_text, host=None: ["x"])
    monkeypatch.setattr("storyindex.app._ensure_scheduler_running", fake_ensure_scheduler_running)

    client = _client(dbpath)
    r = client.post("/jobs/extract", data={"prompt_id": str(prompt_id), "model": "m", "scope": "all"})
    assert r.status_code == 302

    conn = db.connect(dbpath)
    job = db.list_jobs(conn)[0]
    assert job["status"] == "done"
    assert job["done"] == 1
    conn.close()


def test_cancel_job_route_marks_failed_and_signals_process(tmp_path, monkeypatch):
    # cluster/sync jobs still run as their own dedicated subprocess, so
    # cancelling one should SIGTERM its recorded pid.
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "cluster", "2026-01-01T00:00:00Z")
    db.mark_job_running(conn, job_id, "2026-01-01T00:00:00Z", pid=999999)
    conn.commit()
    conn.close()

    killed = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))

    client = _client(dbpath)
    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 302
    assert killed == [(999999, __import__("signal").SIGTERM)]

    conn = db.connect(dbpath)
    job = db.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["error"] == "cancelled by user"
    conn.close()


def test_cancel_extract_job_does_not_kill_shared_scheduler_pid(tmp_path, monkeypatch):
    # extract jobs are all handled by one shared scheduler process (see
    # app._ensure_scheduler_running) - killing its pid on a single job's
    # cancel would take down every other extract job it's working too.
    # Marking the row failed is enough; the scheduler drops it on its own.
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z")
    db.mark_job_running(conn, job_id, "2026-01-01T00:00:00Z", pid=999999)
    conn.commit()
    conn.close()

    killed = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))

    client = _client(dbpath)
    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 302
    assert killed == []

    conn = db.connect(dbpath)
    job = db.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["error"] == "cancelled by user"
    conn.close()


def test_job_status_json_endpoint(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.get(f"/jobs/{job_id}/status.json")
    assert r.status_code == 200
    assert r.get_json()["status"] == "queued"


def test_job_status_json_respawns_scheduler_if_it_died(tmp_path, monkeypatch):
    # If the scheduler process dies mid-session (crash, OOM-kill) while the
    # app keeps running, nothing else would ever notice a still-queued
    # extract job - it has no pid of its own to go stale. Polling a job's
    # status page is the natural place to self-heal since the UI already
    # hits this endpoint on an interval while a job is in flight.
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr("storyindex.app._ensure_scheduler_running", lambda: calls.append(1))

    client = _client(dbpath)
    r = client.get(f"/jobs/{job_id}/status.json")
    assert r.status_code == 200
    assert calls == [1]


def test_jobs_list_respawns_scheduler_if_it_died(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr("storyindex.app._ensure_scheduler_running", lambda: calls.append(1))

    client = _client(dbpath)
    r = client.get("/jobs")
    assert r.status_code == 200
    assert calls == [1]


def test_job_status_json_does_not_spawn_scheduler_when_nothing_pending(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    db.mark_job_running(conn, job_id, "2026-01-01T00:00:00Z", pid=1)
    db.mark_job_done(conn, job_id, "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr("storyindex.app._ensure_scheduler_running", lambda: calls.append(1))

    client = _client(dbpath)
    r = client.get(f"/jobs/{job_id}/status.json")
    assert r.status_code == 200
    assert calls == []


def test_finished_extract_job_points_to_clustering_not_review(tmp_path):
    # story_tags.job_id is set by whichever cluster job links a tag, never
    # by the extract job that only wrote tag_candidates - a "review the
    # tags this job proposed" link keyed on the extract job's id would
    # always be empty. The done page should point at clustering instead.
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    db.mark_job_running(conn, job_id, "2026-01-01T00:00:00Z", pid=1)
    db.set_job_total(conn, job_id, 1)
    db.increment_job_progress(conn, job_id, done=1)
    db.mark_job_done(conn, job_id, "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    client = _client(dbpath)
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert f"/review?job_id={job_id}" not in body
    assert "start a clustering pass" in body


def test_finished_cluster_job_links_to_its_review_queue(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "cluster", "2026-01-01T00:00:00Z")
    db.mark_job_running(conn, job_id, "2026-01-01T00:00:00Z", pid=1)
    db.mark_job_done(conn, job_id, "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    client = _client(dbpath)
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert f"/review?job_id={job_id}" in body
