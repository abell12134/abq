"""首次初始化历史数据。

两种模式：
  bundle   下载 Qlib 社区维护的 A 股数据包（含历史时点成分股，无幸存者偏差，推荐）
           之后用 update_daily.py 从数据包截止日增量补到今天。
  baostock 从 baostock 全量抓取当前沪深300成分股 + 指数的历史日线。
           注意：成分股为当前时点名单，存在幸存者偏差，仅作 bundle 不可用时的退路。

用法：
    python init_history.py bundle
    python init_history.py baostock --start 2015-01-01
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quality_check import load_config  # noqa: E402

QLIB_REPO = Path(__file__).resolve().parents[2] / "qlib"
PYTHON = sys.executable


def init_bundle(data_dir: Path) -> int:
    cmd = [
        PYTHON, str(QLIB_REPO / "scripts" / "get_data.py"), "qlib_data",
        "--target_dir", str(data_dir), "--region", "cn",
    ]
    return subprocess.run(cmd).returncode


def init_baostock(data_dir: Path, csv_dir: Path, start: str) -> int:
    import datetime as dt

    import fetch_baostock as fb

    end = dt.date.today().isoformat()
    fb.login()
    try:
        members = fb.get_index_members("sh.000300")
        symbols = members + ["SH000300"]  # 基准指数一并入库
        print(f"[1/2] 全量抓取 {len(symbols)} 只标的 {start} ~ {end}")
        ok, failed = fb.fetch_batch(symbols, start, end, csv_dir)
        print(f"  完成：ok={len(ok)} failed={len(failed)}")
    finally:
        fb.logout()
    if len(ok) < len(symbols) * 0.9:
        print("[FATAL] 抓取失败过多")
        return 1

    print("[2/2] dump_bin 全量入库")
    cmd = [
        PYTHON, str(QLIB_REPO / "scripts" / "dump_bin.py"), "dump_all",
        "--data_path", str(csv_dir),
        "--qlib_dir", str(data_dir),
        "--freq", "day",
        "--date_field_name", "date",
        "--symbol_field_name", "symbol",
        "--exclude_fields", "symbol,date",
    ]
    subprocess.run(cmd, check=True)

    # 生成 csi300 instruments（当前成分，起止日期为全区间——幸存者偏差，见模块说明）
    inst_dir = data_dir / "instruments"
    lines = [f"{s}\t{start}\t{end}" for s in members]
    (inst_dir / "csi300.txt").write_text("\n".join(lines) + "\n")
    print("[OK] 初始化完成（注意：baostock 模式成分股存在幸存者偏差）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["bundle", "baostock"])
    parser.add_argument("--start", default="2015-01-01")
    args = parser.parse_args()

    cfg = load_config()
    data_dir = Path(cfg["paths"]["qlib_data"]).expanduser()
    if args.mode == "bundle":
        return init_bundle(data_dir)
    csv_dir = Path(cfg["paths"]["csv_raw"]).expanduser() / "history"
    return init_baostock(data_dir, csv_dir, args.start)


if __name__ == "__main__":
    sys.exit(main())
