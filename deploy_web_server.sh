#!/usr/bin/env bash
# BYD Monitor Server 本地启停脚本（开发 / 单机部署用）
#
# 用法：
#   bash deploy_web_server.sh start|stop|restart|status|logs
#   PORT=8767 UPDATE_HOUR=15 UPDATE_MINUTE=30 bash deploy_web_server.sh restart
#
# 说明：项目零第三方依赖，不需要创建 venv；如需安装 Python，请自行准备 3.9+。
#       生产环境请用 deploy/ubuntu24/install.sh 安装为 systemd 服务。

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SCRIPT="${APP_DIR}/byd_web_server.py"
PID_FILE="${APP_DIR}/web_server.pid"
LOG_FILE="${APP_DIR}/web_server.log"
ENV_FILE="${APP_DIR}/.env"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8767}"
ACTION="${1:-restart}"

info() { printf '[byd-server] %s\n' "$1"; }

if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
  info "已加载环境变量文件：${ENV_FILE}"
  HOST="${HOST:-0.0.0.0}"
  PORT="${PORT:-8767}"
fi

if [ ! -f "${SERVER_SCRIPT}" ]; then
  echo "找不到服务脚本：${SERVER_SCRIPT}" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python || true)}"
if [ -z "${PYTHON_BIN}" ]; then
  echo "未找到 Python 3，请先安装 Python 3.9+" >&2
  exit 1
fi

is_running() {
  [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null
}

port_busy() {
  "${PYTHON_BIN}" - <<PY >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(("127.0.0.1", ${PORT}))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
}

start_server() {
  if is_running; then
    info "服务已在运行，PID=$(cat "${PID_FILE}")"
    return
  fi
  if port_busy; then
    echo "端口 ${PORT} 已被其他进程占用，请换端口：PORT=8768 bash $0 start" >&2
    exit 1
  fi
  info "启动服务：HOST=${HOST} PORT=${PORT} PYTHON=${PYTHON_BIN}"
  : >"${LOG_FILE}"
  HOST="${HOST}" PORT="${PORT}" nohup "${PYTHON_BIN}" -u "${SERVER_SCRIPT}" \
    >>"${LOG_FILE}" 2>&1 &
  echo $! >"${PID_FILE}"

  for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      info "服务已启动，PID=$(cat "${PID_FILE}")"
      info "本机访问：http://127.0.0.1:${PORT}/"
      info "日志文件：${LOG_FILE}"
      return
    fi
    if ! is_running; then
      break
    fi
    sleep 1
  done
  echo "服务启动失败，日志末尾：" >&2
  tail -n 60 "${LOG_FILE}" >&2 || true
  rm -f "${PID_FILE}"
  exit 1
}

stop_server() {
  if ! is_running; then
    info "服务未运行"
    rm -f "${PID_FILE}"
    return
  fi
  local pid
  pid="$(cat "${PID_FILE}")"
  info "停止服务，PID=${pid}"
  kill "${pid}" 2>/dev/null || true
  for _ in $(seq 1 10); do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "${pid}" 2>/dev/null; then
    info "进程未正常退出，强制停止"
    kill -9 "${pid}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}"
  info "服务已停止"
}

status_server() {
  if is_running; then
    info "服务运行中，PID=$(cat "${PID_FILE}")，端口=${PORT}"
    curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/health" || true
    printf '\n'
  else
    info "服务未运行"
  fi
}

case "${ACTION}" in
  start)   start_server ;;
  stop)    stop_server ;;
  restart) stop_server; start_server ;;
  status)  status_server ;;
  logs)    touch "${LOG_FILE}"; tail -n 120 -f "${LOG_FILE}" ;;
  *)
    echo "用法：$0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
