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

### Live lesson message (one per article)

The bot sends **one message per lesson** and **edits it in place** as work progresses:

| Step | Message update |
| :--- | :--- |
| Lesson starts | New "Current Lesson" card |
| Reading | Phase 📖 Reading + today's bar (`6.1 / 8 h · 1.9 h left`) |
| Reflection drafted | Appends the AI reflection text |
| Reflect phase | Phase ✍️ Reflection + timer |
| Submitted | "Reflection submitted to site ✓" |
| Lesson complete | Final summary, then clears for next article |

### Standalone alerts

- Bot **started** or **stopped**
- **Daily limit reached** (8.0 / 8 h — only when actually waiting for midnight)
- **Lesson errors** (bot keeps retrying)

Toggle push without removing credentials:

**Menubar → Settings → Telegram Notifications: ON/OFF**

The menubar toggle reflects real state (reads `.env` token + linked chat).

## Commands

| Command | What it does |
| :--- | :--- |
| `/start` | Register this chat for notifications |
| `/status` | Live phase (Reading / Reflection / Limit wait), today's hours, timer, draft |
| `/stats` | Overall hours, remaining, ETA, today's session, bot completions |
| `/help` | Full command reference |

`/status` and `/stats` re-read `events.jsonl` on every request. Phase and daily hours always come from the **same newest event** — you will never see mismatched hours vs phase (e.g. `6.1 h` while limit-waiting). During limit wait, stale lesson/article lines are hidden and the reset timer counts down live to midnight.

## Example messages

**Live lesson** (edits in place)

```
📚 Current Lesson

Phase   ✍️ Reflection
Lesson   Cognitive Restructuring Techniques
Today   6.1 / 8 h  ██████░░  76%  (1.9 h left)
Timer   45:12
Overall   40.9 / 75 h (55%)

Reflection draft (agy)
I learned that avoiding triggers makes cravings worse...
```

**Daily limit** (`/status` while waiting)

```
📍 Live Status

Engine   🟢 Running
Phase   🌙 Daily limit wait
Today   8.0 / 8 h  ████████  100%  — limit reached
Reset in   12h 14m
Overall   43.9 / 75 h
```

## Architecture

- Notifications use a **background queue** — never blocks Playwright.
- Lesson updates use `editMessageText` when possible (falls back to new message).
- API errors are **rate-limited** to `automation.log`; the bot never stops.
- Only the chat ID saved via `/start` can run commands.
- Stdlib only (`urllib`) — no extra dependencies.

## Files

| File | Purpose |
| :--- | :--- |
| `telegram_notify.py` | Module (queue, API, commands, live lesson edits) |
| `telegram_config.json` | Your linked chat ID (gitignored) |
| `telegram_settings.json` | Runtime ON/OFF from menubar (gitignored) |
| `telegram_lesson_msg.json` | Active lesson message ID for edits (gitignored) |
| `.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ENABLED` |

## Troubleshooting

| Issue | Fix |
| :--- | :--- |
| No push after setup | Send `/start` to link chat ID |
| Menubar shows Telegram OFF but bot works | Restart menubar (loads `.env` on launch) |
| `/status` wrong phase or hours | Restart bot; status uses newest event anchor |
| Commands work, no push | Check menubar Telegram toggle is ON |
| `Telegram API failed` in log | Verify token; regenerate in BotFather if exposed |
