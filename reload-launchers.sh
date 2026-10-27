#!/bin/bash
# Loads (or reloads) the two LaunchAgents the correct way. Use it for the first
# install and after either plist has been edited.
#
# macOS's Background Task Management (BTM, behind System Settings > Login Items
# & Extensions) appears to key its cached job name to the *file identity* of the
# plist symlinked into ~/Library/LaunchAgents/ -- not its path, its launchd
# Label, or its content. Editing the git-tracked plist in place and reloading
# (even rebooting) leaves BTM showing the old, generic name, because the
# symlink's own identity never changes. Recreating that symlink from scratch
# gives it a new identity, which BTM picks up immediately. See docs/DEVELOPMENT.md.

set -eu

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

LABELS="com.readwisenewsdigest.run com.readwisenewsdigest.profile"

mkdir -p "$LAUNCH_AGENTS_DIR"

for label in $LABELS; do
  plist_src="$PROJECT_DIR/$label.plist"

  if [ ! -f "$plist_src" ]; then
    echo "reload-launchers.sh: missing $plist_src -- run ./install.sh first" >&2
    exit 1
  fi

  echo "Reloading $label..."

  # Tolerate the job not being loaded yet: bootout exits non-zero then, which
  # would otherwise abort the script under set -e.
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true

  # Replace whatever is at the LaunchAgents path with a brand-new symlink to the
  # same target every time, so its file identity is always new. `ln -sf`
  # replaces a symlink atomically.
  ln -sf "$plist_src" "$LAUNCH_AGENTS_DIR/$label.plist"

  launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENTS_DIR/$label.plist"

  echo "  done: $label"
done

echo
echo "Both jobs reloaded with fresh LaunchAgent links, forcing BTM's cached name to refresh."
echo "They are listed in System Settings > General > Login Items & Extensions as"
echo "readwise-news-digest-run and readwise-news-digest-profile."
