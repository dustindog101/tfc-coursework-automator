#!/usr/bin/env python3
"""Tests for menubar limit-wait state helpers."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import menubar as mb


class TestMenubarLimitState(unittest.TestCase):
    def test_resume_after_midnight_clears_event_limit_wait(self):
        events = [
            {"event": "daily_limit_wait_start", "ts": "2026-07-28T11:59:03"},
            {"event": "daily_limit_reset_detected", "ts": "2026-07-29T00:00:19"},
            {"event": "daily_limit_wait_complete", "ts": "2026-07-29T00:00:19"},
            {"event": "reading_start", "ts": "2026-07-29T00:00:37"},
            {"event": "timer_sync", "phase": "READ", "timer_secs": 600},
        ]
        self.assertFalse(mb._is_event_limit_wait(events))

    def test_stale_limit_wait_start_does_not_clear_active_timer(self):
        events = [
            {"event": "daily_limit_wait_start", "ts": "2026-07-28T11:59:03"},
            {"event": "reading_start", "ts": "2026-07-29T00:00:37"},
            {"event": "timer_sync", "phase": "READ", "timer_secs": 600},
        ]
        self.assertFalse(mb._should_clear_lesson_timers(events))

    def test_limit_wait_start_after_timer_clears_timers(self):
        events = [
            {"event": "timer_sync", "phase": "READ", "timer_secs": 600},
            {"event": "daily_limit_wait_start", "ts": "2026-07-28T23:00:00"},
        ]
        self.assertTrue(mb._should_clear_lesson_timers(events))

    def test_resolve_limit_wait_while_bot_running(self):
        self.assertTrue(
            mb._resolve_limit_wait_state(
                event_limit_wait=True,
                log_limit_wait=False,
                is_resuming=False,
                read_timer_active=False,
                reflect_timer_active=False,
            )
        )

    def test_active_timer_overrides_limit_wait(self):
        self.assertFalse(
            mb._resolve_limit_wait_state(
                event_limit_wait=True,
                log_limit_wait=True,
                is_resuming=False,
                read_timer_active=True,
                reflect_timer_active=False,
            )
        )

    def test_log_scan_recognizes_reading_not_bracket_read(self):
        log_lines = [
            "2026-07-28 23:52:06,030 [INFO] 🌙 [LIMIT_WAIT] ⏱ 00h 07m remaining\n",
            "2026-07-29 00:00:19,606 [INFO] Limit reset confirmed at 12:00 AM! Resuming coursework...\n",
            "2026-07-29 00:00:37,622 [INFO] 📖 Reading: 'Core Concepts'\n",
        ]
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            f.writelines(log_lines)
            path = f.name
        try:
            old = mb.LOG_FILE
            mb.LOG_FILE = path
            state = mb.TFCCourseworkMenuApp._scan_log_state(object())  # type: ignore[arg-type]
            self.assertFalse(state["log_limit_wait"])
        finally:
            mb.LOG_FILE = old
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
