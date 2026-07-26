#!/usr/bin/env python3
"""
TFC Coursework Automator — Lightweight macOS Menu Bar App
Built with rumps (PyObjC native Cocoa NSStatusItem). Ultra-lightweight (<15MB RAM, 30KB disk).
"""

import os
import sys
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from collections import deque
import rumps

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(ROOT_DIR, "events.jsonl")
LOG_FILE = os.path.join(ROOT_DIR, "automation.log")
SCRIPT_PATH = os.path.join(ROOT_DIR, "run_courses.py")
ENV_FILE = os.path.join(ROOT_DIR, ".env")
LAUNCH_AGENT_PATH = os.path.expanduser("~/Library/LaunchAgents/com.tfc.automator.plist")


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


class TFCCourseworkMenuApp(rumps.App):
    def __init__(self):
        super(TFCCourseworkMenuApp, self).__init__(
            name="TFC Bot",
            title="🎓 TFC Bot: Initializing...",
            quit_button=None
        )
        self.profile = {}
        self.progress = {"done": 18.8, "total": 75.0, "remaining": 56.2}
        self.current_lesson = "None"
        self.upcoming_reflection = "No reflection generated yet."
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
        self.display_mode = "auto"     # auto, timers, progress, full, minimal

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
        
        # ── 5. User Profile & Account Audit ──────────────────────────────────
        self.menu_profile = rumps.MenuItem("👤 User Profile & Audit")
        self.item_prof_name = rumps.MenuItem("• Full Name: Checking...")
        self.item_prof_email = rumps.MenuItem("• Email: Checking...")
        self.item_prof_dob = rumps.MenuItem("• DOB: Checking...")
        self.item_prof_cat = rumps.MenuItem("• Offense Category: Checking...")
        self.item_prof_addr = rumps.MenuItem("• Location/Address: Checking...")
        self.item_prof_id = rumps.MenuItem("• Enrollment ID: Checking...")
        self.item_prof_progress = rumps.MenuItem("📊 Total Progress: [███░░░░░░░] 25% (18.8 / 75.0h)")
        self.item_prof_remaining = rumps.MenuItem("• Remaining Hours: 56.2h")
        self.menu_profile.update([
            self.item_prof_name, self.item_prof_email, self.item_prof_dob,
            self.item_prof_cat, self.item_prof_addr, self.item_prof_id, None,
            self.item_prof_progress, self.item_prof_remaining
        ])
        
        # ── 6. Live Event History ────────────────────────────────────────────
        self.menu_history = rumps.MenuItem("📜 Live History Stream")
        self.menu_history.add(rumps.MenuItem("No recent events"))
        
        # ── 7. Settings & Configuration Submenu ──────────────────────────────
        self.menu_settings = rumps.MenuItem("⚙️ Settings & Preferences")
        self.item_set_headed = rumps.MenuItem("👁️ Browser Mode Toggle: Headless", callback=self.toggle_headed)
        self.item_set_autostart = rumps.MenuItem("🚀 macOS Start on Login: OFF", callback=self.toggle_autostart)
        self.item_set_watchdog = rumps.MenuItem("🛡️ Watchdog Auto-Restart Toggle: ON", callback=self.toggle_watchdog)
        self.item_set_display = rumps.MenuItem("📺 Title Display Mode: Auto", callback=self.cycle_display_mode)
        self.item_set_env = rumps.MenuItem("✏️ Edit Credentials (.env)", callback=self.edit_env)
        self.menu_settings.update([
            self.item_set_headed,
            self.item_set_autostart,
            self.item_set_watchdog,
            self.item_set_display,
            None,
            self.item_set_env
        ])
        
        # ── 8. Automator Controls ────────────────────────────────────────────
        self.item_bot_toggle = rumps.MenuItem("▶️ Start Automator Engine", callback=self.toggle_bot)
        self.item_open_dashboard = rumps.MenuItem("🌐 Open TFC Dashboard", callback=self.open_dashboard)
        self.item_view_log = rumps.MenuItem("📋 Open Log File", callback=self.open_log)
        self.item_quit = rumps.MenuItem("❌ Quit", callback=rumps.quit_application)
        
        # Assemble Menu Layout
        self.menu = [
            self.item_status,
            None,
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
            self.item_quit
        ]
        
        # Check start on login state
        self.sync_autostart_ui()

        # Auto-launch bot process in Headless mode by default
        self.ensure_bot_running_on_start()

    def is_bot_running(self):
        """Check if python3 run_courses.py is currently executing."""
        try:
            res = subprocess.run(["pgrep", "-f", "run_courses.py"], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                return True
        except Exception:
            pass
        return False

    def start_bot_process(self):
        """Start run_courses.py process in background."""
        env = os.environ.copy()
        if self.headed_mode:
            env["HEADED"] = "1"
        else:
            env.pop("HEADED", None)
            
        subprocess.Popen(["python3", "-u", SCRIPT_PATH], cwd=ROOT_DIR, env=env)
        self.user_paused = False

    def ensure_bot_running_on_start(self):
        """Auto-start the automation engine in headless mode by default."""
        if not self.is_bot_running():
            try:
                self.start_bot_process()
                rumps.notification(
                    "TFC Automator",
                    "Automator Engine Started",
                    "Coursework bot is running in background Headless mode."
                )
            except Exception as e:
                print(f"Could not auto-start bot: {e}")
        self.update_toggle_button()

    def update_toggle_button(self):
        """Sync UI toggle labels with active state."""
        bot_running = self.is_bot_running()
        if bot_running:
            self.item_bot_toggle.title = "⏸️ Pause Automator Engine"
        else:
            self.item_bot_toggle.title = "▶️ Start Automator Engine"

    def toggle_bot(self, _):
        """Start or pause the run_courses.py process."""
        if self.is_bot_running():
            try:
                self.user_paused = True
                subprocess.run(["pkill", "-f", "run_courses.py"])
                rumps.notification("TFC Automator", "Automator Paused ⏸️", "Coursework process has been paused.")
            except Exception as e:
                rumps.alert(f"Error pausing bot: {e}")
        else:
            try:
                self.start_bot_process()
                mode_name = "Headed" if self.headed_mode else "Headless"
                rumps.notification("TFC Automator", "Automator Started 🚀", f"Running in {mode_name} mode.")
            except Exception as e:
                rumps.alert(f"Error starting bot: {e}")
        self.update_toggle_button()

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
                subprocess.run(["pkill", "-f", "run_courses.py"])
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

    def open_dashboard(self, _):
        """Open TFC Dashboard in browser."""
        subprocess.run(["open", "https://www.thefoundationofchange.org/dashboard"])

    def open_log(self, _):
        """Open automation.log file."""
        if os.path.exists(LOG_FILE):
            subprocess.run(["open", LOG_FILE])

    @rumps.timer(1)
    def update_state(self, _):
        """Polled every 1 second: monitors runner, watchdog, events.jsonl & automation.log."""
        bot_active = self.is_bot_running()
        self.update_toggle_button()
        
        # Lightweight Watchdog Check (if enabled & not explicitly paused by user)
        if self.watchdog_enabled and not self.user_paused and not bot_active:
            now_ts = time.time()
            if now_ts - self.last_crash_time > 5.0:
                self.last_crash_time = now_ts
                print("🛡️ Watchdog: restarting automation engine in background...")
                try:
                    self.start_bot_process()
                    bot_active = True
                    rumps.notification("TFC Watchdog 🛡️", "Engine Restarted", "Watchdog automatically restarted coursework engine.")
                except Exception as e:
                    print(f"Watchdog restart failed: {e}")

        # 1. Parse events.jsonl
        events = []
        if os.path.exists(EVENTS_FILE):
            try:
                with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                    for line in deque(f, maxlen=500):
                        line = line.strip()
                        if line:
                            try:
                                events.append(json.loads(line))
                            except Exception:
                                pass
            except Exception:
                pass
                
        if events:
            # Latest user profile
            for ev in reversed(events):
                if ev.get("event") == "user_profile_loaded":
                    self.profile = ev
                    break
            
            # Latest progress snapshot
            for ev in reversed(events):
                if ev.get("event") == "progress_snapshot":
                    self.progress = ev
                    break
                    
            # Latest AI reflection generated
            for ev in reversed(events):
                if ev.get("event") == "reflection_generated":
                    refl = ev.get("reflection", "")
                    if refl:
                        self.upcoming_reflection = refl
                        break
                        
            # Get today's hours if available
            for ev in reversed(events):
                if "hours_today" in ev:
                    self.hours_today = float(ev["hours_today"])
                    break
                        
            # Event history items formatted in 12-hour local time
            history_items = []
            for ev in reversed(events[-15:]):
                time_str = format_local_time(ev.get("ts", ""))
                t_prefix = f"[{time_str}] " if time_str else ""
                event_name = ev.get("event", "event")
                
                if event_name == "user_profile_loaded":
                    history_items.append(f"{t_prefix}👤 Profile Loaded")
                elif event_name == "progress_snapshot":
                    history_items.append(f"{t_prefix}📊 Progress Snapshot ({ev.get('done')}h/{ev.get('total')}h)")
                elif event_name == "lesson_start":
                    history_items.append(f"{t_prefix}📖 Started Lesson: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "reflection_generated":
                    history_items.append(f"{t_prefix}📝 AI Reflection Ready ({ev.get('chars')} chars)")
                elif event_name == "lesson_complete":
                    history_items.append(f"{t_prefix}✅ Completed: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "daily_limit_hit":
                    history_items.append(f"{t_prefix}⛔ Daily Limit Hit ({ev.get('hours_today')}h/8.0h)")
                elif event_name == "daily_limit_wait_start":
                    history_items.append(f"{t_prefix}🌙 Waiting for Midnight Reset")
                elif event_name == "bot_start":
                    history_items.append(f"{t_prefix}🚀 Bot Engine Started")
                    
            if history_items:
                self.menu_history.clear()
                for h in history_items[:10]:
                    self.menu_history.add(rumps.MenuItem(h))
                    
        # 2. Update Profile & Progress UI
        name = self.profile.get("FULL NAME") or self.profile.get("name") or "Unknown"
        email = self.profile.get("EMAIL (READ-ONLY)") or self.profile.get("email") or "Unknown"
        dob = self.profile.get("DATE OF BIRTH") or self.profile.get("dob") or "Unknown"
        reason = self.profile.get("COMMUNITY SERVICE RELATED TO") or self.profile.get("reason") or "Unknown"
        addr = self.profile.get("ADDRESS") or self.profile.get("address") or "Unknown"
        eid = self.profile.get("ENROLLMENT PROOF ID") or self.profile.get("enrollment_id") or "Unknown"
        
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
        
        filled = int(round((pct / 100) * 10)) if total else 0
        filled = max(0, min(10, filled))
        bar = "█" * filled + "░" * (10 - filled)
        
        self.item_prof_progress.title = f"📊 Total Progress: [{bar}] {pct}% ({done} / {total}h)"
        self.item_prof_remaining.title = f"• Remaining Hours: {rem:.1f}h"

        # 4. Parse automation.log for live lesson, phase & timers
        read_timer_str = "N/A"
        submit_timer_str = "N/A"
        limit_timer_str = "N/A"
        is_limit_wait = False
        is_retrying_after_midnight = False

        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    for line in reversed(deque(f, maxlen=500)):
                        if "retrying in 2 minutes" in line or "Reset not updated on site yet" in line:
                            is_retrying_after_midnight = True
                        if "[LIMIT_WAIT]" in line or "LIMIT REACHED" in line:
                            is_limit_wait = True
                            m_rem = re.search(r"⏱\s*(\d+h\s*\d+m)", line)
                            if m_rem:
                                limit_timer_str = m_rem.group(1)
                            break
                        if "[READ]" in line or "[REFLECT]" in line:
                            is_limit_wait = False
                            m_l = re.search(r"Lesson #[^\s']+", line)
                            if m_l:
                                self.current_lesson = m_l.group(0)
                            m_t = re.search(r"⏱\s*(\d+:\d+|\d+h\s*\d+m)", line)
                            if m_t:
                                if "[READ]" in line:
                                    read_timer_str = m_t.group(1)
                                else:
                                    submit_timer_str = m_t.group(1)
                            break
            except Exception:
                pass

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
                self.item_limit_timer.title = "• Midnight Reset Timer: Retrying every 2 minutes..."
            else:
                icon = "🌙"
                timer_part = f"Reset in {calc_reset_str}"
                status_text = "Limit Wait"
                self.item_limit_timer.title = f"• Midnight Reset Timer: {calc_reset_str_secs} (12:00 AM)"
                
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
            status_text = "Active"
                
            self.item_status.title = "🟢 Active"
            self.item_queue_today.title = f"📅 Logged Today:  [{daily_bar}] {daily_pct}% ({hrs_today_f:.1f} / 8.0h)"
            
        else:
            new_state_id = "IDLE"
            icon = "⏸️"
            timer_part = ""
            status_text = "Paused"
            self.item_status.title = "⏸️ Paused"
            self.item_queue_today.title = f"📅 Logged Today:  [{daily_bar}] {daily_pct}% ({hrs_today_f:.1f} / 8.0h)"

        # 3. Update Reflection Draft UI
        if is_limit_wait or is_retrying_after_midnight:
            self.menu_reflection.title = "🌙 Limit Wait Active (Auto-generates at 12:00 AM)"
            self.item_refl_preview.title = "Draft: None ready"
        elif self.upcoming_reflection and "No reflection" not in self.upcoming_reflection:
            self.menu_reflection.title = "📝 Drafted Reflection (Copy to Clipboard)"
            preview = self.upcoming_reflection[:35] + "..." if len(self.upcoming_reflection) > 35 else self.upcoming_reflection
            self.item_refl_preview.title = f"Draft: \"{preview}\""
        else:
            self.menu_reflection.title = "📝 Upcoming AI Reflection"
            self.item_refl_preview.title = "Draft: None ready"

        # Apply Display Mode Title
        prog_part = f"{done_f:.1f}/{total_f:g}h"
        
        display = getattr(self, 'display_mode', 'auto')
        
        lesson_title_trunc = ""
        if bot_active and self.current_lesson and self.current_lesson != "None":
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
    app = TFCCourseworkMenuApp()
    app.run()
