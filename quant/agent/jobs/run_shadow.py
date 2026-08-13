#!/usr/bin/env python3
"""Emit challenger shadow predictions for paper_tracking factors."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", required=True)
    args = p.parse_args()
    from agent.prediction.shadow_emit import emit_all_shadows

    res = emit_all_shadows(args.day)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
