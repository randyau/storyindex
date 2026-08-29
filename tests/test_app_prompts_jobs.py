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
    # the job inline instead, monkeypatching the model call.
    from storyindex import jobs as jobs_module
    monkeypatch.setattr(jobs_module, "extract_tags", lambda sig, model, prompt_text, host=None: ["x"])
    monkeypatch.setattr("storyindex.app._spawn_job", lambda job_id: jobs_module.run_extract_job(dbpath, job_id))

    client = _client(dbpath)
    r = client.post("/jobs/extract", data={"prompt_id": str(prompt_id), "model": "m", "scope": "all"})
    assert r.status_code == 302

    conn = db.connect(dbpath)
    job = db.list_jobs(conn)[0]
    assert job["status"] == "done"
    assert job["done"] == 1
    conn.close()


def test_cancel_job_route_marks_failed_and_signals_process(tmp_path, monkeypatch):
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
    assert killed == [(999999, __import__("signal").SIGTERM)]

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
