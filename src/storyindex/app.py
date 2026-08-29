"""Local browse/search/read/review web app.

Single-command launch, binds 0.0.0.0 so it's reachable from the Windows
side of WSL2 at http://localhost:<port>/. Talks only to the local SQLite
DB — no network calls, no model calls, nothing leaves the machine.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, url_for

from storyindex import db, libraries

app = Flask(__name__)
app.config["DB_PATH"] = Path("storyindex.sqlite")
app.config["LIBRARIES_PATH"] = libraries.DEFAULT_CONFIG_PATH


def _libraries_path() -> Path:
    return app.config["LIBRARIES_PATH"]

SRC_DIR = Path(__file__).resolve().parent.parent


def _spawn_job(job_id: int) -> None:
    """Launch the job runner as a detached subprocess so the request that
    created the job returns immediately — the app stays usable (browse,
    read, review other stories) while the job runs in the background."""
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    subprocess.Popen(
        [sys.executable, "-m", "storyindex.jobs", "--job-id", str(job_id), "--db", str(app.config["DB_PATH"])],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = db.connect(app.config["DB_PATH"])
    return g.db


@app.teardown_appcontext
def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _combined_tag_cloud(conn: sqlite3.Connection) -> list[dict]:
    """Merge tags + site_tags into one list for display. Two separate
    storage systems underneath (see docs/crawler-parser-contract.md 3a);
    unified only here, at the UI layer."""
    combined = [
        {"kind": "site", "code": t["code"], "label": t["label"], "count": t["story_count"]}
        for t in db.list_site_tags_with_counts(conn)
    ] + [
        {"kind": "tag", "id": t["id"], "label": t["name"], "count": t["story_count"]}
        for t in db.list_tags_with_counts(conn)
    ]
    combined.sort(key=lambda t: (-t["count"], t["label"]))
    return combined


PAGE_SIZE = 50


@app.route("/")
def index():
    conn = get_db()
    q = request.args.get("q", "").strip()
    page = max(request.args.get("page", 1, type=int), 1)
    offset = (page - 1) * PAGE_SIZE
    stories = (
        db.search_stories_fts(conn, q, limit=PAGE_SIZE, offset=offset)
        if q
        else db.list_stories(conn, limit=PAGE_SIZE, offset=offset)
    )
    tags = _combined_tag_cloud(conn)
    pending_review = db.count_pending_review(conn)
    return render_template(
        "index.html",
        stories=stories,
        tags=tags,
        q=q,
        page=page,
        has_next=len(stories) == PAGE_SIZE,
        pending_review=pending_review,
    )


@app.route("/tag/<int:tag_id>")
def tag_detail(tag_id: int):
    conn = get_db()
    tag = db.get_tag(conn, tag_id)
    if tag is None:
        return "tag not found", 404
    stories = db.stories_for_tag(conn, tag_id)
    return render_template("tag.html", label=tag["name"], stories=stories)


@app.route("/site-tag/<code>")
def site_tag_detail(code: str):
    conn = get_db()
    tag = db.get_site_tag(conn, code)
    if tag is None:
        return "tag not found", 404
    stories = db.stories_for_site_tag(conn, code)
    return render_template("tag.html", label=tag["label"], stories=stories)


@app.route("/story/<story_id>")
def story_detail(story_id: str):
    conn = get_db()
    story = db.get_story(conn, story_id)
    if story is None:
        return "story not found", 404
    parts = db.get_group_parts(conn, story["group_id"])
    tags = db.tags_for_story(conn, story_id)
    site_tags = db.site_tags_for_story(conn, story_id)
    tag_names = db.list_tag_names(conn)
    prompts = db.list_prompts(conn)
    return render_template(
        "story.html",
        story=story, parts=parts, tags=tags, site_tags=site_tags, tag_names=tag_names,
        prompts=prompts,
    )


@app.route("/story/<story_id>/remove", methods=["POST"])
def remove_story(story_id: str):
    conn = get_db()
    db.set_story_status(conn, story_id, "removed")
    conn.commit()
    return redirect(url_for("index"))


@app.route("/story/<story_id>/restore", methods=["POST"])
def restore_story(story_id: str):
    conn = get_db()
    db.set_story_status(conn, story_id, "active")
    conn.commit()
    return redirect(url_for("story_detail", story_id=story_id))


@app.route("/story/<story_id>/tags", methods=["POST"])
def add_tag(story_id: str):
    conn = get_db()
    name = request.form.get("name", "").strip().lower()
    if name:
        db.add_story_tag_by_name(conn, story_id, name, created_at=_now(), source="human")
        conn.commit()
    return redirect(url_for("story_detail", story_id=story_id))


@app.route("/story/<story_id>/tags/<int:tag_id>/delete", methods=["POST"])
def delete_tag(story_id: str, tag_id: int):
    conn = get_db()
    db.delete_story_tag(conn, story_id, tag_id)
    conn.commit()
    return redirect(url_for("story_detail", story_id=story_id))


@app.route("/story/<story_id>/tags/<int:tag_id>/approve", methods=["POST"])
def approve_tag(story_id: str, tag_id: int):
    conn = get_db()
    db.set_story_tag_source(conn, story_id, tag_id, "human")
    conn.commit()
    return redirect(url_for("story_detail", story_id=story_id))


@app.route("/story/<story_id>/tags/approve-all", methods=["POST"])
def approve_all_tags(story_id: str):
    conn = get_db()
    db.approve_all_story_tags(conn, story_id)
    conn.commit()
    return redirect(request.form.get("next") or url_for("story_detail", story_id=story_id))


@app.route("/story/<story_id>/tags/reject-all", methods=["POST"])
def reject_all_tags(story_id: str):
    conn = get_db()
    db.reject_all_story_tags(conn, story_id)
    conn.commit()
    return redirect(request.form.get("next") or url_for("story_detail", story_id=story_id))


@app.route("/review")
def review_queue():
    conn = get_db()
    page = max(request.args.get("page", 1, type=int), 1)
    job_id = request.args.get("job_id", type=int)
    offset = (page - 1) * PAGE_SIZE
    items = db.stories_pending_review(conn, limit=PAGE_SIZE, offset=offset, job_id=job_id)
    total = db.count_pending_review(conn, job_id=job_id)
    job = db.get_job(conn, job_id) if job_id else None
    return render_template(
        "review.html",
        items=items, page=page, total=total, job_id=job_id, job=job,
        has_next=offset + len(items) < total,
    )


@app.route("/jobs/<int:job_id>/revert", methods=["POST"])
def revert_job(job_id: int):
    conn = get_db()
    db.revert_job(conn, job_id, _now())
    conn.commit()
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/prompts")
def prompts_list():
    conn = get_db()
    if not db.list_prompts(conn):
        _seed_default_prompt(conn)
        conn.commit()
    prompts = db.list_prompts(conn)
    return render_template("prompts.html", prompts=prompts)


@app.route("/prompts/new", methods=["POST"])
def create_prompt():
    conn = get_db()
    name = request.form.get("name", "").strip()
    text = request.form.get("text", "").strip()
    based_on_id = request.form.get("based_on_id", type=int)
    if name and text:
        db.create_prompt(conn, name, text, _now(), based_on_id=based_on_id)
        conn.commit()
    return redirect(url_for("prompts_list"))


def _run_prompt_preview(prompt: sqlite3.Row, model: str, stories: list[sqlite3.Row]) -> list[dict]:
    from storyindex.classify import ExtractionError, extract_tags
    from storyindex.jobs import row_to_sig

    results = []
    for row in stories:
        sig = row_to_sig(row)
        try:
            tags = extract_tags(sig, model=model, prompt_text=prompt["text"])
        except ExtractionError as exc:
            results.append({"story": row, "tags": None, "error": str(exc)})
        else:
            results.append({"story": row, "tags": tags, "error": None})
    return results


@app.route("/prompts/<int:prompt_id>/preview", methods=["POST"])
def preview_prompt(prompt_id: int):
    conn = get_db()
    prompt = db.get_prompt(conn, prompt_id)
    if prompt is None:
        return "prompt not found", 404
    model = request.form.get("model", "").strip()
    sample_size = request.form.get("sample_size", 5, type=int)
    stories = db.random_stories(conn, sample_size)
    results = _run_prompt_preview(prompt, model, stories)
    return render_template("prompt_preview.html", prompt=prompt, results=results, model=model)


@app.route("/story/<story_id>/prompts/preview", methods=["POST"])
def preview_prompt_on_story(story_id: str):
    conn = get_db()
    prompt_id = request.form.get("prompt_id", type=int)
    prompt = db.get_prompt(conn, prompt_id) if prompt_id else None
    story = db.get_story(conn, story_id)
    if prompt is None or story is None:
        return "not found", 404
    model = request.form.get("model", "").strip()
    results = _run_prompt_preview(prompt, model, [story])
    return render_template("prompt_preview.html", prompt=prompt, results=results, model=model, target_story=story)


@app.route("/jobs")
def jobs_list():
    conn = get_db()
    if db.reap_dead_pid_jobs(conn, _now()):
        conn.commit()
    prompts = db.list_prompts(conn)
    if not prompts:
        _seed_default_prompt(conn)
        conn.commit()
        prompts = db.list_prompts(conn)
    jobs = db.list_jobs(conn)
    return render_template("jobs.html", jobs=jobs, prompts=prompts)


def _seed_default_prompt(conn: sqlite3.Connection) -> None:
    from storyindex.classify import load_prompt_template

    try:
        text = load_prompt_template("v1")
    except Exception:
        return
    db.ensure_seed_prompt(conn, "default (v1)", text, _now())


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    conn = get_db()
    job = db.get_job(conn, job_id)
    if job is None:
        return "job not found", 404
    return render_template("job_detail.html", job=job)


@app.route("/jobs/<int:job_id>/status.json")
def job_status_json(job_id: int):
    conn = get_db()
    job = db.get_job(conn, job_id)
    if job is None:
        return {"error": "not found"}, 404
    return {
        "status": job["status"], "total": job["total"], "done": job["done"],
        "failed": job["failed"], "error": job["error"],
    }


@app.route("/jobs/extract", methods=["POST"])
def create_extract_job():
    conn = get_db()
    prompt_id = request.form.get("prompt_id", type=int)
    model = request.form.get("model", "").strip()
    scope = request.form.get("scope", "all")
    if not prompt_id or not model:
        return redirect(url_for("jobs_list"))
    job_id = db.create_job(conn, "extract", _now(), prompt_id=prompt_id, model=model, scope=scope)
    conn.commit()
    _spawn_job(job_id)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/cluster", methods=["POST"])
def create_cluster_job():
    conn = get_db()
    model = request.form.get("model", "").strip() or None
    job_id = db.create_job(conn, "cluster", _now(), model=model)
    conn.commit()
    _spawn_job(job_id)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/sync", methods=["POST"])
def create_sync_job():
    import json

    conn = get_db()
    adapter = request.form.get("adapter", "").strip()
    archive_root = request.form.get("archive_root", "").strip()
    if not adapter or not archive_root:
        return redirect(url_for("jobs_list"))
    scope = json.dumps({"adapter": adapter, "archive_root": archive_root})
    job_id = db.create_job(conn, "sync", _now(), scope=scope)
    conn.commit()
    _spawn_job(job_id)
    return redirect(url_for("job_detail", job_id=job_id))


@app.context_processor
def _inject_library_name():
    data = libraries.load(_libraries_path())
    return {"current_library": data.get("active")}


@app.route("/libraries")
def libraries_list():
    data = libraries.load(_libraries_path())
    return render_template("libraries.html", data=data)


@app.route("/libraries/add", methods=["POST"])
def add_library():
    name = request.form.get("name", "").strip()
    path = request.form.get("path", "").strip()
    if name and path:
        libraries.register(name, path, _libraries_path())
    return redirect(url_for("libraries_list"))


@app.route("/libraries/switch", methods=["POST"])
def switch_library():
    name = request.form.get("name", "").strip()
    data = libraries.load(_libraries_path())
    if name in data["libraries"]:
        libraries.set_active(name, _libraries_path())
        app.config["DB_PATH"] = Path(data["libraries"][name])
    return redirect(url_for("index"))


@app.route("/tags")
def tags_admin():
    conn = get_db()
    tags = db.list_tags_with_counts(conn)
    site_tags = db.list_site_tags_with_counts(conn)
    return render_template("tags.html", tags=tags, site_tags=site_tags)


@app.route("/tags/<int:tag_id>/rename", methods=["POST"])
def rename_tag(tag_id: int):
    conn = get_db()
    new_name = request.form.get("name", "").strip().lower()
    if new_name:
        db.rename_tag(conn, tag_id, new_name)
        conn.commit()
    return redirect(url_for("tags_admin"))

@app.route("/tags/merge", methods=["POST"])
def merge_tags():
    conn = get_db()
    src = request.form.get("src_id", type=int)
    dst = request.form.get("dst_id", type=int)
    if src and dst and src != dst:
        db.merge_tags(conn, src, dst)
        conn.commit()
    return redirect(url_for("tags_admin"))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Local story index browse/review app")
    parser.add_argument("--db", type=Path, default=Path("storyindex.sqlite"))
    parser.add_argument("--library-name", default=None, help="name to register this --db under (default: filename)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    app.config["DB_PATH"] = args.db
    name = args.library_name or args.db.stem
    libraries.ensure_registered_and_active(name, str(args.db.resolve()), _libraries_path())

    conn = db.connect(args.db)
    reaped = db.reap_stale_jobs(conn, _now())
    conn.commit()
    conn.close()
    if reaped:
        print(f"recovered from an unclean shutdown: marked {len(reaped)} stuck job(s) failed: {reaped}")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
