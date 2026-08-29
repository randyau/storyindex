"""SiteAdapter interface. See docs/crawler-parser-contract.md section 2."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Protocol


@dataclass(frozen=True)
class ExtractedFields:
    title: str
    author: str
    body_text: str
    tags: tuple[str, ...] = field(default_factory=tuple)


class SiteAdapter(Protocol):
    def matches(self, relpath: str) -> bool:
        """Does this adapter own this path? (e.g. by directory root)"""

    def is_story_page(self, relpath: str) -> bool:
        """False for index/nav/asset pages that should be skipped entirely."""

    def group_key(self, relpath: str) -> str:
        """Identity shared by all parts of one story."""

    def part_index(self, relpath: str) -> int:
        """Order within the group. 0 for standalone / first part."""

    def extract(self, html: str) -> ExtractedFields:
        """Plain-text fields for one story part. No HTML/nav/boilerplate."""


_DEFAULT_BLOCK_TAGS = frozenset(
    {"p", "li", "div", "blockquote", "dd", "dt", "section",
     "h4", "header", "footer", "ul", "ol", "dl"}
)
_DEFAULT_SKIP_TAGS = frozenset({"script", "style", "table", "nav"})


class HtmlBlockTextExtractor(HTMLParser):
    """Reusable plain-text extractor for "prose inside one container
    element" pages — the common shape for story archive sites.

    Captures text found inside the first element matching
    (container_tag, container_attr, container_value) — e.g. an
    `<article id="content">` wrapper — paragraph-breaking on `block_tags`
    and dropping any subtree whose tag is in `skip_tags` or whose
    (tag, class) pair is in `skip_classes` (typically non-prose headers
    like a title/byline that a page repeats inside the same container).

    Not every site's markup fits this shape cleanly; subclass HTMLParser
    directly for anything more irregular. See
    src/storyindex/adapters/example_adapter.py for a worked example.
    """

    def __init__(
        self,
        container_tag: str,
        container_attr: str,
        container_value: str,
        block_tags: frozenset[str] = _DEFAULT_BLOCK_TAGS,
        skip_tags: frozenset[str] = _DEFAULT_SKIP_TAGS,
        skip_classes: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.container_tag = container_tag
        self.container_attr = container_attr
        self.container_value = container_value
        self.block_tags = block_tags
        self.skip_tags = skip_tags
        self.skip_classes = skip_classes
        self.in_container = False
        self.skip_tag: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if not self.in_container:
            if tag == self.container_tag and attrs_d.get(self.container_attr) == self.container_value:
                self.in_container = True
            return
        if self.skip_tag is not None:
            return
        cls = (attrs_d.get("class") or "").split()
        if tag in self.skip_tags or any(tag == t and c in cls for t, c in self.skip_classes):
            self.skip_tag = tag
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in self.block_tags:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if not self.in_container:
            return
        if tag == self.container_tag:
            self.in_container = False
            self.skip_tag = None
            return
        if self.skip_tag is not None:
            if tag == self.skip_tag:
                self.skip_tag = None
            return
        if tag in self.block_tags:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self.in_container and self.skip_tag is None:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        paragraphs = (" ".join(p.split()) for p in raw.split("\n\n"))
        return "\n\n".join(p for p in paragraphs if p)


def extract_block_text(
    html_text: str,
    container_tag: str,
    container_attr: str,
    container_value: str,
    **kwargs,
) -> str:
    """Convenience wrapper around HtmlBlockTextExtractor for the common case."""
    parser = HtmlBlockTextExtractor(container_tag, container_attr, container_value, **kwargs)
    parser.feed(html_text)
    return parser.text()
