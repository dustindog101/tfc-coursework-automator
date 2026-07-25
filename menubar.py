#!/usr/bin/env python3
"""
TFC Coursework Automator — macOS Menu Bar Status App
Built with rumps (PyObjC native Cocoa NSStatusItem). Lightweight & zero heavy dependencies.

Features:
- Live Menu Bar Title (Current phase & countdown timer)
- Real-time Status & Lesson Tracking
- Active Timer Countdown (Article Reading, Submit-Lock, Daily Limit Reset)
- Upcoming AI Reflection Draft Preview with 1-Click Clipboard Copy
- User Account & Progress Breakdown
- Scrollable Event History with Timestamps
- Bot Process Controller (Start / Stop background runner)
- Quick Links (Open Dashboard in browser, Open log file)
"""

import os
import sys
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
import rumps

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(ROOT_DIR, "events.jsonl")
LOG_FILE = os.path.join(ROOT_DIR, "automation.log")
SCRIPT_PATH = os.path.join(ROOT_DIR, "run_courses.py")

class TFCCourseworkMenuApp(rumps.App):
    def __init__(self):
        super(TFCCourseworkMenuApp, self).__init__(
            name="TFC Bot",
            title="🎓 TFC Bot: Idle",
            quit_button=None
        )
        self.bot_process = None
        self.last_events = []
        self.profile = {}
        self.progress = {"done": 18.8, "total": 75.0, "remaining": 56.2}
        self.current_lesson = "None"
        self.upcoming_reflection = "No reflection generated yet."
        self.daily_limit_reached = False
        self.daily_limit_target = None
        
        # Build initial menu structure
        self.item_status = rumps.MenuItem("📌 Status: Idle", callback=None)
        self.item_lesson = rumps.MenuItem("📚 Current Lesson: None", callback=None)
        
        # Timers Submenu
        self.menu_timers = rumps.MenuItem("⏱️ Active Timers")
        self.item_read_timer = rumps.MenuItem("Article Reading: N/A")
        self.item_submit_timer = rumps.MenuItem("Submit Lock: N/A")
        self.item_limit_timer = rumps.MenuItem("Daily Reset Wait: N/A")
        self.menu_timers.update([self.item_read_timer, self.item_submit_timer, self.item_limit_timer])
        
        # Reflection Submenu
        self.menu_reflection = rumps.MenuItem("📝 Upcoming AI Reflection")
        self.item_refl_preview = rumps.MenuItem("Draft: None")
        self.item_refl_copy = rumps.MenuItem("📋 Copy Reflection to Clipboard", callback=self.copy_reflection)
        self.menu_reflection.update([self.item_refl_preview, self.item_refl_copy])
        
        # Profile Submenu
        self.menu_profile = rumps.MenuItem("👤 Account Profile & Progress")
        self.item_prof_name = rumps.MenuItem("Name: Checking...")
        self.item_prof_email = rumps.MenuItem("Email: Checking...")
        self.item_prof_cat = rumps.MenuItem("Category: Checking...")
        self.item_prof_id = rumps.MenuItem("Enrollment ID: Checking...")
        self.item_prof_today = rumps.MenuItem("Today's Logged: 0.0h / 8.0h")
        self.item_prof_progress = rumps.MenuItem("Overall Progress: 18.8h / 75.0h (25%)")
        self.item_prof_remaining = rumps.MenuItem("Hours Remaining: 56.2h")
        self.menu_profile.update([
            self.item_prof_name, self.item_prof_email, self.item_prof_cat,
            self.item_prof_id, None, self.item_prof_today,
            self.item_prof_progress, self.item_prof_remaining
        ])
        
        # Event History Submenu
        self.menu_history = rumps.MenuItem("📜 Recent Event History")
        self.menu_history.add(rumps.MenuItem("No recent events"))
        
        # Bot Toggle Control
        self.item_bot_toggle = rumps.MenuItem("▶️ Start Automator", callback=self.toggle_bot)
        
        # Quick Utilities
        self.item_open_dashboard = rumps.MenuItem("🌐 Open TFC Dashboard", callback=self.open_dashboard)
        self.item_view_log = rumps.MenuItem("📋 Open Log File (automation.log)", callback=self.open_log)
        self.item_quit = rumps.MenuItem("❌ Quit TFC Menu Bar", callback=rumps.quit_application)
        
        # Assemble Menu
        self.menu = [
            self.item_status,
            self.item_lesson,
            None,
            self.menu_timers,
            self.menu_reflection,
            self.menu_profile,
            self.menu_history,
            None,
            self.item_bot_toggle,
            self.item_open_dashboard,
            self.item_view_log,
            None,
            self.item_quit
        ]
        
        # Check initial bot process status
        self.check_bot_running()
        
    def check_bot_running(self):
        """Check if python3 run_courses.py is running in the background."""
        try:
            res = subprocess.run(["pgrep", "-f", "run_courses.py"], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                self.item_bot_toggle.title = "⏸️ Stop Automator"
                return True
        except Exception:
            pass
        self.item_bot_toggle.title = "▶️ Start Automator"
        return False

    def toggle_bot(self, _):
        """Start or stop the run_courses.py background process."""
        if self.check_bot_running():
            try:
                subprocess.run(["pkill", "-f", "run_courses.py"])
                rumps.notification("TFC Automator", "Bot Stopped", "Coursework automation process has been paused.")
            except Exception as e:
                rumps.alert(f"Error stopping bot: {e}")
        else:
            try:
                env = os.environ.copy()
                env["HEADED"] = "1"
                subprocess.Popen(["python3", "-u", SCRIPT_PATH], cwd=ROOT_DIR, env=env)
                rumps.notification("TFC Automator", "Bot Started", "Coursework automation is running in Headed mode.")
            except Exception as e:
                rumps.alert(f"Error starting bot: {e}")
        self.check_bot_running()

    def copy_reflection(self, _):
        """Copy current upcoming reflection text to macOS system clipboard."""
        if self.upcoming_reflection and "No reflection" not in self.upcoming_reflection:
            try:
                p = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
                p.communicate(input=self.upcoming_reflection.encode('utf-8'))
                rumps.notification("TFC Automator", "Reflection Copied!", "Copied AI reflection text to clipboard.")
            except Exception as e:
                rumps.alert(f"Failed to copy to clipboard: {e}")

    def open_dashboard(self, _):
        """Open Foundation of Change website in browser."""
        subprocess.run(["open", "https://www.thefoundationofchange.org/dashboard"])

    def open_log(self, _):
        """Open automation.log file in TextEdit / Console."""
        if os.path.exists(LOG_FILE):
            subprocess.run(["open", LOG_FILE])

    @rumps.timer(1)
    def update_state(self, _):
        """Polled every 1 second: reads events.jsonl & automation.log for live status."""
        is_running = self.check_bot_running()
        
        # Parse events.jsonl for user profile, history, reflections
        events = []
        if os.path.exists(EVENTS_FILE):
            try:
                with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                events.append(json.loads(line))
                            except Exception:
                                pass
            except Exception:
                pass
                
        if events:
            # Get latest user profile
            for ev in reversed(events):
                if ev.get("event") == "user_profile_loaded":
                    self.profile = ev
                    break
            
            # Get latest progress snapshot
            for ev in reversed(events):
                if ev.get("event") == "progress_snapshot":
                    self.progress = ev
                    break
                    
            # Get latest reflection generated
            for ev in reversed(events):
                if ev.get("event") == "reflection_generated":
                    refl = ev.get("reflection", "")
                    if refl:
                        self.upcoming_reflection = refl
                        break
                        
            # Get last 8 events for history menu
            history_items = []
            for ev in reversed(events[-12:]):
                ts = ev.get("ts", "")
                time_str = ts[11:16] if len(ts) >= 16 else ""
                event_name = ev.get("event", "event")
                
                if event_name == "user_profile_loaded":
                    history_items.append(f"[{time_str}] 👤 Profile Loaded")
                elif event_name == "progress_snapshot":
                    history_items.append(f"[{time_str}] 📊 Progress: {ev.get('done')}h/{ev.get('total')}h")
                elif event_name == "lesson_start":
                    history_items.append(f"[{time_str}] 📖 Started: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "reflection_generated":
                    history_items.append(f"[{time_str}] 📝 AI Reflection Ready ({ev.get('chars')} chars)")
                elif event_name == "lesson_complete":
                    history_items.append(f"[{time_str}] ✅ Completed: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "daily_limit_hit":
                    history_items.append(f"[{time_str}] ⛔ Daily Limit Hit ({ev.get('hours_today')}h)")
                elif event_name == "daily_limit_wait_start":
                    history_items.append(f"[{time_str}] 🌙 Waiting for Midnight Reset")
                elif event_name == "bot_start":
                    history_items.append(f"[{time_str}] 🚀 Bot Started")
                    
            if history_items:
                self.menu_history.clear()
                for h in history_items[:8]:
                    self.menu_history.add(rumps.MenuItem(h))
                    
        # Update Profile Submenu UI
        name = self.profile.get("FULL NAME") or self.profile.get("name") or "Amanuel Hailie"
        email = self.profile.get("EMAIL (READ-ONLY)") or self.profile.get("email") or "tfc@cybershare.tech"
        reason = self.profile.get("COMMUNITY SERVICE RELATED TO") or self.profile.get("reason") or "Drug Possession"
        eid = self.profile.get("ENROLLMENT PROOF ID") or self.profile.get("enrollment_id") or "e70e6763-..."
        
        self.item_prof_name.title = f"• Name: {name}"
        self.item_prof_email.title = f"• Email: {email}"
        self.item_prof_cat.title = f"• Category: {reason}"
        self.item_prof_id.title = f"• Enrollment ID: {eid[:18]}..."
        
        done = self.progress.get("done", 18.8)
        total = self.progress.get("total", 75.0)
        rem = self.progress.get("remaining", 56.2)
        pct = int((done / total) * 100) if total else 0
        
        self.item_prof_progress.title = f"• Overall Progress: {done}h / {total}h ({pct}%)"
        self.item_prof_remaining.title = f"• Hours Remaining: {rem:.1f}h"
        
        # Update Reflection Submenu UI
        if self.upcoming_reflection and "No reflection" not in self.upcoming_reflection:
            preview = self.upcoming_reflection[:45] + "..." if len(self.upcoming_reflection) > 45 else self.upcoming_reflection
            self.item_refl_preview.title = f"Draft: \"{preview}\""
        else:
            self.item_refl_preview.title = "Draft: None ready"
            
        # Parse automation.log for live status line & timers
        status_line = "Idle"
        read_timer_str = "N/A"
        submit_timer_str = "N/A"
        limit_timer_str = "N/A"
        
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    for line in reversed(lines[-50:]):
                        if "[LIMIT_WAIT]" in line or "LIMIT REACHED" in line:
                            self.daily_limit_reached = True
                            m_rem = re.search(r"⏱\s*(\d+h\s*\d+m)", line)
                            if m_rem:
                                limit_timer_str = m_rem.group(1)
                            break
                        if "[READ]" in line or "[REFLECT]" in line:
                            self.daily_limit_reached = False
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

        # Calculate time until local midnight for limit timer fallback
        now = datetime.now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        diff_s = int((midnight - now).total_seconds())
        h_rem = diff_s // 3600
        m_rem = (diff_s % 3600) // 60
        s_rem = diff_s % 60
        calc_reset_str = f"{h_rem:02d}h {m_rem:02d}m {s_rem:02d}s"

        if self.daily_limit_reached:
            status_text = "🌙 Daily Limit Reached (Waiting for Reset)"
            self.title = f"🌙 Reset in {h_rem:02d}h {m_rem:02d}m | {done}/{total}h"
            limit_timer_str = f"{calc_reset_str} (Target: Midnight)"
            self.item_prof_today.title = "• Today's Logged: 8.0h / 8.0h (Limit Reached)"
        elif is_running:
            if read_timer_str != "N/A":
                status_text = f"📖 Reading {self.current_lesson}"
                self.title = f"📖 {read_timer_str} | {done}/{total}h"
            elif submit_timer_str != "N/A":
                status_text = f"✍️ Submitting {self.current_lesson}"
                self.title = f"✍️ Submit {submit_timer_str} | {done}/{total}h"
            else:
                status_text = "🤖 Automator Running..."
                self.title = f"🎓 Bot Active | {done}/{total}h"
            self.item_prof_today.title = "• Today's Logged: Active"
        else:
            status_text = "⏸️ Automator Paused / Idle"
            self.title = f"🎓 TFC Bot: Idle ({done}/{total}h)"
            self.item_prof_today.title = f"• Today's Logged: {done}h / 8.0h"
            
        self.item_status.title = f"📌 Status: {status_text}"
        self.item_lesson.title = f"📚 Current Lesson: {self.current_lesson}"
        self.item_read_timer.title = f"Article Reading Timer: {read_timer_str}"
        self.item_submit_timer.title = f"Submit Lock Timer: {submit_timer_str}"
        self.item_limit_timer.title = f"Daily Limit Reset: {limit_timer_str}"

if __name__ == "__main__":
    app = TFCCourseworkMenuApp()
    app.run()
