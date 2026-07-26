# TFC Bot — Mid-work & Multi-case Detection

This is the exact behavior of `inspect_lesson` + `process_lesson` in `run_courses.py`.
Agents: read this when debugging wrong article selection, skipped lessons, or resume-after-crash.

## Catalog statuses (from `/coursework`)

| UI label | Internal `status` | Meaning |
|----------|-------------------|---------|
| Done | `done` | Fully complete — never work |
| Continue | `continue` | In progress (read and/or reflect unfinished) |
| Start | `start` | Not started yet |

Queue build order (`build_work_queue`):

1. Big **"Continue Coursework"** CTA URL (if present)
2. All `continue` rows with URLs
3. All `start` rows with URLs
4. Skip anything already in `processed_urls` this session
5. Skip rows with no URL

## `inspect_lesson` return values

| Return | Meaning | Next action |
|--------|---------|-------------|
| `complete` | Nothing to do | Skip; mark processed |
| `needs_read` | Reading timer still active / unread | `reading_phase` → then `reflect_phase` |
| `needs_reflect` | Reading done; reflect form open | Skip reading; `reflect_phase` only |

## Decision tree (in order)

```
IF catalog status == done
  → complete

IF no URL
  → complete (skip)

IF status == start
  open article URL
  IF timer > 0 OR ("Time Remaining" on page and not reflection page)
    → needs_read

open /reflect
  IF page says Reflection Submitted / Next Article / Great work
    → complete
  IF visible textarea (reflect form)
    → needs_reflect

IF status == continue
  open article URL
  IF timer > 0
    → needs_read

open article URL
  IF timer > 0
    → needs_read

open /reflect again
  IF reflect form
    → needs_reflect
  IF already submitted keywords
    → complete

ELSE
  → complete (unknown; skip with warning)
```

## Concrete mid-work cases

| Real-world situation | Catalog | What inspect finds | Behavior |
|----------------------|---------|--------------------|----------|
| Brand-new article | Start | Reading timer ~30 min | Full read → agy → reflect → submit |
| Crash mid-read (timer still going) | Continue or Start | `needs_read`, remaining timer | Resume waiting that timer (not restart from 30:00 unless site resets) |
| Read finished, never reflected | Continue | Reflect form → `needs_reflect` | Skip read; generate/fill reflection; wait reflect timer; submit |
| Reflect timer running, crash | Continue | Reflect form → `needs_reflect` | Re-enter reflect; fill text; wait remaining timer; submit |
| Already submitted reflection | Done (or Continue briefly stale) | Submitted keywords → `complete` | Skip |
| Catalog says Continue but reflect already done | Continue | Submitted keywords → `complete` | Skip |
| Catalog says Start but somehow reflect exists | Start | Prefer read check first; else reflect | Safe path |
| CTA points at in-progress lesson | Continue | URL from CTA mapped | That lesson is first in queue |
| Lesson row has no link | Start/Continue | No URL → `complete` | Skipped (logged) |
| Session expired mid-nav | any | `safe_goto` re-logins | Retry navigation |
| Reflect page already shows success before submit | any | `reflect_phase` early return True | Treat as success |
| Submit button stays disabled | any | Poll up to ~20 attempts | Then check success keywords |

## `process_lesson` branches

```
state = inspect_lesson(...)

if complete:
  return success, 0 hours

if needs_read:
  reading_phase:   # opens article, starts agy in background, waits timer
  then reflect_phase with pre-generated reflection

if needs_reflect:
  try extract article text from lesson URL for better agy context
  reflect_phase with empty pre_reflection → agy called inside if needed
```

## Timers & keepalive

- Poll every ~25s (`POLL_INTERVAL_S`)
- Scroll keepalive every ~165s during **READ and REFLECT**
- Safety cap ~95 minutes per phase, then move on
- After read timer hits 0, site often starts ~60 min reflect timer — status may still show `READ` until phase switches; events may show large `timer_secs` while phase string lags. Trust log lines `[REFLECT]` / `Reflect:` when present.

## Daily limit cases

| Condition | Result |
|-----------|--------|
| `hours_today >= 8` or remaining ≤ 0 | Stop cleanly (`exit 0` via main return) |
| Remaining today < `TFC_MIN_HOURS_LEFT` (default 0.35h) | Stop — not enough for another lesson |
| Checked before each lesson + at startup | Prevents starting a lesson that can't finish |

Source of truth: site "hours remaining today" text; fallback: sum of today's `lesson_complete` in `events.jsonl`.

## Error / restart cases

| Case | Bot behavior |
|------|----------------|
| Navigation fail | `safe_goto` retries 3× with re-login |
| One lesson exception | Skip/continue; count consecutive errors |
| 3 consecutive lesson errors | `sys.exit(1)` → `run.sh` restarts in 5s |
| Empty catalog 5× | `sys.exit(1)` → restart |
| Auth fail at start | Exit 1 |
| Daily limit / empty queue | Exit 0 → `run.sh` prints Finished cleanly and stops |

## What agents must NOT assume

- Do not assume every incomplete lesson needs a full fresh 30 min read.
- Do not hardcode article order; catalog + CTA win.
- Do not treat a restart as "start from lesson 1".
- Do not clear `events.jsonl` just to "fix" hours — site hours are preferred.
- Do not disable scroll keepalive; sessions drop without it.
