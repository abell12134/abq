"""从实盘 fills 挖掘达标回合 → swing_patterns.yaml（status=live_case）。

用法：
  python overlays/swing_hunter/mine_live_cases.py
  python overlays/swing_hunter/mine_live_cases.py --account live_manual_10k --min-ret 0.10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from overlays.swing_hunter.pattern_mine import (  # noqa: E402
    PATTERNS_PATH,
    mine_from_live_fills,
)
from overlays.swing_hunter.schema import HIT_PCT  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="实盘 fills → 短线猎手 live_case 模式")
    p.add_argument("--account", default="live_manual_10k")
    p.add_argument("--min-ret", type=float, default=HIT_PCT,
                   help="最低收益率门槛（默认 HIT_PCT=0.10）")
    args = p.parse_args()

    written = mine_from_live_fills(account=args.account, min_ret=args.min_ret)
    if not written:
        print(f"[OK] 无新 live_case（已存在或无达标回合）→ {PATTERNS_PATH}")
        return 0
    print(f"[OK] 新写入 {len(written)} 条 live_case → {PATTERNS_PATH}")
    for e in written:
        print(f"  · {e['id']} {e.get('name') or ''} "
              f"{e['buy_day']}→{e['sell_day']} "
              f"ret={e['result_return']*100:+.1f}% tier={e['hit_tier']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
