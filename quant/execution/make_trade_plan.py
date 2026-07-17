"""生成每日调仓清单（路线一：人工在同花顺执行）。

调仓逻辑严格对齐研究层回测用的 qlib TopkDropoutStrategy，避免实盘换手率
与回测假设脱节（回测已验证：低换手 hold_thresh=10 才能把成本拖累压住，
净 IR 由 -0.17 升至 1.04；若实盘每日全量再平衡则会把超额重新吃光）：

  - 仅卖出"跌出组合"的 n_drop 只（且持有天数 ≥ hold_thresh 才允许卖）；
  - 用腾出的资金 + 现金等权买入新晋名次靠前的标的，补足到 topk 只；
  - 已持有且未被卖出的标的不动（不因价格漂移而日内再平衡）。

输入：
    data/signals/YYYY-MM-DD.csv      当日信号（predict_daily.py 产出）
    data/nav/holdings.csv            当前持仓（instrument,shares,last_price[,entry_date]）
    configs/global.yaml              topk / n_drop / hold_thresh / 单票上限

输出：
    data/target_position/YYYY-MM-DD.csv   目标持仓（执行后应达到的持仓）
    data/orders/YYYY-MM-DD.csv            交易清单（含整手数量，人工照做）
（执行后的实际持仓由 record_fills/simulate_fills 按真实成交反推，不再预写 holdings_next）

用法：
    python make_trade_plan.py --capital 100000          # 首次（空仓）需给定资金
    python make_trade_plan.py --cash 5000               # 之后从 holdings.csv 推算
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

QUANT = Path(__file__).resolve().parents[1]
DEFAULT_CFG = QUANT / "configs" / "global.yaml"
CFG = yaml.safe_load(DEFAULT_CFG.read_text())

LOT = 100  # A 股一手

# 板块 → 代码前缀（账户无相应交易权限时，配置 execution.exclude_boards 跳过买入）
BOARD_PREFIXES = {
    "chinext": ("SZ300", "SZ301", "SZ302"),   # 创业板
    "star": ("SH688", "SH689"),               # 科创板
    "bse": ("BJ",),                           # 北交所
}


def excluded_by_board(inst: str, boards: list[str]) -> bool:
    return any(inst.startswith(p) for b in boards
               for p in BOARD_PREFIXES.get(str(b).lower(), ()))


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None, account: str | None = None) -> dict:
    """读取全局配置，并用实盘 profile 覆盖其中一部分参数。"""
    if account:
        sys.path.insert(0, str(QUANT / "ops"))
        import common as C
        base = C.account_config(account)
    else:
        base = yaml.safe_load(DEFAULT_CFG.read_text())
    if not path:
        return base
    p = Path(path)
    if not p.is_absolute():
        p = QUANT / path
    return _merge(base, yaml.safe_load(p.read_text()))


def latest_signal_file(date: str | None = None) -> Path:
    sig_dir = QUANT / "data" / "signals"
    if date:
        f = sig_dir / f"{date}.csv"
        if not f.exists():
            raise FileNotFoundError(f"没有 {date} 的信号文件")
    else:
        files = sorted(sig_dir.glob("????-??-??.csv"))
        if not files:
            raise FileNotFoundError("没有信号文件，请先运行 research/predict_daily.py")
        f = files[-1]
    if not f.with_suffix(".done").exists():
        raise RuntimeError(f"{f.name} 缺少 .done 标记，信号可能未生成完整")
    return f


def account_dirs(account: str | None) -> dict[str, Path]:
    if not account:
        return {
            "orders": QUANT / "data" / "orders",
            "target_position": QUANT / "data" / "target_position",
            "nav": QUANT / "data" / "nav",
        }
    root = QUANT / "data" / "accounts" / account
    return {
        "orders": root / "orders",
        "target_position": root / "target_position",
        "nav": root / "nav",
    }


def load_holdings(account: str | None = None) -> pd.DataFrame:
    """当前持仓：instrument, shares, last_price, [entry_date]。"""
    f = account_dirs(account)["nav"] / "holdings.csv"
    cols = ["instrument", "shares", "last_price", "entry_date"]
    if not f.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(f)
    if "entry_date" not in df.columns:
        df["entry_date"] = pd.NaT  # 缺失视为持有足够久，可被卖出
    return df


def load_close_prices(instruments: list[str], day: str) -> pd.Series:
    import qlib
    from qlib.data import D

    sys.path.insert(0, str(QUANT / "ops"))
    from ensure_qlib_data import resolve_provider_uri

    qlib.init(provider_uri=resolve_provider_uri(CFG["paths"]["qlib_data"]),
              region="cn")
    df = D.features(instruments, ["$close/$factor"], start_time=day, end_time=day)
    s = df.droplevel("datetime")["$close/$factor"]  # 还原为真实价格
    s.name = "price"
    return s


def held_trading_days(entry_date, day: str) -> int:
    """持有的交易日数（以 qlib 日历计）。entry_date 缺失则视为足够久。"""
    if entry_date is None or pd.isna(entry_date):
        return 10 ** 9
    from qlib.data import D
    cal = pd.to_datetime(pd.Series(D.calendar(start_time=str(entry_date),
                                              end_time=day, freq="day")))
    return max(int(len(cal)), 0)


def select_trades(score: pd.Series, held: list[str], topk: int, n_drop: int):
    """复刻 TopkDropoutStrategy(method_sell='bottom', method_buy='top') 的选股。

    返回 (sell_candidates, buy_candidates)；hold_thresh 由调用方再过滤。
    """
    last = score.reindex(held).dropna().sort_values(ascending=False).index
    n_buy_candi = max(n_drop + topk - len(last), 0)
    ranked_new = score[~score.index.isin(last)].sort_values(ascending=False).index
    today = ranked_new[:n_buy_candi]
    comb = score.reindex(last.union(pd.Index(today))).sort_values(
        ascending=False).index
    sell = last[last.isin(comb[-n_drop:])] if n_drop > 0 and len(comb) else pd.Index([])
    # 第二个返回值给出更宽的买入候选（含名次靠后的备选），用于应对
    # 小资金下高价股无法凑整手时顺位补足，避免持仓只数与现金长期闲置
    return list(sell), list(ranked_new)


def main() -> int:
    global CFG
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=None,
                        help="总资金（首次空仓时必填；有持仓时按市值+现金推算可不填）")
    parser.add_argument("--cash", type=float, default=0.0, help="当前可用现金")
    parser.add_argument("--ump", action="store_true",
                        help="启用 UMP 裁判否决买入候选（需 validation/ump_model.pkl）")
    parser.add_argument("--ta-veto", action="store_true",
                        help="启用 TA 定性否决（读 data/overlays/ta_veto/日期.json）")
    parser.add_argument("--no-risk-check", action="store_true",
                        help="跳过涨跌停/停牌预检（默认开启）")
    parser.add_argument("--date", default=None, help="指定信号日期，默认取最新信号")
    parser.add_argument("--config", default=None,
                        help="实盘配置覆盖文件（一般用 --account 即可）")
    parser.add_argument("--account", default=None,
                        help="账户名，如 research_sim_100k / live_manual_10k")
    args = parser.parse_args()
    CFG = load_config(args.config, args.account)

    sig_file = latest_signal_file(args.date)
    day = sig_file.stem
    topk = CFG["strategy"]["topk"]
    n_drop = CFG["strategy"]["n_drop"]
    hold_thresh = CFG["strategy"].get("hold_thresh", 1)
    min_pos = CFG.get("strategy", {}).get("min_positions")
    max_pos = CFG.get("strategy", {}).get("max_positions")

    signals = pd.read_csv(sig_file)
    score = signals.set_index("instrument")["score"]

    holdings = load_holdings(args.account)
    held_inst = holdings["instrument"].tolist()
    entry_map = dict(zip(holdings["instrument"], holdings.get("entry_date", pd.Series())))

    # 选股：跌出组合者卖、新晋者买（对齐回测策略）
    sell_candi, buy_candi = select_trades(score, held_inst, topk, n_drop)
    # 板块排除：账户无交易权限的板块（如创业板）不进入买入候选；卖出不受影响
    exclude_boards = CFG.get("execution", {}).get("exclude_boards", []) or []
    n_board_excluded = 0
    if exclude_boards:
        before = len(buy_candi)
        buy_candi = [b for b in buy_candi if not excluded_by_board(b, exclude_boards)]
        n_board_excluded = before - len(buy_candi)
    # 买入候选只取名次靠前的一段（topk 的 3 倍足够覆盖凑整手的顺位补足）
    buy_candi = buy_candi[: topk * 3]

    # 17:10 UMP 否决：砍掉裁判判定胜率最差的尾部买入候选（与阶段2b A/B 口径一致）
    vetoed: set = set()
    if args.ump or CFG.get("execution", {}).get("use_ump"):
        sys.path.insert(0, str(QUANT / "validation"))
        from ump_judge import live_veto_instruments
        vetoed = live_veto_instruments(day, score)
        buy_candi = [b for b in buy_candi if b not in vetoed]

    # 17:12 TA 定性否决（TradingAgents 精简版）：读 overlays JSON；缺文件/fail_open → 空集
    ta_vetoed: set = set()
    use_ta = args.ta_veto or bool(CFG.get("execution", {}).get("use_ta_veto"))
    if use_ta:
        sys.path.insert(0, str(QUANT))
        from overlays.ta_veto.schema import load_vetoed_instruments
        ta_vetoed = load_vetoed_instruments(day)
        buy_candi = [b for b in buy_candi if b not in ta_vetoed]

    all_inst = sorted(set(held_inst) | set(buy_candi))
    prices = load_close_prices(all_inst, day)

    # 17:20 风控预检：涨停/停牌的买入候选剔除；跌停/停牌的持仓本次不卖
    susp_block_sell: list[str] = []
    if not args.no_risk_check:
        sys.path.insert(0, str(QUANT / "ops"))
        import common as C
        st = C.trade_status(sorted(set(held_inst) | set(buy_candi)), day)
        buy_candi = [b for b in buy_candi
                     if not (b in st.index and (st.at[b, "limit_up"] or st.at[b, "suspended"]))]

    # 总资产 = 持仓市值 + 现金（或首次给定的 capital）
    if holdings.empty:
        if not args.capital:
            print("[FATAL] 空仓首次运行必须提供 --capital")
            return 1
        total = args.capital
    else:
        mv = sum(float(prices.get(r.instrument, r.last_price)) * r.shares
                 for r in holdings.itertuples())
        total = mv + args.cash

    # hold_thresh 过滤：持有不足 hold_thresh 个交易日的不卖（推迟换仓）
    sell_final = [s for s in sell_candi
                  if held_trading_days(entry_map.get(s), day) >= hold_thresh]
    blocked = [s for s in sell_candi if s not in sell_final]

    # 风控预检（卖出侧）：停牌/跌停无法卖出，本次保留持仓
    if not args.no_risk_check:
        sellable = []
        for s in sell_final:
            if s in st.index and (st.at[s, "suspended"] or st.at[s, "limit_down"]):
                susp_block_sell.append(s)
            else:
                sellable.append(s)
        sell_final = sellable

    # 日亏硬熔断：禁止新开仓（卖出仍允许），与 compute_nav.set_halt 联动
    sys.path.insert(0, str(QUANT / "ops"))
    import common as C  # noqa: E402
    halt_active = C.is_halted(args.account)
    if halt_active:
        buy_candi = []
        n_buy = 0
        acc_halt = C.load_account(args.account) or {}
        print(f"[HALT] 账户熔断中（halt_since={acc_halt.get('halt_since')}），"
              "本次禁止买入，仅生成减仓单")
    else:
        # 补足到 topk：实际卖出数 + (topk - 当前持有数)
        n_buy = max(len(sell_final) + topk - len(held_inst), 0)

    # 单票目标金额：等权，受单票上限约束
    weight = min(1.0 / topk, CFG["strategy"]["max_weight"])
    per_name = total * weight

    cur_shares = holdings.set_index("instrument")["shares"] if not holdings.empty \
        else pd.Series(dtype=float)

    trades, next_holdings = [], []
    # 1) 卖出（整仓卖出被剔除的标的）
    for inst in sell_final:
        shares = int(cur_shares.get(inst, 0))
        if shares > 0:
            trades.append({"instrument": inst, "side": "SELL", "shares": shares,
                           "ref_price": round(float(prices.get(inst, 0)), 2)})
    # 2) 按名次顺位买入：凑不齐整手（小资金 + 高价股）则顺延到下一只，
    #    直至补足 n_buy 只，避免持仓只数不足、现金长期闲置
    #    行业偏离：topk 足够大时跳过会加剧偏离的候选（见 strategy.industry_*）
    n_bought, skipped_unaffordable, skipped_industry = 0, [], []
    ind_map = {}
    bench_ind = {}
    ind_max = float(CFG.get("strategy", {}).get("industry_deviation_max") or 0)
    ind_min_pos = int(CFG.get("strategy", {}).get("industry_min_positions") or 20)
    use_industry = (not halt_active) and ind_max > 0 and topk >= ind_min_pos
    if use_industry:
        try:
            from industry import load_industry_map, industry_weights, would_breach
            ind_map = load_industry_map()
            if ind_map:
                # 基准：中证500 等权行业权重（无成分权重文件时的近似）
                bench_names = sorted(ind_map.keys())
                bench_ind = industry_weights(bench_names, mapping=ind_map)
            else:
                use_industry = False
                print("[industry] 无 industry_map.csv，跳过行业约束"
                      "（先跑 execution/build_industry_map.py）")
        except Exception as e:
            use_industry = False
            print(f"[industry] 加载失败，跳过：{e}")

    sell_set = set(sell_final)
    kept_after_sell = [h for h in held_inst if h not in sell_set]

    for inst in buy_candi:
        if n_bought >= n_buy:
            break
        if inst not in prices.index or prices[inst] <= 0:
            continue
        shares = int(per_name / prices[inst] / LOT) * LOT
        if shares <= 0:
            skipped_unaffordable.append(inst)
            continue
        if use_industry and would_breach(
                kept_after_sell + [t["instrument"] for t in trades if t["side"] == "BUY"],
                inst, set(), ind_max, bench_ind, mapping=ind_map):
            skipped_industry.append(inst)
            continue
        trades.append({"instrument": inst, "side": "BUY", "shares": shares,
                       "ref_price": round(float(prices[inst]), 2)})
        n_bought += 1
    # 2b) 残余现金二次部署（按账户配置 execution.redeploy_residual_cash 开启）：
    #     小资金 + 100 股整手下，等权 floor 取整会留下大量闲置现金（1.2 万实盘线一度 26%），
    #     直接吃掉本就不高的超额。这里用建仓后的剩余现金按分数顺位继续补整手——
    #     既给已选标的补仓、也可顺位补新标的至 max_positions，单票市值不超 max_weight 上限，
    #     把闲置压到 1 手以内。仅对开启该开关的账户生效，研究线等默认行为不变。
    redeploy_left = None
    if (not halt_active) and CFG.get("execution", {}).get("redeploy_residual_cash", False):
        if holdings.empty:
            buy_cash = total
        else:
            sell_proceeds = sum(float(prices.get(s, 0)) * int(cur_shares.get(s, 0))
                                for s in sell_final)
            buy_cash = float(args.cash) + sell_proceeds
        spent = sum(t["shares"] * float(prices.get(t["instrument"], 0))
                    for t in trades if t["side"] == "BUY")
        cash_left = buy_cash - spent
        cap_amt = total * CFG["strategy"]["max_weight"]   # 单票市值上限
        max_open = int(max_pos) if max_pos else topk
        buy_shares = {t["instrument"]: t["shares"] for t in trades if t["side"] == "BUY"}
        held_keep = [h for h in held_inst if h not in set(sell_final)]  # 持仓不补，维持低换手

        def _n_open() -> int:
            return len(held_keep) + sum(1 for v in buy_shares.values() if v > 0)

        progressed = True
        while progressed:
            progressed = False
            for inst in buy_candi:  # 已按分数顺位、过 UMP/风控
                if inst not in prices.index or float(prices[inst]) <= 0:
                    continue
                price = float(prices[inst])
                lot_cost = price * LOT
                cur = buy_shares.get(inst, 0)
                if cur == 0 and _n_open() >= max_open:
                    continue  # 不为新标的突破 max_positions
                if lot_cost <= cash_left and (cur * price + lot_cost) <= cap_amt + 1e-6:
                    buy_shares[inst] = cur + LOT
                    cash_left -= lot_cost
                    progressed = True
        # 用补仓后的股数重建买入清单（卖出条目保留不变）
        trades = [t for t in trades if t["side"] == "SELL"]
        for inst, sh in buy_shares.items():
            if sh > 0:
                trades.append({"instrument": inst, "side": "BUY", "shares": int(sh),
                               "ref_price": round(float(prices[inst]), 2)})
        redeploy_left = cash_left
    # 3) 计算执行后的持仓（含 entry_date，供回填）
    sold = set(sell_final)
    bought = {t["instrument"]: t["shares"] for t in trades if t["side"] == "BUY"}
    for inst in held_inst:
        if inst in sold:
            continue
        next_holdings.append({"instrument": inst, "shares": int(cur_shares.get(inst, 0)),
                              "last_price": round(float(prices.get(inst, 0)), 2),
                              "entry_date": entry_map.get(inst, "")})
    for inst, sh in bought.items():
        next_holdings.append({"instrument": inst, "shares": sh,
                              "last_price": round(float(prices[inst]), 2),
                              "entry_date": day})

    target_df = pd.DataFrame(next_holdings)
    trade_df = pd.DataFrame(trades).sort_values(["side", "instrument"]) if trades \
        else pd.DataFrame(columns=["instrument", "side", "shares", "ref_price"])

    dirs = account_dirs(args.account)
    tp_dir = dirs["target_position"]
    od_dir = dirs["orders"]
    nav_dir = dirs["nav"]
    for d in (tp_dir, od_dir, nav_dir):
        d.mkdir(parents=True, exist_ok=True)
    target_df.to_csv(tp_dir / f"{day}.csv", index=False)
    trade_df.to_csv(od_dir / f"{day}.csv", index=False)
    (tp_dir / f"{day}.done").touch()

    # 熔断已作用于本张调仓单后自动解除，后续若再触日亏阈值会由 compute_nav 重新拉起
    if halt_active:
        C.clear_halt(args.account, day)

    print(f"\n===== {day} 调仓清单（次日开盘后执行，参考价为当日收盘）=====")
    print(f"持仓 {len(held_inst)}→目标 {len(target_df)} 只 | topk={topk} "
          f"n_drop={n_drop} hold_thresh={hold_thresh} | 单票目标≈{per_name:,.0f} 元")
    if halt_active:
        print("[HALT] 本单为熔断减仓单（无买入）；熔断标志已清除，等待下次日亏再触发")
    if min_pos or max_pos:
        print(f"[实盘档案] 目标持仓区间 {min_pos or '-'}~{max_pos or '-'} 只；"
              "若买不满，原因会记录为小资金整手约束/价格过高")
    if min_pos and len(target_df) < int(min_pos):
        print(f"[实盘提示] 当前目标仅 {len(target_df)} 只，低于最小目标 {min_pos} 只；"
              "1000 元账户受 100 股整手限制，不能为凑数量强行买入高价股")
    if exclude_boards and n_board_excluded:
        print(f"[板块排除] {n_board_excluded} 只买入候选属无权限板块"
              f"（{', '.join(exclude_boards)}），已跳过")
    if vetoed:
        print(f"[UMP] 否决了 {len(vetoed)} 只买入候选（裁判判定胜率偏低）")
    if use_ta and ta_vetoed:
        print(f"[TA] 定性否决 {len(ta_vetoed)} 只："
              f"{', '.join(sorted(ta_vetoed))}")
    elif use_ta:
        print("[TA] 已启用定性否决层（本日无生效否决 / fail-open 为空）")
    if susp_block_sell:
        print(f"[风控] 以下持仓停牌/跌停无法卖出，本次保留：{', '.join(susp_block_sell)}")
    if blocked:
        print(f"[hold_thresh] 以下标的名次已跌出但持有不足 {hold_thresh} 日，本次不卖："
              f"{', '.join(blocked)}")
    if skipped_unaffordable:
        print(f"[affordability] {len(skipped_unaffordable)} 只高价股按单票预算 "
              f"{per_name:,.0f} 元凑不齐整手，已顺延买入名次靠后标的")
    if skipped_industry:
        print(f"[industry] {len(skipped_industry)} 只买入候选会使行业偏离 "
              f"> {ind_max:.0%}（相对中证500等权近似），已跳过")
    if use_industry:
        print(f"[industry] 已启用行业偏离约束 ≤{ind_max:.0%} "
              f"（topk={topk} ≥ industry_min_positions={ind_min_pos}）")
    if redeploy_left is not None:
        idle_pct = 100 * redeploy_left / total if total else 0
        print(f"[残余现金二次部署] 已用建仓后剩余现金按分数顺位补整手（≤max_weight）；"
              f"最终闲置 ≈{redeploy_left:,.0f} 元（{idle_pct:.1f}%），单票市值≤{total*CFG['strategy']['max_weight']:,.0f} 元")
    if trade_df.empty:
        print("无需调仓")
    else:
        print(trade_df.to_string(index=False))
        sells = trade_df[trade_df.side == 'SELL']
        buys = trade_df[trade_df.side == 'BUY']
        print(f"\n卖出 {len(sells)} 笔，买入 {len(buys)} 笔；"
              f"先卖后买，限价=开盘价附近，涨停不追买、跌停不挂卖")
    mode = CFG.get("account", {}).get("mode", "manual")
    if mode == "simulated":
        print("\n次日 postclose：simulate_fills.py 将按开盘价自动模拟成交")
    else:
        print("\n次日成交后回填：record_fills.py --template→改实际成交→--apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
