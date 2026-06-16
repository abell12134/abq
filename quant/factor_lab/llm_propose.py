"""阶段3 LLM 因子提议器（RD-Agent fin_factor 思想的自研实现）。

把"现有因子库 + 上一轮评估反馈"喂给 LLM，让它提出新的 Alpha 因子假设：
每个因子给出经济逻辑 + 可被 Qlib 直接计算的表达式。这是因子挖掘循环的"假设生成"
环节；评估与准入由 evaluate.py / run_iteration.py 负责（LLM 看不到未来数据）。

凭证从 configs/secret.env 读取（该文件已 gitignore）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

QUANT = Path(__file__).resolve().parents[1]
SECRET = QUANT / "configs" / "secret.env"

OPERATORS = (
    "Ref(x,n) Mean(x,n) Sum(x,n) Std(x,n) Var(x,n) Max(x,n) Min(x,n) "
    "Med(x,n) Mad(x,n) Delta(x,n)=x-Ref(x,n) Slope(x,n) Rsquare(x,n) Resi(x,n) "
    "Rank(x,n)[时序排名] Skew(x,n) Kurt(x,n) WMA(x,n) EMA(x,n) "
    "Corr(x,y,n) Cov(x,y,n) Greater(x,y) Less(x,y) Abs(x) Log(x) Sign(x) Power(x,a) "
    "If(cond,x,y) And Or"
)
FIELDS = "$open $high $low $close $volume $vwap"


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


SYS = """你是 A 股量化因子研究员。请基于经济/行为金融逻辑提出新的选股 Alpha 因子。
硬性要求：
1. 标的为中证500（中小盘），调仓为 5 日级别低换手，因子用于横截面选股打分。
2. 因子表达式必须能被 Qlib 直接计算，只能用如下时序/算术算子（无横截面算子）：
   {ops}
   可用字段：{fields}
3. 除法分母务必加 +1e-12 防止除零；窗口长度建议 5~60。
4. 必须与"已有因子"在逻辑与构造上不同（避免高相关重复）。
5. 每个因子要讲清经济逻辑（讲不出逻辑的高 IC 因子默认可疑，会被拒）。
6. 因子必须在**个股之间有横截面区分度**（不要产生对所有股票近似常数、或大量缺失/除零的量）；
   优先 5~20 日的中频构造，控制换手；目标横截面 Rank IC 量级约 0.02~0.05。
只输出一个 JSON 数组，每个元素形如：
{{"name":"简短英文名","category":"momentum/reversal/volatility/volume/liquidity/quality/其它",
  "hypothesis":"中文经济逻辑，1-2句","expr":"Qlib表达式"}}
不要输出任何解释性文字，只要 JSON 数组。"""


def _parse(text: str) -> list[dict]:
    """稳健解析：优先整段 JSON 数组；失败则逐个抽取 {...} 对象（容忍截断）。"""
    text = (text or "").strip()
    candidates = []
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            candidates = json.loads(m.group(0))
        except json.JSONDecodeError:
            candidates = []
    if not candidates:  # 数组截断/带 markdown 时，逐个对象抢救（表达式内无花括号）
        candidates = []
        for obj in re.findall(r"\{[^{}]*\}", text, re.S):
            try:
                candidates.append(json.loads(obj))
            except json.JSONDecodeError:
                continue
    out = []
    for d in candidates:
        if isinstance(d, dict) and {"name", "expr", "hypothesis"} <= set(d):
            d.setdefault("category", "other")
            out.append(d)
    return out


def propose(existing: dict[str, str], feedback: str, k: int = 4,
            temperature: float = 0.7) -> list[dict]:
    client, model = _client()
    ex = "\n".join(f"- {n}: {e}" for n, e in existing.items())
    user = (f"已有因子（请勿重复其逻辑/构造）：\n{ex}\n\n"
            f"上一轮评估反馈：\n{feedback or '（首轮，无反馈）'}\n\n"
            f"请提出 {k} 个**新颖且互不相同**的因子。")
    msgs = [{"role": "system", "content": SYS.format(ops=OPERATORS, fields=FIELDS)},
            {"role": "user", "content": user}]
    for attempt in range(2):  # 推理模型偶发截断/空输出，重试一次
        resp = client.chat.completions.create(
            model=model, temperature=temperature, max_tokens=8000, messages=msgs)
        got = _parse(resp.choices[0].message.content or "")
        if got:
            return got
    return []
