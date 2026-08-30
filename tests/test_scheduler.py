import datetime
from itertools import groupby

from storyindex import db, jobs as jobs_module, scheduler


def _now():
    return datetime.datetime.utcnow().isoformat() + "Z"


def _fast(monkeypatch, block_size=None):
    if block_size is not None:
        monkeypatch.setattr(scheduler, "BLOCK_SIZE", block_size)
    monkeypatch.setattr(scheduler, "IDLE_EXIT_SECONDS", 0.05)
    monkeypatch.setattr(scheduler, "POLL_INTERVAL_SECONDS", 0.01)


def test_scheduler_runs_two_jobs_to_completion(tmp_path, make_sig, monkeypatch):
    dbpath = tmp_path / "s.sqlite"
    conn = db.connect(dbpath)
    for i in range(3):
        db.upsert_story(conn, make_sig(f"s{i}"))
    p1 = db.create_prompt(conn, "p1", "text1", _now())
    p2 = db.create_prompt(conn, "p2", "text2", _now())
    j1 = db.create_job(conn, "extract", _now(), prompt_id=p1, model="m", scope="all")
    j2 = db.create_job(conn, "extract", _now(), prompt_id=p2, model="m", scope="all")
    conn.commit()
    conn.close()

    _fast(monkeypatch)
    monkeypatch.setattr(jobs_module, "extract_tags", lambda sig, model, prompt_text, host=None: ["x"])

    scheduler.run_scheduler(dbpath)

    conn = db.connect(dbpath)
    j1row = db.get_job(conn, j1)
    j2row = db.get_job(conn, j2)
    assert j1row["status"] == "done" and j1row["done"] == 3
    assert j2row["status"] == "done" and j2row["done"] == 3
    conn.close()


def test_scheduler_batches_calls_by_job_instead_of_strict_interleave(tmp_path, make_sig, monkeypatch):
    dbpath = tmp_path / "s.sqlite"
    conn = db.connect(dbpath)
    for i in range(5):
        db.upsert_story(conn, make_sig(f"s{i}"))
    p1 = db.create_prompt(conn, "p1", "PROMPT-A", _now())
    p2 = db.create_prompt(conn, "p2", "PROMPT-B", _now())
    db.create_job(conn, "extract", _now(), prompt_id=p1, model="m", scope="all")
    db.create_job(conn, "extract", _now(), prompt_id=p2, model="m", scope="all")
    conn.commit()
    conn.close()

    _fast(monkeypatch, block_size=2)
    calls = []
    monkeypatch.setattr(
        jobs_module, "extract_tags",
        lambda sig, model, prompt_text, host=None: (calls.append(prompt_text), ["x"])[1],
    )

    scheduler.run_scheduler(dbpath)

    # 5 stories per job, block size 2: expect same-prompt calls to land in
    # runs of at least the block size, not a strict A,B,A,B,... interleave
    # (which is what running two independent job subprocesses produces).
    run_lengths = [len(list(g)) for _, g in groupby(calls)]
    assert max(run_lengths) >= 2


def test_scheduler_handles_mixed_scope_sizes(tmp_path, make_sig, monkeypatch):
    dbpath = tmp_path / "s.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s0"))
    db.upsert_story(conn, make_sig("s1"))
    now = _now()
    tag_id = db.get_or_create_tag(conn, "existing", now)
    db.link_story_tag(conn, "s0", tag_id, source="human")
    p1 = db.create_prompt(conn, "p1", "text1", now)
    p2 = db.create_prompt(conn, "p2", "text2", now)
    j_big = db.create_job(conn, "extract", now, prompt_id=p1, model="m", scope="all")
    j_small = db.create_job(conn, "extract", now, prompt_id=p2, model="m", scope="untagged")
    conn.commit()
    conn.close()

    _fast(monkeypatch, block_size=1)
    monkeypatch.setattr(jobs_module, "extract_tags", lambda sig, model, prompt_text, host=None: ["x"])

    scheduler.run_scheduler(dbpath)

    conn = db.connect(dbpath)
    big = db.get_job(conn, j_big)
    small = db.get_job(conn, j_small)
    assert big["status"] == "done" and big["total"] == 2 and big["done"] == 2
    assert small["status"] == "done" and small["total"] == 1 and small["done"] == 1
    conn.close()


def test_scheduler_stops_processing_a_cancelled_job(tmp_path, make_sig, monkeypatch):
    dbpath = tmp_path / "s.sqlite"
    conn = db.connect(dbpath)
    for i in range(5):
        db.upsert_story(conn, make_sig(f"s{i}"))
    p1 = db.create_prompt(conn, "p1", "text1", _now())
    j1 = db.create_job(conn, "extract", _now(), prompt_id=p1, model="m", scope="all")
    conn.commit()
    conn.close()

    _fast(monkeypatch, block_size=1)
    calls = {"n": 0}

    def fake_extract(sig, model, prompt_text, host=None):
        calls["n"] += 1
        if calls["n"] == 2:
            conn2 = db.connect(dbpath)
            db.cancel_job(conn2, j1, _now())
            conn2.commit()
            conn2.close()
        return ["x"]

    monkeypatch.setattr(jobs_module, "extract_tags", fake_extract)
    scheduler.run_scheduler(dbpath)

    conn = db.connect(dbpath)
    job = db.get_job(conn, j1)
    assert job["status"] == "failed"
    assert job["done"] <= 2
    conn.close()


def test_scheduler_groups_jobs_by_model_to_avoid_model_swaps(tmp_path, make_sig, monkeypatch):
    # Switching models mid-rotation forces Ollama to evict/reload weights -
    # far more expensive than a prefix-cache miss between two prompts on
    # the same model. Jobs are created in an order that would interleave
    # models (X, Y, X) if the scheduler just followed creation order; it
    # should instead visit both model-X jobs back to back within a round.
    dbpath = tmp_path / "s.sqlite"
    conn = db.connect(dbpath)
    for i in range(2):
        db.upsert_story(conn, make_sig(f"s{i}"))
    p1 = db.create_prompt(conn, "p1", "text1", _now())
    p2 = db.create_prompt(conn, "p2", "text2", _now())
    p3 = db.create_prompt(conn, "p3", "text3", _now())
    db.create_job(conn, "extract", _now(), prompt_id=p1, model="modelX", scope="all")
    db.create_job(conn, "extract", _now(), prompt_id=p2, model="modelY", scope="all")
    db.create_job(conn, "extract", _now(), prompt_id=p3, model="modelX", scope="all")
    conn.commit()
    conn.close()

    _fast(monkeypatch, block_size=1)
    models_used = []
    monkeypatch.setattr(
        jobs_module, "extract_tags",
        lambda sig, model, prompt_text, host=None: (models_used.append(model), ["x"])[1],
    )

    scheduler.run_scheduler(dbpath)

    # First full round should visit both modelX jobs before modelY, i.e.
    # the first three calls are X, X, Y - not X, Y, X.
    assert models_used[:3] == ["modelX", "modelX", "modelY"]


def test_scheduler_cancel_stops_within_one_call_mid_block(tmp_path, make_sig, monkeypatch):
    # BLOCK_SIZE stays at its production default here (not shrunk to 1) to
    # prove cancellation doesn't have to run out the rest of the block.
    dbpath = tmp_path / "s.sqlite"
    conn = db.connect(dbpath)
    for i in range(8):
        db.upsert_story(conn, make_sig(f"s{i}"))
    p1 = db.create_prompt(conn, "p1", "text1", _now())
    j1 = db.create_job(conn, "extract", _now(), prompt_id=p1, model="m", scope="all")
    conn.commit()
    conn.close()

    _fast(monkeypatch)  # BLOCK_SIZE untouched (10), well above the 8 stories here
    calls = {"n": 0}

    def fake_extract(sig, model, prompt_text, host=None):
        calls["n"] += 1
        if calls["n"] == 1:
            conn2 = db.connect(dbpath)
            db.cancel_job(conn2, j1, _now())
            conn2.commit()
            conn2.close()
        return ["x"]

    monkeypatch.setattr(jobs_module, "extract_tags", fake_extract)
    scheduler.run_scheduler(dbpath)

    conn = db.connect(dbpath)
    job = db.get_job(conn, j1)
    conn.close()
    assert job["status"] == "failed"
    assert calls["n"] == 1  # stopped after the first call, not the rest of the block


def test_scheduler_survives_one_jobs_unexpected_error(tmp_path, make_sig, monkeypatch):
    # process_extract_item only catches ExtractionError (model-call
    # failures) - anything else (a bug, a transient DB error) must be
    # contained to the one job that hit it, not crash the shared process
    # and orphan every other job currently in the rotation.
    dbpath = tmp_path / "s.sqlite"
    conn = db.connect(dbpath)
    for i in range(3):
        db.upsert_story(conn, make_sig(f"s{i}"))
    p1 = db.create_prompt(conn, "p1", "text1", _now())
    p2 = db.create_prompt(conn, "p2", "text2", _now())
    j_bad = db.create_job(conn, "extract", _now(), prompt_id=p1, model="m", scope="all")
    j_good = db.create_job(conn, "extract", _now(), prompt_id=p2, model="m", scope="all")
    conn.commit()
    conn.close()

    _fast(monkeypatch, block_size=1)

    def flaky_extract(sig, model, prompt_text, host=None):
        if prompt_text == "text1":
            raise RuntimeError("boom - not an ExtractionError")
        return ["x"]

    monkeypatch.setattr(jobs_module, "extract_tags", flaky_extract)
    scheduler.run_scheduler(dbpath)

    conn = db.connect(dbpath)
    bad = db.get_job(conn, j_bad)
    good = db.get_job(conn, j_good)
    assert bad["status"] == "failed"
    assert "boom" in bad["error"]
    assert good["status"] == "done" and good["done"] == 3
    conn.close()


def test_scheduler_marks_job_failed_when_prompt_missing(tmp_path, make_sig, monkeypatch):
    dbpath = tmp_path / "s.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s0"))
    job_id = db.create_job(conn, "extract", _now(), prompt_id=None, model="m", scope="all")
    conn.commit()
    conn.close()

    _fast(monkeypatch)
    scheduler.run_scheduler(dbpath)

    conn = db.connect(dbpath)
    job = db.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["error"]
    conn.close()
