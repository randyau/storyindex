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
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from storyindex import db
from storyindex.classify import ExtractionError, extract_tags
from storyindex.cluster import DEFAULT_EMBED_MODEL, canonical_name, cluster_tag_texts
from storyindex.signature import StorySignature

COMMIT_EVERY = 10


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def row_to_sig(row: sqlite3.Row) -> StorySignature:
    return StorySignature(
        id=row["id"], group_id=row["group_id"], part_index=row["part_index"],
        title=row["title"], author=row["author"], body_text=row["body_text"],
        source_relpath=row["source_relpath"], content_hash=row["content_hash"],
        ingested_at=row["ingested_at"],
    )


def _scope_stories(conn: sqlite3.Connection, scope: str | None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    if scope == "untagged":
        rows = conn.execute(
            """
            SELECT * FROM stories s
            WHERE s.status = 'active'
              AND NOT EXISTS (SELECT 1 FROM story_tags st WHERE st.story_id = s.id)
            """
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM stories WHERE status = 'active'").fetchall()
    conn.row_factory = None
    return rows


def run_extract_job(db_path: Path, job_id: int) -> None:
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

        stories = _scope_stories(conn, job["scope"])
        db.set_job_total(conn, job_id, len(stories))
        db.mark_job_running(conn, job_id, _now(), os.getpid())
        conn.commit()

        since_commit = 0
        for row in stories:
            sig = row_to_sig(row)
            try:
                tags = extract_tags(sig, model=job["model"], prompt_text=prompt["text"])
            except ExtractionError:
                db.increment_job_progress(conn, job_id, failed=1)
            else:
                db.insert_candidates(
                    conn, story_id=row["id"], tags=tags,
                    prompt_version=prompt["name"], model=job["model"],
                    created_at=_now(), job_id=job_id,
                )
                db.increment_job_progress(conn, job_id, done=1)
            since_commit += 1
            if since_commit >= COMMIT_EVERY:
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

        clusters = cluster_tag_texts(distinct_texts, model=embed_model)

        since_commit = 0
        for cluster in clusters:
            name = canonical_name(cluster.members, counts)
            tag_id = db.get_or_create_tag(conn, name, _now())
            for text in cluster.members:
                for row in rows_by_text[text]:
                    db.link_story_tag(conn, row["story_id"], tag_id, source="model", job_id=job_id)
                    db.mark_candidate_clustered(conn, row["id"])
                since_commit += 1
                db.increment_job_progress(conn, job_id, done=1)
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


RUNNERS = {
    "extract": run_extract_job,
    "cluster": run_cluster_job,
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
    run_job(args.db, args.job_id)


if __name__ == "__main__":
    main()
