"""阶段5 后台常驻看板服务：FastAPI + 内置 APScheduler。

- Web 看板（默认 0.0.0.0:8000）：多账户净值/超额、持仓、成交、对账、研究vs实盘对比、告警。
- 内置定时（Asia/Shanghai，工作日）：
    10:00 board      大盘看板盘中分析（批量实时报价 → intraday 快照）
    22:30 evening   各账户生成次日调仓清单（含 UMP；影子 TA 线额外跑定性否决）
    23:30 postclose 各账户按 mode 处理成交（simulated→自动模拟；manual→读人工回填）→对账→净值→日报
    周五 23:45 研究 vs 实盘双线复盘
  定时任务即调用经过验证的 ops/run_daily.py，服务进程只做编排与展示。

启动：bash webapp/serve.sh start    （或 uvicorn webapp.server:app --host 0.0.0.0 --port 8000）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

QUANT = Path(__file__).resolve().parents[1]
for sub in ("ops", "contracts", "execution"):
    sys.path.insert(0, str(QUANT / sub))
import common as C  # noqa: E402
import schemas as S  # noqa: E402
import review_accounts as RA  # noqa: E402
sys.path.insert(0, str(QUANT / "webapp"))
import quotes as Q  # noqa: E402

HERE = Path(__file__).resolve().parent
LOG_DIR = QUANT / "data" / "logs"
VISIT_LOG = LOG_DIR / "site_visits.jsonl"
PY = sys.executable
RUN_DAILY = QUANT / "ops" / "run_daily.py"
REVIEW = QUANT / "ops" / "review_accounts.py"

# 看板纳管的账户：研究 + 实盘 + TA 影子 A/B
# paper_40k_sim 有独立页 /api/paper-40k，未落地 yaml 前不进主列表，
# 否则 overview / daily-ops 会因 account_config 抛错整页 500。
ACCOUNTS = [
    "research_sim_100k",
    "live_manual_10k",
    "shadow_ctrl_sim",
    "shadow_ta_sim",
]
# 四线对比页不纳入 4 万纸上线（资金/约束不同，不当 A/B）
COMPARE_ACCOUNTS = [
    "research_sim_100k",
    "live_manual_10k",
    "shadow_ctrl_sim",
    "shadow_ta_sim",
]
RESEARCH, LIVE = "research_sim_100k", "live_manual_10k"
ACCOUNT_LABELS = {
    "research_sim_100k": "研究模拟线",
    "live_manual_10k": "实盘线",
    "shadow_ctrl_sim": "对照影子线",
    "shadow_ta_sim": "TA影子线",
    "paper_40k_sim": "4万纸上线",
}
TZ = "Asia/Shanghai"

_LOCALHOST = frozenset({"127.0.0.1", "::1", "localhost"})

templates = Jinja2Templates(directory=str(HERE / "templates"))


def _client_ip(request: Request) -> str:
    """客户端 IP；Nginx 反代时需配置 X-Forwarded-For / X-Real-IP。"""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return ""


_HERE_CFG = QUANT / "configs"
_WEBAPP_LOCAL = _HERE_CFG / "webapp.local.yaml"


def _webapp_local() -> dict:
    if not _WEBAPP_LOCAL.exists():
        return {}
    import yaml
    return yaml.safe_load(_WEBAPP_LOCAL.read_text()) or {}


def _ip_whitelist() -> list[str]:
    loc = _webapp_local().get("ip_whitelist")
    raw = loc if loc is not None else (C.CFG.get("webapp", {}) or {}).get("ip_whitelist") or []
    return [str(x).strip() for x in raw if str(x).strip()]


def _ip_allowed(ip: str, wl: list[str]) -> bool:
    """精确匹配或 CIDR（如 223.104.124.0/24，适配移动网 IP 小幅变动）。"""
    if not ip or not wl:
        return False
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip in wl
    for entry in wl:
        if "/" in entry:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        elif ip == entry:
            return True
    return False


def full_access(request: Request) -> bool:
    """白名单或本机：完整功能；其余只读演示。"""
    ip = _client_ip(request)
    if ip in _LOCALHOST:
        return True
    wl = _ip_whitelist()
    if not wl:
        return False
    return _ip_allowed(ip, wl)


def _require_full_access(request: Request) -> None:
    if not full_access(request):
        raise HTTPException(403, "演示模式：该操作已禁用（仅白名单 IP 可用）")


def _log_visit(request: Request, path: str = "/") -> None:
    """记录首页访问（IP + 时间），写入 data/logs/site_visits.jsonl（已 gitignore）。"""
    ip = _client_ip(request)
    if not ip:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "time": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip,
        "path": path,
        "full_access": full_access(request),
        "ua": (request.headers.get("user-agent") or "")[:200],
    }
    with VISIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _visit_stats(limit: int = 100) -> dict:
    if not VISIT_LOG.exists():
        return {"total": 0, "unique_ips": 0, "by_ip": [], "recent": []}
    entries: list[dict] = []
    for line in VISIT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    ips = [str(e.get("ip") or "") for e in entries if e.get("ip")]
    recent = list(reversed(entries[-limit:]))
    return {
        "total": len(entries),
        "unique_ips": len(set(ips)),
        "by_ip": [{"ip": ip, "count": c} for ip, c in Counter(ips).most_common(30)],
        "recent": recent,
    }


# ----------------------------- 数据读取 -----------------------------
def _account_label(account: str) -> str:
    cfg = C.account_config(account).get("account", {})
    cap = cfg.get("initial_capital")
    mode = cfg.get("mode")
    name = ACCOUNT_LABELS.get(account, account)
    return f"{name}（{cap:,.0f}元·{mode}）" if cap else name


def _capital_injections(account: str) -> list[dict]:
    """account.json 中的外部现金流（注资/划出），供前端图表标注。"""
    acc = C.load_account(account)
    if not acc:
        return []
    raw = acc.get("capital_injection")
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        day = str(it.get("date") or "").strip()
        if not day:
            continue
        out.append({
            "date": day,
            "amount": round(float(it.get("amount") or 0), 2),
            "note": str(it.get("note") or ""),
        })
    return out


def daily_series(account: str) -> dict:
    d = RA.load_daily(account)
    if d.empty:
        return {"account": account, "dates": [], "series": {}, "capital_injections": []}
    # 累计收益用时间加权（日收益连乘），避免注资后改 start_capital 把历史段压成巨亏。
    cum_ret = C.twr_cum_series(d["daily_ret"])
    cum_bench = (1 + d["bench_ret"].fillna(0)).cumprod() - 1
    cum_excess = (1 + d["excess_ret"].fillna(0)).cumprod() - 1
    return {
        "account": account,
        "dates": d["date"].astype(str).tolist(),
        "series": {
            "nav": d["nav"].round(2).tolist(),
            "cash": d["cash"].round(2).tolist(),
            "position_value": d["position_value"].round(2).tolist(),
            "n_pos": d["n_pos"].astype(int).tolist(),
            "turnover": (d["turnover"] * 100).round(2).tolist(),
            "daily_ret": (d["daily_ret"] * 100).round(3).tolist(),
            "cum_ret": (cum_ret * 100).round(3).tolist(),
            "cum_bench": (cum_bench * 100).round(3).tolist(),
            "cum_excess": (cum_excess * 100).round(3).tolist(),
        },
        "capital_injections": _capital_injections(account),
    }


def positions_daily(account: str, window: int = 22) -> dict:
    """个股每日收盘快照（近一月）：逐股对齐到共同交易日，附当前持仓状态。"""
    f = C.account_subdirs(account)["nav"] / "positions_daily.csv"
    if not f.exists():
        return {"account": account, "dates": [], "instruments": []}
    df = S.read_csv("positions_daily", f)
    if df.empty:
        return {"account": account, "dates": [], "instruments": []}

    dates = sorted(df["date"].astype(str).unique())[-window:]
    dset = set(dates)
    df = df[df["date"].astype(str).isin(dset)]

    # 当前持仓（展示状态用）
    held: dict[str, dict] = {}
    hf = C.account_subdirs(account)["nav"] / "holdings.csv"
    if hf.exists():
        h = S.read_csv("holdings", hf)
        for r in h.itertuples():
            held[str(r.instrument)] = {"shares": int(r.shares),
                                       "entry_date": str(r.entry_date)[:10]
                                       if pd.notna(r.entry_date) else ""}

    # 待买入（最新调仓清单 BUY 指令，尚未成为持仓）
    pending_buys: dict[str, int] = {}
    od = _latest_order_day(account)
    if od:
        of = C.account_subdirs(account)["orders"] / f"{od}.csv"
        if of.exists():
            try:
                o = S.read_csv("orders", of)
                for r in o[o["side"].astype(str).str.upper() == "BUY"].itertuples():
                    if str(r.instrument) not in held:
                        pending_buys[str(r.instrument)] = int(r.shares)
            except Exception:
                pass

    out = []
    for inst, sub in df.groupby("instrument"):
        sub = sub.set_index("date")
        closes = [round(float(sub.loc[d, "close"]), 3) if d in sub.index else None
                  for d in dates]
        chg = [round(float(sub.loc[d, "chg_pct"]), 3) if d in sub.index else None
               for d in dates]
        # 窗口内累计涨幅：以首个有效收盘为基准
        base = next((c for c in closes if c is not None), None)
        cum = [round((c / base - 1) * 100, 3) if (c is not None and base) else None
               for c in closes]
        last_close = next((c for c in reversed(closes) if c is not None), None)
        last_chg = next((c for c in reversed(chg) if c is not None), None)
        month_ret = next((c for c in reversed(cum) if c is not None), None)
        info = held.get(str(inst), {})
        is_held = str(inst) in held
        is_pending = str(inst) in pending_buys
        out.append({
            "instrument": str(inst),
            "held": is_held,
            "pending_buy": is_pending,
            "shares": info.get("shares", pending_buys.get(str(inst), 0)),
            "entry_date": info.get("entry_date", ""),
            "closes": closes,
            "chg": chg,
            "cum": cum,
            "last_close": last_close,
            "last_chg": last_chg,
            "month_ret": month_ret,
        })
    # 排序：持仓 → 待买入 → 其它；组内按当月涨幅降序
    def _rank(x):
        return 0 if x["held"] else (1 if x["pending_buy"] else 2)
    out.sort(key=lambda x: (_rank(x), -(x["month_ret"] or 0)))
    return {"account": account, "dates": dates, "instruments": out}


def holdings_view(account: str) -> list[dict]:
    f = C.account_subdirs(account)["nav"] / "holdings.csv"
    if not f.exists():
        return []
    h = S.read_csv("holdings", f)
    if h.empty:
        return []
    h["market_value"] = (h["shares"] * h["last_price"]).round(2)
    mv = h["market_value"].sum() or 1.0
    h["weight_pct"] = (h["market_value"] / mv * 100).round(2)
    h = h.sort_values("market_value", ascending=False)
    return h[["instrument", "shares", "last_price", "market_value",
              "weight_pct", "entry_date"]].to_dict("records")


def recent_fills(account: str, limit: int = 60) -> list[dict]:
    df = RA.load_fills(account)
    if df.empty:
        return []
    df = df.sort_values(["date", "instrument"]).tail(limit)
    return df[["date", "instrument", "side", "shares", "price",
               "amount", "fee"]].to_dict("records")


def reports_list(account: str) -> list[str]:
    rdir = C.account_subdirs(account)["reports"]
    if not rdir.exists():
        return []
    return sorted((f.name for f in rdir.glob("*.md")), reverse=True)


def alerts(limit: int = 80) -> list[dict]:
    f = C.ALERT_LOG
    if not f.exists():
        return []
    out = []
    for ln in f.read_text().splitlines()[-limit:]:
        out.append({"raw": ln})
    return list(reversed(out))


def _latest_order_day(account: str) -> str | None:
    odir = C.account_subdirs(account)["orders"]
    if not odir.exists():
        return None
    days = sorted(f.stem for f in odir.glob("????-??-??.csv"))
    return days[-1] if days else None


def daily_ops_plan(account: str, order_day: str | None = None) -> dict:
    """读取账户最新（或指定）调仓清单及执行状态。"""
    try:
        cfg = C.account_config(account).get("account", {})
    except FileNotFoundError:
        return {
            "account": account,
            "label": _account_label(account),
            "mode": "unknown",
            "order_day": None,
            "execute_day": None,
            "status": "empty",
            "status_label": "账户未配置",
            "summary": f"缺少 configs/accounts/{account}.yaml",
            "orders": [],
            "target_positions": [],
        }
    mode = cfg.get("mode", "manual")
    order_day = order_day or _latest_order_day(account)
    if not order_day:
        return {
            "account": account,
            "label": _account_label(account),
            "mode": mode,
            "order_day": None,
            "execute_day": None,
            "status": "empty",
            "status_label": "尚无调仓清单",
            "summary": "等待 evening 流水线生成",
            "orders": [],
            "target_positions": [],
        }

    dirs = C.account_subdirs(account)
    execute_day = C.next_trading_day(order_day)
    orders_f = dirs["orders"] / f"{order_day}.csv"
    # 实盘舆情筛后的可执行单优先展示（模型原单仍在 orders/）
    exec_dir = dirs.get("orders_exec")
    exec_f = (exec_dir / f"{order_day}.csv") if exec_dir else None
    use_exec = bool(exec_f and exec_f.exists())
    src_f = exec_f if use_exec else orders_f
    tp_f = dirs["target_position"] / f"{order_day}.csv"
    orders = S.read_csv("orders", src_f) if src_f and src_f.exists() else pd.DataFrame(
        columns=["instrument", "side", "shares", "ref_price"])
    target = S.read_csv("target_position", tp_f) if tp_f.exists() else pd.DataFrame(
        columns=["instrument", "shares", "last_price", "entry_date"])

    n_trades = len(orders)
    sells = orders[orders["side"].str.upper() == "SELL"] if n_trades else orders
    buys = orders[orders["side"].str.upper() == "BUY"] if n_trades else orders

    acc = C.load_account(account) or {}
    last_fill = acc.get("last_fill_date")
    # 数据跳空时实际成交日未必等于 next_trading_day(order_day)（如 06-26 的单补跑到 07-01
    # 才成交），故取「≥ execute_day 的最早已完成 fills」作为真实成交日，而非死盯当天文件。
    fill_day = None
    if execute_day and dirs["fills"].exists():
        done = sorted(f.stem for f in dirs["fills"].glob("????-??-??.csv")
                      if f.stem >= execute_day and f.with_suffix(".done").exists())
        fill_day = done[0] if done else None
    applied = bool(execute_day and last_fill and str(last_fill) >= execute_day and fill_day)
    filled_day = fill_day or execute_day  # 展示用：优先真实成交日

    if n_trades == 0:
        status, status_label = "no_trade", "无需调仓"
        summary = f"订单日 {order_day}，持仓维持不变"
        if applied:
            status_label = "无需调仓 · 已结算"
    elif applied:
        status, status_label = "done", "已执行"
        summary = f"卖出 {len(sells)} 笔 / 买入 {len(buys)} 笔（{filled_day} 已模拟成交）"
    else:
        status, status_label = "pending", "待执行"
        exec_hint = execute_day or "待定"
        if mode == "simulated":
            summary = (f"卖出 {len(sells)} 笔 / 买入 {len(buys)} 笔；"
                       f"执行日 {exec_hint} postclose 自动模拟成交")
        else:
            src_tag = "舆情筛后执行单" if use_exec else "模型原单"
            summary = (f"卖出 {len(sells)} 笔 / 买入 {len(buys)} 笔（{src_tag}）；"
                       f"执行日 {exec_hint} 人工下单后 record_fills 回填")

    order_rows = []
    if n_trades:
        side_order = {"SELL": 0, "BUY": 1}
        sorted_orders = orders.copy()
        sorted_orders["_ord"] = sorted_orders["side"].str.upper().map(side_order)
        sorted_orders = sorted_orders.sort_values(["_ord", "instrument"])
        for r in sorted_orders.itertuples():
            order_rows.append({
                "instrument": r.instrument,
                "side": str(r.side).upper(),
                "shares": int(r.shares),
                "ref_price": round(float(r.ref_price), 2),
            })

    tp_rows = []
    if not target.empty:
        for r in target.itertuples():
            tp_rows.append({
                "instrument": r.instrument,
                "shares": int(r.shares),
                "last_price": round(float(r.last_price), 2),
                "entry_date": str(r.entry_date)[:10] if pd.notna(r.entry_date) else "",
            })

    return {
        "account": account,
        "label": _account_label(account),
        "mode": mode,
        "order_day": order_day,
        "execute_day": execute_day,
        "status": status,
        "status_label": status_label,
        "summary": summary,
        "orders": order_rows,
        "target_positions": tp_rows,
    }


def daily_ops_all() -> dict:
    try:
        data_day = C.latest_trading_day()
    except Exception:
        data_day = None
    plans = []
    for a in ACCOUNTS:
        try:
            plans.append(daily_ops_plan(a))
        except Exception as exc:
            plans.append({
                "account": a,
                "label": _account_label(a),
                "mode": "unknown",
                "order_day": None,
                "execute_day": None,
                "status": "empty",
                "status_label": "读取失败",
                "summary": str(exc),
                "orders": [],
                "target_positions": [],
            })
    return {
        "data_day": data_day,
        "plans": plans,
    }


def overview() -> dict:
    accts = []
    for a in ACCOUNTS:
        s = RA.summary(a)
        s["label"] = _account_label(a)
        acc = C.load_account(a) or {}
        s["cash"] = acc.get("cash")
        s["last_fill_date"] = acc.get("last_fill_date")
        accts.append(s)
    try:
        data_day = C.latest_trading_day()
    except Exception:
        data_day = None
    return {
        "accounts": accts,
        "data_day": data_day,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "jobs": scheduler_jobs(),
    }


# ----------------------------- 调度任务 -----------------------------
def _run_daily(stage: str, account: str, ump: bool = True) -> None:
    log = LOG_DIR / f"{stage}_{account}_{datetime.now():%Y-%m-%d}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [PY, str(RUN_DAILY), "--stage", stage, "--account", account]
    if stage == "evening" and ump:
        cmd += ["--ump"]
    with log.open("a") as fh:
        fh.write(f"\n=== {datetime.now():%F %T} {' '.join(cmd[1:])} ===\n")
        fh.flush()
        subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)


def job_evening() -> None:
    for a in ACCOUNTS:
        _run_daily("evening", a, ump=True)
    # evening 在子进程里 dump 了新日历；父进程必须 reset，否则顶栏「数据日」停在启动时点。
    try:
        C.reset_qlib()
    except Exception:
        pass


def job_postclose() -> None:
    for a in ACCOUNTS:
        _run_daily("postclose", a)


def job_review() -> None:
    log = LOG_DIR / f"review_{datetime.now():%Y-%m-%d}.log"
    with log.open("a") as fh:
        subprocess.run([PY, str(REVIEW), "--research", RESEARCH, "--live", LIVE],
                       stdout=fh, stderr=subprocess.STDOUT)


def job_board_morning() -> None:
    """工作日 10:00 自动拉取池内实时报价，更新大盘看板盘中分析。

    不能用 qlib 日历判断「今天是否交易日」：日历只有已收盘日，10:00 当天必然不在里面，
    会把每个交易日早上都跳过。周末由 CronTrigger(mon-fri) 过滤；节假日用报价
    quote_time 写入 session_day，非当日则前端按过期隐藏。
    """
    started = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
    log = LOG_DIR / f"board_morning_{datetime.now():%Y-%m-%d}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _write_board_refresh_job({"status": "running", "started": started,
                              "message": "10:00 自动拉取池内实时报价…", "pct": 20})
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {started} board intraday refresh ===\n")
        fh.flush()
        try:
            from overlays.market_board import intraday as board_intraday
            payload = board_intraday.build_intraday()
            ok = bool(payload.get("ok"))
            msg = (f"已刷新 {payload.get('quotes_ok')}/{payload.get('quotes_total')} 只"
                   if ok else payload.get("error", "刷新失败"))
            fh.write(f"ok={ok} {msg} session={payload.get('session_day')}\n")
            _write_board_refresh_job({
                "status": "ok" if ok else "error",
                "started": started,
                "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                "message": f"10:00 自动 · {msg}",
                "pct": 100,
            })
        except Exception as exc:  # noqa: BLE001
            fh.write(f"ERROR: {exc}\n")
            _write_board_refresh_job({
                "status": "error", "started": started,
                "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                "message": str(exc), "pct": 100,
            })


_scheduler = None


def scheduler_jobs() -> list[dict]:
    if _scheduler is None:
        return []
    out = []
    for j in _scheduler.get_jobs():
        out.append({"id": j.id,
                    "next_run": j.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
                    if j.next_run_time else None})
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    # origin 服务器不从 MinIO 拉取；client 才 sync_on_startup
    try:
        from minio_sync import sync_on_startup
        r = sync_on_startup()
        if r.get("action") not in {"disabled", None}:
            print(f"[minio] 启动同步: {r.get('action')}")
    except Exception as exc:
        print(f"[minio] 启动同步跳过: {exc}")

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = BackgroundScheduler(timezone=TZ)
    # 工作日 10:00 大盘盘中分析；22:30 出次日清单；23:30 收盘后对账/净值；周五 23:45 双线复盘
    _scheduler.add_job(job_board_morning, CronTrigger(day_of_week="mon-fri", hour=10, minute=0,
                                                      timezone=TZ), id="board_morning",
                       replace_existing=True)
    _scheduler.add_job(job_evening, CronTrigger(day_of_week="mon-fri", hour=22, minute=30,
                                                timezone=TZ), id="evening", replace_existing=True)
    _scheduler.add_job(job_postclose, CronTrigger(day_of_week="mon-fri", hour=23, minute=30,
                                                  timezone=TZ), id="postclose", replace_existing=True)
    _scheduler.add_job(job_review, CronTrigger(day_of_week="fri", hour=23, minute=45,
                                               timezone=TZ), id="review", replace_existing=True)
    _scheduler.start()
    try:
        yield
    finally:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="A股量化双线看板", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

# ---- Quant Agent UI (React) under /agent/ ; API proxied to :8010 ----
AGENT_DIST = QUANT / "agent-ui" / "dist"
AGENT_API_UPSTREAM = os.environ.get("AGENT_API_UPSTREAM", "http://127.0.0.1:8010")


@app.api_route("/agent/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def agent_api_proxy(path: str, request: Request):
    """Browser stays on :8000; proxy to agent_api so 5173/8010 need not be public."""
    import httpx

    url = f"{AGENT_API_UPSTREAM.rstrip('/')}/api/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    # preserve client IP for agent_api whitelist
    headers["x-forwarded-for"] = _client_ip(request) or headers.get("x-forwarded-for", "")
    body = await request.body()
    # Supervisor / research can call remote Peak LLM — allow longer than default
    slow = path.startswith("supervisor/") or path.startswith("research/") or path.startswith("admin/")
    timeout = httpx.Timeout(600.0 if slow else 90.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.request(
                request.method, url, content=body, headers=headers
            )
    except httpx.ConnectError as exc:
        raise HTTPException(
            502,
            f"Agent API 未启动（{AGENT_API_UPSTREAM}）。请先 bash agent_api/serve.sh",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            504,
            f"Agent API 超时（{path}）。Peak LLM 或研究队列可能过慢，请重试或稍后。",
        ) from exc
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@app.get("/agent")
@app.get("/agent/")
@app.get("/agent/{full_path:path}")
def agent_spa(request: Request, full_path: str = ""):
    """Serve built React app; SPA fallback to index.html."""
    if not AGENT_DIST.exists():
        raise HTTPException(
            503,
            "Agent UI 未构建：cd quant/agent-ui && npm run build",
        )
    _log_visit(request, f"/agent/{full_path}")
    # never let this catch /agent/api (registered above)
    candidate = (AGENT_DIST / full_path).resolve()
    try:
        candidate.relative_to(AGENT_DIST.resolve())
    except ValueError as exc:
        raise HTTPException(400, "bad path") from exc
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    index = AGENT_DIST / "index.html"
    if not index.exists():
        raise HTTPException(503, "Agent UI index.html missing")
    return FileResponse(index)


def _asset_ver() -> int:
    """静态资源版本号（取 app.js/style.css 最新修改时间），用于前端缓存击穿。"""
    files = [HERE / "static" / "app.js", HERE / "static" / "style.css"]
    mtimes = [f.stat().st_mtime for f in files if f.exists()]
    return int(max(mtimes)) if mtimes else 0


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    _log_visit(request, "/")
    resp = templates.TemplateResponse(
        request, "index.html", {"accounts": ACCOUNTS, "ver": _asset_ver()})
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/api/access")
def api_access(request: Request):
    """访问模式：白名单内 full_access，其余 demo_mode（前端据此隐藏分析/刷新）。"""
    fa = full_access(request)
    ip = _client_ip(request)
    return {
        "full_access": fa,
        "demo_mode": not fa,
        # 演示模式下返回 IP，便于管理员复制到 configs/webapp.local.yaml
        "client_ip": ip if not fa else None,
    }


@app.get("/api/visits")
def api_visits(request: Request, limit: int = 100):
    """站点访问统计（仅白名单 IP 可查看）。"""
    _require_full_access(request)
    lim = min(max(int(limit), 1), 500)
    return _visit_stats(lim)


@app.get("/api/overview")
def api_overview():
    return overview()


@app.get("/api/daily-ops")
def api_daily_ops():
    return daily_ops_all()


@app.get("/api/account/{account}/daily-ops")
def api_account_daily_ops(account: str):
    _check(account)
    try:
        data_day = C.latest_trading_day()
    except Exception:
        data_day = None
    return {"data_day": data_day, "plan": daily_ops_plan(account)}


@app.get("/api/account/{account}/daily")
def api_daily(account: str):
    _check(account)
    return daily_series(account)


@app.get("/api/account/{account}/holdings")
def api_holdings(account: str):
    _check(account)
    return {"account": account, "holdings": holdings_view(account)}


@app.get("/api/account/{account}/positions-daily")
def api_positions_daily(account: str):
    _check(account)
    return positions_daily(account)


@app.get("/api/account/{account}/fills")
def api_fills(account: str):
    _check(account)
    return {"account": account, "fills": recent_fills(account)}


@app.get("/api/account/{account}/reports")
def api_reports(account: str):
    _check(account)
    return {"account": account, "reports": reports_list(account)}


@app.get("/api/account/{account}/report/{name}", response_class=HTMLResponse)
def api_report(account: str, name: str):
    _check(account)
    if "/" in name or not name.endswith(".md"):
        raise HTTPException(400, "非法报告名")
    f = C.account_subdirs(account)["reports"] / name
    if not f.exists():
        raise HTTPException(404, "报告不存在")
    return f"<pre>{f.read_text()}</pre>"


@app.get("/api/compare")
def api_compare():
    """四线对比：研究 / 实盘 / 对照影子 / TA 影子。

    返回各账户摘要 + 日序列（前端按日期并集画累计收益/超额）；
    成交偏差仍只比研究 vs 实盘（资金量级不同，影子线不参与滑点表）。
    """
    summary = {a: RA.summary(a) for a in COMPARE_ACCOUNTS}
    series = {a: daily_series(a) for a in COMPARE_ACCOUNTS}

    # 研究 vs 实盘：共同交易日收益差（保留，便于看执行偏差日）
    dr, dl = RA.load_daily(RESEARCH), RA.load_daily(LIVE)
    common = []
    if not dr.empty and not dl.empty:
        m = dr[["date", "daily_ret", "excess_ret"]].merge(
            dl[["date", "daily_ret", "excess_ret"]], on="date",
            how="inner", suffixes=("_r", "_l"))
        for r in m.itertuples():
            common.append({"date": str(r.date),
                           "ret_research": round(r.daily_ret_r * 100, 3),
                           "ret_live": round(r.daily_ret_l * 100, 3),
                           "gap": round((r.daily_ret_l - r.daily_ret_r) * 100, 3)})

    # TA vs 对照：共同交易日超额差（影子 A/B）
    dta, dctrl = RA.load_daily("shadow_ta_sim"), RA.load_daily("shadow_ctrl_sim")
    ta_gap = []
    if not dta.empty and not dctrl.empty:
        m2 = dta[["date", "excess_ret"]].merge(
            dctrl[["date", "excess_ret"]], on="date",
            how="inner", suffixes=("_ta", "_ctrl"))
        for r in m2.itertuples():
            ta_gap.append({
                "date": str(r.date),
                "excess_ta": round(r.excess_ret_ta * 100, 3),
                "excess_ctrl": round(r.excess_ret_ctrl * 100, 3),
                "gap": round((r.excess_ret_ta - r.excess_ret_ctrl) * 100, 3),
            })

    fd = RA.fill_diff(RESEARCH, LIVE)
    diffs = []
    if not fd.empty:
        for r in fd.sort_values("adverse_slip_pct", ascending=False).head(20).itertuples():
            diffs.append({"date": r.date, "instrument": r.instrument, "side": r.side,
                          "research_price": round(r.research_price, 2),
                          "live_price": round(r.live_price, 2),
                          "adverse_slip_pct": round(r.adverse_slip_pct, 3)})
    return {
        "accounts": list(COMPARE_ACCOUNTS),
        "labels": ACCOUNT_LABELS,
        "summary": summary,
        "series": series,
        "common_days": common,
        "ta_gap_days": ta_gap,
        "fill_diff": diffs,
    }


@app.get("/api/paper-40k")
def api_paper_40k():
    """4 万纸上线：账户参数 + 嵌套验收摘要，供独立页展示。"""
    _check("paper_40k_sim")
    cfg = C.account_config("paper_40k_sim")
    acct = cfg.get("account", {})
    st = cfg.get("strategy", {})
    ex = cfg.get("execution", {})
    nested = {}
    report_path = QUANT / "data" / "reports" / "stage_40k_strategy_20260814.json"
    if report_path.exists():
        raw = json.loads(report_path.read_text())
        for row in raw.get("grid") or []:
            nested[row["tag"]] = {
                "sel_ir": row["sel"]["ir"],
                "sel_ann": row["sel"]["ann"],
                "y2026_ir": row["y2026"]["ir"],
                "y2026_ann": row["y2026"]["ann"],
                "y2026_mdd": row["y2026"]["mdd"],
                "qualified": row.get("qualified"),
            }
        fork = raw.get("fork")
        qualified = raw.get("qualified_tags") or []
        winner = (raw.get("winner_sel") or {}).get("tag")
    else:
        fork, qualified, winner = "", [], None
    acc = C.load_account("paper_40k_sim") or {}
    return {
        "account": "paper_40k_sim",
        "label": ACCOUNT_LABELS["paper_40k_sim"],
        "mode": acct.get("mode"),
        "cash": acct.get("initial_capital"),
        "book_cash": acc.get("cash"),
        "start_date": acc.get("start_date"),
        "last_fill_date": acc.get("last_fill_date"),
        "strategy": {
            "topk": st.get("topk"),
            "n_drop": st.get("n_drop"),
            "hold_thresh": st.get("hold_thresh"),
            "max_weight": st.get("max_weight"),
            "tag": f"{st.get('topk')}/{st.get('n_drop')}/{st.get('hold_thresh')}",
        },
        "execution": {
            "exclude_boards": ex.get("exclude_boards") or [],
            "filter_unaffordable": bool(ex.get("filter_unaffordable")),
            "lot_slack": ex.get("lot_slack"),
            "lot_size": ex.get("lot_size", 100),
        },
        "paper_tag": "12/1/10",
        "qualified_tags": qualified,
        "sel_winner": winner,
        "nested": nested,
        "fork": fork,
        "note": "纸上模拟，不满 40 个交易日不改 live_manual_10k。",
        "plan": daily_ops_plan("paper_40k_sim"),
    }


@app.get("/api/alerts")
def api_alerts():
    return {"alerts": alerts()}


@app.get("/api/quote/{instrument}")
def api_quote(instrument: str, klt: int = 101, n: int = 120, fqt: int = 1):
    """个股/指数行情：klt=101日线/102周线/1分钟等；优先腾讯 TXApi，其次东财，回退本地 qlib(EOD)。"""
    if not instrument[:2].isalpha() or not instrument[2:].isdigit():
        raise HTTPException(400, "标的格式应为 SH600000 / SZ000001")
    return Q.quote(instrument.upper(), klt=klt, lmt=min(max(n, 5), 500), fqt=fqt)


@app.get("/api/indices")
def api_indices():
    """大盘指数：上证/中证500(基准)/创业板指。"""
    return {"indices": Q.indices()}


# ----------------------------- 市场热度（总览，只读建议） -----------------------------
PULSE_JOB = LOG_DIR / "market_pulse_job.json"
PULSE_BOOKS = ("live_manual_10k", "paper_40k_sim")


def _latest_sector_pulse() -> tuple[Path | None, dict | None]:
    reports = QUANT / "data" / "reports"
    files = sorted(reports.glob("sector_pulse_????????.json"))
    if not files:
        return None, None
    path = files[-1]
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return path, None


def _read_pulse_job() -> dict:
    if not PULSE_JOB.exists():
        return {"status": "idle"}
    try:
        return json.loads(PULSE_JOB.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "idle"}


def _write_pulse_job(payload: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PULSE_JOB.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _pulse_regime(indices: list[dict]) -> dict:
    by = {str(x.get("name") or ""): x for x in indices or []}
    csi300 = by.get("沪深300") or {}
    csi500 = by.get("中证500") or {}
    csi1000 = by.get("中证1000") or {}
    cyb = by.get("创业板指") or {}
    sh = by.get("上证指数") or {}

    def _f(row: dict, key: str) -> float | None:
        v = row.get(key)
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    d20_500 = None
    d20_1000 = None
    if _f(csi500, "chg_20d") is not None and _f(csi300, "chg_20d") is not None:
        d20_500 = round(float(csi500["chg_20d"]) - float(csi300["chg_20d"]), 2)
    if _f(csi1000, "chg_20d") is not None and _f(csi300, "chg_20d") is not None:
        d20_1000 = round(float(csi1000["chg_20d"]) - float(csi300["chg_20d"]), 2)
    tags: list[str] = []
    if d20_500 is not None and d20_1000 is not None:
        if d20_500 > 1 and d20_1000 > 1:
            tags.append("近20日中小盘占优")
        elif d20_500 < -1 and d20_1000 < -1:
            tags.append("近20日大盘相对强")
        else:
            tags.append("近20日风格分化不明显")
    cyb_1d = _f(cyb, "chg_1d")
    sh_1d = _f(sh, "chg_1d")
    if cyb_1d is not None and sh_1d is not None and cyb_1d > 0.3 and sh_1d < 0:
        tags.append("创业板日内逆势")
    if not tags:
        tags.append("指数结构见下表")
    return {
        "tags": tags,
        "csi500_vs_300_20d": d20_500,
        "csi1000_vs_300_20d": d20_1000,
        "csi300_20d": _f(csi300, "chg_20d"),
        "csi500_20d": _f(csi500, "chg_20d"),
        "csi1000_20d": _f(csi1000, "chg_20d"),
        "cyb_1d": cyb_1d,
        "sh_1d": sh_1d,
    }


def _pulse_name_map(pulse: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key in ("rising", "watch", "holdings"):
        for row in pulse.get(key) or []:
            inst = str(row.get("instrument") or "")
            if inst:
                out[inst] = row
    return out


def _hot_board_hit(industry: str, boards: list[str]) -> str:
    if not industry:
        return ""
    for b in boards:
        if not b:
            continue
        if b in industry or industry in b:
            return b
    return ""


def _pulse_book_overlap(pulse: dict) -> list[dict]:
    boards = [str(x.get("name") or "") for x in (pulse.get("industries_top") or [])[:8]]
    names = _pulse_name_map(pulse)
    rows: list[dict] = []
    for account in PULSE_BOOKS:
        try:
            held = {str(h["instrument"]) for h in holdings_view(account)}
        except Exception:
            held = set()
        try:
            plan = daily_ops_plan(account)
        except Exception:
            plan = {}
        targets = {str(t.get("instrument") or "") for t in plan.get("target_positions") or []}
        buys = {
            str(o.get("instrument") or "")
            for o in plan.get("orders") or []
            if str(o.get("side") or "").upper() == "BUY"
        }
        insts = sorted((held | targets | buys) - {""})
        for inst in insts:
            info = names.get(inst) or {}
            industry = str(info.get("industry") or "")
            role = []
            if inst in held:
                role.append("持仓")
            if inst in targets or inst in buys:
                role.append("目标")
            rows.append({
                "account": account,
                "label": ACCOUNT_LABELS.get(account, account),
                "instrument": inst,
                "name": info.get("name") or "",
                "industry": industry,
                "hot_board": _hot_board_hit(industry, boards),
                "policy_hit": bool(info.get("policy_hit")),
                "sector_hit": bool(info.get("sector_hit")),
                "pulse": info.get("pulse"),
                "role": " / ".join(role) or "—",
            })
    rows.sort(key=lambda x: (
        0 if x["hot_board"] or x["policy_hit"] else 1,
        x["label"],
        x["instrument"],
    ))
    return rows


@app.get("/api/market/pulse")
def api_market_pulse():
    """总览用市场热度：读最新 sector_pulse JSON，不改订单。"""
    path, pulse = _latest_sector_pulse()
    job = _read_pulse_job()
    if not pulse:
        return {
            "ok": True,
            "available": False,
            "job": job,
            "disclaimer": "尚无热度报告。点「刷新热度」生成；不改订单。",
        }
    industries = list(pulse.get("industries_top") or [])[:8]
    themes = []
    for t in pulse.get("policy_themes") or []:
        themes.append({
            "theme": t.get("theme"),
            "n": t.get("n"),
            "headlines": list(t.get("headlines") or [])[:3],
        })
    rising = list(pulse.get("rising") or [])
    watch = [x for x in (pulse.get("watch") or [])
             if x.get("instrument") not in {r.get("instrument") for r in rising}][:8]
    return {
        "ok": True,
        "available": True,
        "path": str(path) if path else None,
        "generated": pulse.get("generated"),
        "signal_day": pulse.get("signal_day"),
        "disclaimer": pulse.get("disclaimer") or "研究建议，不改订单。",
        "regime": _pulse_regime(pulse.get("indices") or []),
        "indices": pulse.get("indices") or [],
        "industries_top": industries,
        "policy_themes": themes,
        "rising": rising,
        "watch": watch,
        "books": _pulse_book_overlap(pulse),
        "job": job,
    }


@app.post("/api/market/run")
def api_market_run(request: Request):
    """后台重跑 sector_pulse.py，只写 reports/，不改 orders/。"""
    _require_full_access(request)
    job = _read_pulse_job()
    if job.get("status") == "running":
        return {"ok": True, "queued": False, "busy": True, "job": job}
    started = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "status": "running",
        "started": started,
        "message": "正在拉取指数/行业/政策…",
        "pct": 8,
    }
    _write_pulse_job(payload)
    log = LOG_DIR / f"market_pulse_{datetime.now():%Y-%m-%d}.log"
    cmd = [PY, str(QUANT / "research" / "sector_pulse.py")]

    def _bg():
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {started} {' '.join(cmd[1:])} ===\n")
            fh.flush()
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, cwd=str(QUANT),
                    env={**dict(os.environ), "PYTHONUNBUFFERED": "1"},
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    fh.write(line)
                    fh.flush()
                rc = proc.wait()
                done = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
                _write_pulse_job({
                    "status": "ok" if rc == 0 else "error",
                    "started": started,
                    "finished": done,
                    "message": "已更新" if rc == 0 else f"退出码 {rc}",
                    "pct": 100,
                    "rc": rc,
                })
            except Exception as exc:  # noqa: BLE001
                _write_pulse_job({
                    "status": "error",
                    "started": started,
                    "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                    "message": str(exc),
                    "pct": 100,
                })

    import threading
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "queued": True, "busy": False, "job": payload, "log": str(log)}


# ----------------------------- 舆情长期记忆 -----------------------------
@app.get("/api/sentiment/catalog")
def api_sentiment_catalog(account: str | None = None):
    """跟踪标的目录（最新报告摘要）。可按账户筛持仓/订单宇宙。"""
    sys.path.insert(0, str(QUANT))
    from overlays.sentiment_memory import store as SM  # noqa: WPS433
    from overlays.sentiment_memory.run_memory import resolve_universe
    cat = SM.load_catalog()
    by_inst = dict(cat.get("instruments") or {})
    instruments = list(by_inst.values())
    book: list[str] = []
    if account:
        _check(account)
        book = resolve_universe(account)
        book_set = set(book)
        instruments = [x for x in instruments if x.get("instrument") in book_set]
        have = {x.get("instrument") for x in instruments}
        for inst in book:
            if inst not in have:
                instruments.append({
                    "instrument": inst,
                    "name": "",
                    "headline": "尚未分析",
                    "sentiment": None,
                    "score": None,
                    "accounts": [account],
                })
    instruments.sort(key=lambda x: (
        0 if x.get("latest_date") else 1,
        str(x.get("latest_date") or ""),
    ), reverse=True)
    return {
        "updated_at": cat.get("updated_at"),
        "peak_hour": _sentiment_peak_now(),
        "account": account,
        "book": book,
        "instruments": instruments,
    }


@app.get("/api/sentiment/job")
def api_sentiment_job():
    """当前/最近一次舆情分析任务进度（供进度条与刷新恢复）。"""
    sys.path.insert(0, str(QUANT))
    from overlays.sentiment_memory import job as SJ  # noqa: WPS433
    return SJ.read_job()


@app.get("/api/sentiment/{instrument}")
def api_sentiment_instrument(instrument: str, days: int = 90):
    """个股舆情报告 + 近 N 日报告列表 + 向量库统计。"""
    inst = instrument.upper()
    if not (len(inst) >= 6 and inst[:2].isalpha() and inst[2:].isdigit()):
        raise HTTPException(400, "标的格式应为 SH600000 / SZ000001")
    sys.path.insert(0, str(QUANT))
    from overlays.sentiment_memory import store as SM  # noqa: WPS433
    report = SM.load_report(inst)
    history = SM.list_reports(inst, limit=min(max(days, 30), 120))
    stats = SM.vector_stats(inst)
    return {
        "instrument": inst,
        "report": report,
        "history": history,
        "vector": stats,
        "peak_hour": _sentiment_peak_now(),
    }


@app.post("/api/sentiment/run")
def api_sentiment_run(request: Request,
                      account: str = "live_manual_10k",
                      dry_run: bool = False,
                      instrument: str | None = None,
                      limit: int | None = None,
                      all_traded: bool = False,
                      only_new: bool = False):
    """手动触发舆情记忆分析（后台线程）。

    - 不传 instrument：分析账户持仓/订单/已跟踪标的
    - 传 instrument：只分析该票（支持 600000 / SH600000）
    - 传 limit：本次最多分析 N 只（优先未分析/报告最旧）
    - all_traded：聚合四账户 fills 作为宇宙（与持仓追踪页同口径）
    - only_new：只跑尚无报告的票
    """
    _require_full_access(request)
    sys.path.insert(0, str(QUANT))
    from overlays.sentiment_memory.run_memory import normalize_instrument
    from overlays.sentiment_memory import job as SJ  # noqa: WPS433

    inst = None
    if instrument:
        inst = normalize_instrument(instrument)
        if not inst:
            raise HTTPException(400, "标的格式应为 SH600000 / SZ000001 / 600000")
    else:
        _check(account)

    running = SJ.read_job()
    if running.get("status") == "running":
        # 允许查看已有任务；若重复点击，返回当前任务避免叠加混乱
        return {
            "ok": True,
            "queued": False,
            "busy": True,
            "job": running,
            "instrument": running.get("instrument"),
            "account": running.get("account"),
            "dry_run": bool(running.get("dry_run")),
            "log": str(LOG_DIR / f"sentiment_memory_{datetime.now():%Y-%m-%d}.log"),
        }

    log = LOG_DIR / f"sentiment_memory_{datetime.now():%Y-%m-%d}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [PY, str(QUANT / "overlays" / "sentiment_memory" / "run_memory.py"),
           "--lookback", "90", "--force-llm", "peak"]
    if inst:
        cmd += ["--instruments", inst]
    elif all_traded:
        cmd += ["--all-traded"]
    else:
        cmd += ["--account", account]
    if only_new:
        cmd.append("--only-new")
    if limit and limit > 0:
        cmd += ["--limit", str(int(limit))]
    if dry_run:
        cmd.append("--dry-run")

    job = SJ.start_job(instrument=inst, account=None if inst else account, dry_run=dry_run)
    if inst:
        SJ.write_job({"total": 1, "universe": [inst], "message": f"采集 {inst}…", "pct": 8})

    def _bg():
        with log.open("a") as fh:
            fh.write(f"\n=== {datetime.now():%F %T} {' '.join(cmd[1:])} ===\n")
            fh.flush()
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    env={**dict(__import__("os").environ), "PYTHONUNBUFFERED": "1"},
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    fh.write(line)
                    fh.flush()
                    try:
                        SJ.update_from_line(line)
                    except Exception:
                        pass
                rc = proc.wait()
                SJ.finish_job(ok=(rc == 0),
                              message="分析完成" if rc == 0 else f"分析退出码 {rc}")
            except Exception as e:  # noqa: BLE001
                fh.write(f"[job-error] {e}\n")
                SJ.finish_job(ok=False, message=str(e)[:200])

    import threading
    threading.Thread(target=_bg, daemon=True).start()
    return {
        "ok": True,
        "queued": True,
        "busy": False,
        "job": SJ.read_job(),
        "account": None if inst else account,
        "instrument": inst,
        "dry_run": dry_run,
        "log": str(log),
        "job_id": job.get("id"),
    }


def _sentiment_peak_now() -> bool:
    try:
        sys.path.insert(0, str(QUANT))
        from overlays.sentiment_memory.llm_router import is_peak_hour
        return bool(is_peak_hour())
    except Exception:
        return False


# ----------------- 持仓追踪（自首次买入日起的价格走势 + 舆情） -----------------

@app.get("/api/tracking")
def api_tracking():
    """持仓追踪快照：聚合各账户 fills，自首次买入日起追踪价格走势。

    排序：跌→涨（cum_ret 升序）。每条含买卖节点、涨跌区间、舆情摘要。
    快照由 /api/tracking/run 后台构建，落 data/overlays/tracking/snapshot.json。
    """
    sys.path.insert(0, str(QUANT))
    from overlays.tracking import store as TK  # noqa: WPS433
    from overlays.tracking.build import _attach_sector_fields  # noqa: WPS433
    snap = TK.load_snapshot()
    if not snap:
        return {"empty": True, "total": 0, "instruments": [],
                "message": "尚无追踪快照，点击「重新构建」生成"}
    insts = snap.get("instruments") or []
    if insts:
        _attach_sector_fields(insts)
        snap["instruments"] = insts
        cov = dict(snap.get("coverage") or {})
        cov["sector"] = sum(1 for x in insts if x.get("sector_forecast"))
        snap["coverage"] = cov
    snap["empty"] = False
    return snap


@app.get("/api/tracking/job")
def api_tracking_job():
    """当前/最近一次追踪快照构建任务进度（供进度条与刷新恢复）。"""
    sys.path.insert(0, str(QUANT))
    from overlays.tracking import store as TK  # noqa: WPS433
    return TK.read_job()


@app.post("/api/tracking/run")
def api_tracking_run(request: Request):
    """手动触发追踪快照构建（后台线程）。聚合 fills + 拉行情 + 附舆情。"""
    _require_full_access(request)
    sys.path.insert(0, str(QUANT))
    from overlays.tracking import store as TK  # noqa: WPS433
    from overlays.tracking import build as TB  # noqa: WPS433

    running = TK.read_job()
    if running.get("status") == "running":
        return {"ok": True, "queued": False, "busy": True, "job": running}
    if TK.read_analyze_job().get("status") == "running":
        return {"ok": True, "queued": False, "busy": True,
                "job": TK.read_analyze_job(),
                "message": "持仓分析正在跑，稍后再构建快照"}

    TK.start_job()

    def _bg():
        try:
            TB.build_and_save(progress=True)
        except Exception as e:  # noqa: BLE001
            TK.finish_job(ok=False, message=f"追踪快照失败: {e}"[:200])

    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "queued": True, "busy": False, "job": TK.read_job()}


def _uni_preview(uni: dict) -> dict:
    return {
        "accounts": uni.get("accounts") or [],
        "account_labels": uni.get("account_labels") or [],
        "total": uni.get("total") or 0,
        "held_count": uni.get("held_count") or 0,
        "mode": uni.get("mode") or "holdings",
        "items": uni.get("items") or [],
    }


@app.get("/api/tracking/analyze/universe")
def api_tracking_analyze_universe():
    """跑分析宇宙预览：当前持仓 + 全量（追踪快照/曾买卖）。"""
    sys.path.insert(0, str(QUANT))
    from overlays.tracking.run_analyze import (  # noqa: WPS433
        load_full_universe, load_holdings_universe,
    )
    hold = load_holdings_universe()
    full = load_full_universe()
    out = _uni_preview(hold)
    out["holdings"] = _uni_preview(hold)
    out["full"] = _uni_preview(full)
    return out


@app.get("/api/tracking/analyze/job")
def api_tracking_analyze_job():
    sys.path.insert(0, str(QUANT))
    from overlays.tracking import store as TK  # noqa: WPS433
    return TK.read_analyze_job()


@app.post("/api/tracking/analyze")
def api_tracking_analyze(request: Request, dry_run: bool = False, full: bool = False):
    """串行跑舆情 / 研究 / 短线。默认实盘+TA 当前持仓；full=true 为追踪快照全部标的。"""
    _require_full_access(request)
    sys.path.insert(0, str(QUANT))
    from overlays.tracking import store as TK  # noqa: WPS433
    from overlays.tracking.run_analyze import (  # noqa: WPS433
        _llm_busy_reason, load_analyze_universe, run as run_analyze,
    )

    running = TK.read_analyze_job()
    if running.get("status") == "running":
        return {"ok": True, "queued": False, "busy": True, "job": running}
    snap_job = TK.read_job()
    if snap_job.get("status") == "running":
        return {"ok": True, "queued": False, "busy": True, "job": snap_job,
                "message": "追踪快照正在构建，稍后再跑分析"}
    busy = _llm_busy_reason()
    if busy:
        return {"ok": False, "queued": False, "busy": True, "message": busy,
                "job": running}

    uni = load_analyze_universe(full=full)
    if not uni["total"]:
        msg = ("追踪快照为空，请先重新构建" if full
               else "实盘线 + TA线当前无持仓")
        return {"ok": False, "queued": False, "busy": False,
                "message": msg, "total": 0, "mode": uni["mode"]}

    names = {it["instrument"]: it.get("name") or "" for it in uni["items"]}
    job = TK.start_analyze_job(
        accounts=uni["accounts"], instruments=uni["instruments"], names=names,
        mode=uni["mode"])
    log = LOG_DIR / f"tracking_analyze_{datetime.now():%Y-%m-%d}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tag = "full" if full else "holdings"

    def _bg():
        with log.open("a") as fh:
            fh.write(f"\n=== {datetime.now():%F %T} analyze {tag} "
                     f"{uni['total']} {','.join(uni['instruments'])} ===\n")
            fh.flush()
            try:
                out = run_analyze(dry_run=dry_run, force_llm="peak",
                                  progress=True, start_job=False, full=full)
                fh.write(f"[result] {out}\n")
            except Exception as e:  # noqa: BLE001
                fh.write(f"[job-error] {e}\n")
                TK.finish_analyze_job(ok=False, message=f"持仓分析失败: {e}"[:200])

    threading.Thread(target=_bg, daemon=True).start()
    return {
        "ok": True, "queued": True, "busy": False, "job": job,
        "total": uni["total"], "items": uni["items"], "mode": uni["mode"],
        "log": str(log),
    }


# ----------------- 短线猎手（纯看板建议层） -----------------


@app.get("/api/swing/catalog")
def api_swing_catalog():
    """短线猎手目录：活跃预测 + 累计统计 + 最新预测日全量决策。"""
    sys.path.insert(0, str(QUANT))
    from overlays.swing_hunter import store as SW  # noqa: WPS433
    from overlays.swing_hunter.schema import (  # noqa: WPS433
        latest_prediction_day, read_predictions)

    cat = SW.load_catalog()
    day = latest_prediction_day()
    pf = read_predictions(day) if day else None
    preds = []
    if pf:
        # predict 在前、watch 次之、reject 垫底；同级按 swing_score 降序
        order = {"predict": 0, "watch": 1, "reject": 2}
        preds = sorted(
            (p.to_dict() for p in pf.predictions),
            key=lambda p: (order.get(p.get("action"), 3), -float(p.get("swing_score") or 0)),
        )
    return {
        "latest_date": cat.get("latest_date"),
        "active": cat.get("active", []),
        "stats": cat.get("stats", {}),
        "prediction_day": day,
        "prediction_status": pf.status if pf else None,
        "prediction_meta": pf.meta if pf else {},
        "predictions": preds,
        "updated_at": cat.get("updated_at"),
    }


@app.get("/api/swing/report")
def api_swing_report(day: str | None = None):
    """每日 Markdown 报告（predictions/YYYY-MM-DD.md）。"""
    sys.path.insert(0, str(QUANT))
    from overlays.swing_hunter.schema import latest_prediction_day, read_predictions  # noqa: WPS433
    from overlays.swing_hunter import report as RPT  # noqa: WPS433

    day = day or latest_prediction_day()
    if not day:
        return {"day": None, "markdown": "", "meta": {}}
    md_path = QUANT / "data" / "overlays" / "swing_hunter" / "predictions" / f"{day}.md"
    if not md_path.exists():
        pf = read_predictions(day)
        if pf:
            RPT.write_daily_report(day, pf)
    markdown = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    pf = read_predictions(day)
    return {"day": day, "markdown": markdown, "meta": pf.meta if pf else {}}


@app.get("/api/swing/eval")
def api_swing_eval(day: str | None = None):
    """LLM 双路评测报告（eval/YYYY-MM-DD/comparison.md）。"""
    eval_root = QUANT / "data" / "overlays" / "swing_hunter" / "eval"
    days = []
    if eval_root.exists():
        days = sorted(d.name for d in eval_root.iterdir()
                      if d.is_dir() and len(d.name) == 10 and d.name[4] == "-")
    if not days:
        return {"days": [], "day": None, "markdown": "", "files": {}}
    day = day or days[-1]
    edir = eval_root / day
    cmp_path = edir / "comparison.md"
    files = {}
    for name in ("pass1_local_top15.json", "pass2_deepseek_top5.json", "run.log"):
        p = edir / name
        if p.exists():
            files[name] = str(p)
    markdown = cmp_path.read_text(encoding="utf-8") if cmp_path.exists() else ""
    return {"days": days, "day": day, "markdown": markdown, "files": files}


@app.get("/api/swing/detail/{instrument}")
def api_swing_detail(instrument: str):
    """单票：跟踪全记录 + 最新预测 + delta 时间线。"""
    inst = instrument.upper()
    if not (len(inst) >= 6 and inst[:2].isalpha() and inst[2:].isdigit()):
        raise HTTPException(400, "标的格式应为 SH600000 / SZ000001")
    sys.path.insert(0, str(QUANT))
    from overlays.swing_hunter import store as SW  # noqa: WPS433
    from overlays.swing_hunter.schema import latest_prediction_day, read_predictions  # noqa: WPS433

    tracker = SW.load_tracker(inst)
    pred_day = latest_prediction_day()
    prediction = None
    if pred_day:
        pf = read_predictions(pred_day)
        if pf:
            for p in pf.predictions:
                if p.instrument == inst:
                    prediction = p.to_dict()
                    break
    active_rec = None
    deltas: list[dict] = []
    for r in tracker.get("records", []):
        if r.get("state") in {"triggered", "holding"}:
            active_rec = r
        for d in r.get("deltas") or []:
            deltas.append({**d, "pred_date": r.get("pred_date")})
    deltas.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return {
        "instrument": inst,
        "prediction_day": pred_day,
        "prediction": prediction,
        "tracker": tracker,
        "active_record": active_rec,
        "deltas": deltas[:40],
    }


@app.get("/api/swing/patterns")
def api_swing_patterns(limit: int = 30):
    """达标案例挖掘的模式库（swing_patterns.yaml）。"""
    sys.path.insert(0, str(QUANT))
    from overlays.swing_hunter.pattern_mine import load_patterns, PATTERNS_PATH  # noqa: WPS433
    lim = min(max(int(limit), 1), 100)
    return {
        "patterns": load_patterns(lim),
        "path": str(PATTERNS_PATH),
        "count": len(load_patterns(500)),
    }


@app.get("/api/swing/tracking")
def api_swing_tracking(limit: int = 60):
    """跟踪记录（不含逐日明细，列表视图）+ 收盘口径统计。"""
    sys.path.insert(0, str(QUANT))
    from overlays.swing_hunter import store as SW  # noqa: WPS433
    recs = SW.all_records(limit_per_stock=10)
    out = []
    for r in recs[:limit]:
        d = r.to_dict()
        d.pop("daily", None)
        out.append(d)
    return {"records": out, "stats": SW.compute_stats()}


@app.get("/api/swing/track/{instrument}")
def api_swing_track_one(instrument: str):
    """单票全部预测与逐日跟踪明细（弹窗用）。"""
    sys.path.insert(0, str(QUANT))
    from overlays.swing_hunter import store as SW  # noqa: WPS433
    return SW.load_tracker(instrument.upper())


@app.get("/api/swing/job")
def api_swing_job():
    """当前/最近一次短线猎手任务进度（供进度条与刷新恢复）。"""
    sys.path.insert(0, str(QUANT))
    from overlays.swing_hunter import job as SJ  # noqa: WPS433
    return SJ.read_job()


@app.post("/api/swing/run")
def api_swing_run(request: Request,
                  account: str = "live_manual_10k",
                  dry_run: bool = False,
                  track_only: bool = False,
                  force: bool = True):
    """手动触发短线猎手（后台线程，纯建议层，不改订单）。

    页面点击默认 force=True，忽略同日已有预测，强制重跑 LLM。
    """
    _require_full_access(request)
    _check(account)
    sys.path.insert(0, str(QUANT))
    from overlays.swing_hunter import job as SJ  # noqa: WPS433

    running = SJ.read_job()
    if running.get("status") == "running":
        return {
            "ok": True,
            "queued": False,
            "busy": True,
            "job": running,
            "account": account,
            "dry_run": dry_run,
            "track_only": track_only,
            "log": str(LOG_DIR / f"swing_hunter_{datetime.now():%Y-%m-%d}.log"),
        }

    log = LOG_DIR / f"swing_hunter_{datetime.now():%Y-%m-%d}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [PY, str(QUANT / "overlays" / "swing_hunter" / "run_swing.py"),
           "--account", account]
    if dry_run:
        cmd.append("--dry-run")
    if track_only:
        cmd.append("--track-only")
    if force:
        cmd.append("--force")
    # 默认强制本地 LLM_PEAK_*（见 LLM_OVERLAYS_LOCAL_ONLY）
    cmd += ["--force-llm", "peak"]

    job = SJ.start_job(account=account, dry_run=dry_run, track_only=track_only)

    def _bg():
        with log.open("a") as fh:
            fh.write(f"\n=== {datetime.now():%F %T} {' '.join(cmd[1:])} ===\n")
            fh.flush()
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    env={**dict(__import__("os").environ), "PYTHONUNBUFFERED": "1"},
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    fh.write(line)
                    fh.flush()
                    try:
                        SJ.update_from_line(line)
                    except Exception:
                        pass
                rc = proc.wait()
                # run_swing 内部也会 finish_job；若进程异常退出再兜底
                cur = SJ.read_job()
                if cur.get("status") == "running":
                    SJ.finish_job(
                        ok=(rc == 0),
                        message="短线猎手完成" if rc == 0 else f"退出码 {rc}",
                    )
            except Exception as e:  # noqa: BLE001
                fh.write(f"[job-error] {e}\n")
                SJ.finish_job(ok=False, message=str(e)[:200])

    import threading
    threading.Thread(target=_bg, daemon=True).start()
    return {
        "ok": True,
        "queued": True,
        "busy": False,
        "job": SJ.read_job(),
        "account": account,
        "dry_run": dry_run,
        "track_only": track_only,
        "log": str(log),
        "job_id": job.get("id"),
    }


# ----------------- 研究分析（4 分析师 + 中英双辩论 → 可结算预测） -----------------


@app.get("/api/research/catalog")
def api_research_catalog(account: str | None = None):
    """研究宇宙目录：合并三源（舆情/短线/订单）+ catalog 最新报告摘要。"""
    sys.path.insert(0, str(QUANT))
    from overlays.research import store as RS  # noqa: WPS433
    from overlays.research.universe import build_research_universe

    cat = RS.load_catalog()
    by_inst = {k.upper(): v for k, v in (cat.get("instruments") or {}).items()}

    # 研究宇宙（account 缺则只取已跟踪）
    try:
        day = RS.latest_research_day() or datetime.now().strftime("%Y-%m-%d")
        uni = build_research_universe(day, account=account)
    except Exception:  # noqa: BLE001
        uni = []
    uni_map = {e["instrument"]: e for e in uni}

    instruments = []
    seen: set[str] = set()
    # 先放宇宙里的票（带 sources 富化）
    for inst, e in uni_map.items():
        ent = by_inst.get(inst, {})
        instruments.append({
            "instrument": inst,
            "name": ent.get("name") or "",
            "sources": e.get("sources") or [],
            "swing_action": e.get("swing_action"),
            "swing_score": e.get("swing_score"),
            "order_side": e.get("order_side"),
            "sentiment_score": e.get("sentiment_score"),
            "latest_date": ent.get("latest_date"),
            "merged_direction": ent.get("merged_direction"),
            "merged_confidence": ent.get("merged_confidence"),
            "consensus": ent.get("consensus"),
            "action_cn": ent.get("action_cn"),
            "action_en": ent.get("action_en"),
            "pred_id": ent.get("pred_id"),
        })
        seen.add(inst)
    # 再补 catalog 里有但宇宙没覆盖的（历史跟踪）
    for inst, ent in by_inst.items():
        if inst in seen:
            continue
        instruments.append({
            "instrument": inst, "name": ent.get("name") or "",
            "sources": ent.get("sources") or [],
            "latest_date": ent.get("latest_date"),
            "merged_direction": ent.get("merged_direction"),
            "merged_confidence": ent.get("merged_confidence"),
            "consensus": ent.get("consensus"),
            "action_cn": ent.get("action_cn"),
            "action_en": ent.get("action_en"),
            "pred_id": ent.get("pred_id"),
        })

    instruments.sort(key=lambda x: (
        0 if x.get("latest_date") else 1,
        str(x.get("latest_date") or ""),
    ), reverse=True)
    return {
        "updated_at": cat.get("updated_at"),
        "peak_hour": _sentiment_peak_now(),
        "account": account,
        "instruments": instruments,
    }


@app.get("/api/research/job")
def api_research_job():
    """当前/最近一次研究分析任务进度。"""
    sys.path.insert(0, str(QUANT))
    from overlays.research import job as RJ  # noqa: WPS433
    return RJ.read_job()


@app.get("/api/research/{instrument}")
def api_research_instrument(instrument: str, days: int = 90):
    """单票研究报告（CN/EN 双裁决 + 分析师）+ 近 N 日历史。"""
    inst = instrument.upper()
    if not (len(inst) >= 6 and inst[:2].isalpha() and inst[2:].isdigit()):
        raise HTTPException(400, "标的格式应为 SH600000 / SZ000001")
    sys.path.insert(0, str(QUANT))
    from overlays.research import store as RS  # noqa: WPS433
    report = RS.load_report(inst)
    history = RS.list_reports(inst, limit=min(max(days, 30), 120))
    return {
        "instrument": inst,
        "report": report,
        "history": history,
        "peak_hour": _sentiment_peak_now(),
    }


@app.post("/api/research/run")
def api_research_run(request: Request,
                     account: str = "live_manual_10k",
                     dry_run: bool = False,
                     force: bool = False,
                     instrument: str | None = None):
    """手动触发研究分析（后台线程）。

    - 不传 instrument：跑三源合并研究宇宙
    - 传 instrument：只研究该票
    """
    _require_full_access(request)
    sys.path.insert(0, str(QUANT))
    from overlays.sentiment_memory.run_memory import normalize_instrument
    from overlays.research import job as RJ  # noqa: WPS433

    inst = None
    if instrument:
        inst = normalize_instrument(instrument)
        if not inst:
            raise HTTPException(400, "标的格式应为 SH600000 / SZ000001 / 600000")
    else:
        _check(account)

    running = RJ.read_job()
    if running.get("status") == "running":
        return {
            "ok": True, "queued": False, "busy": True, "job": running,
            "account": running.get("account"),
            "dry_run": bool(running.get("dry_run")),
            "log": str(LOG_DIR / f"research_{datetime.now():%Y-%m-%d}.log"),
        }

    log = LOG_DIR / f"research_{datetime.now():%Y-%m-%d}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [PY, str(QUANT / "overlays" / "research" / "run_research.py"),
           "--force-llm", "peak"]
    if inst:
        cmd += ["--instruments", inst]
    else:
        cmd += ["--account", account]
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")

    job = RJ.start_job(account=None if inst else account, dry_run=dry_run)
    if inst:
        RJ.write_job({"total": 1, "message": f"研究 {inst}…", "pct": 8})

    def _bg():
        with log.open("a") as fh:
            fh.write(f"\n=== {datetime.now():%F %T} {' '.join(cmd[1:])} ===\n")
            fh.flush()
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    env={**dict(__import__("os").environ), "PYTHONUNBUFFERED": "1"},
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    fh.write(line)
                    fh.flush()
                    try:
                        RJ.update_from_line(line)
                    except Exception:
                        pass
                rc = proc.wait()
                cur = RJ.read_job()
                if cur.get("status") == "running":
                    RJ.finish_job(
                        ok=(rc == 0),
                        message="研究分析完成" if rc == 0 else f"退出码 {rc}",
                    )
            except Exception as e:  # noqa: BLE001
                fh.write(f"[job-error] {e}\n")
                RJ.finish_job(ok=False, message=str(e)[:200])

    import threading
    threading.Thread(target=_bg, daemon=True).start()
    return {
        "ok": True, "queued": True, "busy": False, "job": RJ.read_job(),
        "account": None if inst else account, "instrument": inst,
        "dry_run": dry_run, "log": str(log), "job_id": job.get("id"),
    }


@app.post("/api/run/{stage}/{account}")
def api_run(request: Request, stage: str, account: str):
    """手动触发一次 evening/postclose（用于演示或补跑）。"""
    _require_full_access(request)
    _check(account)
    if stage not in ("evening", "postclose"):
        raise HTTPException(400, "stage 必须是 evening/postclose")
    _run_daily(stage, account)
    return {"ok": True, "stage": stage, "account": account}


def _check(account: str) -> None:
    if account not in ACCOUNTS:
        raise HTTPException(404, f"未知账户 {account}")


# ----------------------------- 大盘看板（market_board，池内统计，只读） -----------------------------
from overlays.market_board import store as board_store  # noqa: E402

BOARD_JOB = LOG_DIR / "market_board_job.json"
BOARD_REFRESH_JOB = LOG_DIR / "market_board_refresh_job.json"


def _read_board_job() -> dict:
    if not BOARD_JOB.exists():
        return {"status": "idle"}
    try:
        return json.loads(BOARD_JOB.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "idle"}


def _write_board_job(payload: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    BOARD_JOB.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_board_refresh_job() -> dict:
    if not BOARD_REFRESH_JOB.exists():
        return {"status": "idle"}
    try:
        return json.loads(BOARD_REFRESH_JOB.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "idle"}


def _write_board_refresh_job(payload: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    BOARD_REFRESH_JOB.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _board_payload(day: str | None) -> tuple[str | None, dict | None]:
    """指定日快照；空则取最新。"""
    if day:
        return day, board_store.load_daily(day)
    return board_store.load_latest_daily()


@app.get("/api/board/days")
def api_board_days():
    """可用快照日期列表（供前端切换盘后日期）。"""
    return {"ok": True, "days": board_store.list_daily_days(40)}


@app.get("/api/board/overview")
def api_board_overview(day: str | None = None):
    """全景：温度计 + 涨跌榜 + 情绪/30日周期 + 指数环境（需求 1/2/4）。"""
    d, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False, "job": _read_board_job(),
                "disclaimer": "尚无盘后快照。点「生成快照」或等 evening 自动跑。"}
    cyc = snap.get("cycle") or {}
    return {
        "ok": True, "available": True, "day": snap.get("day"),
        "snapshot_day": d, "status": snap.get("status"), "errors": snap.get("errors") or [],
        "pool": snap.get("pool"), "generated": snap.get("generated"),
        "thermometer": snap.get("thermometer") or {},
        "temperature": cyc.get("temperature"), "temperature_label": cyc.get("temperature_label"),
        "emotion": cyc.get("emotion"), "emotion_reasons": cyc.get("emotion_reasons") or [],
        "period30": cyc.get("period30"), "period30_reasons": cyc.get("period30_reasons") or [],
        "index_env": snap.get("index_env") or {},
        "gainers": snap.get("gainers") or [], "losers": snap.get("losers") or [],
        "job": _read_board_job(),
        "disclaimer": snap.get("disclaimer"),
    }


@app.get("/api/board/cycle")
def api_board_cycle(day: str | None = None):
    """15 日情绪曲线 + 30 日周期序列（需求 2）。"""
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False}
    cyc = snap.get("cycle") or {}
    return {
        "ok": True, "available": True, "day": snap.get("day"),
        "series15": cyc.get("series15") or [],
        "series30": cyc.get("series30") or [],
        "emotion": cyc.get("emotion"), "period30": cyc.get("period30"),
        "daily_stats": snap.get("daily_stats") or [],
    }


@app.get("/api/board/ladder")
def api_board_ladder(day: str | None = None):
    """连板梯队 + 存活率 + 行业分布（需求 4、5 部分）。"""
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False}
    return {
        "ok": True, "available": True, "day": snap.get("day"),
        "ladder": snap.get("ladder") or {},
        "industry_dist": snap.get("industry_dist") or [],
        "thermometer": snap.get("thermometer") or {},
    }


@app.get("/api/board/strong")
def api_board_strong(day: str | None = None):
    """强势个股榜（需求 7，P1 三维口径）。"""
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False}
    strong = snap.get("strong") or {}
    return {
        "ok": True, "available": True, "day": snap.get("day"),
        "stocks": strong.get("stocks") or [],
        "weights": strong.get("weights") or {},
        "caliber": strong.get("caliber"),
    }


@app.post("/api/board/run")
def api_board_run(request: Request, day: str | None = None, force: bool = False):
    """后台重跑 run_board.py 生成盘后快照（fail-open，不改订单）。"""
    _require_full_access(request)
    job = _read_board_job()
    if job.get("status") == "running":
        return {"ok": True, "queued": False, "busy": True, "job": job}
    started = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
    _write_board_job({"status": "running", "started": started,
                      "message": "正在统计池内涨跌停/周期/梯队…", "pct": 10})
    log_file = LOG_DIR / f"market_board_{datetime.now():%Y-%m-%d}.log"
    cmd = [PY, str(QUANT / "overlays" / "market_board" / "run_board.py")]
    if day:
        cmd += ["--day", day]
    if force:
        cmd += ["--force"]

    def _bg():
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {started} {' '.join(cmd[1:])} ===\n")
            fh.flush()
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, cwd=str(QUANT),
                    env={**dict(os.environ), "PYTHONUNBUFFERED": "1"},
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    fh.write(line)
                    fh.flush()
                rc = proc.wait()
                _write_board_job({
                    "status": "ok" if rc == 0 else "error",
                    "started": started,
                    "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                    "message": "已更新" if rc == 0 else f"退出码 {rc}",
                    "pct": 100, "rc": rc,
                })
            except Exception as exc:  # noqa: BLE001
                _write_board_job({
                    "status": "error", "started": started,
                    "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                    "message": str(exc), "pct": 100,
                })

    import threading
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "queued": True, "busy": False, "job": _read_board_job(),
            "log": str(log_file)}


@app.get("/api/board/intraday")
def api_board_intraday():
    """盘中实时快照（手动刷新产物）：温度计/涨跌榜/涨停预警/封单散点。"""
    data = board_store.load_intraday()
    if not data:
        return {"ok": True, "available": False,
                "disclaimer": "尚无盘中快照。点「刷新行情」拉取实时报价（约10秒）。"}
    if not board_store.intraday_is_fresh(data):
        snap_day, _ = board_store.load_latest_daily()
        return {
            "ok": True, "available": False, "stale": True,
            "generated": data.get("generated"), "snapshot_day": snap_day,
            "disclaimer": "盘中快照已过期（非当日）。点「刷新行情」或等 10:00 自动刷新。",
            "job": _read_board_refresh_job(),
        }
    return {"ok": True, "available": True, **data, "job": _read_board_refresh_job()}


@app.post("/api/board/refresh")
def api_board_refresh(request: Request):
    """手动拉取腾讯批量报价，写 intraday/latest.json（后台执行）。"""
    _require_full_access(request)
    job = _read_board_refresh_job()
    if job.get("status") == "running":
        return {"ok": True, "queued": False, "busy": True, "job": job}
    started = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
    _write_board_refresh_job({"status": "running", "started": started,
                              "message": "正在拉取池内批量实时报价…", "pct": 20})

    def _bg():
        try:
            from overlays.market_board import intraday as board_intraday
            payload = board_intraday.build_intraday()
            ok = bool(payload.get("ok"))
            _write_board_refresh_job({
                "status": "ok" if ok else "error",
                "started": started,
                "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                "message": (f"已刷新 {payload.get('quotes_ok')}/{payload.get('quotes_total')} 只"
                            if ok else payload.get("error", "刷新失败")),
                "pct": 100,
            })
        except Exception as exc:  # noqa: BLE001
            _write_board_refresh_job({
                "status": "error", "started": started,
                "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                "message": str(exc), "pct": 100,
            })

    import threading
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "queued": True, "busy": False,
            "job": _read_board_refresh_job()}


@app.get("/api/board/limit-scatter")
def api_board_limit_scatter(day: str | None = None):
    """封单强度散点图：看最新盘后日（或未指定日）时优先盘中；历史日强制盘后口径。"""
    latest_d, _ = board_store.load_latest_daily()
    use_live = (not day) or (latest_d and day == latest_d)
    if use_live:
        intra = board_store.load_intraday()
        if intra and board_store.intraday_is_fresh(intra) and intra.get("ok") and intra.get("scatter"):
            return {"ok": True, "available": True, "mode": "intraday",
                    "generated": intra.get("generated"),
                    "scatter": intra.get("scatter") or [],
                    "warn_near_limit": intra.get("warn_near_limit") or [],
                    "thermometer": intra.get("thermometer") or {}}
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False,
                "disclaimer": "无盘后/盘中数据。先生成快照或刷新行情。"}
    # 盘后口径：无封单数据（EOD 无盘口），散点退化为 连板数×成交额
    scatter = []
    for t in (snap.get("ladder") or {}).get("tiers") or []:
        for s in t.get("stocks") or []:
            scatter.append({
                "instrument": s["instrument"], "name": s.get("name"),
                "industry": s.get("industry"), "x": 0.0,
                "y": s.get("streak"), "r_amt": round((s.get("amount") or 0) / 1e8, 2),
                "limit_type": s.get("limit_type"),
            })
    for s in (snap.get("ladder") or {}).get("first_boards") or []:
        scatter.append({
            "instrument": s["instrument"], "name": s.get("name"),
            "industry": s.get("industry"), "x": 0.0,
            "y": 1, "r_amt": round((s.get("amount") or 0) / 1e8, 2),
            "limit_type": s.get("limit_type"),
        })
    return {"ok": True, "available": True, "mode": "eod",
            "generated": snap.get("generated"), "scatter": scatter,
            "warn_near_limit": [], "thermometer": snap.get("thermometer") or {},
            "note": "盘后口径无封单数据（x=0 列）；点「刷新行情」看盘中封单强度。"}


@app.get("/api/board/stock/{instrument}")
def api_board_stock(instrument: str):
    """单票五维评分卡（点击散点气泡/榜单弹出）。"""
    from overlays.market_board import stock_card
    card = stock_card.build_card(instrument)
    if not card.get("ok"):
        raise HTTPException(404, card.get("error", "无数据"))
    return card


@app.get("/api/board/themes")
def api_board_themes(day: str | None = None):
    """题材热度 + 行业榜（东财概念/行业板块，池内命中标注）（需求 5）。"""
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False}
    t = snap.get("themes") or {}
    return {"ok": True, "available": bool(t.get("available")), "day": snap.get("day"),
            "concepts": t.get("concepts") or [], "industries": t.get("industries") or [],
            "industry_dist": snap.get("industry_dist") or []}


@app.get("/api/board/rotation")
def api_board_rotation(day: str | None = None):
    """板块轮动矩阵：池内行业 × 近5/10/20日涨跌幅（需求 6）。"""
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False}
    rot = snap.get("rotation") or {}
    return {"ok": True, "available": bool(rot.get("rows")), "day": snap.get("day"),
            "rows": rot.get("rows") or []}


@app.get("/api/board/fundflow")
def api_board_fundflow(day: str | None = None):
    """资金流向榜：主力净流入/流出 TOP（池内）（需求 8）。"""
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False}
    f = snap.get("fundflow") or {}
    return {"ok": True, "available": bool(f.get("available")), "day": snap.get("day"),
            "inflow": f.get("inflow") or [], "outflow": f.get("outflow") or []}


@app.get("/api/board/news")
def api_board_news(day: str | None = None):
    """资讯快讯：全局电报（池内命中标注）+ 池内个股动态（需求 9）。"""
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False}
    n = snap.get("news") or {}
    return {"ok": True, "available": bool(n.get("available")), "day": snap.get("day"),
            "global_feed": n.get("global_feed") or [],
            "pool_feed": n.get("pool_feed") or [],
            "stats": n.get("stats") or {},
            "error": n.get("error")}


@app.get("/api/board/unlock")
def api_board_unlock(day: str | None = None):
    """解禁雷区清单（需求 4）：未来30日、占流通≥1%，高危≥10%标红。"""
    _, snap = _board_payload(day)
    if not snap:
        return {"ok": True, "available": False}
    return {"ok": True, "available": True, "day": snap.get("day"),
            "alerts": snap.get("unlock_alerts") or []}


# ----------------------------- 板块预测（sector_forecast，只读） -----------------------------


@app.get("/api/sector/forecast")
def api_sector_forecast(day: str | None = None):
    """双期限申万一级行业预测（池内等权 vs 中证500）。"""
    sys.path.insert(0, str(QUANT))
    from overlays.sector_forecast import store as SF
    if day:
        pred = SF.load_prediction(day)
        d = day
    else:
        d, pred = SF.load_latest()
    if not pred:
        return {"ok": True, "available": False,
                "disclaimer": "尚无板块预测。evening 自动跑，或 POST /api/sector/run。"}
    return {"ok": True, "available": True, "day": d, **pred, "job": SF.read_job()}


@app.get("/api/sector/eval")
def api_sector_eval():
    """OOS 成绩单 + 线上已结算命中率。"""
    sys.path.insert(0, str(QUANT))
    from overlays.sector_forecast import store as SF
    oos = SF.load_json(SF.eval_path("oos.json")) or SF.load_json(SF.eval_path("scorecard.json"))
    live = SF.load_json(SF.eval_path("live.json"))
    meta = SF.load_json(SF.model_dir() / "meta.json")
    return {"ok": True, "oos": oos, "live": live, "meta": meta}


@app.post("/api/sector/run")
def api_sector_run(request: Request, day: str | None = None, force: bool = False,
                   retrain: bool = False, brief_only: bool = False,
                   skip_llm: bool = False):
    """后台重跑板块预测（fail-open，不改订单）。"""
    _require_full_access(request)
    sys.path.insert(0, str(QUANT))
    from overlays.sector_forecast import store as SF
    job = SF.read_job()
    if job.get("status") == "running":
        return {"ok": True, "queued": False, "busy": True, "job": job}
    started = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
    SF.write_job({"status": "running", "started": started, "pct": 5,
                  "message": "正在补 LLM 简报…" if brief_only else "正在构建行业特征…"})
    log_file = LOG_DIR / f"sector_forecast_{datetime.now():%Y-%m-%d}.log"
    cmd = [PY, str(QUANT / "overlays" / "sector_forecast" / "run_forecast.py"),
           "--progress"]
    if day:
        cmd += ["--day", day]
    if force:
        cmd += ["--force"]
    if retrain:
        cmd += ["--retrain"]
    if brief_only:
        cmd += ["--brief-only"]
    if skip_llm:
        cmd += ["--skip-llm"]

    def _bg():
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {started} {' '.join(cmd[1:])} ===\n")
            fh.flush()
            try:
                proc = subprocess.Popen(
                    cmd, stdout=fh, stderr=subprocess.STDOUT,
                    text=True, cwd=str(QUANT),
                    env={**dict(os.environ), "PYTHONUNBUFFERED": "1"},
                )
                rc = proc.wait()
                cur = SF.read_job()
                if cur.get("status") == "running":
                    SF.write_job({
                        "status": "ok" if rc == 0 else "error",
                        "started": started, "pct": 100, "rc": rc,
                        "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                        "message": "已更新" if rc == 0 else f"退出码 {rc}",
                    })
            except Exception as exc:  # noqa: BLE001
                SF.write_job({
                    "status": "error", "started": started, "pct": 100,
                    "message": str(exc),
                    "finished": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                })

    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "queued": True, "busy": False, "job": SF.read_job(),
            "log": str(log_file)}
