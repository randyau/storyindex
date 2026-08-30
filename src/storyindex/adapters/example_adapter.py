"""Template SiteAdapter — copy this file, rename it, and adapt it to a
real source site. It is deliberately self-contained and runnable against
the fabricated sample HTML in `_SAMPLE_INDEX_HTML` / `_SAMPLE_CHAPTER_HTML`
below (see the bottom of this file), so you can confirm your adaptation
still behaves correctly with `python -m storyindex.adapters.example_adapter`
before pointing it at a real archive.

Assumed (fictional) site layout — swap this whole section for whatever
your real target site actually does:

    {root}/{story-slug}/index.html       -- story metadata + chapter order
    {root}/{story-slug}/chapter-1.html   -- chapter 1 body
    {root}/{story-slug}/chapter-2.html   -- chapter 2 body, etc.
    {root}/about.html, {root}/browse/... -- assorted non-story pages

index.html example:

    <html><head>
    <meta name="story:author" content="Jane Doe">
    <meta name="story:tags" content="adventure, mystery">
    </head><body>
    <h1 class="story-title">The Example</h1>
    <ol class="chapters">
      <li><a href="chapter-1.html">Chapter 1</a></li>
      <li><a href="chapter-2.html">Chapter 2</a></li>
    </ol>
    </body></html>

chapter-N.html example:

    <html><head>
    <meta name="story:author" content="Jane Doe">
    <meta name="story:tags" content="adventure, mystery">
    </head><body>
    <nav>...site chrome to ignore...</nav>
    <article id="content">
      <h1 class="story-title">The Example</h1>
      <p>Once upon a time...</p>
      <p>The end.</p>
    </article>
    </body></html>

Swap the constants/regexes below for whatever your real site uses, keep
the five SiteAdapter methods, and you have a working adapter. See
src/storyindex/adapters/base.py for the shared HtmlBlockTextExtractor
this leans on for body-text extraction, and
docs/crawler-parser-contract.md for the full interface contract.
"""

from __future__ import annotations

import html
import re
from functools import cache
from pathlib import Path

from .base import ExtractedFields, extract_block_text

# --- 1. Which top-level dirs are NOT stories? Adjust to your site. -------

NON_STORY_DIRS = {"about", "browse", "search", "static"}

# --- 2. How to pull metadata + chapter order out of a page. Adjust the --
#        regexes/selectors to match your real site's markup.

_AUTHOR_RE = re.compile(r'<meta name="story:author" content="([^"]*)"')
_TAGS_RE = re.compile(r'<meta name="story:tags" content="([^"]*)"')
_TITLE_RE = re.compile(r'<h1 class="story-title">(.*?)</h1>', re.S)
_CHAPTER_LIST_RE = re.compile(r'<ol class="chapters">(.*?)</ol>', re.S)
_HREF_RE = re.compile(r'<a href="([^"/]+\.html)">')


class ExampleAdapter:
    """Reference implementation of the SiteAdapter protocol. Copy this
    class (and the regexes above it) as your starting point."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def matches(self, relpath: str) -> bool:
        # Return True for anything this adapter should even consider.
        # If you're only running one adapter over one archive root, "own
        # everything under this root" (True) is usually fine — is_story_page
        # below does the real filtering. Return False here instead if this
        # adapter should only claim a subset (e.g. multiple adapters sharing
        # one archive root, split by URL prefix).
        return True

    def _story_dir(self, relpath: str) -> str | None:
        parts = relpath.split("/")
        if len(parts) != 2 or parts[0] in NON_STORY_DIRS:
            return None
        return parts[0]

    def is_story_page(self, relpath: str) -> bool:
        story_dir = self._story_dir(relpath)
        if story_dir is None:
            return False
        return relpath.split("/")[1] != "index.html"

    def group_key(self, relpath: str) -> str:
        return self._story_dir(relpath) or relpath

    @cache  # noqa: B019 - adapter instances are short-lived (one parse run), not a leak risk
    def _chapter_order(self, story_dir: str) -> tuple[str, ...]:
        # Read the authoritative chapter order from the story's own index
        # page rather than guessing from filenames (e.g. don't assume
        # "chapter-2.html" sorts after "chapter-10.html" correctly, or that
        # every site even numbers files that predictably).
        index_path = self.root / story_dir / "index.html"
        text = index_path.read_text(encoding="utf-8", errors="replace")
        m = _CHAPTER_LIST_RE.search(text)
        if not m:
            return ()
        return tuple(_HREF_RE.findall(m.group(1)))

    def part_index(self, relpath: str) -> int:
        story_dir, filename = relpath.split("/")
        order = self._chapter_order(story_dir)
        try:
            return order.index(filename)
        except ValueError:
            return 0

    def extract(self, html_text: str) -> ExtractedFields:
        title_m = _TITLE_RE.search(html_text)
        title = html.unescape(title_m.group(1)).strip() if title_m else ""

        author_m = _AUTHOR_RE.search(html_text)
        author = html.unescape(author_m.group(1)).strip() if author_m else ""

        tags_m = _TAGS_RE.search(html_text)
        tags = tuple(t.strip() for t in tags_m.group(1).split(",") if t.strip()) if tags_m else ()

        body_text = extract_block_text(
            html_text,
            container_tag="article",
            container_attr="id",
            container_value="content",
            # Drop the repeated title heading inside the body container;
            # add more (tag, class) pairs here for any other furniture
            # your real site repeats inside its content wrapper.
            skip_classes=frozenset({("h1", "story-title")}),
        )
        return ExtractedFields(title=title, author=author, body_text=body_text, tags=tags)

    # Optional: if your site publishes human-readable definitions for its
    # tags (a "categories" index page), parse them here and the generic
    # driver (scripts/parse_site.py --vocab-out) will pick this up
    # automatically. Delete this method entirely if not applicable.
    def tag_vocab(self) -> dict[str, str]:
        return {}


# --- self-test against fabricated sample HTML -----------------------------
# Not a real site. Confirms the adapter's own logic is internally
# consistent; run after adapting this file to make sure you haven't broken
# the wiring before pointing it at a real archive.

_SAMPLE_INDEX_HTML = """<html><head>
<meta name="story:author" content="Jane Doe">
<meta name="story:tags" content="adventure, mystery">
</head><body>
<h1 class="story-title">The Example</h1>
<ol class="chapters">
  <li><a href="chapter-1.html">Chapter 1</a></li>
  <li><a href="chapter-2.html">Chapter 2</a></li>
</ol>
</body></html>"""

_SAMPLE_CHAPTER_HTML = """<html><head>
<meta name="story:author" content="Jane Doe">
<meta name="story:tags" content="adventure, mystery">
</head><body>
<nav>site chrome to ignore</nav>
<article id="content">
<h1 class="story-title">The Example</h1>
<p>Once upon a time...</p>
<p>The end.</p>
</article>
</body></html>"""


def _self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        story_dir = root / "the-example"
        story_dir.mkdir()
        (story_dir / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
        (story_dir / "chapter-1.html").write_text(_SAMPLE_CHAPTER_HTML, encoding="utf-8")
        (story_dir / "chapter-2.html").write_text(_SAMPLE_CHAPTER_HTML, encoding="utf-8")

        adapter = ExampleAdapter(root)
        assert adapter.is_story_page("the-example/chapter-1.html")
        assert not adapter.is_story_page("the-example/index.html")
        assert adapter.group_key("the-example/chapter-1.html") == "the-example"
        assert adapter.part_index("the-example/chapter-1.html") == 0
        assert adapter.part_index("the-example/chapter-2.html") == 1

        fields = adapter.extract((story_dir / "chapter-1.html").read_text(encoding="utf-8"))
        assert fields.title == "The Example", fields.title
        assert fields.author == "Jane Doe", fields.author
        assert fields.tags == ("adventure", "mystery"), fields.tags
        assert fields.body_text == "Once upon a time...\n\nThe end.", repr(fields.body_text)

    print("example_adapter self-test: OK")


if __name__ == "__main__":
    _self_test()
