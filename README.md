# storyindex

A tool that runs on your own computer to organize, tag, and search a
personal collection of text — story archives, fanfic downloads, scanned
books, academic papers, whatever you've got a pile of. It reads your
files, can optionally use a local AI model to suggest descriptive tags for
each one, and gives you a simple web page to browse, search, and manage
the whole collection.

**Nothing you feed it ever leaves your computer.** There's no cloud
service, no account, no upload step. The only thing this tool ever talks
to over the network is [Ollama](https://ollama.com), an AI model runner
that also runs entirely on your own machine — not a hosted API.

## Try it in 5 minutes

You'll need [uv](https://docs.astral.sh/uv/getting-started/installation/)
installed first (a Python tool manager — one command on any OS, see the
link). Then, from a terminal in this folder:

```bash
./scripts/setup.sh                # one-time: installs everything this needs
./scripts/demo_gutenberg.sh       # downloads 4 public-domain children's books
                                   # (Alice in Wonderland, The Wizard of Oz,
                                   # Peter Pan, The Secret Garden) and builds
                                   # a small demo library from them
./scripts/run.sh --db library/gutenberg-demo.sqlite
```

Then open **http://localhost:8765/** in your browser. You'll see the four
books, searchable and browsable right away. If you also have
[Ollama](https://ollama.com) installed, the in-app `/ollama` page will
help you install a tagging model — after that, add a prompt under
`/prompts` (or use the one already there) and start a tagging pass under
`/jobs` to see the AI-tagging side working too, on real (if very
G-rated) books.

This is the same thing you'd do with your own files — the demo just skips
the "where do I get files" step by grabbing a few small, well-known public-
domain ones for you to try it on first.

## Using it with your own files

1. **Get your files into a folder on disk**, one file per item (story,
   chapter, paper, whatever). Plain text or HTML both work.
2. **Start the app** (`./scripts/run.sh`) and open it in your browser.
3. Under **Jobs → sync a library from disk**, point it at your folder. No
   coding needed for typical cases — it can use the filename as the title,
   or you can give it a couple of small pattern rules (called "regexes")
   to pull the title/author out of each file's own content instead.
4. Under **Prompts**, write (or edit) the instructions the AI model should
   follow when suggesting tags — what to look for, what *not* to bother
   tagging, how many tags to produce. This is the one part you'll want to
   customize for your own collection; the shipped default is tuned for
   tagging fiction by trope/theme, so if you're tagging something else
   (papers by subfield, comics by genre, whatever), write a prompt aimed
   at that instead.
5. Under **Jobs → start an extraction pass**, run your prompt over your
   files. This needs [Ollama](https://ollama.com) running locally — the
   in-app `/ollama` page will tell you if it's missing and help you set it
   up. A **/scheduler** status page shows what's currently running and a
   rough time-remaining estimate, since a big collection can take a while.
6. Under **Jobs → start a clustering pass**, fold the AI's raw tag
   suggestions into a clean, deduplicated tag list, and review/approve
   them from the **review queue**.
7. **Browse, search, and filter** by tag from the home page any time —
   this part never needs the AI model at all.

Everything above also works by hand instead of with the AI: add a story,
add a tag, tag a story yourself — no model required if you'd rather curate
manually or don't have anything set up yet.

## Keeping it running

- **Back up your library** any time with `./scripts/backup.sh` (or point
  it at a specific file: `./scripts/backup.sh library/mycollection.sqlite`)
  — it makes a timestamped copy under `backups/`. Your whole collection,
  its tags, and its job history live in one file, so this one command is
  the entire backup story.
- **Multiple collections**: run more than one library by passing a
  different `--db` file to `./scripts/run.sh`; the nav bar's library
  switcher lets you jump between any you've used before, from the same
  running app.
- **Stopping/restarting**: Ctrl-C the terminal running `./scripts/run.sh`
  to stop it. An unclean stop (crash, killed terminal) never corrupts your
  data — anything mid-job just gets marked "failed" next time you start
  it back up, with whatever it had already finished intact.

---

## For more technical detail

The sections above are the whole story for most people. What follows is
for anyone customizing ingestion, writing a real `SiteAdapter`, hosting
this for LAN access, or contributing to the code itself.

### What it does

- **Ingest** a folder of text/HTML files — story pages, papers, OCR'd
  scans, anything with a title and a body — into a local SQLite database,
  either via a zero-code generic parser (filename-as-title, optional
  regexes, batch tagging) or a hand-written `SiteAdapter` for archives
  with real structure (multi-part chapters, per-item index pages, etc).
- **Tag** stories two ways: by hand, or via a two-pass local-model
  pipeline (free-form extraction → embedding-based clustering into a
  canonical vocabulary), with a review queue for approving/rejecting
  model proposals.
- **Schedule** extraction jobs through one persistent background process
  that round-robins across whatever's queued, batched to keep the local
  model's cache warm rather than thrashing between jobs on every call —
  see `/scheduler` for what's currently running and roughly how long it
  has left.
- **Browse/search** with full-text search (SQLite FTS5), tag pages,
  author pages, and a reading view.
- **Manage** multiple libraries (one SQLite file = one collection),
  remove/restore stories, add a story by hand, rename/merge/delete tags.
- **Run jobs** (extraction, clustering, sync) as background processes from
  the web UI, with per-item failure detail and one-click revert of
  everything a bad job produced.

Despite the name, nothing about the pipeline is fiction-specific. A
"story" is just this tool's word for one document in your corpus — the
same ingest → prompt-driven tag extraction → clustering → browse/search
flow works for any batch of text you'd want to run the same extraction
prompt over: academic papers in a field, OCR'd comic/manga scans converted
to text, a folder of articles, whatever. The only two requirements are
that each item is (or can be turned into) plain text/HTML on disk, and
that you write an extraction prompt suited to what you're tagging instead
of reusing the shipped fiction-trope one.

### Requirements

- Python >= 3.10 (managed for you by `uv`, see `scripts/setup.sh`)
- [Ollama](https://ollama.com) running locally, for the tagging pipeline
  (browsing/search/manual tagging all work without it). See the in-app
  `/ollama` status page for install/model-pull guidance, or `/settings` to
  point at a non-default host/port.

### Manual install (without `scripts/setup.sh`)

```bash
uv sync --extra dev
```

### The `scripts/` helpers

- `scripts/setup.sh` — one-time environment setup (`uv sync`, checks for
  Ollama). Safe to re-run any time.
- `scripts/run.sh` — starts the web app; forwards any arguments straight
  to `python -m storyindex.app` (`--db`, `--port`, `--host`,
  `--library-name`).
- `scripts/demo_gutenberg.sh` — downloads a handful of small public-domain
  books and builds a demo library, for trying the tool out or smoke-
  testing a change.
- `scripts/backup.sh` — safe, live-safe backup of a library's SQLite file
  (uses SQLite's own backup API rather than a plain file copy, since the
  database runs in WAL mode).
- `scripts/sync_archive.py`, `scripts/extract_tags.py`,
  `scripts/cluster_tags.py`, `scripts/parse_site.py` — the underlying
  Python CLI tools the web UI's job runner also uses, if you'd rather
  drive ingestion/tagging without the browser (e.g. scripting a large
  batch import).

### Running and connecting

`storyindex.app` starts Flask's built-in dev server and binds by default to
`0.0.0.0:8765` — reachable at `http://localhost:8765/` from the same
machine, and at `http://<this machine's LAN IP>:8765/` from another device
on your network (e.g. to browse from a phone or another computer). Nothing
here is meant to be exposed past your own LAN — there's no auth, and this
is a single-user local tool.

Useful flags (pass through `./scripts/run.sh` or directly to
`uv run python -m storyindex.app`):

```bash
# Custom port, and a name to register this library under in the library
# switcher (defaults to the filename without .sqlite).
./scripts/run.sh --db mylibrary.sqlite --port 9000 --library-name my-fic

# Bind only to localhost (refuse LAN connections):
./scripts/run.sh --db mylibrary.sqlite --host 127.0.0.1
```

`library/`, `archive/`, and `drop/` are all gitignored — nothing under
them is meant to enter the repo. Pass `--db somewhere-else.sqlite` (web
app) or `--db` (any `scripts/*.py` tool) to use a different location;
every tool defaults to `library/storyindex.sqlite` if you don't.

If you have more than one library (multiple `--db` files you've launched
against over time), the nav bar's "library: ..." link switches between
them without restarting the server — see `/libraries`.

For an archive with real per-site structure (chaptered stories, an index
page, a fixed tag vocabulary), write a `SiteAdapter` instead of using the
generic parser — see `docs/crawler-parser-contract.md` and
`src/storyindex/adapters/example_adapter.py`.

### Architecture

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
- `src/storyindex/jobs.py` — job runner. Extract jobs run through the
  persistent scheduler (`src/storyindex/scheduler.py`); cluster/sync jobs
  each run as their own detached subprocess so the web app stays
  responsive while a long pass runs.
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

### Testing

```bash
uv run pytest tests/ -q
```

Tests use a temp SQLite file per test and never call a real Ollama server —
model calls are monkeypatched. No network access is required to run the
suite.

### Privacy constraints (load-bearing, not just style)

- Story text and embeddings never leave the machine. The only network calls
  in this codebase go to `ollama_client.DEFAULT_HOST` (`localhost:11434` by
  default, configurable in `/settings`, still meant to stay local).
- Real source domains never appear in this repo, in commit history, or in
  anything a `StorySignature` carries — see
  `docs/crawler-parser-contract.md` section 3 for the exact field rules.
