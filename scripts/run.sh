#!/usr/bin/env bash
# Start the web app. Thin wrapper around `uv run python -m storyindex.app`
# so day-to-day use doesn't require remembering the module invocation -
# any arguments you pass through (--db, --port, --host, --library-name)
# are forwarded as-is; see `uv run python -m storyindex.app --help` for
# the full list.
#
# With no arguments, this reads/writes library/storyindex.sqlite (created
# on first run) and binds to http://localhost:8765/ - open that in a
# browser once this prints "Running on".
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

exec uv run python -m storyindex.app "$@"
