#!/usr/bin/env python3
"""
TFC Coursework Automator — Lightweight macOS Menu Bar App
Built with rumps (PyObjC native Cocoa NSStatusItem). Ultra-lightweight (<15MB RAM, 30KB disk).
"""

import os
import sys
import json
import re
import math
import subprocess
import time
from datetime import datetime, timedelta
from collections import deque
import rumps

import socket

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(ROOT_DIR, "events.jsonl")
# Skip when loading events.jsonl (keeps timer/reflection events for live UI)
_EVENTS_SKIP_LOAD = frozenset({"scroll_keepalive"})
# Skip in history submenu (draft/timer shown elsewhere)
_HISTORY_SKIP_EVENTS = frozenset({
    "scroll_keepalive", "timer_sync", "timer_tick", "reflection_generated",
})
LOG_FILE = os.path.join(ROOT_DIR, "automation.log")
BOT_PID_FILE = os.path.join(ROOT_DIR, "bot.pid")
COMPLETED_COURSES_FILE = os.path.join(ROOT_DIR, "completed_courses.json")
SCRIPT_PATH = os.path.join(ROOT_DIR, "run_courses.py")
ENV_FILE = os.path.join(ROOT_DIR, ".env")
LAUNCH_AGENT_PATH = os.path.expanduser("~/Library/LaunchAgents/com.tfc.automator.plist")

BOT_START_GRACE_S = 25
BOT_RESTART_COOLDOWN_S = 60
WATCHDOG_MAX_RESTARTS = 3
WATCHDOG_RESTART_WINDOW_S = 300

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_bot_pid_file() -> int | None:
    try:
        with open(BOT_PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _remove_bot_pid_file() -> None:
    try:
        if os.path.exists(BOT_PID_FILE):
            os.remove(BOT_PID_FILE)
    except Exception:
        pass


_MALLOC_ENV_KEYS = (
    "MallocStackLogging", "MallocStackLoggingNoCompact", "MallocScribble",
    "MallocGuardEdges", "MALLOC_STACK_LOGGING",
)


def _subprocess_env(extra=None) -> dict:
    env = os.environ.copy()
    for key in _MALLOC_ENV_KEYS:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env

_SINGLE_INSTANCE_LOCK_SOCKET = None

def enforce_single_instance():
    """Ensure only ONE instance of menubar.py runs simultaneously on the system."""
    global _SINGLE_INSTANCE_LOCK_SOCKET
    try:
        _SINGLE_INSTANCE_LOCK_SOCKET = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _SINGLE_INSTANCE_LOCK_SOCKET.bind(("127.0.0.1", 49153))
    except socket.error:
        sys.stderr.write("⚠️ Another instance of TFC Menu Bar is already running. Exiting.\n")
        sys.exit(0)


def format_local_time(ts_iso_str: str) -> str:
    """Parse ISO timestamp and return user's 12-hour local time (e.g. '3:45 PM')."""
    if not ts_iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_iso_str)
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        if len(ts_iso_str) >= 16:
            return ts_iso_str[11:16]
        return ts_iso_str


def format_timer_remaining(end_ts: float) -> str:
    """Format a countdown from a wall-clock end timestamp."""
    if end_ts <= 0:
        return "N/A"
    secs = max(0, int(end_ts - time.time()))
    if secs <= 0:
        return "N/A"
    mins, s = secs // 60, secs % 60
    return f"{mins}m {s}s" if s else f"{mins}m"


def estimate_days_to_complete(hours_remaining: float, hours_today: float = 0.0, daily_limit: float = 8.0) -> int:
    if hours_remaining <= 0:
        return 0
    avail_today = max(0.0, daily_limit - hours_today)
    if hours_remaining <= avail_today:
        return 0
    after_today = hours_remaining - avail_today
    return int(math.ceil(after_today / daily_limit))


def format_eta_label(days: int) -> str:
    if days <= 0:
        return "finish today"
    if days == 1:
        return "~1 day"
    return f"~{days} days"


class TFCCourseworkMenuApp(rumps.App):
    def __init__(self):
        super(TFCCourseworkMenuApp, self).__init__(
            name="TFC Bot",
            title="🎓 TFC Bot: Initializing...",
            quit_button=None
        )
        self.profile = {}
        self.progress = {"done": 0.0, "total": 75.0, "remaining": 75.0}
        self.current_lesson = "None"
        self.upcoming_reflection = "No reflection generated yet."
        self.upcoming_reflection_source = ""
        self.daily_limit_reached = False
        self.last_state_id = "INIT"
        self.last_notified_state = "INIT"
        self.consecutive_state_count = 0
        self.display_mode = "auto"
        self.hours_today = 0.0
        
        # User settings
        self.headed_mode = False        # Headless by default
        self.watchdog_enabled = True   # Watchdog active by default
        self.user_paused = False       # Tracks if user manually paused bot
        self.last_crash_time = 0.0
        self._watchdog_suppressed_until = 0.0
        self._bot_pid: int | None = None
        self._last_bot_start_time = 0.0
        self._watchdog_restart_times: list[float] = []
        self._watchdog_backoff_logged = False
        self._watchdog_armed = False
        self._telegram_ui_ts = 0.0
        self.display_mode = "auto"     # auto, timers, progress, full, minimal
        self._read_timer_end = 0.0
        self._reflect_timer_end = 0.0
        self._events_cache = []
        self._events_mtime = 0.0
        self._log_mtime = 0.0
        self._log_state = {}

        # ── 1. Status ──────────────────────────────────────────
        self.item_status = rumps.MenuItem("⚙️ Initializing...", callback=None)
        


        # ── 2. Coursework Queue ──────────────────────────────────────────────
        self.menu_queue = rumps.MenuItem("📚 Coursework Queue & Lesson")
        self.item_queue_lesson = rumps.MenuItem("📌 Active Lesson: None")
        self.item_queue_today = rumps.MenuItem("📅 Logged Today:  [░░░░░░░░░░] 0% (0.0 / 8.0h)")
        self.menu_queue.update([self.item_queue_lesson, self.item_queue_today])
        
        # ── 3. Active Countdown Timers ───────────────────────────────────────
        self.menu_timers = rumps.MenuItem("⏱️ Active Timers")
        self.item_read_timer = rumps.MenuItem("• Reading Timer: N/A")
        self.item_submit_timer = rumps.MenuItem("• Reflection Submit-Lock Timer: N/A")
        self.item_limit_timer = rumps.MenuItem("• Midnight Reset Timer: N/A (12:00 AM)")
        self.item_scroll_timer = rumps.MenuItem("• Keep-Alive Scroll Timer: Active (every 2.75m)")
        self.menu_timers.update([
            self.item_read_timer, self.item_submit_timer,
            self.item_limit_timer, self.item_scroll_timer
        ])
        
        # ── 4. Upcoming AI Reflection Draft ──────────────────────────────────
        self.menu_reflection = rumps.MenuItem("📝 Upcoming AI Reflection")
        self.item_refl_preview = rumps.MenuItem("Draft: None ready")
        self.item_refl_copy = rumps.MenuItem("📋 Copy Reflection", callback=self.copy_reflection)
        self.menu_reflection.update([self.item_refl_preview, self.item_refl_copy])
        
        # ── 4.5. Completed Courses ───────────────────────────────────────────
        self.menu_completed_courses = rumps.MenuItem("🎓 Completed Courses on Site (0)")
        self.menu_bot_completed = rumps.MenuItem("🤖 Bot Completed Courses (0)")
        self.menu_bot_completed.add(rumps.MenuItem("No courses completed by bot yet"))
        self.menu_site_completed = rumps.MenuItem("🌐 All Site Completed (0)")
        self.menu_site_completed.add(rumps.MenuItem("No courses loaded yet"))
        self.menu_completed_courses.update([self.menu_bot_completed, self.menu_site_completed])
        
        # ── 5. User Profile & Court Proof ──────────────────────────────────
        self.menu_profile = rumps.MenuItem("👤 User Profile & Court Proof")
        self.item_prof_name = rumps.MenuItem("• Full Name: Checking...")
        self.item_prof_email = rumps.MenuItem("• Email: Checking...")
        self.item_prof_dob = rumps.MenuItem("• DOB: Checking...")
        self.item_prof_cat = rumps.MenuItem("• Offense Category: Checking...")
        self.item_prof_addr = rumps.MenuItem("• Location/Address: Checking...")
        self.item_prof_id = rumps.MenuItem("• Enrollment ID: Checking...")
        
        self.item_copy_proof = rumps.MenuItem("📄 Copy Official Proof PDF Link", callback=self.copy_proof_pdf)
        self.item_copy_court = rumps.MenuItem("⚖️ Copy Court Authorization Letter Link", callback=self.copy_court_letter)
        self.item_copy_portal = rumps.MenuItem("🔍 Copy Verification Portal Link", callback=self.copy_verify_portal)

        self.item_prof_progress = rumps.MenuItem("📊 Total Progress: Calculating...")
        self.item_prof_remaining = rumps.MenuItem("• Remaining Hours: 56.2h")
        self.item_prof_eta = rumps.MenuItem("• Est. Days to Complete: —")
        self.menu_profile.update([
            self.item_prof_name, self.item_prof_email, self.item_prof_dob,
            self.item_prof_cat, self.item_prof_addr, self.item_prof_id, None,
            self.item_copy_proof, self.item_copy_court, self.item_copy_portal, None,
            self.item_prof_progress, self.item_prof_remaining, self.item_prof_eta
        ])
        
        # ── 6. Live Event History ────────────────────────────────────────────
        self.menu_history = rumps.MenuItem("📜 Live History Stream")
        self.menu_history.add(rumps.MenuItem("No recent events"))
        
        # ── 7. Settings & Configuration Submenu ──────────────────────────────
        self.menu_settings = rumps.MenuItem("⚙️ Settings & Preferences")
        self.item_set_headed = rumps.MenuItem("👁️ Browser Mode Toggle: Headless", callback=self.toggle_headed)
        self.item_set_autostart = rumps.MenuItem("🚀 macOS Start on Login: OFF", callback=self.toggle_autostart)
        self.item_set_watchdog = rumps.MenuItem("🛡️ Watchdog Auto-Restart Toggle: ON", callback=self.toggle_watchdog)
        self.item_set_telegram = rumps.MenuItem("📱 Telegram Notifications: OFF", callback=self.toggle_telegram)
        self.item_set_caffeinate = rumps.MenuItem("☕ Smart Caffeinate: ON", callback=self.toggle_caffeinate)
        self.item_set_display = rumps.MenuItem("📺 Title Display Mode: Auto", callback=self.cycle_display_mode)
        self.item_set_env = rumps.MenuItem("✏️ Edit Credentials (.env)", callback=self.edit_env)
        self.menu_settings.update([
            self.item_set_headed,
            self.item_set_autostart,
            self.item_set_watchdog,
            self.item_set_telegram,
            self.item_set_caffeinate,
            self.item_set_display,
            None,
            self.item_set_env
        ])
        
        # ── 8. Automator Controls ────────────────────────────────────────────
        self.item_bot_toggle = rumps.MenuItem("▶️ Start Automator Engine", callback=self.toggle_bot)
        self.item_open_dashboard = rumps.MenuItem("🌐 Open TFC Dashboard", callback=self.open_dashboard)
        self.item_view_log = rumps.MenuItem("📋 Open Log File", callback=self.open_log)
        self.item_quit_menubar = rumps.MenuItem("Close Menubar Only (bot keeps running)", callback=self.quit_menubar_only)
        self.item_quit_all = rumps.MenuItem("Quit All (Menubar + Bot)", callback=self.quit_all)
        
        # Assemble Menu Layout
        self.menu = [
            self.item_status,
            None,
            self.menu_completed_courses,
            self.menu_queue,
            self.menu_timers,
            self.menu_reflection,
            self.menu_profile,
            self.menu_history,
            self.menu_settings,
            None,
            self.item_bot_toggle,
            self.item_open_dashboard,
            self.item_view_log,
            None,
            self.item_quit_menubar,
            self.item_quit_all,
        ]
        
        # Check start on login state
        self.sync_autostart_ui()
        self.sync_telegram_ui()

        # Start bot before first UI poll — avoids watchdog firing during init
        self.ensure_bot_running_on_start()
        self._watchdog_armed = True

        # Initial state update (instant UI populate)
        try:
            self.update_state(None)
        except Exception:
            pass

    def is_bot_running(self):
        """Check if run_courses.py is currently executing."""
        if self._bot_pid and _pid_alive(self._bot_pid):
            return True
        pid = _read_bot_pid_file()
        if pid:
            if _pid_alive(pid):
                self._bot_pid = pid
                return True
            _remove_bot_pid_file()
        if self._bot_pid and _pid_alive(self._bot_pid):
            return True
        self._bot_pid = None
        for pattern in ("run_courses.py",):
            try:
                res = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True, text=True,
                )
                if res.returncode == 0 and res.stdout.strip():
                    self._bot_pid = int(res.stdout.strip().splitlines()[0])
                    return True
            except Exception:
                pass
        return False

    def stop_bot_process(self, *, user_initiated: bool = True) -> bool:
        """Stop run_courses.py and suppress watchdog restarts."""
        if user_initiated:
            self.user_paused = True
            self._watchdog_suppressed_until = time.time() + 300
        pid = _read_bot_pid_file() or self._bot_pid
        if pid and _pid_alive(pid):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        try:
            subprocess.run(["pkill", "-f", "run_courses.py"], check=False)
            for _ in range(24):
                if not self.is_bot_running():
                    _remove_bot_pid_file()
                    self._bot_pid = None
                    self.update_toggle_button()
                    return True
                time.sleep(0.25)
            subprocess.run(["pkill", "-9", "-f", "run_courses.py"], check=False)
            time.sleep(0.5)
        except Exception:
            pass
        _remove_bot_pid_file()
        self._bot_pid = None
        self.update_toggle_button()
        return not self.is_bot_running()

    def start_bot_process(self):
        """Start run_courses.py process in background."""
        if self.is_bot_running():
            self._last_bot_start_time = time.time()
            return
        self.user_paused = False
        self._watchdog_suppressed_until = 0.0
        env = _subprocess_env()
        if self.headed_mode:
            env["HEADED"] = "1"
        else:
            env.pop("HEADED", None)

        proc = subprocess.Popen(
            ["python3", "-u", SCRIPT_PATH],
            cwd=ROOT_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._bot_pid = proc.pid
        self._last_bot_start_time = time.time()

    def ensure_bot_running_on_start(self):
        """Start bot on menubar launch if not already running."""
        if self.is_bot_running():
            self._last_bot_start_time = time.time()
            pid = self._bot_pid or _read_bot_pid_file() or "?"
            print(
                f"✅ Bot already running (pid {pid}) — background process from a prior session"
            )
            print(
                "   (Menubar Ctrl+C / Close Menubar Only does not stop the bot.)"
            )
            print("   Live log streams below, or: tail -f automation.log")
            self.user_paused = False
            self.update_toggle_button()
            return
        try:
            self.start_bot_process()
            pid = self._bot_pid or _read_bot_pid_file() or "?"
            print(f"✅ Bot started (pid {pid}) — live log streams below")
            rumps.notification(
                "TFC Automator",
                "Automator Engine Started",
                "Coursework bot is running. See automation.log for details.",
            )
        except Exception as e:
            print(f"Could not auto-start bot: {e}")
        self.update_toggle_button()

    def update_toggle_button(self):
        """Sync UI toggle labels with active state."""
        bot_running = self.is_bot_running()
        if bot_running:
            self.item_bot_toggle.title = "⏹️ Stop Automator Engine"
        else:
            self.item_bot_toggle.title = "▶️ Start Automator Engine"

    def toggle_bot(self, _):
        """Start or pause the run_courses.py process."""
        if self.is_bot_running():
            if self.stop_bot_process(user_initiated=True):
                rumps.notification("TFC Automator", "Automator Stopped ⏸️", "Coursework bot fully stopped.")
            else:
                rumps.alert("Could not fully stop the bot. Try Quit All or run: pkill -f run_courses.py")
        else:
            try:
                self.start_bot_process()
                mode_name = "Headed" if self.headed_mode else "Headless"
                rumps.notification("TFC Automator", "Automator Started 🚀", f"Running in {mode_name} mode.")
            except Exception as e:
                rumps.alert(f"Error starting bot: {e}")
        self.update_toggle_button()

    def quit_menubar_only(self, _):
        """Close menubar; leave bot running in background."""
        rumps.quit_application()

    def quit_all(self, _):
        """Stop bot and close menubar."""
        self.stop_bot_process(user_initiated=True)
        rumps.quit_application()

    def toggle_headed(self, _):
        """Toggle browser visibility mode (Headless vs Headed)."""
        self.headed_mode = not self.headed_mode
        if self.headed_mode:
            self.item_set_headed.title = "👁️ Browser Mode Toggle: Headed"
            rumps.notification("TFC Settings", "Browser Mode: Headed", "Future bot runs will display a visible browser window.")
        else:
            self.item_set_headed.title = "👁️ Browser Mode Toggle: Headless"
            rumps.notification("TFC Settings", "Browser Mode: Headless", "Future bot runs will execute in background Headless mode.")
            
        # Restart bot process with new mode if active
        if self.is_bot_running():
            try:
                self.stop_bot_process(user_initiated=False)
                time.sleep(1)
                self.start_bot_process()
            except Exception:
                pass
        self.update_toggle_button()

    def is_autostart_enabled(self):
        """Check if launch agent plist exists."""
        return os.path.exists(LAUNCH_AGENT_PATH)

    def sync_autostart_ui(self):
        """Update menu title for start on login setting."""
        if self.is_autostart_enabled():
            self.item_set_autostart.title = "🚀 macOS Start on Login: ON"
        else:
            self.item_set_autostart.title = "🚀 macOS Start on Login: OFF"

    def toggle_autostart(self, _):
        """Enable or disable start on macOS login via LaunchAgent."""
        if self.is_autostart_enabled():
            try:
                os.remove(LAUNCH_AGENT_PATH)
                rumps.notification("TFC Settings", "Start on Login Disabled", "Automator will no longer launch automatically on login.")
            except Exception as e:
                rumps.alert(f"Failed to remove login item: {e}")
        else:
            try:
                os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
                plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tfc.automator</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{os.path.join(ROOT_DIR, "menubar.py")}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{ROOT_DIR}</string>
</dict>
</plist>
"""
                with open(LAUNCH_AGENT_PATH, "w", encoding="utf-8") as f:
                    f.write(plist_content)
                rumps.notification("TFC Settings", "Start on Login Enabled 🚀", "Menu bar app will launch automatically whenever you log into macOS.")
            except Exception as e:
                rumps.alert(f"Failed to enable start on login: {e}")
        self.sync_autostart_ui()

    def toggle_watchdog(self, _):
        """Toggle lightweight watchdog auto-restart."""
        self.watchdog_enabled = not self.watchdog_enabled
        if self.watchdog_enabled:
            self.item_set_watchdog.title = "🛡️ Watchdog Auto-Restart Toggle: ON"
            rumps.notification("TFC Settings", "Watchdog Active 🛡️", "Lightweight watchdog will auto-restart runner on crash.")
        else:
            self.item_set_watchdog.title = "🛡️ Watchdog Auto-Restart Toggle: OFF"
            rumps.notification("TFC Settings", "Watchdog Disabled", "Automatic crash restart disabled.")

    def sync_telegram_ui(self):
        """Sync Telegram menu label with runtime settings."""
        try:
            import telegram_notify
            telegram_notify.ensure_settings_file()
            self.item_set_telegram.title = telegram_notify.menubar_label()
        except Exception:
            self.item_set_telegram.title = "📱 Telegram: Unavailable"

    def toggle_telegram(self, _):
        """Enable or disable Telegram push notifications."""
        try:
            import telegram_notify
            telegram_notify.ensure_settings_file()
            if not telegram_notify.is_configured():
                rumps.alert(
                    "Telegram not configured",
                    "Add TELEGRAM_BOT_TOKEN and TELEGRAM_ENABLED=1 to .env, then restart the bot.",
                )
                return
            new_state = not telegram_notify.is_enabled()
            telegram_notify.set_enabled(new_state)
            self.sync_telegram_ui()
            if new_state:
                hint = "Message /start to your bot if not linked yet."
                if telegram_notify.is_linked():
                    hint = "Push notifications enabled."
                rumps.notification("TFC Settings", "Telegram ON 📱", hint)
            else:
                rumps.notification(
                    "TFC Settings", "Telegram OFF",
                    "Push disabled. /status and /stats still work while the bot is running.",
                )
        except Exception as e:
            rumps.alert(f"Telegram settings error: {e}")

    def toggle_caffeinate(self, _):
        """Toggle Smart Caffeinate power management setting."""
        caff_on = os.getenv("ENABLE_CAFFEINATE", "1") == "1"
        new_val = "0" if caff_on else "1"
        
        # Update .env file
        env_path = os.path.join(ROOT_DIR, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                content = f.read()
            if "ENABLE_CAFFEINATE=" in content:
                content = re.sub(r"ENABLE_CAFFEINATE=\d", f"ENABLE_CAFFEINATE={new_val}", content)
            else:
                content += f"\nENABLE_CAFFEINATE={new_val}\n"
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(content)
        os.environ["ENABLE_CAFFEINATE"] = new_val

        if new_val == "1":
            self.item_set_caffeinate.title = "☕ Smart Caffeinate: ON"
            rumps.notification("TFC Settings", "Smart Caffeinate Enabled ☕", "Mac stays awake during active coursework. Released during daily limit wait.")
        else:
            self.item_set_caffeinate.title = "☕ Smart Caffeinate: OFF"
            rumps.notification("TFC Settings", "Smart Caffeinate Disabled", "Standard macOS power sleep settings active.")

    def cycle_display_mode(self, _):
        """Cycle through menu bar title display modes."""
        modes = ["auto", "timers", "progress", "full", "minimal"]
        idx = modes.index(self.display_mode)
        self.display_mode = modes[(idx + 1) % len(modes)]
        self.item_set_display.title = f"📺 Title Display Mode: {self.display_mode.capitalize()}"
        rumps.notification("TFC Settings", "Display Mode Changed", f"Menu bar title will now use {self.display_mode} layout.")
        self.update_state(None) # Force UI refresh

    def edit_env(self, _):
        """Open .env file in default text editor."""
        if not os.path.exists(ENV_FILE):
            example_path = os.path.join(ROOT_DIR, ".env.example")
            if os.path.exists(example_path):
                import shutil
                shutil.copy(example_path, ENV_FILE)
        subprocess.run(["open", ENV_FILE])

    def copy_reflection(self, _):
        """Copy current upcoming reflection to macOS system clipboard."""
        if self.upcoming_reflection and "No reflection" not in self.upcoming_reflection:
            try:
                p = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
                p.communicate(input=self.upcoming_reflection.encode('utf-8'))
                rumps.notification("TFC Automator", "Reflection Copied! 📋", "AI reflection essay copied to system clipboard.")
            except Exception as e:
                rumps.alert(f"Failed to copy to clipboard: {e}")

    def _copy_to_clipboard(self, text, title, message):
        try:
            p = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
            p.communicate(input=text.encode('utf-8'))
            rumps.notification("TFC Automator", title, message)
        except Exception as e:
            rumps.alert(f"Failed to copy to clipboard: {e}")

    def copy_proof_pdf(self, _):
        link = self.profile.get("OFFICIAL PROOF PDF LINK") or self.profile.get("proof_pdf_link") or "Link not available yet."
        self._copy_to_clipboard(link, "Proof PDF Copied! 📄", "Official Proof PDF link copied to clipboard.")

    def copy_court_letter(self, _):
        link = self.profile.get("COURT AUTHORIZATION LETTER") or self.profile.get("court_letter_link") or "Link not available yet."
        self._copy_to_clipboard(link, "Court Letter Copied! ⚖️", "Court Authorization Letter link copied to clipboard.")

    def copy_verify_portal(self, _):
        link = self.profile.get("VERIFICATION PORTAL") or self.profile.get("verification_portal_link") or "Link not available yet."
        self._copy_to_clipboard(link, "Portal Link Copied! 🔍", "Verification Portal link copied to clipboard.")

    def open_dashboard(self, _):
        """Open TFC Dashboard in browser."""
        subprocess.run(["open", "https://www.thefoundationofchange.org/dashboard"])

    def open_log(self, _):
        """Open automation.log file."""
        if os.path.exists(LOG_FILE):
            subprocess.run(["open", LOG_FILE])

    def safe_clear_menu(self, menu_item):
        """Safely remove all sub-items from a rumps MenuItem without rumps _menu NSMenu crashes."""
        try:
            for k in list(menu_item.keys()):
                del menu_item[k]
        except Exception:
            pass

    def _scan_log_state(self) -> dict:
        """Parse automation.log tail once; caller caches by mtime."""
        state = {
            "read_timer": "N/A",
            "reflect_timer": "N/A",
            "limit_timer": "N/A",
            "log_limit_wait": False,
            "retrying_after_midnight": False,
            "lesson": "",
            "progress": None,
        }
        if not os.path.exists(LOG_FILE):
            return state
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                for line in reversed(deque(f, maxlen=500)):
                    m_prog = re.search(
                        r"TFC\s+(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)h|(\d+(?:\.\d+)?)h\s*/\s*(\d+(?:\.\d+)?)h",
                        line,
                    )
                    if m_prog and state["progress"] is None:
                        d_val = float(m_prog.group(1) or m_prog.group(3))
                        t_val = float(m_prog.group(2) or m_prog.group(4))
                        if d_val > 0:
                            state["progress"] = (d_val, t_val)

                    if "retrying in 2 minutes" in line or "Reset not updated on site yet" in line:
                        state["retrying_after_midnight"] = True
                    if "[LIMIT_WAIT]" in line or "LIMIT REACHED" in line:
                        state["log_limit_wait"] = True
                        m_rem = re.search(r"⏱\s*(\d+h\s*\d+m)", line)
                        if m_rem:
                            state["limit_timer"] = m_rem.group(1)
                        break
                    if "[READ]" in line or "[REFLECT]" in line:
                        state["log_limit_wait"] = False
                        m_l = re.search(r"Lesson #[^\s']+", line)
                        if m_l:
                            state["lesson"] = m_l.group(0)
                        m_t = re.search(r"⏱\s*(\d+min(?:\s*remaining)?|\d+:\d+|\d+h\s*\d+m)", line)
                        if m_t:
                            parsed_time = (
                                m_t.group(1).replace("min remaining", "m").replace("min", "m").strip()
                            )
                            if "[READ]" in line:
                                state["read_timer"] = parsed_time
                            else:
                                state["reflect_timer"] = parsed_time
                        break
        except Exception:
            pass
        return state

    @rumps.timer(1)
    def update_state(self, _):
        """Polled every 1 second: monitors runner, watchdog, events.jsonl & automation.log."""
        bot_active = self.is_bot_running()
        self.update_toggle_button()

        now_ts = time.time()
        if now_ts - self._telegram_ui_ts >= 15.0:
            self._telegram_ui_ts = now_ts
            self.sync_telegram_ui()
        
        # Watchdog: auto-restart on crash only (not after manual stop)
        in_start_grace = (
            self._last_bot_start_time > 0
            and time.time() - self._last_bot_start_time < BOT_START_GRACE_S
        )
        if (
            self._watchdog_armed
            and self.watchdog_enabled
            and not self.user_paused
            and not bot_active
            and not in_start_grace
            and time.time() >= self._watchdog_suppressed_until
        ):
            now_ts = time.time()
            self._watchdog_restart_times = [
                t for t in self._watchdog_restart_times
                if now_ts - t < WATCHDOG_RESTART_WINDOW_S
            ]
            if len(self._watchdog_restart_times) >= WATCHDOG_MAX_RESTARTS:
                if not self._watchdog_backoff_logged:
                    print(
                        "🛡️ Watchdog: too many restarts — backing off "
                        "(use ▶️ Start or restart menubar)"
                    )
                    self._watchdog_backoff_logged = True
            elif now_ts - self.last_crash_time >= BOT_RESTART_COOLDOWN_S:
                self.last_crash_time = now_ts
                self._watchdog_restart_times.append(now_ts)
                self._watchdog_backoff_logged = False
                print("🛡️ Watchdog: restarting automation engine in background...")
                try:
                    self.start_bot_process()
                    bot_active = True
                    rumps.notification(
                        "TFC Watchdog 🛡️", "Engine Restarted",
                        "Watchdog automatically restarted coursework engine.",
                    )
                except Exception as e:
                    print(f"Watchdog restart failed: {e}")

        # 1. Parse events.jsonl (skip re-read when file unchanged)
        events = self._events_cache
        if os.path.exists(EVENTS_FILE):
            try:
                mtime = os.path.getmtime(EVENTS_FILE)
                if mtime != self._events_mtime:
                    self._events_mtime = mtime
                    loaded = []
                    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                        for line in deque(f, maxlen=500):
                            line = line.strip()
                            if line:
                                try:
                                    ev = json.loads(line)
                                    if ev.get("event") in _EVENTS_SKIP_LOAD:
                                        continue
                                    loaded.append(ev)
                                except Exception:
                                    pass
                    self._events_cache = loaded
                    events = loaded
            except Exception:
                pass
                
        prof_file = os.path.join(ROOT_DIR, "user_profile.json")
        if os.path.exists(prof_file):
            try:
                with open(prof_file, "r", encoding="utf-8") as f:
                    self.profile = json.load(f)
            except Exception:
                pass

        if events:
            # Latest user profile
            for ev in reversed(events):
                if ev.get("event") == "user_profile_loaded":
                    self.profile.update(ev)
                    break
            
            # Latest progress snapshot or hours_done from events
            for ev in reversed(events):
                if ev.get("event") == "progress_snapshot":
                    self.progress = {
                        "done": float(ev.get("done", 0.0)),
                        "total": float(ev.get("total", 75.0)),
                        "remaining": float(ev.get("remaining", 0.0)),
                    }
                    break
                elif "hours_done" in ev:
                    h_d = float(ev["hours_done"])
                    h_tot = float(ev.get("hours_total", 75.0))
                    h_rem = float(ev.get("hours_remaining", max(0.0, h_tot - h_d)))
                    self.progress = {"done": h_d, "total": h_tot, "remaining": h_rem}
                    break
                    
            # Latest AI reflection generated
            for ev in reversed(events):
                if ev.get("event") == "reflection_generated":
                    refl = ev.get("reflection", "")
                    if refl:
                        self.upcoming_reflection = refl
                        self.upcoming_reflection_source = ev.get("source", "")
                        break
                        
            # Latest catalog_snapshot for total site completed
            for ev in reversed(events):
                if ev.get("event") == "catalog_snapshot":
                    self.site_completed = ev.get("done", 0)
                    break
                        
            # Latest timer_sync (or legacy timer_tick); menubar interpolates between syncs
            for ev in reversed(events):
                if ev.get("event") not in ("timer_sync", "timer_tick"):
                    continue
                timer_secs = ev.get("timer_secs", 0)
                phase = ev.get("phase", "")
                lesson_title = ev.get("lesson_title", "")
                if lesson_title:
                    self.current_lesson = lesson_title
                timer_end_at = ev.get("timer_end_at", "")
                if timer_end_at:
                    try:
                        end_ts = datetime.fromisoformat(timer_end_at).timestamp()
                    except Exception:
                        end_ts = time.time() + max(0, int(timer_secs))
                else:
                    end_ts = time.time() + max(0, int(timer_secs))
                if phase == "READ":
                    self._read_timer_end = end_ts
                    self._reflect_timer_end = 0.0
                elif phase == "REFLECT":
                    self._reflect_timer_end = end_ts
                    self._read_timer_end = 0.0
                break

            for ev in reversed(events):
                if ev.get("event") in ("lesson_complete", "daily_limit_wait_start", "daily_limit_hit"):
                    self._read_timer_end = 0.0
                    self._reflect_timer_end = 0.0
                    break
                        
            # Get today's hours if available (date matching today)
            today_str = datetime.now().strftime("%Y-%m-%d")
            self.hours_today = 0.0
            for ev in reversed(events):
                if "hours_today" in ev:
                    ev_date = ev.get("date") or (ev.get("ts", "")[:10] if "ts" in ev else "")
                    if ev_date == today_str:
                        self.hours_today = float(ev["hours_today"])
                        break
                        
            # Event history items formatted in 12-hour local time
            history_items = []
            seen_history = set()

            def _add_history(item: str) -> None:
                if item in seen_history or len(history_items) >= 10:
                    return
                seen_history.add(item)
                history_items.append(item)

            for ev in reversed(events):
                if len(history_items) >= 10:
                    break
                event_name = ev.get("event", "event")
                if event_name in _HISTORY_SKIP_EVENTS:
                    continue

                time_str = format_local_time(ev.get("ts", ""))
                t_prefix = f"[{time_str}] " if time_str else ""

                if event_name == "user_profile_loaded":
                    _add_history(f"{t_prefix}👤 Profile Loaded")
                elif event_name == "progress_snapshot":
                    _add_history(f"{t_prefix}📊 Progress Snapshot ({ev.get('done')}h/{ev.get('total')}h)")
                elif event_name == "lesson_start":
                    _add_history(f"{t_prefix}📖 Started Lesson: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "reading_start":
                    _add_history(f"{t_prefix}📖 Reading: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "reflect_start":
                    _add_history(f"{t_prefix}✍️ Reflecting: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "reflect_submitted":
                    _add_history(f"{t_prefix}📤 Submitted Reflection: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "lesson_complete":
                    _add_history(f"{t_prefix}✅ Completed: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "daily_limit_hit":
                    _add_history(f"{t_prefix}⛔ Daily Limit Hit ({ev.get('hours_today')}h/8.0h)")
                elif event_name == "daily_limit_wait_start":
                    _add_history(f"{t_prefix}🌙 Waiting for Midnight Reset")
                elif event_name == "daily_limit_reset_detected":
                    _add_history(f"{t_prefix}🌅 Midnight Reset Detected")
                elif event_name == "bot_start":
                    _add_history(f"{t_prefix}🚀 Bot Engine Started")

            # Fallback: fill history from automation.log if events are sparse
            if len(history_items) < 5 and os.path.exists(LOG_FILE):
                try:
                    with open(LOG_FILE, "r", encoding="utf-8") as f:
                        for line in deque(f, maxlen=200):
                            if len(history_items) >= 10:
                                break
                            line_str = line.strip()
                            if not line_str:
                                continue
                            ts_match = re.search(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", line_str)
                            t_prefix = f"[{format_local_time(ts_match.group(1))}] " if ts_match else ""
                            
                            if "Reading: " in line_str:
                                m = re.search(r"Reading:\s*'([^']+)'", line_str)
                                title = m.group(1)[:25] if m else "Lesson"
                                item = f"{t_prefix}📖 Reading: {title}"
                                if item not in history_items:
                                    history_items.append(item)
                            elif "Submitted reflection" in line_str or "Submitted: " in line_str:
                                m = re.search(r"Submitted[^:]*:\s*'([^']+)'", line_str)
                                title = m.group(1)[:25] if m else "Lesson"
                                item = f"{t_prefix}📤 Submitted Reflection: {title}"
                                if item not in history_items:
                                    history_items.append(item)
                            elif "DAILY LIMIT REACHED" in line_str or "LIMIT REACHED" in line_str:
                                item = f"{t_prefix}⛔ Daily Limit Hit (8.0h)"
                                if item not in history_items:
                                    history_items.append(item)
                            elif "Limit reset confirmed" in line_str:
                                item = f"{t_prefix}🌅 Midnight Reset Detected"
                                if item not in history_items:
                                    history_items.append(item)
                            elif "TFC Bot v" in line_str:
                                item = f"{t_prefix}🚀 Bot Engine Started"
                                if item not in history_items:
                                    history_items.append(item)
                except Exception:
                    pass
                    
            if history_items and history_items != getattr(self, '_cached_history_items', None):
                self._cached_history_items = list(history_items)
                self.safe_clear_menu(self.menu_history)
                for h in history_items:
                    self.menu_history.add(rumps.MenuItem(h))
                    
        # 1.5 Update Completed Courses UI
        # Bot completed (stored locally in bot_completed_courses.json)
        bot_path = os.path.join(ROOT_DIR, "bot_completed_courses.json")
        bot_courses = []
        bot_count = 0
        if os.path.exists(bot_path):
            try:
                with open(bot_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        bot_courses = data.get("courses", [])
                        bot_count = data.get("count", len(bot_courses))
                    elif isinstance(data, list):
                        bot_courses = data
                        bot_count = len(bot_courses)
            except Exception:
                pass

        # Fallback: scan events.jsonl for lesson_complete events if bot_completed_courses.json is empty
        if not bot_courses and events:
            seen_titles = set()
            for ev in events:
                if ev.get("event") == "lesson_complete" and ev.get("success", False):
                    title = ev.get("lesson_title")
                    if title and title not in seen_titles:
                        seen_titles.add(title)
                        bot_courses.append({
                            "title": title,
                            "url": ev.get("url", ""),
                            "ts": ev.get("ts", "")
                        })
            if bot_courses:
                bot_count = len(bot_courses)
                # Auto-seed bot_completed_courses.json
                try:
                    with open(bot_path, "w", encoding="utf-8") as f:
                        json.dump({"count": bot_count, "courses": bot_courses, "updated": datetime.now().isoformat()}, f, indent=2)
                except Exception:
                    pass

        bot_count = max(bot_count, len(bot_courses))
        self.menu_bot_completed.title = f"🤖 Bot Completed Courses ({bot_count})"
        
        if bot_courses != getattr(self, '_cached_bot_courses', None):
            self._cached_bot_courses = list(bot_courses)
            self.safe_clear_menu(self.menu_bot_completed)
            if bot_count > 0:
                self.menu_bot_completed.add(rumps.MenuItem(f"✅ Finished by Bot: {bot_count}"))
                self.menu_bot_completed.add(None)
                for idx, c in enumerate(bot_courses, 1):
                    if isinstance(c, dict):
                        t = c.get("title", "Unknown")
                        ts = format_local_time(c.get("ts", ""))
                        ts_str = f" [{ts}]" if ts else ""
                    else:
                        t = str(c)
                        ts_str = ""
                    self.menu_bot_completed.add(rumps.MenuItem(f"{idx}. {t}{ts_str}"))
            else:
                self.menu_bot_completed.add(rumps.MenuItem("No courses completed by bot yet"))

        # All completed on site
        all_path = os.path.join(ROOT_DIR, "completed_courses.json")
        all_courses = []
        all_count = 0
        if os.path.exists(all_path):
            try:
                with open(all_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        all_courses = data.get("courses", [])
                        all_count = data.get("count", len(all_courses))
                    elif isinstance(data, list):
                        all_courses = data
                        all_count = len(all_courses)
            except Exception:
                pass
                
        all_count = max(all_count, len(all_courses))
        self.menu_completed_courses.title = f"🎓 Completed Courses on Site ({all_count})"
        self.menu_site_completed.title = f"🌐 All Site Completed ({all_count})"
        
        if all_courses != getattr(self, '_cached_site_courses', None):
            self._cached_site_courses = list(all_courses)
            self.safe_clear_menu(self.menu_site_completed)
            if all_count > 0:
                done_f = float(self.progress.get("done", 0.0))
                total_f = float(self.progress.get("total", 0.0))
                self.menu_site_completed.add(rumps.MenuItem(f"✅ Total Completed: {all_count} ({done_f:.1f} / {total_f:.0f}h)"))
                self.menu_site_completed.add(None)
                for idx, course_title in enumerate(all_courses, 1):
                    t = course_title.get("title", str(course_title)) if isinstance(course_title, dict) else str(course_title)
                    self.menu_site_completed.add(rumps.MenuItem(f"{idx}. {t}"))
            else:
                self.menu_site_completed.add(rumps.MenuItem("No completed courses loaded yet"))

        # 2. Update Profile & Progress UI
        name = self.profile.get("FULL NAME") or self.profile.get("name") or "amanuel hailie"
        email = self.profile.get("EMAIL (READ-ONLY)") or self.profile.get("email") or "tfc@cybershare.tech"
        dob = self.profile.get("DATE OF BIRTH") or self.profile.get("dob") or "2005-10-11"
        reason = self.profile.get("COMMUNITY SERVICE RELATED TO") or self.profile.get("reason") or "Drug Possession"
        addr = self.profile.get("ADDRESS") or self.profile.get("address") or "217 spring ave, rockville, Maryland 20850"
        eid = self.profile.get("ENROLLMENT PROOF ID") or self.profile.get("enrollment_id") or "e70e6763-2e7f-4970-a48b-2a582338f41a"
        
        self.item_prof_name.title = f"• Full Name: {name}"
        self.item_prof_email.title = f"• Email: {email}"
        self.item_prof_dob.title = f"• DOB: {dob}"
        self.item_prof_cat.title = f"• Offense Category: {reason}"
        self.item_prof_addr.title = f"• Location/Address: {addr[:30]}"
        self.item_prof_id.title = f"• Enrollment ID: {eid[:18]}..."
        
        done = self.progress.get("done", 0.0)
        total = self.progress.get("total", 0.0)
        rem = self.progress.get("remaining", 0.0)
        pct = int((done / total) * 100) if total else 0
        
        try:
            hrs_today_f = float(getattr(self, "hours_today", 0.0))
        except (ValueError, TypeError):
            hrs_today_f = 0.0

        filled = int(round((pct / 100) * 10)) if total else 0
        filled = max(0, min(10, filled))
        bar = "█" * filled + "░" * (10 - filled)
        
        self.item_prof_progress.title = f"📊 Total Progress: [{bar}] {pct}% ({done} / {total}h)"
        self.item_prof_remaining.title = f"• Remaining Hours: {rem:.1f}h"
        eta_days = estimate_days_to_complete(rem, hrs_today_f)
        self.item_prof_eta.title = f"• Est. Days to Complete: {format_eta_label(eta_days)}"
        site_done = getattr(self, "site_completed", 0) or all_count
        if site_done:
            self.item_prof_progress.title += f" │ {site_done} courses done"

        # 4. Timers: smooth local countdown; log scan cached by mtime
        read_timer_str = format_timer_remaining(self._read_timer_end)
        submit_timer_str = format_timer_remaining(self._reflect_timer_end)
        limit_timer_str = "N/A"
        is_limit_wait = False
        log_limit_wait = False
        is_retrying_after_midnight = False

        if os.path.exists(LOG_FILE):
            try:
                log_mtime = os.path.getmtime(LOG_FILE)
                if log_mtime != self._log_mtime:
                    self._log_mtime = log_mtime
                    self._log_state = self._scan_log_state()
                log_state = getattr(self, "_log_state", {})
                if log_state.get("progress"):
                    d_val, t_val = log_state["progress"]
                    self.progress["done"] = d_val
                    self.progress["total"] = t_val
                    self.progress["remaining"] = max(0.0, t_val - d_val)
                log_limit_wait = log_state.get("log_limit_wait", False)
                is_retrying_after_midnight = log_state.get("retrying_after_midnight", False)
                limit_timer_str = log_state.get("limit_timer", "N/A")
                if (
                    (not self.current_lesson or self.current_lesson == "None")
                    and log_state.get("lesson")
                ):
                    self.current_lesson = log_state["lesson"]
                if read_timer_str == "N/A":
                    read_timer_str = log_state.get("read_timer", "N/A")
                if submit_timer_str == "N/A":
                    submit_timer_str = log_state.get("reflect_timer", "N/A")
            except Exception:
                pass

        event_limit_wait = False
        is_resuming = False
        if events:
            last_limit_wait = -1
            last_resume = -1
            for idx, ev in enumerate(events):
                ev_name = ev.get("event")
                if ev_name in ("daily_limit_wait_start", "daily_limit_hit"):
                    last_limit_wait = idx
                elif ev_name in ("daily_limit_reset_detected", "reading_start", "lesson_start"):
                    # NOTE: "bot_start" is intentionally NOT included here so logging bot_start DOES NOT clear is_limit_wait
                    last_resume = idx
            
            if last_limit_wait != -1 and last_limit_wait > last_resume:
                event_limit_wait = True
            elif last_resume > last_limit_wait and last_limit_wait != -1:
                is_resuming = True

        try:
            hrs_today_f = float(getattr(self, 'hours_today', 0.0))
        except (ValueError, TypeError):
            hrs_today_f = 0.0

        # Master is_limit_wait determination:
        # Active reading/reflection timer or [READ]/[REFLECT] log overrides limit wait
        if read_timer_str != "N/A" or submit_timer_str != "N/A":
            is_limit_wait = False
            is_retrying_after_midnight = False
        elif hrs_today_f >= 8.0 or log_limit_wait or event_limit_wait:
            is_limit_wait = True
        else:
            is_limit_wait = False

        # Calculate time until local midnight (12:00 AM local time)
        now = datetime.now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        diff_s = max(0, int((midnight - now).total_seconds()))
        h_rem = diff_s // 3600
        m_rem = (diff_s % 3600) // 60
        s_rem = diff_s % 60
        calc_reset_str = f"{h_rem:02d}h {m_rem:02d}m"
        calc_reset_str_secs = f"{h_rem:02d}h {m_rem:02d}m {s_rem:02d}s"

        # Make sure variables are numbers for formatting
        try:
            done_f = float(done)
            total_f = float(total)
            rem_f = float(rem)
        except (ValueError, TypeError):
            done_f = 0.0
            total_f = 0.0
            rem_f = 0.0
            
        try:
            hrs_today_f = float(getattr(self, 'hours_today', 0.0))
        except (ValueError, TypeError):
            hrs_today_f = 0.0

        daily_pct = int(min(100, max(0, (hrs_today_f / 8.0) * 100)))
        daily_filled = int(round((daily_pct / 100.0) * 10))
        daily_filled = max(0, min(10, daily_filled))
        daily_bar = "█" * daily_filled + "░" * (10 - daily_filled)

        # Update Submenu Timer Titles
        if read_timer_str != "N/A":
            self.item_read_timer.title = f"• Reading Timer: {read_timer_str}"
        else:
            self.item_read_timer.title = "• Reading Timer: Inactive"

        if submit_timer_str != "N/A":
            self.item_submit_timer.title = f"• Reflection Submit Timer: {submit_timer_str}"
        else:
            self.item_submit_timer.title = "• Reflection Submit Timer: Inactive"

        if is_limit_wait or is_retrying_after_midnight:
            if is_retrying_after_midnight and diff_s == 0:
                self.item_limit_timer.title = "• Midnight Reset Timer: Retrying every 2 minutes..."
            else:
                self.item_limit_timer.title = f"• Midnight Reset Timer: {calc_reset_str_secs} (12:00 AM)"
        else:
            self.item_limit_timer.title = "• Midnight Reset Timer: Inactive"

        timer_part = ""
        icon = ""
        status_text = ""

        # Update UI according to active state
        if is_limit_wait or is_retrying_after_midnight:
            new_state_id = "LIMIT"
            if is_retrying_after_midnight and diff_s == 0:
                icon = "🔄"
                timer_part = "2m Retry"
                status_text = "Retrying Reset"
            else:
                icon = "🌙"
                timer_part = calc_reset_str
                status_text = "Limit Wait"
                
            self.item_status.title = f"🌙 {status_text}"
            self.item_queue_today.title = f"📅 Logged Today:  [{daily_bar}] {daily_pct}% ({hrs_today_f:.1f} / 8.0h - Limit Hit)"
            
        elif bot_active:
            new_state_id = "ACTIVE"
            icon = "🟢"
            if read_timer_str != "N/A":
                timer_part = read_timer_str
            elif submit_timer_str != "N/A":
                icon = "✍️"
                timer_part = f"Submit {submit_timer_str}"
            
            status_text = "Resuming..." if (is_resuming and read_timer_str == "N/A" and submit_timer_str == "N/A") else "Active"
                
            self.item_status.title = f"🟢 {status_text}"
            self.item_queue_today.title = f"📅 Logged Today:  [{daily_bar}] {daily_pct}% ({hrs_today_f:.1f} / 8.0h)"
            
        else:
            new_state_id = "IDLE"
            icon = "⏸️"
            timer_part = ""
            status_text = "Paused"
            self.item_status.title = "⏸️ Paused"
            self.item_queue_today.title = f"📅 Logged Today:  [{daily_bar}] {daily_pct}% ({hrs_today_f:.1f} / 8.0h)"

        # 3. Update Reflection Draft UI
        self.menu_reflection.title = "📝 Upcoming AI Reflection"
        if self.upcoming_reflection and "No reflection" not in self.upcoming_reflection:
            preview = self.upcoming_reflection[:35] + "..." if len(self.upcoming_reflection) > 35 else self.upcoming_reflection
            src = self.upcoming_reflection_source or "?"
            self.item_refl_preview.title = f"Draft [{src}]: \"{preview}\""
        else:
            self.item_refl_preview.title = "Draft: None ready"



        # Apply Display Mode Title
        prog_part = f"{done_f:.1f}/{total_f:g}h"
        
        display = getattr(self, 'display_mode', 'auto')
        
        lesson_title_trunc = ""
        if bot_active and new_state_id == "ACTIVE" and self.current_lesson and self.current_lesson != "None":
            lt = self.current_lesson.replace("Lesson #", "").strip()
            if len(lt) > 12:
                lt = lt[:11].strip() + "…"
            lesson_title_trunc = f"📖 {lt} • "

        if display == "auto":
            if timer_part:
                if bot_active and lesson_title_trunc:
                    self.title = f"{lesson_title_trunc}{timer_part} | {prog_part}"
                else:
                    self.title = f"{icon} {timer_part} | {prog_part}"
            else:
                self.title = f"{icon} {status_text} | {prog_part}"
                
        elif display == "timers":
            if new_state_id == "LIMIT":
                self.title = f"🌙 Reset in {calc_reset_str_secs}"
            elif new_state_id == "IDLE":
                self.title = "⏸️ Paused"
            else:
                timers = []
                if read_timer_str != "N/A": timers.append(f"📖 {read_timer_str}")
                if submit_timer_str != "N/A": timers.append(f"✍️ Submit {submit_timer_str}")
                if not timers:
                    self.title = "🟢 Active"
                else:
                    self.title = " | ".join(timers)
                    
        elif display == "progress":
            self.title = f"📊 {done_f:.1f}/{total_f:.1f}h ({pct}%) | ⏳ {rem_f:.1f}h Left"
            
        elif display == "full":
            disp_status = timer_part if timer_part else status_text
            if bot_active and lesson_title_trunc and timer_part:
                self.title = f"{lesson_title_trunc}{disp_status} | 📊 {prog_part} ({pct}%) | 📅 {hrs_today_f:.1f}/8h"
            else:
                self.title = f"{icon} {disp_status} | 📊 {prog_part} ({pct}%) | 📅 {hrs_today_f:.1f}/8h"
            
        elif display == "minimal":
            if new_state_id == "IDLE":
                short_timer = "Paused"
            elif new_state_id == "LIMIT":
                if is_retrying_after_midnight and diff_s == 0:
                    short_timer = "Retry"
                else:
                    short_timer = f"{h_rem:02d}h {m_rem:02d}m" if h_rem > 0 else f"{m_rem}m"
            else:
                if read_timer_str != "N/A":
                    short_timer = read_timer_str.split("m")[0] + "m" if "m" in read_timer_str else read_timer_str.split(":")[0] + "m"
                elif submit_timer_str != "N/A":
                    short_timer = submit_timer_str.split("m")[0] + "m" if "m" in submit_timer_str else submit_timer_str.split(":")[0] + "m"
                else:
                    short_timer = "Active"
            self.title = f"{icon} {short_timer}"

        self.item_queue_lesson.title = f"📌 Active Lesson: {self.current_lesson}"
        self.item_read_timer.title = f"• Reading Timer: {read_timer_str}"
        self.item_submit_timer.title = f"• Reflection Submit-Lock Timer: {submit_timer_str}"
        
        if not is_limit_wait and not is_retrying_after_midnight:
             self.item_limit_timer.title = f"• Midnight Reset Timer: {calc_reset_str_secs} (12:00 AM)"

        # Notifications for state transitions (debounced)
        if new_state_id == self.last_state_id:
            self.consecutive_state_count += 1
        else:
            self.consecutive_state_count = 1

        if self.consecutive_state_count >= 3:
            if self.last_notified_state != "INIT" and new_state_id != self.last_notified_state:
                if new_state_id == "ACTIVE":
                    rumps.notification("TFC Automator", "Status: Active 🟢", "Bot has resumed active coursework.")
                elif new_state_id == "LIMIT":
                    rumps.notification("TFC Automator", "Status: Limit Wait 🌙", "Daily limit reached. Waiting for reset.")
                elif new_state_id == "IDLE":
                    rumps.notification("TFC Automator", "Status: Paused ⏸️", "Automator is now idle/paused.")
                self.last_notified_state = new_state_id
        
        self.last_state_id = new_state_id


if __name__ == "__main__":
    enforce_single_instance()
    app = TFCCourseworkMenuApp()
    app.run()
