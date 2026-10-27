#!/bin/bash
# Gets this folder ready to run, on a Mac:
#   1. a private Python environment (.venv) holding the one dependency;
#   2. the two tiny launcher programs the background jobs start through;
#   3. the two launchd job files, written for wherever you put this folder.
# It does not load the jobs and it needs no passwords. The README says what to do
# next (store your keys, export your feeds, describe your interests, check, load).
# Safe to run again.

set -eu

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This only runs on macOS: it uses launchd, the Keychain and macOS notifications." >&2
  exit 1
fi

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "python3 not found. Install it first, for example: brew install python" >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is needed; this one is $("$PY" -V 2>&1)." >&2
  exit 1
fi

echo "1/3  Python environment (.venv)"
"$PY" -m venv .venv
.venv/bin/python -m pip install --quiet -r requirements.txt

echo "2/3  Launchers"
./build-launchers.sh

echo "3/3  Job files"
.venv/bin/python - "$PROJECT_DIR" <<'PYEOF'
import sys
from pathlib import Path
from xml.sax.saxutils import escape

project = Path(sys.argv[1])
python = project / ".venv" / "bin" / "python"
for name in ("run", "profile"):
    template = (project / "launchd" / f"{name}.plist.template").read_text()
    text = (template.replace("@PROJECT_DIR@", escape(str(project)))
                    .replace("@PYTHON@", escape(str(python))))
    target = project / f"com.readwisenewsdigest.{name}.plist"
    target.write_text(text)
    print(f"wrote {target.name}")
PYEOF

echo
echo "Done. Next, follow the README from 'Store your two keys'."
echo "When everything is ready:  .venv/bin/python check_setup.py   and then   ./reload-launchers.sh"
