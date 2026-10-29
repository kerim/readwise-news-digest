#!/bin/bash
# Compiles the passthrough launchers for the two LaunchAgents into ./bin, and the
# small app that sends the program's notifications under its own name.
# Idempotent and cheap; re-run after editing newsdigest-launcher.c or notifier/.
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

# Notifications: an app bundle, so macOS files them under "Readwise News Digest".
# It must live here, not in a temporary folder: macOS refuses notifications from
# an app it finds in one, without asking.
APP="$OUT_DIR/Readwise News Digest.app"
mkdir -p "$APP/Contents/MacOS"
cp "$SRC_DIR/notifier/Info.plist" "$APP/Contents/Info.plist"
clang -O2 -Wall -Wextra -Werror -fobjc-arc -framework Foundation \
  -framework UserNotifications \
  -o "$APP/Contents/MacOS/readwise-news-digest-notify" "$SRC_DIR/notifier/notifier.m"
codesign --force --sign - "$APP" 2>/dev/null
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"
echo "built $APP"
