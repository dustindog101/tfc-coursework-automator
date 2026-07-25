# Developer Notes

## Flow

1. `fetch_coursework_catalog` — scrape `/coursework` for Done/Continue/Start
2. `build_work_queue` — CTA first, then Continue, then Start
3. `get_daily_status` — read hours remaining today from site (fallback: events.jsonl)
4. `check_daily_limit` — stop if ≥8h or not enough time left for another lesson
5. `inspect_lesson` — determine needs_read / needs_reflect / complete
6. `reading_phase` + `reflect_phase` — timers, scroll keepalive, agy reflection
7. Re-scrape catalog, loop until daily limit or queue empty

## Resilience

- `safe_goto` — retries navigation, re-login on session expiry
- Lesson errors — skip and continue; 3 consecutive errors → exit(1) for bash restart
- Empty catalog — retry 5× then exit(1) for restart
- `run.sh` — bash loop restarts on non-zero exit; exit 0 on daily limit = clean stop

## Credentials

`TFC_EMAIL` / `TFC_PASSWORD` from `.env` (gitignored). Never hardcode in source.
