#!/usr/bin/env python3
"""CLI driver for the sync-as-a-job pipeline: walks an archive with a
SiteAdapter/GenericAdapter straight into a SQLite library - the same
storyindex.jobs.run_sync_job() code path /jobs/sync runs as a background
job from the web app, invoked here synchronously with no Flask server
needed. This is the missing "how do I get a database" step for someone
driving parse_site.py-style ingestion entirely from the command line: it
goes directly from an archive root to a queryable library.sqlite, without
detouring through parse_site.py's drop/ JSON intermediate.

Conventional locations, matching the web UI and the other scripts/*.py
defaults: archive roots live under archive/<opaque-label>/, and the
resulting library defaults to library/storyindex.sqlite (created if
missing) - the same file the web UI's "sync a library from disk" form
would write to, so a CLI-driven sync and a web-driven one interchangeably
build on the same library instead of scattering databases around.

Safe to re-run: story id/content_hash are stable, so a second pass over an
updated archive just reports new/changed/unchanged rather than duplicating.

Usage:
    python scripts/sync_archive.py \\
        --adapter storyindex.adapters.site_a:SiteAAdapter \\
        --archive-root archive/site-a
    # equivalent to:
    python scripts/sync_archive.py --db library/storyindex.sqlite \\
        --adapter storyindex.adapters.site_a:SiteAAdapter \\
        --archive-root archive/site-a

    # generic (no-adapter) mode, same config shape as the web UI's form:
    python scripts/sync_archive.py \\
        --adapter storyindex.adapters.generic_adapter:GenericAdapter \\
        --archive-root my-download/ --glob "*.html,*.txt" \\
        --adapter-config my-adapter-config.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from storyindex import db
from storyindex.jobs import run_sync_job


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db", type=Path, default=REPO_ROOT / "library" / "storyindex.sqlite",
        help="path to the library's SQLite file (default: library/storyindex.sqlite)",
    )
    parser.add_argument("--adapter", required=True, help="module.path:ClassName")
    parser.add_argument(
        "--archive-root", type=Path, required=True,
        help="crawled site root, conventionally under archive/<opaque-label>/",
    )
    parser.add_argument("--glob", default="*.html", help="comma-separated glob(s) (default: *.html)")
    parser.add_argument(
        "--adapter-config", type=Path, default=None,
        help="optional JSON config, for adapters that take one (e.g. GenericAdapter)",
    )
    args = parser.parse_args()

    scope = {
        "adapter": args.adapter,
        "archive_root": str(args.archive_root),
        "glob": args.glob,
    }
    if args.adapter_config is not None:
        scope["config"] = json.loads(args.adapter_config.read_text(encoding="utf-8"))

    conn = db.connect(args.db)
    job_id = db.create_job(conn, "sync", datetime.datetime.utcnow().isoformat() + "Z", scope=json.dumps(scope))
    conn.commit()
    conn.close()

    print(f"running sync job #{job_id} against {args.archive_root} ...")
    run_sync_job(args.db, job_id)

    conn = db.connect(args.db)
    job = db.get_job(conn, job_id)
    print(f"\ndone. status={job['status']} done={job['done']} failed={job['failed']} total={job['total']}")
    if job["failed"]:
        for err in db.list_job_errors(conn, job_id, limit=20):
            print(f"  FAILED {err['story_ref']}: {err['error']}")
    conn.close()

    if job["status"] != "done":
        raise SystemExit(f"sync job failed: {job['error']}")


if __name__ == "__main__":
    main()
