#!/usr/bin/env bash
# 看板 + 调度常驻服务启停脚本。
#   bash webapp/serve.sh start [port]   后台启动（默认 8000）
#   bash webapp/serve.sh stop
#   bash webapp/serve.sh status
#   bash webapp/serve.sh logs
set -euo pipefail

QUANT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$QUANT/../quant-venv/bin"
PID_FILE="$QUANT/data/logs/webapp.pid"
LOG_FILE="$QUANT/data/logs/webapp.log"
PORT="${2:-8000}"
mkdir -p "$QUANT/data/logs"

is_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if is_running; then echo "已在运行 (pid=$(cat "$PID_FILE"))"; exit 0; fi
    cd "$QUANT"
    nohup "$VENV/uvicorn" webapp.server:app --host 0.0.0.0 --port "$PORT" \
      >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if is_running; then echo "已启动: http://0.0.0.0:$PORT (pid=$(cat "$PID_FILE"))";
    else echo "启动失败，见 $LOG_FILE"; tail -n 20 "$LOG_FILE"; exit 1; fi
    ;;
  stop)
    if is_running; then kill "$(cat "$PID_FILE")" && echo "已停止"; else echo "未运行"; fi
    rm -f "$PID_FILE"
    ;;
  restart)
    "$0" stop "$PORT" || true; sleep 1; "$0" start "$PORT"
    ;;
  status)
    if is_running; then echo "运行中 (pid=$(cat "$PID_FILE"))"; else echo "未运行"; fi
    ;;
  logs)
    tail -n 60 -f "$LOG_FILE"
    ;;
  *)
    echo "用法: bash webapp/serve.sh {start|stop|restart|status|logs} [port]"; exit 1
    ;;
esac
