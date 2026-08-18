"""纯 pandas 技术指标（无 stockstats/talib 依赖）。

输入：kline 列表 [{date,open,close,high,low,volume}, ...]（升序）
输出：dict——最近一日的 MA5/10/20、MACD、RSI(14)、BOLL(20,2) + 近期涨跌。
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def compute(klines: list[dict[str, Any]]) -> dict[str, Any]:
    if not klines or len(klines) < 5:
        return {"ok": False, "bars": len(klines or [])}
    df = pd.DataFrame(klines)
    for col in ("close", "open", "high", "low", "volume"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if df.empty:
        return {"ok": False, "bars": 0}

    close = df["close"]
    out: dict[str, Any] = {"ok": True, "bars": len(df), "last_date": str(df.iloc[-1]["date"]),
                           "latest": round(float(close.iloc[-1]), 3)}

    # 均线
    for n in (5, 10, 20):
        if len(close) >= n:
            out[f"ma{n}"] = round(float(close.rolling(n).mean().iloc[-1]), 3)
    # 近期涨跌
    for n in (5, 10, 20):
        if len(close) > n:
            out[f"ret_{n}d"] = round(float(close.iloc[-1] / close.iloc[-1 - n] - 1) * 100, 2)

    # MACD(12,26,9)
    if len(close) >= 26:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd = (dif - dea) * 2
        out["macd_dif"] = round(float(dif.iloc[-1]), 3)
        out["macd_dea"] = round(float(dea.iloc[-1]), 3)
        out["macd_hist"] = round(float(macd.iloc[-1]), 3)

    # RSI(14)
    if len(close) >= 15:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rs = gain / loss.replace(0, pd.NA)
        rsi = 100 - 100 / (1 + rs)
        v = rsi.iloc[-1]
        out["rsi14"] = round(float(v), 1) if v == v else None

    # BOLL(20,2)
    if len(close) >= 20:
        mid = close.rolling(20).mean()
        std = close.rolling(20).std()
        out["boll_mid"] = round(float(mid.iloc[-1]), 3)
        out["boll_up"] = round(float(mid.iloc[-1] + 2 * std.iloc[-1]), 3)
        out["boll_low"] = round(float(mid.iloc[-1] - 2 * std.iloc[-1]), 3)

    # 量价：近 5 日均量 / 前 5 日均量
    if "volume" in df and len(df) >= 10:
        vol = df["volume"]
        out["vol_ratio_5d"] = round(
            float(vol.iloc[-5:].mean() / max(vol.iloc[-10:-5].mean(), 1e-9)), 2)

    out["recent_klines"] = [
        {"date": str(r["date"]), "close": round(float(r["close"]), 2),
         "volume": round(float(r.get("volume") or 0), 0)}
        for _, r in df.tail(15).iterrows()
    ]
    return out
