"""Loading and validating StorySignature records.

See docs/crawler-parser-contract.md for the authoritative field definitions.
This module only consumes signatures — it has no knowledge of the crawler
or the parser that produced them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

REQUIRED_FIELDS = (
    "id",
    "group_id",
    "part_index",
    "title",
    "author",
    "body_text",
    "source_relpath",
    "content_hash",
    "ingested_at",
)


@dataclass(frozen=True)
class StorySignature:
    id: str
    group_id: str
    part_index: int
    title: str
    author: str
    body_text: str
    source_relpath: str
    content_hash: str
    ingested_at: str
    # Site-provided tags (e.g. a source site's own category codes), if any.
    # Optional and separate from the local-LLM tagging pipeline: these land
    # in their own site_tags/story_site_tags tables, never in tags/story_tags.
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> "StorySignature":
        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            raise ValueError(f"signature missing required fields: {missing}")
        fields = {f: data[f] for f in REQUIRED_FIELDS}
        fields["tags"] = tuple(data.get("tags", ()))
        return cls(**fields)


def load_signature(path: Path) -> StorySignature:
    with path.open("r", encoding="utf-8") as fh:
        return StorySignature.from_dict(json.load(fh))


def iter_signatures(drop_dir: Path) -> Iterator[StorySignature]:
    """Yield every StorySignature found under drop_dir, skipping bad files
    with a printed warning rather than aborting the whole batch."""
    for path in sorted(drop_dir.glob("*.json")):
        try:
            yield load_signature(path)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"skipping invalid signature {path}: {exc}")
