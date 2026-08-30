#!/usr/bin/env bash
# Back up a library's SQLite file. Uses `sqlite3 <db> ".backup ..."` rather
# than a plain file copy: this database runs in WAL mode (see db.connect),
# where recent writes can sit in a separate -wal file rather than the main
# .sqlite file, so a raw `cp` can silently miss them or copy a
# half-written state. The .backup command is safe to run while the app (or
# a job) is using the database.
#
# Usage: ./scripts/backup.sh [path/to/library.sqlite]
#   (defaults to library/storyindex.sqlite, same default the app/scripts use)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SRC="${1:-library/storyindex.sqlite}"

if [ ! -f "$SRC" ]; then
  echo "no such file: $SRC"
  echo "usage: $0 [path/to/library.sqlite]"
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 CLI not found - install it (e.g. 'apt install sqlite3',"
  echo "'brew install sqlite') so backups can be taken safely while the"
  echo "database is in WAL mode. Refusing to fall back to a plain file"
  echo "copy, which can miss recent writes still sitting in the -wal file."
  exit 1
fi

mkdir -p backups
NAME="$(basename "$SRC" .sqlite)"
DEST="backups/${NAME}-$(date +%Y%m%d-%H%M%S).sqlite"

sqlite3 "$SRC" ".backup '$DEST'"
echo "backed up $SRC -> $DEST"

echo
echo "existing backups:"
ls -lh backups/ | tail -n +2
