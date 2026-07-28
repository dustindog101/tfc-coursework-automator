"""
Optional Telegram notifications + on-demand status commands for TFC bot.
Non-blocking: failures never stop the coursework automator.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import date, datetime
from typing import Optional

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(ROOT_DIR, "telegram_settings.json")
CONFIG_FILE = os.path.join(ROOT_DIR, "telegram_config.json")
EVENTS_FILE = os.getenv("TFC_EVENTS_FILE", os.path.join(ROOT_DIR, "events.jsonl"))
BOT_PID_FILE = os.path.join(ROOT_DIR, "bot.pid")
BOT_COMPLETED_FILE = os.path.join(ROOT_DIR, "bot_completed_courses.json")
DAILY_HOUR_LIMIT = float(os.getenv("TFC_DAILY_HOUR_LIMIT", "8.0"))

_log = logging.getLogger("tfc.telegram")
_started = False
_start_lock = threading.Lock()
_msg_queue: queue.Queue[Optional[tuple[int, str]]] = queue.Queue()
_last_error_log = 0.0
_ERROR_LOG_INTERVAL_S = 300.0

_SKIP_EVENTS = frozenset({
    "timer_sync", "timer_tick", "scroll_keepalive", "reflection_generated",
})

_CMD_FOOTER = "<i>Commands: /status · /stats · /help</i>"


def _esc(text: object) -> str:
    return html.escape(str(text))


def _card(title: str, lines: list[str], *, footer: bool = True) -> str:
    body = "\n".join(lines)
    msg = f"<b>{_esc(title)}</b>\n\n{body}"
    if footer:
        msg += f"\n\n{_CMD_FOOTER}"
    return msg


def _env_enabled() -> bool:
    return os.getenv("TELEGRAM_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        _log_throttled(f"Could not write {path}: {exc}")


def _log_throttled(msg: str) -> None:
    global _last_error_log
    now = time.time()
    if now - _last_error_log >= _ERROR_LOG_INTERVAL_S:
        _last_error_log = now
        _log.warning(msg)


def is_enabled() -> bool:
    """True when env + runtime settings allow Telegram."""
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return False
    settings = _load_json(SETTINGS_FILE)
    if "enabled" in settings:
        return bool(settings["enabled"])
    return _env_enabled()


def get_chat_id() -> Optional[int]:
    data = _load_json(CONFIG_FILE)
    cid = data.get("chat_id")
    if cid is None:
        return None
    try:
        return int(cid)
    except (TypeError, ValueError):
        return None


def register_chat(chat_id: int) -> None:
    _save_json(CONFIG_FILE, {"chat_id": chat_id})


def ensure_settings_file() -> None:
    if os.path.exists(SETTINGS_FILE):
        return
    _save_json(SETTINGS_FILE, {"enabled": _env_enabled()})


def set_enabled(enabled: bool) -> None:
    _save_json(SETTINGS_FILE, {"enabled": enabled})


def _api_request(method: str, payload: dict) -> Optional[dict]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            detail = str(exc)
        _log_throttled(f"Telegram API {method} failed: {detail}")
    except Exception as exc:
        _log_throttled(f"Telegram API {method} error: {exc}")
    return None


def send_message(chat_id: int, text: str, *, parse_mode: str = "HTML") -> bool:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    result = _api_request("sendMessage", payload)
    return bool(result and result.get("ok"))


def notify(text: str) -> None:
    """Enqueue a push notification; no-op if disabled or chat not registered."""
    if not is_enabled():
        return
    chat_id = get_chat_id()
    if chat_id is None:
        return
    _msg_queue.put((chat_id, text))


def _worker() -> None:
    while True:
        item = _msg_queue.get()
        if item is None:
            break
        chat_id, text = item
        if not is_enabled():
            continue
        send_message(chat_id, text)


def _fmt_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).strftime("%b %d · %I:%M %p").lstrip("0")
    except Exception:
        return ts[:16]


def event_to_message(record: dict) -> Optional[str]:
    event = record.get("event", "")
    if event in _SKIP_EVENTS:
        return None

    if event == "bot_start":
        when = _fmt_ts(record.get("ts", ""))
        return _card(
            "🎓 Bot Started",
            [
                f"<b>Time</b>  {_esc(when)}" if when else "<b>Status</b>  Engine online",
                "<b>Mode</b>  Automated coursework",
            ],
        )

    if event == "lesson_start":
        title = record.get("lesson_title") or "Unknown lesson"
        return _card(
            "📖 New Article",
            [
                f"<b>Title</b>  {_esc(title)}",
                "<b>Phase</b>  Starting lesson",
            ],
        )

    if event == "lesson_complete":
        title = record.get("lesson_title") or "Unknown lesson"
        gained = float(record.get("hours_gained", 0.0))
        today = float(record.get("hours_today", 0.0))
        done = float(record.get("hours_done", 0.0))
        total = float(record.get("hours_total", 75.0) or 75.0)
        pct = int((done / total) * 100) if total > 0 else 0
        return _card(
            "✅ Lesson Complete",
            [
                f"<b>Article</b>  {_esc(title)}",
                f"<b>This lesson</b>  +{gained:.2f} h",
                f"<b>Today</b>  {today:.1f} h",
                f"<b>Overall</b>  {done:.1f} / {total:.0f} h ({pct}%)",
            ],
        )

    if event == "lesson_error":
        title = record.get("lesson_title") or "Unknown lesson"
        err = record.get("error", "unknown error")
        return _card(
            "⚠️ Lesson Error",
            [
                f"<b>Article</b>  {_esc(title)}",
                f"<b>Detail</b>  {_esc(err)}",
                "<b>Bot</b>  Retrying — check automation.log if this repeats",
            ],
        )

    if event == "daily_limit_hit":
        hours = float(record.get("hours_today", DAILY_HOUR_LIMIT))
        return _card(
            "🌙 Daily Limit Reached",
            [
                f"<b>Logged today</b>  {hours:.1f} h / {DAILY_HOUR_LIMIT:.0f} h",
                "<b>Next</b>  Waiting for midnight reset",
                "<b>Bot</b>  Still running — will resume automatically",
            ],
        )

    if event == "bot_stop":
        n = int(record.get("lessons_this_run", 0))
        done = record.get("hours_done")
        lines = [f"<b>Session</b>  {n} lesson{'s' if n != 1 else ''} completed"]
        if done is not None:
            lines.append(f"<b>Total hours</b>  {float(done):.1f} h")
        return _card("⏹ Bot Stopped", lines)

    return None


def on_event(record: dict) -> None:
    msg = event_to_message(record)
    if msg:
        notify(msg)


def _tail_events(max_lines: int = 500) -> list[dict]:
    if not os.path.exists(EVENTS_FILE):
        return []
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            lines = deque(f, maxlen=max_lines)
    except Exception:
        return []
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def is_bot_running() -> bool:
    try:
        with open(BOT_PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
        return _pid_alive(pid)
    except Exception:
        pass
    return False


def _estimate_days(hours_remaining: float, hours_today: float = 0.0) -> int:
    if hours_remaining <= 0:
        return 0
    avail_today = max(0.0, DAILY_HOUR_LIMIT - hours_today)
    if hours_remaining <= avail_today:
        return 0
    after_today = hours_remaining - avail_today
    return int(math.ceil(after_today / DAILY_HOUR_LIMIT))


def _progress_bar(done: float, total: float, width: int = 10) -> str:
    pct = done / total if total > 0 else 0
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)


def _format_timer_secs(secs: int) -> str:
    if secs >= 3600:
        return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"
    return f"{secs // 60}:{secs % 60:02d}"


def build_status_text() -> str:
    events = _tail_events()
    running = is_bot_running()

    progress: dict = {}
    lesson_title = None
    phase = None
    timer_end_at = None
    limit_wait = False
    session_done = 0

    for ev in reversed(events):
        if not progress and ev.get("event") == "progress_snapshot":
            progress = ev
        ev_name = ev.get("event", "")
        if ev_name in ("daily_limit_hit", "daily_limit_wait_start"):
            limit_wait = True
        if ev_name == "lesson_start" and lesson_title is None:
            lesson_title = ev.get("lesson_title")
        if ev_name == "reading_start":
            phase = "Reading"
            if not lesson_title:
                lesson_title = ev.get("lesson_title")
        if ev_name == "reflect_start":
            phase = "Reflection"
            if not lesson_title:
                lesson_title = ev.get("lesson_title")
        if ev_name == "timer_sync" and timer_end_at is None:
            timer_end_at = ev.get("timer_end_at")

    today = date.today().isoformat()
    for ev in events:
        if ev.get("event") == "lesson_complete" and ev.get("date") == today:
            session_done += 1

    lines = [
        f"<b>Engine</b>  {'🟢 Running' if running else '🔴 Stopped'}",
    ]

    if limit_wait:
        lines.append("<b>Phase</b>  🌙 Daily limit wait")
    elif phase:
        lines.append(f"<b>Phase</b>  {_esc(phase)}")
    elif lesson_title:
        lines.append("<b>Phase</b>  Lesson")
    else:
        lines.append("<b>Phase</b>  Idle")

    if lesson_title:
        lines.append(f"<b>Article</b>  {_esc(lesson_title)}")

    if timer_end_at:
        try:
            end = datetime.fromisoformat(timer_end_at)
            secs = max(0, int(end.timestamp() - time.time()))
            lines.append(f"<b>Timer</b>  {_esc(_format_timer_secs(secs))}")
        except Exception:
            pass

    if progress:
        done = float(progress.get("done", progress.get("hours_done", 0)))
        total = float(progress.get("total", 75))
        pct = int((done / total) * 100) if total > 0 else 0
        bar = _progress_bar(done, total)
        lines.append(f"<b>Progress</b>  {done:.1f} / {total:.0f} h")
        lines.append(f"<code>{bar}</code>  {pct}%")

    if session_done:
        lines.append(f"<b>Today</b>  {session_done} lesson{'s' if session_done != 1 else ''} this session")

    if not events:
        lines.append("<i>No events logged yet — start the bot first.</i>")

    return _card("📍 Live Status", lines)


def build_stats_text() -> str:
    events = _tail_events()
    today = date.today().isoformat()

    progress: dict = {}
    session_done = 0
    hours_today = 0.0

    for ev in events:
        if ev.get("event") == "progress_snapshot":
            progress = ev
        if ev.get("event") == "lesson_complete" and ev.get("date") == today:
            hours_today += float(ev.get("hours_gained", 0.0))
            session_done += 1

    done = float(progress.get("done", progress.get("hours_done", 0)))
    total = float(progress.get("total", 75))
    remaining = float(progress.get("remaining", max(0.0, total - done)))
    pct = int((done / total) * 100) if total > 0 else 0
    bar = _progress_bar(done, total)
    eta_days = _estimate_days(remaining, hours_today)

    bot_count = 0
    if os.path.exists(BOT_COMPLETED_FILE):
        try:
            with open(BOT_COMPLETED_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                bot_count = len(data)
            elif isinstance(data, dict):
                bot_count = len(data.get("courses", data.get("titles", [])))
        except Exception:
            pass

    if eta_days <= 0:
        eta = "finish today"
    elif eta_days == 1:
        eta = "~1 day"
    else:
        eta = f"~{eta_days} days"

    lines = [
        f"<b>Overall</b>  {done:.1f} / {total:.0f} h",
        f"<code>{bar}</code>  {pct}%",
        f"<b>Remaining</b>  {remaining:.1f} h · {eta}",
        f"<b>Today</b>  {hours_today:.1f} h · {session_done} lesson{'s' if session_done != 1 else ''}",
        f"<b>Lessons completed</b>  {bot_count} (bot-tracked)",
        f"<b>Engine</b>  {'🟢 Running' if is_bot_running() else '🔴 Stopped'}",
        f"<b>Daily cap</b>  {DAILY_HOUR_LIMIT:.0f} h / day",
    ]

    return _card("📊 Coursework Stats", lines)


def build_help_text() -> str:
    return _card(
        "🤖 TFC Bot — Commands",
        [
            "<b>/start</b>  Link this chat for push notifications",
            "<b>/status</b>  Live article, phase, timer, and progress",
            "<b>/stats</b>  Total hours, today's work, completions, ETA",
            "<b>/help</b>  Show this message",
            "",
            "<b>Push alerts</b>  Bot start/stop, new article, lesson done, daily limit, errors",
            "<b>Toggle</b>  Menubar → Settings → Telegram Notifications",
        ],
        footer=False,
    )


def build_welcome_text() -> str:
    return _card(
        "✅ Chat Linked",
        [
            "You will receive notifications when the bot:",
            "  • Starts or stops",
            "  • Begins a new article",
            "  • Completes a lesson",
            "  • Hits the daily hour limit",
            "  • Encounters an error",
            "",
            "Use the commands below anytime for a live check-in.",
        ],
        footer=False,
    ) + "\n\n" + build_help_text()


def _handle_command(chat_id: int, text: str) -> None:
    parts = (text or "").strip().split()
    cmd = parts[0].lower().split("@")[0] if parts else ""

    if cmd == "/start":
        register_chat(chat_id)
        send_message(chat_id, build_welcome_text())
        return

    registered = get_chat_id()
    if registered is not None and chat_id != registered:
        return

    if registered is None:
        send_message(
            chat_id,
            _card(
                "👋 Welcome",
                [
                    "Send <b>/start</b> to link this chat.",
                    "Then use <b>/status</b> and <b>/stats</b> anytime.",
                ],
            ),
        )
        return

    if cmd == "/status":
        send_message(chat_id, build_status_text())
    elif cmd == "/stats":
        send_message(chat_id, build_stats_text())
    elif cmd == "/help":
        send_message(chat_id, build_help_text())


def _command_listener() -> None:
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return
    offset = 0
    while True:
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            time.sleep(5)
            continue
        result = _api_request("getUpdates", {
            "offset": offset,
            "timeout": 25,
            "allowed_updates": ["message"],
        })
        if not result or not result.get("ok"):
            time.sleep(2)
            continue
        for update in result.get("result", []):
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = msg.get("text", "")
            if chat_id is None or not text:
                continue
            try:
                if text.strip().startswith("/"):
                    _handle_command(int(chat_id), text)
            except Exception as exc:
                _log_throttled(f"Telegram command error: {exc}")


def start() -> None:
    """Start worker + command listener threads (idempotent)."""
    global _started
    with _start_lock:
        if _started:
            return
        ensure_settings_file()
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            return
        _started = True
        threading.Thread(target=_worker, name="telegram-worker", daemon=True).start()
        threading.Thread(target=_command_listener, name="telegram-commands", daemon=True).start()
