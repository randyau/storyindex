#!/usr/bin/env python3
"""Generic driver: walk an archive tree, defer to a SiteAdapter, emit
StorySignature JSON files into a drop folder. See
docs/crawler-parser-contract.md for the contract this implements, and
src/storyindex/adapters/example_adapter.py for the adapter template.

This script is site-agnostic on purpose — it never hardcodes a site's
directory conventions. All of that fragility lives in the adapter you pass
via --adapter, which typically stays out of version control (see
.gitignore and docs/crawler-parser-contract.md) since it can encode
enough structural detail to fingerprint the source site.

Usage:
    python scripts/parse_site.py \\
        --adapter storyindex.adapters.example_adapter:ExampleAdapter \\
        --archive-root archive/site-a --out drop/ \\
        [--vocab-out drop/site_tags_vocab.json]

--adapter is `module.path:ClassName`, importable from src/ or elsewhere on
sys.path, and constructed as AdapterClass(archive_root).

No adapter to write? storyindex.adapters.generic_adapter:GenericAdapter is a
configurable, zero-code fallback (filename-as-title, optional regexes,
static/batch tags) — see its module docstring. Point --adapter-config at a
JSON file to configure it:

    python scripts/parse_site.py \\
        --adapter storyindex.adapters.generic_adapter:GenericAdapter \\
        --archive-root my-download/ --out drop/ \\
        --adapter-config my-adapter-config.json \\
        --glob "*.html,*.txt"

--glob accepts a comma-separated list of patterns (default: *.html) so
plain-text archives work without an HTML-only assumption.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load_adapter_class(spec: str):
    module_name, _, class_name = spec.partition(":")
    if not class_name:
        raise SystemExit(f"--adapter must be 'module.path:ClassName', got: {spec!r}")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def build_adapter(adapter_class, archive_root: Path, config_path: Path | None):
    """Construct AdapterClass(archive_root) or, if --adapter-config was
    given and the class accepts it, AdapterClass(archive_root, config)."""
    if config_path is None:
        return adapter_class(archive_root)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    try:
        return adapter_class(archive_root, config)
    except TypeError:
        print(f"note: {adapter_class.__name__} doesn't accept a config argument, ignoring --adapter-config")
        return adapter_class(archive_root)


def call_extract(adapter, text: str, relpath: str):
    """Pass relpath to adapter.extract() only if it accepts a second
    parameter — keeps single-arg adapters (extract(self, html)) working."""
    params = inspect.signature(adapter.extract).parameters
    if len(params) >= 2:
        return adapter.extract(text, relpath)
    return adapter.extract(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adapter", required=True, help="module.path:ClassName")
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--vocab-out", type=Path, default=None,
        help="optional: if the adapter defines tag_vocab(), write code->label JSON here",
    )
    parser.add_argument(
        "--adapter-config", type=Path, default=None,
        help="optional: JSON file passed as a second constructor arg, e.g. for generic_adapter.GenericAdapter",
    )
    parser.add_argument(
        "--glob", default="*.html",
        help="comma-separated glob(s) of files to walk, relative to --archive-root (default: *.html)",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    AdapterClass = load_adapter_class(args.adapter)
    adapter = build_adapter(AdapterClass, args.archive_root, args.adapter_config)

    if args.vocab_out is not None:
        tag_vocab = getattr(adapter, "tag_vocab", None)
        if callable(tag_vocab):
            vocab = tag_vocab()
            args.vocab_out.write_text(
                json.dumps(vocab, indent=2, sort_keys=True), encoding="utf-8"
            )
            print(f"wrote {len(vocab)} tag definitions to {args.vocab_out}")
        else:
            print(f"{args.adapter} has no tag_vocab(), skipping --vocab-out")

    total = written = skipped = failed = 0
    seen_paths: set[Path] = set()
    patterns = [p.strip() for p in args.glob.split(",") if p.strip()]

    for pattern in patterns:
        for path in args.archive_root.rglob(pattern):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            relpath = path.relative_to(args.archive_root).as_posix()
            total += 1
            if not adapter.matches(relpath) or not adapter.is_story_page(relpath):
                skipped += 1
                continue

            try:
                html_text = path.read_text(encoding="utf-8", errors="replace")
                fields = call_extract(adapter, html_text, relpath)
                group_key = adapter.group_key(relpath)
                part_idx = adapter.part_index(relpath)
            except Exception as exc:  # noqa: BLE001 - log and keep going across a large tree
                print(f"FAILED  {relpath}: {exc}")
                failed += 1
                continue

            sig = {
                "id": sha1(relpath),
                "group_id": sha1(group_key),
                "part_index": part_idx,
                "title": fields.title,
                "author": fields.author,
                "body_text": fields.body_text,
                "source_relpath": relpath,
                "content_hash": sha1(fields.body_text),
                "ingested_at": datetime.datetime.utcnow().isoformat() + "Z",
                "tags": list(getattr(fields, "tags", ())),
            }
            (args.out / f"{sig['id']}.json").write_text(
                json.dumps(sig, ensure_ascii=False), encoding="utf-8"
            )
            written += 1
            if written % 1000 == 0:
                print(f"... {written} written")

    print(f"\ndone. total_html={total} written={written} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
