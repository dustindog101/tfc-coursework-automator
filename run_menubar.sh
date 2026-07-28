#!/usr/bin/env bash
# Launch TFC menubar + live bot log in this terminal
set -euo pipefail
cd "$(dirname "$0")"

unset MallocStackLogging MallocStackLoggingNoCompact MALLOC_STACK_LOGGING 2>/dev/null || true

touch automation.log

echo "🎓 TFC Menu Bar starting..."
echo "   Live bot output below (also saved to automation.log)"
echo "   Ctrl+C = close this log view only (menubar + bot keep running)"
echo "   Stop bot: menubar ⏹ or ./run_cli.sh after pkill, or Quit All in menu"
echo "   Foreground bot (no menubar): ./run_cli.sh"
echo "   Telegram: set TELEGRAM_BOT_TOKEN in .env, then message /start to your bot"
echo ""

python3 menubar.py &
MENUBAR_PID=$!

trap 'echo ""; echo "👋 Log view closed. Menubar (pid '"$MENUBAR_PID"') + bot still running."; exit 0' INT TERM

sleep 0.5
tail -F automation.log
