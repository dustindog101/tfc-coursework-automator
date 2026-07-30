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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Awaitable, Callable, Optional

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
    env["PLAYWRIGHT_BROWSERS_PATH"] = ensure_playwright_browsers_path()
    if extra:
        env.update(extra)
    return env


def ensure_playwright_browsers_path() -> str:
    """Use a stable user cache — ignore broken Cursor sandbox browser paths."""
    default = os.path.expanduser("~/Library/Caches/ms-playwright")
    cur = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    cur_norm = cur.replace("\\", "/")
    if not cur or "cursor-sandbox-cache" in cur_norm or not os.path.isdir(cur):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = default
        return default
    return cur


def _chromium_executable_exists(playwright) -> bool:
    try:
        exe = playwright.chromium.executable_path
        return bool(exe and os.path.exists(exe))
    except Exception:
        return False


async def launch_browser(playwright, *, timeout_s: float = 90.0):
    """Launch Chromium with install fallback and a hard timeout."""
    ensure_playwright_browsers_path()
    if not _chromium_executable_exists(playwright):
        log.warning("Chromium missing — installing via: python3 -m playwright install chromium")
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                cwd=ROOT_DIR,
                env=subprocess_env(),
                timeout=300,
                check=False,
            )
        except Exception as e:
            log.error(f"playwright install failed: {e}")

    if not _chromium_executable_exists(playwright):
        raise RuntimeError(
            "Chromium not installed. Run: PLAYWRIGHT_BROWSERS_PATH=~/Library/Caches/ms-playwright "
            "python3 -m playwright install chromium"
        )

    log.info("🌐 Launching browser...")
    return await asyncio.wait_for(
        playwright.chromium.launch(
            headless=not bool(os.getenv("HEADED")),
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu", "--blink-settings=imagesEnabled=true"],
        ),
        timeout=timeout_s,
    )

EMAIL = os.getenv("TFC_EMAIL", "")
PASSWORD = os.getenv("TFC_PASSWORD", "")
BASE_URL = os.getenv("TFC_BASE_URL", "https://www.thefoundationofchange.org")
LOG_FILE = os.getenv("TFC_LOG_FILE", os.path.join(ROOT_DIR, "automation.log"))
EVENTS_FILE = os.getenv("TFC_EVENTS_FILE", os.path.join(ROOT_DIR, "events.jsonl"))
COMPLETED_COURSES_FILE = os.getenv("TFC_COMPLETED_COURSES_FILE", os.path.join(ROOT_DIR, "completed_courses.json"))
REFLECTION_DRAFTS_FILE = os.getenv(
    "TFC_REFLECTION_DRAFTS_FILE", os.path.join(ROOT_DIR, "reflection_drafts.json")
)

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
        "↕ scroll", "⏱ ",
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
    """Parse hours left today from coursework/dashboard — not overall course remaining."""
    if re.search(r"daily limit reached", body, re.IGNORECASE):
        return 0.0

    for pat in [
        r"TODAY'S LIMIT\s*\n\s*([\d.]+)\s*h",
        r"([\d.]+)\s*h\s*\n\s*remaining today",
        r"([\d.]+)\s*h\s+remaining today\b",
        r"remaining today[^\d]*([\d.]+)\s*h",
        r"([\d.]+)\s*h\s*\n\s*daily limit reached",
        r"today['']?s limit[^\d]*([\d.]+)\s*h",
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

LLM_ABORT = threading.Event()
_pending_llm_futures: list = []


def should_stop_work(rs: Optional[RunState] = None) -> bool:
    """True when daily limit wait is active — no lessons, timers, or LLM work."""
    state = rs or RUN_STATE
    return state.phase == "LIMIT_WAIT" or LLM_ABORT.is_set()


async def drain_llm_tasks() -> None:
    """Cancel pending background LLM executor jobs (e.g. orphaned reading drafts)."""
    global _pending_llm_futures
    for fut in list(_pending_llm_futures):
        try:
            fut.cancel()
        except Exception:
            pass
    _pending_llm_futures.clear()


async def enter_limit_wait(page, rs: RunState) -> None:
    """Halt all coursework work and rest until the site daily limit resets."""
    LLM_ABORT.set()
    await drain_llm_tasks()
    rs.phase = "LIMIT_WAIT"
    rs.title = "Daily limit — waiting for midnight reset"
    await wait_for_daily_reset(page, rs)
    LLM_ABORT.clear()


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
    "quota exceeded",
    "quota limit",
    "rate limit",
    "rate-limit",
    "too many requests",
    "resource exhausted",
    "exhausted quota",
    "429",
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_OPENCODE_ERROR_MARKERS = (
    "error:",
    "unknownerror",
    "failed to execute statement",
    "session not found",
    "unexpected server error",
)

AGY_QUOTA_COOLDOWN_S = int(os.getenv("AGY_QUOTA_COOLDOWN_S", str(2 * 3600)))
_AGY_QUOTA_UNTIL = 0.0
_LLM_LOCK = threading.Lock()

def _strip_em_dash(t: str) -> str:
    return t.replace("\u2014", ",").replace("\u2013", ",").replace("—", ",").replace("–", ",")


_DEFAULT_OPENCODE_MODELS = "opencode/deepseek-v4-flash-free,opencode/mimo-v2.5-free"
OPENCODE_VARIANT = os.getenv("OPENCODE_VARIANT", "minimal")
OPENCODE_USER_MSG = (
    "Reply with ONLY the reflection paragraph. "
    "No title, labels, bullet points, rules, or commentary about how you wrote it."
)

_LLM_META_MARKERS = (
    "output rules",
    "follow exactly",
    "write only the reflection",
    "reflection question:",
    "article title:",
    "article content:",
    "you are writing",
    "no preamble",
    "meta-commentary",
    "style check",
    "style checks",
    "missing apostrophes",
    "forbidden buzzwords",
    "informal phrasing",
    "lowercased start",
    "no em-dash",
    "no em dash",
    "answers the prompt directly",
    "reply format",
    "hard limit",
    "characters (hard",
    "guidelines",
    "do not include",
    "includes missing",
    "no forbidden",
)

_META_LINE_PREFIX = re.compile(
    r"^(style\s*checks?|reply\s*format|output\s*rules|voice|length)\s*:",
    re.IGNORECASE,
)


def build_reflection_system_prompt(
    article_title: str, article_body: str, prompt_text: str,
) -> str:
    """System/context prompt for opencode and agy — reflection text only in the reply."""
    body = (article_body or "")[:2500]
    return (
        "Write one short coursework reflection for a community service program.\n\n"
        "REPLY FORMAT (critical):\n"
        "- Output ONLY the reflection paragraph itself.\n"
        "- Do NOT repeat these instructions, the question, or article title.\n"
        "- Do NOT use labels like 'Reflection:', bullet lists, or style notes.\n"
        "- Do NOT explain your writing process.\n"
        "- Length: 80–295 characters (never exceed 295).\n"
        "- Voice: casual 19-year-old college student, first person, informal.\n"
        "- Light natural typos are ok (dont, im, cant). Lowercase sentence starts are fine.\n"
        "- No em dashes (— or –). No AI buzzwords (delve, tapestry, furthermore, crucial).\n"
        "- Answer the reflection question using ideas from the article.\n\n"
        f"Article title: {article_title}\n\n"
        f"Article content:\n{body}\n\n"
        f"Reflection question: {prompt_text}\n"
    )


def opencode_models() -> list[str]:
    """Comma-separated OPENCODE_MODEL — try each in order before agy fallback."""
    raw = os.getenv("OPENCODE_MODEL", _DEFAULT_OPENCODE_MODELS)
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models or ["opencode/mimo-v2.5-free"]


OPENCODE_MODEL = opencode_models()[0]


def _clean_llm_text(text: str) -> str:
    text = text.strip().strip('"\'')
    text = _strip_em_dash(text)
    text = re.sub(r'^```.*?\n', '', text, flags=re.MULTILINE).strip()
    return text


def _line_looks_like_meta(line: str) -> bool:
    low = line.lower().strip()
    if _META_LINE_PREFIX.match(low):
        return True
    return any(marker in low for marker in _LLM_META_MARKERS)


def _strip_meta_lines(text: str) -> str:
    """Drop instruction-echo lines; keep prose paragraphs."""
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _line_looks_like_meta(s):
            continue
        kept.append(s)
    return "\n".join(kept).strip()


def _llm_output_is_invalid(text: str) -> bool:
    """Reject instruction echoes, meta commentary, or too-short replies."""
    cleaned = _strip_meta_lines((text or "").strip())
    if len(cleaned) < REFLECTION_MIN:
        return True
    low = cleaned.lower()
    if any(marker in low for marker in _LLM_META_MARKERS):
        return True
    if cleaned.count("\n") >= 2 and ("- " in cleaned or "• " in cleaned):
        return True
    if low.startswith("reflection:") and len(cleaned) < REFLECTION_MIN + 12:
        return True
    return False


def _extract_prose(text: str) -> str:
    text = _strip_meta_lines(_clean_llm_text(text))
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    candidates = [
        l for l in reversed(lines)
        if len(l) >= REFLECTION_MIN
        and not l.startswith("{")
        and not l.startswith("timestamp")
        and not _line_looks_like_meta(l)
    ]
    prose = candidates[0] if candidates else text
    prose = re.sub(r"^(reflection|answer)\s*:\s*", "", prose, flags=re.IGNORECASE).strip()
    if _llm_output_is_invalid(prose):
        return ""
    return prose[:REFLECTION_MAX]


def _agy_hit_limit(result: subprocess.CompletedProcess) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in text for marker in _AGY_LIMIT_MARKERS)


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _opencode_has_error(result: subprocess.CompletedProcess) -> bool:
    """Treat non-zero exit or error text in output as failure."""
    if result.returncode != 0:
        return True
    text = _strip_ansi(f"{result.stdout or ''}\n{result.stderr or ''}").lower()
    return any(marker in text for marker in _OPENCODE_ERROR_MARKERS)


def _agy_in_cooldown() -> bool:
    return time.time() < _AGY_QUOTA_UNTIL


def _mark_agy_quota_hit() -> None:
    global _AGY_QUOTA_UNTIL
    _AGY_QUOTA_UNTIL = time.time() + AGY_QUOTA_COOLDOWN_S
    hours = AGY_QUOTA_COOLDOWN_S / 3600
    log.warning(
        f"agy quota/rate limit — skipping agy fallback for {hours:g}h"
    )


def _log_llm_draft(source: str, text: str) -> None:
    preview = text if len(text) <= 120 else text[:117] + "..."
    log.info(f"   ✍️  {source} draft ({len(text)} chars): {preview!r}")


def _build_opencode_cmd(system_prompt: str, model: str) -> tuple[list[str], Optional[str]]:
    """
    Build opencode argv. Always attach prompt via -f after the user message.
    Prompts often start with '-' (bullet rules) which breaks positional parsing.
    """
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(system_prompt)
    tmp.close()
    cmd = [
        "opencode", "run", "-m", model, "--auto",
    ]
    if OPENCODE_VARIANT:
        cmd.extend(["--variant", OPENCODE_VARIANT])
    cmd.extend([OPENCODE_USER_MSG, "-f", tmp.name])
    return cmd, tmp.name


def _run_llm_prompt(system_prompt: str) -> Optional[tuple[str, str]]:
    """
    Try opencode → agy → return None.
    Returns (reflection text, source) or None on all failures.
    Serialized — one LLM job at a time.
    """
    if LLM_ABORT.is_set():
        return None

    with _LLM_LOCK:
        if LLM_ABORT.is_set():
            return None

        for model in opencode_models():
            tmp_path = None
            try:
                cmd, tmp_path = _build_opencode_cmd(system_prompt, model)
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120, cwd=ROOT_DIR, env=subprocess_env(),
                )
                if _opencode_has_error(result):
                    err = _strip_ansi((result.stderr or result.stdout or ""))[:200]
                    log.warning(f"opencode/{model} failed: {err}")
                else:
                    prose = _extract_prose(_clean_llm_text(result.stdout))
                    if len(prose) >= REFLECTION_MIN:
                        _log_llm_draft(f"opencode/{model.split('/')[-1]}", prose)
                        return prose, "opencode"
                    if _llm_output_is_invalid(_clean_llm_text(result.stdout)):
                        log.warning(f"opencode/{model} returned meta/instruction text — skipping")
                    else:
                        log.warning(f"opencode/{model} output too short ({len(prose)} chars)")
            except subprocess.TimeoutExpired:
                log.warning(f"opencode/{model} timed out (120s)")
            except FileNotFoundError:
                log.warning("opencode not found in PATH — trying agy fallback")
                break
            except Exception as e:
                log.warning(f"opencode/{model} error: {e}")
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

        log.info("   opencode models exhausted — trying agy fallback")

        if _agy_in_cooldown():
            log.info("   agy on cooldown — skipping agy fallback")
            return None

        try:
            result = subprocess.run(
                ["agy", "-p", system_prompt, "--model", "Gemini 3.6 Flash (Low)"],
                capture_output=True, text=True, timeout=60, cwd=ROOT_DIR, env=subprocess_env(),
            )
            if result.returncode == 0:
                text = _extract_prose(_clean_llm_text(result.stdout))
                if len(text) >= REFLECTION_MIN:
                    _log_llm_draft("agy", text)
                    return text, "agy"
                log.warning(f"agy output too short ({len(text)} chars): {text!r}")
            elif _agy_hit_limit(result):
                _mark_agy_quota_hit()
            else:
                log.warning(f"agy exit {result.returncode}: {(result.stderr or result.stdout)[:300]}")
        except subprocess.TimeoutExpired:
            log.warning("agy timed out (60s)")
        except FileNotFoundError:
            log.warning("agy not found in PATH")
        except Exception as e:
            log.warning(f"agy error: {e}")

        return None


def call_agy(article_title: str, article_body: str, prompt_text: str) -> tuple[str, str]:
    """
    Generate a reflection via opencode → agy → hardcoded fallback chain.
    Returns (reflection_text, source) where source is opencode|agy|fallback.
    """
    if LLM_ABORT.is_set():
        r = random.choice(_FALLBACKS)
        return r, "fallback"

    system_prompt = build_reflection_system_prompt(article_title, article_body, prompt_text)

    result = _run_llm_prompt(system_prompt)
    if result:
        text, source = result
        if len(text) > REFLECTION_MAX:
            cut = text.rfind(".", REFLECTION_MIN, REFLECTION_MAX)
            text = text[:cut + 1] if cut > REFLECTION_MIN else text[:REFLECTION_MAX]
        return text, source

    r = random.choice(_FALLBACKS)
    log.warning(f"LLM unavailable — using hardcoded placeholder ({len(r)} chars)")
    return r, "fallback"


_last_reflection_logged: tuple[str, str, str] = ("", "", "")


def _draft_key(lesson_url: str) -> str:
    return lesson_url.rstrip("/").replace("/reflect", "")


def load_reflection_draft(lesson_url: str) -> Optional[dict]:
    """Return a saved reflection draft for this lesson URL, if any."""
    key = _draft_key(lesson_url)
    try:
        with open(REFLECTION_DRAFTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        draft = data.get("drafts", {}).get(key)
        return draft if isinstance(draft, dict) and draft.get("reflection") else None
    except Exception:
        return None


def save_reflection_draft(
    lesson_url: str,
    *,
    lesson_title: str,
    article_title: str,
    reflection: str,
    source: str,
    lesson_prompt: str = "",
) -> None:
    """Persist reflection until submitted."""
    text = (reflection or "").strip()
    if len(text) < REFLECTION_MIN:
        return
    key = _draft_key(lesson_url)
    try:
        data: dict = {"drafts": {}}
        if os.path.exists(REFLECTION_DRAFTS_FILE):
            with open(REFLECTION_DRAFTS_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        drafts = data.setdefault("drafts", {})
        drafts[key] = {
            "lesson_url": key,
            "lesson_title": lesson_title,
            "article_title": article_title,
            "reflection": text,
            "source": source,
            "lesson_prompt": lesson_prompt,
            "saved_at": datetime.now().isoformat(),
        }
        with open(REFLECTION_DRAFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save reflection draft: {e}")


def clear_reflection_draft(lesson_url: str) -> None:
    key = _draft_key(lesson_url)
    try:
        if not os.path.exists(REFLECTION_DRAFTS_FILE):
            return
        with open(REFLECTION_DRAFTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        drafts = data.get("drafts", {})
        if key not in drafts:
            return
        del drafts[key]
        with open(REFLECTION_DRAFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"Could not clear reflection draft: {e}")


def _apply_saved_reflection_draft(
    lesson_url: str, lesson_title: str, article_title: str,
) -> Optional[tuple[str, str]]:
    """Load a disk draft and log that it was restored."""
    saved = load_reflection_draft(lesson_url)
    if not saved:
        return None
    reflection = str(saved.get("reflection", "")).strip()
    if len(reflection) < REFLECTION_MIN:
        return None
    source = str(saved.get("source") or "saved")
    log.info(
        f"   ✍️ Loaded saved reflection from disk ({source}, {len(reflection)} chars)"
    )
    log_reflection_generated(
        lesson_title,
        saved.get("article_title") or article_title,
        reflection,
        source,
        draft_origin="loaded",
    )
    return reflection, source


def _persist_reflection_draft(
    lesson: "LessonEntry",
    article_title: str,
    reflection: str,
    source: str,
    lesson_prompt: str = "",
    *,
    draft_origin: str = "generated",
) -> None:
    """Save draft to disk and log whether it was freshly generated."""
    text = (reflection or "").strip()
    if len(text) < REFLECTION_MIN:
        return
    save_reflection_draft(
        lesson.url,
        lesson_title=lesson.title,
        article_title=article_title,
        reflection=text,
        source=source,
        lesson_prompt=lesson_prompt,
    )
    if draft_origin == "generated":
        log.info(f"   ✍️ Generated reflection ({source}, {len(text)} chars) — saved to disk")
        if source in ("agy", "opencode", "fallback"):
            log_reflection_generated(
                lesson.title, article_title, text, source, draft_origin="generated",
            )


async def _reading_llm_with_persist(
    lesson: "LessonEntry",
    article_title: str,
    title: str,
    body: str,
    lesson_prompt: str,
) -> tuple[str, str]:
    """Run LLM during reading and persist as soon as it finishes (even mid-timer)."""
    loop = asyncio.get_event_loop()
    exec_fut = loop.run_in_executor(None, call_agy, title, body, lesson_prompt)
    _pending_llm_futures.append(exec_fut)
    try:
        reflection, reflection_source = await exec_fut
    finally:
        if exec_fut in _pending_llm_futures:
            _pending_llm_futures.remove(exec_fut)
    _persist_reflection_draft(
        lesson, article_title, reflection, reflection_source, lesson_prompt,
        draft_origin="generated",
    )
    return reflection, reflection_source


def log_reflection_generated(
    lesson_title: str, article_title: str, reflection: str, source: str,
    *, draft_origin: str = "generated",
) -> None:
    """Write reflection to events.jsonl so menubar/Telegram can display it."""
    global _last_reflection_logged
    key = (article_title, reflection, source, draft_origin)
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
        draft_origin=draft_origin,
    )


def needs_llm_recheck(reflection: str, source: str) -> bool:
    """Only retry LLM when we lack a real agy/opencode draft."""
    return not (reflection and reflection.strip()) or source in ("", "fallback")


def try_upgrade_reflection(
    art_title: str, art_body: str, lesson_prompt: str,
    current: str, current_source: str,
) -> tuple[str, str]:
    """One pre-submit LLM attempt when reading only produced a placeholder."""
    if LLM_ABORT.is_set():
        return current or random.choice(_FALLBACKS), current_source or "fallback"
    log.info("   Pre-submit LLM upgrade...")
    new_text, new_source = call_agy(art_title, art_body, lesson_prompt)
    if new_source in ("agy", "opencode"):
        log.info(f"   Pre-submit upgrade: {new_source} ({len(new_text)} chars)")
        return new_text, new_source
    log.info("   Pre-submit upgrade failed — keeping placeholder draft")
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


COURSE_HOUR_TOTAL = 75.0
TYPICAL_LESSON_HOURS = 1.1
MAX_LESSON_HOURS = 2.0

_PROGRESS_JS = """() => {
  const DEFAULT_TOTAL = 75;
  const text = document.body.innerText || '';
  const candidates = [];
  const fracRe = /(\\d+(?:\\.\\d+)?)\\s*\\/\\s*(\\d+(?:\\.\\d+)?)\\s*h(?:ours?)?/gi;
  let m;
  while ((m = fracRe.exec(text)) !== null) {
    const done = parseFloat(m[1]), total = parseFloat(m[2]);
    if (total >= 20) candidates.push({done, total, w: Math.abs(total - DEFAULT_TOTAL)});
  }
  const remRe = /(\\d+(?:\\.\\d+)?)\\s*h(?:ours?)?\\s+remaining(?!\\s+today)/gi;
  while ((m = remRe.exec(text)) !== null) {
    const rem = parseFloat(m[1]);
    if (rem > 0 && rem < DEFAULT_TOTAL) {
      candidates.push({done: DEFAULT_TOTAL - rem, total: DEFAULT_TOTAL, w: 1});
    }
  }
  if (!candidates.length) return null;
  candidates.sort((a, b) => a.w - b.w || Math.abs(a.total - DEFAULT_TOTAL) - Math.abs(b.total - DEFAULT_TOTAL));
  const best = candidates[0];
  return {done: best.done, total: best.total, remaining: best.total - best.done};
}"""


def parse_progress_from_body(body: str, default_total: float = COURSE_HOUR_TOTAL) -> dict:
    """Extract overall course progress; ignore daily limits like 6.1/8 hours."""
    candidates: list[tuple[float, float, int]] = []

    for m in re.finditer(
        r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*h(?:ours?)?",
        body,
        re.I,
    ):
        done, total = float(m.group(1)), float(m.group(2))
        if total >= 20:
            candidates.append((done, total, int(abs(total - default_total) * 10)))

    for m in re.finditer(
        r"(?:Overall\s+)?Progress[^\d]{0,60}(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)",
        body,
        re.I | re.S,
    ):
        done, total = float(m.group(1)), float(m.group(2))
        if total >= 20:
            candidates.append((done, total, 0))

    for m in re.finditer(
        r"(\d+(?:\.\d+)?)\s*h(?:ours?)?\s+remaining(?!\s+today)",
        body,
        re.I,
    ):
        remaining = float(m.group(1))
        if 0 < remaining < default_total:
            candidates.append((default_total - remaining, default_total, 1))

    if not candidates:
        return {"done": 0.0, "total": default_total, "remaining": default_total}

    done, total, _ = min(candidates, key=lambda c: (c[2], abs(c[1] - default_total)))
    return {"done": done, "total": total, "remaining": max(0.0, total - done)}


async def get_progress(page, *, navigate: bool = True) -> dict:
    """Read overall hours from dashboard/coursework — never from lesson/reflect pages."""
    default = {"done": 0.0, "total": COURSE_HOUR_TOTAL, "remaining": COURSE_HOUR_TOTAL}
    urls = [f"{BASE_URL}/dashboard", f"{BASE_URL}/coursework"] if navigate else [None]

    for url in urls:
        try:
            if url and not await safe_goto(page, url):
                continue
            if url:
                await page.wait_for_timeout(2000)
            js_prog = await page.evaluate(_PROGRESS_JS)
            if js_prog and float(js_prog.get("done", 0)) > 0:
                return {
                    "done": float(js_prog["done"]),
                    "total": float(js_prog.get("total", COURSE_HOUR_TOTAL)),
                    "remaining": float(js_prog.get("remaining", 0)),
                }
            body = await page.inner_text("body")
            parsed = parse_progress_from_body(body)
            if parsed["done"] > 0:
                return parsed
        except Exception:
            continue
    return default


async def measure_lesson_hours(page, prog_before: dict) -> tuple[dict, float]:
    """Scrape dashboard after a lesson; correct inflated deltas from wrong-page reads."""
    prog_after = await get_progress(page, navigate=True)
    hours_gained = max(0.0, prog_after["done"] - prog_before["done"])

    if hours_gained > MAX_LESSON_HOURS:
        log.warning(f"   Suspicious hours_gained {hours_gained:.2f} — re-scraping dashboard...")
        await page.wait_for_timeout(4000)
        prog_after = await get_progress(page, navigate=True)
        hours_gained = max(0.0, prog_after["done"] - prog_before["done"])

    if hours_gained > MAX_LESSON_HOURS:
        rem_before = prog_before.get("remaining")
        rem_after = prog_after.get("remaining")
        if rem_before is not None and rem_after is not None:
            gain_rem = float(rem_before) - float(rem_after)
            if 0 < gain_rem <= MAX_LESSON_HOURS:
                hours_gained = gain_rem
                prog_after = dict(prog_after)
                prog_after["done"] = prog_before["done"] + hours_gained
                prog_after["remaining"] = max(0.0, prog_after["total"] - prog_after["done"])
                log.warning(f"   Corrected via hours-remaining delta: +{hours_gained:.2f}h")

    if hours_gained > MAX_LESSON_HOURS:
        hours_gained = TYPICAL_LESSON_HOURS
        prog_after = dict(prog_before)
        prog_after["done"] = prog_before["done"] + hours_gained
        prog_after["remaining"] = max(0.0, prog_after["total"] - prog_after["done"])
        log.warning(
            f"   Capped lesson credit to typical +{hours_gained:.2f}h "
            f"(before {prog_before['done']:.1f}h → after {prog_after['done']:.1f}h)"
        )

    return prog_after, hours_gained


async def extract_article(page) -> tuple[str, str]:
    """Extract (title, body) from the article reading page only."""
    title = "Community Service Article"
    body = ""
    try:
        if "/reflect" in page.url:
            base_url = page.url.split("/reflect")[0]
            if base_url:
                log.info("   extract_article: on /reflect — navigating to article page")
                if await safe_goto(page, base_url):
                    await page.wait_for_timeout(1000)
                else:
                    log.info("   extract_article: could not leave /reflect — skipping body")
                    return title, body

        for sel in ["h1", "h2", ".article-title", ".lesson-title", ".course-title"]:
            el = page.locator(sel).first
            if await el.count() > 0:
                t = (await el.inner_text()).strip()
                if 5 < len(t) < 200 and "Foundation" not in t and "Log" not in t:
                    title = t
                    break

        if "/reflect" in page.url:
            log.info("   extract_article: still on /reflect — skipping body extraction")
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


async def fetch_coursework_catalog(page, *, log_completed_list: bool = False) -> tuple[list[LessonEntry], Optional[str]]:
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
        if log_completed_list:
            clear_live_status()
            log.info("╭────────────────────────────────────────────────────────╮")
            log.info(f"│ 🎓 COMPLETED COURSES LIST ({len(merged_titles):<27}) │")
            log.info("├────────────────────────────────────────────────────────┤")
            for i, title in enumerate(merged_titles, 1):
                log.info(f"│ {i:2d}. {title}")
            log.info("╰────────────────────────────────────────────────────────╯")
        else:
            log.debug(f"completed_courses.json updated ({len(merged_titles)} titles)")
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
        if "/reflect" in page.url:
            body = await page.inner_text("body")
            if any(kw in body for kw in ["Reflection Submitted", "Next Article", "Great work"]):
                log.info(f"   ✓ Reflection already submitted: {lesson.title!r}")
                return "complete"
            if await page_has_reflect_form(page):
                log.info(f"   → Needs reflect (redirected to /reflect): {lesson.title!r}")
                return "needs_reflect"
            secs = await get_timer(page)
            if secs > 0:
                log.info(f"   → Needs reflect (timer on /reflect {secs//60}:{secs%60:02d}): {lesson.title!r}")
                return "needs_reflect"
        else:
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


async def _handle_telegram_reflection_actions(
    lesson: "LessonEntry",
    art_title: str,
    art_body: str,
    lesson_prompt: str,
    reflection: str,
    reflection_source: str,
    *,
    page=None,
    fill_form: bool = False,
    handler: Optional["_TelegramActionHandler"] = None,
) -> tuple[str, str]:
    """Apply Telegram regenerate/custom actions. Never blocks on LLM — bot keeps going."""
    if handler is None:
        handler = _TelegramActionHandler()
    return await handler.poll(
        lesson, art_title, art_body, lesson_prompt,
        reflection, reflection_source,
        page=page, fill_form=fill_form,
    )


class _TelegramActionHandler:
    """Non-blocking Telegram actions — regenerate runs in a background executor."""

    def __init__(self) -> None:
        self._regen_future: Optional[asyncio.Future] = None

    async def poll(
        self,
        lesson: "LessonEntry",
        art_title: str,
        art_body: str,
        lesson_prompt: str,
        reflection: str,
        reflection_source: str,
        *,
        page=None,
        fill_form: bool = False,
    ) -> tuple[str, str]:
        try:
            import telegram_notify as tg
            actions = tg.drain_actions_for_lesson(lesson.url)
        except Exception as e:
            log.warning(f"Telegram actions skipped: {e}")
            return reflection, reflection_source

        for action in actions:
            atype = action.get("type")
            if atype == "regenerate":
                if self._regen_future is None or self._regen_future.done():
                    log.info("   📱 Telegram: regenerating reflection (background)…")
                    try:
                        import telegram_notify as tg
                        tg.set_card_overlay("🔄 Queued — new AI draft incoming (usually within 60s).")
                    except Exception:
                        pass
                    loop = asyncio.get_running_loop()
                    self._regen_future = loop.run_in_executor(
                        None, call_agy, art_title, art_body, lesson_prompt,
                    )
            elif atype == "custom":
                text = str(action.get("text", "")).strip()
                if len(text) >= REFLECTION_MIN:
                    reflection = text[:REFLECTION_MAX]
                    reflection_source = "telegram"
                    _persist_reflection_draft(
                        lesson, art_title, reflection, reflection_source, lesson_prompt,
                        draft_origin="loaded",
                    )
                    log.info(f"   📱 Using your Telegram reflection ({len(reflection)} chars)")
                    log_reflection_generated(
                        lesson.title, art_title, reflection, reflection_source,
                        draft_origin="loaded",
                    )
                    try:
                        import telegram_notify as tg
                        tg.set_card_overlay(f"✅ Using your reflection ({len(reflection)} chars)")
                    except Exception:
                        pass

        if self._regen_future is not None and self._regen_future.done():
            try:
                new_r, new_s = self._regen_future.result()
                if new_r and len(new_r.strip()) >= REFLECTION_MIN:
                    reflection, reflection_source = new_r, new_s
                    _persist_reflection_draft(
                        lesson, art_title, reflection, reflection_source, lesson_prompt,
                        draft_origin="generated",
                    )
                    log_reflection_generated(
                        lesson.title, art_title, reflection, reflection_source,
                        draft_origin="generated",
                    )
                    log.info(f"   📱 New draft ({reflection_source}, {len(reflection)} chars)")
                else:
                    log.warning("   📱 Regenerate produced no usable draft — kept previous")
                    try:
                        import telegram_notify as tg
                        tg.set_card_overlay("⚠️ Regenerate failed — kept previous draft")
                    except Exception:
                        pass
            except Exception as e:
                log.warning(f"   📱 Regenerate error: {e}")
                try:
                    import telegram_notify as tg
                    tg.set_card_overlay("⚠️ Regenerate failed — kept previous draft")
                except Exception:
                    pass
            self._regen_future = None

        if fill_form and page is not None and reflection:
            try:
                await fill_reflection_textarea(page, reflection)
            except Exception as e:
                log.warning(f"Telegram form fill skipped: {e}")
        return reflection, reflection_source


def _telegram_set_lesson_context(
    lesson: "LessonEntry", article_title: str, article_body: str, lesson_prompt: str = "",
) -> None:
    try:
        import telegram_notify as tg
        tg.set_lesson_context(
            lesson_url=lesson.url,
            lesson_title=lesson.title,
            article_title=article_title,
            article_body=article_body,
            lesson_prompt=lesson_prompt,
        )
    except Exception:
        pass


# ── Lesson phases ─────────────────────────────────────────────────────────────
async def wait_for_timer(
    page, phase: str, lesson_title: str,
    hours_done: float, hours_today: float, hours_total: float,
    rs: Optional[RunState] = None,
    on_interval: Optional[Callable[[], Awaitable[None]]] = None,
):
    """Local wall-clock countdown; resync from page every TIMER_RESYNC_S with scroll."""
    local = LocalTimer()
    elapsed = 0
    since_resync = TIMER_RESYNC_S  # trigger initial DOM read immediately
    last_log_min = -1

    while True:
        if should_stop_work(rs):
            log.info(f"[{phase}] stopping — daily limit reached")
            clear_live_status()
            return

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
        if on_interval:
            try:
                await on_interval()
            except Exception as e:
                log.warning(f"Timer hook skipped: {e}")


async def reading_phase(
    page, lesson: LessonEntry, hours_done: float, hours_today: float,
    hours_total: float, rs: Optional[RunState] = None,
) -> tuple[str, str, str, str, str]:
    """Returns (article_title, article_body, pre_reflection, lesson_prompt, reflection_source)."""
    if should_stop_work(rs):
        raise RuntimeError("Daily limit reached — skipping reading")

    await drain_llm_tasks()

    log.info(f"📖 Reading: {lesson.title!r} — {lesson.url}")
    log_event("reading_start", lesson_url=lesson.url, lesson_title=lesson.title,
              hours_today=hours_today, hours_done=hours_done)

    if not await safe_goto(page, lesson.url):
        raise RuntimeError(f"Could not open reading page for {lesson.title!r}")

    title, body = await extract_article(page)
    log.info(f"   Article: {title!r}")
    if not body:
        try:
            import telegram_notify as tg
            ctx = tg.get_lesson_context()
            key = lesson.url.rstrip("/").replace("/reflect", "")
            if ctx.get("lesson_url", "").rstrip("/").replace("/reflect", "") == key:
                saved_body = str(ctx.get("article_body") or "").strip()
                if len(saved_body) > 80:
                    body = saved_body
                    log.info(f"   Article body restored from Telegram context ({len(body)} chars)")
        except Exception:
            pass
    if not body:
        body = (
            f"This article covered important topics related to {title}. "
            "It discussed community impact, personal responsibility, and evidence-based approaches."
        )
    lesson_prompt = default_reflect_prompt(title)
    _telegram_set_lesson_context(lesson, title, body, lesson_prompt)

    saved = _apply_saved_reflection_draft(lesson.url, lesson.title, title)
    llm_task: Optional[asyncio.Task] = None
    reflection = ""
    reflection_source = ""
    if saved:
        reflection, reflection_source = saved
    else:
        llm_task = asyncio.create_task(
            _reading_llm_with_persist(lesson, title, title, body, lesson_prompt)
        )

    tg_handler = _TelegramActionHandler()

    async def _reading_telegram_hook() -> None:
        nonlocal reflection, reflection_source
        r, s = reflection, reflection_source
        if llm_task:
            if llm_task.done() and not llm_task.cancelled():
                try:
                    r, s = llm_task.result()
                except Exception:
                    pass
        reflection, reflection_source = await _handle_telegram_reflection_actions(
            lesson, title, body, lesson_prompt, r, s,
            handler=tg_handler,
        )

    secs = await get_timer(page)
    if secs > 0:
        log.info(f"   Reading timer: {secs//60}:{secs%60:02d}")
        await wait_for_timer(
            page, "READ", title, hours_done, hours_today, hours_total, rs,
            on_interval=_reading_telegram_hook,
        )
    else:
        log.info("   No reading timer")

    if should_stop_work(rs):
        if llm_task:
            llm_task.cancel()
        raise RuntimeError("Daily limit reached — skipping reading draft")

    if llm_task:
        reflection, reflection_source = await llm_task
        if reflection_source not in ("agy", "opencode") and reflection:
            log.info("   Reading-phase LLM pending — will upgrade before submit")
    return title, body, reflection, lesson_prompt, reflection_source


async def reflect_phase(
    page, lesson: LessonEntry, art_title: str, art_body: str, pre_reflection: str,
    hours_done: float, hours_today: float, hours_total: float,
    pre_source: str = "",
    rs: Optional[RunState] = None,
) -> bool:
    if should_stop_work(rs):
        log.info("   Skipping reflect — daily limit reached")
        return False

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
    _telegram_set_lesson_context(lesson, art_title, art_body, lesson_prompt)

    reflection = pre_reflection
    reflection_source = pre_source
    if not reflection or not reflection.strip():
        saved = _apply_saved_reflection_draft(lesson.url, lesson.title, art_title)
        if saved:
            reflection, reflection_source = saved

    if reflection and reflection_source not in ("", "fallback"):
        if pre_reflection and pre_reflection == reflection:
            log.info(f"   Using pre-generated reflection from {reflection_source} ({len(reflection)} chars)")
        # loaded drafts already logged in _apply_saved_reflection_draft
    elif reflection and reflection_source == "fallback":
        log.info("   Placeholder draft from reading — will try LLM before submit")
    else:
        log.info("   Calling LLM for reflection...")
        reflection, reflection_source = call_agy(art_title, art_body, lesson_prompt)
        _persist_reflection_draft(
            lesson, art_title, reflection, reflection_source, lesson_prompt,
            draft_origin="generated",
        )

    if reflection_source in ("agy", "opencode") and not (
        pre_reflection and pre_reflection == reflection and pre_source == reflection_source
    ):
        log_reflection_generated(
            lesson.title, art_title, reflection, reflection_source, draft_origin="generated",
        )

    filled_len = await fill_reflection_textarea(page, reflection)
    save_reflection_draft(
        lesson.url,
        lesson_title=lesson.title,
        article_title=art_title,
        reflection=reflection,
        source=reflection_source,
        lesson_prompt=lesson_prompt,
    )

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

        tg_reflect_handler = _TelegramActionHandler()

        async def _reflect_telegram_hook() -> None:
            nonlocal reflection, reflection_source, filled_len
            reflection, reflection_source = await _handle_telegram_reflection_actions(
                lesson, art_title, art_body, lesson_prompt,
                reflection, reflection_source,
                page=page, fill_form=True,
                handler=tg_reflect_handler,
            )

        await wait_for_timer(
            page, "REFLECT", art_title, hours_done, hours_today, hours_total, rs,
            on_interval=_reflect_telegram_hook,
        )

    if needs_llm_recheck(reflection, reflection_source):
        if should_stop_work(rs):
            log.info("   Pre-submit skip — daily limit reached")
        else:
            loop = asyncio.get_event_loop()
            upgrade_task = loop.run_in_executor(
                None, try_upgrade_reflection,
                art_title, art_body, lesson_prompt, reflection, reflection_source,
            )
            _pending_llm_futures.append(upgrade_task)
            upgraded, upgraded_source = await upgrade_task
            if upgrade_task in _pending_llm_futures:
                _pending_llm_futures.remove(upgrade_task)
            if upgraded_source in ("agy", "opencode"):
                reflection, reflection_source = upgraded, upgraded_source
                _persist_reflection_draft(
                    lesson, art_title, reflection, reflection_source, lesson_prompt,
                    draft_origin="generated",
                )
                log_reflection_generated(
                    lesson.title, art_title, reflection, reflection_source,
                    draft_origin="generated",
                )
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
        clear_reflection_draft(lesson.url)
        try:
            import telegram_notify as tg
            tg.clear_lesson_context()
        except Exception:
            pass
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
        saved = load_reflection_draft(lesson.url)
        if saved:
            reflection = str(saved.get("reflection", ""))
            reflection_source = str(saved.get("source") or "")
            if reflection:
                log.info(
                    f"   ✍️ Restored saved reflection for reflect phase "
                    f"({reflection_source}, {len(reflection)} chars)"
                )
                log_reflection_generated(
                    lesson.title,
                    saved.get("article_title") or art_title,
                    reflection,
                    reflection_source,
                    draft_origin="loaded",
                )
        log.info(f"   Reading already complete — going to reflect for {art_title!r}")


    prog_before = await get_progress(page, navigate=True)
    success = await reflect_phase(
        page, lesson, art_title, art_body, reflection,
        prog_before["done"], hours_today, prog_before["total"],
        pre_source=reflection_source, rs=rs,
    )

    prog_after, hours_gained = await measure_lesson_hours(page, prog_before)

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
            log.info(f"   ✓ Bot completed: {lesson.title!r} ({len(b_courses)} total)")
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

    ensure_playwright_browsers_path()

    async with async_playwright() as p:
        try:
            browser = await launch_browser(p)
        except asyncio.TimeoutError:
            log.error("Browser launch timed out after 90s — check Playwright install")
            sys.exit(1)
        except RuntimeError as e:
            log.error(str(e))
            sys.exit(1)
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

        log.info("📅 Checking daily hour limit...")
        daily = await get_daily_status(page)
        hours_today = daily["hours_today"]
        hours_remaining_today = daily["hours_remaining_today"]
        log.info(
            f"   ✓ Today {hours_today:.1f}h used, {hours_remaining_today:.1f}h left "
            f"(source: {daily['source']})"
        )

        rs = RUN_STATE
        rs.hours_today = hours_today
        at_limit = check_daily_limit(hours_today, hours_remaining_today)

        if at_limit:
            log.info("📊 Quick progress read (limit reached — skipping profile & catalog)...")
            prog = await get_progress(page)
            log_event("progress_snapshot", **prog)
            log.info(f"   ✓ {prog['done']:.1f}h / {prog['total']:.0f}h ({prog['remaining']:.1f}h remaining)")
            rs.hours_done = prog["done"]
            rs.hours_total = prog["total"]
            rs.hours_remaining = prog["remaining"]
            await enter_limit_wait(page, rs)
        else:
            log.info("📊 Loading dashboard progress...")
            prog = await get_progress(page)
            log_event("progress_snapshot", **prog)
            log.info(f"   ✓ {prog['done']:.1f}h / {prog['total']:.0f}h ({prog['remaining']:.1f}h remaining)")

            log.info("👤 Loading user profile...")
            user_info = await get_user_profile(page)
            log.info("   ✓ Profile loaded")

            log.info("📚 Scraping coursework catalog...")
            lessons, _cta_url = await fetch_coursework_catalog(page, log_completed_list=True)
            catalog_done = sum(1 for l in lessons if l.status == "done")
            log.info(f"   ✓ {len(lessons)} lessons ({catalog_done} done on site)")

            log_user_profile(user_info, prog, hours_today, catalog_done)
            log_catalog_summary(lessons)
            log_completion_breakdown(lessons)

            rs.hours_done = prog["done"]
            rs.hours_total = prog["total"]
            rs.hours_remaining = prog["remaining"]
            rs.catalog_done = catalog_done
            rs.queue_total = len(lessons)
            rs.user_name = (
                user_info.get("FULL NAME") or user_info.get("Full Name") or user_info.get("name") or ""
            ).strip()

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
                await enter_limit_wait(page, rs)
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
