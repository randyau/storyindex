import os

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
    conn.commit()
    conn.close()

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
    conn.commit()
    conn.close()

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

    monkeypatch.setattr(classify, "extract_tags", lambda sig, model, prompt_text, host=None, max_ctx_tokens=None: ["mystery"])
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
    prompt_id = db.create_prompt(conn, "p", "some saved text", "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    monkeypatch.setattr(classify, "extract_tags", lambda sig, model, prompt_text, host=None, max_ctx_tokens=None: ["theme-x"])
    client = _client(dbpath)
    r = client.post(
        "/story/s1/prompts/preview",
        data={"text": "a tweaked ad-hoc prompt", "based_on_id": str(prompt_id), "model": "m"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Target Story" in body
    assert "Other Story" not in body
    assert "theme-x" in body
    assert "a tweaked ad-hoc prompt" in body


def test_preview_prompt_on_story_requires_text(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1"))
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.post("/story/s1/prompts/preview", data={"text": "", "model": "m"})
    assert r.status_code == 404


def test_save_prompt_from_preview_creates_prompt_and_redirects_to_story(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1"))
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.post(
        "/prompts/save-from-preview",
        data={"name": "my tweaked prompt", "text": "find the dragons", "story_id": "s1"},
    )
    assert r.status_code == 302
    assert r.headers["Location"] == "/story/s1"

    conn = db.connect(dbpath)
    prompts = db.list_prompts(conn)
    assert len(prompts) == 1
    assert prompts[0]["name"] == "my tweaked prompt"
    assert prompts[0]["text"] == "find the dragons"


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
    conn.commit()
    conn.close()

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

    monkeypatch.setattr(jobs_module, "extract_tags", lambda sig, model, prompt_text, host=None, max_ctx_tokens=None: ["x"])
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
    # sync jobs still run as their own dedicated subprocess (unlike
    # extract/cluster, which now share the scheduler's pid), so cancelling
    # one should SIGTERM its recorded pid.
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "sync", "2026-01-01T00:00:00Z")
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


def test_cancel_cluster_job_does_not_kill_shared_scheduler_pid(tmp_path, monkeypatch):
    # Same reasoning as the extract case above: cluster jobs now also
    # share the scheduler's pid.
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
    assert killed == []

    conn = db.connect(dbpath)
    job = db.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["error"] == "cancelled by user"
    conn.close()


def test_create_cluster_job_goes_through_shared_scheduler(tmp_path, monkeypatch):
    # cluster jobs used to _spawn_job a dedicated subprocess; they now go
    # through _ensure_scheduler_running like extract jobs, so they share
    # the same model-grouped rotation (see scheduler.py).
    dbpath = tmp_path / "t.sqlite"
    calls = []
    monkeypatch.setattr("storyindex.app._ensure_scheduler_running", lambda: calls.append(1))
    monkeypatch.setattr("storyindex.app._spawn_job", lambda job_id: calls.append(("spawn", job_id)))

    client = _client(dbpath)
    r = client.post("/jobs/cluster", data={"model": "m"})
    assert r.status_code == 302
    assert calls == [1]

    conn = db.connect(dbpath)
    job = db.list_jobs(conn)[0]
    assert job["type"] == "cluster"
    assert job["model"] == "m"
    conn.close()


def test_create_cluster_job_defaults_model_when_blank(tmp_path, monkeypatch):
    # A concrete default (not None) keeps the scheduler's group-by-model
    # sort key stable across cluster jobs left at their default model.
    from storyindex.cluster import DEFAULT_EMBED_MODEL

    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr("storyindex.app._ensure_scheduler_running", lambda: None)

    client = _client(dbpath)
    r = client.post("/jobs/cluster", data={"model": ""})
    assert r.status_code == 302

    conn = db.connect(dbpath)
    job = db.list_jobs(conn)[0]
    assert job["model"] == DEFAULT_EMBED_MODEL
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


def test_format_duration():
    from storyindex.app import _format_duration

    assert _format_duration(30) == "<1m"
    assert _format_duration(90) == "~1m"
    assert _format_duration(3700) == "~1h 1m"
    assert _format_duration(90000) == "~1d 1h"


def test_scheduled_job_etas_sole_job_ignores_sharing(tmp_path):
    from storyindex.app import _scheduled_job_etas

    conn = db.connect(tmp_path / "t.sqlite")
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    db.set_job_total(conn, job_id, 100)
    db.increment_job_progress(conn, job_id, done=10)
    db.record_block_timing(conn, job_id, 10.0, 10)  # 1 item/sec
    conn.commit()
    job = db.get_job(conn, job_id)
    conn.close()

    # 90 remaining at 1 item/sec, nothing else in the rotation to share with.
    assert _scheduled_job_etas([job]) == {job_id: 90.0}


def test_scheduled_job_etas_none_without_timing_data(tmp_path):
    from storyindex.app import _scheduled_job_etas

    conn = db.connect(tmp_path / "t.sqlite")
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    db.set_job_total(conn, job_id, 100)
    conn.commit()
    job = db.get_job(conn, job_id)
    conn.close()

    assert _scheduled_job_etas([job]) == {job_id: None}


def test_scheduled_job_etas_smaller_job_finishes_first_and_bigger_one_then_speeds_up(tmp_path):
    # Two jobs at the same 1 item/sec rate, sharing the scheduler equally:
    # the smaller job (10 remaining) finishes at 2x its own pace (20s,
    # matching the old flat-N guess exactly), but the bigger job (100
    # remaining) should NOT also be flat-multiplied by 2 for its entire
    # remaining time (that would say 200s) - once the small job drains at
    # 20s it stops competing, so the big job gets the whole scheduler for
    # its last (100-10)=90s of own-work, landing at 20+90=110s, well under
    # the naive 200s a flat-N model would have shown.
    from storyindex.app import _scheduled_job_etas

    conn = db.connect(tmp_path / "t.sqlite")
    small_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    big_id = db.create_job(conn, "extract", "2026-01-01T00:00:01Z", scope="all")
    db.set_job_total(conn, small_id, 10)
    db.set_job_total(conn, big_id, 100)
    db.record_block_timing(conn, small_id, 1.0, 1)  # 1 item/sec, 10s of own-work
    db.record_block_timing(conn, big_id, 1.0, 1)  # 1 item/sec, 100s of own-work
    conn.commit()
    small = db.get_job(conn, small_id)
    big = db.get_job(conn, big_id)
    conn.close()

    etas = _scheduled_job_etas([small, big])
    assert etas[small_id] == 20.0
    assert etas[big_id] == 110.0
    # The two numbers shouldn't be misread as "everything done in 110s either
    # way" nor summed to "130s total" - draining both takes exactly 110s.


def test_job_status_json_includes_eta_once_a_block_has_completed(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", scope="all")
    db.mark_job_running(conn, job_id, "2026-01-01T00:00:00Z", pid=os.getpid())
    db.set_job_total(conn, job_id, 100)
    db.increment_job_progress(conn, job_id, done=10)
    db.record_block_timing(conn, job_id, 10.0, 10)
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.get(f"/jobs/{job_id}/status.json")
    assert r.status_code == 200
    assert r.get_json()["eta"] == "~1m"


def test_scheduler_page_shows_idle_when_nothing_queued(tmp_path):
    dbpath = tmp_path / "t.sqlite"
    client = _client(dbpath)
    r = client.get("/scheduler")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "idle" in body
    assert "nothing queued" in body


def test_scheduler_page_lists_active_jobs_grouped_by_model(tmp_path, monkeypatch):
    # Mirrors scheduler.run_scheduler's own visiting order: grouped by
    # model first, creation order within a model - jobs created in an
    # order that would interleave models (X, Y, X) should still list both
    # model-X jobs before the model-Y one.
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    p1 = db.create_prompt(conn, "p1", "text1", "2026-01-01T00:00:00Z")
    p2 = db.create_prompt(conn, "p2", "text2", "2026-01-01T00:00:01Z")
    p3 = db.create_prompt(conn, "p3", "text3", "2026-01-01T00:00:02Z")
    j_x1 = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", prompt_id=p1, model="modelX", scope="all")
    db.create_job(conn, "extract", "2026-01-01T00:00:01Z", prompt_id=p2, model="modelY", scope="all")
    j_x2 = db.create_job(conn, "extract", "2026-01-01T00:00:02Z", prompt_id=p3, model="modelX", scope="all")
    conn.commit()
    conn.close()

    monkeypatch.setattr("storyindex.app._ensure_scheduler_running", lambda: None)
    client = _client(dbpath)
    r = client.get("/scheduler")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert body.index(f">#{j_x1}<") < body.index(f">#{j_x2}<") < body.index("modelY")


def test_scheduler_status_json_endpoint(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    job_id = db.create_job(conn, "extract", "2026-01-01T00:00:00Z", model="m", scope="all")
    conn.commit()
    conn.close()

    monkeypatch.setattr("storyindex.app._ensure_scheduler_running", lambda: None)
    client = _client(dbpath)
    r = client.get("/scheduler/status.json")
    assert r.status_code == 200
    data = r.get_json()
    assert data["alive"] is False
    assert [j["id"] for j in data["jobs"]] == [job_id]


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
