"""舆情长期记忆：多源采集 → LLM 摘要 → 本地向量库 → 看板展示。

数据源：东方财富 JSONP、财联社(AKShare)、新浪财经(AKShare)。
模型路由：默认自部署 `LLM_PEAK_*`（`LLM_OVERLAYS_LOCAL_ONLY=1`）；可 `--force-llm offpeak` 临时用云端。
"""

from .run_memory import run  # noqa: F401

__all__ = ["run"]
