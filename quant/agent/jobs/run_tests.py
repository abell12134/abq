#!/usr/bin/env python3
"""Run agent golden / unit tests. Exit non-zero on failure.

    cd quant && ../quant-venv/bin/python agent/jobs/run_tests.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
TESTS = [
    "agent/tests/test_settle_golden.py",
    "agent/tests/test_gates.py",
    "agent/tests/test_l2_trust.py",
    "agent/tests/test_critic_assert.py",
    "agent/tests/test_recommend_graph.py",
]


def main() -> int:
    py = sys.executable
    failed = 0
    for rel in TESTS:
        path = QUANT / rel
        if not path.exists():
            print(f"[SKIP] missing {rel}")
            continue
        print(f"\n===== {rel} =====")
        r = subprocess.run([py, str(path)], cwd=str(QUANT))
        if r.returncode != 0:
            failed += 1
            print(f"[FAIL] {rel}")
        else:
            print(f"[OK] {rel}")
    print(f"\n=== summary: {len(TESTS) - failed} passed, {failed} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
