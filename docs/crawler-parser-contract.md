# Crawler → Parser Contract

This document defines the boundary between the crawler (wget, run outside this repo,
against the live site) and the parser (lives in this repo, reads the wget archive,
emits `StorySignature` records). Nothing but `StorySignature` JSON ever crosses from
crawler output into the rest of this system.

## 1. Crawl step (wget)

Run from a machine/process outside this repo. Produces a mirrored directory tree,
no repo dependency.

```bash
wget \
  --recursive \
  --level=inf \
  --no-remove-listing \
  --page-requisites \
  --adjust-extension \
  --no-parent \
  --no-clobber \
  --domains=example.com \
  --no-host-directories \
  --directory-prefix=archive/site-a \
  --restrict-file-names=windows \
  --wait=2 \
  --random-wait \
  --limit-rate=200k \
  -e robots=off \
  --user-agent="Mozilla/5.0 (compatible; archive-tool/1.0)" \
  --tries=3 \
  --timeout=30 \
  --waitretry=5 \
  -o wget.log \
  https://example.com/
```

Flag notes:

- `--recursive --level=inf --no-remove-listing` — this is `--mirror` spelled out
  manually, **minus** the `-N` (timestamping) it normally implies. We can't use
  `--mirror` as-is here: it bakes in `-N`, which breaks resuming an interrupted
  crawl (see `--no-clobber` note below), so the equivalent flags are given
  explicitly with `-nc` substituted for the `-N` that `--mirror` would have added.
- `--page-requisites` — pull images/css for archival completeness. Parser ignores
  these; text only crosses the contract boundary.
- `--adjust-extension` — ensures HTML pages get `.html` even if served without one,
  so the parser can glob reliably.
- `--no-parent` — never climb above the starting path.
- `--domains=example.com` — hard restriction to the target domain; no off-site
  crawling even if pages link out.
- `--no-host-directories` — do **not** nest output under a directory named after
  the domain. Combined with `--directory-prefix`, this means the domain name never
  appears anywhere in the on-disk path, which matters because that path (or a hash
  of it) is what ends up in `StorySignature`.
- `--directory-prefix=archive/site-a` — every crawl lands under a fixed local root
  (`archive/`) inside an **opaque, operator-chosen label** (`site-a`, not the real
  domain). Pick a label that doesn't reveal the source; write down the
  label→domain mapping somewhere outside this repo if you need to remember it
  later, not in any file that reaches the indexer.
- **No `-k` / `--convert-links`.** Deliberate. Keeping the raw directory structure
  intact (paths as the site itself lays them out, just rooted under the opaque
  label instead of the domain) is what makes the relative path a stable,
  recoverable identifier. Rewriting links for offline browsing would make that lossy.
- `--wait=2 --random-wait` — base 2s delay between requests, jittered 0.5x–1.5x, to
  avoid hammering a site with thousands of pages.
- `--limit-rate=200k` — caps bandwidth as a second politeness lever independent of
  request pacing.
- `--tries=3 --timeout=30 --waitretry=5` — resilient to transient failures without
  retry-storming the server.
- `--no-clobber` — this, not `--timestamping`, is the correct resume mechanism for
  an interrupted recursive crawl. If we used `-N` instead: when a page is already
  on disk and unchanged, wget skips re-downloading it — but that also means it
  never re-parses that page for outbound links, so recursion silently dead-ends at
  every already-fetched page and a re-run makes no further progress. `-nc` behaves
  differently for recursion: when it skips re-downloading an existing `.html`
  file, it still **loads it from disk and parses it for links**, so a re-run of
  the exact same command continues discovering and fetching anything not yet
  retrieved. This means a Ctrl-C'd crawl is safe to just re-run verbatim until it
  completes. (`-N`/`-nc` are mutually exclusive in wget — this is also why
  `--mirror`, which implies `-N`, isn't used directly above.) Tradeoff: `-nc`
  won't pick up content that changed on the server since the last crawl of that
  page — fine for a one-time archival pass; a deliberate freshness re-crawl later
  is a separate, explicit job, not something this flag set tries to do for you.
- `-o wget.log` — full log kept alongside the archive for auditing what was fetched.
  Keep this log with the crawler, outside this repo — it's the one place the real
  domain still shows up.

For a multi-thousand-page site, expect this to run for hours. That's the intended
tradeoff for politeness; don't shrink `--wait` to speed it up.

If you crawl multiple sites, give each its own label under `archive/` (`archive/site-a`,
`archive/site-b`, …). `relpath` in the contract below is always computed relative to
that per-label root, so it's stable and collision-free across sites without ever
encoding which real domain it came from.

## 2. Parser step (this repo)

The parser walks the mirrored tree and, for each file, defers to whichever
`SiteAdapter` claims it. All site-specific fragility (selectors, filename
conventions, part-numbering schemes) lives inside one adapter module per source
site and never leaks past this interface.

```python
class SiteAdapter:
    def matches(self, relpath: str) -> bool:
        """Does this adapter own this path? (e.g. by host prefix)"""

    def is_story_page(self, relpath: str) -> bool:
        """False for index/nav/asset pages that should be skipped entirely."""

    def group_key(self, relpath: str) -> str:
        """Identity shared by all parts of one story. Standalone story: still
        returns a key (e.g. the relpath itself)."""

    def part_index(self, relpath: str) -> int:
        """Order within the group. 0 for standalone / first part."""

    def extract(self, html: str) -> ExtractedFields:
        """Returns {title, author, body_text, tags}. body_text is plain
        text, paragraphs separated by \\n\\n, no HTML/scripts/nav/boilerplate.
        tags is a list of site-provided category codes/strings, or [] if the
        source site doesn't have any — see section 3a, these are NOT the
        same thing as the local-LLM tag pipeline's output."""
```

Driver behavior:

1. Walk the archive tree, skip non-HTML and page-requisite assets.
2. For each HTML file, find the first matching adapter; skip files with no match
   (log them — new adapter needed) and files where `is_story_page` is false.
3. Group extracted files by `group_key`, sort each group by `part_index`.
4. Emit one `StorySignature` JSON file per part into the drop folder.

`scripts/parse_site.py` is this driver, and is site-agnostic — it takes an
adapter via `--adapter module.path:ClassName` and never hardcodes a site's
conventions itself. Writing a new adapter: copy
`src/storyindex/adapters/example_adapter.py` to
`src/storyindex/adapters/site_<label>.py` (matching the crawl label under
`archive/`) and adapt it. Files named `adapters/site_*.py` are gitignored —
see the note in section 3a on why.

```bash
python scripts/parse_site.py \
  --adapter storyindex.adapters.site_a:SiteAAdapter \
  --archive-root archive/site-a --out drop/ \
  --vocab-out drop/site_tags_vocab.json
```

Don't want to write a class at all? `src/storyindex/adapters/generic_adapter.py`
is a configurable, zero-code fallback: filename-as-title (falls back
automatically when no regex matches or none is given), optional regexes for
title/author/tags searched near the top of the file, a static tag list for
batch-tagging everything in one run, and an optional naive HTML-tag-strip for
messy markup. One file = one story (no chaptering). Configure it with JSON:

```bash
python scripts/parse_site.py \
  --adapter storyindex.adapters.generic_adapter:GenericAdapter \
  --archive-root my-download/ --out drop/ \
  --adapter-config my-adapter-config.json \
  --glob "*.html,*.txt"
```

`--glob` takes a comma-separated list (default `*.html`) so plain-text
archives don't need to pretend to be HTML. See the module docstring in
generic_adapter.py for the full config schema. Reach for a real `SiteAdapter`
once the archive has structure this can't express — multi-part chapters, a
per-story index page, non-trivial grouping.

## 3. StorySignature (the contract)

One JSON file per story-part, filename `{id}.json`, written to the drop folder that
this repo's ingestion step consumes.

```json
{
  "id": "sha1(relpath)",
  "group_id": "sha1(group_key)",
  "part_index": 0,
  "title": "string",
  "author": "string",
  "body_text": "plain text, paragraphs as \\n\\n, no HTML",
  "source_relpath": "authors/j-smith/story-1.html",
  "content_hash": "sha1(body_text)",
  "ingested_at": "2026-08-26T00:00:00Z",
  "tags": ["mc", "mf", "md"]
}
```

Field rules:

| Field | Rule |
|---|---|
| `id` | `sha1` of `relpath` (relative to the per-site label root, e.g. `archive/site-a/`). Stable across re-runs; re-crawling the same page reproduces the same `id`. |
| `group_id` | `sha1` of `group_key`. Shared by all parts of a multi-page story; used by the indexer to stitch parts back together. |
| `part_index` | Integer, 0-based, defines reading order within `group_id`. |
| `title`, `author` | Required. Empty string if genuinely unrecoverable from the page — never omit the field. |
| `body_text` | Required. Plain text only — no HTML tags, no nav/ads/comments/boilerplate. Paragraph breaks as `\n\n`. |
| `source_relpath` | Kept, but relative to the per-site label root only (per decision 2026-08-26) — never includes the domain or the `archive/site-a` prefix itself, just the path the site uses internally (e.g. `authors/j-smith/story-1.html`). Useful for debugging bad extractions without reintroducing the domain name into the signature. |
| `content_hash` | `sha1` of `body_text`, distinct from `id`. Lets the indexer detect a changed source page (re-tag) vs. a genuinely new story (`id` unchanged, `content_hash` changed). |
| `ingested_at` | UTC timestamp of when the parser produced this signature (not the original crawl time). |
| `tags` | Optional, defaults to `[]`. Site-provided category codes/strings, read straight off the source page (e.g. a controlled vocabulary the site already maintains). Never touched by the local-LLM extraction/clustering pipeline. |

Nothing else crosses the boundary. No raw HTML, no live URLs, no crawler
internals — only these signature files reach the indexer/tagging side of the
system.

## 3a. Site-provided tags vs. LLM-derived tags

Some sites hand us an already-curated tag vocabulary for free (a fixed set of
category codes with human-readable definitions). Where that exists, it's
worth keeping — it's higher-precision than anything the local model will
infer, and it costs nothing to carry over.

These live in **two entirely separate storage systems**, never merged at the
data layer:

- `tags` / `story_tags` / `tag_candidates` — the local-LLM two-pass pipeline
  (free-form extraction → clustering → human review). Fully mutable: rename,
  merge, delete, approve.
- `site_tags` / `story_site_tags` — populated directly from
  `StorySignature.tags` at ingest time. Treated as **read-only / largely
  immutable** in the UI — no rename/merge/delete affordances, since they're
  a direct reflection of the source site's own categorization, not a model
  guess a human needs to curate.

The web app surfaces both as visually identical tag pills on browse/search/
story pages — the distinction is invisible to the end user, but the
underlying tables, and what's allowed to mutate them, stay separate.

If a source site publishes human-readable definitions for its codes (e.g. a
`Categories` index page), an adapter may optionally define a `tag_vocab()`
method returning a `code -> label` dict; `scripts/parse_site.py --vocab-out`
picks it up automatically and writes it as a one-time sidecar file, which
`scripts/extract_tags.py --site-tags-vocab` then loads to give `site_tags`
nicer display labels than the bare code. This is a convenience, not part of
the `StorySignature` contract itself, and adapters that don't define
`tag_vocab()` are skipped silently.
