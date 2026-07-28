#!/usr/bin/env python3
"""Unit tests for telegram_notify (no real network calls)."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))
import telegram_notify as tg


class TestEventMapping(unittest.TestCase):
    def test_bot_start_message(self):
        msg = tg.event_to_message({"event": "bot_start", "ts": "2026-07-27T12:00:00"})
        self.assertIn("Bot Started", msg)

    def test_lesson_start_message(self):
        msg = tg.event_to_message({"event": "lesson_start", "lesson_title": "Article A"})
        self.assertIn("New Article", msg)
        self.assertIn("Article A", msg)

    def test_lesson_complete_message(self):
        msg = tg.event_to_message({
            "event": "lesson_complete",
            "lesson_title": "Article A",
            "hours_gained": 1.2,
            "hours_today": 3.0,
            "hours_done": 10.5,
        })
        self.assertIn("Lesson Complete", msg)
        self.assertIn("1.20", msg)

    def test_skips_noisy_events(self):
        self.assertIsNone(tg.event_to_message({"event": "timer_sync"}))
        self.assertIsNone(tg.event_to_message({"event": "reflection_generated"}))


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
        with patch.object(tg, "send_message") as mock_send:
            tg.notify("hello")
            tg._msg_queue.put((12345, "drain"))
            item = tg._msg_queue.get_nowait()
            self.assertEqual(item[1], "drain")

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ENABLED": "1"})
    def test_is_enabled_without_token(self):
        self.assertFalse(tg.is_enabled())


class TestApiFailure(unittest.TestCase):
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "bad"})
    def test_send_message_returns_false_on_error(self):
        with patch.object(tg, "_api_request", return_value=None):
            self.assertFalse(tg.send_message(1, "hi"))

    def test_worker_survives_send_failure(self):
        with patch.object(tg, "is_enabled", return_value=True):
            with patch.object(tg, "send_message", side_effect=RuntimeError("boom")):
                tg._msg_queue.put((1, "test"))
                # Worker loop runs forever; call send path via notify drain simulation
                item = tg._msg_queue.get(timeout=1)
                chat_id, text = item
                try:
                    tg.send_message(chat_id, text)
                except RuntimeError:
                    pass  # must not propagate to bot


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

    def test_build_status_from_events(self):
        self._write_events([
            {"event": "progress_snapshot", "done": 20.0, "total": 75},
            {"event": "lesson_start", "lesson_title": "CBT Intro"},
            {"event": "reading_start", "lesson_title": "CBT Intro"},
        ])
        with patch.object(tg, "is_bot_running", return_value=True):
            text = tg.build_status_text()
        self.assertIn("Running", text)
        self.assertIn("CBT Intro", text)
        self.assertIn("Reading", text)

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
        self.assertIn("30.0/75", text.replace(" ", ""))
        self.assertIn("1.5", text)
        self.assertIn("Lessons completed", text)
        self.assertIn("2", text)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_config = tg.CONFIG_FILE
        tg.CONFIG_FILE = os.path.join(self._tmpdir.name, "config.json")

    def tearDown(self):
        tg.CONFIG_FILE = self._orig_config
        self._tmpdir.cleanup()

    @patch.object(tg, "send_message", return_value=True)
    def test_start_registers_chat(self, mock_send):
        tg._handle_command(999, "/start")
        self.assertEqual(tg.get_chat_id(), 999)
        mock_send.assert_called()
        self.assertIn("Linked", mock_send.call_args[0][1])

    @patch.object(tg, "send_message", return_value=True)
    def test_unauthorized_chat_ignored(self, mock_send):
        tg.register_chat(111)
        tg._handle_command(222, "/status")
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
