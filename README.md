# TFC Coursework Automator

Automates community service coursework on [The Foundation of Change](https://www.thefoundationofchange.org). Discovers incomplete lessons, waits out timers, writes AI reflections, and stops at your daily hour limit.

---

## Quick start (3 commands)

```bash
git clone https://github.com/dustindog101/tfc-coursework-automator.git
cd tfc-coursework-automator
./setup.sh
```

Edit `.env` with your email and password, then:

```bash
./run.sh
```

That's it. A Chrome window opens and the bot runs until today's hours are done.

---

## How to run in Terminal

Open **Terminal** and paste these one at a time:

```bash
cd ~/community-service
./run.sh
```

| What you want | Command |
|---------------|---------|
| **Normal run** (browser visible) | `./run.sh` |
| **First-time setup** | `./setup.sh` |
| **Stop the bot** | `Ctrl + C` |
| **Watch live logs** | `tail -f automation.log` |

### Headless (no browser window)

```bash
HEADED=0 ./run.sh
```

---

## What it does

```
Login → scrape /coursework → skip Done articles
      → read article (wait timer) → write reflection (wait timer) → submit
      → repeat until daily 8h limit → stop cleanly
```

| Feature | Detail |
|---------|--------|
| **Smart catalog** | Reads Done / Continue / Start from the coursework page |
| **Daily limit** | Checks the site's "hours remaining today" before each lesson |
| **Anti-logout** | Scrolls every ~3 min during reading *and* reflect timers |
| **AI reflections** | Uses `agy` CLI (falls back to canned text if unavailable) |
| **Resilience** | Auto-restarts on crash; re-logins on session expiry; re-scrapes catalog after each lesson |
| **Live status** | Terminal line + window title show article name, timer, progress |

---

## Setup details

### Requirements

- Python 3.10+
- Chromium via Playwright
- `agy` CLI (optional, for AI reflections)

### Credentials

```bash
cp .env.example .env
```

```env
TFC_EMAIL=your-email@example.com
TFC_PASSWORD=your-password
```

Never commit `.env` — it's gitignored.

### macOS Playwright note

If you see "Executable doesn't exist", set:

```bash
export PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright"
```

`run.sh` sets this automatically.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TFC_EMAIL` | — | Login email (required) |
| `TFC_PASSWORD` | — | Login password (required) |
| `TFC_DAILY_HOUR_LIMIT` | `8.0` | Max hours per day |
| `TFC_MIN_HOURS_LEFT` | `0.35` | Won't start a lesson if less time left |
| `HEADED` | `1` in run.sh | `1` = visible browser, `0` = headless |

---

## Logs

```bash
tail -f automation.log                              # text log
jq 'select(.event=="lesson_complete")' events.jsonl   # completed lessons
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Missing .env` | `cp .env.example .env` and add credentials |
| Playwright browser missing | Run `./setup.sh` again |
| Bot keeps restarting | Check `automation.log` for errors |
| Wrong article / stuck | Stop with Ctrl+C, run `./run.sh` again — it re-reads the catalog |

---

## For AI agents

Hand a Cursor/agent this skill and say: **use the tfc-coursework-bot skill**.

| File | Purpose |
|------|---------|
| [`.cursor/skills/tfc-coursework-bot/SKILL.md`](.cursor/skills/tfc-coursework-bot/SKILL.md) | How to set up, run, monitor, restart, and what not to do |
| [`.cursor/skills/tfc-coursework-bot/state-cases.md`](.cursor/skills/tfc-coursework-bot/state-cases.md) | Mid-work detection: Done / Continue / Start, read vs reflect, crash resume |

The bot **already** detects mid-lesson state (reading timer still going, needs reflect only, already submitted, etc.). Agents should run `./run.sh` and monitor — not reinvent the flow.

---

## Disclaimer

Educational tool. Use responsibly and in accordance with the platform's terms of service.
