# Agent Documentation & Developer Notes

For AI agents or developers maintaining `run_courses.py`.

## Architecture & Flow

1. **Catalog (`fetch_coursework_catalog`)** — scrapes `/coursework` for lesson rows (Done / Continue / Start), maps titles to URLs, picks up the "Continue Coursework" CTA.
2. **Queue (`build_work_queue`)** — CTA first, then Continue, then Start; skips Done.
3. **State check (`inspect_lesson`)** — per article: skip if Done; for Start check reading timer first; then check reflect form vs already submitted.
4. **Reading (`reading_phase`)** — extracts article, runs `agy` in background, waits reading timer via `wait_for_timer`.
5. **Reflect (`reflect_phase`)** — fills textarea, 4 stars, waits reflect timer via `wait_for_timer`, submits.
6. **Main loop** — processes one lesson, re-scrapes catalog, repeats until daily limit or queue empty.

## Key Details

- **Credentials:** `TFC_EMAIL` / `TFC_PASSWORD` from `.env` or environment (never committed).
- **Headed mode:** `HEADED=1` env var.
- **Auto-recovery:** bash `while` loop restarts on crash; script re-reads catalog from DOM on each run.
- **Keep-alive:** `wait_for_timer` scrolls every `SCROLL_INTERVAL_S` (165s) on **both** reading and reflect pages.
- **Reflect timer:** submit button stays disabled until timer expires — `wait_for_timer` handles this.
- **AI:** `agy -p --model "Gemini 3.6 Flash (Low)"`; hardcoded fallbacks if agy fails.
- **Status:** `live_status()` writes to stderr and sets terminal title via OSC sequence.

## Files

- `run_courses.py` — main bot
- `events.jsonl` — JSONL event log (gitignored, local only)
- `automation.log` — text log (gitignored)
- `.env` — local credentials (gitignored)
