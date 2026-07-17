"""增量补数（上游停更时的回退路径）：用 baostock 把本地 Qlib 数据从末日补到最新交易日。

背景：主路径 update_daily.py 依赖 chenditc/investment_data 的每日 release。该上游
偶发停更（如连续数日不发版），此时本地日历卡死、流水线在旧日期上空转。本脚本只对
「当前标的池 + 基准指数」按缺失的交易日增量抓取 baostock 原始行情，换算到 investment_data
的存储口径后用 dump_bin 的 dump_update 追加，使系统在上游恢复前仍能出信号、正常运行。

    python update_incremental.py                 # 补 [本地末日+1, 今天] 的缺口
    python update_incremental.py --end 2026-07-09
    python update_incremental.py --dry-run       # 只抓取换算、打印计划，不落盘

为什么能与现有数据无缝衔接（关键）：investment_data 存的是归一化价（$close = 原始价 ×
factor，见下），并非绝对后复权价。因此不能直接套用 fetch_baostock.py 的绝对 hfq 价。
本脚本从本地已有数据里取每只票最后一个有效交易日的 factor0、adjclose/close 比值 A0 与
昨收，把 baostock 的**不复权**原始行情按同一口径换算：

    open/high/low/close = 原始价 × factor0
    volume              = 原始量(股) / (100 × factor0)      # 归一化后的"手"
    amount              = 原始额(元) / 1000                  # investment_data 以千元为单位
    vwap                = (原始额 / 原始量) × factor0
    factor              = factor0                            # 缺口内沿用（无除权时恒定）
    adjclose            = close × A0
    change              = close / 前一有效收盘 - 1

上述换算已在 07-03 重叠日对沪深个股与中证500指数逐字段核验一致（误差 <1e-4）。

局限：缺口区间内若发生除权除息，沿用旧 factor 会有微小偏差；待上游恢复、update_daily
全量原子替换后自动纠正。故本脚本仅作停更期的应急，不改变主路径地位。
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

QUANT = Path(__file__).resolve().parents[1]
CONFIG = QUANT / "configs" / "global.yaml"
QLIB_REPO = QUANT.parent / "qlib"
DUMP_BIN = QLIB_REPO / "scripts" / "dump_bin.py"

# investment_data / qlib cn_data 每只票的字段（必须全部产出，否则 dump_update 逐字段
# 追加会造成同一只票各字段 bin 长度不一致，读取时错位）
FIELDS = ["open", "high", "low", "close", "volume", "amount",
          "factor", "vwap", "change", "adjclose"]
BAOSTOCK_FIELDS = "date,open,high,low,close,volume,amount,tradestatus"


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def _bs_code(symbol: str) -> str:
    """SH600000 -> sh.600000"""
    return f"{symbol[:2].lower()}.{symbol[2:]}"


def local_last_date(data_dir: Path) -> str | None:
    cal = data_dir / "calendars" / "day.txt"
    if not cal.exists():
        return None
    return cal.read_text().strip().splitlines()[-1]


def active_universe(data_dir: Path, benchmark: str, market: str = "all") -> list[str]:
    """当前在册标的池：instruments/<market>.txt 最新时点成分 + 基准指数 + 各账户持仓。

    持仓票未必仍在指数成分；若不纳入，增量补数后 close_prices 盯市会缺价、净值失真。
    """
    inst = data_dir / "instruments" / f"{market}.txt"
    if not inst.exists():
        raise FileNotFoundError(f"缺少成分文件: {inst}")
    rows = [ln.split("\t") for ln in inst.read_text().strip().splitlines()]
    max_end = max(r[2] for r in rows)
    members = {r[0] for r in rows if r[2] == max_end}
    members.add(benchmark)
    # 实盘/研究线持仓（data/accounts/*/nav/holdings.csv）
    acc_root = QUANT / "data" / "accounts"
    if acc_root.exists():
        for hf in acc_root.glob("*/nav/holdings.csv"):
            try:
                for inst_code in pd.read_csv(hf, usecols=["instrument"])["instrument"]:
                    if isinstance(inst_code, str) and len(inst_code) >= 8:
                        members.add(inst_code)
            except Exception:
                pass
    return sorted(members)


def extend_universe_end(data_dir: Path, new_last: str, market: str = "all") -> int:
    """把 instruments/<market>.txt 中最新时点成分的截止日推进到 new_last。"""
    inst = data_dir / "instruments" / f"{market}.txt"
    rows = [ln.split("\t") for ln in inst.read_text().strip().splitlines()]
    max_end = max(r[2] for r in rows)
    n = 0
    for r in rows:
        if r[2] == max_end and new_last > r[2]:
            r[2] = new_last
            n += 1
    inst.write_text("\n".join("\t".join(r) for r in rows) + "\n")
    return n


def carry_constants(data_dir: Path, symbols: list[str], as_of: str) -> dict[str, dict]:
    """从本地已有数据取每只票最后一个有效交易日的换算常量。

    返回 {symbol: {factor0, A0, prev_close, prev_date}}；无有效数据的票不在结果内。
    """
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(data_dir), region="cn")
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    df = D.features(symbols, ["$close", "$factor", "$adjclose"],
                    start_time=start, end_time=as_of)
    out: dict[str, dict] = {}
    if df is None or df.empty:
        return out
    for sym, g in df.groupby(level=0):
        g = g.droplevel(0).dropna(subset=["$close", "$factor"])
        g = g[g["$close"] > 0]
        if g.empty:
            continue
        last = g.iloc[-1]
        close0 = float(last["$close"])
        out[sym] = {
            "factor0": float(last["$factor"]),
            "A0": float(last["$adjclose"]) / close0 if close0 else np.nan,
            "prev_close": close0,
            "prev_date": g.index[-1].strftime("%Y-%m-%d"),
        }
    return out


def fetch_raw(bs, symbol: str, start: str, end: str) -> pd.DataFrame | None:
    """baostock 不复权原始日线 (start, end]，只保留正常交易日。"""
    rs = bs.query_history_k_data_plus(
        _bs_code(symbol), BAOSTOCK_FIELDS,
        start_date=start, end_date=end, frequency="d", adjustflag="3")
    if rs.error_code != "0":
        print(f"  [warn] {symbol} 查询失败: {rs.error_msg}", file=sys.stderr)
        return None
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=BAOSTOCK_FIELDS.split(","))
    num = ["open", "high", "low", "close", "volume", "amount"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    df = df[(df["tradestatus"] == "1") & (df["close"] > 0) & (df["volume"] > 0)]
    return df if not df.empty else None


def to_stored(symbol: str, raw: pd.DataFrame, c: dict) -> pd.DataFrame:
    """把 baostock 原始行情换算到 investment_data 存储口径。"""
    f0, A0, prev = c["factor0"], c["A0"], c["prev_close"]
    close = raw["close"] * f0
    out = pd.DataFrame({
        "date": raw["date"].values,
        "symbol": symbol,
        "open": raw["open"] * f0,
        "high": raw["high"] * f0,
        "low": raw["low"] * f0,
        "close": close,
        "volume": raw["volume"] / (100.0 * f0),
        "amount": raw["amount"] / 1000.0,
        "factor": f0,
        "vwap": (raw["amount"] / raw["volume"]) * f0,
        "adjclose": close * A0,
    })
    # change 为归一化收盘的日收益，首个新日相对本地最后有效收盘
    chain = [prev] + close.tolist()
    out["change"] = [chain[i + 1] / chain[i] - 1 for i in range(len(close))]
    return out[["date", "symbol"] + FIELDS]


def backup(data_dir: Path, symbols: list[str]) -> Path:
    """备份将被追加的 feature 目录 + calendars + instruments，便于回滚。"""
    from qlib.utils import code_to_fname

    bak = data_dir.with_name(data_dir.name + ".incr_bak")
    if bak.exists():
        shutil.rmtree(bak)
    (bak / "features").mkdir(parents=True)
    for sym in symbols:
        fdir = data_dir / "features" / code_to_fname(sym.lower())
        if fdir.exists():
            shutil.copytree(fdir, bak / "features" / fdir.name)
    shutil.copytree(data_dir / "calendars", bak / "calendars")
    shutil.copytree(data_dir / "instruments", bak / "instruments")
    return bak


def verify_bins(data_dir: Path, symbols: list[str]) -> list[str]:
    """校验被追加的每只票 10 个字段 bin 等长（错位=数据损坏）。"""
    from qlib.utils import code_to_fname

    bad = []
    for sym in symbols:
        fdir = data_dir / "features" / code_to_fname(sym.lower())
        sizes = {f: (fdir / f"{f}.day.bin").stat().st_size
                 for f in FIELDS if (fdir / f"{f}.day.bin").exists()}
        if len(set(sizes.values())) > 1:
            bad.append(f"{sym}: {sizes}")
    return bad


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=None, help="补到该日期（含），默认今天")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.05)
    args = parser.parse_args()

    cfg = load_config()
    sys.path.insert(0, str(QUANT / "ops"))
    from ensure_qlib_data import resolve_provider_uri
    data_dir = Path(resolve_provider_uri(cfg["paths"]["qlib_data"]))
    benchmark = cfg["universe"]["benchmark"]
    market = cfg["universe"].get("market", "all")

    old_last = local_last_date(data_dir)
    if old_last is None:
        print("[FATAL] 本地无 calendars/day.txt，请先 init_history.py")
        return 1
    end = args.end or dt.date.today().isoformat()
    fetch_start = (dt.date.fromisoformat(old_last) + dt.timedelta(days=1)).isoformat()
    if fetch_start > end:
        print(f"[OK] 本地数据已到 {old_last}，无缺口（目标 {end}）")
        return 0

    symbols = active_universe(data_dir, benchmark, market=market)
    print(f"[1/5] 标的池 {len(symbols)} 只（market={market}，含基准 {benchmark}），"
          f"补数区间 {fetch_start} ~ {end}")

    consts = carry_constants(data_dir, symbols, old_last)
    print(f"[2/5] 读取换算常量：{len(consts)}/{len(symbols)} 只有本地历史可衔接")

    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        print(f"[FATAL] baostock 登录失败: {lg.error_msg}")
        return 1

    frames, skipped, new_dates = [], [], set()
    try:
        for i, sym in enumerate(symbols):
            c = consts.get(sym)
            if c is None:
                skipped.append(sym)
                continue
            try:
                raw = fetch_raw(bs, sym, fetch_start, end)
            except Exception as e:
                print(f"  [error] {sym}: {e}", file=sys.stderr)
                raw = None
            if raw is None:
                continue
            frames.append(to_stored(sym, raw, c))
            new_dates.update(raw["date"].tolist())
            if (i + 1) % 100 == 0:
                print(f"  进度 {i + 1}/{len(symbols)}（已获 {len(frames)} 只）")
            import time
            time.sleep(args.sleep)
    finally:
        bs.logout()

    if not frames:
        print("[OK] 缺口区间内无新交易日（或全部停牌），无需补数")
        return 0

    new_last = max(new_dates)
    trading_days = sorted(new_dates)
    print(f"[3/5] 抓取完成：{len(frames)} 只 × 新交易日 {trading_days}")
    if skipped:
        print(f"  [warn] {len(skipped)} 只无本地衔接常量、跳过：{skipped[:10]}"
              f"{' ...' if len(skipped) > 10 else ''}")

    if args.dry_run:
        print("[dry-run] 不落盘。样例：")
        print(frames[0].to_string(index=False))
        return 0

    csv_dir = Path(cfg["paths"]["csv_raw"]).expanduser() / "incremental"
    if csv_dir.exists():
        shutil.rmtree(csv_dir)
    csv_dir.mkdir(parents=True)
    touched = []
    for df in frames:
        sym = df["symbol"].iloc[0]
        df.to_csv(csv_dir / f"{sym}.csv", index=False)
        touched.append(sym)

    print(f"[4/5] 备份 {len(touched)} 只 feature + 日历/成分 → 追加 dump_update")
    bak = backup(data_dir, touched)
    cmd = [
        sys.executable, str(DUMP_BIN), "dump_update",
        "--data_path", str(csv_dir),
        "--qlib_dir", str(data_dir),
        "--freq", "day",
        "--date_field_name", "date",
        "--symbol_field_name", "symbol",
        "--exclude_fields", "symbol,date",
    ]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"[FATAL] dump_update 失败（exit {r.returncode}），从 {bak} 回滚")
        _rollback(data_dir, bak)
        return 1

    bad = verify_bins(data_dir, touched)
    if bad:
        print(f"[FATAL] 追加后字段 bin 长度不一致（数据错位），从 {bak} 回滚：")
        for b in bad[:5]:
            print("  ", b)
        _rollback(data_dir, bak)
        return 1

    n_ext = extend_universe_end(data_dir, new_last, market=market)
    now_last = local_last_date(data_dir)
    print(f"[5/5] 完成：日历推进至 {now_last}，{market} 成分截止日更新 {n_ext} 只")
    if now_last != new_last:
        print(f"  [warn] 日历末日 {now_last} 与抓取末日 {new_last} 不一致，请复核")
    return 0


def _rollback(data_dir: Path, bak: Path) -> None:
    from qlib.utils import code_to_fname  # noqa: F401

    for sub in ("calendars", "instruments"):
        shutil.rmtree(data_dir / sub)
        shutil.copytree(bak / sub, data_dir / sub)
    for fdir in (bak / "features").iterdir():
        dst = data_dir / "features" / fdir.name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(fdir, dst)


if __name__ == "__main__":
    sys.exit(main())
