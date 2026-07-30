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
from typing import Any, Optional

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(ROOT_DIR, "telegram_settings.json")
CONFIG_FILE = os.path.join(ROOT_DIR, "telegram_config.json")
LESSON_MSG_FILE = os.path.join(ROOT_DIR, "telegram_lesson_msg.json")
EVENTS_FILE = os.getenv("TFC_EVENTS_FILE", os.path.join(ROOT_DIR, "events.jsonl"))
BOT_PID_FILE = os.path.join(ROOT_DIR, "bot.pid")
BOT_COMPLETED_FILE = os.path.join(ROOT_DIR, "bot_completed_courses.json")
DAILY_HOUR_LIMIT = float(os.getenv("TFC_DAILY_HOUR_LIMIT", "8.0"))
COURSE_HOUR_TOTAL = 75.0
TYPICAL_LESSON_HOURS = 1.1
MAX_LESSON_HOURS = 2.0

_log = logging.getLogger("tfc.telegram")
_started = False
_start_lock = threading.Lock()
_msg_queue: queue.Queue = queue.Queue()
_last_error_log = 0.0
_ERROR_LOG_INTERVAL_S = 300.0

_SKIP_PUSH_EVENTS = frozenset({
    "timer_sync", "timer_tick", "scroll_keepalive",
})

_CMD_FOOTER = "<i>Commands: /status · /stats · /help</i>"
_REFLECTION_PREVIEW_MAX = 600
_LESSON_EDIT_INTERVAL_S = int(os.getenv("TELEGRAM_LESSON_EDIT_INTERVAL_S", "60"))
_last_lesson_card_edit = 0.0

# Queue ops: ("send", chat_id, text) | ("edit", chat_id, message_id, text)


def _load_dotenv() -> None:
    env_path = os.path.join(ROOT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv()


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


def is_configured() -> bool:
    _load_dotenv()
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())


def is_enabled() -> bool:
    if not is_configured():
        return False
    settings = _load_json(SETTINGS_FILE)
    if "enabled" in settings:
        return bool(settings["enabled"])
    return _env_enabled()


def is_linked() -> bool:
    return get_chat_id() is not None


def get_integration_status() -> dict:
    _load_dotenv()
    configured = is_configured()
    linked = is_linked()
    push_enabled = is_enabled() if configured else False
    return {
        "configured": configured,
        "linked": linked,
        "push_enabled": push_enabled,
        "commands_available": configured,
        "fully_active": configured and push_enabled and linked,
    }


def menubar_label() -> str:
    s = get_integration_status()
    if not s["configured"]:
        return "📱 Telegram: Not configured (.env)"
    if s["push_enabled"] and s["linked"]:
        return "📱 Telegram Notifications: ON"
    if s["push_enabled"] and not s["linked"]:
        return "📱 Telegram: ON — send /start to link"
    return "📱 Telegram Notifications: OFF"


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


def _load_lesson_msg() -> dict:
    return _load_json(LESSON_MSG_FILE)


def _save_lesson_msg(message_id: int, lesson_title: str, lesson_url: str = "") -> None:
    _save_json(LESSON_MSG_FILE, {
        "message_id": message_id,
        "lesson_title": lesson_title,
        "lesson_url": lesson_url,
    })


def _clear_lesson_msg() -> None:
    try:
        if os.path.exists(LESSON_MSG_FILE):
            os.remove(LESSON_MSG_FILE)
    except Exception:
        pass


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
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        if method == "editMessageText" and exc.code == 400 and "message is not modified" in detail.lower():
            return {"ok": True}
        _log_throttled(f"Telegram API {method} failed: {detail[:200]}")
    except Exception as exc:
        _log_throttled(f"Telegram API {method} error: {exc}")
    return None


def send_message(chat_id: int, text: str, *, parse_mode: str = "HTML") -> Optional[int]:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    result = _api_request("sendMessage", payload)
    if result and result.get("ok"):
        try:
            return int(result["result"]["message_id"])
        except (KeyError, TypeError, ValueError):
            pass
    return None


def edit_message(chat_id: int, message_id: int, text: str, *, parse_mode: str = "HTML") -> bool:
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    result = _api_request("editMessageText", payload)
    return bool(result and result.get("ok"))


def notify(text: str) -> None:
    if not is_enabled():
        return
    chat_id = get_chat_id()
    if chat_id is None:
        return
    _msg_queue.put(("send", chat_id, text))


def _enqueue_lesson_update(text: str, *, new_lesson: bool = False) -> None:
    if not is_enabled():
        return
    chat_id = get_chat_id()
    if chat_id is None:
        return
    stored = _load_lesson_msg()
    msg_id = stored.get("message_id")
    if msg_id and not new_lesson:
        _msg_queue.put(("edit", chat_id, int(msg_id), text))
    else:
        _msg_queue.put(("send_lesson", chat_id, text))


def _worker() -> None:
    while True:
        item = _msg_queue.get()
        if item is None:
            break
        if not is_enabled():
            continue
        try:
            op = item[0]
            if op == "send":
                _, chat_id, text = item
                send_message(int(chat_id), text)
            elif op == "send_lesson":
                _, chat_id, text = item
                mid = send_message(int(chat_id), text)
                if mid:
                    title = _extract_title_from_card(text)
                    _save_lesson_msg(mid, title)
            elif op == "edit":
                _, chat_id, message_id, text = item
                if not edit_message(int(chat_id), int(message_id), text):
                    mid = send_message(int(chat_id), text)
                    if mid:
                        title = _extract_title_from_card(text)
                        _save_lesson_msg(mid, title)
        except Exception as exc:
            _log_throttled(f"Telegram worker error: {exc}")


def _map_timer_phase(phase: str) -> str:
    p = (phase or "").upper()
    if p == "READ":
        return "reading"
    if p == "REFLECT":
        return "reflect"
    return (phase or "reading").lower()


def _maybe_refresh_lesson_card(record: dict, *, phase: Optional[str] = None) -> None:
    """Edit the pinned lesson message from timer_sync (throttled)."""
    global _last_lesson_card_edit
    if not is_enabled() or get_chat_id() is None:
        return
    if not _load_lesson_msg().get("message_id"):
        return
    now = time.time()
    if now - _last_lesson_card_edit < _LESSON_EDIT_INTERVAL_S:
        return
    card_phase = phase or _map_timer_phase(str(record.get("phase", "")))
    if card_phase not in ("reading", "reflect"):
        return
    _last_lesson_card_edit = now
    _enqueue_lesson_update(_build_lesson_card_from_record(record, phase=card_phase))


def _extract_title_from_card(text: str) -> str:
    for line in text.splitlines():
        if "<b>Article</b>" in line or "<b>Lesson</b>" in line:
            return line.split("  ", 1)[-1].strip()
    return ""


def _fmt_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).strftime("%b %d · %I:%M %p").lstrip("0")
    except Exception:
        return ts[:16]


def _format_timer_secs(secs: Optional[int]) -> str:
    if secs is None or secs <= 0:
        return "—"
    if secs >= 3600:
        return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"
    return f"{secs // 60}:{secs % 60:02d}"


def _daily_line(hours_today: float, *, at_limit: bool = False) -> str:
    hours_today = max(0.0, float(hours_today))
    pct = min(100, int((hours_today / DAILY_HOUR_LIMIT) * 100)) if DAILY_HOUR_LIMIT > 0 else 0
    bar_w = 8
    filled = int((hours_today / DAILY_HOUR_LIMIT) * bar_w) if DAILY_HOUR_LIMIT > 0 else 0
    filled = min(bar_w, max(0, filled))
    bar = "█" * filled + "░" * (bar_w - filled)
    if at_limit or hours_today >= DAILY_HOUR_LIMIT - 0.05:
        return (
            f"<b>Today</b>  {hours_today:.1f} / {DAILY_HOUR_LIMIT:.0f} h  "
            f"<code>{bar}</code> {pct}%  — <b>limit reached</b>"
        )
    remaining = max(0.0, DAILY_HOUR_LIMIT - hours_today)
    return (
        f"<b>Today</b>  {hours_today:.1f} / {DAILY_HOUR_LIMIT:.0f} h  "
        f"<code>{bar}</code> {pct}%  ({remaining:.1f} h left)"
    )


def _phase_label(phase: str) -> str:
    return {
        "starting": "🚀 Starting",
        "reading": "📖 Reading",
        "reflect": "✍️ Reflection",
        "submitted": "✅ Submitted",
        "limit_wait": "🌙 Daily limit wait",
        "idle": "💤 Idle",
    }.get(phase, phase)


def _truncate_reflection(text: str, max_len: int = _REFLECTION_PREVIEW_MAX) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _live_reset_seconds(events: list[dict]) -> Optional[int]:
    """Countdown to midnight from the newest limit-wait event (live, not stale)."""
    for ev in reversed(events):
        if ev.get("event") != "daily_limit_wait_start":
            continue
        target = ev.get("reset_target")
        if target:
            try:
                end = datetime.fromisoformat(str(target))
                return max(0, int(end.timestamp() - time.time()))
            except Exception:
                pass
        secs = ev.get("seconds_until_midnight")
        ts = ev.get("ts")
        if secs is not None and ts:
            try:
                started = datetime.fromisoformat(ts).timestamp()
                return max(0, int(secs) - int(time.time() - started))
            except Exception:
                pass
        if secs is not None:
            return max(0, int(secs))
    return None


def _hours_from_limit_hit(events: list[dict]) -> tuple[float, float]:
    for ev in reversed(events):
        if ev.get("event") == "daily_limit_hit":
            return (
                float(ev.get("hours_today", DAILY_HOUR_LIMIT)),
                float(ev.get("hours_remaining", 0.0)),
            )
    return DAILY_HOUR_LIMIT, 0.0


def _infer_lesson_phase(record: dict) -> str:
    """Pick reading vs reflect for reflection_generated updates."""
    title = record.get("lesson_title")
    for ev in reversed(_tail_events()):
        e = ev.get("event")
        if e == "reflect_start" and ev.get("lesson_title") == title:
            return "reflect"
        if e in ("reading_start", "lesson_start") and ev.get("lesson_title") == title:
            return "reading"
    return "reading"


def _reconcile_overall_hours(events: list[dict], snapshot_done: float) -> float:
    """Correct inflated overall hours after a bad post-lesson scrape (e.g. +3.0h)."""
    completes = [
        e for e in events
        if e.get("event") == "lesson_complete" and e.get("hours_done") is not None
    ]
    if not completes:
        return snapshot_done

    last = completes[-1]
    gain = float(last.get("hours_gained", 0))
    if gain <= MAX_LESSON_HOURS:
        return snapshot_done

    title = last.get("lesson_title")
    anchor_done: Optional[float] = None
    for ev in reversed(events):
        if ev.get("event") not in ("reading_start", "reflect_start"):
            continue
        if ev.get("lesson_title") != title:
            continue
        if ev.get("hours_done") is not None:
            anchor_done = float(ev["hours_done"])
            break

    if anchor_done is not None:
        corrected = anchor_done + TYPICAL_LESSON_HOURS
        if abs(snapshot_done - float(last["hours_done"])) < 0.05:
            return corrected

    if len(completes) >= 2:
        prev_done = float(completes[-2]["hours_done"])
        corrected = prev_done + TYPICAL_LESSON_HOURS
        if snapshot_done > corrected + 0.5:
            return corrected

    return snapshot_done


def _parse_live_state(events: list[dict]) -> dict[str, Any]:
    """Phase + hours from the same newest anchor — never mix stale timer_sync with limit wait."""
    state: dict[str, Any] = {
        "phase": "idle",
        "lesson_title": None,
        "article_title": None,
        "timer_secs": None,
        "hours_today": 0.0,
        "hours_remaining_today": DAILY_HOUR_LIMIT,
        "hours_done": 0.0,
        "hours_total": 75.0,
        "reflection": None,
        "reflection_source": None,
        "reflection_draft_origin": None,
        "limit_reset_secs": None,
        "at_daily_limit": False,
    }

    anchor: Optional[dict] = None

    for ev in reversed(events):
        e = ev.get("event")
        if e == "reflect_submitted":
            state["phase"] = "submitted"
            state["lesson_title"] = ev.get("lesson_title")
            state["article_title"] = ev.get("article_title")
            anchor = ev
            break
        if e == "reflect_start":
            state["phase"] = "reflect"
            state["lesson_title"] = ev.get("lesson_title")
            state["article_title"] = ev.get("article_title")
            anchor = ev
            break
        if e == "reading_start":
            state["phase"] = "reading"
            state["lesson_title"] = ev.get("lesson_title")
            anchor = ev
            break
        if e == "lesson_start":
            state["phase"] = "starting"
            state["lesson_title"] = ev.get("lesson_title")
            anchor = ev
            break
        if e == "daily_limit_wait_complete":
            state["phase"] = "idle"
            anchor = ev
            break
        if e in ("daily_limit_wait_start", "daily_limit_hit"):
            state["phase"] = "limit_wait"
            state["at_daily_limit"] = True
            anchor = ev
            if e == "daily_limit_wait_start":
                state["limit_reset_secs"] = ev.get("seconds_until_midnight")
            break

    for ev in reversed(events):
        if ev.get("event") == "progress_snapshot":
            state["hours_done"] = float(ev.get("done", state["hours_done"]))
            state["hours_total"] = float(ev.get("total", COURSE_HOUR_TOTAL))
            break

    phase = state["phase"]

    if phase == "limit_wait":
        hit_hours, hit_rem = _hours_from_limit_hit(events)
        if anchor and anchor.get("hours_today") is not None:
            state["hours_today"] = float(anchor["hours_today"])
        else:
            state["hours_today"] = hit_hours
        state["hours_remaining_today"] = hit_rem
        reset = _live_reset_seconds(events)
        state["limit_reset_secs"] = reset
        state["timer_secs"] = reset
        state["lesson_title"] = None
        state["article_title"] = None
        state["reflection"] = None
        state["reflection_source"] = None
        state["reflection_draft_origin"] = None
    elif phase in ("reading", "reflect", "starting", "submitted"):
        for ev in reversed(events):
            if ev.get("event") != "timer_sync":
                continue
            state["timer_secs"] = ev.get("timer_secs")
            if ev.get("hours_today") is not None:
                state["hours_today"] = float(ev["hours_today"])
            if ev.get("hours_done") is not None:
                state["hours_done"] = float(ev["hours_done"])
            if ev.get("lesson_title"):
                state["lesson_title"] = ev["lesson_title"]
            break
        if anchor:
            if anchor.get("hours_today") is not None and state["hours_today"] <= 0:
                state["hours_today"] = float(anchor["hours_today"])
            if anchor.get("hours_done") is not None and state["hours_done"] <= 0:
                state["hours_done"] = float(anchor["hours_done"])
            if anchor.get("lesson_title"):
                state["lesson_title"] = anchor.get("lesson_title")
            if anchor.get("article_title"):
                state["article_title"] = anchor.get("article_title")
        state["hours_remaining_today"] = max(0.0, DAILY_HOUR_LIMIT - state["hours_today"])
        state["at_daily_limit"] = state["hours_today"] >= DAILY_HOUR_LIMIT - 0.05
        for ev in reversed(events):
            if ev.get("event") == "reflection_generated":
                state["reflection"] = ev.get("reflection")
                state["reflection_source"] = ev.get("source")
                state["reflection_draft_origin"] = ev.get("draft_origin")
                if not state["article_title"]:
                    state["article_title"] = ev.get("article_title")
                break
    elif anchor and anchor.get("hours_today") is not None:
        state["hours_today"] = float(anchor["hours_today"])
        state["hours_remaining_today"] = max(0.0, DAILY_HOUR_LIMIT - state["hours_today"])

    if state["hours_done"] > 0:
        state["hours_done"] = _reconcile_overall_hours(events, state["hours_done"])
        state["hours_total"] = float(state.get("hours_total") or COURSE_HOUR_TOTAL)

    return state


def _build_lesson_card(
    *,
    lesson_title: str,
    phase: str,
    article_title: str = "",
    hours_today: float = 0.0,
    hours_done: float = 0.0,
    hours_total: float = 75.0,
    timer_secs: Optional[int] = None,
    reflection: Optional[str] = None,
    reflection_source: Optional[str] = None,
    draft_origin: Optional[str] = None,
    submitted: bool = False,
) -> str:
    title = "✅ Lesson Submitted" if submitted or phase == "submitted" else "📚 Current Lesson"
    lines = [
        f"<b>Phase</b>  {_phase_label('submitted' if submitted else phase)}",
        f"<b>Lesson</b>  {_esc(lesson_title or '—')}",
    ]
    if article_title and article_title != lesson_title:
        lines.append(f"<b>Article</b>  {_esc(article_title)}")
    if phase in ("reading", "reflect", "starting", "submitted") and hours_today is not None:
        at_limit = hours_today >= DAILY_HOUR_LIMIT - 0.05
        lines.append(_daily_line(hours_today, at_limit=at_limit))
    if timer_secs and phase in ("reading", "reflect"):
        lines.append(f"<b>Timer</b>  {_esc(_format_timer_secs(timer_secs))}")
    if hours_done or hours_total:
        pct = int((hours_done / hours_total) * 100) if hours_total > 0 else 0
        bar = _progress_bar(hours_done, hours_total)
        lines.append(f"<b>Overall</b>  {hours_done:.1f} / {hours_total:.0f} h")
        lines.append(f"<code>{bar}</code>  {pct}%")
    if reflection:
        preview = _truncate_reflection(reflection)
        src = f" ({reflection_source})" if reflection_source else ""
        origin = draft_origin or ""
        if origin == "loaded":
            label = f"<b>Reflection draft</b>  <i>loaded from disk</i>{_esc(src)}"
        elif origin == "generated":
            label = f"<b>Reflection draft</b>  <i>generated</i>{_esc(src)}"
        else:
            label = f"<b>Reflection draft</b>{_esc(src)}"
        lines.append("")
        lines.append(label)
        lines.append(f"<i>{_esc(preview)}</i>")
    if submitted or phase == "submitted":
        lines.append("")
        lines.append("<b>Status</b>  Reflection submitted to site ✓")
    return _card(title, lines)


def _build_lesson_card_from_record(record: dict, phase: Optional[str] = None) -> str:
    events = _tail_events()
    live = _parse_live_state(events)
    p = phase or live["phase"]
    reflection = record.get("reflection") or live.get("reflection")
    return _build_lesson_card(
        lesson_title=record.get("lesson_title") or live.get("lesson_title") or "—",
        phase=p,
        article_title=record.get("article_title") or live.get("article_title") or "",
        hours_today=float(record.get("hours_today", live.get("hours_today", 0))),
        hours_done=float(record.get("hours_done", live.get("hours_done", 0))),
        hours_total=float(live.get("hours_total", 75)),
        timer_secs=record.get("timer_secs") if record.get("timer_secs") is not None else live.get("timer_secs"),
        reflection=reflection,
        reflection_source=record.get("source") or live.get("reflection_source"),
        draft_origin=record.get("draft_origin") or live.get("reflection_draft_origin"),
        submitted=(p == "submitted"),
    )


def event_to_message(record: dict) -> Optional[str]:
    """Standalone push messages (not the live lesson thread)."""
    event = record.get("event", "")
    if event in _SKIP_PUSH_EVENTS:
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
                _daily_line(hours, at_limit=True),
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
    if not is_enabled() or get_chat_id() is None:
        return

    event = record.get("event", "")

    if event == "lesson_start":
        _clear_lesson_msg()
        _enqueue_lesson_update(
            _build_lesson_card_from_record(record, phase="starting"),
            new_lesson=True,
        )
        return

    if event == "reading_start":
        _enqueue_lesson_update(_build_lesson_card_from_record(record, phase="reading"))
        return

    if event == "reflection_generated":
        _enqueue_lesson_update(
            _build_lesson_card_from_record(record, phase=_infer_lesson_phase(record)),
        )
        return

    if event == "reflect_start":
        _enqueue_lesson_update(_build_lesson_card_from_record(record, phase="reflect"))
        return

    if event == "reflect_submitted":
        card = _build_lesson_card_from_record(record, phase="submitted")
        _enqueue_lesson_update(card)
        return

    if event == "timer_sync":
        _maybe_refresh_lesson_card(record)
        return

    if event == "daily_limit_hit":
        _clear_lesson_msg()
        return

    if event == "daily_limit_wait_start":
        _clear_lesson_msg()
        hours = float(record.get("hours_today", DAILY_HOUR_LIMIT))
        reset = record.get("seconds_until_midnight")
        lines = [
            _daily_line(hours, at_limit=True),
            "<b>Status</b>  Bot resting until midnight",
        ]
        if reset:
            lines.append(f"<b>Reset in</b>  {_esc(_format_timer_secs(int(reset)))}")
        notify(_card("🌙 Daily Limit — Waiting", lines))
        return

    if event == "lesson_complete":
        title = record.get("lesson_title") or "Unknown lesson"
        gained = float(record.get("hours_gained", 0.0))
        today = float(record.get("hours_today", 0.0))
        done = float(record.get("hours_done", 0.0))
        total = float(record.get("hours_total", 75.0) or 75.0)
        pct = int((done / total) * 100) if total > 0 else 0
        complete_card = _card(
            "✅ Lesson Complete",
            [
                f"<b>Article</b>  {_esc(title)}",
                f"<b>This lesson</b>  +{gained:.2f} h",
                _daily_line(today),
                f"<b>Overall</b>  {done:.1f} / {total:.0f} h ({pct}%)",
            ],
        )
        stored = _load_lesson_msg()
        chat_id = get_chat_id()
        if stored.get("message_id") and chat_id:
            _msg_queue.put(("edit", chat_id, int(stored["message_id"]), complete_card))
        else:
            notify(complete_card)
        _clear_lesson_msg()
        return

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


def build_status_text() -> str:
    events = _tail_events()
    live = _parse_live_state(events)
    running = is_bot_running()

    lines = [f"<b>Engine</b>  {'🟢 Running' if running else '🔴 Stopped'}"]

    phase = live["phase"]
    lines.append(f"<b>Phase</b>  {_phase_label(phase)}")

    if live.get("lesson_title") and phase in ("reading", "reflect", "starting", "submitted"):
        lines.append(f"<b>Lesson</b>  {_esc(live['lesson_title'])}")
    if (
        live.get("article_title")
        and phase in ("reading", "reflect", "submitted")
        and live["article_title"] != live.get("lesson_title")
    ):
        lines.append(f"<b>Article</b>  {_esc(live['article_title'])}")

    hours_today = float(live.get("hours_today", 0))

    if phase == "limit_wait":
        lines.append(_daily_line(hours_today or DAILY_HOUR_LIMIT, at_limit=True))
        reset = live.get("limit_reset_secs") or live.get("timer_secs")
        if reset:
            lines.append(f"<b>Reset in</b>  {_esc(_format_timer_secs(int(reset)))}")
    elif phase in ("reading", "reflect", "starting", "submitted"):
        lines.append(_daily_line(hours_today, at_limit=live.get("at_daily_limit", False)))

    if live.get("timer_secs") and phase in ("reading", "reflect"):
        lines.append(f"<b>Timer</b>  {_esc(_format_timer_secs(live['timer_secs']))}")

    done = float(live.get("hours_done", 0))
    total = float(live.get("hours_total", 75))
    if done or total:
        pct = int((done / total) * 100) if total > 0 else 0
        bar = _progress_bar(done, total)
        lines.append(f"<b>Overall</b>  {done:.1f} / {total:.0f} h")
        lines.append(f"<code>{bar}</code>  {pct}%")

    if live.get("reflection") and phase in ("reading", "reflect", "submitted"):
        preview = _truncate_reflection(str(live["reflection"]), 200)
        lines.append(f"<b>Draft</b>  <i>{_esc(preview)}</i>")

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

    live = _parse_live_state(events)
    if live["phase"] == "limit_wait":
        hours_today = float(live["hours_today"])
    elif live.get("hours_today"):
        hours_today = max(hours_today, float(live["hours_today"]))

    done = float(progress.get("done", progress.get("hours_done", live.get("hours_done", 0))))
    done = _reconcile_overall_hours(events, done)
    total = float(progress.get("total", COURSE_HOUR_TOTAL))
    remaining = max(0.0, total - done)
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
        _daily_line(hours_today, at_limit=live.get("at_daily_limit", False)),
        f"<b>Session</b>  {session_done} lesson{'s' if session_done != 1 else ''} today",
        f"<b>Lessons completed</b>  {bot_count} (bot-tracked)",
        f"<b>Engine</b>  {'🟢 Running' if is_bot_running() else '🔴 Stopped'}",
        f"<b>Now</b>  {_phase_label(live['phase'])}",
    ]

    return _card("📊 Coursework Stats", lines)


def build_help_text() -> str:
    return _card(
        "🤖 TFC Bot — Commands",
        [
            "<b>/start</b>  Link this chat for push notifications",
            "<b>/status</b>  Live phase (reading/reflect), daily limit, timer, draft",
            "<b>/stats</b>  Total hours, today's bar, completions, ETA",
            "<b>/help</b>  Show this message",
            "",
            "<b>Live lesson message</b>  One message per article — updates as the bot reads, drafts reflection, and submits",
            "<b>Toggle</b>  Menubar → Settings → Telegram Notifications",
        ],
        footer=False,
    )


def build_welcome_text() -> str:
    return _card(
        "✅ Chat Linked",
        [
            "One live message per lesson — it updates through:",
            "  📖 Reading → draft reflection → ✍️ Reflect → ✅ Submitted",
            "",
            "Also get alerts for daily limit, errors, and bot start/stop.",
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
