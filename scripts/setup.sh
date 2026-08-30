#!/usr/bin/env bash
# One-time setup: installs Python dependencies via uv, and checks whether
# Ollama (needed for the tagging pipeline, not for browsing/search) is
# available. Safe to re-run any time - uv sync is idempotent, and this
# script never touches your data (library/, archive/, drop/).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== checking for uv =="
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. storyindex uses uv to manage its Python"
  echo "environment instead of a manually-activated venv - see"
  echo "https://docs.astral.sh/uv/getting-started/installation/ for a"
  echo "one-line install command for your platform, then re-run this script."
  exit 1
fi
echo "found: $(uv --version)"

echo
echo "== installing Python dependencies =="
uv sync --extra dev

echo
echo "== checking for Ollama (optional - only needed for tagging) =="
if command -v ollama >/dev/null 2>&1; then
  echo "found: $(ollama --version 2>&1 | head -n1)"
  echo "start it with 'ollama serve' (or the in-app /ollama page can do this"
  echo "for you), then pull a model, e.g.:"
  echo "  ollama pull qwen2.5:7b-instruct   # tagging"
  echo "  ollama pull nomic-embed-text      # clustering"
else
  echo "not found. Browsing, search, and manual tagging all work without it,"
  echo "but the automatic tagging pipeline (extraction/clustering passes)"
  echo "needs a local Ollama server. Install it from https://ollama.com"
  echo "whenever you're ready to use that part - nothing else in this repo"
  echo "requires it."
fi

echo
echo "== done =="
echo "next: ./scripts/run.sh to start the app, then open http://localhost:8765/"
