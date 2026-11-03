#!/usr/bin/env sh
# Mnemo bootstrap (macOS/Linux). Everything installs inside this folder.
#   sh bootstrap.sh
set -e
cd "$(dirname "$0")"

command -v python3 >/dev/null 2>&1 || { echo "Python 3.10+ is required." >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
  || { echo "Python 3.10+ required." >&2; exit 1; }

echo "Working offline is fine -- the app runs with no models at all."
echo "This downloads the local model stack (~5 GB) into ./vendor/ for better quality."
echo

python3 -m mnemo.setup "$@"
python3 -m mnemo.doctor
python3 -m mnemo status
