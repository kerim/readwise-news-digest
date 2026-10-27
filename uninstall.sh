#!/bin/bash
# Stops the two background jobs and removes their links from ~/Library/LaunchAgents.
# It leaves everything else alone; the last lines say how to remove the rest.

set -eu

for label in com.readwisenewsdigest.run com.readwisenewsdigest.profile; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
  echo "stopped and removed $label"
done

cat <<'MSG'

The jobs are gone. To remove the rest:
  - the Keychain items:
      security delete-generic-password -a readwise-news-digest -s readwise-news-digest-token
      security delete-generic-password -a newsdigest -s newsdigest-anthropic-api-key
  - the `newsdigest` tags in Reader (and the notes it saved, which carry the tags
    newsdigest-report and newsdigest-suggestions): delete them in Reader if you like;
  - this folder (it holds your settings and the record of what was tagged).
MSG
