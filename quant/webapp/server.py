"""阶段5 后台常驻看板服务：FastAPI + 内置 APScheduler。

- Web 看板（默认 0.0.0.0:8000）：两条线净值/超额曲线、持仓、成交、对账、双线对比、告警。
- 内置定时（Asia/Shanghai，工作日）：
    22:30 evening   两条线生成次日调仓清单（含 UMP/风控预检）
    23:30 postclose 两条线按 mode 处理成交（simulated→自动模拟；manual→读人工回填）→对账→净值→日报
    周五 23:45 双线复盘
  定时任务即调用经过验证的 ops/run_daily.py，服务进程只做编排与展示。

启动：bash webapp/serve.sh start    （或 uvicorn webapp.server:app --host 0.0.0.0 --port 8000）
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
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
PY = sys.executable
RUN_DAILY = QUANT / "ops" / "run_daily.py"
REVIEW = QUANT / "ops" / "review_accounts.py"

# 看板纳管的账户（研究模拟线 + 实盘线）
ACCOUNTS = ["research_sim_100k", "live_manual_10k", "live_manual_10k_new"]
RESEARCH, LIVE = ACCOUNTS[0], ACCOUNTS[1]
TZ = "Asia/Shanghai"

templates = Jinja2Templates(directory=str(HERE / "templates"))


# ----------------------------- 数据读取 -----------------------------
def _account_label(account: str) -> str:
    cfg = C.account_config(account).get("account", {})
    cap = cfg.get("initial_capital")
    mode = cfg.get("mode")
    return f"{account}（{cap:,.0f}元·{mode}）" if cap else account


def daily_series(account: str) -> dict:
    d = RA.load_daily(account)
    if d.empty:
        return {"account": account, "dates": [], "series": {}}
    acc = C.load_account(account) or {}
    start = float(acc.get("start_capital", d.iloc[0]["nav"]))
    cum_ret = (d["nav"] / start - 1.0)
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
    }


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
    cfg = C.account_config(account).get("account", {})
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
    tp_f = dirs["target_position"] / f"{order_day}.csv"
    orders = S.read_csv("orders", orders_f) if orders_f.exists() else pd.DataFrame(
        columns=["instrument", "side", "shares", "ref_price"])
    target = S.read_csv("target_position", tp_f) if tp_f.exists() else pd.DataFrame(
        columns=["instrument", "shares", "last_price", "entry_date"])

    n_trades = len(orders)
    sells = orders[orders["side"].str.upper() == "SELL"] if n_trades else orders
    buys = orders[orders["side"].str.upper() == "BUY"] if n_trades else orders

    acc = C.load_account(account) or {}
    last_fill = acc.get("last_fill_date")
    fills_done = (dirs["fills"] / f"{execute_day}.csv").with_suffix(".done").exists() \
        if execute_day else False
    applied = bool(execute_day and last_fill and str(last_fill) >= execute_day and fills_done)

    if n_trades == 0:
        status, status_label = "no_trade", "无需调仓"
        summary = f"订单日 {order_day}，持仓维持不变"
        if applied:
            status_label = "无需调仓 · 已结算"
    elif applied:
        status, status_label = "done", "已执行"
        summary = f"卖出 {len(sells)} 笔 / 买入 {len(buys)} 笔（{execute_day} 已模拟成交）"
    else:
        status, status_label = "pending", "待执行"
        exec_hint = execute_day or "待定"
        if mode == "simulated":
            summary = (f"卖出 {len(sells)} 笔 / 买入 {len(buys)} 笔；"
                       f"执行日 {exec_hint} postclose 自动模拟成交")
        else:
            summary = (f"卖出 {len(sells)} 笔 / 买入 {len(buys)} 笔；"
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
    return {
        "data_day": data_day,
        "plans": [daily_ops_plan(a) for a in ACCOUNTS],
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


def job_postclose() -> None:
    for a in ACCOUNTS:
        _run_daily("postclose", a)


def job_review() -> None:
    log = LOG_DIR / f"review_{datetime.now():%Y-%m-%d}.log"
    with log.open("a") as fh:
        subprocess.run([PY, str(REVIEW), "--research", RESEARCH, "--live", LIVE],
                       stdout=fh, stderr=subprocess.STDOUT)


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
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = BackgroundScheduler(timezone=TZ)
    # 工作日 22:30 出次日清单；23:30 收盘后对账/净值；周五 23:45 双线复盘
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"accounts": ACCOUNTS})


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
    dr, dl = RA.load_daily(RESEARCH), RA.load_daily(LIVE)
    common = []
    if not dr.empty and not dl.empty:
        m = dr[["date", "daily_ret", "excess_ret"]].merge(
            dl[["date", "daily_ret", "excess_ret"]], on="date",
            how="inner", suffixes=("_r", "_l"))
        for r in m.itertuples():
            common.append({"date": r.date,
                           "ret_research": round(r.daily_ret_r * 100, 3),
                           "ret_live": round(r.daily_ret_l * 100, 3),
                           "gap": round((r.daily_ret_l - r.daily_ret_r) * 100, 3)})
    fd = RA.fill_diff(RESEARCH, LIVE)
    diffs = []
    if not fd.empty:
        for r in fd.sort_values("adverse_slip_pct", ascending=False).head(20).itertuples():
            diffs.append({"date": r.date, "instrument": r.instrument, "side": r.side,
                          "research_price": round(r.research_price, 2),
                          "live_price": round(r.live_price, 2),
                          "adverse_slip_pct": round(r.adverse_slip_pct, 3)})
    return {"summary": {RESEARCH: RA.summary(RESEARCH), LIVE: RA.summary(LIVE)},
            "common_days": common, "fill_diff": diffs}


@app.get("/api/alerts")
def api_alerts():
    return {"alerts": alerts()}


@app.get("/api/sentiment")
def api_sentiment():
    """获取情绪因子数据。"""
    import json
    from pathlib import Path
    
    # 读取最新的情绪信号文件
    signals_dir = QUANT / "data" / "signals"
    sentiment_files = sorted(signals_dir.glob("sentiment_*.csv"), reverse=True)
    
    if not sentiment_files:
        return {"date": None, "signals": [], "summary": {}}
    
    latest_file = sentiment_files[0]
    date = latest_file.stem.replace("sentiment_", "")
    
    # 读取信号数据
    import pandas as pd
    df = pd.read_csv(latest_file)
    
    signals = []
    for _, row in df.iterrows():
        signals.append({
            "instrument": row["instrument"],
            "score": round(row["score"], 4),
            "rank": int(row["rank"]),
        })
    
    # 计算摘要
    summary = {
        "count": len(signals),
        "avg_score": round(df["score"].mean(), 4),
        "max_score": round(df["score"].max(), 4),
        "min_score": round(df["score"].min(), 4),
        "positive_count": int((df["score"] > 0).sum()),
        "negative_count": int((df["score"] < 0).sum()),
    }
    
    return {"date": date, "signals": signals, "summary": summary}


@app.post("/api/sentiment/refresh")
def api_sentiment_refresh():
    """刷新情绪因子数据。"""
    import subprocess
    
    try:
        # 获取当前持仓股票列表
        instruments = []
        for account in ACCOUNTS:
            holdings_file = QUANT / "data" / "accounts" / account / "nav" / "holdings.csv"
            if holdings_file.exists():
                import pandas as pd
                df = pd.read_csv(holdings_file)
                instruments.extend(df["instrument"].tolist())
        
        # 如果没有持仓，使用默认列表
        if not instruments:
            instruments = [
                "SH600519", "SH601318", "SZ000858", "SH600036", "SZ000333",
                "SH600276", "SH601888", "SZ000651", "SH600900", "SH601012",
            ]
        
        # 去重
        instruments = list(set(instruments))
        
        # 运行情绪分析
        result = subprocess.run(
            [sys.executable, "-c", f"""
import sys
sys.path.insert(0, '{QUANT}')
from factor_lab.sentiment_factor import generate_sentiment_signals, save_sentiment_signals
signals = generate_sentiment_signals({instruments})
save_sentiment_signals(signals)
"""],
            capture_output=True,
            text=True,
            timeout=120,
        )
        
        if result.returncode == 0:
            return {"ok": True, "message": f"情绪分析完成，处理 {len(instruments)} 只股票"}
        else:
            return {"ok": False, "message": f"情绪分析失败: {result.stderr[:200]}"}
            
    except Exception as e:
        return {"ok": False, "message": f"情绪分析失败: {str(e)}"}


@app.get("/api/portfolio")
def api_portfolio():
    """获取组合优化数据。"""
    # 返回预计算的优化结果
    # 实际使用时可以缓存结果或实时计算
    return {
        "strategies": [
            {"method": "mean_risk", "annual_return": 0.1276, "annual_vol": 0.1245, "sharpe": 0.86, "max_drawdown": 0.2016},
            {"method": "risk_parity", "annual_return": 0.0924, "annual_vol": 0.1388, "sharpe": 0.52, "max_drawdown": 0.2194},
            {"method": "inverse_vol", "annual_return": 0.0948, "annual_vol": 0.1435, "sharpe": 0.52, "max_drawdown": 0.2271},
            {"method": "hrp", "annual_return": 0.1056, "annual_vol": 0.1386, "sharpe": 0.62, "max_drawdown": 0.2062},
        ],
        "weight_stats": {
            "n_assets": 20,
            "effective_n": 10.04,
            "top5_weight": 0.586,
            "gini": 0.55,
        },
        "param_grid": [
            {"topk": 10, "max_weight": 0.15, "sharpe": 1.23, "annual_return": 0.1919},
            {"topk": 10, "max_weight": 0.20, "sharpe": 1.23, "annual_return": 0.1904},
            {"topk": 15, "max_weight": 0.10, "sharpe": 1.13, "annual_return": 0.1688},
            {"topk": 15, "max_weight": 0.15, "sharpe": 1.12, "annual_return": 0.1624},
            {"topk": 20, "max_weight": 0.15, "sharpe": 0.86, "annual_return": 0.1276},
        ],
    }


@app.post("/api/portfolio/optimize")
def api_portfolio_optimize(topk: int = 10, max_weight: float = 0.15):
    """运行组合优化。"""
    import subprocess
    
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"""
import sys
sys.path.insert(0, '{QUANT}')
from validation.portfolio_optimizer import load_historical_returns, run_portfolio_optimization
import json

# 加载数据
instruments = ['SH600519', 'SH601318', 'SZ000858', 'SH600036', 'SZ000333',
               'SH600276', 'SH601888', 'SZ000651', 'SH600900', 'SH601012',
               'SH600030', 'SH601166', 'SZ000001', 'SH600000', 'SH601398',
               'SH601288', 'SH601988', 'SH601601', 'SH600016', 'SH601328']

returns = load_historical_returns(instruments, start_date='2023-01-01')

# 运行优化
result = run_portfolio_optimization(returns, method='mean_risk', max_weight={max_weight})
print(json.dumps(result))
"""],
            capture_output=True,
            text=True,
            timeout=180,
        )
        
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout.strip().split('\n')[-1])
            return {"ok": True, "result": data}
        else:
            return {"ok": False, "message": f"优化失败: {result.stderr[:200]}"}
            
    except Exception as e:
        return {"ok": False, "message": f"优化失败: {str(e)}"}


@app.get("/api/quote/{instrument}")
def api_quote(instrument: str, klt: int = 101, n: int = 120, fqt: int = 1):
    """个股/指数行情：klt=101日线/102周线/1分钟等；优先东财(当日)，回退本地 qlib(EOD)。"""
    if not instrument[:2].isalpha() or not instrument[2:].isdigit():
        raise HTTPException(400, "标的格式应为 SH600000 / SZ000001")
    return Q.quote(instrument.upper(), klt=klt, lmt=min(max(n, 5), 500), fqt=fqt)


@app.get("/api/indices")
def api_indices():
    """大盘指数：上证/中证500(基准)/创业板指。"""
    return {"indices": Q.indices()}


@app.post("/api/run/{stage}/{account}")
def api_run(stage: str, account: str):
    """手动触发一次 evening/postclose（用于演示或补跑）。"""
    _check(account)
    if stage not in ("evening", "postclose"):
        raise HTTPException(400, "stage 必须是 evening/postclose")
    _run_daily(stage, account)
    return {"ok": True, "stage": stage, "account": account}


def _check(account: str) -> None:
    if account not in ACCOUNTS:
        raise HTTPException(404, f"未知账户 {account}")
