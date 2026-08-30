"""Persistent scheduler for `extract` jobs.

Each extract job makes one slow local-model call per story, and Ollama's
KV-cache reuses a request's shared prefix only with whatever the
*immediately preceding* request to that model was. Every extract job has
a different, fixed instruction prefix (it's tagging a different facet) -
so running several extract jobs as independent subprocesses means their
calls interleave in whatever order the OS happens to schedule them, and
essentially every call becomes a full prefix-cache miss instead of
reusing the previous call's prefix. Benchmarked: the same 5 stories x 2
facets took 44s interleaved vs 25s processed as two back-to-back blocks -
a ~44% cut from scheduling alone, no prompt changes, no quality loss.

This module is the fix: one long-lived process, round-robining across
every queued/running extract job in blocks of BLOCK_SIZE stories, so a
job's fixed prefix stays warm for BLOCK_SIZE consecutive calls before the
scheduler switches to another job's (differently-prefixed) work. A block
size much smaller than a job's total (e.g. a 40k-item backlog) still
gives most of the caching benefit while keeping the scheduler responsive
to new jobs - a handful of single-story jobs queued alongside a huge one
each complete within one block-worth of latency rather than waiting for
the whole backlog to drain.

Within a round, jobs are visited grouped by model (see the `sorted(...,
key=...)` in the main loop), not just in creation order. Two jobs on
*different* models cost far more to alternate between than two jobs on
the same model with different prompts: a prefix-cache miss just means
recomputing that request's prompt tokens, but a model switch means Ollama
evicting one model's weights from VRAM and loading the other's - visiting
job A (model X), job B (model Y), job C (model X) in that order forces
two full model loads per round for no reason, when A and C could run
back-to-back on the same loaded model. Grouping by model each round costs
at most one swap per distinct model per round, which is the minimum
possible when several models are in play at once.

A job is dropped mid-block, not just between blocks, the moment it's
cancelled: the per-story loop re-checks the job's own row after every
single item (cheap next to a multi-second model call) rather than only
between blocks, so cancelling a job while it's mid-block wastes at most
one in-flight call instead of running out the rest of BLOCK_SIZE.

Launched as a singleton by app._ensure_scheduler_running (one instance
system-wide, not one per job) whenever an extract job is created and no
live scheduler is already running; exits on its own after sitting idle
with nothing queued.

Each block's wall-clock time and item count get written to the job row
(db.record_block_timing) so the /scheduler and job-detail views can show
a rough "time remaining" - a 40k-item backlog is a multi-day job, and a
user staring at a bare progress counter has no way to tell "almost done"
from "days left" without it.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from storyindex import db, settings
from storyindex.jobs import _now, _scope_stories, process_extract_item

BLOCK_SIZE = 10
POLL_INTERVAL_SECONDS = 2.0
IDLE_EXIT_SECONDS = 30.0


def _active_extract_jobs(conn) -> list:
    import sqlite3

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM jobs WHERE type = 'extract' AND status IN ('queued', 'running') "
        "ORDER BY created_at"
    ).fetchall()
    conn.row_factory = None
    return rows


def run_scheduler(db_path: Path) -> None:
    conn = db.connect(db_path)
    states: dict[int, dict] = {}
    rotation: list[int] = []
    idle_since: float | None = None
    try:
        while True:
            active = _active_extract_jobs(conn)
            active_by_id = {row["id"]: row for row in active}

            for jid in list(states):
                if jid not in active_by_id:
                    del states[jid]
            rotation = [jid for jid in rotation if jid in active_by_id]
            for jid in active_by_id:
                if jid not in rotation:
                    rotation.append(jid)

            if not rotation:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > IDLE_EXIT_SECONDS:
                    return
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            idle_since = None

            loaded_settings = settings.load()
            host = loaded_settings["ollama_host"]
            max_ctx_tokens = loaded_settings["max_ctx_tokens"]
            # Group by model, not just creation order: alternating models
            # forces a full VRAM swap in Ollama every switch, far more
            # expensive than the prefix-cache miss from alternating prompts
            # on the *same* model. sorted() is stable, so jobs sharing a
            # model still run in creation order relative to each other.
            for jid in sorted(rotation, key=lambda j: active_by_id[j]["model"] or ""):
                job = active_by_id[jid]
                try:
                    if jid not in states:
                        prompt = db.get_prompt(conn, job["prompt_id"]) if job["prompt_id"] else None
                        if prompt is None:
                            db.mark_job_failed(conn, jid, _now(), "job has no prompt assigned")
                            conn.commit()
                            continue
                        # exclude_job_id: this branch runs both for a
                        # genuinely new job AND for one this scheduler
                        # process is resuming (it was left 'running' by a
                        # prior scheduler process that died/restarted -
                        # db.reap_dead_pid_jobs deliberately never fails
                        # extract jobs for that, see its docstring) -
                        # excluding stories this job_id already has
                        # tag_candidates for makes the resume case pick up
                        # where it left off instead of redoing finished
                        # work. done/failed are already cumulative from any
                        # earlier run, so total is done+failed+remaining,
                        # not just len(remaining).
                        stories = _scope_stories(conn, job["scope"], exclude_job_id=jid)
                        db.set_job_total(conn, jid, job["done"] + job["failed"] + len(stories))
                        if job["status"] == "queued":
                            db.mark_job_running(conn, jid, _now(), os.getpid())
                            conn.commit()
                        states[jid] = {"stories": stories, "cursor": 0, "prompt": prompt}

                    state = states[jid]
                    block = state["stories"][state["cursor"]: state["cursor"] + BLOCK_SIZE]
                    if not block:
                        db.mark_job_done(conn, jid, _now())
                        conn.commit()
                        del states[jid]
                        continue

                    processed = 0
                    block_started = time.time()
                    for row in block:
                        process_extract_item(
                            conn, jid, job["model"], state["prompt"]["text"], state["prompt"]["name"], row, host,
                            max_ctx_tokens=max_ctx_tokens,
                        )
                        conn.commit()
                        processed += 1
                        # Re-check after every item, not just between blocks
                        # - a cancel mid-block should stop within one
                        # in-flight call, not run out the rest of BLOCK_SIZE
                        # for a job nobody wants finished anymore.
                        still_active = conn.execute(
                            "SELECT status FROM jobs WHERE id = ?", (jid,)
                        ).fetchone()
                        if still_active is None or still_active[0] not in ("queued", "running"):
                            break
                    if processed:
                        # Recorded per-job (not per-block-wall-clock) since
                        # this only counts time actually spent on this job's
                        # own calls - the app layer accounts separately for
                        # time this job spends waiting its turn while other
                        # jobs are being worked (see app._eta_seconds).
                        db.record_block_timing(conn, jid, time.time() - block_started, processed)
                        conn.commit()
                    state["cursor"] += processed
                except Exception as exc:  # noqa: BLE001 - one job's bug/DB hiccup
                    # (process_extract_item already turns model-call failures
                    # into per-story job_errors; this is a backstop for
                    # anything else, e.g. a transient "database is locked")
                    # must not take down every other job sharing this one
                    # process - mark just this job failed and keep rotating.
                    conn.rollback()
                    try:
                        db.mark_job_failed(conn, jid, _now(), f"scheduler error: {exc}")
                        conn.commit()
                    except Exception:  # noqa: BLE001 - best-effort
                        pass
                    states.pop(jid, None)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Round-robin scheduler for extract jobs")
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    run_scheduler(args.db)


if __name__ == "__main__":
    main()
