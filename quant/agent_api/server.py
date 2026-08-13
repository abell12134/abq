"""Agent API server — port 8010 by default.

    cd quant && bash agent_api/serve.sh

Serves real SQLite ledger when present; falls back to synthetic demo_data.
IP whitelist mirrors webapp.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

QUANT = Path(__file__).resolve().parents[1]
ROOT = QUANT.parent
for p in (ROOT, QUANT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent_api import demo_data  # noqa: E402
from agent_api.models import (  # noqa: E402
    SupervisorAskRequest,
    SupervisorMessage,
    SupervisorSession,
)
from agent.core import store as ledger  # noqa: E402
from agent.orchestration.service import (  # noqa: E402
    build_system_status,
    list_calibration,
    list_enriched,
)

TZ = ZoneInfo("Asia/Shanghai")
_LOCALHOST = frozenset({"127.0.0.1", "::1", "localhost"})

_PUBLIC_HOST = os.environ.get("AGENT_PUBLIC_HOST", "43.159.136.65")
_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    f"http://{_PUBLIC_HOST}:5173",
    f"http://{_PUBLIC_HOST}:8000",
    f"http://{_PUBLIC_HOST}:8010",
]
_extra = os.environ.get("AGENT_CORS_ORIGINS", "")
if _extra.strip():
    _CORS_ORIGINS.extend(x.strip() for x in _extra.split(",") if x.strip())

app = FastAPI(title="Quant Analysis Agent API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_origin_regex=rf"https?://{_PUBLIC_HOST}(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SESSIONS: dict[str, SupervisorSession] = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return ""


def _ip_whitelist() -> list[str]:
    try:
        import yaml

        local = QUANT / "configs" / "webapp.local.yaml"
        if local.exists():
            data = yaml.safe_load(local.read_text()) or {}
            wl = data.get("ip_whitelist")
            if wl:
                return [str(x).strip() for x in wl if str(x).strip()]
        g = QUANT / "configs" / "global.yaml"
        if g.exists():
            data = yaml.safe_load(g.read_text()) or {}
            wl = (data.get("webapp") or {}).get("ip_whitelist") or []
            return [str(x).strip() for x in wl if str(x).strip()]
    except Exception:
        pass
    return []


def _ip_allowed(ip: str, wl: list[str]) -> bool:
    if not wl:
        return True
    if ip in _LOCALHOST or ip in wl:
        return True
    try:
        import ipaddress

        addr = ipaddress.ip_address(ip)
        for item in wl:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif ip == item:
                return True
    except ValueError:
        pass
    return False


def _use_live() -> bool:
    ledger.init_db()
    return ledger.has_real_rows()


@app.middleware("http")
async def ip_gate(request: Request, call_next):
    if request.url.path in ("/api/health", "/health"):
        return await call_next(request)
    # Bearer token (optional upgrade): if AGENT_API_TOKEN set, accept token OR IP whitelist
    token = (os.environ.get("AGENT_API_TOKEN") or "").strip()
    auth = request.headers.get("authorization") or ""
    bearer_ok = False
    if token and auth.lower().startswith("bearer "):
        bearer_ok = auth[7:].strip() == token
    if bearer_ok:
        return await call_next(request)
    wl = _ip_whitelist()
    ip = _client_ip(request)
    if wl and not _ip_allowed(ip, wl):
        # if token configured, hint that IP failed and token may be used
        detail = "forbidden"
        if token:
            detail = "forbidden (IP not whitelisted; use Authorization: Bearer <AGENT_API_TOKEN>)"
        return JSONResponse({"detail": detail}, status_code=403)
    # Optional strict mode: require token even for whitelisted IPs on mutating methods
    strict = (os.environ.get("AGENT_API_TOKEN_STRICT") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if strict and token and request.method in ("POST", "PUT", "PATCH", "DELETE") and not bearer_ok:
        return JSONResponse({"detail": "Bearer token required"}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
def health():
    token = bool((os.environ.get("AGENT_API_TOKEN") or "").strip())
    info = {
        "ok": True,
        "service": "quant-agent-api",
        "ledger": "live" if _use_live() else "demo",
        "auth": {
            "ip_whitelist": bool(_ip_whitelist()),
            "bearer_token_configured": token,
            "token_strict": (os.environ.get("AGENT_API_TOKEN_STRICT") or "").strip().lower()
            in ("1", "true", "yes"),
        },
        "caliber": None,
        "langgraph": (os.environ.get("AGENT_USE_LANGGRAPH") or "1").strip().lower()
        not in ("0", "false", "no"),
    }
    try:
        from agent.core.caliber import CALIBER

        info["caliber"] = CALIBER
    except Exception:
        pass
    try:
        from agent.orchestration.llm import describe_route

        info["llm"] = describe_route()
    except Exception as exc:  # noqa: BLE001
        info["llm"] = {"error": str(exc)}
    return info


@app.get("/api/system/status")
def system_status():
    if _use_live():
        return build_system_status()
    return demo_data.system_status()


@app.get("/api/predictions")
def predictions(gate: str | None = None, status: str | None = None):
    if _use_live():
        items = list_enriched()
        if gate:
            items = [p for p in items if p.get("release_gate") == gate]
        if status:
            items = [p for p in items if p.get("status") == status]
        return items
    items = demo_data.list_predictions()
    if gate:
        items = [p for p in items if p.release_gate.value == gate]
    if status:
        items = [p for p in items if p.status.value == status]
    return items


@app.get("/api/predictions/{pred_id}")
def prediction_detail(pred_id: str):
    if _use_live():
        for p in list_enriched():
            if p.get("pred_id") == pred_id:
                return p
        raise HTTPException(404, "prediction not found")
    for p in demo_data.list_predictions():
        if p.pred_id == pred_id:
            return p
    raise HTTPException(404, "prediction not found")


@app.get("/api/strategies")
def strategies():
    if _use_live():
        from agent.trust.trust import list_trust, refresh_trust

        refresh_trust()
        return list_trust()
    return demo_data.list_strategies()


@app.get("/api/calibration")
def calibration():
    if _use_live():
        return list_calibration()
    return demo_data.calibration_buckets()


@app.get("/api/recommend/blend")
def recommend_blend(day: str | None = None):
    if not _use_live():
        return {"ok": False, "error": "demo mode", "instruments": {}}
    from agent.trust.recommend import blend_day
    from agent.core import store as S

    d = day or S.get_meta("last_emit_day") or datetime.now(TZ).strftime("%Y-%m-%d")
    return {"ok": True, **blend_day(d)}


@app.post("/api/admin/recompute")
def admin_recompute(dry_run: bool = True, limit: int = 2000):
    if not _use_live():
        return {"ok": False, "error": "demo mode"}
    from agent.settlement.recompute import recompute_all

    return recompute_all(dry_run=dry_run, limit=min(max(limit, 1), 10000))


@app.get("/api/research/queue")
def research_queue():
    if not _use_live():
        return {"sync": {"ok": False, "error": "demo mode"}, "gate": {"challengers": []}, "strategies": []}
    from agent.trust.challenger import list_research_queue

    return list_research_queue()


@app.post("/api/research/sync")
def research_sync():
    if not _use_live():
        raise HTTPException(400, "demo mode")
    from agent.trust.challenger import sync_from_factor_lab

    return sync_from_factor_lab()


@app.post("/api/research/evaluate")
def research_evaluate():
    if not _use_live():
        raise HTTPException(400, "demo mode")
    from agent.trust.challenger import evaluate_challengers

    return evaluate_challengers()


@app.post("/api/research/promote/{strategy_id}")
def research_promote(strategy_id: str, force: bool = False):
    if not _use_live():
        raise HTTPException(400, "demo mode")
    from agent.trust.challenger import promote_challenger

    return promote_challenger(strategy_id, force=force)


def _pred_as_dict(pred) -> dict:
    if isinstance(pred, dict):
        return pred
    return pred.model_dump()


@app.post("/api/supervisor/ask")
def supervisor_ask(body: SupervisorAskRequest):
    now = datetime.now(TZ).isoformat()
    sid = body.session_id or f"sess_{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}"
    sess = _SESSIONS.get(sid) or SupervisorSession(session_id=sid)
    if body.intent:
        sess.intent = body.intent
    if body.pred_id and body.pred_id not in sess.attached_pred_ids:
        sess.attached_pred_ids.append(body.pred_id)

    sess.messages.append(
        SupervisorMessage(role="user", content=body.message, pred_id=body.pred_id, ts=now)
    )

    if _use_live():
        use_graph = (os.environ.get("AGENT_USE_LANGGRAPH") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        if use_graph:
            from agent.orchestration.graph import run_graph

            result = run_graph(
                message=body.message,
                pred_id=body.pred_id,
                intent=body.intent,
                use_llm=True,
            )
            reply = result.get("reply") or ""
            trace = {
                "tools": result.get("tool_trace"),
                "meta": result.get("meta"),
                "error": result.get("error"),
            }
        else:
            from agent.orchestration.supervisor import run_supervisor

            result = run_supervisor(
                body.message,
                pred_id=body.pred_id,
                intent=body.intent,
                use_llm=True,
            )
            reply = result["reply"]
            trace = {
                "tools": result.get("tool_trace"),
                "meta": result.get("meta"),
            }
        sess.messages.append(
            SupervisorMessage(
                role="tool",
                content=json.dumps(trace, ensure_ascii=False, default=str),
                pred_id=body.pred_id,
                tool_name="supervisor_tools",
                ts=now,
            )
        )
        sess.messages.append(
            SupervisorMessage(role="assistant", content=reply, pred_id=body.pred_id, ts=now)
        )
        _SESSIONS[sid] = sess
        return sess

    # demo fallback (unchanged shape)
    pred = None
    source = demo_data.list_predictions()
    if body.pred_id:
        for p in source:
            pd = _pred_as_dict(p)
            if pd.get("pred_id") == body.pred_id:
                pred = pd
                break
    if pred:
        sc = pred.get("scorecard") or {}
        if hasattr(sc, "model_dump"):
            sc = sc.model_dump()
        reply = f"【demo】{pred.get('pred_id')} status={pred.get('status')}"
    else:
        st = demo_data.system_status()
        reply = f"【demo】{st.disclaimer}"

    sess.messages.append(
        SupervisorMessage(role="assistant", content=reply, pred_id=body.pred_id, ts=now)
    )
    _SESSIONS[sid] = sess
    return sess


@app.get("/api/supervisor/sessions/{session_id}")
def supervisor_session(session_id: str):
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    return sess
