#!/usr/bin/env bash
# Launch TFC menubar + live bot log in this terminal
set -euo pipefail
cd "$(dirname "$0")"

unset MallocStackLogging MallocStackLoggingNoCompact MALLOC_STACK_LOGGING 2>/dev/null || true
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/Library/Caches/ms-playwright}"
case "$PLAYWRIGHT_BROWSERS_PATH" in
  *cursor-sandbox-cache*) export PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright" ;;
esac
export HEADED="${HEADED:-0}"

touch automation.log

echo "🎓 TFC Menu Bar starting..."
echo "   Live bot output below (also saved to automation.log)"
echo "   Close menubar (menu) → this terminal returns to the shell"
echo "   Ctrl+C → stop log view only (menubar + bot keep running)"
echo "   Stop bot: menubar ⏹ or Quit All in menu"
echo "   Foreground bot (no menubar): ./run_cli.sh"
echo ""

python3 menubar.py &
MENUBAR_PID=$!
TAIL_PID=""

cleanup_log() {
  if [[ -n "$TAIL_PID" ]]; then
    kill "$TAIL_PID" 2>/dev/null || true
    wait "$TAIL_PID" 2>/dev/null || true
  fi
}

trap 'cleanup_log; echo ""; echo "👋 Log view closed. Menubar (pid '"$MENUBAR_PID"') + bot still running."; exit 0' INT TERM

sleep 0.5
tail -F automation.log &
TAIL_PID=$!

wait "$MENUBAR_PID" 2>/dev/null || true
cleanup_log
echo ""
echo "👋 Menubar closed — terminal ready. Bot may still be running in background."
