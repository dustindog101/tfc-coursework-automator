#!/usr/bin/env python3
"""
TFC Coursework Automator — Premium macOS Menu Bar App
Built with rumps (PyObjC native Cocoa NSStatusItem). Lightweight & zero heavy dependencies.

Features:
- Live Menu Bar Title (Current phase, lesson & countdown timers)
- Deep Integration with run_courses.py Engine & Event Stream
- Automatic Daily Midnight Reset Detection & 2-Minute Retry Monitoring
- Active Timers: Reading, Submit-Lock, Midnight Reset, Anti-Logout Keep-Alive
- Upcoming AI Reflection Draft Preview with 1-Click Clipboard Copy
- Comprehensive User Account Profile & Coursework Progress Audit
- Real-time Scrollable Event History with Timestamps
- 1-Click Process Controller (Start / Stop Automator)
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
            title="🎓 TFC Bot: Initializing...",
            quit_button=None
        )
        self.profile = {}
        self.progress = {"done": 18.8, "total": 75.0, "remaining": 56.2}
        self.current_lesson = "None"
        self.upcoming_reflection = "No reflection generated yet."
        self.daily_limit_reached = False
        
        # ── 1. Status & Mode Header ──────────────────────────────────────────
        self.item_mode = rumps.MenuItem("⚙️ Mode: Checking...", callback=None)
        self.item_status = rumps.MenuItem("📌 Status: Idle", callback=None)
        self.item_retry = rumps.MenuItem("⏰ Reset Policy: 00:00 midnight with 2m retry", callback=None)
        
        # ── 2. Coursework Queue ──────────────────────────────────────────────
        self.menu_queue = rumps.MenuItem("📚 Coursework Queue & Lesson")
        self.item_queue_lesson = rumps.MenuItem("Active Lesson: None")
        self.item_queue_today = rumps.MenuItem("Logged Today: 0.0h / 8.0h")
        self.menu_queue.update([self.item_queue_lesson, self.item_queue_today])
        
        # ── 3. Active Countdown Timers ───────────────────────────────────────
        self.menu_timers = rumps.MenuItem("⏱️ Active Timers")
        self.item_read_timer = rumps.MenuItem("• Article Reading Timer: N/A")
        self.item_submit_timer = rumps.MenuItem("• Reflection Submit Lock: N/A")
        self.item_limit_timer = rumps.MenuItem("• Daily Midnight Reset: N/A")
        self.item_scroll_timer = rumps.MenuItem("• Keep-Alive Micro-Scroll: Active (every 2.75m)")
        self.menu_timers.update([
            self.item_read_timer, self.item_submit_timer,
            self.item_limit_timer, self.item_scroll_timer
        ])
        
        # ── 4. Upcoming AI Reflection Draft ──────────────────────────────────
        self.menu_reflection = rumps.MenuItem("📝 Upcoming AI Reflection")
        self.item_refl_preview = rumps.MenuItem("Draft: None ready")
        self.item_refl_copy = rumps.MenuItem("📋 Copy Reflection to Clipboard", callback=self.copy_reflection)
        self.menu_reflection.update([self.item_refl_preview, self.item_refl_copy])
        
        # ── 5. User Profile & Account Audit ──────────────────────────────────
        self.menu_profile = rumps.MenuItem("👤 Account Profile & Audit")
        self.item_prof_name = rumps.MenuItem("• Name: Checking...")
        self.item_prof_email = rumps.MenuItem("• Email: Checking...")
        self.item_prof_cat = rumps.MenuItem("• Category: Checking...")
        self.item_prof_addr = rumps.MenuItem("• Address: Checking...")
        self.item_prof_id = rumps.MenuItem("• Enrollment ID: Checking...")
        self.item_prof_today = rumps.MenuItem("• Today's Hours: 0.0h / 8.0h")
        self.item_prof_progress = rumps.MenuItem("• Overall Progress: 18.8h / 75.0h (25%)")
        self.item_prof_remaining = rumps.MenuItem("• Hours Remaining: 56.2h")
        self.menu_profile.update([
            self.item_prof_name, self.item_prof_email, self.item_prof_cat,
            self.item_prof_addr, self.item_prof_id, None,
            self.item_prof_today, self.item_prof_progress, self.item_prof_remaining
        ])
        
        # ── 6. Live Event History ────────────────────────────────────────────
        self.menu_history = rumps.MenuItem("📜 Live Event History")
        self.menu_history.add(rumps.MenuItem("No recent events"))
        
        # ── 7. Automator Controls ────────────────────────────────────────────
        self.item_bot_toggle = rumps.MenuItem("▶️ Start Automator Engine", callback=self.toggle_bot)
        self.item_open_dashboard = rumps.MenuItem("🌐 Open TFC Dashboard", callback=self.open_dashboard)
        self.item_view_log = rumps.MenuItem("📋 Open Log File (automation.log)", callback=self.open_log)
        self.item_open_folder = rumps.MenuItem("📂 Open Project Directory", callback=self.open_folder)
        self.item_quit = rumps.MenuItem("❌ Quit Menu Bar App", callback=rumps.quit_application)
        
        # Assemble Menu Layout
        self.menu = [
            self.item_mode,
            self.item_status,
            self.item_retry,
            None,
            self.menu_queue,
            self.menu_timers,
            self.menu_reflection,
            self.menu_profile,
            self.menu_history,
            None,
            self.item_bot_toggle,
            self.item_open_dashboard,
            self.item_view_log,
            self.item_open_folder,
            None,
            self.item_quit
        ]
        
        # Auto-launch bot process on app start if not already running
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

    def ensure_bot_running_on_start(self):
        """Auto-start the automation engine if it isn't running already."""
        if not self.is_bot_running():
            try:
                env = os.environ.copy()
                env["HEADED"] = "1"
                subprocess.Popen(["python3", "-u", SCRIPT_PATH], cwd=ROOT_DIR, env=env)
                rumps.notification(
                    "TFC Automator",
                    "Automator Engine Auto-Started 🚀",
                    "Coursework bot is running in the background and monitoring daily limits."
                )
            except Exception as e:
                print(f"Could not auto-start bot: {e}")
        self.update_toggle_button()

    def update_toggle_button(self):
        """Sync toggle button label with running state."""
        if self.is_bot_running():
            self.item_bot_toggle.title = "⏸️ Pause Automator Engine"
            self.item_mode.title = "⚙️ Mode: Background Engine Active (Headed)"
        else:
            self.item_bot_toggle.title = "▶️ Start Automator Engine"
            self.item_mode.title = "⚙️ Mode: Automator Engine Idle"

    def toggle_bot(self, _):
        """Start or stop the run_courses.py process."""
        if self.is_bot_running():
            try:
                subprocess.run(["pkill", "-f", "run_courses.py"])
                rumps.notification("TFC Automator", "Automator Paused ⏸️", "Coursework process has been paused.")
            except Exception as e:
                rumps.alert(f"Error stopping bot: {e}")
        else:
            try:
                env = os.environ.copy()
                env["HEADED"] = "1"
                subprocess.Popen(["python3", "-u", SCRIPT_PATH], cwd=ROOT_DIR, env=env)
                rumps.notification("TFC Automator", "Automator Started 🚀", "Coursework automation is actively processing.")
            except Exception as e:
                rumps.alert(f"Error starting bot: {e}")
        self.update_toggle_button()

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

    def open_folder(self, _):
        """Open project root directory in macOS Finder."""
        subprocess.run(["open", ROOT_DIR])

    @rumps.timer(1)
    def update_state(self, _):
        """Polled every 1 second: parses events.jsonl & automation.log for real-time state."""
        bot_active = self.is_bot_running()
        self.update_toggle_button()
        
        # 1. Parse events.jsonl
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
                        
            # Event history items (last 10)
            history_items = []
            for ev in reversed(events[-15:]):
                ts = ev.get("ts", "")
                time_str = ts[11:16] if len(ts) >= 16 else ""
                event_name = ev.get("event", "event")
                
                if event_name == "user_profile_loaded":
                    history_items.append(f"[{time_str}] 👤 Account Profile Loaded")
                elif event_name == "progress_snapshot":
                    history_items.append(f"[{time_str}] 📊 Progress Snapshot ({ev.get('done')}h/{ev.get('total')}h)")
                elif event_name == "lesson_start":
                    history_items.append(f"[{time_str}] 📖 Started Lesson: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "reflection_generated":
                    history_items.append(f"[{time_str}] 📝 AI Reflection Ready ({ev.get('chars')} chars)")
                elif event_name == "lesson_complete":
                    history_items.append(f"[{time_str}] ✅ Completed: {ev.get('lesson_title', 'Lesson')[:25]}")
                elif event_name == "daily_limit_hit":
                    history_items.append(f"[{time_str}] ⛔ Daily Limit Hit ({ev.get('hours_today')}h/8.0h)")
                elif event_name == "daily_limit_wait_start":
                    history_items.append(f"[{time_str}] 🌙 Waiting for Midnight Reset")
                elif event_name == "bot_start":
                    history_items.append(f"[{time_str}] 🚀 Bot Engine Started")
                    
            if history_items:
                self.menu_history.clear()
                for h in history_items[:10]:
                    self.menu_history.add(rumps.MenuItem(h))
                    
        # 2. Update Profile & Progress UI
        name = self.profile.get("FULL NAME") or self.profile.get("name") or "Amanuel Hailie"
        email = self.profile.get("EMAIL (READ-ONLY)") or self.profile.get("email") or "tfc@cybershare.tech"
        reason = self.profile.get("COMMUNITY SERVICE RELATED TO") or self.profile.get("reason") or "Drug Possession"
        addr = self.profile.get("ADDRESS") or self.profile.get("address") or "217 spring ave, rockville"
        eid = self.profile.get("ENROLLMENT PROOF ID") or self.profile.get("enrollment_id") or "e70e6763-..."
        
        self.item_prof_name.title = f"• Name: {name}"
        self.item_prof_email.title = f"• Email: {email}"
        self.item_prof_cat.title = f"• Category: {reason}"
        self.item_prof_addr.title = f"• Address: {addr[:30]}"
        self.item_prof_id.title = f"• Enrollment ID: {eid[:18]}..."
        
        done = self.progress.get("done", 18.8)
        total = self.progress.get("total", 75.0)
        rem = self.progress.get("remaining", 56.2)
        pct = int((done / total) * 100) if total else 0
        
        self.item_prof_progress.title = f"• Overall Progress: {done}h / {total}h ({pct}% Complete)"
        self.item_prof_remaining.title = f"• Hours Remaining: {rem:.1f}h"
        
        # 3. Update Reflection Draft UI
        if self.upcoming_reflection and "No reflection" not in self.upcoming_reflection:
            preview = self.upcoming_reflection[:45] + "..." if len(self.upcoming_reflection) > 45 else self.upcoming_reflection
            self.item_refl_preview.title = f"Draft: \"{preview}\""
        else:
            self.item_refl_preview.title = "Draft: None ready"

        # 4. Parse automation.log for live lesson, phase & timers
        read_timer_str = "N/A"
        submit_timer_str = "N/A"
        limit_timer_str = "N/A"
        is_limit_wait = False
        is_retrying_after_midnight = False

        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    for line in reversed(lines[-50:]):
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

        # Calculate time until local midnight (00:00:00)
        now = datetime.now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        diff_s = max(0, int((midnight - now).total_seconds()))
        h_rem = diff_s // 3600
        m_rem = (diff_s % 3600) // 60
        s_rem = diff_s % 60
        calc_reset_str = f"{h_rem:02d}h {m_rem:02d}m {s_rem:02d}s"

        # Update UI according to active state
        if is_limit_wait or is_retrying_after_midnight:
            if is_retrying_after_midnight and diff_s == 0:
                self.title = f"🔄 Reset Check (2m Retry) | {done}/{total}h"
                self.item_status.title = "📌 Status: 🌙 Midnight Passed — Retrying Site Reset (2m loop)"
                self.item_limit_timer.title = "• Daily Midnight Reset: Retrying every 2 minutes..."
            else:
                self.title = f"🌙 Reset in {h_rem:02d}h {m_rem:02d}m | {done}/{total}h"
                self.item_status.title = "📌 Status: 🌙 Daily Limit Reached (Auto-resuming at 00:00:00)"
                self.item_limit_timer.title = f"• Daily Midnight Reset: {calc_reset_str} (Target: 00:00)"
                
            self.item_prof_today.title = "• Today's Logged: 8.0h / 8.0h (Limit Reached)"
            self.item_queue_today.title = "Logged Today: 8.0h / 8.0h (Daily Limit Reached)"
            self.item_retry.title = "⏰ Reset Policy: Resumes at 00:00 local time (2m retry if delayed)"

        elif bot_active:
            if read_timer_str != "N/A":
                self.title = f"📖 {read_timer_str} | {done}/{total}h"
                self.item_status.title = f"📌 Status: 📖 Reading Article for {self.current_lesson}"
            elif submit_timer_str != "N/A":
                self.title = f"✍️ Submit {submit_timer_str} | {done}/{total}h"
                self.item_status.title = f"📌 Status: ✍️ Submitting Reflection for {self.current_lesson}"
            else:
                self.title = f"🎓 Bot Active | {done}/{total}h"
                self.item_status.title = "📌 Status: 🤖 Automator Running & Monitoring Queue"
                
            self.item_prof_today.title = "• Today's Logged: Active Coursework"
            self.item_queue_today.title = f"Logged Today: Processing ({done}h total)"
            self.item_retry.title = "⏰ Reset Policy: Active Coursework Mode"

        else:
            self.title = f"🎓 TFC Bot: Idle ({done}/{total}h)"
            self.item_status.title = "📌 Status: ⏸️ Automator Paused / Idle"
            self.item_prof_today.title = f"• Today's Logged: {done}h / 8.0h"
            self.item_queue_today.title = f"Logged Today: Paused ({done}h total)"
            self.item_retry.title = "⏰ Reset Policy: 00:00 midnight with 2m retry"

        self.item_queue_lesson.title = f"Active Lesson: {self.current_lesson}"
        self.item_read_timer.title = f"• Article Reading Timer: {read_timer_str}"
        self.item_submit_timer.title = f"• Reflection Submit Lock: {submit_timer_str}"


if __name__ == "__main__":
    app = TFCCourseworkMenuApp()
    app.run()
