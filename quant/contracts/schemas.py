"""层间数据契约：CSV schema 定义 + 带校验的读写工具（含 .done 标记）。

所有跨层 CSV 都应经此模块读写，保证：
  - 列齐全、类型正确；
  - 取值合法（股票代码格式、side 取值、份额非负且整手等）；
  - 写入后生成同名 .done 标记，下游以标记为触发条件，支持断点重跑。

路线一（人工下单）尤其依赖此校验：成交回填是人工录入，schema 校验能
第一时间拦住录错的代码/方向/份额，避免脏数据流入对账与净值。
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

INSTRUMENT_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")
LOT = 100


class SchemaError(ValueError):
    pass


# 每个契约：列 -> 期望 pandas 读入后的 dtype 类别（'str'/'float'/'int'/'date'）
SCHEMAS: dict[str, dict[str, str]] = {
    "signals": {"instrument": "str", "score": "float", "rank": "int"},
    "target_position": {"instrument": "str", "shares": "int", "last_price": "float",
                        "entry_date": "date"},
    "orders": {"instrument": "str", "side": "str", "shares": "int", "ref_price": "float"},
    "fills": {"instrument": "str", "side": "str", "shares": "int", "price": "float",
              "amount": "float", "fee": "float"},
    "holdings": {"instrument": "str", "shares": "int", "last_price": "float",
                 "entry_date": "date"},
    "daily": {"date": "date", "nav": "float", "cash": "float", "position_value": "float",
              "n_pos": "int", "turnover": "float", "daily_ret": "float",
              "bench_ret": "float", "excess_ret": "float"},
    # 个股每日收盘快照（离线，本地 qlib EOD）：close 为真实收盘价（后复权还原），
    # chg_pct 为按复权价算的当日涨跌幅（%）。仅记录价格与涨跌幅，逐日持仓在展示侧现算。
    "positions_daily": {"date": "date", "instrument": "str", "close": "float",
                        "chg_pct": "float"},
}


def _check_values(name: str, df: pd.DataFrame) -> list[str]:
    errs: list[str] = []
    if "instrument" in df.columns:
        bad = df.loc[~df["instrument"].astype(str).str.match(INSTRUMENT_RE), "instrument"]
        if len(bad):
            errs.append(f"非法股票代码: {list(bad.unique())[:5]}")
    if "side" in df.columns:
        bad = set(df["side"].astype(str).str.upper()) - {"BUY", "SELL"}
        if bad:
            errs.append(f"side 取值非法: {bad}")
    if "shares" in df.columns and len(df):
        sh = pd.to_numeric(df["shares"], errors="coerce")
        if (sh < 0).any():
            errs.append("shares 出现负值")
        # 卖出可能因不足整手清仓而非整百，仅对买入强制整手在业务层校验
    return errs


def validate(name: str, df: pd.DataFrame) -> pd.DataFrame:
    if name not in SCHEMAS:
        raise SchemaError(f"未知契约: {name}")
    schema = SCHEMAS[name]
    missing = [c for c in schema if c not in df.columns]
    if missing:
        raise SchemaError(f"[{name}] 缺少列: {missing}（应有 {list(schema)}）")
    errs = _check_values(name, df)
    if errs:
        raise SchemaError(f"[{name}] 取值校验失败: {'; '.join(errs)}")
    # 类型规整
    for col, typ in schema.items():
        if typ == "float":
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        elif typ == "int":
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
        elif typ == "date":
            df[col] = df[col].astype(str).replace({"": None, "nan": None, "NaT": None})
        else:
            df[col] = df[col].astype(str)
    return df[list(schema)]


def write_csv(name: str, df: pd.DataFrame, path: Path, mark_done: bool = True) -> Path:
    df = validate(name, df.copy())
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    if mark_done:
        path.with_suffix(".done").touch()
    return path


def read_csv(name: str, path: Path, require_done: bool = False) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"[{name}] 文件不存在: {path}")
    if require_done and not path.with_suffix(".done").exists():
        raise SchemaError(f"[{name}] 缺少 .done 标记: {path.name}")
    return validate(name, pd.read_csv(path))
