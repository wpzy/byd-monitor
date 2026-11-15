#!/usr/bin/env bash
# BYD Monitor Server - Ubuntu 24.04 幂等安装器
#
#   sudo bash deploy/ubuntu24/install.sh [源码目录]
#
# 完成：系统依赖 -> 专用用户 -> 同步代码 -> 虚拟环境 -> 环境配置
#       -> systemd 服务 -> UFW -> 本机验收。可重复执行。
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 运行：sudo bash deploy/ubuntu24/install.sh [源码目录]" >&2
  exit 1
fi

SOURCE_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
APP_DIR="${APP_DIR:-/opt/byd_monitor}"
APP_USER="${APP_USER:-bydmon}"
APP_GROUP="${APP_GROUP:-bydmon}"
STATE_DIR="${STATE_DIR:-/var/lib/byd-monitor}"
ENV_DIR="/etc/byd-monitor"
ENV_FILE="${ENV_DIR}/byd-monitor.env"
SERVICE_FILE="/etc/systemd/system/byd-monitor.service"
PORT="${PORT:-8767}"
SSH_PORT="${SSH_PORT:-22}"
ENABLE_UFW="${ENABLE_UFW:-1}"
UPDATE_HOUR="${UPDATE_HOUR:-15}"
UPDATE_MINUTE="${UPDATE_MINUTE:-30}"

log() { printf '[ubuntu24-install] %s\n' "$*"; }

upsert_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
}

if [[ ! -f "${SOURCE_DIR}/byd_web_server.py" ]]; then
  echo "源码目录无效：${SOURCE_DIR}" >&2
  exit 1
fi

log "安装 Ubuntu 24.04 系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates curl jq rsync tar tzdata ufw \
  python3 python3-pip python3-venv

if ! getent group "${APP_GROUP}" >/dev/null; then
  groupadd --system "${APP_GROUP}"
fi
if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${APP_GROUP}" \
    --home-dir "${STATE_DIR}" --create-home \
    --shell /usr/sbin/nologin "${APP_USER}"
fi

install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}" "${STATE_DIR}"
install -d -o root -g "${APP_GROUP}" -m 0750 "${ENV_DIR}"

log "同步代码到 ${APP_DIR}（保留服务器上已有的 data/ 与 report/）"
if [[ "$(realpath "${SOURCE_DIR}")" != "$(realpath "${APP_DIR}")" ]]; then
  rsync -a \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'venv/' \
    --exclude '.venv/' \
    --exclude 'data/' \
    --exclude 'report/' \
    --exclude '*.log' \
    --exclude '*.pid' \
    --exclude '.env' \
    "${SOURCE_DIR}/" "${APP_DIR}/"
else
  log "源码已位于安装目录，跳过文件同步"
fi
install -d -o "${APP_USER}" -g "${APP_GROUP}" \
  "${APP_DIR}/data" "${APP_DIR}/data/daily" "${APP_DIR}/data/cache" "${APP_DIR}/report"

if [[ -f "${SOURCE_DIR}/.env" && ! -f "${ENV_FILE}" ]]; then
  install -m 0640 -o root -g "${APP_GROUP}" "${SOURCE_DIR}/.env" "${ENV_FILE}"
elif [[ ! -f "${ENV_FILE}" ]]; then
  install -m 0640 -o root -g "${APP_GROUP}" \
    "${APP_DIR}/deploy/ubuntu24/byd-monitor.env.example" "${ENV_FILE}"
fi

# 保留运维自填的令牌 / IP，只强制网络与调度参数
upsert_env HOST "0.0.0.0"
upsert_env PORT "${PORT}"
upsert_env UPDATE_HOUR "${UPDATE_HOUR}"
upsert_env UPDATE_MINUTE "${UPDATE_MINUTE}"
upsert_env SKIP_WEEKENDS "1"
upsert_env RUN_ON_START "1"
upsert_env PYTHONUNBUFFERED "1"
upsert_env TZ "Asia/Shanghai"
chmod 0640 "${ENV_FILE}"
chown root:"${APP_GROUP}" "${ENV_FILE}"

log "在 Ubuntu 系统 Python 上重建虚拟环境"
systemctl stop byd-monitor.service 2>/dev/null || true
rm -rf "${APP_DIR}/venv"
runuser -u "${APP_USER}" -- python3 -m venv "${APP_DIR}/venv"
runuser -u "${APP_USER}" -- \
  "${APP_DIR}/venv/bin/python" -m pip install --upgrade pip setuptools wheel
runuser -u "${APP_USER}" -- \
  "${APP_DIR}/venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}" "${STATE_DIR}"

log "安装 systemd 服务"
sed \
  -e "s|User=bydmon|User=${APP_USER}|" \
  -e "s|Group=bydmon|Group=${APP_GROUP}|" \
  -e "s|/opt/byd_monitor|${APP_DIR}|g" \
  -e "s|/var/lib/byd-monitor|${STATE_DIR}|g" \
  "${APP_DIR}/deploy/ubuntu24/byd-monitor.service" >"${SERVICE_FILE}"
chmod 0644 "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable byd-monitor.service

if [[ "${ENABLE_UFW}" == "1" ]]; then
  log "配置 UFW：先保留 SSH，再开放 TCP ${PORT}"
  ufw allow "${SSH_PORT}/tcp"
  ufw allow "${PORT}/tcp"
  ufw --force enable
fi

log "启动服务并执行本机验收"
systemctl restart byd-monitor.service
"${APP_DIR}/deploy/ubuntu24/verify.sh" "127.0.0.1" "${PORT}"

log "部署完成：http://<服务器IP>:${PORT}/"
log "自动更新：每交易日 ${UPDATE_HOUR}:$(printf '%02d' "${UPDATE_MINUTE}")（北京时间）"
log "注意：云平台安全组还需放行 TCP ${PORT}；公网部署建议设置 REFRESH_TOKEN 或 Nginx 反向代理"
