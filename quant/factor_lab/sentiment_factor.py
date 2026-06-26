"""情绪分析因子模块 - 基于 LLM 的新闻/公告情绪打分。

使用 DeepSeek API（或兼容 OpenAI 的 API）对股票相关新闻进行情绪分析，
生成情绪因子作为选股信号的补充。

输出格式符合 signals schema: instrument, score, rank
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

QUANT = Path(__file__).resolve().parents[1]
SECRET = QUANT / "configs" / "secret.env"


def _load_secret() -> dict:
    env = {}
    if SECRET.exists():
        for line in SECRET.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _client():
    from openai import OpenAI
    s = _load_secret()
    key = s.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("缺少 LLM_API_KEY（configs/secret.env）")
    base = s.get("LLM_BASE_URL", "https://api.deepseek.com")
    model = s.get("LLM_MODEL", "deepseek-chat")
    return OpenAI(api_key=key, base_url=base), model


# 情绪分析 prompt
SENTIMENT_SYS = """你是专业的 A 股量化分析师，负责对股票相关新闻/公告进行情绪打分。

评分规则：
- 范围：-1.0（极度利空）到 +1.0（极度利好）
- 0.0 表示中性
- 考虑因素：业绩预增/预减、重大合同、行业政策、高管变动、股东增减持、诉讼风险等

输出要求：
- 只输出 JSON 对象，格式：{"score": 0.3, "reason": "简短理由"}
- 不要输出其他内容"""


def fetch_news_mock(instruments: list[str], days: int = 3) -> dict[str, list[str]]:
    """从东方财富抓取真实新闻数据。"""
    from data_pipeline.fetch_eastmoney import fetch_batch_news, save_news_data
    
    print(f"[情绪分析] 从东方财富抓取 {len(instruments)} 只股票的新闻...")
    news_dict = fetch_batch_news(instruments, days=days)
    
    # 保存新闻数据
    save_news_data(news_dict)
    
    return news_dict


def analyze_sentiment_batch(news_dict: dict[str, list[str]], 
                           batch_size: int = 10) -> dict[str, float]:
    """批量分析股票情绪得分。
    
    Args:
        news_dict: {instrument: [news1, news2, ...]}
        batch_size: 每批处理的股票数量
        
    Returns:
        {instrument: sentiment_score}
    """
    client, model = _client()
    results = {}
    
    instruments = list(news_dict.keys())
    
    for i in range(0, len(instruments), batch_size):
        batch = instruments[i:i+batch_size]
        
        # 构建批量分析请求
        batch_text = ""
        for inst in batch:
            news_list = news_dict[inst]
            news_text = "\n".join(f"  - {n}" for n in news_list[:5])  # 最多5条
            batch_text += f"\n【{inst}】\n{news_text}\n"
        
        user_msg = f"请对以下股票的新闻进行情绪打分（-1到+1）：\n{batch_text}\n\n输出JSON数组：[{{\"instrument\":\"SH600519\",\"score\":0.3,\"reason\":\"理由\"}}]"
        
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.1,  # 低温度保证一致性
                max_tokens=2000,
                messages=[
                    {"role": "system", "content": SENTIMENT_SYS},
                    {"role": "user", "content": user_msg}
                ]
            )
            
            content = resp.choices[0].message.content or ""
            parsed = _parse_sentiment_response(content)
            
            for item in parsed:
                inst = item.get("instrument", "")
                score = item.get("score", 0.0)
                if inst in batch:
                    results[inst] = float(score)
                    
        except Exception as e:
            print(f"情绪分析批次 {i//batch_size + 1} 失败: {e}")
            # 失败的股票给中性分
            for inst in batch:
                results[inst] = 0.0
    
    return results


def _parse_sentiment_response(text: str) -> list[dict]:
    """解析 LLM 返回的情绪分析结果。"""
    text = text.strip()
    
    # 尝试解析 JSON 数组
    m = re.search(r'\[.*\]', text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    
    # 尝试解析单个 JSON 对象
    m = re.search(r'\{.*\}', text, re.S)
    if m:
        try:
            return [json.loads(m.group(0))]
        except json.JSONDecodeError:
            pass
    
    return []


def generate_sentiment_signals(instruments: list[str], 
                               date: str = None) -> pd.DataFrame:
    """生成情绪因子信号。
    
    Args:
        instruments: 股票代码列表
        date: 日期（默认今天）
        
    Returns:
        DataFrame with columns: instrument, score, rank
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    
    print(f"[情绪分析] 开始分析 {len(instruments)} 只股票...")
    
    # 1. 获取新闻（这里用模拟数据，实际替换为真实数据源）
    news_dict = fetch_news_mock(instruments)
    
    # 2. 情绪打分
    sentiment_scores = analyze_sentiment_batch(news_dict)
    
    # 3. 构建信号 DataFrame
    records = []
    for inst in instruments:
        score = sentiment_scores.get(inst, 0.0)
        records.append({
            "instrument": inst,
            "score": round(score, 4),
        })
    
    df = pd.DataFrame(records)
    
    # 4. 计算排名（分数越高排名越前）
    df["rank"] = df["score"].rank(ascending=False, method="dense").astype(int)
    
    # 5. 排序
    df = df.sort_values("rank")
    
    print(f"[情绪分析] 完成，最高分: {df['score'].max():.4f}, 最低分: {df['score'].min():.4f}")
    
    return df


def save_sentiment_signals(df: pd.DataFrame, date: str = None):
    """保存情绪信号到 signals 目录。"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    
    signals_dir = QUANT / "data" / "signals"
    signals_dir.mkdir(parents=True, exist_ok=True)
    
    filepath = signals_dir / f"sentiment_{date}.csv"
    df.to_csv(filepath, index=False)
    print(f"[情绪分析] 信号已保存: {filepath}")
    
    return filepath


# ============================================================
# 作为独立模块使用时的入口
# ============================================================
if __name__ == "__main__":
    import sys
    
    # 从命令行或配置获取股票列表
    # 这里用沪深300部分成分股示例
    sample_instruments = [
        "SH600519",  # 贵州茅台
        "SH601318",  # 中国平安
        "SZ000858",  # 五粮液
        "SH600036",  # 招商银行
        "SZ000333",  # 美的集团
    ]
    
    if len(sys.argv) > 1:
        # 从文件读取股票列表
        inst_file = Path(sys.argv[1])
        if inst_file.exists():
            sample_instruments = inst_file.read_text().strip().split("\n")
    
    # 生成信号
    signals_df = generate_sentiment_signals(sample_instruments)
    
    # 保存
    save_sentiment_signals(signals_df)
    
    # 显示结果
    print("\n情绪分析结果：")
    print(signals_df.to_string(index=False))
