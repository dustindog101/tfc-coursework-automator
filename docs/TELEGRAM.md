# Telegram Notifications

Optional mobile alerts and live status for the TFC coursework automator.

## Quick setup

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Add to `.env`:

```env
TELEGRAM_BOT_TOKEN=your-token-here
TELEGRAM_ENABLED=1
```

3. Start the bot: `./run_menubar.sh` or `./run_cli.sh`
4. In Telegram, open your bot and send **`/start`**

You should receive a welcome message with the command list.

## Push notifications

Formatted HTML messages are sent when:

- The bot **starts** or **stops**
- A **new article** begins
- A **lesson completes** (hours gained + progress)
- The **daily 8h limit** is reached
- A **lesson error** occurs (bot keeps retrying)

Toggle push without removing credentials:

**Menubar → Settings → Telegram Notifications: ON/OFF**

## Commands

| Command | What it does |
| :--- | :--- |
| `/start` | Register this chat for notifications |
| `/status` | Live engine state, article, phase, timer, progress bar |
| `/stats` | Overall hours, remaining, ETA, today’s session, bot completions |
| `/help` | Full command reference |

`/status` and `/stats` re-read `events.jsonl` on every request — always live.

## Example messages

**Lesson complete**

```
✅ Lesson Complete

Article   Harm Reduction Strategies
This lesson   +1.20 h
Today   3.0 h
Overall   35.8 / 75 h (48%)

Commands: /status · /stats · /help
```

**Live status** (`/status`)

```
📍 Live Status

Engine   🟢 Running
Phase   Reading
Article   Introduction to CBT
Timer   29:45
Progress   35.8 / 75 h
█████░░░░░  48%

Commands: /status · /stats · /help
```

## Architecture

- Notifications use a **background queue** — never blocks Playwright.
- API errors are **rate-limited** to `automation.log`; the bot never stops.
- Only the chat ID saved via `/start` can run commands.
- Stdlib only (`urllib`) — no extra dependencies.

## Files

| File | Purpose |
| :--- | :--- |
| `telegram_notify.py` | Module (queue, API, commands) |
| `telegram_config.json` | Your linked chat ID (gitignored) |
| `telegram_settings.json` | Runtime ON/OFF from menubar |
| `.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ENABLED` |

## Troubleshooting

| Issue | Fix |
| :--- | :--- |
| No push after setup | Send `/start` to link chat ID |
| Commands work, no push | Check menubar Telegram toggle is ON |
| `Telegram API failed` in log | Verify token; regenerate in BotFather if exposed |
| Bot runs, Telegram silent | `TELEGRAM_ENABLED=1` in `.env` and restart bot |
