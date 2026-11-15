#!/usr/bin/env bash
# BYD Monitor Server 本机验收
#   sudo /opt/byd_monitor/deploy/ubuntu24/verify.sh [HOST] [PORT]
set -Eeuo pipefail

HOST_TO_TEST="${1:-127.0.0.1}"
PORT="${2:-8767}"
BASE_URL="http://${HOST_TO_TEST}:${PORT}"
VERIFY_SYSTEMD="${VERIFY_SYSTEMD:-1}"

log() { printf '[verify] %s\n' "$*"; }

log "等待 ${BASE_URL}/health（首次部署无缓存，服务会先同步抓取全量行情数据，最长等待 90 秒）"
health_payload=""
for _ in $(seq 1 90); do
  if health_payload="$(curl -fsS --max-time 5 "${BASE_URL}/health" 2>/dev/null)"; then
    break
  fi
  sleep 1
done
if [[ -z "${health_payload}" ]]; then
  echo "健康检查不可达：${BASE_URL}/health" >&2
  exit 1
fi
printf '%s' "${health_payload}" | jq -e '.ok == true and .source_exists == true' >/dev/null
log "健康检查通过（数据时点 $(printf '%s' "${health_payload}" | jq -r '.asof')）"

home_payload="$(curl -fsS --max-time 15 "${BASE_URL}/")"
grep -q "综合评分" <<<"${home_payload}"
grep -q "免责声明" <<<"${home_payload}"
log "Web 看板检查通过"

analysis_payload="$(curl -fsS --max-time 10 "${BASE_URL}/api/analysis")"
printf '%s' "${analysis_payload}" | jq -e \
  '(.total_score | type == "number") and (.signal | type == "string")' >/dev/null
log "分析 API 检查通过：$(printf '%s' "${analysis_payload}" | jq -r '.signal')"

history_payload="$(curl -fsS --max-time 10 "${BASE_URL}/api/history?days=30")"
printf '%s' "${history_payload}" | jq -e '.items | type == "array"' >/dev/null
log "每日快照 API 检查通过，共 $(printf '%s' "${history_payload}" | jq '.count') 条"

asset_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
  "${BASE_URL}/static/echarts.min.js")"
[[ "${asset_code}" == "200" ]] || { echo "静态图表库不可用：HTTP ${asset_code}" >&2; exit 1; }
log "离线图表库检查通过"

if [[ "${VERIFY_SYSTEMD}" == "1" ]] &&
   [[ "${HOST_TO_TEST}" == "127.0.0.1" || "${HOST_TO_TEST}" == "localhost" ]]; then
  systemctl is-active --quiet byd-monitor.service
  systemctl is-enabled --quiet byd-monitor.service
  log "systemd 运行与开机自启动检查通过"
fi

log "全部验收项通过"
