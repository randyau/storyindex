#!/usr/bin/env python3
"""Batch driver for the extraction pass: reads StorySignature JSON files
from a drop folder, upserts them into SQLite, and runs the local-model
extraction pass on any story that doesn't yet have candidates for the
given prompt version.

Usage:
    python scripts/extract_tags.py --drop-dir drop/ --db storyindex.sqlite \
        --model qwen2.5:14b-instruct --prompt-version v1
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from storyindex import db
from storyindex.classify import ExtractionError, extract_tags
from storyindex.signature import iter_signatures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop-dir", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt-version", default="v1")
    parser.add_argument(
        "--force", action="store_true",
        help="re-run extraction even if candidates already exist for this prompt version",
    )
    parser.add_argument(
        "--site-tags-vocab", type=Path, default=None,
        help="optional code->label JSON (from parse_site_a.py --vocab-out) to "
             "backfill nicer site_tags labels",
    )
    args = parser.parse_args()

    conn = db.connect(args.db)

    if args.site_tags_vocab is not None:
        vocab = json.loads(args.site_tags_vocab.read_text(encoding="utf-8"))
        db.load_site_tag_vocab(conn, vocab)
        conn.commit()
        print(f"loaded {len(vocab)} site tag labels from {args.site_tags_vocab}")

    total = 0
    tagged = 0
    skipped = 0
    failed = 0

    for sig in iter_signatures(args.drop_dir):
        total += 1
        db.upsert_story(conn, sig)
        conn.commit()

        if not args.force and db.has_candidates(conn, sig.id, args.prompt_version):
            skipped += 1
            continue

        try:
            tags = extract_tags(sig, model=args.model, prompt_version=args.prompt_version)
        except ExtractionError as exc:
            print(f"FAILED  {sig.id}: {exc}")
            failed += 1
            continue

        db.insert_candidates(
            conn,
            story_id=sig.id,
            tags=tags,
            prompt_version=args.prompt_version,
            model=args.model,
            created_at=datetime.datetime.utcnow().isoformat() + "Z",
        )
        conn.commit()
        tagged += 1
        print(f"OK      {sig.id} ({sig.title!r}): {tags}")

    print(
        f"\ndone. total={total} tagged={tagged} skipped={skipped} failed={failed}"
    )


if __name__ == "__main__":
    main()
