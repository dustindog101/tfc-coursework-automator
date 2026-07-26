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
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
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

EMAIL = os.getenv("TFC_EMAIL", "")
PASSWORD = os.getenv("TFC_PASSWORD", "")
BASE_URL = os.getenv("TFC_BASE_URL", "https://www.thefoundationofchange.org")
LOG_FILE = os.getenv("TFC_LOG_FILE", os.path.join(ROOT_DIR, "automation.log"))
EVENTS_FILE = os.getenv("TFC_EVENTS_FILE", os.path.join(ROOT_DIR, "events.jsonl"))
COMPLETED_COURSES_FILE = os.getenv("TFC_COMPLETED_COURSES_FILE", os.path.join(ROOT_DIR, "completed_courses.json"))

SCROLL_INTERVAL_S = 165   # ~2.75 min
POLL_INTERVAL_S   = 25    # timer poll frequency
REFLECTION_MIN    = 80
REFLECTION_MAX    = 295
DAILY_HOUR_LIMIT  = float(os.getenv("TFC_DAILY_HOUR_LIMIT", "8.0"))
MIN_HOURS_LEFT    = float(os.getenv("TFC_MIN_HOURS_LEFT", "0.35"))  # don't start if less left today

# ── Logging (text) ────────────────────────────────────────────────────────────
class TerminalLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            sys.stderr.write("\033[2K\r")
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        TerminalLogHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("tfc")


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
            self.proc = subprocess.Popen(["caffeinate", "-i", "-s"])
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


RUN_STATE = RunState()


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
    remaining_today = max(0, DAILY_HOUR_LIMIT - rs.hours_today)
    timer_str = f"{rs.timer_secs//60}:{rs.timer_secs%60:02d}" if rs.timer_secs > 0 else "00:00"
    title = rs.title[:42] if rs.title else "…"
    pos = f"#{rs.queue_pos}/{rs.queue_total}" if rs.queue_total else "#?"
    done_str = f"done:{rs.catalog_done}+{rs.session_done}"
    
    C_RESET = "\033[0m"
    C_PHASE = "\033[1;36m" if "WAIT" not in rs.phase else "\033[1;35m"
    C_TIMER = "\033[1;33m"
    C_STATS = "\033[1;97m"
    C_GREEN = "\033[1;32m"
    
    phase_icons = {
        "READ": "📖 READ",
        "REFLECT": "✍️ REFLECT",
        "LIMIT_WAIT": "🌙 LIMIT_WAIT",
        "START": "🚀 START",
    }
    phase_str = phase_icons.get(rs.phase, rs.phase)

    prog_pct = int((rs.hours_done / rs.hours_total) * 100) if rs.hours_total > 0 else 0
    bar = make_progress_bar(rs.hours_done, rs.hours_total)
    
    return (
        f"TFC {done_str:<12} │ {pos:<7} │ "
        f"{C_PHASE}{phase_str:<15}{C_RESET} │ "
        f"{C_TIMER}{timer_str:<5}{C_RESET} │ "
        f"{title:<42} │ "
        f"{C_STATS}today {rs.hours_today:.1f}/{DAILY_HOUR_LIMIT:.0f}h{C_RESET} │ "
        f"{C_GREEN}[{bar}] {prog_pct:3}%{C_RESET} "
        f"({rs.hours_done:.1f}/{rs.hours_total:.0f}h) │ "
        f"left {remaining_today:.1f}h"
    )


def live_status(phase: str, timer_secs: int, lesson_title: str,
                hours_done: float, hours_today: float, hours_total: float,
                rs: Optional[RunState] = None):
    """Print live status to stderr and set terminal window title."""
    state = rs or RUN_STATE
    state.phase = phase
    state.timer_secs = timer_secs
    state.title = lesson_title
    state.hours_done = hours_done
    state.hours_today = hours_today
    state.hours_total = hours_total
    line = format_status_line(state)
    sys.stderr.write("\033[2K\r" + line)
    sys.stderr.flush()
    clean_line = re.sub(r'\033\[[0-9;]*m', '', line)
    set_terminal_title(clean_line)


# ── agy reflection via CLI pipe ───────────────────────────────────────────────
_FALLBACKS = [
    "this was actually really interesting. the info about how the brain reacts to substances made me think differently about addiction. didnt realize how much it impacts the whole body not just the mind.",
    "i think the biggest thing i got from this is that people dealing with addiction arent just making bad choices, theres actual science behind why its so hard to stop. makes me more understanding.",
    "the article brought up some stuff i hadnt considered before. like the idea that community and support really do make a difference in recovery. that resonated with me more than i expected honestly.",
    "it was good to learn about the research behind this topic. i feel like i understand now why certain approaches to treatment work better. gave me a new perspective on things.",
    "this lesson was helpful in understanding that change is a process not just a decision. the examples made it clear that real growth takes time and that setbacks are part of it.",
    "what stood out most was how the article connected individual choices to bigger community impacts. its something i want to keep in mind going forward.",
    "honestly this covered alot more than i thought it would. the part about evidence based approaches was new to me and it actually makes sense why those methods are used.",
    "i learned that accountability and self awareness are key for making changes that stick. reading this made me reflect on areas where i could apply some of these ideas.",
]

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


def _run_llm_prompt(system_prompt: str) -> Optional[str]:
    """
    Try agy → opencode (mimo) → Gemini API → return None.
    Returns clean reflection text or None on all failures.
    """
    # ── 1. Try agy ────────────────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["agy", "-p", system_prompt, "--model", "Gemini 3.6 Flash (Low)"],
            capture_output=True, text=True, timeout=60, cwd=ROOT_DIR,
        )
        if result.returncode == 0:
            text = _extract_prose(_clean_llm_text(result.stdout))
            if len(text) >= REFLECTION_MIN:
                log.info(f"agy reflection ({len(text)} chars): {text!r}")
                return text
            log.warning(f"agy output too short ({len(text)} chars): {text!r}")
        else:
            stderr = result.stderr[:300]
            if any(k in stderr.lower() for k in ["quota", "rate", "limit", "429", "exhausted"]):
                log.warning("agy quota/rate limit hit — trying opencode fallback")
            else:
                log.warning(f"agy exit {result.returncode}: {stderr}")
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
            cmd, capture_output=True, text=True, timeout=120, cwd=ROOT_DIR,
        )
        if result.returncode == 0:
            prose = _extract_prose(_clean_llm_text(result.stdout))
            if len(prose) >= REFLECTION_MIN:
                log.info(f"opencode reflection ({len(prose)} chars): {prose!r}")
                return prose
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

    # ── 3. Try Gemini API directly (HTTP fallback) ────────────────────────────
    try:
        api_key = os.environ.get("GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY", ""))
        if not api_key:
            agy_cfg = os.path.expanduser("~/.config/agy/config.json")
            if os.path.exists(agy_cfg):
                with open(agy_cfg, encoding="utf-8") as f:
                    cfg = json.load(f)
                    api_key = cfg.get("api_key") or cfg.get("gemini_api_key") or cfg.get("key", "")
        if api_key:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={api_key}"
            )
            payload = json.dumps({
                "contents": [{"parts": [{"text": system_prompt}]}],
                "generationConfig": {"temperature": 0.8, "maxOutputTokens": 300},
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            prose = _extract_prose(_clean_llm_text(text))
            if len(prose) >= REFLECTION_MIN:
                log.info(f"gemini-api reflection ({len(prose)} chars): {prose!r}")
                return prose
            log.warning(f"gemini-api output too short ({len(prose)} chars)")
        else:
            log.warning("No GEMINI_API_KEY found — skipping Gemini API fallback")
    except Exception as e:
        log.warning(f"Gemini API fallback error: {e}")

    return None


def call_agy(article_title: str, article_body: str, prompt_text: str) -> str:
    """
    Generate a reflection via agy → opencode → hardcoded fallback chain.
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
        if len(result) > REFLECTION_MAX:
            cut = result.rfind(".", REFLECTION_MIN, REFLECTION_MAX)
            result = result[:cut + 1] if cut > REFLECTION_MIN else result[:REFLECTION_MAX]
        return result

    # ── 3. Hardcoded fallback ─────────────────────────────────────────────────
    idx = int(time.time() / 3600) % len(_FALLBACKS)
    r = _FALLBACKS[idx]
    log.warning(f"agy, opencode, and gemini-api failed — using hardcoded fallback [{idx}] ({len(r)} chars)")
    return r


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
        log_event("scroll_keepalive")
    except Exception as e:
        log.debug(f"scroll err: {e}")


def parse_timer(body: str) -> int:
    for m, s in re.findall(r'\b(\d{1,2}):(\d{2})\b', body):
        mins, secs = int(m), int(s)
        if 0 <= mins <= 120 and 0 <= secs <= 59:
            total = mins * 60 + secs
            if total > 0:
                return total
    return 0


async def get_timer(page) -> int:
    try:
        body = await page.inner_text("body")
        if "ERR_" in body or "No internet" in body or "net::" in body:
            return -1
        return parse_timer(body)
    except:
        return -1


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


async def extract_article(page) -> tuple[str, str, str]:
    """Extract (title, body, lesson_prompt) from the article reading page."""
    title = "Community Service Article"
    body  = "community service, personal growth, and community impact"
    lesson_prompt = ""
    try:
        # ── Title: try multiple selectors in priority order ───────────────────
        title_selectors = [
            "h1", ".article-title", ".lesson-title", ".course-title",
            "[class*='title']", "[class*='heading']", "h2"
        ]
        for sel in title_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    t = (await el.inner_text()).strip()
                    if 5 < len(t) < 200 and "Foundation" not in t and "Log" not in t:
                        title = t
                        break
            except Exception:
                continue

        # ── Body + Lesson Prompt: single JS evaluation pass ────────────────
        result = await page.evaluate('''() => {
            const CLUTTER_WORDS = [
                "time remaining", "navigation", "dashboard", "log out", "logout",
                "reflection submitted", "submit", "next article", "great work",
                "sign in", "sign out", "copyright", "all rights reserved",
                "cookie", "privacy policy", "terms of service"
            ];
            const CLUTTER_TAGS = new Set(["button", "nav", "footer", "header", "script", "style", "noscript"]);
            const MIN_PARA_LEN = 40;
            // These are the GENERIC site-wide prompts that appear on /reflect — exclude from body and prompt
            const GENERIC_PROMPTS = [
                "please share your thoughts on the article you just read.",
                "what did you take away from this article?",
                "write your reflection here",
                "enter your response",
                "how relevant was the information",
                "what improvements, if any",
                "do you have any other feedback"
            ];

            // Detect if we're on the /reflect page (not the article reading page)
            const isReflectPage = window.location.pathname.endsWith("/reflect");

            function isGenericPrompt(text) {
                const lower = text.trim().toLowerCase();
                for (let gp of GENERIC_PROMPTS) { if (lower.startsWith(gp)) return true; }
                return false;
            }

            function isClutter(el, text) {
                if (CLUTTER_TAGS.has(el.tagName.toLowerCase())) return true;
                if (el.closest("nav, footer, header, [role='navigation']")) return true;
                const lower = text.toLowerCase();
                for (let w of CLUTTER_WORDS) { if (lower.includes(w)) return true; }
                if (isGenericPrompt(text)) return true;
                return false;
            }

            function isGoodPrompt(text) {
                if (!text || text.trim().length < 15) return false;
                if (isGenericPrompt(text)) return false;
                const lower = text.trim().toLowerCase();
                return (
                    text.includes("?") ||
                    lower.startsWith("describe") ||
                    lower.startsWith("explain") ||
                    lower.startsWith("how") ||
                    lower.startsWith("what") ||
                    lower.startsWith("why") ||
                    lower.startsWith("reflect") ||
                    lower.startsWith("think about") ||
                    lower.startsWith("share") ||
                    lower.startsWith("discuss")
                );
            }

            // ── Lesson Prompt Extraction ─────────────────────────────────────
            // Priority 1: Numbered <li> items at the bottom (e.g. "1. What physical symptoms...")
            // These are the actual per-lesson reflection questions embedded in the article
            let lessonPrompt = "";
            if (!isReflectPage) {
                const liEls = Array.from(document.querySelectorAll("li"));
                for (let el of liEls) {
                    if (el.closest("nav, footer, header")) continue;
                    const t = (el.innerText || "").trim();
                    // Numbered reflection questions look like "1. Have you ever..." or "2. If you reflect..."
                    if (/^\\d+\\.\\s+/.test(t) && isGoodPrompt(t) && t.length < 500) {
                        lessonPrompt = t.replace(/^\\d+\\.\\s+/, "").trim();
                        break;
                    }
                }

                // Priority 2: Class-based selectors
                if (!lessonPrompt) {
                    for (let sel of [".reflection-prompt", ".reflection-question", ".prompt", ".question",
                                     "[class*='prompt']", "[class*='question']", "blockquote", "h2", "h3", "h4"]) {
                        const els = Array.from(document.querySelectorAll(sel));
                        for (let el of els) {
                            if (el.closest("nav, footer, header")) continue;
                            const t = (el.innerText || "").trim();
                            if (isGoodPrompt(t) && t.length < 400) { lessonPrompt = t; break; }
                        }
                        if (lessonPrompt) break;
                    }
                }

                // Priority 3: <p> tags with question marks
                if (!lessonPrompt) {
                    for (let el of document.querySelectorAll("p")) {
                        if (el.closest("nav, footer, header")) continue;
                        const t = (el.innerText || "").trim();
                        if (isGoodPrompt(t) && t.includes("?") && t.length < 400) {
                            lessonPrompt = t; break;
                        }
                    }
                }
            }

            // ── Body Extraction ──────────────────────────────────────────────
            // Only extract body on the article reading page, not /reflect
            let bodyText = "";
            if (!isReflectPage) {
                const CONTAINER_SELS = [
                    "article", "main", ".prose", ".article-body", ".lesson-content",
                    ".course-content", "#content", "[class*='content']", "[class*='article']",
                    "[class*='lesson']", "[class*='course']"
                ];
                let container = null;
                for (let sel of CONTAINER_SELS) {
                    const el = document.querySelector(sel);
                    if (el) { container = el; break; }
                }

                let elements = [];
                if (container) {
                    elements = Array.from(container.querySelectorAll("p, h2, h3, h4, li"));
                    if (elements.length === 0) elements = [container];
                }
                if (elements.length === 0) {
                    elements = Array.from(document.querySelectorAll("p, li"));
                }

                const seen = new Set();
                const texts = [];
                for (let el of elements) {
                    const text = (el.innerText || "").trim();
                    if (!text || text.length < MIN_PARA_LEN) continue;
                    if (seen.has(text)) continue;
                    if (isClutter(el, text)) continue;
                    // Skip numbered reflection question li items from body (they're the prompt)
                    if (/^\\d+\\.\\s+/.test(text) && text.includes("?")) continue;
                    seen.add(text);
                    texts.push(text);
                    if (texts.join("\\n\\n").length > 3500) break;
                }
                bodyText = texts.join("\\n\\n");
            }

            return { body: bodyText, prompt: lessonPrompt, isReflectPage };
        }''')

        if result.get("isReflectPage"):
            log.info("   extract_article: on /reflect page — body/prompt skipped (will use fallback)")

        if result.get("body") and len(result["body"].strip()) > 80:
            body = result["body"][:3000]
        else:
            body = ""

        if result.get("prompt"):
            lesson_prompt = result["prompt"].strip()
            log.info(f"   Lesson prompt from article page: {lesson_prompt!r}")
        elif not result.get("isReflectPage"):
            log.info("   No lesson-specific prompt found on article page")

    except Exception as e:
        log.warning(f"article extract: {e}")
    return title, body, lesson_prompt


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
    """Scrape /coursework for lesson statuses and build an ordered catalog."""
    if not await safe_goto(page, f"{BASE_URL}/coursework"):
        log.error("Failed to load coursework page")
        return [], None

    await page.wait_for_timeout(3000)
    body = await page.inner_text("body")

    lessons: list[LessonEntry] = []
    for m in LESSON_ROW_RE.finditer(body):
        lessons.append(LessonEntry(
            title=m.group(1).strip(),
            duration=m.group(2).strip(),
            status=m.group(3).lower(),
        ))

    link_rows = await page.evaluate("""() => {
      const out = [];
      const seen = new Set();
      for (const a of document.querySelectorAll('a[href*="/coursework/"]')) {
        const m = a.href.match(/\\/coursework\\/([a-f0-9-]{36})$/);
        if (!m || seen.has(a.href)) continue;
        const linkText = (a.innerText || '').trim();
        if (/Continue Coursework/i.test(linkText)) continue;
        seen.add(a.href);

        let rowText = '';
        let el = a.parentElement;
        for (let i = 0; i < 3; i++) {
          if (!el) break;
          const t = (el.innerText || '').trim();
          if (t.length > 8 && t.length < 220 && /\\d+\\s*min/i.test(t)) {
            rowText = t;
            break;
          }
          el = el.parentElement;
        }
        if (!rowText) continue;

        const lines = rowText.split('\\n').map(s => s.trim()).filter(Boolean);
        let title = '';
        for (const line of lines) {
          if (/^(✅|📖|○|Done|Continue|Start)$/i.test(line)) continue;
          if (/^\\d+\\s*min$/i.test(line)) continue;
          title = line.replace(/\\s*\\d+\\s*min\\s*$/i, '').trim();
          if (title.length > 5) break;
        }
        if (!title) continue;
        out.push({title, href: a.href, linkText});
      }
      return out;
    }""")

    url_map = {_normalize_title(r["title"]): r["href"] for r in link_rows}
    for lesson in lessons:
        lesson.url = _match_url_for_title(lesson.title, url_map)

    cta_url = None
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

    # Fallback: attach CTA URL to the Continue lesson from text catalog
    if cta_url:
        for lesson in lessons:
            if lesson.status == "continue" and not lesson.url:
                lesson.url = cta_url
                log.info(f"   Mapped continue lesson {lesson.title!r} → CTA URL")

    done = sum(1 for l in lessons if l.status == "done")
    cont = sum(1 for l in lessons if l.status == "continue")
    start = sum(1 for l in lessons if l.status == "start")
    log.info(f"Catalog: {len(lessons)} lessons ({done} done, {cont} continue, {start} start)")
    if cta_url:
        log.info(f"   CTA continue: {cta_url}")

    completed_titles = [l.title for l in lessons if l.status == "done"]
    try:
        with open(os.path.join(ROOT_DIR, "completed_courses.json"), "w", encoding="utf-8") as f:
            json.dump({"count": len(completed_titles), "courses": completed_titles, "updated": datetime.now().isoformat()}, f, indent=2)
        log.info("╭────────────────────────────────────────────────────────╮")
        log.info("│ 🎓 COMPLETED COURSES LIST                              │")
        log.info("├────────────────────────────────────────────────────────┤")
        for i, title in enumerate(completed_titles, 1):
            log.info(f"│ {i}. {title}")
        log.info("╰────────────────────────────────────────────────────────╯")
    except Exception as e:
        log.error(f"Failed to save completed courses: {e}")
    log_event("completed_courses_snapshot", count=len(completed_titles), courses=completed_titles)

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

    # Unstarted lessons: verify reading before reflect
    if lesson.status == "start":
        if await safe_goto(page, lesson.url):
            secs = await get_timer(page)
            body = await page.inner_text("body")
            if secs > 0:
                log.info(f"   → Needs reading ({secs//60}:{secs%60:02d}): {lesson.title!r}")
                return "needs_read"
            if "Time Remaining" in body and "REFLECTION FOR" not in body:
                log.info(f"   → Needs reading (timer on page): {lesson.title!r}")
                return "needs_read"

    if await safe_goto(page, reflect_url):
        body = await page.inner_text("body")
        if any(kw in body for kw in ["Reflection Submitted", "Next Article", "Great work"]):
            log.info(f"   ✓ Reflection already submitted: {lesson.title!r}")
            return "complete"
        if await page_has_reflect_form(page):
            log.info(f"   → Needs reflect: {lesson.title!r}")
            return "needs_reflect"

    if lesson.status == "continue":
        if await safe_goto(page, lesson.url):
            secs = await get_timer(page)
            if secs > 0:
                log.info(f"   → Continue: reading timer still active: {lesson.title!r}")
                return "needs_read"

    if await safe_goto(page, lesson.url):
        secs = await get_timer(page)
        if secs > 0:
            log.info(f"   → Needs reading ({secs//60}:{secs%60:02d}): {lesson.title!r}")
            return "needs_read"

    if await safe_goto(page, reflect_url):
        if await page_has_reflect_form(page):
            log.info(f"   → Reading done, needs reflect: {lesson.title!r}")
            return "needs_reflect"
        body = await page.inner_text("body")
        if any(kw in body for kw in ["Reflection Submitted", "Next Article", "Great work"]):
            return "complete"

    log.warning(f"   Could not determine state for {lesson.title!r} — skipping")
    return "complete"


# ── Lesson phases ─────────────────────────────────────────────────────────────
async def wait_for_timer(
    page, phase: str, lesson_title: str,
    hours_done: float, hours_today: float, hours_total: float,
    rs: Optional[RunState] = None,
):
    """Poll timer, emit live status line, scroll keepalive every ~3 min."""
    elapsed = 0
    last_scroll = 0
    last_log = -1

    while True:
        secs = await get_timer(page)
        if secs == 0:
            log.info(f"[{phase}] ✓ timer expired — {lesson_title!r}")
            sys.stderr.write("\n")
            break
        elif secs == -1:
            log.warning(f"[{phase}] network error reading timer, retrying...")
            await page.wait_for_timeout(POLL_INTERVAL_S * 1000)
            elapsed += POLL_INTERVAL_S
            continue

        live_status(phase, secs, lesson_title, hours_done, hours_today, hours_total, rs)

        mins_remaining = secs // 60
        if mins_remaining != last_log and mins_remaining % 5 == 0:
            log.info(f"[{phase}] ⏱ {mins_remaining}min remaining  {lesson_title!r}  today:{hours_today:.1f}h")
            last_log = mins_remaining

        log_event("timer_tick", phase=phase, timer_secs=secs,
                  lesson_title=lesson_title, hours_done=hours_done, hours_today=hours_today)

        if elapsed - last_scroll >= SCROLL_INTERVAL_S:
            await scroll_keepalive(page)
            last_scroll = elapsed
            log.info(f"[{phase}] ↕ scroll keepalive at {elapsed//60}min elapsed")

        if elapsed > 95 * 60:
            log.warning(f"[{phase}] safety cap hit — moving on")
            sys.stderr.write("\n")
            break

        await page.wait_for_timeout(POLL_INTERVAL_S * 1000)
        elapsed += POLL_INTERVAL_S


async def reading_phase(
    page, lesson: LessonEntry, hours_done: float, hours_today: float,
    hours_total: float, rs: Optional[RunState] = None,
) -> tuple[str, str, str, str]:
    """Returns (article_title, article_body, pre_reflection, lesson_prompt)."""
    log.info(f"📖 Reading: {lesson.title!r} — {lesson.url}")
    log_event("reading_start", lesson_url=lesson.url, lesson_title=lesson.title)

    if not await safe_goto(page, lesson.url):
        raise RuntimeError(f"Could not open reading page for {lesson.title!r}")

    title, body, lesson_prompt = await extract_article(page)
    log.info(f"   Article: {title!r}")
    if not lesson_prompt:
        lesson_prompt = f"What key lessons did you take away from reading '{title}'?"
        log.info(f"   No specific prompt on article page — using title fallback")

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

    reflection = await agy_task
    return title, body, reflection, lesson_prompt


async def reflect_phase(
    page, lesson: LessonEntry, art_title: str, art_body: str, pre_reflection: str,
    hours_done: float, hours_today: float, hours_total: float,
    lesson_prompt: str = "",
    rs: Optional[RunState] = None,
) -> bool:
    reflect_url = lesson.url.rstrip("/") + "/reflect"
    log.info(f"✍️  Reflect: {art_title!r} — {reflect_url}")
    log_event("reflect_start", lesson_url=lesson.url, lesson_title=lesson.title,
              article_title=art_title)

    if not await safe_goto(page, reflect_url):
        log.error(f"Could not open reflect page for {lesson.title!r}")
        return False

    body_text = await page.inner_text("body")
    if any(kw in body_text for kw in ["Reflection Submitted", "Next Article", "Great work"]):
        log.info(f"   ✓ Already submitted: {art_title!r}")
        return True

    # Use the lesson-specific prompt extracted from the article page.
    # The /reflect page only has a generic prompt; the real question is on the reading page.
    if not lesson_prompt:
        lesson_prompt = f"What key lessons and insights did you take away from reading '{art_title}'?"
        log.info(f"   No lesson prompt passed in — using article-title fallback")

    log.info(f"Reflection Prompt Question: {lesson_prompt!r}")

    # Always re-call the LLM with the exact extracted prompt at reflect time
    log.info("   Calling agy for reflection with exact lesson prompt...")
    reflection = call_agy(art_title, art_body, lesson_prompt)

    log_event("reflection_generated", lesson_title=lesson.title,
              article_title=art_title, reflection=reflection,
              chars=len(reflection), source="agy")

    filled_len = 0
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
                break
        except Exception as e:
            log.debug(f"   textarea {sel}: {e}")

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


def log_user_profile(user_info: dict, prog: dict):
    """Log structured user information banner upon login."""
    log.info("╭────────────────────────────────────────────────────────╮")
    log.info("│ 👤 USER ACCOUNT PROFILE & EDIT PROFILE DETAILS         │")
    log.info("├────────────────────────────────────────────────────────┤")

    display_order = [
        "FULL NAME", "Full Name",
        "EMAIL (READ-ONLY)", "EMAIL", "Email",
        "DATE OF BIRTH", "PHONE", "GENDER",
        "REASON FOR COMMUNITY SERVICE", "COMMUNITY SERVICE RELATED TO",
        "ADDRESS", "CITY", "STATE", "ZIP CODE",
        "PROBATION OFFICER", "COURT ID",
        "ENROLLMENT PROOF ID", "Enrollment Proof ID",
        "OFFICIAL ENROLLMENT PROOF PDF URL",
        "COURT AUTHORIZATION LETTER LINK",
        "CERTIFICATE VERIFICATION PORTAL LINK",
    ]

    logged_keys = set()
    for key in display_order:
        if key in user_info and key not in logged_keys:
            val = user_info[key]
            if val:
                log.info(f"│ {key:<30}: {val}")
                logged_keys.add(key)

    for k, v in user_info.items():
        if k not in logged_keys and v:
            log.info(f"│ {k:<30}: {v}")
            logged_keys.add(k)

    if prog:
        pct = f"({prog.get('percent', 0)}% Complete)" if 'percent' in prog else ""
        log.info(f"│ {'OVERALL PROGRESS':<30}: {prog.get('done', 0)}h / {prog.get('total', 75)}h total {pct}")
        log.info(f"│ {'HOURS REMAINING':<30}: {prog.get('remaining', 0):.1f}h")

    log.info("╰────────────────────────────────────────────────────────╯")
    log_event("user_profile_loaded", **user_info)


async def wait_for_daily_reset(page, rs: RunState):
    """
    Called when daily limit (8.0h) is reached.
    Notifies the user and waits until midnight local time (00:00:00) or until site limit resets.
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
    log.info("│ 🔔 USER NOTIFICATION: Limit reached. Bot will wait...  │")
    log.info(f"│ ⏰ Next reset estimated at: {tomorrow.strftime('%Y-%m-%d 12:00:00 AM')}")
    log.info(f"│ ⏳ Time until reset: {h_str}")
    log.info("╰────────────────────────────────────────────────────────╯")

    log_event("daily_limit_wait_start", seconds_until_midnight=secs_until_midnight, reset_target=tomorrow.isoformat())

    start_time = time.time()
    last_check_time = time.time()
    last_notify_time = 0.0
    last_scroll_time = time.time()

    while True:
        now = datetime.now()
        secs_remaining = max(0, int((tomorrow - now).total_seconds()))

        live_status(
            "LIMIT_WAIT", secs_remaining, "Daily Limit Reached - Waiting for Reset",
            rs.hours_done, rs.hours_today, rs.hours_total, rs
        )

        if time.time() - last_notify_time >= 600:
            last_notify_time = time.time()
            rem_h = secs_remaining // 3600
            rem_m = (secs_remaining % 3600) // 60
            log.info(f"🌙 [LIMIT_WAIT] ⏱ {rem_h:02d}h {rem_m:02d}m remaining until daily reset (12:00 AM local time). Waiting...")

        if time.time() - last_check_time >= 900:
            last_check_time = time.time()
            log.info("🔍 Periodic check: inspecting daily limit status on site...")
            daily = await get_daily_status(page)
            if daily["hours_remaining_today"] > 0 and not daily.get("site_limit_reached") and daily.get("source") == "site":
                log_event("daily_limit_reset_detected")
                log.info("🌅 Limit reset confirmed! Resuming coursework...")
                break

        if secs_remaining <= 0:
            log.info("🌅 Local midnight reached! Checking daily limit status on site...")
            await page.wait_for_timeout(5000)
            daily = await get_daily_status(page)
            if daily["hours_remaining_today"] > 0 and not daily.get("site_limit_reached") and daily.get("source") == "site":
                log_event("daily_limit_reset_detected")
                log.info("🌅 Limit reset confirmed! Resuming coursework...")
                break
            else:
                log.info("⏳ Midnight reached! Reset not updated on site yet; retrying in 2 minutes...")
                await page.wait_for_timeout(120000)

        if time.time() - last_scroll_time >= 120:
            last_scroll_time = time.time()
            try:
                await page.evaluate("window.scrollBy(0, 100)")
                await page.wait_for_timeout(300)
                await page.evaluate("window.scrollBy(0, -100)")
            except Exception:
                pass

        await page.wait_for_timeout(25000)

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
    lesson_prompt = ""

    if state == "needs_read":
        art_title, art_body, reflection, lesson_prompt = await reading_phase(
            page, lesson, prog["done"], hours_today, prog["total"], rs
        )
    elif state == "needs_reflect":
        # Navigate to article page to get body + lesson prompt.
        # Note: if reading is complete the site may redirect to /reflect automatically.
        # We always explicitly navigate to the base lesson URL (not /reflect) to get real content.
        article_url = lesson.url.rstrip("/").replace("/reflect", "")
        if await safe_goto(page, article_url):
            # Wait briefly for any redirect to settle
            await page.wait_for_timeout(1500)
            # If page redirected to /reflect, extract_article returns empty.
            # Check current URL and log it.
            current_url = page.url
            if "/reflect" in current_url:
                log.info(f"   Article page redirected to /reflect — body/prompt unavailable from redirect")
            t, b, p = await extract_article(page)
            if t and t != "Community Service Article":
                art_title, art_body = t, b
            if p:
                lesson_prompt = p
                log.info(f"   Extracted lesson prompt: {lesson_prompt!r}")
        log.info(f"   Reading already complete — going to reflect for {art_title!r}")


    prog_before = await get_progress(page)
    success = await reflect_phase(
        page, lesson, art_title, art_body, reflection,
        prog_before["done"], hours_today, prog_before["total"],
        lesson_prompt=lesson_prompt, rs=rs,
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
    """Truncate logs to the last 200 lines if they exceed 100 KB."""
    for filepath in [LOG_FILE, EVENTS_FILE]:
        try:
            if os.path.exists(filepath) and os.path.getsize(filepath) > 100 * 1024:
                with open(filepath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                with open(filepath, "w", encoding="utf-8") as f:
                    f.writelines(lines[-200:])
                log.info(f"Rotated {os.path.basename(filepath)} (was > 100 KB)")
        except Exception as e:
            log.warning(f"Failed to rotate {filepath}: {e}")


async def main():
    global RUN_STATE

    log.info("╭────────────────────────────────────────────────────────╮")
    log.info(f"│ TFC Bot v4  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<31} │")
    log.info("├────────────────────────────────────────────────────────┤")
    log.info("│ Catalog discovery | state verify | agy reflections     │")
    log.info("╰────────────────────────────────────────────────────────╯")

    log_event("bot_start", version=4)
    rotate_logs_if_large()

    hours_today_start = get_today_hours_from_log()
    log.info(f"📅 Hours logged today (from events.jsonl): {hours_today_start:.1f}h")

    async with async_playwright() as p:
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

        if not await ensure_auth(page):
            log.error("Auth failed. Exiting.")
            await page.close()
            await ctx.close()
            await browser.close()
            sys.exit(1)

        prog = await get_progress(page)
        log_event("progress_snapshot", **prog)

        user_info = await get_user_profile(page)
        log_user_profile(user_info, prog)

        daily = await get_daily_status(page)
        hours_today = daily["hours_today"]
        hours_remaining_today = daily["hours_remaining_today"]
        log.info(
            f"📅 Today: {hours_today:.1f}h done, {hours_remaining_today:.1f}h left "
            f"(source: {daily['source']})"
        )

        rs = RUN_STATE
        rs.hours_done = prog["done"]
        rs.hours_total = prog["total"]
        rs.hours_today = hours_today

        if check_daily_limit(hours_today, hours_remaining_today):
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
            live_status("START", 0, lesson.title, rs.hours_done, hours_today, rs.hours_total, rs)

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
                      
            # Save to bot_completed_courses.json
            if success:
                bot_comp_file = os.path.join(ROOT_DIR, "bot_completed_courses.json")
                bot_list = []
                if os.path.exists(bot_comp_file):
                    try:
                        with open(bot_comp_file, "r", encoding="utf-8") as f:
                            bdata = json.load(f)
                            if isinstance(bdata, dict):
                                bot_list = bdata.get("courses", [])
                            elif isinstance(bdata, list):
                                bot_list = bdata
                    except Exception:
                        bot_list = []

                exists = any(
                    (c.get("title") if isinstance(c, dict) else c) == lesson.title
                    for c in bot_list
                )
                if not exists:
                    bot_list.append({
                        "title": lesson.title,
                        "url": lesson.url,
                        "ts": datetime.now().isoformat()
                    })
                    try:
                        with open(bot_comp_file, "w", encoding="utf-8") as f:
                            json.dump({"count": len(bot_list), "courses": bot_list, "updated": datetime.now().isoformat()}, f, indent=2)
                        log.info(f"✅ Saved bot-completed course {lesson.title!r} to bot_completed_courses.json")
                    except Exception as e:
                        log.error(f"Failed to write bot_completed_courses.json: {e}")

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
    asyncio.run(main())
