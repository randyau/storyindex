#!/usr/bin/env python3
"""Batch driver for the normalization pass: pulls every tag_candidates row
still in status='candidate', clusters the distinct tag texts by embedding
similarity, folds each cluster into one canonical row in `tags`, links every
story that proposed a member tag to that canonical tag, and marks the
candidates 'clustered'.

This does not require re-running the extraction pass — it only consumes
what's already in tag_candidates. Safe to re-run: already-clustered rows are
skipped, and get_or_create_tag/link_story_tag are idempotent.

Conventional location, matching the web UI and the other scripts/*.py
defaults: the library lives at library/storyindex.sqlite. Override with
--db if yours lives elsewhere.

Usage:
    python scripts/cluster_tags.py
    # equivalent to:
    python scripts/cluster_tags.py --db library/storyindex.sqlite \
        --embed-model nomic-embed-text --threshold 0.82
"""

from __future__ import annotations

import argparse
import datetime
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from storyindex import db
from storyindex.cluster import DEFAULT_EMBED_MODEL, DEFAULT_SIMILARITY_THRESHOLD, canonical_name, cluster_tag_texts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db", type=Path, default=REPO_ROOT / "library" / "storyindex.sqlite",
        help="path to the library's SQLite file (default: library/storyindex.sqlite)",
    )
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD)
    args = parser.parse_args()

    conn = db.connect(args.db)
    rows = db.pending_candidate_rows(conn)

    if not rows:
        print("no pending candidates to cluster")
        return

    # Group candidate rows by tag text so we embed each distinct string once.
    rows_by_text: dict[str, list] = defaultdict(list)
    counts: Counter = Counter()
    for row in rows:
        rows_by_text[row["tag_text"]].append(row)
        counts[row["tag_text"]] += 1

    distinct_texts = list(rows_by_text.keys())
    print(f"{len(rows)} pending candidates, {len(distinct_texts)} distinct tag strings")

    clusters = cluster_tag_texts(distinct_texts, model=args.embed_model, threshold=args.threshold)
    print(f"formed {len(clusters)} clusters")

    now = datetime.datetime.utcnow().isoformat() + "Z"
    for cluster in clusters:
        name = canonical_name(cluster.members, counts)
        tag_id = db.get_or_create_tag(conn, name, now)
        print(f"  [{name!r}] <- {sorted(set(cluster.members))}")

        for text in cluster.members:
            for row in rows_by_text[text]:
                db.link_story_tag(conn, row["story_id"], tag_id, source="model")
                db.mark_candidate_clustered(conn, row["id"])

    conn.commit()
    print("done.")


if __name__ == "__main__":
    main()
