#!/usr/bin/env bash
# BYD Monitor Server - 部署前环境自检（只读，不安装、不修改任何系统设置）
#
#   在百度云服务器上，进入 clone 下来的项目根目录后执行：
#       bash deploy/preflight.sh
#
#   把完整输出贴回对话，用于确定走 Docker 还是 systemd 路线，
#   以及确认行情数据源在云服务器上是否可达。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "无法进入项目目录：${ROOT}"; exit 1; }

hr()  { printf '\n%s\n' "──────────── $* ────────────"; }
ok()  { printf '  [ OK ] %s\n' "$*"; }
wa()  { printf '  [WARN] %s\n' "$*"; }
no()  { printf '  [FAIL] %s\n' "$*"; }
inf() { printf '  [INFO] %s\n' "$*"; }
has() { command -v "$1" >/dev/null 2>&1; }

echo "BYD Monitor 部署前环境自检"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
echo "目录：${ROOT}"
echo "说明：本脚本只读，不会改动系统。请把完整输出贴回对话。"

hr "1. 操作系统 / 架构"
DISTRO_ID="unknown"
DISTRO_VER="0"
if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO_ID="${ID:-unknown}"
  DISTRO_VER="${VERSION_ID:-0}"
  inf "${PRETTY_NAME:-unknown}"
  inf "ID=${DISTRO_ID}  VERSION_ID=${DISTRO_VER}"
else
  wa "未找到 /etc/os-release（可能不是 Linux）"
fi
inf "kernel: $(uname -srm 2>/dev/null || echo unknown)"
inf "时区: $(date '+%Z %z')（服务内部固定按北京时间 UTC+8 调度，不依赖系统时区）"

hr "2. 权限 / 运行时"
if [ "$(id -u 2>/dev/null || echo 1)" -eq 0 ]; then
  inf "当前为 root"
elif has sudo; then
  inf "当前为普通用户，sudo 可用：$(whoami)"
else
  wa "非 root 且无 sudo：systemd 安装路线不可用，可用 Docker 或前台 python 运行"
fi

PY=""
if has python3; then PY=python3; elif has python; then PY=python; fi
PY_OK=0
if [ -n "${PY}" ]; then
  inf "Python: $(${PY} -V 2>&1)"
  if "${PY}" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
    ok "Python >= 3.9，可直接运行（项目零第三方依赖）"
    PY_OK=1
  else
    no "Python 版本过低，需要 3.9+"
  fi
else
  no "未找到 python3 / python"
fi

DOCKER_OK=0
if has docker; then
  inf "Docker: $(docker --version 2>&1 | head -1)"
  if docker compose version >/dev/null 2>&1; then
    inf "Compose: $(docker compose version 2>&1 | head -1)"
    DOCKER_OK=1
  elif has docker-compose; then
    inf "Compose: $(docker-compose --version 2>&1 | head -1)"
    wa "检测到旧版 docker-compose，命令请用 docker-compose 而非 docker compose"
  else
    wa "Docker 存在但缺少 compose 插件"
  fi
else
  inf "未安装 Docker"
fi

if [ -d /run/systemd/system ]; then
  ok "systemd 可用（支持开机自启动）"
else
  wa "systemd 不可用（容器 / 非 systemd 系统），请用 Docker 或 nohup 方式"
fi

hr "3. 仓库文件完整性"
MISSING=0
for f in byd_monitor.py byd_web_server.py config.json requirements.txt \
         Dockerfile docker-compose.yml deploy_web_server.sh \
         deploy/ubuntu24/install.sh deploy/ubuntu24/verify.sh \
         deploy/ubuntu24/byd-monitor.service assets/echarts.min.js; do
  if [ -f "$f" ]; then ok "$f"; else no "缺少 $f"; MISSING=$((MISSING+1)); fi
done
if [ -f assets/echarts.min.js ]; then
  SZ=$(wc -c < assets/echarts.min.js 2>/dev/null | tr -d ' ')
  if [ "${SZ:-0}" -ge 500000 ]; then
    ok "离线图表库大小 ${SZ} 字节（完整）"
  else
    no "图表库只有 ${SZ} 字节，疑似 Git LFS 指针或 clone 不完整，页面会没有图"
  fi
fi
if [ -f data/latest.json ]; then
  inf "存在本机缓存 data/latest.json（服务会优先展示，注意数据时点可能已过期）"
else
  inf "无 data/latest.json，首次启动会联网抓取（需第 5 项全部通过）"
fi

hr "4. 端口与防火墙"
PORT="${PORT:-8767}"
if [ "${PY_OK}" -eq 1 ]; then
  if "${PY}" - "$PORT" >/dev/null 2>&1 <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(1)
try:
    s.connect(("127.0.0.1", int(sys.argv[1]))); sys.exit(0)
except OSError:
    sys.exit(1)
PY
  then
    wa "127.0.0.1:${PORT} 已被占用，请确认是否有旧服务，或改用 PORT=其他端口"
  else
    ok "127.0.0.1:${PORT} 空闲"
  fi
fi
if has ufw; then
  inf "UFW: $(ufw status 2>/dev/null | head -1 || echo 需 root 查看)"
elif has firewall-cmd; then
  inf "firewalld: $(firewall-cmd --state 2>&1 | head -1)"
else
  inf "未检测到本机防火墙命令"
fi
wa "云服务器仍需在【百度云控制台 → 安全组】放行 TCP ${PORT}，否则公网访问不到"
inf "磁盘: $(df -h . 2>/dev/null | tail -1 | awk '{print $4" 可用 / "$2" 总量"}' || echo unknown)"

hr "5. 行情数据源连通性（最关键）"
NET_OK=0
NET_TOTAL=0
probe() {
  label="$1"; url="$2"; code="N/A"
  if has curl; then
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
                 -A 'Mozilla/5.0' "$url" 2>/dev/null)
    [ -n "$code" ] || code="000"
  elif [ "${PY_OK}" -eq 1 ]; then
    code=$("${PY}" -c 'import ssl,sys,urllib.request
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
try:
    req=urllib.request.Request(sys.argv[1], headers={"User-Agent":"Mozilla/5.0"})
    print(urllib.request.urlopen(req,timeout=10,context=ctx).status)
except Exception:
    print("000")' "$url" 2>/dev/null)
    [ -n "$code" ] || code="000"
  fi
  NET_TOTAL=$((NET_TOTAL+1))
  case "$code" in
    200|301|302|304) ok "$label → HTTP $code"; NET_OK=$((NET_OK+1)) ;;
    *)               no "$label → HTTP $code（不通，该数据源会抓取失败）" ;;
  esac
}
probe "腾讯实时行情 qt.gtimg.cn" 'https://qt.gtimg.cn/q=sz002594'
probe "东财日K push2his.eastmoney.com" 'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.002594&fields1=f1&fields2=f51,f53&klt=101&fqt=1&lmt=2'
probe "东财估值/财报 datacenter-web" 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET&columns=SECURITY_CODE,TRADE_DATE,PE_TTM&filter=(SECURITY_CODE%3D%22002594%22)&pageNumber=1&pageSize=1&source=WEB&client=WEB'
if has curl; then
  HUB_CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
                  https://registry-1.docker.io/v2/ 2>/dev/null)
  [ -n "${HUB_CODE}" ] || HUB_CODE="000"
  inf "Docker Hub 可达性: HTTP ${HUB_CODE}（401/200 均表示可达；国内云常需配置镜像加速）"
fi

hr "6. 路线建议"
if [ "${MISSING}" -gt 0 ]; then
  no "仓库文件不齐，请先重新 git pull 或检查 clone 是否完整"
fi
if [ "${NET_OK}" -lt "${NET_TOTAL}" ]; then
  wa "有数据源不通：每日自动更新会失败。请检查出站 HTTPS / CA 证书 / 代理；也可先手工把本机 data/latest.json 传到服务器作为兜底缓存"
fi
if [ "${PY_OK}" -eq 1 ] && [ "${NET_OK}" -eq "${NET_TOTAL}" ]; then
  case "${DISTRO_ID}" in
    ubuntu|debian)
      ok "推荐路线 A：systemd 原生安装（零第三方依赖，无需拉镜像）"
      inf "sudo env SSH_PORT=你的SSH端口 bash deploy/ubuntu24/install.sh \"\$(pwd)\""
      ;;
    *)
      ok "推荐路线 A：直接用 python3 前台验证，再按实际发行版改为 systemd/Docker"
      inf "HOST=0.0.0.0 PORT=${PORT} python3 -u byd_web_server.py"
      ;;
  esac
fi
if [ "${DOCKER_OK}" -eq 1 ]; then
  ok "备选路线 B：docker compose up -d --build（跨发行版，但国内拉 python:3.12-slim 可能很慢）"
else
  wa "Docker 路线暂不可用（未装 docker / compose）"
fi
inf "最快验证（不装任何东西）：HOST=0.0.0.0 PORT=${PORT} python3 -u byd_web_server.py"
inf "验收：curl -s http://127.0.0.1:${PORT}/health | python3 -m json.tool"
printf '\n自检结束。请把以上输出贴回对话，我按实际环境给出精确命令。\n'
