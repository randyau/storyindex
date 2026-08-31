"""Job runner: executes one extract/cluster job by id, updating its jobs
row's progress as it goes.

Launched as a detached subprocess by the web app (non-blocking — the app
returns immediately and polls the jobs row for progress), or run directly
from the CLI for the same effect the old batch scripts had:

    python -m storyindex.jobs --job-id 3 --db storyindex.sqlite

Each run_*_job function opens its own connection since it executes in its
own process; the web app never shares a live connection with it.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from storyindex import db, ollama_client, settings
from storyindex.classify import ExtractionError, extract_tags
from storyindex.cluster import DEFAULT_EMBED_MODEL, canonical_name, cluster_tag_texts
from storyindex.file_text import read_file_text
from storyindex.signature import StorySignature

COMMIT_EVERY = 10
# Extract jobs interleave a slow model call (seconds) with each write. An
# uncommitted write holds SQLite's single WAL writer slot, so batching
# COMMIT_EVERY writes here would hold that slot for the sum of several
# stories' worth of model latency - long enough to starve a concurrent
# extract job (or the Flask app itself) well past PRAGMA busy_timeout.
# Cluster jobs don't have this problem: their one slow call (embedding)
# happens once up front, before the write loop starts.
EXTRACT_COMMIT_EVERY = 1


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def row_to_sig(row: sqlite3.Row) -> StorySignature:
    return StorySignature(
        id=row["id"], group_id=row["group_id"], part_index=row["part_index"],
        title=row["title"], author=row["author"], body_text=row["body_text"],
        source_relpath=row["source_relpath"], content_hash=row["content_hash"],
        ingested_at=row["ingested_at"], media_path=row["media_path"],
    )


def _scope_stories(
    conn: sqlite3.Connection, scope: str | None, exclude_job_id: int | None = None
) -> list[sqlite3.Row]:
    """Stories an extract job should (still) process. exclude_job_id makes
    this resume-safe: it drops any story that already has a tag_candidates
    row from *this specific job*, so restarting a long-running job (the
    scheduler process got killed/crashed and picked back up, see
    scheduler.run_scheduler and db.reap_dead_pid_jobs) continues from where
    it left off instead of re-extracting - and writing duplicate
    tag_candidates for - stories it already finished."""
    conn.row_factory = sqlite3.Row
    where = ["s.status = 'active'"]
    params: list = []
    if scope == "untagged":
        where.append("NOT EXISTS (SELECT 1 FROM story_tags st WHERE st.story_id = s.id)")
    if exclude_job_id is not None:
        where.append(
            "NOT EXISTS (SELECT 1 FROM tag_candidates tc WHERE tc.story_id = s.id AND tc.job_id = ?)"
        )
        params.append(exclude_job_id)
    rows = conn.execute(f"SELECT * FROM stories s WHERE {' AND '.join(where)}", params).fetchall()
    conn.row_factory = None
    return rows


def process_extract_item(
    conn: sqlite3.Connection,
    job_id: int,
    model: str,
    prompt_text: str,
    prompt_name: str,
    row: sqlite3.Row,
    host: str,
    max_ctx_tokens: int = ollama_client.MAX_CTX_TOKENS,
) -> None:
    """Run extraction for one story and record the outcome (candidates or
    a job_error) against job_id. Shared by run_extract_job's own single-job
    loop and scheduler.py's cross-job block loop, so both paths behave
    identically on a per-story basis."""
    sig = row_to_sig(row)
    try:
        tags = extract_tags(
            sig, model=model, prompt_text=prompt_text, host=host, max_ctx_tokens=max_ctx_tokens
        )
    except ExtractionError as exc:
        db.increment_job_progress(conn, job_id, failed=1)
        db.record_job_error(conn, job_id, f"{row['title']} ({row['id']})", str(exc), _now())
    else:
        db.insert_candidates(
            conn, story_id=row["id"], tags=tags,
            prompt_version=prompt_name, model=model,
            created_at=_now(), job_id=job_id,
        )
        db.increment_job_progress(conn, job_id, done=1)


def run_extract_job(db_path: Path, job_id: int) -> None:
    """Standalone single-job runner - still used directly by the CLI
    (`python -m storyindex.jobs`) and by tests. The web app no longer
    spawns this for `extract` jobs (see app._ensure_scheduler_running /
    scheduler.py): running multiple extract jobs as independent processes
    means their Ollama calls interleave in whatever order the OS happens
    to schedule them, and each job's prompt has a different fixed
    instruction prefix - so every single call becomes a full prefix-cache
    miss instead of reusing the previous call's prefix. The scheduler
    keeps one job's calls consecutive in blocks instead."""
    conn = db.connect(db_path)
    try:
        job = db.get_job(conn, job_id)
        if job is None:
            raise ValueError(f"no such job: {job_id}")

        prompt = db.get_prompt(conn, job["prompt_id"]) if job["prompt_id"] else None
        if prompt is None:
            db.mark_job_failed(conn, job_id, _now(), "job has no prompt assigned")
            conn.commit()
            return

        # exclude_job_id makes this resume-safe: done/failed are already
        # cumulative from any prior run of this same job_id, so total is
        # recomputed as done+failed+remaining rather than overwritten with
        # just len(remaining) - see _scope_stories' docstring.
        stories = _scope_stories(conn, job["scope"], exclude_job_id=job_id)
        db.set_job_total(conn, job_id, job["done"] + job["failed"] + len(stories))
        if job["status"] == "queued":
            db.mark_job_running(conn, job_id, _now(), os.getpid())
        conn.commit()

        loaded_settings = settings.load()
        host = loaded_settings["ollama_host"]
        max_ctx_tokens = loaded_settings["max_ctx_tokens"]
        since_commit = 0
        for row in stories:
            process_extract_item(
                conn, job_id, job["model"], prompt["text"], prompt["name"], row, host,
                max_ctx_tokens=max_ctx_tokens,
            )
            since_commit += 1
            if since_commit >= EXTRACT_COMMIT_EVERY:
                conn.commit()
                since_commit = 0

        conn.commit()
        db.mark_job_done(conn, job_id, _now())
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - record it on the job, don't just crash silently
        conn.rollback()
        db.mark_job_failed(conn, job_id, _now(), str(exc))
        conn.commit()
        raise
    finally:
        conn.close()


def run_cluster_job(db_path: Path, job_id: int) -> None:
    conn = db.connect(db_path)
    try:
        job = db.get_job(conn, job_id)
        if job is None:
            raise ValueError(f"no such job: {job_id}")
        embed_model = job["model"] or DEFAULT_EMBED_MODEL

        rows = db.pending_candidate_rows(conn)
        rows_by_text: dict[str, list] = defaultdict(list)
        counts: Counter = Counter()
        for row in rows:
            rows_by_text[row["tag_text"]].append(row)
            counts[row["tag_text"]] += 1
        distinct_texts = list(rows_by_text.keys())

        db.set_job_total(conn, job_id, len(distinct_texts))
        db.mark_job_running(conn, job_id, _now(), os.getpid())
        conn.commit()

        host = settings.load()["ollama_host"]

        # Embedding (one network round trip per distinct text) is the
        # dominant cost of this pass for any real-sized backlog, so report
        # progress as each one completes rather than leaving `done` at 0
        # for however long that takes and only moving once clustering
        # starts writing results out - a job_detail page watching a large
        # cluster job would otherwise sit at a stuck-looking 0/N for most
        # of its actual runtime.
        embed_progress = {"since_commit": 0}

        def _on_embedded(_text: str) -> None:
            db.increment_job_progress(conn, job_id, done=1)
            embed_progress["since_commit"] += 1
            if embed_progress["since_commit"] >= COMMIT_EVERY:
                conn.commit()
                embed_progress["since_commit"] = 0

        clusters = cluster_tag_texts(
            distinct_texts, model=embed_model, host=host, on_embedded=_on_embedded
        )
        conn.commit()

        # `done` already reached len(distinct_texts) during embedding above
        # - this pass only writes clustering's results out, so it doesn't
        # increment progress again (that would double-count past 100%).
        since_commit = 0
        for cluster in clusters:
            name = canonical_name(cluster.members, counts)
            tag_id = db.get_or_create_tag(conn, name, _now())
            for text in cluster.members:
                for row in rows_by_text[text]:
                    db.link_story_tag(conn, row["story_id"], tag_id, source="model", job_id=job_id)
                    db.mark_candidate_clustered(conn, row["id"])
                since_commit += 1
                if since_commit >= COMMIT_EVERY:
                    conn.commit()
                    since_commit = 0

        conn.commit()
        db.mark_job_done(conn, job_id, _now())
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        db.mark_job_failed(conn, job_id, _now(), str(exc))
        conn.commit()
        raise
    finally:
        conn.close()


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _load_adapter_class(spec: str):
    module_name, _, class_name = spec.partition(":")
    if not class_name:
        raise ValueError(f"adapter spec must be 'module.path:ClassName', got: {spec!r}")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def _build_adapter(adapter_class, archive_root: Path, config: dict | None):
    """Mirrors scripts/parse_site.py build_adapter(): pass config through
    only if the adapter's constructor accepts a second argument."""
    if not config:
        return adapter_class(archive_root)
    try:
        return adapter_class(archive_root, config)
    except TypeError:
        return adapter_class(archive_root)


def _call_extract(adapter, text: str, relpath: str):
    """Mirrors scripts/parse_site.py call_extract(): pass relpath through
    only if the adapter's extract() accepts a second argument."""
    import inspect

    params = inspect.signature(adapter.extract).parameters
    if len(params) >= 2:
        return adapter.extract(text, relpath)
    return adapter.extract(text)


# 200 seemed harmless (sync's own per-item work is just disk I/O + regex,
# no slow model call like extract's), but a batch of 200 upserts - each
# rewriting a full body_text blob and its FTS index entry - held the WAL
# writer lock long enough that two concurrently-running extract jobs both
# hit "database is locked" past their 5s busy_timeout and crashed mid-run
# (observed running all three job types at once against the full library).
# Same fix as EXTRACT_COMMIT_EVERY: commit often enough that the lock is
# never held for more than one item's worth of work.
SYNC_COMMIT_EVERY = 20


def run_sync_job(db_path: Path, job_id: int) -> None:
    """Re-walk an archive root with a SiteAdapter and upsert every story
    page straight into the DB — idempotent via StorySignature.id/
    content_hash, so re-running reports new/changed/unchanged for free.
    Wraps the same adapter contract scripts/parse_site.py uses, as a
    monitorable job instead of a separate offline step."""
    conn = db.connect(db_path)
    try:
        job = db.get_job(conn, job_id)
        if job is None:
            raise ValueError(f"no such job: {job_id}")

        params = json.loads(job["scope"] or "{}")
        adapter_spec = params.get("adapter")
        archive_root = Path(params["archive_root"]) if params.get("archive_root") else None
        if not adapter_spec or not archive_root:
            db.mark_job_failed(conn, job_id, _now(), "sync job missing adapter/archive_root")
            conn.commit()
            return

        adapter_class = _load_adapter_class(adapter_spec)
        adapter = _build_adapter(adapter_class, archive_root, params.get("config"))
        patterns = [p.strip() for p in (params.get("glob") or "*.html").split(",") if p.strip()]
        save_media_path = bool(params.get("save_media_path"))

        paths = []
        seen: set[Path] = set()
        for pattern in patterns:
            for p in archive_root.rglob(pattern):
                if p in seen:
                    continue
                seen.add(p)
                relpath = p.relative_to(archive_root).as_posix()
                if adapter.matches(relpath) and adapter.is_story_page(relpath):
                    paths.append(p)

        db.set_job_total(conn, job_id, len(paths))
        db.mark_job_running(conn, job_id, _now(), os.getpid())
        conn.commit()

        since_commit = 0
        for path in paths:
            relpath = path.relative_to(archive_root).as_posix()
            try:
                html_text = read_file_text(path)
                fields = _call_extract(adapter, html_text, relpath)
                group_key = adapter.group_key(relpath)
                part_idx = adapter.part_index(relpath)
                sig = StorySignature(
                    id=_sha1(relpath), group_id=_sha1(group_key), part_index=part_idx,
                    title=fields.title, author=fields.author, body_text=fields.body_text,
                    source_relpath=relpath, content_hash=_sha1(fields.body_text),
                    ingested_at=_now(), tags=tuple(getattr(fields, "tags", ())),
                    media_path=str(path) if save_media_path else None,
                )
                db.upsert_story(conn, sig)
            except Exception as exc:  # noqa: BLE001 - one bad file shouldn't abort a 100k-file walk
                db.increment_job_progress(conn, job_id, failed=1)
                db.record_job_error(conn, job_id, relpath, str(exc), _now())
            else:
                db.increment_job_progress(conn, job_id, done=1)
            since_commit += 1
            if since_commit >= SYNC_COMMIT_EVERY:
                conn.commit()
                since_commit = 0

        conn.commit()
        db.mark_job_done(conn, job_id, _now())
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        db.mark_job_failed(conn, job_id, _now(), str(exc))
        conn.commit()
        raise
    finally:
        conn.close()


RUNNERS = {
    "extract": run_extract_job,
    "cluster": run_cluster_job,
    "sync": run_sync_job,
}


def run_job(db_path: Path, job_id: int) -> None:
    conn = db.connect(db_path)
    job = db.get_job(conn, job_id)
    conn.close()
    if job is None:
        raise ValueError(f"no such job: {job_id}")
    runner = RUNNERS.get(job["type"])
    if runner is None:
        raise ValueError(f"no runner for job type {job['type']!r}")
    runner(db_path, job_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    try:
        run_job(args.db, args.job_id)
    except Exception as exc:  # noqa: BLE001
        # _spawn_job routes this process's stdout/stderr to DEVNULL, so an
        # exception here (e.g. db.connect() itself hitting "database is
        # locked" under concurrent job load, before the runner's own
        # try/except even starts) would otherwise vanish - the job just
        # sits at "queued" forever with no pid and no error, and nothing
        # short of noticing the missing process would ever explain why.
        # Each run_*_job already marks its own failures; this is only a
        # backstop for whatever happens outside that.
        try:
            conn = db.connect(args.db)
            job = db.get_job(conn, args.job_id)
            if job is not None and job["status"] in ("queued", "running"):
                db.mark_job_failed(conn, args.job_id, _now(), f"job process crashed: {exc}")
                conn.commit()
            conn.close()
        except Exception:  # noqa: BLE001 - best-effort; don't mask the original error
            pass
        raise


if __name__ == "__main__":
    main()
