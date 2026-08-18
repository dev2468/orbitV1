"""Eval harness skeleton — Section 14.7 (P1).

Deliberately minimal: a small fixed set of representative tasks re-run
after any tool/prompt/model change, so "did this change help or hurt" has
an answer instead of a guess. `adk eval` (installed with google-adk) is the
long-term home for this per Section 2's tooling note — this script is the
concrete starting point that exists today, structured so it's easy to
migrate a case at a time.

Usage: python -m eval.run_eval
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orbit.run_task import run_task

TASKS_PATH = Path(__file__).resolve().parent / "tasks.json"


async def run_all() -> int:
    cases = json.loads(TASKS_PATH.read_text())
    results = []
    for case in cases:
        outcome = await run_task(case["title"], case["goal"])
        result_text = str(outcome.get("result", "")).lower()
        missing = [
            needle
            for needle in case["expect_contains"]
            if needle.lower() not in result_text
        ]
        passed = outcome["status"] == "COMPLETED" and not missing
        results.append((case["id"], passed, outcome["status"], missing))

    print("\n=== Eval Results ===")
    n_passed = 0
    for case_id, passed, status, missing in results:
        mark = "PASS" if passed else "FAIL"
        n_passed += passed
        detail = "" if passed else f" (status={status}, missing={missing})"
        print(f"[{mark}] {case_id}{detail}")
    print(f"\n{n_passed}/{len(results)} passed")
    return 0 if n_passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_all()))
