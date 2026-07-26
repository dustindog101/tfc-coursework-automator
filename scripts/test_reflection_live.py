#!/usr/bin/env python3
"""Live smoke test for reflection chain. Prints inputs and outputs."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run_courses as rc

SAMPLE_TITLE = "Rebuilding Physical Health During Early Recovery"
SAMPLE_BODY = (
    "Early recovery often brings fatigue, poor sleep, and appetite changes. "
    "Gentle exercise and nutrition help rebuild strength over time."
)
SAMPLE_PROMPT = rc.default_reflect_prompt(SAMPLE_TITLE)

SYSTEM_PROMPT = (
    "- Write like a 19yo college student. Min 80 max 295 chars. No em dashes.\n"
    f"Article Title: {SAMPLE_TITLE}\n"
    f"Article Content:\n{SAMPLE_BODY}\n"
    f"Reflection Prompt Question: {SAMPLE_PROMPT}\n"
)


def main():
    print("=" * 60)
    print("INPUT")
    print("=" * 60)
    print("Title:", SAMPLE_TITLE)
    print("Body:", SAMPLE_BODY)
    print("Prompt:", SAMPLE_PROMPT)
    print()
    print("System prompt sent to LLM:")
    print(SYSTEM_PROMPT)
    print()

    print("=" * 60)
    print("TEST 1: agy quota -> opencode fallback")
    print("=" * 60)
    import subprocess
    agy_fail = type("R", (), {
        "returncode": 1, "stdout": "", "stderr": "quota exceeded", "args": ["agy"]
    })()
    with patch.object(subprocess, "run") as mock_run:
        mock_run.side_effect = [
            agy_fail,
            subprocess.run(rc._build_opencode_cmd(SYSTEM_PROMPT)[0],
                           capture_output=True, text=True, timeout=120, cwd=rc.ROOT_DIR),
        ]
        out = rc._run_llm_prompt(SYSTEM_PROMPT)
    print("agy detected quota:", rc._agy_hit_limit(agy_fail))
    print("OUTPUT:", out)
    print("LEN:", len(out) if out else 0)
    print()

    print("=" * 60)
    print("TEST 2: agy + opencode both fail -> hardcoded fallback")
    print("=" * 60)
    with patch.object(rc, "_run_llm_prompt", return_value=None):
        fallback, source = rc.call_agy(SAMPLE_TITLE, SAMPLE_BODY, SAMPLE_PROMPT)
    print("OUTPUT:", fallback)
    print("SOURCE:", source)
    print("IN_FALLBACKS:", fallback in rc._FALLBACKS)
    print()

    print("=" * 60)
    print("TEST 3: live opencode/mimo (no mock)")
    print("=" * 60)
    cmd, tmp = rc._build_opencode_cmd(SYSTEM_PROMPT)
    print("Command:", cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=rc.ROOT_DIR)
        print("Exit code:", result.returncode)
        prose = rc._extract_prose(rc._clean_llm_text(result.stdout))
        print("OUTPUT:", prose)
        print("LEN:", len(prose))
        if result.stderr.strip():
            print("STDERR:", result.stderr[:300])
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    main()
