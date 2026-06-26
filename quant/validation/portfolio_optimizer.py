"""组合优化验证模块 - 使用 skfolio 验证和优化投资组合参数。

基于 scikit-learn 生态的组合优化库，用于：
1. 验证当前策略的组合配置是否合理
2. 优化 topk、max_weight 等参数
3. 对比不同优化方法的效果

依赖：pip install skfolio
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

QUANT = Path(__file__).resolve().parents[1]
CSV_DIR = QUANT / "data" / "csv_raw" / "history"


def load_historical_returns(instruments: list[str] = None,
                           start_date: str = "2023-01-01",
                           end_date: str = None) -> pd.DataFrame:
    """加载历史收益率数据（直接从 CSV 文件读取）。"""
    if instruments is None:
        # 获取所有可用的股票
        instruments = [f.stem for f in CSV_DIR.glob("*.csv")]
    
    all_prices = {}
    
    for inst in instruments:
        csv_file = CSV_DIR / f"{inst}.csv"
        if not csv_file.exists():
            continue
        
        df = pd.read_csv(csv_file)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        
        if start_date:
            df = df[df.index >= start_date]
        if end_date:
            df = df[df.index <= end_date]
        
        if not df.empty:
            all_prices[inst] = df["close"]
    
    if not all_prices:
        raise ValueError("无法获取历史数据")
    
    # 构建价格 DataFrame
    prices = pd.DataFrame(all_prices)
    prices = prices.dropna(how="all")
    
    # 计算日收益率
    returns = prices.pct_change().dropna()
    
    return returns


def run_portfolio_optimization(returns: pd.DataFrame,
                               method: str = "mean_risk",
                               max_weight: float = 0.05,
                               min_weight: float = 0.0,
                               risk_free_rate: float = 0.02) -> dict:
    """运行组合优化。"""
    from skfolio.optimization import (
        MeanRisk,
        InverseVolatility,
        RiskBudgeting,
        HierarchicalRiskParity,
        HierarchicalEqualRiskContribution
    )
    
    optimizers = {
        "mean_risk": MeanRisk(
            min_weights=min_weight,
            max_weights=max_weight,
            raise_on_failure=False,
        ),
        "min_variance": MeanRisk(
            min_weights=min_weight,
            max_weights=max_weight,
            raise_on_failure=False,
        ),
        "risk_parity": RiskBudgeting(
            min_weights=min_weight,
            max_weights=max_weight,
            raise_on_failure=False,
        ),
        "hrp": HierarchicalRiskParity(),
        "herc": HierarchicalEqualRiskContribution(),
        "inverse_vol": InverseVolatility(),
    }
    
    if method not in optimizers:
        raise ValueError(f"不支持的方法: {method}, 可选: {list(optimizers.keys())}")
    
    optimizer = optimizers[method]
    portfolio = optimizer.fit(returns)
    
    weights = portfolio.weights_
    
    # 检查权重是否有效
    if weights is None:
        print(f"警告: 方法 {method} 优化失败，返回空结果")
        return None
    
    result = {
        "method": method,
        "n_assets": len(returns.columns),
        "weights": dict(zip(returns.columns, weights)),
        "max_weight": float(max(weights)),
        "min_weight": float(min(weights)),
        "mean_weight": float(np.mean(weights)),
        "concentration": float(np.sum(weights ** 2)),  # HHI
    }
    
    # 计算组合统计
    port_returns = (returns * weights).sum(axis=1)
    result["annual_return"] = float(port_returns.mean() * 252)
    result["annual_vol"] = float(port_returns.std() * np.sqrt(252))
    result["sharpe"] = float((result["annual_return"] - risk_free_rate) / result["annual_vol"]) if result["annual_vol"] > 0 else 0
    result["max_drawdown"] = float(((1 + port_returns).cumprod().cummax() - (1 + port_returns).cumprod()).max())
    
    return result


def compare_strategies(returns: pd.DataFrame,
                       max_weight: float = 0.2) -> pd.DataFrame:
    """对比不同优化策略的效果。"""
    methods = ["mean_risk", "risk_parity", "inverse_vol", "hrp"]
    
    results = []
    for method in methods:
        try:
            result = run_portfolio_optimization(
                returns, 
                method=method,
                max_weight=max_weight
            )
            if result:
                results.append(result)
        except Exception as e:
            print(f"方法 {method} 失败: {e}")
    
    if not results:
        return pd.DataFrame()
    
    df = pd.DataFrame(results)
    
    cols = ["method", "n_assets", "annual_return", "annual_vol", "sharpe", 
            "max_drawdown", "max_weight", "concentration"]
    df = df[[c for c in cols if c in df.columns]]
    
    for col in ["annual_return", "annual_vol", "max_drawdown", "max_weight", "concentration"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f"{x:.4f}")
    if "sharpe" in df.columns:
        df["sharpe"] = df["sharpe"].apply(lambda x: f"{x:.2f}")
    
    return df


def analyze_weight_distribution(returns: pd.DataFrame,
                                method: str = "mean_risk",
                                max_weight: float = 0.05) -> dict:
    """分析权重分布。"""
    result = run_portfolio_optimization(returns, method=method, max_weight=max_weight)
    
    if not result:
        return {}
    
    weights = np.array(list(result["weights"].values()))
    
    stats = {
        "n_assets": len(weights),
        "effective_n": float(1 / np.sum(weights ** 2)),
        "top5_weight": float(np.sort(weights)[-5:].sum()),
        "top10_weight": float(np.sort(weights)[-10:].sum()),
        "gini": float(_gini_coefficient(weights)),
        "weights_above_1pct": int(np.sum(weights > 0.01)),
        "weights_above_3pct": int(np.sum(weights > 0.03)),
    }
    
    return stats


def _gini_coefficient(weights: np.ndarray) -> float:
    """计算基尼系数。"""
    n = len(weights)
    if n == 0:
        return 0
    weights_sorted = np.sort(weights)
    index = np.arange(1, n + 1)
    return float((2 * np.sum(index * weights_sorted) / (n * np.sum(weights_sorted))) - (n + 1) / n)


def optimize_parameters(returns: pd.DataFrame,
                       topk_range: list[int] = [30, 40, 50, 60, 70],
                       weight_range: list[float] = [0.03, 0.05, 0.08, 0.10],
                       method: str = "mean_risk") -> pd.DataFrame:
    """网格搜索优化参数。"""
    results = []
    
    for topk in topk_range:
        for max_weight in weight_range:
            mean_returns = returns.mean().sort_values(ascending=False)
            selected = mean_returns.head(topk).index.tolist()
            
            result = run_portfolio_optimization(
                returns[selected],
                method=method,
                max_weight=max_weight
            )
            
            if result:
                result["topk"] = topk
                result["max_weight_param"] = max_weight
                results.append(result)
    
    if not results:
        return pd.DataFrame()
    
    df = pd.DataFrame(results)
    
    cols = ["topk", "max_weight_param", "n_assets", "annual_return", "annual_vol", 
            "sharpe", "max_drawdown", "concentration"]
    df = df[[c for c in cols if c in df.columns]]
    
    if "sharpe" in df.columns:
        df = df.sort_values("sharpe", ascending=False)
    
    return df


def main():
    """主函数 - 演示组合优化功能。"""
    print("=" * 60)
    print("投资组合优化验证工具")
    print("=" * 60)
    
    # 示例股票列表
    sample_instruments = [
        "SH600519",  # 贵州茅台
        "SH601318",  # 中国平安
        "SZ000858",  # 五粮液
        "SH600036",  # 招商银行
        "SZ000333",  # 美的集团
        "SH600276",  # 恒瑞医药
        "SH601888",  # 中国中免
        "SZ000651",  # 格力电器
        "SH600900",  # 长江电力
        "SH601012",  # 隆基绿能
    ]
    
    try:
        print("\n1. 加载历史收益率数据...")
        returns = load_historical_returns(sample_instruments, start_date="2024-01-01")
        print(f"   数据期间: {returns.index[0]} 到 {returns.index[-1]}")
        print(f"   股票数量: {len(returns.columns)}")
        
        print("\n2. 对比不同优化策略...")
        comparison = compare_strategies(returns, max_weight=0.2)
        if not comparison.empty:
            print(comparison.to_string(index=False))
        
        print("\n3. 分析权重分布（均值-方差优化）...")
        weight_stats = analyze_weight_distribution(returns, method="mean_risk", max_weight=0.2)
        if weight_stats:
            for k, v in weight_stats.items():
                print(f"   {k}: {v}")
        
        print("\n4. 参数网格搜索...")
        param_results = optimize_parameters(
            returns,
            topk_range=[5, 8, 10],
            weight_range=[0.15, 0.20, 0.25],
            method="mean_risk"
        )
        if not param_results.empty:
            print(param_results.head(10).to_string(index=False))
        
    except Exception as e:
        print(f"运行出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
