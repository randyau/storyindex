"""Zero-code fallback adapter. For archives that don't need real per-site
logic — a flat pile of .txt/.html files, no chaptering, maybe a header
worth grabbing with a regex — this is a working adapter you configure with
JSON instead of writing a `SiteAdapter` subclass. Reach for a real adapter
(see example_adapter.py) once the archive has structure this can't express:
multi-part chapters, per-story index pages, an author listing to crawl.

Every file is its own story (group_key == relpath, part_index == 0). No
custom subclass required.

Config (all keys optional; pass as a dict, or as JSON via
`--adapter-config path.json` on scripts/parse_site.py):

    {
      "title_regex": "<title>(.*?)</title>",   // searched near the top of
                                                 // the file; first capture
                                                 // group is the title
      "author_regex": "by ([^\\n<]+)",
      "author_default": "Unknown",
      "tags": ["gutenberg-import"],             // static tags stamped onto
                                                 // every story this run --
                                                 // batch tagging by config
      "tags_regex": "Tags: ([^\\n<]+)",         // comma-split into tags,
                                                 // added on top of "tags"
      "scan_chars": 4000,                       // how much of the file the
                                                 // *_regex options search
      "strip_html": false,                      // true: naive tag-strip
                                                 // for messy/unknown markup
      "keep_header_in_body": false,             // by default, whatever the
                                                 // *_regex options matched
                                                 // is cut out of body_text
                                                 // so it isn't duplicated
      "exclude_globs": ["**/index.html"]
    }

If title_regex is absent or doesn't match, the title falls back to the
filename: stem, underscores/dashes turned into spaces, title-cased.
"""

from __future__ import annotations

import fnmatch
import html as html_module
import re
from pathlib import Path

from .base import ExtractedFields

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html_module.unescape(_TAG_RE.sub(" ", text))


def _normalize(text: str) -> str:
    paragraphs = (" ".join(p.split()) for p in re.split(r"\n\s*\n", text))
    return "\n\n".join(p for p in paragraphs if p)


def _title_from_filename(relpath: str) -> str:
    stem = Path(relpath).stem if relpath else "untitled"
    words = re.sub(r"[_\-]+", " ", stem).strip()
    return words.title() if words else "Untitled"


class GenericAdapter:
    """Configurable no-code SiteAdapter. See module docstring for the
    config schema."""

    def __init__(self, root: Path, config: dict | None = None):
        self.root = Path(root)
        cfg = config or {}
        self.title_regex = re.compile(cfg["title_regex"], re.S) if cfg.get("title_regex") else None
        self.author_regex = re.compile(cfg["author_regex"], re.S) if cfg.get("author_regex") else None
        self.author_default = cfg.get("author_default", "Unknown")
        self.static_tags = tuple(cfg.get("tags", []))
        self.tags_regex = re.compile(cfg["tags_regex"], re.S) if cfg.get("tags_regex") else None
        self.scan_chars = cfg.get("scan_chars", 4000)
        self.strip_html = bool(cfg.get("strip_html", False))
        self.keep_header_in_body = bool(cfg.get("keep_header_in_body", False))
        self.exclude_globs = list(cfg.get("exclude_globs", []))

    def matches(self, relpath: str) -> bool:
        return True

    def is_story_page(self, relpath: str) -> bool:
        return not any(fnmatch.fnmatch(relpath, pat) for pat in self.exclude_globs)

    def group_key(self, relpath: str) -> str:
        return relpath

    def part_index(self, relpath: str) -> int:
        return 0

    def extract(self, text: str, relpath: str = "") -> ExtractedFields:
        scan = text[: self.scan_chars]

        header_spans: list[tuple[int, int]] = []

        title = None
        if self.title_regex:
            m = self.title_regex.search(scan)
            if m:
                title = html_module.unescape(m.group(1)).strip()
                header_spans.append(m.span())
        if not title:
            title = _title_from_filename(relpath)

        author = self.author_default
        if self.author_regex:
            m = self.author_regex.search(scan)
            if m:
                author = html_module.unescape(m.group(1)).strip()
                header_spans.append(m.span())

        tags = list(self.static_tags)
        if self.tags_regex:
            m = self.tags_regex.search(scan)
            if m:
                tags += [t.strip() for t in m.group(1).split(",") if t.strip()]
                header_spans.append(m.span())

        body_source = text
        if header_spans and not self.keep_header_in_body:
            # Drop whatever the title/author/tags regexes matched (they only
            # ever search the `scan` prefix) so the header line doesn't
            # reappear duplicated inside body_text.
            keep = []
            cursor = 0
            for start, end in sorted(header_spans):
                if start > cursor:
                    keep.append(text[cursor:start])
                cursor = max(cursor, end)
            keep.append(text[cursor:])
            body_source = "".join(keep)

        body = _strip_html(body_source) if self.strip_html else body_source
        body_text = _normalize(body)

        return ExtractedFields(title=title, author=author, body_text=body_text, tags=tuple(tags))


def _self_test() -> None:
    adapter = GenericAdapter(Path("."), config={
        "title_regex": r"<title>(.*?)</title>",
        "author_regex": r"by ([^\n<]+)",
        "tags": ["imported"],
        "tags_regex": r"Tags: ([^\n<]+)",
        "strip_html": True,
    })

    html_text = (
        "<title>The Example</title>\nby Jane Doe\nTags: adventure, mystery\n"
        "<p>Once upon a time...</p>\n\n<p>The end.</p>"
    )
    fields = adapter.extract(html_text, relpath="stories/the-example.html")
    assert fields.title == "The Example", fields.title
    assert fields.author == "Jane Doe", fields.author
    assert fields.tags == ("imported", "adventure", "mystery"), fields.tags
    assert "Once upon a time..." in fields.body_text
    assert "<p>" not in fields.body_text
    assert "Jane Doe" not in fields.body_text, fields.body_text
    assert "The Example" not in fields.body_text, fields.body_text

    bare = GenericAdapter(Path("."))
    fields2 = bare.extract("Just plain text.\n\nSecond paragraph.", relpath="raw/my_old_story-final.txt")
    assert fields2.title == "My Old Story Final", fields2.title
    assert fields2.author == "Unknown"
    assert fields2.tags == ()
    assert fields2.body_text == "Just plain text.\n\nSecond paragraph."

    assert bare.group_key("a/b.txt") == "a/b.txt"
    assert bare.part_index("a/b.txt") == 0
    excluding = GenericAdapter(Path("."), config={"exclude_globs": ["**/index.html"]})
    assert not excluding.is_story_page("stories/index.html")
    assert excluding.is_story_page("stories/chapter.html")

    print("generic_adapter self-test: OK")


if __name__ == "__main__":
    _self_test()
