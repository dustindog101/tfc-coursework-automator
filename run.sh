#!/usr/bin/env bash
# TFC Coursework Bot — one command to run everything (headed + auto-restart)
set -euo pipefail
cd "$(dirname "$0")"

# macOS Playwright browser cache (skip if already set)
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/Library/Caches/ms-playwright}"
export HEADED=1

if [[ ! -f .env ]]; then
  echo "❌ Missing .env file"
  echo "   Run:  cp .env.example .env"
  echo "   Then add your TFC email and password to .env"
  exit 1
fi

echo "🤖 TFC Bot starting (visible browser, auto-restart on crash)"
echo "   Press Ctrl+C to stop"
echo ""

while true; do
  python3 run_courses.py
  code=$?
  if [[ $code -eq 0 ]]; then
    echo ""
    echo "✅ Finished cleanly (daily limit hit or all done)."
    break
  fi
  echo ""
  echo "⚠️  Crashed (exit $code) — restarting in 5 seconds..."
  sleep 5
done
