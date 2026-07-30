#!/usr/bin/env python3
"""Unit tests for reflection draft persistence."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import run_courses as rc


class TestReflectionDrafts(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig = rc.REFLECTION_DRAFTS_FILE
        rc.REFLECTION_DRAFTS_FILE = os.path.join(self._tmpdir.name, "drafts.json")
        rc._last_reflection_logged = ("", "", "", "")

    def tearDown(self):
        rc.REFLECTION_DRAFTS_FILE = self._orig
        rc._last_reflection_logged = ("", "", "", "")
        self._tmpdir.cleanup()

    def test_save_load_and_clear(self):
        url = "https://example.org/coursework/abc-123"
        rc.save_reflection_draft(
            url,
            lesson_title="Lesson A",
            article_title="Article A",
            reflection="x" * 100,
            source="opencode",
        )
        loaded = rc.load_reflection_draft(url)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["source"], "opencode")
        self.assertEqual(len(loaded["reflection"]), 100)
        rc.clear_reflection_draft(url)
        self.assertIsNone(rc.load_reflection_draft(url))

    def test_reflect_url_normalizes_key(self):
        base = "https://example.org/coursework/abc-123"
        rc.save_reflection_draft(
            base,
            lesson_title="L",
            article_title="A",
            reflection="y" * 90,
            source="agy",
        )
        self.assertIsNotNone(rc.load_reflection_draft(base + "/reflect"))

    def test_short_reflection_not_saved(self):
        rc.save_reflection_draft(
            "https://example.org/x",
            lesson_title="L",
            article_title="A",
            reflection="too short",
            source="agy",
        )
        self.assertFalse(os.path.exists(rc.REFLECTION_DRAFTS_FILE))

    def test_draft_origin_in_event(self):
        with unittest.mock.patch.object(rc, "log_event") as mock_log:
            rc.log_reflection_generated("L", "A", "z" * 100, "opencode", draft_origin="loaded")
        self.assertEqual(mock_log.call_args[0][0], "reflection_generated")
        self.assertEqual(mock_log.call_args[1]["draft_origin"], "loaded")


if __name__ == "__main__":
    unittest.main()
