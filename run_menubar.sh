#!/usr/bin/env bash
# Launch TFC Coursework Automator macOS Menu Bar Status App
set -euo pipefail
cd "$(dirname "$0")"

echo "🎓 Starting TFC Coursework Automator macOS Menu Bar App..."
python3 menubar.py
