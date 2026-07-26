#!/usr/bin/env bash
# Launch TFC Coursework Automator macOS Menu Bar Status App
set -euo pipefail
cd "$(dirname "$0")"

echo "🎓 Starting TFC Coursework Automator macOS Menu Bar App..."
echo "   Running with caffeinate to prevent system sleep (background operation allowed)"
caffeinate -i -s python3 menubar.py
