#!/usr/bin/env bash
# Tutorial/smoke-test script: downloads four small, well-known public-
# domain children's books from Project Gutenberg and syncs them into a
# demo library, so a new user (or a fresh checkout) has something real to
# tag/browse/search within a couple of minutes, without needing their own
# archive yet. See the README's "try it in 5 minutes" section for the
# manual, step-by-step version of what this automates.
#
# Safe to re-run - sync is idempotent, and this only ever writes under
# archive/gutenberg-demo/ and --db (both gitignored, never touching a
# real library unless you pass one in).
#
# Usage: ./scripts/demo_gutenberg.sh [path/to/demo.sqlite]
#   (defaults to library/gutenberg-demo.sqlite, kept separate from
#   library/storyindex.sqlite so this never mixes into a real library)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DB="${1:-library/gutenberg-demo.sqlite}"
ARCHIVE="archive/gutenberg-demo"

mkdir -p "$ARCHIVE"

# id, filename, title (for the echo below only - the adapter derives the
# real title from each filename once downloaded)
BOOKS=(
  "11:alice-in-wonderland:Alice's Adventures in Wonderland (Lewis Carroll)"
  "55:wizard-of-oz:The Wonderful Wizard of Oz (L. Frank Baum)"
  "16:peter-pan:Peter Pan (J. M. Barrie)"
  "113:the-secret-garden:The Secret Garden (Frances Hodgson Burnett)"
)

echo "== downloading from Project Gutenberg (gutenberg.org) =="
for entry in "${BOOKS[@]}"; do
  IFS=':' read -r id name label <<< "$entry"
  dest="$ARCHIVE/$name.txt"
  if [ -f "$dest" ]; then
    echo "already have: $label"
    continue
  fi
  echo "fetching: $label"
  curl -sL "https://www.gutenberg.org/files/$id/$id-0.txt" -o "$dest"
done

echo
echo "== syncing into $DB =="
uv run python scripts/sync_archive.py \
  --db "$DB" \
  --adapter storyindex.adapters.generic_adapter:GenericAdapter \
  --archive-root "$ARCHIVE" \
  --glob "*.txt" \
  --adapter-config scripts/gutenberg_demo_config.json

echo
echo "== done =="
echo "start the app against this demo library and open it in a browser:"
echo "  ./scripts/run.sh --db $DB"
echo "then add an extraction prompt under /prompts and run a tagging pass"
echo "under /jobs once Ollama is up - see the README for the full walkthrough."
