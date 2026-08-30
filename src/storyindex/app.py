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

from storyindex import db, libraries, settings
from storyindex import scheduler as scheduler_module

app = Flask(__name__)
app.config["DB_PATH"] = Path("storyindex.sqlite")
app.config["LIBRARIES_PATH"] = libraries.DEFAULT_CONFIG_PATH
app.config["SETTINGS_PATH"] = settings.DEFAULT_CONFIG_PATH


def _libraries_path() -> Path:
    return app.config["LIBRARIES_PATH"]


def _settings_path() -> Path:
    return app.config["SETTINGS_PATH"]


@app.context_processor
def _inject_settings() -> dict:
    return {"settings": settings.load(_settings_path())}

SRC_DIR = Path(__file__).resolve().parent.parent


def _spawn_job(job_id: int) -> None:
    """Launch the job runner as a detached subprocess so the request that
    created the job returns immediately — the app stays usable (browse,
    read, review other stories) while the job runs in the background.

    Not used for `extract` jobs — see _ensure_scheduler_running below."""
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    subprocess.Popen(
        [sys.executable, "-m", "storyindex.jobs", "--job-id", str(job_id), "--db", str(app.config["DB_PATH"])],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _scheduler_pidfile() -> Path:
    return Path(str(app.config["DB_PATH"]) + ".scheduler.pid")


def _ensure_scheduler_running() -> None:
    """Make sure the singleton extract-job scheduler (scheduler.py) is
    alive, spawning it if not. One process handles every queued/running
    extract job for this DB - see scheduler.py's module docstring for why
    that matters (prefix-cache locality across a job's own calls)."""
    pidfile = _scheduler_pidfile()
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
        except ValueError:
            pid = None
        if pid is not None and db._pid_alive(pid):
            return
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "storyindex.scheduler", "--db", str(app.config["DB_PATH"])],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pidfile.write_text(str(proc.pid))


def _ensure_scheduler_if_extract_jobs_pending(conn: sqlite3.Connection) -> None:
    """Self-healing hook: if the scheduler process dies mid-session (OOM,
    an uncaught exception, `kill -9`) while the app itself keeps running,
    nothing else respawns it - reap_dead_pid_jobs only fails jobs that were
    already 'running' with a dead pid, but a job still 'queued' (never
    picked up yet) has no pid to notice is dead, so it would sit queued
    forever with no scheduler left to serve it. Call this from any
    jobs-related view so a page load is enough to notice and respawn."""
    if db.count_jobs(conn, status="queued", type="extract") or db.count_jobs(conn, status="running", type="extract"):
        _ensure_scheduler_running()


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


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"~{days}d {hours}h"
    if hours:
        return f"~{hours}h {minutes}m"
    if minutes:
        return f"~{minutes}m"
    return "<1m"


def _extract_job_etas(active_jobs: list[sqlite3.Row]) -> dict[int, float | None]:
    """Rough completion-time estimate for every active extract job at
    once - has to see the whole rotation together, not one job at a time,
    because the jobs aren't independent: they're all splitting the same
    scheduler.

    own_time[j] = remaining_j / rate_j is how long job j would take if it
    had the scheduler to itself, from its own most recent block's
    throughput (db.record_block_timing). A first cut at correcting for
    sharing multiplied every job's own_time by the number of active jobs
    - which is right for whichever job finishes first, but wrong for
    everything after it: once the fastest-draining job completes, it stops
    competing for a turn and the rest speed up. Flat-multiplying every
    job's own_time by the same N overstates the slower jobs and, worse,
    means the displayed numbers don't add up - a user glancing at "~3d"
    and "~2d" side by side has no way to know those already double-count
    the time they spend sharing, so summing or comparing them is
    misleading.

    Modeled instead as egalitarian processor sharing (the standard result
    for N jobs splitting one resource equally, draining smallest-
    remaining-work-first, each freed-up share going to whoever's left):
    sort by own_time ascending, then each job's completion time is the
    previous one's plus its share of the *extra* own_time beyond that,
    split across however many jobs are still in the race at that point.
    The fastest job's estimate comes out identical to the old flat-N
    guess; every slower job's comes out lower and consistent with the
    others, since it accounts for gaining a larger share once faster jobs
    finish rather than assuming the full original N for its entire run."""
    own_time: dict[int, float] = {}
    for j in active_jobs:
        if not j["last_block_items"] or not j["last_block_seconds"]:
            continue
        remaining = j["total"] - j["done"] - j["failed"]
        if remaining <= 0:
            own_time[j["id"]] = 0.0
            continue
        rate = j["last_block_items"] / j["last_block_seconds"]
        if rate > 0:
            own_time[j["id"]] = remaining / rate

    etas: dict[int, float | None] = {j["id"]: None for j in active_jobs}
    ordered = sorted(own_time.items(), key=lambda kv: kv[1])
    n = len(ordered)
    completed = 0.0
    prev_t = 0.0
    for idx, (jid, t) in enumerate(ordered):
        completed += (n - idx) * (t - prev_t)
        etas[jid] = completed
        prev_t = t
    return etas


TAG_CLOUD_SIZE = 40


def _combined_tag_cloud(conn: sqlite3.Connection, limit: int | None = TAG_CLOUD_SIZE) -> tuple[list[dict], int]:
    """Merge tags + site_tags into one list for display. Two separate
    storage systems underneath (see docs/crawler-parser-contract.md 3a);
    unified only here, at the UI layer. Returns (top `limit` by story
    count, total distinct tags) - the homepage shows a bounded cloud
    rather than every tag in the library, with a link to /tags for the
    rest, so a library with hundreds of tags doesn't turn browse into a
    wall of pills."""
    combined = [
        {"kind": "site", "code": t["code"], "label": t["label"], "count": t["story_count"]}
        for t in db.list_site_tags_with_counts(conn)
    ] + [
        {"kind": "tag", "id": t["id"], "label": t["name"], "count": t["story_count"]}
        for t in db.list_tags_with_counts(conn)
    ]
    combined.sort(key=lambda t: (-t["count"], t["label"]))
    total = len(combined)
    if limit is not None:
        combined = combined[:limit]
    return combined, total


PAGE_SIZE = 50
PROMPTS_PAGE_SIZE = 20
JOBS_PAGE_SIZE = 25
TAGS_PAGE_SIZE = 50


def _parse_tag_ids(raw: str) -> list[int]:
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def _filter_url(q: str, include_ids: list[int], exclude_ids: list[int]) -> str:
    return url_for(
        "index", q=q or None,
        tags=",".join(str(i) for i in include_ids) or None,
        exclude_tags=",".join(str(i) for i in exclude_ids) or None,
    )


@app.route("/")
def index():
    conn = get_db()
    q = request.args.get("q", "").strip()
    include_ids = _parse_tag_ids(request.args.get("tags", ""))
    exclude_ids = _parse_tag_ids(request.args.get("exclude_tags", ""))

    # From the tag-search picker: a typed name to fold into the filter,
    # resolved server-side so the URL only ever carries ids. Redirect to
    # the canonical form rather than rendering here, so the picked name
    # doesn't linger as a separate, bookmarkable-but-stale query param.
    add_name = request.args.get("add_tag", "").strip().lower()
    add_exclude_name = request.args.get("add_exclude_tag", "").strip().lower()
    if add_name or add_exclude_name:
        if add_name:
            row = db.get_tag_by_name(conn, add_name)
            if row is not None and row["id"] not in include_ids:
                include_ids = include_ids + [row["id"]]
        if add_exclude_name:
            row = db.get_tag_by_name(conn, add_exclude_name)
            if row is not None and row["id"] not in exclude_ids:
                exclude_ids = exclude_ids + [row["id"]]
        return redirect(_filter_url(q, include_ids, exclude_ids))

    page = max(request.args.get("page", 1, type=int), 1)
    offset = (page - 1) * PAGE_SIZE
    stories = db.search_stories(
        conn, q, include_tag_ids=include_ids, exclude_tag_ids=exclude_ids,
        limit=PAGE_SIZE, offset=offset,
    )
    cloud_tags, total_tag_count = _combined_tag_cloud(conn, limit=TAG_CLOUD_SIZE)

    # Chip labels come from a direct lookup, not the (bounded) cloud, so a
    # selected tag outside the top TAG_CLOUD_SIZE by story count still
    # shows its name correctly instead of silently vanishing from view.
    selected_ids = set(include_ids) | set(exclude_ids)
    label_by_id = {t["id"]: t["label"] for t in cloud_tags if t["kind"] == "tag" and t["id"] in selected_ids}
    for tid in selected_ids - label_by_id.keys():
        row = db.get_tag(conn, tid)
        if row is not None:
            label_by_id[tid] = row["name"]

    include_chips = [
        {"label": label_by_id[i], "remove_href": _filter_url(q, [x for x in include_ids if x != i], exclude_ids)}
        for i in include_ids if i in label_by_id
    ]
    exclude_chips = [
        {"label": label_by_id[i], "remove_href": _filter_url(q, include_ids, [x for x in exclude_ids if x != i])}
        for i in exclude_ids if i in label_by_id
    ]

    cloud = []
    for t in cloud_tags:
        if t["kind"] == "tag" and t["id"] in selected_ids:
            continue
        if t["kind"] != "tag":
            cloud.append({**t, "include_href": None, "exclude_href": None})
            continue
        cloud.append({
            **t,
            "include_href": _filter_url(q, include_ids + [t["id"]], exclude_ids),
            "exclude_href": _filter_url(q, include_ids, exclude_ids + [t["id"]]),
        })

    pending_review = db.count_pending_review(conn)
    return render_template(
        "index.html",
        stories=stories,
        tags=cloud,
        total_tag_count=total_tag_count,
        include_chips=include_chips,
        exclude_chips=exclude_chips,
        tags_param=",".join(str(i) for i in include_ids),
        exclude_tags_param=",".join(str(i) for i in exclude_ids),
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
    page = max(request.args.get("page", 1, type=int), 1)
    offset = (page - 1) * PAGE_SIZE
    stories = db.stories_for_tag(conn, tag_id, limit=PAGE_SIZE, offset=offset)
    total = db.count_stories_for_tag(conn, tag_id)
    return render_template(
        "tag.html", label=tag["name"], stories=stories, page=page, total=total,
        has_next=offset + len(stories) < total,
        pager_endpoint="tag_detail", pager_params={"tag_id": tag_id},
    )


@app.route("/site-tag/<code>")
def site_tag_detail(code: str):
    conn = get_db()
    tag = db.get_site_tag(conn, code)
    if tag is None:
        return "tag not found", 404
    page = max(request.args.get("page", 1, type=int), 1)
    offset = (page - 1) * PAGE_SIZE
    stories = db.stories_for_site_tag(conn, code, limit=PAGE_SIZE, offset=offset)
    total = db.count_stories_for_site_tag(conn, code)
    return render_template(
        "tag.html", label=tag["label"], stories=stories, page=page, total=total,
        has_next=offset + len(stories) < total,
        pager_endpoint="site_tag_detail", pager_params={"code": code},
    )


@app.route("/story/<story_id>")
def story_detail(story_id: str):
    conn = get_db()
    story = db.get_story(conn, story_id)
    if story is None:
        return "story not found", 404
    parts = db.get_group_parts(conn, story["group_id"])
    tags = db.tags_for_story(conn, story_id)
    site_tags = db.site_tags_for_story(conn, story_id)
    prompts = db.list_prompts(conn)
    more_by_author = db.stories_by_author(conn, story["author"], story["group_id"])
    return render_template(
        "story.html",
        story=story, parts=parts, tags=tags, site_tags=site_tags,
        prompts=prompts, more_by_author=more_by_author,
    )


@app.route("/stories/removed")
def removed_stories():
    conn = get_db()
    page = max(request.args.get("page", 1, type=int), 1)
    offset = (page - 1) * PAGE_SIZE
    stories = db.list_removed_stories(conn, limit=PAGE_SIZE, offset=offset)
    total = db.count_removed_stories(conn)
    return render_template(
        "removed_stories.html", stories=stories, page=page, total=total,
        has_next=offset + len(stories) < total,
    )


@app.route("/stories/new", methods=["GET", "POST"])
def new_story():
    if request.method == "GET":
        return render_template("new_story.html")
    conn = get_db()
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    body_text = request.form.get("body_text", "").strip()
    if not title or not body_text:
        return render_template(
            "new_story.html", error="title and story text are required",
            title=title, author=author, body_text=body_text,
        )
    import hashlib
    import uuid

    story_id = hashlib.sha1(f"manual-{uuid.uuid4()}".encode()).hexdigest()
    db.create_manual_story(conn, story_id, title, author or "Unknown", body_text, _now())
    conn.commit()
    return redirect(url_for("story_detail", story_id=story_id))


@app.route("/author/<path:author>")
def author_detail(author: str):
    conn = get_db()
    page = max(request.args.get("page", 1, type=int), 1)
    offset = (page - 1) * PAGE_SIZE
    stories = db.stories_for_author(conn, author, limit=PAGE_SIZE, offset=offset)
    total = db.count_stories_for_author(conn, author)
    return render_template(
        "author.html", author=author, stories=stories, page=page, total=total,
        has_next=offset + len(stories) < total,
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


@app.route("/tags/autocomplete.json")
def tags_autocomplete():
    conn = get_db()
    q = request.args.get("q", "").strip()
    if not q:
        return {"tags": []}
    rows = db.search_tag_names(conn, q, limit=20)
    return {"tags": [{"id": r["id"], "name": r["name"], "count": r["story_count"]} for r in rows]}


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
    """Tag-centric review: one row per pending tag (most-applied first)
    rather than one row per story - a model's mistakes tend to repeat
    across many stories, so approving/rejecting a whole tag at once
    usually clears far more of the queue per decision than the per-story
    view at /review/stories. Each tag's row carries every story still
    pending under it, so a reviewer can spot-check the actual stories
    before bulk-approving or -rejecting."""
    conn = get_db()
    page = max(request.args.get("page", 1, type=int), 1)
    job_id = request.args.get("job_id", type=int)
    offset = (page - 1) * TAGS_PAGE_SIZE
    tags = db.pending_review_tags(conn, limit=TAGS_PAGE_SIZE, offset=offset, job_id=job_id)
    total = db.count_pending_review_tags(conn, job_id=job_id)
    stories_by_tag = db.pending_review_stories_for_tags(conn, [t["id"] for t in tags], job_id=job_id)
    job = db.get_job(conn, job_id) if job_id else None
    return render_template(
        "review.html",
        tags=tags, stories_by_tag=stories_by_tag, page=page, total=total,
        job_id=job_id, job=job, has_next=offset + len(tags) < total,
    )


@app.route("/review/tags/<int:tag_id>/approve", methods=["POST"])
def approve_pending_tag(tag_id: int):
    conn = get_db()
    job_id = request.form.get("job_id", type=int)
    db.approve_tag_pending(conn, tag_id, job_id=job_id)
    conn.commit()
    return redirect(request.form.get("next") or url_for("review_queue", job_id=job_id))


@app.route("/review/tags/<int:tag_id>/reject", methods=["POST"])
def reject_pending_tag(tag_id: int):
    conn = get_db()
    job_id = request.form.get("job_id", type=int)
    db.reject_tag_pending(conn, tag_id, job_id=job_id)
    conn.commit()
    return redirect(request.form.get("next") or url_for("review_queue", job_id=job_id))


@app.route("/review/stories")
def review_stories_queue():
    """Per-story review queue - the original view, kept for cases where
    the tag-centric /review view above is too coarse (e.g. double-checking
    one specific story's full tag set)."""
    conn = get_db()
    page = max(request.args.get("page", 1, type=int), 1)
    job_id = request.args.get("job_id", type=int)
    offset = (page - 1) * PAGE_SIZE
    items = db.stories_pending_review(conn, limit=PAGE_SIZE, offset=offset, job_id=job_id)
    total = db.count_pending_review(conn, job_id=job_id)
    job = db.get_job(conn, job_id) if job_id else None
    return render_template(
        "review_stories.html",
        items=items, page=page, total=total, job_id=job_id, job=job,
        has_next=offset + len(items) < total,
    )


@app.route("/jobs/<int:job_id>/revert", methods=["POST"])
def revert_job(job_id: int):
    conn = get_db()
    db.revert_job(conn, job_id, _now())
    conn.commit()
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<int:job_id>/cancel", methods=["POST"])
def cancel_job(job_id: int):
    conn = get_db()
    job = db.get_job(conn, job_id)
    pid = db.cancel_job(conn, job_id, _now())
    conn.commit()
    # extract jobs share one scheduler process's pid across many jobs
    # (see _ensure_scheduler_running) - marking the row failed is enough
    # for the scheduler to drop it on its next pass, and SIGTERM-ing that
    # pid would kill every other extract job it's currently working too.
    if pid is not None and job is not None and job["type"] != "extract":
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/prompts")
def prompts_list():
    conn = get_db()
    if not db.list_prompts(conn):
        _seed_default_prompt(conn)
        conn.commit()
    q = request.args.get("q", "").strip()
    page = max(request.args.get("page", 1, type=int), 1)
    offset = (page - 1) * PROMPTS_PAGE_SIZE
    prompts = db.list_prompts(conn, q=q or None, limit=PROMPTS_PAGE_SIZE, offset=offset)
    total = db.count_prompts(conn, q=q or None)
    return render_template(
        "prompts.html", prompts=prompts, q=q, page=page, total=total,
        has_next=offset + len(prompts) < total,
    )


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

    host = settings.load(_settings_path())["ollama_host"]
    results = []
    for row in stories:
        sig = row_to_sig(row)
        try:
            tags = extract_tags(sig, model=model, prompt_text=prompt["text"], host=host)
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


@app.route("/ollama")
def ollama_status():
    from storyindex import ollama_client

    host = settings.load(_settings_path())["ollama_host"]
    running = ollama_client.is_running(host=host)
    models = ollama_client.list_models(host=host) if running else []
    return render_template(
        "ollama.html", running=running, models=models,
        recommended=ollama_client.RECOMMENDED_MODELS, host=host,
    )


@app.route("/ollama/start", methods=["POST"])
def ollama_start():
    from storyindex import ollama_client

    try:
        ollama_client.start_server()
    except ollama_client.OllamaError as exc:
        host = settings.load(_settings_path())["ollama_host"]
        return render_template("ollama.html", running=False, models=[], start_error=str(exc),
                                recommended=ollama_client.RECOMMENDED_MODELS, host=host)
    return redirect(url_for("ollama_status"))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        settings.update(
            {
                "theme": request.form.get("theme", "dark").strip(),
                "ollama_host": request.form.get("ollama_host", "").strip() or settings.DEFAULTS["ollama_host"],
                "default_extract_model": request.form.get("default_extract_model", "").strip()
                or settings.DEFAULTS["default_extract_model"],
                "default_embed_model": request.form.get("default_embed_model", "").strip()
                or settings.DEFAULTS["default_embed_model"],
            },
            _settings_path(),
        )
        return redirect(url_for("settings_page"))
    return render_template("settings.html", current=settings.load(_settings_path()))


@app.route("/jobs")
def jobs_list():
    conn = get_db()
    if db.reap_dead_pid_jobs(conn, _now()):
        conn.commit()
    _ensure_scheduler_if_extract_jobs_pending(conn)
    prompts = db.list_prompts(conn)
    if not prompts:
        _seed_default_prompt(conn)
        conn.commit()
        prompts = db.list_prompts(conn)
    status = request.args.get("status", "").strip()
    type_ = request.args.get("type", "").strip()
    page = max(request.args.get("page", 1, type=int), 1)
    offset = (page - 1) * JOBS_PAGE_SIZE
    jobs = db.list_jobs(conn, limit=JOBS_PAGE_SIZE, offset=offset, status=status or None, type=type_ or None)
    total = db.count_jobs(conn, status=status or None, type=type_ or None)
    return render_template(
        "jobs.html", jobs=jobs, prompts=prompts, status=status, type=type_,
        page=page, total=total, has_next=offset + len(jobs) < total,
    )


def _seed_default_prompt(conn: sqlite3.Connection) -> None:
    from storyindex.classify import load_prompt_template

    try:
        text = load_prompt_template("v1")
    except Exception:
        return
    db.ensure_seed_prompt(conn, "default (v1)", text, _now())


def _job_eta_display(conn: sqlite3.Connection, job: sqlite3.Row) -> str | None:
    if job["type"] != "extract" or job["status"] != "running":
        return None
    active_jobs = db.list_active_extract_jobs(conn)
    eta = _extract_job_etas(active_jobs).get(job["id"])
    return _format_duration(eta) if eta is not None else None


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    conn = get_db()
    job = db.get_job(conn, job_id)
    if job is None:
        return "job not found", 404
    errors = db.list_job_errors(conn, job_id) if job["failed"] else []
    eta = _job_eta_display(conn, job)
    return render_template("job_detail.html", job=job, errors=errors, eta=eta)


@app.route("/jobs/<int:job_id>/status.json")
def job_status_json(job_id: int):
    conn = get_db()
    if db.reap_dead_pid_jobs(conn, _now()):
        conn.commit()
    _ensure_scheduler_if_extract_jobs_pending(conn)
    job = db.get_job(conn, job_id)
    if job is None:
        return {"error": "not found"}, 404
    return {
        "status": job["status"], "total": job["total"], "done": job["done"],
        "failed": job["failed"], "error": job["error"], "eta": _job_eta_display(conn, job),
    }


def _scheduler_status() -> dict:
    """Shared by /scheduler and /scheduler/status.json - pid liveness plus
    the active extract jobs in the same model-grouped order scheduler.py
    itself visits them in, so the view reflects the real rotation rather
    than just job-creation order."""
    pidfile = _scheduler_pidfile()
    pid = None
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
        except ValueError:
            pid = None
    alive = pid is not None and db._pid_alive(pid)
    conn = get_db()
    active_jobs = db.list_active_extract_jobs(conn)
    etas = {
        jid: _format_duration(eta) if eta is not None else None
        for jid, eta in _extract_job_etas(active_jobs).items()
    }
    return {"alive": alive, "pid": pid, "active_jobs": active_jobs, "etas": etas}


@app.route("/scheduler")
def scheduler_status():
    conn = get_db()
    if db.reap_dead_pid_jobs(conn, _now()):
        conn.commit()
    _ensure_scheduler_if_extract_jobs_pending(conn)
    status = _scheduler_status()
    return render_template("scheduler.html", block_size=scheduler_module.BLOCK_SIZE, **status)


@app.route("/scheduler/status.json")
def scheduler_status_json():
    conn = get_db()
    if db.reap_dead_pid_jobs(conn, _now()):
        conn.commit()
    _ensure_scheduler_if_extract_jobs_pending(conn)
    status = _scheduler_status()
    return {
        "alive": status["alive"],
        "pid": status["pid"],
        "jobs": [
            {
                "id": j["id"], "status": j["status"], "model": j["model"],
                "prompt_name": j["prompt_name"], "done": j["done"],
                "total": j["total"], "failed": j["failed"], "eta": status["etas"][j["id"]],
            }
            for j in status["active_jobs"]
        ],
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
    _ensure_scheduler_running()
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/cluster", methods=["POST"])
def create_cluster_job():
    conn = get_db()
    model = request.form.get("model", "").strip() or None
    job_id = db.create_job(conn, "cluster", _now(), model=model)
    conn.commit()
    _spawn_job(job_id)
    return redirect(url_for("job_detail", job_id=job_id))


GENERIC_ADAPTER_SPEC = "storyindex.adapters.generic_adapter:GenericAdapter"


@app.route("/jobs/sync", methods=["POST"])
def create_sync_job():
    import json

    conn = get_db()
    mode = request.form.get("mode", "generic")
    archive_root = request.form.get("archive_root", "").strip()
    glob = request.form.get("glob", "*.html").strip() or "*.html"
    if not archive_root:
        return redirect(url_for("jobs_list"))

    if mode == "custom":
        adapter = request.form.get("adapter", "").strip()
        if not adapter:
            return redirect(url_for("jobs_list"))
        scope = {"adapter": adapter, "archive_root": archive_root, "glob": glob}
    else:
        config = {}
        title_regex = request.form.get("title_regex", "").strip()
        author_regex = request.form.get("author_regex", "").strip()
        tags_regex = request.form.get("tags_regex", "").strip()
        tags = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
        if title_regex:
            config["title_regex"] = title_regex
        if author_regex:
            config["author_regex"] = author_regex
        if tags_regex:
            config["tags_regex"] = tags_regex
        if tags:
            config["tags"] = tags
        if request.form.get("strip_html") == "on":
            config["strip_html"] = True
        scope = {"adapter": GENERIC_ADAPTER_SPEC, "archive_root": archive_root, "glob": glob, "config": config}

    job_id = db.create_job(conn, "sync", _now(), scope=json.dumps(scope))
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


@app.route("/libraries/remove", methods=["POST"])
def remove_library():
    name = request.form.get("name", "").strip()
    data = libraries.load(_libraries_path())
    if name in data["libraries"]:
        libraries.unregister(name, _libraries_path())
        data = libraries.load(_libraries_path())
        if data.get("active"):
            app.config["DB_PATH"] = Path(data["libraries"][data["active"]])
    return redirect(url_for("libraries_list"))


@app.route("/libraries/rename", methods=["POST"])
def rename_library_route():
    old_name = request.form.get("old_name", "").strip()
    new_name = request.form.get("new_name", "").strip()
    if old_name and new_name:
        try:
            libraries.rename_library(old_name, new_name, _libraries_path())
        except KeyError:
            pass
    return redirect(url_for("libraries_list"))


@app.route("/tags")
def tags_admin():
    conn = get_db()
    tag_q = request.args.get("tag_q", "").strip()
    tag_page = max(request.args.get("tag_page", 1, type=int), 1)
    tag_offset = (tag_page - 1) * TAGS_PAGE_SIZE
    tags = db.list_tags_with_counts(conn, q=tag_q or None, limit=TAGS_PAGE_SIZE, offset=tag_offset)
    tag_total = db.count_tags(conn, q=tag_q or None)

    site_q = request.args.get("site_q", "").strip()
    site_page = max(request.args.get("site_page", 1, type=int), 1)
    site_offset = (site_page - 1) * TAGS_PAGE_SIZE
    site_tags = db.list_site_tags_with_counts(conn, q=site_q or None, limit=TAGS_PAGE_SIZE, offset=site_offset)
    site_total = db.count_site_tags(conn, q=site_q or None)

    return render_template(
        "tags.html",
        tags=tags, tag_q=tag_q, tag_page=tag_page, tag_total=tag_total,
        tag_has_next=tag_offset + len(tags) < tag_total,
        site_tags=site_tags, site_q=site_q, site_page=site_page, site_total=site_total,
        site_has_next=site_offset + len(site_tags) < site_total,
    )


@app.route("/tags/<int:tag_id>/rename", methods=["POST"])
def rename_tag(tag_id: int):
    conn = get_db()
    new_name = request.form.get("name", "").strip().lower()
    if new_name:
        db.rename_tag(conn, tag_id, new_name)
        conn.commit()
    return redirect(url_for("tags_admin"))

@app.route("/tags/<int:tag_id>/delete", methods=["POST"])
def delete_tag_route(tag_id: int):
    conn = get_db()
    db.delete_tag(conn, tag_id)
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


def resolve_startup_db_path(name: str, db_path: Path, libraries_path: Path) -> Path:
    """Registers/activates `name` -> db_path (first run only - won't clobber
    an active library chosen in a previous run, e.g. via the /libraries
    switcher), then returns whichever library ends up active. Keeps the
    nav's "current library" label truthful about what's actually served,
    instead of always trusting the just-passed --db."""
    libraries.ensure_registered_and_active(name, str(db_path.resolve()), libraries_path)
    data = libraries.load(libraries_path)
    return Path(data["libraries"][data["active"]])


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Local story index browse/review app")
    parser.add_argument(
        "--db", type=Path, default=Path("library/storyindex.sqlite"),
        help="path to this library's SQLite file (default: library/storyindex.sqlite, "
             "created if missing - the same default the CLI scripts in scripts/ use)",
    )
    parser.add_argument("--library-name", default=None, help="name to register this --db under (default: filename)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    name = args.library_name or args.db.stem
    app.config["DB_PATH"] = resolve_startup_db_path(name, args.db, _libraries_path())

    conn = db.connect(app.config["DB_PATH"])
    reaped = db.reap_stale_jobs(conn, _now())
    conn.commit()
    if reaped:
        print(f"recovered from an unclean shutdown: marked {len(reaped)} stuck job(s) failed: {reaped}")
    _ensure_scheduler_if_extract_jobs_pending(conn)
    conn.close()

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
