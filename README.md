# Foundation of Change Coursework Automator

Playwright bot that completes coursework on [The Foundation of Change](https://www.thefoundationofchange.org) platform. It discovers incomplete lessons from your coursework page, waits out reading/reflection timers, generates reflections via the `agy` CLI, and tracks daily hour limits.

## Features

- **Catalog discovery** — scrapes `/coursework` for Done / Continue / Start status and skips completed articles
- **State verification** — checks whether each lesson needs reading, reflection, or is already finished
- **Dual timers** — handles both reading-page and reflection-page countdown timers
- **Anti-logout scroll** — scrolls the page every ~3 minutes during any timer wait (reading *and* reflect)
- **AI reflections** — uses `agy -p` to generate short, natural-sounding reflections while timers run
- **Daily limit** — stops at 8 hours/day (tracked via local `events.jsonl`)
- **Live status** — updates terminal line and macOS window title with article name, progress, and timer
- **Auto-recovery** — designed to run in a bash restart loop for network blips

## Prerequisites

- Python 3.10+
- [Playwright](https://playwright.dev/python/) Chromium browser
- [`agy` CLI](https://github.com/) (optional — falls back to canned reflections if unavailable)

```bash
pip install -r requirements.txt
playwright install chromium
```

## Setup

1. Clone this repo
2. Copy `.env.example` → `.env`
3. Add your Foundation of Change login credentials

```bash
cp .env.example .env
# edit .env with your email and password
```

## Usage

**Headed mode** (visible browser) with auto-restart loop:

```bash
export PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright"  # macOS
while true; do
  HEADED=1 python3 run_courses.py
  if [ $? -eq 0 ]; then echo "Finished cleanly."; break; fi
  echo "Crashed, restarting in 5s..."
  sleep 5
done
```

**Headless mode** — omit `HEADED=1`.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TFC_EMAIL` | Yes | Login email |
| `TFC_PASSWORD` | Yes | Login password |
| `TFC_BASE_URL` | No | Platform URL (default: foundationofchange.org) |
| `TFC_LOG_FILE` | No | Text log path (default: `./automation.log`) |
| `TFC_EVENTS_FILE` | No | JSONL events path (default: `./events.jsonl`) |
| `HEADED` | No | Set to `1` for visible browser |

## Logs

```bash
tail -f automation.log
jq 'select(.event=="lesson_complete")' events.jsonl
```

## Disclaimer

This tool is for educational purposes. Use responsibly and in accordance with the platform's terms of service.
