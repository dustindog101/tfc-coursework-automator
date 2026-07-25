# Developer Notes

Agent playbook (preferred for Cursor agents):
[`.cursor/skills/tfc-coursework-bot/SKILL.md`](.cursor/skills/tfc-coursework-bot/SKILL.md)
State / mid-work cases:
[`.cursor/skills/tfc-coursework-bot/state-cases.md`](.cursor/skills/tfc-coursework-bot/state-cases.md)

## Flow

1. `fetch_coursework_catalog` — scrape `/coursework` for Done/Continue/Start
2. `build_work_queue` — CTA first, then Continue, then Start
3. `get_daily_status` — read hours remaining today from site (fallback: events.jsonl)
4. `check_daily_limit` — detect platform 8.0h daily limit ("daily limit reached" or 0.0h remaining)
5. `wait_for_daily_reset` — when daily limit is reached, notifies user, prints live countdown to midnight, and automatically resumes coursework once reset
6. `inspect_lesson` — determine needs_read / needs_reflect / complete (handles mid-work)
7. `reading_phase` + `reflect_phase` — timers, scroll keepalive, agy reflection
8. Re-scrape catalog, loop until queue empty or all hours completed

## Resilience

- `safe_goto` — retries navigation, re-login on session expiry
- Daily limit auto-wait — stays active, periodically re-verifies limit status on site, and resumes after midnight reset
- Lesson errors — skip and continue; 3 consecutive errors → exit(1) for bash restart
- Empty catalog — retry 5× then exit(1) for restart
- `run.sh` — bash loop restarts on non-zero exit; exit 0 on completion = clean stop

## Credentials

`TFC_EMAIL` / `TFC_PASSWORD` from `.env` (gitignored). Never hardcode in source.
