#!/usr/bin/env python3
"""Unit tests for LLM fallback chain (no network calls)."""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import run_courses as rc


class TestProgressParsing(unittest.TestCase):
    def test_ignores_daily_limit_first(self):
        body = (
            "Today's Limit\n6.1 / 8 hours\n"
            "Overall Progress\n42.0 / 75 hours\n"
            "33.0 hours remaining"
        )
        prog = rc.parse_progress_from_body(body)
        self.assertAlmostEqual(prog["done"], 42.0)
        self.assertAlmostEqual(prog["total"], 75.0)

    def test_prefers_seventy_five_total(self):
        body = "Progress: 40.9 / 75 hours and 6.1 / 8 hours today"
        prog = rc.parse_progress_from_body(body)
        self.assertAlmostEqual(prog["done"], 40.9)
        self.assertAlmostEqual(prog["total"], 75.0)

    def test_remaining_hours_fallback(self):
        body = "Hours Remaining: 33.0 hours remaining\n6.1 / 8 hours"
        prog = rc.parse_progress_from_body(body)
        self.assertAlmostEqual(prog["done"], 42.0)
        self.assertAlmostEqual(prog["total"], 75.0)


class TestDailyLimitParsing(unittest.TestCase):
    def test_daily_limit_reached(self):
        body = "TODAY'S LIMIT\n0.0h\ndaily limit reached\n31.1 hours remaining"
        self.assertEqual(rc.parse_daily_remaining(body), 0.0)

    def test_remaining_today_not_overall(self):
        body = (
            "Overall Progress\n42.0 / 75 hours\n"
            "31.1 hours remaining\n"
            "TODAY'S LIMIT\n1.9h remaining today (8h max)"
        )
        self.assertAlmostEqual(rc.parse_daily_remaining(body), 1.9)

    def test_overall_remaining_not_used_for_daily(self):
        """Overall '31.1 hours remaining' must not be mistaken for daily hours left."""
        body = "43.9 / 75 hours\n31.1 hours remaining\n6.1 / 8 hours today"
        self.assertIsNone(rc.parse_daily_remaining(body))

    def test_progress_and_daily_parse_independently(self):
        body = (
            "43.9 / 75 hours\n31.1 hours remaining\n"
            "TODAY'S LIMIT\n0.0h remaining today\n daily limit reached"
        )
        prog = rc.parse_progress_from_body(body)
        daily = rc.parse_daily_remaining(body)
        self.assertAlmostEqual(prog["done"], 43.9)
        self.assertEqual(daily, 0.0)


class TestLocalTimer(unittest.TestCase):
    def test_set_and_remaining(self):
        t = rc.LocalTimer()
        t.set(120)
        self.assertGreaterEqual(t.remaining(), 118)
        self.assertLessEqual(t.remaining(), 120)
        self.assertFalse(t.expired())

    def test_expired_when_zero(self):
        t = rc.LocalTimer()
        t.end_ts = rc.time.time() - 1
        self.assertTrue(t.expired())
        self.assertEqual(t.remaining(), 0)

    def test_resync_corrects_large_drift(self):
        t = rc.LocalTimer()
        t.set(60)
        with patch.object(t, "remaining", return_value=60):
            adjusted = t.resync(95, tolerance=15)
        self.assertTrue(adjusted)
        self.assertGreaterEqual(t.remaining(), 93)

    def test_resync_ignores_small_drift(self):
        t = rc.LocalTimer()
        t.set(60)
        with patch.object(t, "remaining", return_value=60):
            adjusted = t.resync(65, tolerance=15)
        self.assertFalse(adjusted)

    def test_end_at_iso(self):
        t = rc.LocalTimer()
        t.set(30)
        self.assertTrue(t.end_at_iso())

    def test_parse_timer_picks_smallest(self):
        body = "Reading 0:30 remaining\nReflect timer 59:45"
        self.assertEqual(rc.parse_timer(body), 30)

    def test_estimate_days_to_complete(self):
        self.assertEqual(rc.estimate_days_to_complete(0), 0)
        self.assertEqual(rc.estimate_days_to_complete(5, hours_today=3.0), 0)
        self.assertEqual(rc.estimate_days_to_complete(39.2, hours_today=8.0), 5)

    def test_log_reflection_generated_dedupes(self):
        rc._last_reflection_logged = ("", "", "", "")
        with patch.object(rc, "log_event") as mock_log:
            rc.log_reflection_generated("L", "Article", "same text", "agy")
            rc.log_reflection_generated("L", "Article", "same text", "agy")
            self.assertEqual(mock_log.call_count, 1)
        rc._last_reflection_logged = ("", "", "", "")


class TestLLMChain(unittest.TestCase):
    def setUp(self):
        rc._AGY_QUOTA_UNTIL = 0.0

    def test_build_opencode_cmd_always_uses_file(self):
        cmd, tmp = rc._build_opencode_cmd("- starts with dash", "opencode/mimo-v2.5-free")
        self.assertIsNotNone(tmp)
        try:
            msg_idx = cmd.index(rc.OPENCODE_USER_MSG)
            file_flag_idx = cmd.index("-f")
            self.assertLess(msg_idx, file_flag_idx)
            self.assertEqual(cmd[file_flag_idx + 1], tmp)
            self.assertIn("opencode/mimo-v2.5-free", cmd)
            self.assertIn("--variant", cmd)
            self.assertIn("minimal", cmd)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def test_agy_limit_detection(self):
        result = type("R", (), {"stdout": "quota exceeded", "stderr": "", "returncode": 1})()
        self.assertTrue(rc._agy_hit_limit(result))
        result2 = type("R", (), {"stdout": "", "stderr": "HTTP 429 Too Many Requests", "returncode": 1})()
        self.assertTrue(rc._agy_hit_limit(result2))
        result3 = type("R", (), {"stdout": "ok", "stderr": "", "returncode": 0})()
        self.assertFalse(rc._agy_hit_limit(result3))
        daily = type("R", (), {"stdout": "", "stderr": "daily limit reached on platform", "returncode": 1})()
        self.assertFalse(rc._agy_hit_limit(daily))

    def test_opencode_error_detection(self):
        err = (
            "\x1b[0m\n> build · deepseek-v4-flash-free\n"
            "\x1b[91m\x1b[1mError:\x1b[0m Failed to execute statement\n"
            'Error: {"name": "UnknownError", "data": {"message": "Unexpected server error"}}'
        )
        result = type("R", (), {"stdout": err, "stderr": "", "returncode": 0})()
        self.assertTrue(rc._opencode_has_error(result))
        ok = type("R", (), {"stdout": "Hi there.", "stderr": "", "returncode": 0})()
        self.assertFalse(rc._opencode_has_error(ok))
        bad_exit = type("R", (), {"stdout": "", "stderr": "fail", "returncode": 1})()
        self.assertTrue(rc._opencode_has_error(bad_exit))

    def test_agy_cooldown_skips_agy_fallback(self):
        rc._AGY_QUOTA_UNTIL = time.time() + 3600
        try:
            with patch("run_courses.subprocess.run") as mock_run:
                mock_run.return_value = type(
                    "R", (), {"returncode": 1, "stdout": "", "stderr": "fail"},
                )()
                out = rc._run_llm_prompt("prompt")
            self.assertIsNone(out)
            self.assertEqual(mock_run.call_args[0][0][0], "opencode")
        finally:
            rc._AGY_QUOTA_UNTIL = 0.0

    def test_fallback_is_random_from_four(self):
        self.assertEqual(len(rc._FALLBACKS), 4)
        with patch.object(rc, "_run_llm_prompt", return_value=None):
            out, source = rc.call_agy("Title", "Body", "Prompt?")
        self.assertIn(out, rc._FALLBACKS)
        self.assertEqual(source, "fallback")

    def test_default_reflect_prompt(self):
        prompt = rc.default_reflect_prompt("Addiction Basics")
        self.assertIn("Addiction Basics", prompt)

    def test_build_reflection_system_prompt_requires_plain_output(self):
        prompt = rc.build_reflection_system_prompt("Title", "Body text", "What did you learn?")
        self.assertIn("ONLY the reflection paragraph", prompt)
        self.assertIn("What did you learn?", prompt)

    def test_llm_output_rejects_meta_text(self):
        meta = (
            "OUTPUT RULES: Write only the reflection.\n"
            "Reflection question: What did you learn?\n"
            "I think the article was helpful and made me think about recovery."
        )
        self.assertTrue(rc._llm_output_is_invalid(meta))

    def test_needs_llm_recheck(self):
        self.assertTrue(rc.needs_llm_recheck("", ""))
        self.assertTrue(rc.needs_llm_recheck("fallback text", "fallback"))
        self.assertFalse(rc.needs_llm_recheck("agy text", "agy"))
        self.assertFalse(rc.needs_llm_recheck("opencode text", "opencode"))

    def test_pre_submit_upgrade_from_fallback(self):
        with patch.object(rc, "call_agy", return_value=("opencode text here" + "x" * 80, "opencode")):
            text, source = rc.try_upgrade_reflection(
                "Title", "Body", "Prompt?", "old fallback text" + "x" * 80, "fallback",
            )
        self.assertEqual(source, "opencode")
        self.assertTrue(text.startswith("opencode text here"))

    def test_pre_submit_keeps_fallback_when_recheck_fails(self):
        with patch.object(rc, "call_agy", return_value=(rc._FALLBACKS[0], "fallback")):
            current = "old fallback text" + "x" * 80
            text, source = rc.try_upgrade_reflection(
                "Title", "Body", "Prompt?", current, "fallback",
            )
        self.assertEqual(source, "fallback")
        self.assertEqual(text, current)

    @patch("run_courses.subprocess.run")
    @patch("run_courses.opencode_models", return_value=["opencode/test-model"])
    def test_run_llm_prompt_uses_opencode_first(self, _models, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "a" * 100
        mock_run.return_value.stderr = ""
        out = rc._run_llm_prompt("test prompt")
        self.assertIsNotNone(out)
        text, source = out
        self.assertEqual(source, "opencode")
        self.assertEqual(mock_run.call_args[0][0][0], "opencode")

    @patch("run_courses.subprocess.run")
    @patch("run_courses.opencode_models", return_value=["opencode/a", "opencode/b"])
    def test_run_llm_prompt_falls_back_to_agy_on_opencode_fail(self, _models, mock_run):
        opencode_fail = unittest.mock.Mock(returncode=1, stdout="", stderr="error")
        agy_ok = unittest.mock.Mock(returncode=0, stdout="b" * 100, stderr="")
        mock_run.side_effect = [opencode_fail, opencode_fail, agy_ok]
        out = rc._run_llm_prompt("test prompt")
        self.assertIsNotNone(out)
        text, source = out
        self.assertEqual(source, "agy")
        self.assertEqual(mock_run.call_args_list[2][0][0][0], "agy")

    @patch("run_courses.subprocess.run")
    @patch("run_courses.opencode_models", return_value=["opencode/a", "opencode/b"])
    def test_run_llm_prompt_tries_second_opencode_model(self, _models, mock_run):
        first_fail = unittest.mock.Mock(returncode=1, stdout="", stderr="server error")
        second_ok = unittest.mock.Mock(returncode=0, stdout="c" * 100, stderr="")
        mock_run.side_effect = [first_fail, second_ok]
        out = rc._run_llm_prompt("test prompt")
        self.assertIsNotNone(out)
        text, source = out
        self.assertEqual(source, "opencode")
        self.assertEqual(mock_run.call_count, 2)

    @patch("run_courses.subprocess.run")
    @patch("run_courses.opencode_models", return_value=["opencode/a", "opencode/b"])
    def test_run_llm_prompt_rejects_opencode_error_text(self, _models, mock_run):
        err = 'Error: Failed to execute statement\nError: {"name": "UnknownError"}'
        mock_run.side_effect = [
            unittest.mock.Mock(returncode=0, stdout=err, stderr=""),
            unittest.mock.Mock(returncode=0, stdout="d" * 100, stderr=""),
        ]
        out = rc._run_llm_prompt("test prompt")
        self.assertIsNotNone(out)
        text, source = out
        self.assertEqual(source, "opencode")
        self.assertEqual(mock_run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
