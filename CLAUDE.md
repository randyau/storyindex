# CLAUDE.md

Guidance for Claude Code sessions working in this repo. See `README.md` for
what the project does and how to run it.

## Hard constraints — do not violate these

- **Story text and embeddings never leave the machine.** The only network
  calls anywhere in this codebase go through `storyindex/ollama_client.py`
  to a local Ollama server (`localhost:11434` by default, or whatever
  `/settings` → `ollama_host` points at — still meant to stay local). Never
  add a call to a remote LLM/embedding API, analytics, telemetry, or any
  other external service.
- **Real source domains never appear in this repo** — not in code, not in
  commit messages, not in test fixtures, not in comments. `archive/` and
  `drop/` are gitignored precisely because they can contain this. Adapter
  files named `adapters/site_*.py` are also gitignored since they can encode
  enough of a real site's structure to fingerprint it — only
  `adapters/base.py`, `adapters/example_adapter.py`, and
  `adapters/generic_adapter.py` are meant to be committed. If you're writing
  a new site-specific adapter, name it `site_<opaque-label>.py` and don't ask
  the user to tell you the real domain in a way that ends up in a file this
  repo tracks. See `docs/crawler-parser-contract.md` for the full contract.

## Working conventions established in this project

- **TDD, and commit as you go.** Write/extend a test alongside (or before)
  each behavior change, run `uv run pytest tests/ -q` before committing,
  and keep the suite green at every commit — don't batch a large amount of
  unverified work into one commit.
- **Be token-efficient during a work chunk.** Don't narrate step-by-step
  while working; give a summary at the end of a chunk or when you need
  feedback/clarification, not continuously.
- **Reuse the shared UI macros.** `src/storyindex/templates/_macros.html`
  has `story_list`, `tag_pill` / `tag_pill_link`, `pager`, and `action_form`
  — the recurring patterns across browse/tag/author/review/job pages. Import
  it with `{% import "_macros.html" as m with context %}` (the `with
  context` is required for the macros' internal `url_for()` calls to work)
  and use the existing macro rather than hand-rolling another variant of a
  story `<li>`, a pill, a pager, or a single-button confirm form. If a new
  page needs a genuinely new repeated pattern, add it to `_macros.html`
  rather than inlining it — the whole point is one implementation per UI
  pattern, not a fifth slightly-different tag pill.
- **Theming.** All colors go through the CSS custom properties defined in
  `base.html` (`--bg`, `--fg`, `--fg-muted`, `--border`, `--link`, `--accent`,
  `--danger`, `--pill-bg`, `--input-*`) under `:root[data-theme="dark"]` /
  `:root[data-theme="light"]`. Never hardcode a color in a template or a new
  CSS rule — add a variable if the existing set doesn't cover it, so both
  themes stay consistent and legible. The theme is read from
  `settings.load()` and injected into every template via the
  `_inject_settings` context processor in `app.py` (available as `settings`
  in any template, not just ones that explicitly pass it) — rendered
  server-side into `<html data-theme="...">` so there's no flash-of-wrong-
  theme on load.
- **Jobs run as detached subprocesses**, not threads — see `_spawn_job` in
  `app.py` and the `run_*_job` functions in `jobs.py`. Each opens its own
  DB connection and commits progress incrementally (`COMMIT_EVERY`) so a
  killed process leaves committed work intact and gets reaped as "failed"
  rather than stuck "running" (`db.reap_stale_jobs`/`reap_dead_pid_jobs`).
  If you add a new job type, follow this pattern: own connection, periodic
  commit, `db.record_job_error` per-item on failure (don't let one bad file/
  story abort the whole batch), `db.mark_job_done`/`mark_job_failed` in a
  `try/except/finally` that always closes the connection.
- **Job provenance.** Anything a job produces (`tag_candidates.job_id`,
  `story_tags.job_id`) should be traceable back to the job that made it, so
  `revert_job` can cleanly undo exactly one run without touching
  hand-curated or other-job data.
- **Test pattern for Flask routes**: a local `_client(tmp_path)` helper that
  sets `app.config["DB_PATH"]`/`["LIBRARIES_PATH"]`/`["SETTINGS_PATH"]` to
  paths under `tmp_path`, and monkeypatches `storyindex.app._spawn_job` (or
  the relevant `extract_tags`/`cluster_tag_texts`/`ollama_client` function)
  to avoid subprocesses or real network calls. Follow the existing test
  files (`tests/test_app_*.py`) rather than inventing a new fixture style.
  Model-call fakes take a `host=None` keyword (mirrors the real signatures)
  even when the fake ignores it, so monkeypatched functions don't reject the
  keyword the real code now passes.
- **No migration framework.** Schema changes in `db.py` are additive
  (`CREATE TABLE IF NOT EXISTS` in `SCHEMA`, or a guarded `ALTER TABLE ...
  ADD COLUMN` catching `OperationalError`) — this is a single local sqlite
  file per user, not a multi-environment deployment. Don't add Alembic or
  similar; it would be solving a problem this project doesn't have.
- **Don't add feature flags, config toggles, or abstractions "for later."**
  This is a single-user local tool; match the existing minimal style rather
  than generalizing preemptively.
- **Use `uv`, not bare `pip`/`python`.** This repo has no committed
  virtualenv or lockfile workflow beyond what `uv` manages, so drive
  everything through it rather than a manually-activated venv:
  - `uv sync --extra dev` — create/update `.venv` from `pyproject.toml`
    (installs `pytest` too via the `dev` extra).
  - `uv run pytest tests/ -q` — run the suite (equivalent to the
    `python -m pytest tests/ -q` used elsewhere in this doc/README).
  - `uv run python -m storyindex.app --db mylibrary.sqlite` — launch the
    web app.
  - `uv add <package>` / `uv remove <package>` — change dependencies;
    this edits `pyproject.toml` directly, so don't hand-edit
    `dependencies`/`optional-dependencies` there instead.
  Don't reach for `pip install`, `python -m venv`, or a system Python
  interpreter — `uv run`/`uv sync` keep the environment reproducible from
  `pyproject.toml` alone.

## Where things live

- `src/storyindex/app.py` — Flask routes + view logic.
- `src/storyindex/db.py` — schema and all SQL.
- `src/storyindex/jobs.py` — background job runner (subprocess entry point).
- `src/storyindex/classify.py`, `cluster.py` — the two local-model tagging
  passes.
- `src/storyindex/adapters/` — `SiteAdapter` protocol + generic/example
  adapters (site-specific ones are gitignored, see above).
- `src/storyindex/libraries.py`, `settings.py` — small JSON config files
  outside any one library's sqlite (library switcher, theme/Ollama/model
  preferences).
- `src/storyindex/templates/` — Jinja templates; `_macros.html` holds the
  shared building blocks (see above).
- `scripts/` — standalone CLI drivers (`parse_site.py`, `extract_tags.py`,
  `cluster_tags.py`) that wrap the same library code the web app's job
  runner uses.
- `docs/crawler-parser-contract.md` — the crawl→parse contract, privacy
  rules, and `StorySignature` field-by-field spec.
- `tests/` — one file per module/feature area; `tests/conftest.py` has the
  shared `conn`/`make_sig` fixtures.
