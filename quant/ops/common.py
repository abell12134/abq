"""阶段4 运维层公共库：配置、Qlib 价格/日历、交易成本、账户现金账本、告警。

所有 ops/execution 脚本共用，避免重复并保证口径一致（费率、价格还原、日历）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import yaml

QUANT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((QUANT / "configs" / "global.yaml").read_text())

DATA = QUANT / "data"
LOG_DIR = DATA / "logs"
NAV_DIR = DATA / "nav"
ACCOUNT_FILE = NAV_DIR / "account.json"
ALERT_LOG = LOG_DIR / "alerts.log"

_QLIB_READY = False


def data_path(*parts) -> Path:
    return DATA.joinpath(*parts)


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def account_config(account: str | None) -> dict:
    """全局配置 + 账户 profile 覆盖。account=None 时返回全局配置。"""
    if not account:
        return CFG
    p = QUANT / "configs" / "accounts" / f"{account}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"未知账户 profile: {account} ({p})")
    cfg = _merge(CFG, yaml.safe_load(p.read_text()))
    cfg.setdefault("account", {})["name"] = account
    return cfg


def account_dir(account: str | None) -> Path:
    """账户运行时数据根目录。None 为旧版单账户目录兼容模式。"""
    return DATA if not account else DATA / "accounts" / account


def account_path(account: str | None, *parts: str) -> Path:
    return account_dir(account).joinpath(*parts)


def account_subdirs(account: str | None) -> dict[str, Path]:
    root = account_dir(account)
    if not account:
        return {
            "orders": DATA / "orders",
            "fills": DATA / "fills",
            "target_position": DATA / "target_position",
            "nav": DATA / "nav",
            "reports": DATA / "reports",
        }
    return {
        "orders": root / "orders",
        "fills": root / "fills",
        "target_position": root / "target_position",
        "nav": root / "nav",
        "reports": root / "reports",
    }


def ensure_account_dirs(account: str | None) -> dict[str, Path]:
    dirs = account_subdirs(account)
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


# ---------------- Qlib 价格 / 日历 ----------------
def init_qlib() -> None:
    global _QLIB_READY
    if _QLIB_READY:
        return
    import qlib
    qlib.init(provider_uri=str(Path(CFG["paths"]["qlib_data"]).expanduser()), region="cn")
    _QLIB_READY = True


def reset_qlib() -> None:
    """长驻进程（如 webapp）在 qlib 数据包更新后需重置，否则日历/价格仍停在首次 init 时点。"""
    global _QLIB_READY
    _QLIB_READY = False
    init_qlib()


def calendar() -> list:
    init_qlib()
    from qlib.data import D
    return list(D.calendar(freq="day"))


def latest_trading_day() -> str:
    return pd.Timestamp(calendar()[-1]).strftime("%Y-%m-%d")


def prev_trading_day(day: str) -> str | None:
    cal = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in calendar()]
    if day not in cal:
        prior = [d for d in cal if d < day]
        return prior[-1] if prior else None
    i = cal.index(day)
    return cal[i - 1] if i > 0 else None


def trading_days_between(start: str, end: str) -> int:
    """[start, end] 闭区间交易日数（含端点）。"""
    init_qlib()
    from qlib.data import D
    if start is None:
        return 10 ** 9
    cal = D.calendar(start_time=str(start), end_time=str(end), freq="day")
    return int(len(cal))


def close_prices(instruments: list[str], day: str) -> pd.Series:
    """真实价格（后复权还原）：$close/$factor。"""
    init_qlib()
    from qlib.data import D
    if not instruments:
        return pd.Series(dtype=float, name="price")
    df = D.features(list(instruments), ["$close/$factor"], start_time=day, end_time=day)
    if df.empty:
        return pd.Series(dtype=float, name="price")
    s = df.droplevel("datetime")["$close/$factor"]
    s.name = "price"
    return s


def open_prices(instruments: list[str], day: str) -> pd.Series:
    """真实开盘价（后复权还原）：$open/$factor。"""
    init_qlib()
    from qlib.data import D
    if not instruments:
        return pd.Series(dtype=float, name="price")
    df = D.features(list(instruments), ["$open/$factor"], start_time=day, end_time=day)
    if df.empty:
        return pd.Series(dtype=float, name="price")
    s = df.droplevel("datetime")["$open/$factor"]
    s.name = "price"
    return s


def _limit_pct(inst: str) -> float:
    code = inst[2:]
    if inst.startswith("BJ") or code.startswith(("688", "689", "300", "301")):
        return 0.20  # 科创板/创业板/北交所
    return 0.10      # 主板


def trade_status(instruments: list[str], day: str) -> pd.DataFrame:
    """当日交易状态：用于风控预检。返回索引为 instrument 的 DataFrame，
    列 ret/volume/suspended/limit_up/limit_down。"""
    init_qlib()
    from qlib.data import D
    if not instruments:
        return pd.DataFrame(columns=["ret", "volume", "suspended", "limit_up", "limit_down"])
    df = D.features(list(instruments),
                    ["$close/$factor", "Ref($close/$factor,1)", "$volume"],
                    start_time=day, end_time=day)
    out = []
    for inst in instruments:
        if inst not in df.index.get_level_values("instrument"):
            out.append((inst, float("nan"), 0.0, True, False, False))
            continue
        row = df.xs(inst, level="instrument").iloc[0]
        close, prev, vol = row.iloc[0], row.iloc[1], row.iloc[2]
        susp = (not vol > 0) or pd.isna(close)
        ret = (close / prev - 1) if (prev and prev == prev and not susp) else float("nan")
        lim = _limit_pct(inst) * 0.97  # 留 3% 容差，贴近涨跌停即视为受限
        lu = bool(ret >= lim) if ret == ret else False
        ld = bool(ret <= -lim) if ret == ret else False
        out.append((inst, ret, float(vol), susp, lu, ld))
    return pd.DataFrame(out, columns=["instrument", "ret", "volume", "suspended",
                                      "limit_up", "limit_down"]).set_index("instrument")


def benchmark_return(day: str, prev: str) -> float:
    """基准（中证500）从 prev 收盘到 day 收盘的收益率。"""
    init_qlib()
    from qlib.data import D
    bench = CFG["universe"]["benchmark"]
    df = D.features([bench], ["$close"], start_time=prev, end_time=day)
    if len(df) < 2:
        return float("nan")
    c = df["$close"].values
    return float(c[-1] / c[0] - 1)


# ---------------- 交易成本 ----------------
def fill_fee(side: str, amount: float) -> float:
    """单笔成交费用：佣金(万2.5,最低5) + 卖出印花税0.05%。滑点已体现在成交价里。"""
    c = CFG["costs"]
    comm = max(amount * c["commission"], c["min_commission"])
    stamp = amount * c["stamp_duty_sell"] if side.upper() == "SELL" else 0.0
    return round(comm + stamp, 2)


# ---------------- 账户现金账本 ----------------
def account_file(account: str | None = None) -> Path:
    return account_subdirs(account)["nav"] / "account.json"


def load_account(account: str | None = None) -> dict | None:
    f = account_file(account)
    if not f.exists():
        return None
    return json.loads(f.read_text())


def save_account(acc: dict, account: str | None = None) -> None:
    f = account_file(account)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(acc, ensure_ascii=False, indent=2))


def init_account(capital: float, day: str, account: str | None = None) -> dict:
    acc = {"start_capital": float(capital), "start_date": day,
           "cash": float(capital), "last_fill_date": None,
           "account": account or "legacy"}
    save_account(acc, account)
    return acc


# ---------------- 告警 ----------------
def alert(level: str, msg: str, day: str | None = None) -> dict:
    """记录告警到 alerts.log 并打印。level: INFO/WARN/CRIT。

    路线一下"推送到手机"由部署侧接 webhook/server酱 等；此处留统一出口。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "level": level.upper(), "day": day or "", "msg": msg}
    line = f"[{rec['ts']}] {rec['level']} {('('+day+') ') if day else ''}{msg}"
    with ALERT_LOG.open("a") as f:
        f.write(line + "\n")
    print(line)
    return rec
