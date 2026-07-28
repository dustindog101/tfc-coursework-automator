#!/usr/bin/env python3
"""Unit tests for LLM fallback chain (no network calls)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import run_courses as rc


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
        rc._last_reflection_logged = ("", "", "")
        with patch.object(rc, "log_event") as mock_log:
            rc.log_reflection_generated("L", "Article", "same text", "agy")
            rc.log_reflection_generated("L", "Article", "same text", "agy")
            self.assertEqual(mock_log.call_count, 1)
        rc._last_reflection_logged = ("", "", "")


class TestLLMChain(unittest.TestCase):
    def test_build_opencode_cmd_always_uses_file(self):
        cmd, tmp = rc._build_opencode_cmd("- starts with dash")
        self.assertIsNotNone(tmp)
        try:
            msg_idx = cmd.index(rc.OPENCODE_USER_MSG)
            file_flag_idx = cmd.index("-f")
            self.assertLess(msg_idx, file_flag_idx)
            self.assertEqual(cmd[file_flag_idx + 1], tmp)
            self.assertIn("opencode/mimo-v2.5-free", cmd)
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

    def test_fallback_is_random_from_four(self):
        self.assertEqual(len(rc._FALLBACKS), 4)
        with patch.object(rc, "_run_llm_prompt", return_value=None):
            out, source = rc.call_agy("Title", "Body", "Prompt?")
        self.assertIn(out, rc._FALLBACKS)
        self.assertEqual(source, "fallback")

    def test_default_reflect_prompt(self):
        prompt = rc.default_reflect_prompt("Addiction Basics")
        self.assertIn("Addiction Basics", prompt)

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
    def test_run_llm_prompt_uses_agy_first(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "a" * 100
        mock_run.return_value.stderr = ""
        out = rc._run_llm_prompt("test prompt")
        self.assertIsNotNone(out)
        text, source = out
        self.assertEqual(source, "agy")
        self.assertEqual(mock_run.call_args[0][0][0], "agy")

    @patch("run_courses.subprocess.run")
    def test_run_llm_prompt_falls_back_to_opencode_on_agy_quota(self, mock_run):
        agy_fail = unittest.mock.Mock(returncode=1, stdout="", stderr="quota exceeded")
        opencode_ok = unittest.mock.Mock(returncode=0, stdout="b" * 100, stderr="")
        mock_run.side_effect = [agy_fail, opencode_ok]
        out = rc._run_llm_prompt("test prompt")
        self.assertIsNotNone(out)
        text, source = out
        self.assertEqual(source, "opencode")
        self.assertEqual(mock_run.call_args_list[1][0][0][0], "opencode")


if __name__ == "__main__":
    unittest.main()
