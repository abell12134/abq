"""从 baostock 抓取 A 股日线数据，输出 Qlib dump_bin 可用的 CSV。

价格字段使用后复权（hfq），并计算 factor = hfq_close / raw_close，
与 Qlib 官方 cn_data 的格式约定一致。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import baostock as bs
import pandas as pd

FIELDS = "date,code,open,high,low,close,volume,amount,turn,tradestatus"


def _bs_code(symbol: str) -> str:
    """SH600000 -> sh.600000"""
    return f"{symbol[:2].lower()}.{symbol[2:]}"


def _qlib_symbol(bs_code: str) -> str:
    """sh.600000 -> SH600000"""
    ex, num = bs_code.split(".")
    return f"{ex.upper()}{num}"


def login():
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")


def logout():
    bs.logout()


def fetch_one(symbol: str, start: str, end: str) -> pd.DataFrame | None:
    """抓取单只股票 [start, end] 区间日线，返回 qlib 格式 DataFrame。

    symbol: SH600000 / SZ000001 格式；指数同样支持（如 SH000300）。
    """
    code = _bs_code(symbol)
    frames = {}
    # adjustflag: 1=后复权 3=不复权
    for name, adj in (("hfq", "1"), ("raw", "3")):
        rs = bs.query_history_k_data_plus(
            code, FIELDS, start_date=start, end_date=end,
            frequency="d", adjustflag=adj,
        )
        if rs.error_code != "0":
            print(f"  [warn] {symbol} query failed: {rs.error_msg}", file=sys.stderr)
            return None
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=FIELDS.split(","))
        num_cols = ["open", "high", "low", "close", "volume", "amount", "turn"]
        df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")
        frames[name] = df

    hfq, raw = frames["hfq"], frames["raw"]
    if len(hfq) != len(raw):
        merged = hfq.merge(raw[["date", "close"]], on="date", suffixes=("", "_raw"))
    else:
        merged = hfq.copy()
        merged["close_raw"] = raw["close"].values

    # 停牌日 volume=0 且价格可能为 0，剔除无效行
    merged = merged[(merged["close"] > 0) & (merged["close_raw"] > 0)]
    if merged.empty:
        return None

    out = pd.DataFrame({
        "date": merged["date"],
        "symbol": _qlib_symbol(code),
        "open": merged["open"],
        "high": merged["high"],
        "low": merged["low"],
        "close": merged["close"],
        "volume": merged["volume"],
        "amount": merged["amount"],
        "factor": merged["close"] / merged["close_raw"],
    })
    return out


def fetch_batch(symbols: list[str], start: str, end: str, out_dir: Path,
                sleep: float = 0.1) -> tuple[list[str], list[str]]:
    """批量抓取并写出 CSV（每股一个文件，dump_bin 的输入格式）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, failed = [], []
    for i, sym in enumerate(symbols):
        try:
            df = fetch_one(sym, start, end)
        except Exception as e:  # 网络抖动等
            print(f"  [error] {sym}: {e}", file=sys.stderr)
            df = None
        if df is None or df.empty:
            failed.append(sym)
        else:
            df.to_csv(out_dir / f"{sym}.csv", index=False)
            ok.append(sym)
        if (i + 1) % 50 == 0:
            print(f"  progress: {i + 1}/{len(symbols)} (ok={len(ok)} failed={len(failed)})")
        time.sleep(sleep)
    return ok, failed


def get_index_members(index_code: str = "sh.000300") -> list[str]:
    """获取指数最新成分股列表（qlib 符号格式）。"""
    query = {
        "sh.000300": bs.query_hs300_stocks,
        "sh.000905": bs.query_zz500_stocks,
    }[index_code]
    rs = query()
    members = []
    while rs.next():
        row = rs.get_row_data()
        members.append(_qlib_symbol(row[1]))
    return members
