#!/usr/bin/env python3
"""Unit tests for LLM fallback chain (no network calls)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import run_courses as rc


class TestLLMChain(unittest.TestCase):
    def test_build_opencode_cmd_always_uses_file(self):
        cmd, tmp = rc._build_opencode_cmd("- starts with dash")
        self.assertIsNotNone(tmp)
        try:
            msg_idx = cmd.index(rc.OPENCODE_USER_MSG)
            file_flag_idx = cmd.index("-f")
            self.assertLess(msg_idx, file_flag_idx)
            self.assertEqual(cmd[file_flag_idx + 1], tmp)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def test_clean_llm_text_strips_fences_and_em_dash(self):
        raw = '```\nhello — world\n```'
        self.assertIn("hello , world", rc._clean_llm_text(raw))

    @patch("run_courses.subprocess.run")
    def test_run_llm_prompt_uses_agy_first(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "a" * 100
        mock_run.return_value.stderr = ""
        out = rc._run_llm_prompt("test prompt")
        self.assertIsNotNone(out)
        self.assertEqual(mock_run.call_args[0][0][0], "agy")

    @patch("run_courses.subprocess.run")
    def test_run_llm_prompt_falls_back_to_opencode(self, mock_run):
        agy_fail = unittest.mock.Mock(returncode=1, stdout="", stderr="quota exceeded")
        opencode_ok = unittest.mock.Mock(
            returncode=0,
            stdout="b" * 100,
            stderr="",
        )
        mock_run.side_effect = [agy_fail, opencode_ok]
        out = rc._run_llm_prompt("test prompt")
        self.assertIsNotNone(out)
        self.assertEqual(mock_run.call_args_list[1][0][0][0], "opencode")


if __name__ == "__main__":
    unittest.main()
