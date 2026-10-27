#!/bin/bash
# Compiles the passthrough launchers for the two LaunchAgents into ./bin.
# Idempotent and cheap; re-run after editing newsdigest-launcher.c.
#
# Each job gets its own binary so Login Items & Extensions names it after the
# job instead of "bash" or "python3". See newsdigest-launcher.c.

set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SRC_DIR/newsdigest-launcher.c"
OUT_DIR="$SRC_DIR/bin"

command -v clang >/dev/null 2>&1 || {
  echo "clang not found. Install the Xcode Command Line Tools:" >&2
  echo "  xcode-select --install" >&2
  exit 1
}

[ -f "$SOURCE" ] || { echo "Missing: $SOURCE" >&2; exit 1; }

mkdir -p "$OUT_DIR"
for name in readwise-news-digest-run readwise-news-digest-profile; do
  clang -O2 -Wall -Wextra -Werror -o "$OUT_DIR/$name" "$SOURCE"
  chmod 755 "$OUT_DIR/$name"
  echo "built $OUT_DIR/$name"
done
