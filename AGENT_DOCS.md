# Developer Notes

Agent playbook (preferred for Cursor agents):
[`.cursor/skills/tfc-coursework-bot/SKILL.md`](.cursor/skills/tfc-coursework-bot/SKILL.md)
State / mid-work cases:
[`.cursor/skills/tfc-coursework-bot/state-cases.md`](.cursor/skills/tfc-coursework-bot/state-cases.md)

## Flow

1. `fetch_coursework_catalog` — scrape `/coursework` for Done/Continue/Start (full list log **startup only**)
2. `build_work_queue` — CTA first, then Continue, then Start
3. `get_daily_status` — read hours remaining today from site (fallback: events.jsonl)
4. `check_daily_limit` — detect platform 8.0h daily limit
5. `wait_for_daily_reset` — notify, countdown to midnight, auto-resume
6. `inspect_lesson` — needs_read / needs_reflect / complete (mid-work resume)
7. `reading_phase` + `reflect_phase` — timers, scroll keepalive, LLM reflection
8. Re-scrape catalog, loop until queue empty or daily limit

## LLM chain

1. **OpenCode** — `OPENCODE_MODEL` comma list, `OPENCODE_VARIANT=minimal`
2. **agy** — Gemini 3.6 Flash (Low) if OpenCode fails
3. **Hardcoded fallback** — last resort; upgraded before submit when possible

Prompt: `build_reflection_system_prompt()` — model must return **only** the reflection paragraph (no rules/meta echoed back). Invalid meta output is rejected and falls through the chain.

## Reflection drafts

- File: `reflection_drafts.json` (gitignored), keyed by lesson URL
- Saved as soon as LLM finishes (even during reading timer)
- Loaded on restart; cleared only on **confirmed** submit
- Events: `reflection_generated` with `draft_origin`: `generated` | `loaded`
- CLI logs: `✍️ Generated reflection …` / `✍️ Loaded saved reflection from disk …`

## Telegram

- One live lesson message (`telegram_lesson_msg.json`) edited in place
- `timer_sync` refreshes card every `TELEGRAM_LESSON_EDIT_INTERVAL_S` (default 60)
- `/status` reads `events.jsonl` on demand

## Menubar state

- `🟢 Active` — reading/reflecting or between lessons while working
- `🌙 Limit Wait` — daily cap hit; process may still run (`LIMIT_WAIT` loop)
- `⏸️ Paused` — bot process stopped

**Do not** treat `bot_active` as "Active coursework" — use `_resolve_limit_wait_state()`.

## Resilience

- `safe_goto` — retries navigation, re-login on session expiry
- Daily limit auto-wait — stays alive, resumes after midnight
- Lesson errors — skip and continue; 3 consecutive errors → exit(1)
- `run.sh` — bash loop restarts on non-zero exit

## Credentials

`TFC_EMAIL` / `TFC_PASSWORD` from `.env` (gitignored). Never hardcode in source.
