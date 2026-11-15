#!/usr/bin/env bash
# BYD Monitor Server - 代码更新后同步到运行目录并重启（幂等，可重复执行）
#
#   必须在 git clone 的源码目录里执行，例如：
#     cd /root/byd/byd-monitor
#     sudo bash deploy/ubuntu24/update.sh                # git pull + 同步 + 重启 + 验收
#     sudo bash deploy/ubuntu24/update.sh --no-pull      # 已在服务器本地改好代码，跳过 git pull
#     sudo bash deploy/ubuntu24/update.sh --restart-only # 只重启（改了 /etc/byd-monitor/*.env 时用）
#
#   注意：systemd 运行的是 /opt/byd_monitor（由 install.sh rsync 过去的副本），
#   不是本仓库目录。只执行 systemctl restart 而不会同步代码，改动不会生效。
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="${APP_DIR:-/opt/byd_monitor}"
APP_USER="${APP_USER:-bydmon}"
APP_GROUP="${APP_GROUP:-bydmon}"
STATE_DIR="${STATE_DIR:-/var/lib/byd-monitor}"
PORT="${PORT:-8767}"
SERVICE="byd-monitor.service"
MODE="${1:-}"

log() { printf '[byd-update] %s\n' "$*"; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "请用 root 运行：sudo bash deploy/ubuntu24/update.sh" >&2
  exit 1
fi
case "${MODE}" in
  ""|"--no-pull"|"--restart-only") ;;
  *) echo "用法：bash deploy/ubuntu24/update.sh [--no-pull|--restart-only]" >&2; exit 1 ;;
esac
if [[ ! -f "${SOURCE_DIR}/byd_web_server.py" ]]; then
  echo "源码目录无效：${SOURCE_DIR}" >&2
  exit 1
fi
if [[ ! -d "${APP_DIR}" ]]; then
  echo "运行目录不存在，请先执行 deploy/ubuntu24/install.sh 完成首次安装：${APP_DIR}" >&2
  exit 1
fi

if [[ "${MODE}" != "--restart-only" ]]; then
  if [[ "${MODE}" != "--no-pull" ]]; then
    log "拉取最新代码：git pull --ff-only"
    git -C "${SOURCE_DIR}" pull --ff-only
  else
    log "跳过 git pull：使用服务器上已改好的代码"
  fi

  log "语法自检（坏代码不会同步到运行目录）"
  python3 -m py_compile "${SOURCE_DIR}/byd_monitor.py" "${SOURCE_DIR}/byd_web_server.py"
  for f in "${SOURCE_DIR}"/deploy/ubuntu24/*.sh \
           "${SOURCE_DIR}/deploy/preflight.sh" "${SOURCE_DIR}/deploy_web_server.sh"; do
    bash -n "${f}"
  done
  log "语法检查通过"

  # --chown 让同步过去的文件直接归服务账号所有，避免 rsync -a 把源码目录的
  # root:root 带过来导致服务写不了 data/（Errno 13）。venv / data / report
  # 不在同步范围内，保持原样，不会被误改权限。
  log "同步代码到 ${APP_DIR}（保留 data/、report/、venv/）"
  rsync -a --chown="${APP_USER}:${APP_GROUP}" \
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

  chown "${APP_USER}:${APP_GROUP}" "${APP_DIR}" "${STATE_DIR}" \
        "${APP_DIR}/data" "${APP_DIR}/report" 2>/dev/null || true
  chmod 0755 "${APP_DIR}"
  if ! runuser -u "${APP_USER}" -- test -w "${APP_DIR}"; then
    echo "服务账号 ${APP_USER} 对 ${APP_DIR} 无写权限，请检查属主" >&2
    exit 1
  fi
else
  log "--restart-only：不同步代码，仅重启服务（适用于修改环境文件后）"
fi

log "重启 ${SERVICE}"
systemctl restart "${SERVICE}"

log "执行本机验收（首次抓数时最长等待 90 秒）"
bash "${APP_DIR}/deploy/ubuntu24/verify.sh" "127.0.0.1" "${PORT}"

log "更新完成：http://<服务器IP>:${PORT}/"
log "查看日志：journalctl -u byd-monitor -n 50 --no-pager"
