#!/usr/bin/env bash
# Run TFC bot in foreground — full startup banners + live terminal output
set -euo pipefail
cd "$(dirname "$0")"

unset MallocStackLogging MallocStackLoggingNoCompact MALLOC_STACK_LOGGING 2>/dev/null || true
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/Library/Caches/ms-playwright}"
export HEADED="${HEADED:-0}"

if [[ ! -f .env ]]; then
  echo "❌ Missing .env — cp .env.example .env and add credentials"
  exit 1
fi

if pgrep -f "run_courses.py" >/dev/null 2>&1; then
  echo "ℹ️  Stopping background bot so this terminal can own it..."
  pkill -f "run_courses.py" 2>/dev/null || true
  rm -f bot.pid
  sleep 1
fi

echo "🤖 TFC Bot (foreground) — output also appended to automation.log"
echo "   Telegram: message /start to your bot after setting TELEGRAM_BOT_TOKEN in .env"
echo "   Press Ctrl+C to stop"
echo ""

python3 -u run_courses.py 2>&1 | tee -a automation.log
