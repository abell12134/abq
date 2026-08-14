#!/usr/bin/env bash
# Quant Analysis Agent API 常驻服务
#   bash agent_api/serve.sh start [port]   默认 8010
#   bash agent_api/serve.sh stop|restart|status|logs
set -euo pipefail

QUANT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$QUANT/.." && pwd)"
VENV="$ROOT/quant-venv/bin"
PID_FILE="$QUANT/data/logs/agent_api.pid"
LOG_FILE="$QUANT/data/logs/agent_api.log"
PORT="${2:-8010}"
mkdir -p "$QUANT/data/logs"

is_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if is_running; then echo "已在运行 (pid=$(cat "$PID_FILE"))"; exit 0; fi
    # 启动前从 MinIO 同步数据
    if [[ -x "$VENV/python" ]]; then
      "$VENV/python" "$QUANT/ops/minio_sync.py" pull --qlib-only 2>/dev/null || true
    fi
    # free stale listener if pid file missing
    if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
      echo "端口 $PORT 已被占用，请先 stop 或释放端口"; exit 1
    fi
    cd "$QUANT"
    export PYTHONPATH="${ROOT}:${QUANT}${PYTHONPATH:+:$PYTHONPATH}"
    export AGENT_PUBLIC_HOST="${AGENT_PUBLIC_HOST:-43.159.136.65}"
    nohup "$VENV/uvicorn" agent_api.server:app --host 0.0.0.0 --port "$PORT" \
      >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if is_running; then echo "已启动: http://0.0.0.0:$PORT (pid=$(cat "$PID_FILE"))";
    else echo "启动失败，见 $LOG_FILE"; tail -n 30 "$LOG_FILE"; exit 1; fi
    ;;
  stop)
    if is_running; then kill "$(cat "$PID_FILE")" && echo "已停止"; else echo "未运行"; fi
    rm -f "$PID_FILE"
    # also kill stray uvicorn on port if needed
    fuser -k "${PORT}/tcp" 2>/dev/null || true
    ;;
  restart)
    "$0" stop "$PORT" || true; sleep 1; "$0" start "$PORT"
    ;;
  status)
    if is_running; then echo "运行中 (pid=$(cat "$PID_FILE"))"; else echo "未运行"; fi
    ;;
  logs)
    tail -n 80 -f "$LOG_FILE"
    ;;
  *)
    echo "用法: bash agent_api/serve.sh {start|stop|restart|status|logs} [port]"; exit 1
    ;;
esac
