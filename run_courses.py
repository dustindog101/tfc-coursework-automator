#!/usr/bin/env python3
"""
TFC Community Service Bot — v4
==============================
Changes from v3:
  - Discovers lessons from /coursework (Done / Continue / Start)
  - Skips completed articles; verifies reading vs reflect state
  - Auto-recovers navigation (re-login + re-scrape catalog)
  - Live status on stderr + macOS terminal window title
  - Uses `agy -p` for AI reflections, 8h/day limit, JSONL event log

Usage:
    python3 run_courses.py

Check logs:
    tail -f ~/community-service/automation.log
    grep "REFLECTION" ~/community-service/events.jsonl | python3 -m json.tool
    jq 'select(.event=="lesson_complete")' ~/community-service/events.jsonl
"""

import asyncio
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Load optional .env from project root (local only, never commit)."""
    env_path = os.path.join(ROOT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# macOS: avoid harmless "MallocStackLogging: can't turn off..." spam from child processes
_MALLOC_ENV_KEYS = (
    "MallocStackLogging", "MallocStackLoggingNoCompact", "MallocScribble",
    "MallocGuardEdges", "MALLOC_STACK_LOGGING",
)
for _mk in _MALLOC_ENV_KEYS:
    os.environ.pop(_mk, None)


def subprocess_env(extra: Optional[dict] = None) -> dict:
    """Clean env for child processes (Playwright/agy/caffeinate on macOS)."""
    env = os.environ.copy()
    for key in _MALLOC_ENV_KEYS:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env

EMAIL = os.getenv("TFC_EMAIL", "")
PASSWORD = os.getenv("TFC_PASSWORD", "")
BASE_URL = os.getenv("TFC_BASE_URL", "https://www.thefoundationofchange.org")
LOG_FILE = os.getenv("TFC_LOG_FILE", os.path.join(ROOT_DIR, "automation.log"))
EVENTS_FILE = os.getenv("TFC_EVENTS_FILE", os.path.join(ROOT_DIR, "events.jsonl"))
COMPLETED_COURSES_FILE = os.getenv("TFC_COMPLETED_COURSES_FILE", os.path.join(ROOT_DIR, "completed_courses.json"))

SCROLL_INTERVAL_S = 165   # ~2.75 min (bundled with timer resync)
TIMER_RESYNC_S    = 165   # DOM timer read + scroll keepalive interval
LOCAL_TICK_S      = 60    # local sleep between UI updates (no DOM)
STATUS_UPDATE_S   = 60    # refresh terminal status line at most this often
TIMER_DRIFT_TOLERANCE_S = 15
REFLECTION_MIN    = 80
REFLECTION_MAX    = 295
DAILY_HOUR_LIMIT  = float(os.getenv("TFC_DAILY_HOUR_LIMIT", "8.0"))
MIN_HOURS_LEFT    = float(os.getenv("TFC_MIN_HOURS_LEFT", "0.35"))  # don't start if less left today

# ── Logging (text) ────────────────────────────────────────────────────────────
_status_line_active = False


class TerminalLogHandler(logging.Handler):
    """Terminal: important logs only; always newline before log if status line is active."""
    def emit(self, record):
        global _status_line_active
        try:
            msg = self.format(record)
            if _status_line_active:
                sys.stderr.write("\n")
                _status_line_active = False
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)


class _StatusOnlyFilter(logging.Filter):
    """Drop noisy INFO during normal operation from the terminal handler."""
    _NOISY_PREFIXES = (
        "agy reflection", "opencode reflection", "↕ scroll", "⏱ ",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO:
            return True
        msg = record.getMessage()
        if any(msg.startswith(p) or f" {p}" in msg for p in self._NOISY_PREFIXES):
            return False
        if "[READ] ↕" in msg or "[REFLECT] ↕" in msg:
            return False
        if "min remaining" in msg and record.levelno == logging.INFO:
            return False
        return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("tfc")
if sys.stderr.isatty():
    _term_handler = TerminalLogHandler()
    _term_handler.setLevel(logging.INFO)
    _term_handler.addFilter(_StatusOnlyFilter())
    log.addHandler(_term_handler)

BOT_PID_FILE = os.path.join(ROOT_DIR, "bot.pid")


def write_bot_pid() -> None:
    try:
        with open(BOT_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def remove_bot_pid() -> None:
    try:
        if os.path.exists(BOT_PID_FILE):
            os.remove(BOT_PID_FILE)
    except Exception:
        pass


# ── Structured event log (JSONL) ──────────────────────────────────────────────
def log_event(event: str, **kwargs):
    """
    Append a JSON event to events.jsonl.
    Searchable with:  grep '"event":"X"' events.jsonl
                      jq 'select(.event=="X")' events.jsonl
    """
    record = {
        "ts": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "event": event,
        **kwargs,
    }
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    try:
        import telegram_notify
        telegram_notify.on_event(record)
    except Exception as e:
        log.warning("Telegram notify skipped: %s", e)
    return record


def get_today_hours_from_log() -> float:
    """Read events.jsonl and sum hours completed today."""
    today = date.today().isoformat()
    total = 0.0
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("date") == today and rec.get("event") == "lesson_complete":
                        total += rec.get("hours_gained", 0.0)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return total


def parse_daily_remaining(body: str) -> Optional[float]:
    """Parse '3.9h remaining today (8h max)' or '0.0h daily limit reached' from coursework/dashboard."""
    if re.search(r"daily limit reached", body, re.IGNORECASE):
        return 0.0

    for pat in [
        r"TODAY'S LIMIT\s*\n\s*([\d.]+)\s*h",
        r"([\d.]+)\s*h\s*\n\s*remaining today",
        r"remaining today[^\d]*([\d.]+)\s*h",
        r"([\d.]+)\s*h\s*\n\s*daily limit reached",
        r"([\d.]+)\s*h\s*\n\s*remaining",
    ]:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


async def get_daily_status(page) -> dict:
    """Read today's hours from the site (source of truth) with log fallback."""
    hours_from_log = get_today_hours_from_log()
    remaining_today = None
    site_limit_reached = False
    try:
        if await safe_goto(page, f"{BASE_URL}/coursework"):
            await page.wait_for_timeout(2000)
            body = await page.inner_text("body")
            if re.search(r"daily limit reached", body, re.IGNORECASE):
                site_limit_reached = True
                remaining_today = 0.0
            else:
                remaining_today = parse_daily_remaining(body)
    except Exception as e:
        log.warning(f"Could not read daily limit from site: {e}")

    if remaining_today is not None:
        hours_today = max(0.0, DAILY_HOUR_LIMIT - remaining_today)
        if site_limit_reached or remaining_today <= 0:
            hours_today = DAILY_HOUR_LIMIT
            remaining_today = 0.0
        return {
            "hours_today": hours_today,
            "hours_remaining_today": remaining_today,
            "site_limit_reached": site_limit_reached,
            "source": "site",
        }

    remaining = max(0.0, DAILY_HOUR_LIMIT - hours_from_log)
    limit_reached = (hours_from_log >= DAILY_HOUR_LIMIT or remaining <= 0)
    return {
        "hours_today": hours_from_log,
        "hours_remaining_today": remaining,
        "site_limit_reached": limit_reached,
        "source": "log",
    }


# ── Caffeinate Manager ────────────────────────────────────────────────────────
class CaffeinateManager:
    def __init__(self):
        self.enabled = os.getenv("ENABLE_CAFFEINATE", "1") == "1" and sys.platform == "darwin"
        self.proc = None

    def start(self):
        if self.enabled and (self.proc is None or self.proc.poll() is not None):
            self.proc = subprocess.Popen(["caffeinate", "-i", "-s"], env=subprocess_env())
            log.info("☕ Smart Caffeinate Active (Keeping Mac awake during active coursework)")

    def stop(self):
        if self.enabled and self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None
            log.info("🌙 Daily limit reached — Caffeinate released (Mac allowed to sleep until 12:00 AM)")

CAFFEINATE_MANAGER = CaffeinateManager()


# ── Run state + live status display ───────────────────────────────────────────
@dataclass
class RunState:
    catalog_done: int = 0
    session_done: int = 0
    queue_pos: int = 0
    queue_total: int = 0
    title: str = ""
    phase: str = "INIT"
    timer_secs: int = 0
    hours_done: float = 0.0
    hours_today: float = 0.0
    hours_total: float = 75.0
    hours_remaining: float = 75.0
    user_name: str = ""


RUN_STATE = RunState()


def estimate_days_to_complete(
    hours_remaining: float, hours_today: float = 0.0,
    daily_limit: float = DAILY_HOUR_LIMIT,
) -> int:
    """Estimate calendar days until all hours are done at daily_limit per day."""
    if hours_remaining <= 0:
        return 0
    avail_today = max(0.0, daily_limit - hours_today)
    if hours_remaining <= avail_today:
        return 0
    after_today = hours_remaining - avail_today
    return int(math.ceil(after_today / daily_limit))


def format_timer_display(secs: int) -> str:
    """Human timer string; handles long limit-wait countdowns."""
    if secs <= 0:
        return "00:00"
    if secs >= 3600:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h {m:02d}m"
    return f"{secs // 60}:{secs % 60:02d}"


def format_eta_label(days: int) -> str:
    if days <= 0:
        return "finish today"
    if days == 1:
        return "~1 day left"
    return f"~{days} days left"


def set_terminal_title(text: str):
    """Set macOS Terminal / iTerm window title."""
    safe = text.replace("\x1b", "").replace("\007", "")[:180]
    sys.stderr.write(f"\033]0;{safe}\007")
    sys.stderr.flush()


def make_progress_bar(done: float, total: float, width: int = 10) -> str:
    pct = done / total if total > 0 else 0
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)

def format_status_line(rs: RunState) -> str:
    """Single-line live status — no cryptic counters."""
    timer_str = format_timer_display(rs.timer_secs)
    title = (rs.title[:28] + "…") if len(rs.title) > 29 else (rs.title or "—")
    pct = int((rs.hours_done / rs.hours_total) * 100) if rs.hours_total > 0 else 0
    bar = make_progress_bar(rs.hours_done, rs.hours_total)
    eta = format_eta_label(estimate_days_to_complete(rs.hours_remaining, rs.hours_today))

    if rs.phase == "LIMIT_WAIT":
        return (
            f"🌙 Daily limit │ reset in {timer_str} │ "
            f"{rs.hours_done:.1f}/{rs.hours_total:.0f}h [{bar}] {pct}% │ {eta}"
        )

    phase = {"READ": "📖 Reading", "REFLECT": "✍️ Reflecting", "START": "🚀 Starting"}.get(
        rs.phase, rs.phase
    )
    lesson_no = f" #{rs.queue_pos}" if rs.queue_pos else ""
    return (
        f"{phase}{lesson_no} │ {timer_str} │ {title} │ "
        f"{rs.hours_done:.1f}/{rs.hours_total:.0f}h [{bar}] {pct}% │ "
        f"today {rs.hours_today:.1f}/{DAILY_HOUR_LIMIT:.0f}h │ {eta}"
    )


def live_status(phase: str, timer_secs: int, lesson_title: str,
                hours_done: float, hours_today: float, hours_total: float,
                rs: Optional[RunState] = None, *, force: bool = False):
    """Update in-place terminal status line (throttled unless force=True)."""
    global _status_line_active
    state = rs or RUN_STATE
    now = time.time()
    if not force and phase == state.phase and (now - getattr(state, "_last_status_ts", 0)) < STATUS_UPDATE_S:
        state.timer_secs = timer_secs
        state.title = lesson_title
        return
    state._last_status_ts = now  # type: ignore[attr-defined]
    state.phase = phase
    state.timer_secs = timer_secs
    state.title = lesson_title
    state.hours_done = hours_done
    state.hours_today = hours_today
    state.hours_total = hours_total
    state.hours_remaining = max(0.0, hours_total - hours_done)
    line = format_status_line(state)
    sys.stderr.write("\033[2K\r" + line)
    sys.stderr.flush()
    _status_line_active = True
    clean_line = re.sub(r'\033\[[0-9;]*m', '', line)
    set_terminal_title(clean_line)


def clear_live_status():
    """End in-place status line before multi-line log output."""
    global _status_line_active
    if _status_line_active:
        sys.stderr.write("\n")
        sys.stderr.flush()
        _status_line_active = False


# ── agy reflection via CLI pipe ───────────────────────────────────────────────
_FALLBACKS = [
    "this article was actually pretty interesting. i didnt realize how much addiction affects the brain and body, not just choices people make. made me think about things differently.",
    "the biggest thing i took away is that recovery isnt just about stopping, its about rebuilding habits and getting support. the info on community help made sense to me.",
    "i learned that change takes time and setbacks happen. reading this made me understand why certain treatment approaches work better than just willpower alone.",
    "honestly this gave me a new perspective on how addiction impacts families and communities too. its not just an individual problem and support really does matter.",
]

_AGY_LIMIT_MARKERS = (
    "quota", "rate limit", "rate-limit", "429", "exhausted",
    "too many requests", "resource exhausted", "limit reached",
)

def _strip_em_dash(t: str) -> str:
    return t.replace("\u2014", ",").replace("\u2013", ",").replace("—", ",").replace("–", ",")


OPENCODE_MODEL = os.getenv("OPENCODE_MODEL", "opencode/mimo-v2.5-free")
OPENCODE_USER_MSG = "Write only the reflection text, no preamble."


def _clean_llm_text(text: str) -> str:
    text = text.strip().strip('"\'')
    text = _strip_em_dash(text)
    text = re.sub(r'^```.*?\n', '', text, flags=re.MULTILINE).strip()
    return text


def _extract_prose(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    prose = next(
        (
            l for l in reversed(lines)
            if len(l) >= REFLECTION_MIN
            and not l.startswith("{")
            and not l.startswith("timestamp")
        ),
        text,
    )
    return prose[:REFLECTION_MAX]


def _agy_hit_limit(result: subprocess.CompletedProcess) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in text for marker in _AGY_LIMIT_MARKERS)


def _build_opencode_cmd(system_prompt: str) -> tuple[list[str], Optional[str]]:
    """
    Build opencode argv. Always attach prompt via -f after the user message.
    Prompts often start with '-' (bullet rules) which breaks positional parsing.
    """
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(system_prompt)
    tmp.close()
    return [
        "opencode", "run", "-m", OPENCODE_MODEL, "--auto",
        OPENCODE_USER_MSG, "-f", tmp.name,
    ], tmp.name


def _run_llm_prompt(system_prompt: str) -> Optional[tuple[str, str]]:
    """
    Try agy → opencode (mimo) → return None.
    Returns (reflection text, source) or None on all failures.
    """
    # ── 1. Try agy ────────────────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["agy", "-p", system_prompt, "--model", "Gemini 3.6 Flash (Low)"],
            capture_output=True, text=True, timeout=60, cwd=ROOT_DIR, env=subprocess_env(),
        )
        if result.returncode == 0:
            text = _extract_prose(_clean_llm_text(result.stdout))
            if len(text) >= REFLECTION_MIN:
                log.debug(f"agy reflection ({len(text)} chars): {text!r}")
                return text, "agy"
            log.warning(f"agy output too short ({len(text)} chars): {text!r}")
        elif _agy_hit_limit(result):
            log.warning("agy quota/rate limit hit — trying opencode fallback")
        else:
            log.warning(f"agy exit {result.returncode}: {(result.stderr or result.stdout)[:300]}")
    except subprocess.TimeoutExpired:
        log.warning("agy timed out (60s) — trying opencode fallback")
    except FileNotFoundError:
        log.warning("agy not found in PATH — trying opencode fallback")
    except Exception as e:
        log.warning(f"agy error: {e} — trying opencode fallback")

    # ── 2. Try opencode (mimo) ────────────────────────────────────────────────
    tmp_path = None
    try:
        cmd, tmp_path = _build_opencode_cmd(system_prompt)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=ROOT_DIR, env=subprocess_env(),
        )
        if result.returncode == 0:
            prose = _extract_prose(_clean_llm_text(result.stdout))
            if len(prose) >= REFLECTION_MIN:
                log.debug(f"opencode reflection ({len(prose)} chars): {prose!r}")
                return prose, "opencode"
            log.warning(f"opencode output too short ({len(prose)} chars)")
        else:
            log.warning(f"opencode exit {result.returncode}: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log.warning("opencode timed out (120s)")
    except FileNotFoundError:
        log.warning("opencode not found in PATH")
    except Exception as e:
        log.warning(f"opencode error: {e}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return None


def call_agy(article_title: str, article_body: str, prompt_text: str) -> tuple[str, str]:
    """
    Generate a reflection via agy → opencode → hardcoded fallback chain.
    Returns (reflection_text, source) where source is agy|opencode|fallback.
    """
    system_prompt = (
        "- Include multiple natural typing and writing errors: missing apostrophes (e.g. 'dont', 'im', 'cant'), minor casual typos, lowercased sentence start, informal phrasing, missing commas.\n"
        "- Write like an average 19-year-old college student typing fast on a laptop.\n"
        "- Min 80 characters, MAX 295 characters (HARD LIMIT - count carefully).\n"
        "- NO em dashes (— or –), NO AI buzzwords ('delve', 'tapestry', 'furthermore', 'crucial').\n"
        "- Directly answer the Reflection Prompt Question based on the Article Content.\n\n"
        f"Article Title: {article_title}\n"
        f"Article Content:\n{article_body[:2500]}\n"
        f"Reflection Prompt Question: {prompt_text}\n\n"
    )

    result = _run_llm_prompt(system_prompt)
    if result:
        text, source = result
        if len(text) > REFLECTION_MAX:
            cut = text.rfind(".", REFLECTION_MIN, REFLECTION_MAX)
            text = text[:cut + 1] if cut > REFLECTION_MIN else text[:REFLECTION_MAX]
        return text, source

    r = random.choice(_FALLBACKS)
    log.warning(f"agy and opencode failed — using hardcoded fallback ({len(r)} chars)")
    return r, "fallback"


_last_reflection_logged: tuple[str, str, str] = ("", "", "")


def log_reflection_generated(
    lesson_title: str, article_title: str, reflection: str, source: str,
) -> None:
    """Write reflection to events.jsonl so menubar can display it immediately."""
    global _last_reflection_logged
    key = (article_title, reflection, source)
    if key == _last_reflection_logged:
        return
    _last_reflection_logged = key
    log_event(
        "reflection_generated",
        lesson_title=lesson_title,
        article_title=article_title,
        reflection=reflection,
        chars=len(reflection),
        source=source,
    )


def needs_llm_recheck(reflection: str, source: str) -> bool:
    """Only retry LLM when we lack a real agy/opencode draft."""
    return not (reflection and reflection.strip()) or source in ("", "fallback")


def try_upgrade_reflection(
    art_title: str, art_body: str, lesson_prompt: str,
    current: str, current_source: str,
) -> tuple[str, str]:
    """Last-chance agy/opencode retry before submit (quota may have reopened)."""
    log.info("   Pre-submit LLM retry (no agy/opencode draft yet)...")
    new_text, new_source = call_agy(art_title, art_body, lesson_prompt)
    if new_source in ("agy", "opencode"):
        log.info(f"   Pre-submit got {new_source} ({len(new_text)} chars)")
        return new_text, new_source
    log.info("   Pre-submit retry still fallback — keeping current draft")
    return current or new_text, current_source or new_source


async def fill_reflection_textarea(page, reflection: str) -> int:
    """Fill the reflection textarea and return final character count."""
    for sel in ["#reflection-response", "textarea[placeholder*='minimum' i]", "textarea"]:
        try:
            ta = page.locator(sel).first
            if await ta.count() > 0 and await ta.is_visible():
                await ta.click()
                await page.wait_for_timeout(400)
                await ta.fill(reflection)
                await page.wait_for_timeout(400)
                filled_len = len(await ta.input_value())
                log.info(f"   Filled textarea: {filled_len} chars")
                if filled_len < REFLECTION_MIN:
                    pad = " this was genuinely useful info and i plan to think about it more."
                    await ta.fill((reflection + pad)[:REFLECTION_MAX])
                    filled_len = len(await ta.input_value())
                    log.info(f"   Padded to {filled_len} chars")
                return filled_len
        except Exception as e:
            log.debug(f"   textarea {sel}: {e}")
    return 0


def default_reflect_prompt(title: str) -> str:
    return f"What key lessons and insights did you take away from reading '{title}'?"


# ── Browser utils ─────────────────────────────────────────────────────────────
async def do_login(page) -> bool:
    if not EMAIL or not PASSWORD:
        log.error("Missing TFC_EMAIL or TFC_PASSWORD — set env vars or create a local .env file")
        return False
    log.info("Logging in...")
    await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    await page.fill("input[type='email']", EMAIL)
    await page.fill("input[type='password']", PASSWORD)
    await page.click("button[type='submit']")
    try:
        await page.wait_for_url("**/dashboard**", timeout=14000)
    except PWTimeout:
        pass
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(2000)
    if "login" in page.url:
        log.error(f"Login failed! URL: {page.url}")
        return False
    log.info(f"Logged in → {page.url}")
    log_event("login", status="ok")
    return True


async def ensure_auth(page) -> bool:
    await page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    if "login" in page.url:
        return await do_login(page)
    return True


async def scroll_keepalive(page):
    try:
        for pct in [0.3, 0.6, 0.9, 0.5, 0.1]:
            await page.evaluate(
                f"window.scrollTo({{top: document.body.scrollHeight*{pct}, behavior:'smooth'}})"
            )
            await page.wait_for_timeout(350)
        log.debug("↕ scroll keepalive")
    except Exception as e:
        log.debug(f"scroll err: {e}")


@dataclass
class LocalTimer:
    """Wall-clock countdown; resync from page only on schedule."""
    end_ts: float = 0.0

    def set(self, secs: int) -> None:
        self.end_ts = time.time() + max(0, secs)

    def remaining(self) -> int:
        if self.end_ts <= 0:
            return 0
        return max(0, int(self.end_ts - time.time()))

    def expired(self) -> bool:
        return self.remaining() == 0

    def end_at_iso(self) -> str:
        if self.end_ts <= 0:
            return ""
        return datetime.fromtimestamp(self.end_ts).isoformat(timespec="seconds")

    def resync(self, page_secs: int, tolerance: int = TIMER_DRIFT_TOLERANCE_S) -> bool:
        """Adjust if page timer differs from local estimate beyond tolerance."""
        if page_secs <= 0:
            return False
        drift = abs(page_secs - self.remaining())
        if drift > tolerance:
            log.debug(f"Timer drift {drift}s — resyncing local to {page_secs}s")
            self.set(page_secs)
            return True
        return False


def log_timer_sync(
    local: LocalTimer, phase: str, lesson_title: str,
    hours_done: float, hours_today: float,
) -> None:
    rem = local.remaining()
    log_event(
        "timer_sync",
        phase=phase,
        timer_secs=rem,
        timer_end_at=local.end_at_iso(),
        lesson_title=lesson_title,
        hours_done=hours_done,
        hours_today=hours_today,
    )


def parse_timer(body: str) -> int:
    """Return the smallest MM:SS countdown found (active phase timer)."""
    best = 0
    for m, s in re.findall(r'\b(\d{1,2}):(\d{2})\b', body):
        mins, secs = int(m), int(s)
        if 0 <= mins <= 120 and 0 <= secs <= 59:
            total = mins * 60 + secs
            if total > 0 and (best == 0 or total < best):
                best = total
    return best


_TIMER_JS = """() => {
  for (const el of document.querySelectorAll(
    '[class*="timer"],[class*="Timer"],[class*="countdown"],[class*="Countdown"]'
  )) {
    const t = (el.innerText || '').trim();
    if (/\\d{1,2}:\\d{2}/.test(t)) return t;
  }
  const body = document.body ? document.body.innerText : '';
  if (/ERR_|No internet|net::/.test(body)) return '__NET_ERR__';
  return body.slice(0, 1500);
}"""


async def get_timer_light(page) -> int:
    """Read timer from page without scraping full article body."""
    try:
        text = await page.evaluate(_TIMER_JS)
        if text == "__NET_ERR__":
            return -1
        if not text:
            return 0
        return parse_timer(text)
    except Exception:
        return -1


async def get_timer(page) -> int:
    return await get_timer_light(page)


async def get_progress(page) -> dict:
    try:
        if await safe_goto(page, f"{BASE_URL}/dashboard"):
            await page.wait_for_timeout(2000)
            body = await page.inner_text("body")
        m = re.search(r'(\d+\.?\d*)\s*/\s*(\d+)\s*hours', body)
        if m:
            done  = float(m.group(1))
            total = float(m.group(2))
            return {"done": done, "total": total, "remaining": total - done}
    except:
        pass
    return {"done": 0.0, "total": 75.0, "remaining": 75.0}


async def extract_article(page) -> tuple[str, str]:
    """Extract (title, body) from the article reading page only."""
    title = "Community Service Article"
    body = ""
    try:
        for sel in ["h1", "h2", ".article-title", ".lesson-title", ".course-title"]:
            el = page.locator(sel).first
            if await el.count() > 0:
                t = (await el.inner_text()).strip()
                if 5 < len(t) < 200 and "Foundation" not in t and "Log" not in t:
                    title = t
                    break

        if "/reflect" in page.url:
            log.info("   extract_article: on /reflect page — skipping body extraction")
            return title, body

        extracted_body = await page.evaluate('''() => {
            const CLUTTER = [
                "time remaining", "navigation", "dashboard", "log out", "logout",
                "reflection submitted", "submit reflection", "next article", "great work",
                "sign in", "sign out", "copyright", "privacy policy", "terms of service",
                "please share your thoughts", "write your reflection here"
            ];
            const CLUTTER_TAGS = new Set(["button", "nav", "footer", "header", "script", "style", "noscript"]);
            const MIN_LEN = 40;

            function isClutter(el, text) {
                if (CLUTTER_TAGS.has(el.tagName.toLowerCase())) return true;
                if (el.closest("nav, footer, header, [role='navigation']")) return true;
                const lower = text.toLowerCase();
                for (const word of CLUTTER) { if (lower.includes(word)) return true; }
                return false;
            }

            let container = document.querySelector("article, main, .prose, .article-body, .lesson-content, #content");
            let elements = [];
            if (container) {
                elements = Array.from(container.querySelectorAll("p, h2, h3, h4, li"));
                if (elements.length === 0) elements = [container];
            } else {
                elements = Array.from(document.querySelectorAll("p, li"));
            }

            const seen = new Set();
            const texts = [];
            for (const el of elements) {
                const text = (el.innerText || "").trim();
                if (!text || text.length < MIN_LEN) continue;
                if (seen.has(text) || isClutter(el, text)) continue;
                seen.add(text);
                texts.push(text);
                if (texts.join("\\n\\n").length > 3500) break;
            }
            return texts.join("\\n\\n");
        }''')

        if extracted_body and len(extracted_body.strip()) > 80:
            body = extracted_body[:3000]
    except Exception as e:
        log.warning(f"article extract: {e}")
    return title, body


async def extract_reflect_prompt(page, title: str) -> str:
    """Read the reflection instructions from the /reflect page."""
    try:
        prompt = await page.evaluate('''() => {
            const form = document.querySelector("form");
            if (!form) return "";
            const paras = Array.from(form.parentElement ? form.parentElement.querySelectorAll("p") : []);
            for (const p of paras) {
                const text = (p.innerText || "").trim();
                if (text.length >= 40 && text.length <= 500) return text;
            }
            return "";
        }''')
        if prompt:
            log.info(f"   Reflect page prompt: {prompt[:120]!r}")
            return prompt
    except Exception as e:
        log.warning(f"reflect prompt extract: {e}")
    return default_reflect_prompt(title)


async def safe_goto(page, url: str, retries: int = 3) -> bool:
    """Navigate with login recovery."""
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            if "login" in page.url:
                log.warning(f"Session expired navigating to {url} — re-logging in")
                if not await do_login(page):
                    return False
                continue
            return True
        except Exception as e:
            log.warning(f"nav attempt {attempt + 1}/{retries} failed for {url}: {e}")
            await page.wait_for_timeout(2000)
    return False


@dataclass
class LessonEntry:
    title: str
    status: str          # done | continue | start
    url: Optional[str] = None
    duration: str = ""


LESSON_ROW_RE = re.compile(
    r"(?:✅|📖|○)\s*\n(.+?)(\d+\s*min)\s*\n(Done|Continue|Start)",
    re.MULTILINE,
)
UUID_RE = re.compile(r"/coursework/([a-f0-9-]{36})$")


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def _match_url_for_title(title: str, url_map: dict[str, str]) -> Optional[str]:
    return url_map.get(_normalize_title(title))


async def fetch_coursework_catalog(page) -> tuple[list[LessonEntry], Optional[str]]:
    """Scrape /coursework for lesson statuses and build an ordered catalog using robust DOM evaluation."""
    if not await safe_goto(page, f"{BASE_URL}/coursework"):
        log.error("Failed to load coursework page")
        return [], None

    await page.wait_for_timeout(3000)

    # Robust Playwright JS evaluation querying lesson row containers directly from the DOM on /coursework
    catalog_data = await page.evaluate("""() => {
        const rows = [];
        const seenTitles = new Set();
        const ignoreRegex = /Need help\\?|RECOMMENDED FOR YOU|Your Coursework|Overall Progress|Hours Remaining|Today's Limit|Dashboard|Back to Dashboard/i;

        const candidates = Array.from(document.querySelectorAll('div, li'));

        for (const el of candidates) {
            const text = (el.innerText || '').trim();
            if (!text || text.length > 350) continue;
            if (ignoreRegex.test(text)) continue;

            const durMatch = text.match(/(\\d+\\s*min)/i);
            if (!durMatch) continue;

            let status = null;
            if (/\\bDone\\b/i.test(text)) status = 'done';
            else if (/\\bContinue\\b/i.test(text)) status = 'continue';
            else if (/\\bStart\\b/i.test(text)) status = 'start';
            if (!status) continue;

            const childMatches = Array.from(el.querySelectorAll('div, li')).some(child => {
                if (child === el) return false;
                const ct = (child.innerText || '').trim();
                return ct.length < 350 && /(\\d+\\s*min)/i.test(ct) && /\\b(Done|Continue|Start)\\b/i.test(ct);
            });
            if (childMatches) continue;

            const duration = durMatch[1].trim();

            const linkEl = el.querySelector('a[href*="/coursework/"]');
            let href = null;
            if (linkEl) {
                const h = linkEl.getAttribute('href');
                if (h) {
                    href = h.startsWith('/') ? window.location.origin + h : h;
                }
            }

            const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
            let rawTitle = '';
            for (const line of lines) {
                if (/^(✅|📖|○|Done|Continue|Start)$/i.test(line)) continue;
                if (/^\\d+\\s*min$/i.test(line)) continue;
                if (line.length > 3) {
                    rawTitle = line.replace(/\\s*\\d+\\s*min\\s*$/i, '').trim();
                    break;
                }
            }

            if (!rawTitle || rawTitle.length < 3) continue;

            let title = rawTitle
                .replace(/^(✅|📖|○)\\s*/, '')
                .replace(/\\s*(Done|Continue|Start)$/i, '')
                .replace(/\\s*\\d+\\s*min$/, '')
                .trim();

            if (ignoreRegex.test(title)) continue;

            const normKey = title.toLowerCase();
            if (!seenTitles.has(normKey)) {
                seenTitles.add(normKey);
                rows.push({
                    title: title,
                    duration: duration,
                    status: status,
                    url: href
                });
            }
        }

        let ctaUrl = null;
        for (const a of document.querySelectorAll('a[href*="/coursework/"]')) {
            if (/Continue Coursework/i.test(a.innerText || '')) {
                const h = a.getAttribute('href');
                if (h) {
                    ctaUrl = h.startsWith('/') ? window.location.origin + h : h;
                    break;
                }
            }
        }

        return { rows, ctaUrl };
    }""")

    rows_raw = catalog_data.get("rows", [])
    cta_url = catalog_data.get("ctaUrl")

    # If CTA button wasn't found by text, try Playwright locator fallback
    if not cta_url:
        try:
            cta = page.locator("a:has-text('Continue Coursework')").first
            if await cta.count() > 0:
                href = await cta.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = BASE_URL + href
                    if UUID_RE.search(href):
                        cta_url = href
        except Exception:
            pass

    # Build LessonEntry objects
    lessons: list[LessonEntry] = []
    for r in rows_raw:
        entry = LessonEntry(
            title=r["title"],
            duration=r["duration"],
            status=r["status"],
            url=r["url"],
        )
        lessons.append(entry)

    # Attach CTA URL to the Continue lesson if URL wasn't directly in the row
    if cta_url:
        for lesson in lessons:
            if lesson.status == "continue" and not lesson.url:
                lesson.url = cta_url
                log.info(f"   Mapped continue lesson {lesson.title!r} → CTA URL")

    done = sum(1 for l in lessons if l.status == "done")
    cont = sum(1 for l in lessons if l.status == "continue")
    start = sum(1 for l in lessons if l.status == "start")
    log.info(f"Catalog: {len(lessons)} lessons extracted ({done} done, {cont} continue, {start} start)")
    if cta_url:
        log.info(f"   CTA continue: {cta_url}")

    # ── Update completed_courses.json ──────────────────────────────────────────
    completed_titles = [l.title for l in lessons if l.status == "done"]
    
    existing_completed = []
    if os.path.exists(COMPLETED_COURSES_FILE):
        try:
            with open(COMPLETED_COURSES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    existing_completed = data.get("courses", [])
                elif isinstance(data, list):
                    existing_completed = data
        except Exception as e:
            log.warning(f"Could not read existing completed_courses.json: {e}")

    merged_titles = list(existing_completed)
    for t in completed_titles:
        if t not in merged_titles:
            merged_titles.append(t)

    try:
        with open(COMPLETED_COURSES_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "count": len(merged_titles),
                "courses": merged_titles,
                "updated": datetime.now().isoformat()
            }, f, indent=2)
        log.info("╭────────────────────────────────────────────────────────╮")
        log.info(f"│ 🎓 COMPLETED COURSES LIST ({len(merged_titles):<27}) │")
        log.info("├────────────────────────────────────────────────────────┤")
        for i, title in enumerate(merged_titles, 1):
            log.info(f"│ {i:2d}. {title}")
        log.info("╰────────────────────────────────────────────────────────╯")
    except Exception as e:
        log.error(f"Failed to save completed courses: {e}")

    log_event("completed_courses_snapshot", count=len(merged_titles), courses=merged_titles)
    log_event("catalog_snapshot", total=len(lessons), done=done,
              continue_count=cont, start=start, cta_url=cta_url)

    return lessons, cta_url


def build_work_queue(lessons: list[LessonEntry], cta_url: Optional[str],
                     skip_urls: set[str]) -> list[LessonEntry]:
    """Return incomplete lessons in priority order, skipping known URLs."""
    queue: list[LessonEntry] = []
    seen: set[str] = set()

    def add(lesson: LessonEntry):
        if lesson.status == "done" or not lesson.url:
            return
        if lesson.url in skip_urls or lesson.url in seen:
            return
        queue.append(lesson)
        seen.add(lesson.url)

    if cta_url and cta_url not in skip_urls:
        match = next((l for l in lessons if l.url == cta_url), None)
        if not match:
            match = next((l for l in lessons if l.status == "continue"), None)
        if match:
            entry = LessonEntry(
                title=match.title,
                status=match.status,
                url=cta_url,
                duration=match.duration,
            )
            if entry.status != "done":
                add(entry)

    for lesson in lessons:
        if lesson.status in ("continue", "start"):
            add(lesson)

    return queue


async def page_has_reflect_form(page) -> bool:
    try:
        body = await page.inner_text("body")
        if any(kw in body for kw in ["Reflection Submitted", "Next Article", "Great work"]):
            return False
        ta = page.locator("textarea").first
        return await ta.count() > 0 and await ta.is_visible()
    except Exception:
        return False


async def inspect_lesson(page, lesson: LessonEntry) -> str:
    """
    Determine what work a lesson still needs.
    Returns: complete | needs_read | needs_reflect
    """
    if lesson.status == "done":
        log.info(f"   ✓ Catalog marks done — skipping: {lesson.title!r}")
        return "complete"
    if not lesson.url:
        log.warning(f"   No URL for lesson — skipping: {lesson.title!r}")
        return "complete"

    reflect_url = lesson.url.rstrip("/") + "/reflect"

    # Step 1: Check reading URL first (resilient detection of active reading timer)
    if await safe_goto(page, lesson.url):
        secs = await get_timer(page)
        body = await page.inner_text("body")
        if secs > 0:
            log.info(f"   → Needs reading (timer active {secs//60}:{secs%60:02d}): {lesson.title!r}")
            return "needs_read"
        if "Time Remaining" in body and "REFLECTION FOR" not in body:
            log.info(f"   → Needs reading (timer on page): {lesson.title!r}")
            return "needs_read"

    # Step 2: Check reflect URL (resilient detection of submitted or active reflection timer)
    if await safe_goto(page, reflect_url):
        body = await page.inner_text("body")
        if any(kw in body for kw in ["Reflection Submitted", "Next Article", "Great work"]):
            log.info(f"   ✓ Reflection already submitted: {lesson.title!r}")
            return "complete"
        if await page_has_reflect_form(page):
            log.info(f"   → Needs reflect: {lesson.title!r}")
            return "needs_reflect"
        secs = await get_timer(page)
        if secs > 0:
            log.info(f"   → Needs reflect (timer active {secs//60}:{secs%60:02d}): {lesson.title!r}")
            return "needs_reflect"

    if lesson.status == "continue":
        log.info(f"   → Status is continue — treating as needs_read: {lesson.title!r}")
        return "needs_read"

    if lesson.status == "start":
        log.info(f"   → Status is start — treating as needs_read: {lesson.title!r}")
        return "needs_read"

    log.warning(f"   Could not determine state for {lesson.title!r} — skipping")
    return "complete"


# ── Lesson phases ─────────────────────────────────────────────────────────────
async def wait_for_timer(
    page, phase: str, lesson_title: str,
    hours_done: float, hours_today: float, hours_total: float,
    rs: Optional[RunState] = None,
):
    """Local wall-clock countdown; resync from page every TIMER_RESYNC_S with scroll."""
    local = LocalTimer()
    elapsed = 0
    since_resync = TIMER_RESYNC_S  # trigger initial DOM read immediately
    last_log_min = -1

    while True:
        if since_resync >= TIMER_RESYNC_S:
            secs = await get_timer_light(page)
            if secs == -1:
                log.warning(f"[{phase}] network error reading timer, retrying in {LOCAL_TICK_S}s...")
                await asyncio.sleep(LOCAL_TICK_S)
                elapsed += LOCAL_TICK_S
                since_resync += LOCAL_TICK_S
                continue
            if secs == 0:
                log.info(f"[{phase}] ✓ timer expired — {lesson_title!r}")
                clear_live_status()
                break

            if local.end_ts == 0:
                local.set(secs)
                live_status(phase, secs, lesson_title, hours_done, hours_today, hours_total, rs, force=True)
                log_timer_sync(local, phase, lesson_title, hours_done, hours_today)
            else:
                await scroll_keepalive(page)
                log.debug(f"[{phase}] ↕ scroll keepalive + timer resync at {elapsed // 60}min elapsed")
                local.set(secs)
                log_timer_sync(local, phase, lesson_title, hours_done, hours_today)
            since_resync = 0

        rem = local.remaining()
        if local.end_ts > 0 and rem == 0:
            verify = await get_timer_light(page)
            if verify == 0:
                log.info(f"[{phase}] ✓ timer expired — {lesson_title!r}")
                clear_live_status()
                break
            if verify == -1:
                log.warning(f"[{phase}] network error verifying expiry, retrying...")
                await asyncio.sleep(LOCAL_TICK_S)
                elapsed += LOCAL_TICK_S
                since_resync += LOCAL_TICK_S
                continue
            if verify > 180:
                log.info(f"[{phase}] ✓ timer expired — {lesson_title!r}")
                clear_live_status()
                break
            local.set(verify)
            log_timer_sync(local, phase, lesson_title, hours_done, hours_today)
            since_resync = 0
            continue

        if local.end_ts > 0:
            live_status(phase, rem, lesson_title, hours_done, hours_today, hours_total, rs)

            mins_remaining = rem // 60
            if mins_remaining != last_log_min and mins_remaining % 5 == 0:
                log.debug(
                    f"[{phase}] ⏱ {mins_remaining}min remaining  {lesson_title!r}  "
                    f"today:{hours_today:.1f}h"
                )
                last_log_min = mins_remaining

        if elapsed > 95 * 60:
            log.warning(f"[{phase}] safety cap hit — moving on")
            clear_live_status()
            break

        await asyncio.sleep(LOCAL_TICK_S)
        elapsed += LOCAL_TICK_S
        since_resync += LOCAL_TICK_S


async def reading_phase(
    page, lesson: LessonEntry, hours_done: float, hours_today: float,
    hours_total: float, rs: Optional[RunState] = None,
) -> tuple[str, str, str, str, str]:
    """Returns (article_title, article_body, pre_reflection, lesson_prompt, reflection_source)."""
    log.info(f"📖 Reading: {lesson.title!r} — {lesson.url}")
    log_event("reading_start", lesson_url=lesson.url, lesson_title=lesson.title,
              hours_today=hours_today, hours_done=hours_done)

    if not await safe_goto(page, lesson.url):
        raise RuntimeError(f"Could not open reading page for {lesson.title!r}")

    title, body = await extract_article(page)
    log.info(f"   Article: {title!r}")
    if not body:
        body = (
            f"This article covered important topics related to {title}. "
            "It discussed community impact, personal responsibility, and evidence-based approaches."
        )
    lesson_prompt = default_reflect_prompt(title)

    loop = asyncio.get_event_loop()
    agy_task = loop.run_in_executor(
        None, call_agy, title, body, lesson_prompt
    )

    secs = await get_timer(page)
    if secs > 0:
        log.info(f"   Reading timer: {secs//60}:{secs%60:02d}")
        await wait_for_timer(page, "READ", title, hours_done, hours_today, hours_total, rs)
    else:
        log.info("   No reading timer")

    reflection, reflection_source = await agy_task
    log_reflection_generated(lesson.title, title, reflection, reflection_source)
    return title, body, reflection, lesson_prompt, reflection_source


async def reflect_phase(
    page, lesson: LessonEntry, art_title: str, art_body: str, pre_reflection: str,
    hours_done: float, hours_today: float, hours_total: float,
    pre_source: str = "",
    rs: Optional[RunState] = None,
) -> bool:
    reflect_url = lesson.url.rstrip("/") + "/reflect"
    log.info(f"✍️  Reflect: {art_title!r} — {reflect_url}")
    log_event("reflect_start", lesson_url=lesson.url, lesson_title=lesson.title,
              article_title=art_title, hours_today=hours_today, hours_done=hours_done)

    if not await safe_goto(page, reflect_url):
        log.error(f"Could not open reflect page for {lesson.title!r}")
        return False

    body_text = await page.inner_text("body")
    if any(kw in body_text for kw in ["Reflection Submitted", "Next Article", "Great work"]):
        log.info(f"   ✓ Already submitted: {art_title!r}")
        return True

    lesson_prompt = await extract_reflect_prompt(page, art_title)
    log.info(f"Reflection Prompt Question: {lesson_prompt!r}")

    reflection = pre_reflection
    reflection_source = pre_source
    if reflection and reflection_source not in ("", "fallback"):
        log.info(f"   Using pre-generated reflection from {reflection_source} ({len(reflection)} chars)")
    else:
        if reflection and reflection_source == "fallback":
            log.info("   Pre-generated reflection was hardcoded fallback — retrying LLM...")
        else:
            log.info("   Calling LLM for reflection...")
        reflection, reflection_source = call_agy(art_title, art_body, lesson_prompt)

    if not (pre_reflection and pre_reflection == reflection and pre_source == reflection_source):
        log_reflection_generated(lesson.title, art_title, reflection, reflection_source)

    filled_len = await fill_reflection_textarea(page, reflection)

    try:
        stars = page.locator("button:has-text('★')")
        n = await stars.count()
        if n >= 4:
            await stars.nth(3).click()
            log.info("   ⭐⭐⭐⭐ (4/5 stars)")
        elif n > 0:
            await stars.last.click()
        await page.wait_for_timeout(500)
    except Exception as e:
        log.warning(f"   star err: {e}")

    secs = await get_timer(page)
    if secs > 0:
        log.info(f"   Reflect timer: {secs//60}:{secs%60:02d}")
        await wait_for_timer(page, "REFLECT", art_title, hours_done, hours_today, hours_total, rs)

    if needs_llm_recheck(reflection, reflection_source):
        loop = asyncio.get_event_loop()
        upgraded, upgraded_source = await loop.run_in_executor(
            None, try_upgrade_reflection,
            art_title, art_body, lesson_prompt, reflection, reflection_source,
        )
        if upgraded_source in ("agy", "opencode"):
            reflection, reflection_source = upgraded, upgraded_source
            log_reflection_generated(lesson.title, art_title, reflection, reflection_source)
            filled_len = await fill_reflection_textarea(page, reflection)
    else:
        log.info(f"   Pre-submit skip — already have {reflection_source} draft")

    submit_sel = "button:has-text('Submit Reflection'), button.btn-cta[type='submit']"
    for attempt in range(20):
        try:
            btn = page.locator(submit_sel).first
            if await btn.count() > 0:
                disabled = await btn.get_attribute("disabled")
                if disabled is None:
                    log.info(f"   Submitting (attempt {attempt+1})...")
                    await btn.click()
                    break
                live_status("SUBMIT_WAIT", 0, art_title, hours_done, hours_today, hours_total, rs)
                await page.wait_for_timeout(5000)
        except Exception as e:
            log.warning(f"   submit check: {e}")
            await page.wait_for_timeout(4000)

    sys.stderr.write("\n")
    await page.wait_for_timeout(3000)

    body_text = await page.inner_text("body")
    success = any(kw in body_text for kw in
                  ["Reflection Submitted", "Next Article", "Great work"])

    log_event("reflect_submitted", lesson_title=lesson.title,
              article_title=art_title, success=success,
              reflection_chars=filled_len)

    if success:
        log.info(f"   ✅ Submitted: {art_title!r}")
    else:
        log.warning(f"   ⚠️  Submission unconfirmed: {art_title!r}")

    return success


# ── Daily limit check ─────────────────────────────────────────────────────────
def check_daily_limit(hours_today: float, hours_remaining: Optional[float] = None) -> bool:
    """Returns True if we've hit the daily limit or can't fit another lesson."""
    remaining = hours_remaining if hours_remaining is not None else max(0, DAILY_HOUR_LIMIT - hours_today)

    if hours_today >= DAILY_HOUR_LIMIT or remaining <= 0:
        log.warning(
            f"⛔ Daily limit reached: {hours_today:.1f}h / {DAILY_HOUR_LIMIT}h. "
            f"Stopping for today."
        )
        log_event("daily_limit_hit", hours_today=hours_today, limit=DAILY_HOUR_LIMIT,
                  hours_remaining=remaining)
        return True

    if remaining < MIN_HOURS_LEFT:
        log.warning(
            f"⛔ Only {remaining:.1f}h left today (need ~{MIN_HOURS_LEFT}h per lesson). "
            f"Stopping — resume tomorrow."
        )
        log_event("daily_limit_near", hours_today=hours_today, hours_remaining=remaining,
                  min_needed=MIN_HOURS_LEFT)
        return True

    return False


async def get_user_profile(page) -> dict:
    """Scrape full user profile fields directly from /dashboard/profile and /dashboard."""
    info = {}

    try:
        # Check /dashboard for welcome name and enrollment proof link
        await safe_goto(page, f"{BASE_URL}/dashboard")
        await page.wait_for_timeout(1000)
        text = await page.inner_text("body")

        m_name = re.search(r"Welcome back,\s*([^\n\r]+)", text, re.IGNORECASE)
        if m_name:
            info["FULL NAME"] = m_name.group(1).strip()

        proof_link = page.locator("a[href*='/api/enrollment-proof/']")
        if await proof_link.count() > 0:
            href = await proof_link.first.get_attribute("href") or ""
            m_id = re.search(r"/enrollment-proof/([^/]+)", href)
            if m_id:
                enrollment_id = m_id.group(1)
                info["ENROLLMENT PROOF ID"] = enrollment_id
                info["OFFICIAL ENROLLMENT PROOF PDF URL"] = f"https://www.thefoundationofchange.org/api/enrollment-proof/{enrollment_id}/pdf"

        info["COURT AUTHORIZATION LETTER LINK"] = "https://www.thefoundationofchange.org/letter-of-introductions"
        info["CERTIFICATE VERIFICATION PORTAL LINK"] = "https://www.thefoundationofchange.org/certificate-verification"

        # Go to Edit Profile page (/dashboard/profile)
        await safe_goto(page, f"{BASE_URL}/dashboard/profile")
        await page.wait_for_timeout(1000)

        # Execute JS evaluator to pair every label with its input/select value
        fields = await page.evaluate('''() => {
            const result = {};
            const form = document.querySelector("form") || document.body;
            const elements = form.querySelectorAll("label, input, select");
            let currentLabel = "";
            elements.forEach(el => {
                if (el.tagName.toLowerCase() === "label") {
                    currentLabel = el.innerText.trim();
                } else if (el.tagName.toLowerCase() === "input" || el.tagName.toLowerCase() === "select") {
                    let val = el.value || "";
                    if (el.tagName.toLowerCase() === "select") {
                        val = el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : val;
                    }
                    val = val.trim();
                    if (currentLabel && val && val !== "Select...") {
                        result[currentLabel] = val;
                    }
                    currentLabel = "";
                }
            });
            return result;
        }''')

        if isinstance(fields, dict):
            for k, v in fields.items():
                if v:
                    info[k] = v

    except Exception as e:
        log.warning(f"Could not scrape detailed user profile: {e}")

    return info


def load_bot_completed_titles() -> list[str]:
    path = os.path.join(ROOT_DIR, "bot_completed_courses.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        courses = data.get("courses", data) if isinstance(data, dict) else data
        titles = []
        for c in courses:
            if isinstance(c, dict):
                t = c.get("title", "")
            else:
                t = str(c)
            if t:
                titles.append(t)
        return titles
    except Exception:
        return []


def log_user_profile(user_info: dict, prog: dict, hours_today: float = 0.0, catalog_done: int = 0):
    """Log structured user information banner upon login."""
    clear_live_status()
    rem_h = float(prog.get("remaining", 0) or 0)
    done_h = float(prog.get("done", 0) or 0)
    total_h = float(prog.get("total", 75) or 75)
    pct = int((done_h / total_h) * 100) if total_h > 0 else 0
    eta_label = format_eta_label(estimate_days_to_complete(rem_h, hours_today))

    log.info("╭────────────────────────────────────────────────────────╮")
    log.info("│ 👤 USER PROFILE                                        │")
    log.info("├────────────────────────────────────────────────────────┤")

    display_order = [
        "FULL NAME", "Full Name", "name",
        "EMAIL (READ-ONLY)", "EMAIL", "Email", "email",
        "DATE OF BIRTH", "dob", "PHONE", "GENDER",
        "REASON FOR COMMUNITY SERVICE", "COMMUNITY SERVICE RELATED TO", "reason",
        "ADDRESS", "address", "CITY", "STATE", "ZIP CODE",
        "PROBATION OFFICER", "COURT ID",
        "ENROLLMENT PROOF ID", "Enrollment Proof ID", "enrollment_id",
        "OFFICIAL ENROLLMENT PROOF PDF URL", "OFFICIAL PROOF PDF LINK",
        "COURT AUTHORIZATION LETTER LINK", "CERTIFICATE VERIFICATION PORTAL LINK",
    ]
    logged_keys: set[str] = set()
    for key in display_order:
        if key in user_info and key not in logged_keys and user_info[key]:
            log.info(f"│ {key:<30}: {str(user_info[key])[:80]}")
            logged_keys.add(key)

    for k, v in sorted(user_info.items()):
        if k not in logged_keys and v and k not in ("ts", "event", "date"):
            log.info(f"│ {k:<30}: {str(v)[:80]}")
            logged_keys.add(k)

    log.info("├────────────────────────────────────────────────────────┤")
    log.info(f"│ {'Progress':<30}: {done_h:.1f}h / {total_h:.0f}h ({pct}%)")
    log.info(f"│ {'Hours remaining':<30}: {rem_h:.1f}h")
    log.info(f"│ {'Est. completion':<30}: {eta_label}")
    log.info(f"│ {'Logged today':<30}: {hours_today:.1f}h / {DAILY_HOUR_LIMIT:.0f}h")
    if catalog_done:
        log.info(f"│ {'Lessons done on site':<30}: {catalog_done}")
    log.info("╰────────────────────────────────────────────────────────╯")
    prof_path = os.path.join(ROOT_DIR, "user_profile.json")
    try:
        with open(prof_path, "w", encoding="utf-8") as f:
            json.dump(user_info, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save user_profile.json: {e}")
    log_event("user_profile_loaded", **user_info)


def log_catalog_summary(lessons: list, session_done: int = 0) -> None:
    """Log coursework catalog snapshot at startup."""
    done = sum(1 for l in lessons if l.status == "done")
    cont = sum(1 for l in lessons if l.status == "continue")
    start = sum(1 for l in lessons if l.status == "start")
    clear_live_status()
    log.info("╭────────────────────────────────────────────────────────╮")
    log.info("│ 📚 COURSEWORK CATALOG                                  │")
    log.info("├────────────────────────────────────────────────────────┤")
    log.info(f"│ {'Total lessons':<24}: {len(lessons):>5}")
    log.info(f"│ {'Completed on site':<24}: {done:>5}")
    log.info(f"│ {'In progress':<24}: {cont:>5}")
    log.info(f"│ {'Not started':<24}: {start:>5}")
    if session_done:
        log.info(f"│ {'Finished this session':<24}: {session_done:>5}")
    upcoming = [l for l in lessons if l.status in ("continue", "start")][:3]
    if upcoming:
        log.info("├────────────────────────────────────────────────────────┤")
        log.info("│ Up next:")
        for i, lesson in enumerate(upcoming, 1):
            t = lesson.title[:44] + ("…" if len(lesson.title) > 44 else "")
            log.info(f"│   {i}. [{lesson.status:8}] {t}")
    log.info("╰────────────────────────────────────────────────────────╯")


def log_completion_breakdown(lessons: list) -> None:
    """Show bot-completed vs site-completed lessons."""
    bot_titles = load_bot_completed_titles()
    bot_norm = {t.strip().lower() for t in bot_titles}
    site_done = [l.title for l in lessons if l.status == "done"]
    site_norm = {t.strip().lower() for t in site_done}

    bot_only = [t for t in bot_titles if t.strip().lower() not in site_norm]
    site_only = [t for t in site_done if t.strip().lower() not in bot_norm]
    both = [t for t in bot_titles if t.strip().lower() in site_norm]

    clear_live_status()
    log.info("╭────────────────────────────────────────────────────────╮")
    log.info("│ ✅ COMPLETION BREAKDOWN                                │")
    log.info("├────────────────────────────────────────────────────────┤")
    log.info(f"│ {'Completed on site (catalog)':<30}: {len(site_done):>5}")
    log.info(f"│ {'Completed by this bot':<30}: {len(bot_titles):>5}")
    log.info(f"│ {'Bot + site (matched)':<30}: {len(both):>5}")
    log.info(f"│ {'Bot only (not yet on site)':<30}: {len(bot_only):>5}")
    log.info(f"│ {'Site only (not by bot)':<30}: {len(site_only):>5}")

    if bot_titles:
        log.info("├────────────────────────────────────────────────────────┤")
        log.info("│ Last completed by bot:")
        for i, t in enumerate(bot_titles[-5:], 1):
            log.info(f"│   {i}. {t[:52]}{'…' if len(t) > 52 else ''}")
    log.info("╰────────────────────────────────────────────────────────╯")


async def check_site_reset_with_reauth(page) -> dict:
    """
    Check site daily limit status. If session expired or cookie cleared,
    automatically triggers fallback re-login via ensure_auth.
    """
    daily = await get_daily_status(page)
    if daily.get("source") != "site":
        log.warning("🔑 Site status check lost session / logged out — executing fallback re-login...")
        if await ensure_auth(page):
            daily = await get_daily_status(page)
    return daily


async def wait_for_daily_reset(page, rs: RunState):
    """
    Called when daily limit (8.0h) is reached.
    Notifies the user and rests until:
      1. 5 minutes before midnight (11:55 PM) - Early reset check
      2. At Midnight (12:00 AM) - Midnight reset check
      3. Every 15 minutes after midnight (12:15 AM, 12:30 AM...) - Retries until site updates
    Uses automatic re-login as a fallback if cookies/session cleared.
    """
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    secs_until_midnight = int((tomorrow - now).total_seconds())

    h_str = f"{secs_until_midnight // 3600}h {(secs_until_midnight % 3600) // 60}m {secs_until_midnight % 60}s"

    CAFFEINATE_MANAGER.stop()

    log.info("╭────────────────────────────────────────────────────────╮")
    log.info("│ ⛔ DAILY LIMIT REACHED ON PLATFORM (8.0h / 8.0h max)   │")
    log.info("├────────────────────────────────────────────────────────┤")
    log.info(f"│ 📅 Today's Logged Hours: {rs.hours_today:.1f}h")
    log.info(f"│ 📊 Overall Progress: {rs.hours_done:.1f}h / {rs.hours_total:.1f}h total")
    log.info("│ 🔔 USER NOTIFICATION: Limit reached. Bot resting...    │")
    log.info(f"│ ⏰ Target Midnight Reset: {tomorrow.strftime('%Y-%m-%d 12:00:00 AM')}")
    log.info("│ 🕒 Scheduled Checks: 11:55 PM | 12:00 AM | Every 15m post │")
    log.info(f"│ ⏳ Time until reset: {h_str}")
    log.info("╰────────────────────────────────────────────────────────╯")

    log_event(
        "daily_limit_wait_start",
        seconds_until_midnight=secs_until_midnight,
        reset_target=tomorrow.isoformat(),
        hours_today=rs.hours_today,
        hours_done=rs.hours_done,
    )

    start_time = time.time()
    last_notify_time = 0.0
    last_scroll_time = time.time()

    pre_midnight_checked = False
    last_limit_status_min = -1

    while True:
        now = datetime.now()
        secs_remaining = max(0, int((tomorrow - now).total_seconds()))

        limit_min = secs_remaining // 60
        if limit_min != last_limit_status_min:
            live_status(
                "LIMIT_WAIT", secs_remaining, "Daily Limit — midnight reset",
                rs.hours_done, rs.hours_today, rs.hours_total, rs, force=True,
            )
            last_limit_status_min = limit_min

        if time.time() - last_notify_time >= 600:
            last_notify_time = time.time()
            rem_h = secs_remaining // 3600
            rem_m = (secs_remaining % 3600) // 60
            log.info(f"🌙 [LIMIT_WAIT] ⏱ {rem_h:02d}h {rem_m:02d}m remaining until daily reset (12:00 AM). Resting...")

        # ── 1. Check 5 minutes before midnight (11:55 PM) ────────────────────
        if secs_remaining <= 300 and not pre_midnight_checked:
            pre_midnight_checked = True
            log.info("⏰ 5 minutes before midnight (11:55 PM) — inspecting site for early reset...")
            daily = await check_site_reset_with_reauth(page)
            if daily.get("hours_remaining_today", 0) > 0 and not daily.get("site_limit_reached") and daily.get("source") == "site":
                log_event("daily_limit_reset_detected", timing="pre_midnight")
                log.info("🌅 Early limit reset confirmed (11:55 PM)! Resuming coursework...")
                break

        # ── 2. Check at Midnight (12:00 AM) & every 15 minutes after ──────────
        if secs_remaining <= 0:
            log.info("🌅 Local midnight reached (12:00 AM)! Checking daily limit status on site...")
            await page.wait_for_timeout(3000)
            daily = await check_site_reset_with_reauth(page)

            if daily.get("hours_remaining_today", 0) > 0 and not daily.get("site_limit_reached") and daily.get("source") == "site":
                log_event("daily_limit_reset_detected", timing="midnight")
                log.info("🌅 Limit reset confirmed at 12:00 AM! Resuming coursework...")
                break
            else:
                log.info("⏳ Midnight reached, but site hasn't updated yet. Checking every 15 minutes (12:15 AM, 12:30 AM...)...")
                for _ in range(15):  # 15 x 60s = 900s (15 min)
                    now_post = datetime.now()
                    live_status(
                        "LIMIT_WAIT", 0, "Midnight Passed - Retrying every 15m",
                        rs.hours_done, rs.hours_today, rs.hours_total, rs
                    )
                    await asyncio.sleep(LOCAL_TICK_S)

                log.info("🔍 15-minute post-midnight check: inspecting daily limit status on site...")
                daily_retry = await check_site_reset_with_reauth(page)
                if daily_retry.get("hours_remaining_today", 0) > 0 and not daily_retry.get("site_limit_reached") and daily_retry.get("source") == "site":
                    log_event("daily_limit_reset_detected", timing="post_midnight_15m")
                    log.info("🌅 Limit reset confirmed post-midnight! Resuming coursework...")
                    break
                continue

        # In-page micro-scroll keep-alive every 5 minutes
        if time.time() - last_scroll_time >= 300:
            last_scroll_time = time.time()
            try:
                await page.evaluate("window.scrollBy(0, 50)")
                await page.wait_for_timeout(200)
                await page.evaluate("window.scrollBy(0, -50)")
            except Exception:
                pass

        await asyncio.sleep(LOCAL_TICK_S)

    log_event("daily_limit_wait_complete", waited_seconds=int(time.time() - start_time))


async def process_lesson(
    page, lesson: LessonEntry, hours_today: float, rs: RunState,
) -> tuple[bool, float]:
    """Run one lesson end-to-end. Returns (success, hours_gained)."""
    prog = await get_progress(page)
    rs.hours_done = prog["done"]
    rs.hours_total = prog["total"]
    rs.hours_today = hours_today

    state = await inspect_lesson(page, lesson)
    if state == "complete":
        return True, 0.0

    art_title = lesson.title
    art_body = (
        f"This article covered important topics related to {lesson.title}. "
        "It discussed community impact, personal responsibility, and evidence-based approaches."
    )
    reflection = ""
    reflection_source = ""

    if state == "needs_read":
        art_title, art_body, reflection, _, reflection_source = await reading_phase(
            page, lesson, prog["done"], hours_today, prog["total"], rs
        )
    elif state == "needs_reflect":
        article_url = lesson.url.rstrip("/").replace("/reflect", "")
        if await safe_goto(page, article_url):
            await page.wait_for_timeout(1500)
            if "/reflect" not in page.url:
                t, b = await extract_article(page)
                if t and t != "Community Service Article":
                    art_title = t
                if b:
                    art_body = b
        log.info(f"   Reading already complete — going to reflect for {art_title!r}")


    prog_before = await get_progress(page)
    success = await reflect_phase(
        page, lesson, art_title, art_body, reflection,
        prog_before["done"], hours_today, prog_before["total"],
        pre_source=reflection_source, rs=rs,
    )

    prog_after = await get_progress(page)
    hours_gained = max(0.0, prog_after["done"] - prog_before["done"])

    if success:
        completed_path = os.path.join(ROOT_DIR, "completed_courses.json")
        try:
            with open(completed_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                courses = data.get("courses", [])
        except Exception:
            courses = []
        if lesson.title not in courses:
            courses.append(lesson.title)
        try:
            with open(completed_path, "w", encoding="utf-8") as f:
                json.dump({"count": len(courses), "courses": courses, "updated": datetime.now().isoformat()}, f, indent=2)
            log_event("completed_courses_snapshot", count=len(courses), courses=courses)
        except Exception as e:
            log.error(f"Failed to update completed courses: {e}")

        bot_completed_path = os.path.join(ROOT_DIR, "bot_completed_courses.json")
        try:
            with open(bot_completed_path, "r", encoding="utf-8") as f:
                b_data = json.load(f)
                b_courses = b_data.get("courses", [])
        except Exception:
            b_courses = []
            
        found = False
        for c in b_courses:
            if isinstance(c, dict) and c.get("title") == lesson.title:
                found = True
                break
            elif c == lesson.title:
                found = True
                break
                
        if not found:
            b_courses.append({"title": lesson.title, "ts": datetime.now().isoformat()})
            
        try:
            with open(bot_completed_path, "w", encoding="utf-8") as f:
                json.dump({"count": len(b_courses), "courses": b_courses, "updated": datetime.now().isoformat()}, f, indent=2)
            log_event("bot_completed_courses_snapshot", count=len(b_courses), courses=b_courses)
            
            log.info("╭────────────────────────────────────────────────────────╮")
            log.info(f"│ 🤖 BOT COMPLETED COURSES ({len(b_courses):<28}) │")
            log.info("├────────────────────────────────────────────────────────┤")
            for i, c in enumerate(b_courses, 1):
                t = c.get("title") if isinstance(c, dict) else c
                log.info(f"│ {i}. {t}")
            log.info("╰────────────────────────────────────────────────────────╯")
        except Exception as e:
            log.error(f"Failed to update bot completed courses: {e}")

    return success, hours_gained


def rotate_logs_if_large():
    """Truncate logs to the last 500 lines if they exceed 150 KB."""
    for filepath in [LOG_FILE, EVENTS_FILE]:
        try:
            if os.path.exists(filepath) and os.path.getsize(filepath) > 150 * 1024:
                with open(filepath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                with open(filepath, "w", encoding="utf-8") as f:
                    f.writelines(lines[-500:])
                log.info(f"Rotated {os.path.basename(filepath)} (was > 150 KB, kept last 500 lines)")
        except Exception as e:
            log.warning(f"Failed to rotate {filepath}: {e}")


async def main():
    try:
        import telegram_notify
        telegram_notify.start()
    except Exception as e:
        log.warning("Telegram runtime skipped: %s", e)
    await _main_inner()


async def _main_inner():
    global RUN_STATE

    log.info("╭────────────────────────────────────────────────────────╮")
    log.info(f"│ TFC Bot v4  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<31} │")
    log.info("├────────────────────────────────────────────────────────┤")
    log.info("│ Startup: login → progress → profile → catalog → run    │")
    log.info("╰────────────────────────────────────────────────────────╯")

    log_event("bot_start", version=4)
    rotate_logs_if_large()

    hours_today_start = get_today_hours_from_log()
    log.info(f"📅 Hours logged today (events): {hours_today_start:.1f}h")

    async with async_playwright() as p:
        log.info("🌐 Launching browser...")
        browser = await p.chromium.launch(
            headless=not bool(os.getenv("HEADED")),
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu", "--blink-settings=imagesEnabled=true"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        log.info("🔐 Authenticating...")
        if not await ensure_auth(page):
            log.error("Auth failed. Exiting.")
            await page.close()
            await ctx.close()
            await browser.close()
            sys.exit(1)
        log.info("   ✓ Logged in")

        log.info("📊 Loading dashboard progress...")
        prog = await get_progress(page)
        log_event("progress_snapshot", **prog)
        log.info(f"   ✓ {prog['done']:.1f}h / {prog['total']:.0f}h ({prog['remaining']:.1f}h remaining)")

        log.info("📅 Checking daily hour limit...")
        daily = await get_daily_status(page)
        hours_today = daily["hours_today"]
        hours_remaining_today = daily["hours_remaining_today"]
        log.info(
            f"   ✓ Today {hours_today:.1f}h used, {hours_remaining_today:.1f}h left "
            f"(source: {daily['source']})"
        )

        log.info("👤 Loading user profile...")
        user_info = await get_user_profile(page)
        log.info("   ✓ Profile loaded")

        log.info("📚 Scraping coursework catalog...")
        lessons, _cta_url = await fetch_coursework_catalog(page)
        catalog_done = sum(1 for l in lessons if l.status == "done")
        log.info(f"   ✓ {len(lessons)} lessons ({catalog_done} done on site)")

        log_user_profile(user_info, prog, hours_today, catalog_done)
        log_catalog_summary(lessons)
        log_completion_breakdown(lessons)

        rs = RUN_STATE
        rs.hours_done = prog["done"]
        rs.hours_total = prog["total"]
        rs.hours_remaining = prog["remaining"]
        rs.hours_today = hours_today
        rs.catalog_done = catalog_done
        rs.queue_total = len(lessons)
        rs.user_name = (
            user_info.get("FULL NAME") or user_info.get("Full Name") or user_info.get("name") or ""
        ).strip()

        if check_daily_limit(hours_today, hours_remaining_today):
            rs.phase = "LIMIT_WAIT"
            rs.title = "Daily limit — waiting for midnight reset"
            await wait_for_daily_reset(page, rs)

        CAFFEINATE_MANAGER.start()

        if prog["remaining"] <= 0:
            log.info("🎉 All hours complete!")
            CAFFEINATE_MANAGER.stop()
            await page.close()
            await ctx.close()
            await browser.close()
            return

        session_done = 0
        processed_urls: set[str] = set()
        consecutive_errors = 0

        while True:
            daily = await get_daily_status(page)
            hours_today = daily["hours_today"]
            hours_remaining_today = daily["hours_remaining_today"]
            rs.hours_today = hours_today

            if check_daily_limit(hours_today, hours_remaining_today):
                await wait_for_daily_reset(page, rs)
                CAFFEINATE_MANAGER.start()
                continue

            lessons, cta_url = await fetch_coursework_catalog(page)
            if not lessons:
                consecutive_errors += 1
                log.error(f"Empty catalog — retry {consecutive_errors}/5 in 10s")
                if consecutive_errors >= 5:
                    log.error("Catalog failed 5 times — waiting 60s before retry...")
                    await page.wait_for_timeout(60000)
                    continue
                await page.wait_for_timeout(10000)
                continue

            consecutive_errors = 0

            rs.catalog_done = sum(1 for l in lessons if l.status == "done")
            queue = build_work_queue(lessons, cta_url, processed_urls)
            rs.queue_total = len(queue)

            if not queue:
                log.info("🎉 No incomplete lessons in catalog — all caught up!")
                break

            lesson = queue[0]
            rs.queue_pos = rs.catalog_done + session_done + 1
            rs.title = lesson.title
            rs.phase = "START"
            live_status("START", 0, lesson.title, rs.hours_done, hours_today, rs.hours_total, rs, force=True)

            log.info(f"\n{'─'*55}")
            log.info(
                f"Next: {lesson.title!r} [{lesson.status}]  "
                f"(site done: {rs.catalog_done}, session done: {session_done}, "
                f"queue: {len(queue)})"
            )
            log.info(f"URL: {lesson.url}")
            log_event("lesson_start", lesson_title=lesson.title, url=lesson.url,
                      status=lesson.status, hours_today=hours_today,
                      catalog_done=rs.catalog_done, session_done=session_done)

            try:
                success, hours_gained = await process_lesson(page, lesson, hours_today, rs)
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                log.error(f"Lesson error ({lesson.title!r}): {e}")
                log_event("lesson_error", lesson_title=lesson.title, url=lesson.url, error=str(e))
                if consecutive_errors >= 3:
                    log.error("3 lesson errors in a row — waiting 60s before retry...")
                    await page.wait_for_timeout(60000)
                    continue
                await page.wait_for_timeout(5000)
                continue

            processed_urls.add(lesson.url)
            if success:
                session_done += 1
                rs.session_done = session_done

            hours_today += hours_gained
            rs.hours_today = hours_today
            prog = await get_progress(page)
            rs.hours_done = prog["done"]

            log.info(
                f"📊 {prog['done']:.1f}h / {prog['total']:.0f}h  "
                f"(+{hours_gained:.2f}h this lesson  |  today: {hours_today:.1f}h  |  "
                f"session: {session_done} done)"
            )
            log_event("lesson_complete", lesson_title=lesson.title, url=lesson.url,
                      success=success, hours_gained=hours_gained, hours_done=prog["done"],
                      hours_today=hours_today, hours_remaining=prog["remaining"],
                      session_done=session_done, catalog_done=rs.catalog_done)
                      
            if prog["remaining"] <= 0:
                log.info("🎉 All hours complete!")
                log_event("all_complete", total_hours=prog["done"])
                break

        prog = await get_progress(page)
        log.info(f"\n{'='*55}")
        log.info(
            f"Run complete. {session_done} lessons this session. "
            f"{prog['done']:.1f}h / {prog['total']:.0f}h total."
        )
        log.info(f"Today's hours this session: {hours_today:.1f}h")
        log_event("bot_stop", lessons_this_run=session_done,
                  hours_done=prog["done"], hours_today=hours_today)
        set_terminal_title(f"TFC stopped · session {session_done} done · {prog['done']:.1f}/{prog['total']:.0f}h")
        CAFFEINATE_MANAGER.stop()
        await page.close()
        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    write_bot_pid()
    try:
        asyncio.run(main())
    finally:
        remove_bot_pid()
