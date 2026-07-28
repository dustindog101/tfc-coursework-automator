#!/usr/bin/env python3
"""Unit tests for telegram_notify (no real network calls)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import telegram_notify as tg


class TestEventMapping(unittest.TestCase):
    def test_bot_start_message(self):
        msg = tg.event_to_message({"event": "bot_start", "ts": "2026-07-27T12:00:00"})
        self.assertIn("Bot Started", msg)

    def test_lesson_start_not_standalone_push(self):
        self.assertIsNone(tg.event_to_message({"event": "lesson_start", "lesson_title": "A"}))

    def test_daily_limit_includes_bar(self):
        msg = tg.event_to_message({"event": "daily_limit_hit", "hours_today": 8.0})
        self.assertIn("8.0", msg)
        self.assertIn("█", msg)

    def test_skips_noisy_events(self):
        self.assertIsNone(tg.event_to_message({"event": "timer_sync"}))


class TestLiveState(unittest.TestCase):
    def test_reading_beats_stale_limit_wait(self):
        events = [
            {"event": "daily_limit_hit", "hours_today": 8.0},
            {"event": "daily_limit_wait_start", "hours_today": 8.0},
            {"event": "daily_limit_wait_complete"},
            {"event": "lesson_start", "lesson_title": "New Lesson", "hours_today": 0.5},
            {"event": "reading_start", "lesson_title": "New Lesson", "hours_today": 0.5},
        ]
        state = tg._parse_live_state(events)
        self.assertEqual(state["phase"], "reading")
        self.assertEqual(state["lesson_title"], "New Lesson")

    def test_limit_wait_clears_stale_lesson_fields(self):
        events = [
            {"event": "reflect_start", "lesson_title": "Old", "article_title": "Art"},
            {"event": "reflection_generated", "lesson_title": "Old", "reflection": "draft"},
            {"event": "daily_limit_hit", "hours_today": 8.0, "hours_remaining": 0.0},
            {
                "event": "daily_limit_wait_start",
                "hours_today": 8.0,
                "seconds_until_midnight": 3600,
                "reset_target": (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                                 + timedelta(days=1)).isoformat(),
                "ts": datetime.now().isoformat(),
            },
        ]
        state = tg._parse_live_state(events)
        self.assertEqual(state["phase"], "limit_wait")
        self.assertIsNone(state["lesson_title"])
        self.assertIsNone(state["reflection"])
        with patch.object(tg, "is_bot_running", return_value=True):
            text = tg.build_status_text()
        self.assertNotIn("Old", text)
        self.assertIn("limit reached", text.lower())

    def test_live_reset_counts_down(self):
        target = (datetime.now() + timedelta(hours=2)).isoformat()
        events = [{"event": "daily_limit_wait_start", "reset_target": target}]
        secs = tg._live_reset_seconds(events)
        self.assertIsNotNone(secs)
        self.assertGreater(secs, 3500)
        self.assertLess(secs, 7500)

    def test_limit_wait_uses_limit_hours_not_stale_timer(self):
        events = [
            {"event": "timer_sync", "hours_today": 6.1, "phase": "REFLECT"},
            {"event": "daily_limit_hit", "hours_today": 8.0, "hours_remaining": 0.0},
            {"event": "daily_limit_wait_start", "hours_today": 8.0, "seconds_until_midnight": 44000},
        ]
        state = tg._parse_live_state(events)
        self.assertEqual(state["phase"], "limit_wait")
        self.assertEqual(state["hours_today"], 8.0)
        text = tg.build_status_text()
        self.assertIn("limit reached", text.lower())
        self.assertNotIn("6.1", text)

    def test_lesson_card_includes_reflection(self):
        card = tg._build_lesson_card(
            lesson_title="L1",
            phase="reading",
            hours_today=3.5,
            reflection="This is my draft reflection text.",
            reflection_source="agy",
        )
        self.assertIn("Reflection draft", card)
        self.assertIn("draft reflection", card)
        self.assertIn("3.5", card)


class TestEnabledGating(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_settings = tg.SETTINGS_FILE
        self._orig_config = tg.CONFIG_FILE
        tg.SETTINGS_FILE = os.path.join(self._tmpdir.name, "settings.json")
        tg.CONFIG_FILE = os.path.join(self._tmpdir.name, "config.json")

    def tearDown(self):
        tg.SETTINGS_FILE = self._orig_settings
        tg.CONFIG_FILE = self._orig_config
        self._tmpdir.cleanup()

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ENABLED": "1"})
    def test_notify_noop_when_disabled(self):
        tg.set_enabled(False)
        tg.register_chat(12345)
        tg.notify("hello")
        with self.assertRaises(Exception):
            tg._msg_queue.get_nowait()

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ENABLED": "1"})
    def test_is_enabled_without_token(self):
        self.assertFalse(tg.is_enabled())

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ENABLED": "1"}, clear=False)
    def test_menubar_label_linked_on(self):
        tg.set_enabled(True)
        tg.register_chat(123)
        self.assertIn("ON", tg.menubar_label())


class TestApiFailure(unittest.TestCase):
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "bad"})
    def test_send_message_returns_none_on_error(self):
        with patch.object(tg, "_api_request", return_value=None):
            self.assertIsNone(tg.send_message(1, "hi"))


class TestStatusAndStats(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_events = tg.EVENTS_FILE
        self._orig_pid = tg.BOT_PID_FILE
        self._orig_bot = tg.BOT_COMPLETED_FILE
        tg.EVENTS_FILE = os.path.join(self._tmpdir.name, "events.jsonl")
        tg.BOT_PID_FILE = os.path.join(self._tmpdir.name, "bot.pid")
        tg.BOT_COMPLETED_FILE = os.path.join(self._tmpdir.name, "bot_completed.json")

    def tearDown(self):
        tg.EVENTS_FILE = self._orig_events
        tg.BOT_PID_FILE = self._orig_pid
        tg.BOT_COMPLETED_FILE = self._orig_bot
        self._tmpdir.cleanup()

    def _write_events(self, events):
        with open(tg.EVENTS_FILE, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def test_build_status_shows_reading_not_limit(self):
        self._write_events([
            {"event": "daily_limit_hit", "hours_today": 8.0},
            {"event": "reading_start", "lesson_title": "CBT Intro", "hours_today": 1.0},
        ])
        with patch.object(tg, "is_bot_running", return_value=True):
            text = tg.build_status_text()
        self.assertIn("Reading", text)
        self.assertNotIn("limit wait", text.lower())

    def test_build_stats_from_events(self):
        today = __import__("datetime").date.today().isoformat()
        self._write_events([
            {"event": "progress_snapshot", "done": 30.0, "total": 75, "remaining": 45.0},
            {"event": "lesson_complete", "date": today, "hours_gained": 1.0},
            {"event": "lesson_complete", "date": today, "hours_gained": 0.5},
        ])
        with open(tg.BOT_COMPLETED_FILE, "w", encoding="utf-8") as f:
            json.dump(["Lesson A", "Lesson B"], f)
        with patch.object(tg, "is_bot_running", return_value=False):
            text = tg.build_stats_text()
        self.assertIn("30.0", text)
        self.assertIn("Lessons completed", text)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_config = tg.CONFIG_FILE
        tg.CONFIG_FILE = os.path.join(self._tmpdir.name, "config.json")

    def tearDown(self):
        tg.CONFIG_FILE = self._orig_config
        self._tmpdir.cleanup()

    @patch.object(tg, "send_message", return_value=1)
    def test_start_registers_chat(self, mock_send):
        tg._handle_command(999, "/start")
        self.assertEqual(tg.get_chat_id(), 999)
        mock_send.assert_called()
        self.assertIn("Linked", mock_send.call_args[0][1])

    @patch.object(tg, "send_message", return_value=1)
    def test_unauthorized_chat_ignored(self, mock_send):
        tg.register_chat(111)
        tg._handle_command(222, "/status")
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
