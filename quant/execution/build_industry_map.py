"""从 baostock 拉取行业分类，生成 data/meta/industry_map.csv。

优先覆盖中证500成分（与策略宇宙一致）；可加 --all-a 扩到全 A（更慢）。

用法：
    python build_industry_map.py
    python build_industry_map.py --all-a
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import baostock as bs
import pandas as pd

QUANT = Path(__file__).resolve().parents[1]
OUT = QUANT / "data" / "meta" / "industry_map.csv"


def _qlib_symbol(bs_code: str) -> str:
    ex, num = bs_code.split(".")
    return f"{ex.upper()}{num}"


def fetch_zz500() -> list[str]:
    rs = bs.query_zz500_stocks()
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    # code is typically sh.600xxx
    codes = []
    for r in rows:
        # fields: updateDate, code, code_name
        if len(r) >= 2:
            codes.append(r[1])
    return codes


def fetch_industry_for(codes: list[str]) -> pd.DataFrame:
    """query_stock_industry 返回全市场行业表；再按 codes 过滤。"""
    rs = bs.query_stock_industry()
    if rs.error_code != "0":
        raise RuntimeError(f"query_stock_industry failed: {rs.error_msg}")
    rows, fields = [], rs.fields
    while rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=fields)
    # 预期列含 code, code_name, industry, industryClassification
    code_col = "code" if "code" in df.columns else df.columns[0]
    ind_col = "industry" if "industry" in df.columns else None
    if ind_col is None:
        for c in df.columns:
            if "industry" in c.lower() and "class" not in c.lower():
                ind_col = c
                break
    if ind_col is None:
        raise RuntimeError(f"无法识别行业列: {list(df.columns)}")
    want = set(codes)
    if want:
        df = df[df[code_col].isin(want)]
    out = pd.DataFrame({
        "instrument": df[code_col].map(_qlib_symbol),
        "industry": df[ind_col].astype(str).str.strip(),
        "name": df["code_name"] if "code_name" in df.columns else "",
    })
    out = out.dropna(subset=["instrument", "industry"])
    out = out[out["industry"] != ""]
    out = out.drop_duplicates("instrument", keep="last")
    return out.sort_values("instrument")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--all-a", action="store_true",
                   help="不按中证500过滤（写入全表，文件更大）")
    args = p.parse_args()

    lg = bs.login()
    if lg.error_code != "0":
        print(f"[FATAL] baostock login: {lg.error_msg}", file=sys.stderr)
        return 1
    try:
        codes = [] if args.all_a else fetch_zz500()
        print(f"[OK] 标的池 {len(codes) if codes else '全市场'}")
        df = fetch_industry_for(codes)
    finally:
        bs.logout()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"[OK] 写入 {OUT}（{len(df)} 行，行业数 {df['industry'].nunique()}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
