"""舆情长期记忆：多源采集 → LLM 摘要 → 本地向量库 → 看板展示。

数据源：东方财富 JSONP、财联社(AKShare)、新浪财经(AKShare)。
模型路由：北京时间高峰用自部署 OpenAI 兼容端点，闲时用 DeepSeek。
"""

from .run_memory import run  # noqa: F401

__all__ = ["run"]
