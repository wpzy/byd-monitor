#!/usr/bin/env bash
# BYD Monitor Server - Linux（Ubuntu 24.04 / systemd）一键部署与更新
#
#   在 git clone 下来的源码目录里执行：
#       sudo bash deploy/onekey.sh
#
#   自动完成：拉取代码 → 判断首次安装 / 增量更新 → 同步到 /opt/byd_monitor
#   → 重启服务 → 触发 16 标的抓取 → 本机验收 → 输出访问地址。
#   可重复执行（幂等）。首次使用它之前需先 git pull 一次把本脚本同步下来。
#
#   可选环境变量：APP_DIR（默认 /opt/byd_monitor）、PORT（默认 8767）
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-/opt/byd_monitor}"
PORT="${PORT:-8767}"
ENV_FILE="/etc/byd-monitor/byd-monitor.env"
SERVICE="byd-monitor.service"
BASE="http://127.0.0.1:${PORT}"

log() { printf '[onekey] %s\n' "$*"; }
die() { printf '[onekey][ERROR] %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "请用 root 运行：sudo bash deploy/onekey.sh"
[[ -f "${SOURCE_DIR}/byd_web_server.py" ]] || die "源码目录无效：${SOURCE_DIR}"

cd "${SOURCE_DIR}"
if [[ -d .git ]]; then
  log "拉取最新代码：git pull --ff-only"
  git pull --ff-only || die "git pull 失败（本地有改动或冲突），请先处理再重跑"
else
  log "非 git 仓库，跳过拉取，使用当前目录文件"
fi

if [[ ! -f "${APP_DIR}/venv/bin/python" ]]; then
  log "检测到首次安装 → install.sh（装依赖 / 建用户 / 建 venv / 装 systemd）"
  bash "${SOURCE_DIR}/deploy/ubuntu24/install.sh" "${SOURCE_DIR}"
else
  log "检测到已有安装 → update.sh（同步代码 + 权限 + 重启 + 验收）"
  bash "${SOURCE_DIR}/deploy/ubuntu24/update.sh" --no-pull
fi

log "等待服务健康检查 ${BASE}/health"
HEALTH_OK=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "${BASE}/health" >/dev/null 2>&1; then HEALTH_OK=1; break; fi
  sleep 2
done
[[ "${HEALTH_OK}" == "1" ]] || die "服务健康检查不可达，查看 journalctl -u ${SERVICE} -n 60 --no-pager"

# 主动触发一次多标的抓取，保证部署完成后 /portfolio 立刻有数据。
# REFRESH_TOKEN 从 systemd 环境文件读取（若已设置）。
TOKEN=""
if [[ -f "${ENV_FILE}" ]]; then
  TOKEN="$(sed -n 's/^REFRESH_TOKEN=//p' "${ENV_FILE}" | head -1 | tr -d '\r')"
fi
if [[ -n "${TOKEN}" ]]; then
  curl -fsS --max-time 10 -X POST -H "X-Refresh-Token: ${TOKEN}" \
       "${BASE}/api/portfolio/refresh" >/dev/null 2>&1 || true
else
  curl -fsS --max-time 10 -X POST \
       "${BASE}/api/portfolio/refresh" >/dev/null 2>&1 || true
fi
log "已触发 16 标的抓取，等待缓存就绪"

PF_OK=0
for _ in $(seq 1 60); do
  if PF_JSON="$(curl -fsS --max-time 8 "${BASE}/api/portfolio/status" 2>/dev/null)"; then
    read -r PF_COUNT PF_BUSY <<<"$(printf '%s' "${PF_JSON}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(int(d.get("count") or 0), 1 if d.get("refreshing") else 0)
except Exception:
    print(0, 1)
' 2>/dev/null || echo '0 1')"
    if [[ "${PF_COUNT:-0}" -ge 16 && "${PF_BUSY:-1}" == "0" ]]; then PF_OK=1; break; fi
  fi
  sleep 2
done
if [[ "${PF_OK}" == "1" ]]; then
  log "多标的数据就绪（16/16）"
else
  log "多标的数据尚未就绪，服务本身正常；可稍后在页面点「立即刷新数据」"
fi

log "执行本机验收"
bash "${APP_DIR}/deploy/ubuntu24/verify.sh" "127.0.0.1" "${PORT}" || \
  die "验收未通过，查看 journalctl -u ${SERVICE} -n 60 --no-pager"

PRIVATE_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
log "=========================================================="
log " 部署完成"
log "   比亚迪看板 : ${BASE}/"
log "   多标的看板 : ${BASE}/portfolio   （公网请把 IP 换成弹性公网 IP）"
log "   多标的接口 : ${BASE}/api/portfolio"
log "   日志       : journalctl -u ${SERVICE} -f"
log "   自动更新   : 每交易日 15:30（北京时间），两个看板一起更新"
log "   内网 IP    : ${PRIVATE_IP:-unknown}"
log " 注意：还需在云平台安全组放行 TCP ${PORT}"
log "=========================================================="
