# storyindex

A local, privacy-preserving archive/tagging/search tool for a personal
collection of stories. Point it at a folder of downloaded story pages, sync
it in, tag it (by hand or with a local model), and browse/search it from a
small web UI. Everything stays on your machine — no story text or embedding
ever leaves it, and the only network calls this tool makes are to a local
Ollama server on `localhost`.

## What it does

- **Ingest** a folder of story files into a local SQLite database, either via
  a zero-code generic parser (filename-as-title, optional regexes, batch
  tagging) or a hand-written `SiteAdapter` for archives with real structure
  (multi-part chapters, per-story index pages, etc).
- **Tag** stories two ways: by hand, or via a two-pass local-model pipeline
  (free-form extraction → embedding-based clustering into a canonical
  vocabulary), with a review queue for approving/rejecting model proposals.
- **Browse/search** with full-text search (SQLite FTS5), tag pages, author
  pages, and a reading view.
- **Manage** multiple libraries (one SQLite file = one collection), remove/
  restore stories, add a story by hand, rename/merge/delete tags.
- **Run jobs** (extraction, clustering, sync) as background subprocesses from
  the web UI, with per-item failure detail and one-click revert of
  everything a bad job produced.

## Requirements

- Python >= 3.10
- [Ollama](https://ollama.com) running locally, for the tagging pipeline
  (browsing/search/manual tagging all work without it). See the in-app
  `/ollama` status page for install/model-pull guidance, or `/settings` to
  point at a non-default host/port.

## Install

```bash
uv sync --extra dev
```

## Quick start

```bash
# 1. Get a folder of story files onto disk however you like (see
#    docs/crawler-parser-contract.md for the wget-based crawl this was
#    originally built around, if you're pulling from a live site).

# 2. Launch the web app.
uv run python -m storyindex.app --db mylibrary.sqlite

# 3. Open http://localhost:8765/ and use the "sync a library from disk"
#    form under /jobs to point it at your folder — no code required for a
#    simple filename-as-title / regex-based import. Add prompts under
#    /prompts and start an extraction pass under /jobs once Ollama is up.
```

For an archive with real per-site structure (chaptered stories, an index
page, a fixed tag vocabulary), write a `SiteAdapter` instead of using the
generic parser — see `docs/crawler-parser-contract.md` and
`src/storyindex/adapters/example_adapter.py`.

## Architecture

```
crawl (external, e.g. wget)          -- never part of this repo
  -> archive/<label>/...             -- opaque label, never the real domain
       -> SiteAdapter / GenericAdapter (scripts/parse_site.py)
            -> StorySignature JSON   -- the one contract boundary
                 -> db.upsert_story  -- SQLite (one file per library)
                      -> extract_tags (local model, pass 1: free text)
                           -> cluster_tag_texts (pass 2: canonical vocab)
                                -> review queue (human approves/rejects)
```

- `src/storyindex/adapters/` — the `SiteAdapter` protocol, a zero-code
  `GenericAdapter`, and an example. Site-specific adapters (`site_*.py`) are
  gitignored on purpose — they can encode enough of a site's real structure
  to fingerprint it.
- `src/storyindex/db.py` — schema + all SQLite access. FTS5-backed search,
  soft-delete (`stories.status`), job/prompt tables with full provenance
  (`job_id` on every candidate/tag row, so a bad run is a mechanical revert).
- `src/storyindex/jobs.py` — job runner, launched as a detached subprocess
  per job so the web app stays responsive while a long tagging pass runs.
- `src/storyindex/classify.py` / `cluster.py` — the two tagging passes.
- `src/storyindex/app.py` — the Flask web app (routes, templates).
- `src/storyindex/templates/_macros.html` — shared UI building blocks
  (story lists, tag pills, pagination, confirm/action forms) — new pages
  should reuse these rather than hand-rolling markup, see `CLAUDE.md`.
- `src/storyindex/libraries.py` / `settings.py` — small JSON files (not part
  of any one library's sqlite) for the library switcher and machine-level
  preferences (theme, Ollama host, default models).
- `docs/crawler-parser-contract.md` — the full contract between the crawl
  step and the parser, including the privacy rules around opaque archive
  labels and what fields are/aren't allowed to carry the source domain.

## Testing

```bash
uv run pytest tests/ -q
```

Tests use a temp SQLite file per test and never call a real Ollama server —
model calls are monkeypatched. No network access is required to run the
suite.

## Privacy constraints (load-bearing, not just style)

- Story text and embeddings never leave the machine. The only network calls
  in this codebase go to `ollama_client.DEFAULT_HOST` (`localhost:11434` by
  default, configurable in `/settings`, still meant to stay local).
- Real source domains never appear in this repo, in commit history, or in
  anything a `StorySignature` carries — see
  `docs/crawler-parser-contract.md` section 3 for the exact field rules.
