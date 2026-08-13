#!/usr/bin/env python3
"""Emit L1 predictions from signals CSV into the agent ledger."""

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
    p.add_argument("--top-k", type=int, default=None)
    args = p.parse_args()
    from agent.prediction.emit import emit_from_signals
    from agent.core.caliber import TOP_K_EMIT

    res = emit_from_signals(args.day, top_k=args.top_k or TOP_K_EMIT)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
