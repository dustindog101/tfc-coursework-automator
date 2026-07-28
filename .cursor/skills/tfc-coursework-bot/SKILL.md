---
name: tfc-coursework-bot
description: >-
  Operate, monitor, restart, and debug the TFC coursework automator
  (run_courses.py / run.sh). Use when the user mentions TFC, Foundation of
  Change, community service coursework bot, reading/reflect timers, daily
  hour limit, automation.log, events.jsonl, Telegram notifications, or asks to start/stop/check
  the bot. Also use when resuming mid-lesson or explaining Done/Continue/Start states.
---

# TFC Coursework Bot — Agent Playbook

You are operating a **finished** Playwright bot. Do **not** rewrite it unless the user asks for a code change. Prefer running and monitoring.

## Non-negotiables

1. **Never** commit, print, or paste `.env` credentials.
2. **Never** invent login emails/passwords. If `.env` is missing, tell the user to copy `.env.example` → `.env` and fill it.
3. Prefer `./run.sh` over raw `python3 run_courses.py` (sets Playwright path + auto-restart).
4. One bot process only. Before starting: kill any existing `run_courses.py`.
5. Daily stop at ~8h is **success**, not a crash (`exit 0` → "Finished cleanly").

## First-time setup (only if needed)

```bash
cd /path/to/this/repo
./setup.sh
# then edit .env with real TFC_EMAIL / TFC_PASSWORD
./run.sh
```

## Start / stop / monitor (default workflow)

```bash
# Stop any old bot
pkill -f "run_courses.py" 2>/dev/null || true

# Start (headed browser + crash restart loop)
./run.sh

# Live text log
tail -f automation.log

# Structured events
tail -f events.jsonl
jq 'select(.event=="lesson_complete")' events.jsonl
```

Stop for the user: `Ctrl+C` in the bot terminal, or `pkill -f "run_courses.py"`.

Headless: `HEADED=0 ./run.sh`

## What the bot already does (do not reimplement)

```
Login → scrape /coursework catalog
     → pick next incomplete lesson (CTA → Continue → Start)
     → inspect mid-work state (read vs reflect vs done)
     → wait reading timer (scroll keepalive)
     → AI reflection via agy (fallback text if agy missing)
     → wait reflect timer → submit
     → re-scrape catalog → repeat
     → stop at daily limit or empty queue
```

**Mid-work resume is built in.** If the bot restarts while a timer is running, the next `inspect_lesson` call re-detects the real page state and continues from read or reflect. You do **not** need to remember which article was open.

Full decision tree: [state-cases.md](state-cases.md)

## Status line cheat sheet

Example:

```text
TFC done:26+2 · #29/620 READ 12:40 · Harm Reduction Strategies · today 1.4/8h · all 13.7/75h · left 6.6h
```

| Token | Meaning |
|-------|---------|
| `done:26+2` | Catalog Done count + lessons finished this session |
| `#29/620` | Rough position / remaining queue size |
| `READ` / `REFLECT` / `SUBMIT_WAIT` | Current phase |
| `12:40` | Timer remaining (mm:ss) |
| `today 1.4/8h` | Hours used today vs daily cap |
| `left 6.6h` | Hours remaining today |

Also written to the terminal **window title**.

## Healthy vs broken

| Signal | Meaning | Action |
|--------|---------|--------|
| `Finished cleanly` | Daily limit or queue empty | Done for today |
| `Crashed, restarting` | Non-zero exit; `run.sh` looping | Watch; if loops forever, read `automation.log` |
| `Session expired` / re-login | Normal recovery | None |
| `Empty catalog — retry` | Transient scrape fail | Wait; exits after 5 for restart |
| `3 lesson errors in a row` | Hard fail → restart | Check log; fix site/UI change if persistent |
| `Submission unconfirmed` | Reflect may have failed | Next catalog pass will re-inspect |
| Timer jumps 0 → ~60min on same article | Read finished; reflect timer started (status may still say `READ` briefly) | Normal |

## Common user asks → exact agent actions

### "Is it running / how far?"

1. Check process: `pgrep -fl run_courses.py`
2. Read last lines of `automation.log` and/or terminal status line
3. Report: article title, phase, timer left, today hours, session done count

### "Start it" / "keep going overnight"

1. Confirm `.env` exists
2. Kill old process
3. Start `./run.sh` in background/long terminal
4. Confirm first `Catalog:` / `Next:` log lines appear

### "It crashed / stuck"

1. `tail -80 automation.log`
2. If Playwright missing: re-run `./setup.sh`; ensure `PLAYWRIGHT_BROWSERS_PATH` (set by `run.sh`)
3. Restart with `./run.sh` — catalog + `inspect_lesson` resume mid-work
4. Do **not** hardcode lesson URLs unless debugging a specific page

### "Did reflection submit?"

```bash
grep -E "Submitted|Submission unconfirmed|reflect_submitted" automation.log | tail -20
jq 'select(.event=="reflect_submitted" or .event=="lesson_complete")' events.jsonl | tail -10
```

### "Change daily limit"

Edit `.env` (or env):

```env
TFC_DAILY_HOUR_LIMIT=8.0
TFC_MIN_HOURS_LEFT=0.35
```

Restart the bot.

## Telegram (optional)

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ENABLED=1
```

1. Start bot → message `/start` to link chat
2. `/status` — live phase + daily bar (`X / 8 h`, hours left or limit reached) | `/stats` — hours & ETA
3. Toggle push: menubar → Settings → Telegram Notifications

Full guide: [docs/TELEGRAM.md](../../docs/TELEGRAM.md). Failures never stop the bot.

## Files that matter

| Path | Role |
|------|------|
| `run_courses.py` | Bot logic |
| `run.sh` | Headed + auto-restart wrapper |
| `setup.sh` | Deps + Playwright + `.env` scaffold |
| `.env` | Secrets (gitignored) |
| `.env.example` | Template only |
| `automation.log` | Human log (gitignored) |
| `events.jsonl` | Machine events (gitignored) |
| `telegram_notify.py` | Optional Telegram push + commands |
| `docs/TELEGRAM.md` | Telegram setup guide |
| `AGENT_DOCS.md` | Short developer flow notes |
| `README.md` | Human quick start |

## Code-change rules (only if user asks)

- Keep credentials out of source; load from `.env`.
- Preserve `inspect_lesson` mid-work detection — do not simplify to "always start from reading".
- Preserve catalog priority: CTA continue URL → Continue rows → Start rows; skip Done.
- Preserve scroll keepalive during **both** READ and REFLECT timers.
- Preserve daily limit check **before** starting each lesson.
- Never add exploit/bypass of site security beyond normal form automation already present.

## Dumb-agent checklist (copy and tick)

```
- [ ] .env exists with TFC_EMAIL / TFC_PASSWORD
- [ ] No second bot process running
- [ ] Started via ./run.sh
- [ ] Saw Catalog: … and Next: … in logs
- [ ] Status line updating (timer counting down)
- [ ] If restarted mid-lesson: confirmed needs_read or needs_reflect in log (not forced from scratch blindly)
- [ ] Did not commit .env / logs / screenshots with PII
```
