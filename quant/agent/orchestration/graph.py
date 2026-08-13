"""LangGraph orchestration skeleton for the analysis Agent.

Outer loop: plan → execute (tools + narrate). Numbers stay in deterministic code.
"""

from __future__ import annotations

from typing import Any, TypedDict

from agent.orchestration.supervisor import _select_tools, run_supervisor


class AgentState(TypedDict, total=False):
    message: str
    pred_id: str | None
    intent: str | None
    use_llm: bool
    tool_names: list[str]
    reply: str
    meta: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    error: str | None


def _node_plan(state: AgentState) -> dict[str, Any]:
    tools = _select_tools(
        state.get("message") or "",
        state.get("pred_id"),
        state.get("intent"),
    )
    return {"tool_names": tools}


def _node_execute(state: AgentState) -> dict[str, Any]:
    try:
        result = run_supervisor(
            state.get("message") or "",
            pred_id=state.get("pred_id"),
            intent=state.get("intent"),
            use_llm=bool(state.get("use_llm", True)),
        )
        return {
            "reply": result.get("reply") or "",
            "meta": {
                **(result.get("meta") or {}),
                "graph": "langgraph",
                "planned": state.get("tool_names"),
            },
            "tool_trace": result.get("tool_trace") or [],
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "reply": "",
            "error": str(exc),
            "meta": {"graph": "langgraph"},
            "tool_trace": [],
        }


def build_supervisor_graph():
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(AgentState)
    g.add_node("plan", _node_plan)
    g.add_node("execute", _node_execute)
    g.add_edge(START, "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", END)
    return g.compile()


_GRAPH = None


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_supervisor_graph()
    return _GRAPH


def run_graph(
    *,
    message: str,
    pred_id: str | None = None,
    intent: str | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    return dict(
        get_graph().invoke(
            {
                "message": message,
                "pred_id": pred_id,
                "intent": intent,
                "use_llm": use_llm,
            }
        )
    )
