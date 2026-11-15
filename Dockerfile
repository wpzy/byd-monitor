FROM python:3.12-slim

ENV HOST=0.0.0.0 \
    PORT=8767 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    UPDATE_HOUR=15 \
    UPDATE_MINUTE=30

WORKDIR /app

# HTTPS 行情接口需要 CA 证书；代码使用固定 UTC+8 时区，不依赖宿主机时区设置
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY *.py config.json ./
COPY assets/ ./assets/
COPY deploy/ ./deploy/

RUN mkdir -p /app/data/daily /app/data/cache /app/report

EXPOSE 8767

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import json,sys,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8767/health',timeout=8)); sys.exit(0 if d['ok'] else 1)"

CMD ["python", "-u", "byd_web_server.py"]
