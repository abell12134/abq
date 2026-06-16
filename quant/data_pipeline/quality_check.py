"""数据质检：在数据入库（dump_bin）之前运行，不通过则阻断流水线。

原则：宁可当天不更新，不可用错误数据驱动交易。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "global.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def check_csv_dir(csv_dir: Path, cfg: dict | None = None) -> tuple[bool, list[str]]:
    """对抓取产出的 CSV 目录做质检，返回 (是否通过, 问题列表)。"""
    cfg = cfg or load_config()
    qc = cfg["data_quality"]
    problems: list[str] = []
    files = sorted(csv_dir.glob("*.csv"))
    if not files:
        return False, ["CSV 目录为空"]

    n_bad_price = n_bad_return = 0
    for f in files:
        df = pd.read_csv(f)
        required = {"date", "open", "high", "low", "close", "volume", "factor"}
        if not required.issubset(df.columns):
            problems.append(f"{f.name}: 缺少必需列 {required - set(df.columns)}")
            continue
        if (df["close"] <= qc["min_price"]).any():
            n_bad_price += 1
            problems.append(f"{f.name}: 存在非正价格")
        if (df["high"] < df["low"]).any():
            problems.append(f"{f.name}: high < low")
        ret = df["close"].pct_change().abs()
        n_extreme = int((ret > qc["max_abs_return"]).sum())
        if n_extreme > 0:
            # 后复权价在除权日可能跳变，仅当大量出现才视为异常
            if n_extreme > len(df) * 0.01:
                n_bad_return += 1
                problems.append(f"{f.name}: {n_extreme} 个交易日涨跌幅超限")
        dup = df["date"].duplicated().sum()
        if dup:
            problems.append(f"{f.name}: {dup} 个重复日期")

    # 汇总性检查：异常文件比例超阈值则整体不通过
    bad_ratio = (n_bad_price + n_bad_return) / len(files)
    passed = bad_ratio <= cfg["data_quality"]["max_missing_ratio"] and not any(
        "缺少必需列" in p or "high < low" in p or "重复日期" in p for p in problems
    )
    return passed, problems


if __name__ == "__main__":
    import sys

    cfg = load_config()
    csv_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        cfg["paths"]["csv_raw"]).expanduser()
    ok, problems = check_csv_dir(csv_dir)
    for p in problems[:50]:
        print(f"[QC] {p}")
    print(f"[QC] 检查 {csv_dir}: {'通过' if ok else '不通过'}（{len(problems)} 个问题）")
    sys.exit(0 if ok else 1)
