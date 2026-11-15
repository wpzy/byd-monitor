#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BYD Monitor Web Server - 比亚迪股价监控服务版

沿用 xbox_crawler 的部署约定：
  - Web 层：Python 标准库 http.server.ThreadingHTTPServer，零 Web 框架依赖
  - 分析层：复用 byd_monitor.py（数据抓取 / 技术指标 / 五维评分 / HTML 研报）
  - 定时层：内置 daemon 线程，每交易日收盘后自动抓取，结果落盘 JSON + CSV
  - 页面：服务端渲染的浅底研报风 Dashboard，图表库同源静态提供，离线可用
  - 部署：Ubuntu 24.04 / Python 3.12 / systemd / Docker / UFW，端口默认 8767

启动：
    python -u byd_web_server.py                 # HOST/PORT 走环境变量
    PORT=8767 UPDATE_HOUR=15 UPDATE_MINUTE=30 python -u byd_web_server.py
    bash deploy_web_server.sh start            # 本地后台启停

环境变量（完整模板见 .env.example / deploy/ubuntu24/byd-monitor.env.example）：
    HOST / PORT / UPDATE_HOUR / UPDATE_MINUTE / SCHED_CHECK_SECONDS
    SKIP_WEEKENDS / REFRESH_TOKEN / SYMBOL / COST_PRICE / RUN_ON_START
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import byd_monitor as engine

# 多标的看板是可选模块：即使 portfolio.py 缺失或导入失败，主服务仍须正常提供比亚迪看板
try:
    import portfolio as pf
    PF_IMPORT_ERROR = None
except Exception as _e:      # noqa: BLE001
    pf = None
    PF_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

BASE_DIR = Path(__file__).resolve().parent

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8767"))
UPDATE_HOUR = int(os.environ.get("UPDATE_HOUR", "15"))
UPDATE_MINUTE = int(os.environ.get("UPDATE_MINUTE", "30"))
CHECK_SECONDS = max(10, int(os.environ.get("SCHED_CHECK_SECONDS", "60")))
SKIP_WEEKENDS = os.environ.get("SKIP_WEEKENDS", "1") != "0"
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "").strip()
RUN_ON_START = os.environ.get("RUN_ON_START", "1") != "0"
PORTFOLIO_AUTO = os.environ.get("PORTFOLIO_AUTO", "1") != "0"
PORTFOLIO_WORKERS = max(1, int(os.environ.get("PORTFOLIO_WORKERS", "6")))

STATE_FILE = engine.DATA_DIR / "server_state.json"
LATEST_FILE = engine.DATA_DIR / "latest.json"
DAILY_DIR = engine.DATA_DIR / "daily"
DAILY_DIR.mkdir(parents=True, exist_ok=True)

SCHEDULE_TEXT = (f"每{'交易日' if SKIP_WEEKENDS else '日'} "
                 f"{UPDATE_HOUR:02d}:{UPDATE_MINUTE:02d}（北京时间 UTC+8）")

refresh_lock = threading.Lock()
stop_event = threading.Event()

S = {
    "ctx": None,
    "refreshing": False,
    "last_run_at": None,
    "last_error": None,
    "last_success_date": None,
    "next_run_at": None,
    "run_count": 0,
    "last_trigger": None,
    "started_at": f"{engine.now_cn():%Y-%m-%d %H:%M:%S}",
    "logs": [],
}


# ---------------------------------------------------------------------------
# 状态与持久化
# ---------------------------------------------------------------------------
def log(msg, level="INFO"):
    line = f"[{engine.now_cn():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
    print(line, flush=True)
    S["logs"].append(line)
    del S["logs"][:-100]
    try:
        with (engine.DATA_DIR / "server.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_persisted():
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for k in ("last_success_date", "run_count", "last_run_at", "last_error"):
                if k in d:
                    S[k] = d[k]
        except (OSError, ValueError):
            pass
    if LATEST_FILE.exists():
        try:
            S["ctx"] = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
            log(f"已从 {LATEST_FILE.name} 恢复最近一次分析结果")
        except (OSError, ValueError) as e:
            log(f"缓存恢复失败：{e}", "WARN")


def save_persisted():
    try:
        STATE_FILE.write_text(json.dumps({
            k: S[k] for k in ("last_success_date", "run_count", "last_run_at", "last_error")
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def daily_record(ctx):
    q, vs, ind, sc = ctx["quote"], ctx["val_stat"], ctx["ind"], ctx["scores"]
    flow5 = sum((x["main_net"] or 0) for x in ctx["flow"][-5:]) / 1e8 if ctx["flow"] else None
    return {
        "date": (q.get("asof") or "")[:10],
        "asof": q.get("asof"),
        "price": q.get("price"),
        "pct": q.get("pct"),
        "pe_ttm": q.get("pe_ttm"),
        "pe_pct": vs.get("pe_pct"),
        "pb": q.get("pb"),
        "total": round(ctx["total"], 2),
        "signal": ctx["decision"]["signal"],
        "scores": {v["label"]: round(v["score"], 1) for v in sc.values()},
        "ma20": ind.get("ma20"),
        "rsi6": ind.get("rsi6"),
        "main5_yi": round(flow5, 3) if flow5 is not None else None,
    }


def save_daily(ctx):
    rec = daily_record(ctx)
    key = rec["date"] or f"{engine.now_cn():%Y-%m-%d}"
    try:
        (DAILY_DIR / f"{key}.json").write_text(
            json.dumps(engine.clean_json(rec), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"每日快照写入失败：{e}", "WARN")


def load_history(days=60):
    out = []
    try:
        for p in sorted(DAILY_DIR.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out[-days:]


def compute_next_run():
    now = engine.now_cn()
    target = now.replace(hour=UPDATE_HOUR, minute=UPDATE_MINUTE, second=0, microsecond=0)
    if now >= target or S["last_success_date"] == f"{now:%Y-%m-%d}":
        target += timedelta(days=1)
    if SKIP_WEEKENDS:
        while target.weekday() >= 5:
            target += timedelta(days=1)
    return target


# ---------------------------------------------------------------------------
# 刷新任务（定时 / 手动 / 启动补跑共用）
# ---------------------------------------------------------------------------
def run_refresh(trigger="manual"):
    if not refresh_lock.acquire(blocking=False):
        return False, "已有刷新任务在运行"
    S["refreshing"] = True
    S["last_trigger"] = trigger
    S["last_run_at"] = f"{engine.now_cn():%Y-%m-%d %H:%M:%S}"
    log(f"开始刷新（触发方式：{trigger}）")
    try:
        cfg = engine.load_config()
        if os.environ.get("SYMBOL"):
            cfg["symbol"] = os.environ["SYMBOL"]
        if os.environ.get("COST_PRICE"):
            try:
                cfg["cost_price"] = float(os.environ["COST_PRICE"])
            except ValueError:
                pass
        ctx = engine.full_refresh(cfg, quiet=True)
        S["ctx"] = ctx
        asof_date = (ctx["quote"].get("asof") or "")[:10]
        is_today = asof_date == f"{engine.now_cn():%Y-%m-%d}"
        if not is_today:
            log(f"数据源最新交易日为 {asof_date}，非当日（休市/节假日），沿用最近交易日数据")
        LATEST_FILE.write_text(
            json.dumps(engine.clean_json(ctx), ensure_ascii=False), encoding="utf-8")
        save_daily(ctx)
        S["last_success_date"] = f"{engine.now_cn():%Y-%m-%d}"
        S["last_error"] = None
        S["run_count"] += 1
        save_persisted()
        log(f"刷新成功 | 数据时点 {ctx['quote'].get('asof')} | "
            f"评分 {ctx['total']:+.1f} | 信号 {ctx['decision']['signal']}")
        return True, "刷新成功"
    except Exception as e:      # noqa: BLE001 - 刷新失败不能拖垮服务
        S["last_error"] = f"{type(e).__name__}: {e}"
        log(f"刷新失败：{S['last_error']}\n{traceback.format_exc(limit=3)}", "ERROR")
        save_persisted()
        return False, S["last_error"]
    finally:
        S["refreshing"] = False
        S["next_run_at"] = compute_next_run().isoformat(timespec="seconds")
        refresh_lock.release()


def refresh_async(trigger="manual"):
    if refresh_lock.locked():
        return False
    t = threading.Thread(target=run_refresh, args=(trigger,), daemon=True)
    t.start()
    return True


# ---------------------------------------------------------------------------
# 多标的看板：独立刷新状态 / 后台线程，不阻塞主服务与比亚迪刷新
# ---------------------------------------------------------------------------
pf_lock = threading.Lock()
PF = {
    "refreshing": False, "last_run_at": None, "last_error": None,
    "last_trigger": None, "run_count": 0, "generated_at": None, "count": 0,
    "last_success_date": None,
}


def portfolio_refresh(trigger="manual"):
    if pf is None or not pf_lock.acquire(blocking=False):
        return False
    PF["refreshing"] = True
    PF["last_trigger"] = trigger
    PF["last_run_at"] = f"{engine.now_cn():%Y-%m-%d %H:%M:%S}"
    log(f"多标的看板开始刷新（{trigger}）：{len(pf.UNIVERSE)} 个标的")
    try:
        d = pf.run_all(workers=PORTFOLIO_WORKERS, quiet=True)
        PF.update({"last_error": None, "run_count": PF["run_count"] + 1,
                   "generated_at": d["generated_at"], "count": d["count"],
                   "last_success_date": f"{engine.now_cn():%Y-%m-%d}"})
        log(f"多标的看板刷新成功：{d['count']}/{d['count'] + d['failed']}，耗时 {d['elapsed']}s")
    except Exception as e:      # noqa: BLE001
        PF["last_error"] = f"{type(e).__name__}: {e}"
        log(f"多标的看板刷新失败：{PF['last_error']}", "ERROR")
    finally:
        PF["refreshing"] = False
        pf_lock.release()
    return True


def portfolio_async(trigger="manual"):
    if pf is None or pf_lock.locked():
        return False
    threading.Thread(target=portfolio_refresh, args=(trigger,), daemon=True).start()
    return True


def portfolio_data():
    return pf.load_cached() if pf else None


def scheduler_loop():
    log(f"定时调度已启动：{SCHEDULE_TEXT}，检查间隔 {CHECK_SECONDS}s")
    while not stop_event.is_set():
        try:
            now = engine.now_cn()
            S["next_run_at"] = compute_next_run().isoformat(timespec="seconds")
            due_time = now.hour * 60 + now.minute >= UPDATE_HOUR * 60 + UPDATE_MINUTE
            weekday_ok = (now.weekday() < 5) or not SKIP_WEEKENDS
            done_today = S["last_success_date"] == f"{now:%Y-%m-%d}"
            if due_time and weekday_ok and not done_today:
                ok, msg = run_refresh("scheduled")
                log(f"定时刷新{'成功' if ok else '失败'}：{msg}", "INFO" if ok else "ERROR")
            # 多标的看板独立调度：不依赖比亚迪刷新是否成功，同日幂等
            if (due_time and weekday_ok and PORTFOLIO_AUTO and pf is not None
                    and not PF["refreshing"]
                    and PF["last_success_date"] != f"{now:%Y-%m-%d}"):
                portfolio_async("scheduled")
        except Exception as e:      # noqa: BLE001
            log(f"调度循环异常：{type(e).__name__}: {e}", "ERROR")
        stop_event.wait(CHECK_SECONDS)


# ---------------------------------------------------------------------------
# 图表数据
# ---------------------------------------------------------------------------
def chart_payload(ctx, history):
    kl = ctx["kline"][-120:]
    val = ctx["valuation"]
    step = max(1, len(val) // 400)
    val_s = val[::step]
    flow = ctx["flow"][-30:]
    singles = ctx["singles"][-8:]
    sc = ctx["scores"]

    cum, acc = [], 0.0
    for x in flow:
        acc += (x["main_net"] or 0) / 1e8
        cum.append(round(acc, 3))

    payload = {
        "dates": [x["date"] for x in kl],
        "ohlc": [[x["open"], x["close"], x["low"], x["high"]] for x in kl],
        "vols": [[i, x["volume"], 1 if (x["close"] or 0) >= (x["open"] or 0) else -1]
                 for i, x in enumerate(kl)],
        "ma5": [round(engine.sma([y["close"] for y in kl[:i + 1]], 5) or 0, 2) if i >= 4 else None
                for i in range(len(kl))],
        "ma20": [round(engine.sma([y["close"] for y in kl[:i + 1]], 20) or 0, 2) if i >= 19 else None
                 for i in range(len(kl))],
        "ma60": [round(engine.sma([y["close"] for y in kl[:i + 1]], 60) or 0, 2) if i >= 59 else None
                 for i in range(len(kl))],
        "peDates": [x["date"] for x in val_s],
        "peVals": [x["pe_ttm"] for x in val_s],
        "peQ": ctx["val_stat"].get("pe_quantiles") or {},
        "flowDates": [x["date"] for x in flow],
        "flowMain": [round((x["main_net"] or 0) / 1e8, 3) for x in flow],
        "flowCum": cum,
        "radar": [round(sc[k]["score"], 1) for k in
                  ("valuation", "growth", "trend", "fund", "risk")],
        "qLabels": [f"{x['year']}Q{x['q']}" for x in singles],
        "qRev": [round(x["rev"] / 1e8, 1) for x in singles],
        "qNp": [round(x["np"] / 1e8, 1) for x in singles],
        "qNpYoy": [round(x["np_yoy"], 1) if x.get("np_yoy") is not None else None
                   for x in singles],
        "histDates": [x.get("date") for x in history],
        "histPrice": [x.get("price") for x in history],
        "histScore": [x.get("total") for x in history],
    }
    return engine.clean_json(payload)


def fallback_messages(ctx, hist_count):
    q, vs, sc = ctx["quote"], ctx["val_stat"], ctx["scores"]
    radar = " ｜ ".join(f"{v['label']} <b>{v['score']:+.1f}</b>" for v in sc.values())
    flow5 = sum((x["main_net"] or 0) for x in ctx["flow"][-5:]) / 1e8 if ctx["flow"] else None
    return {
        "chart_kline": "K 线图未渲染。点上方「表」可查看近 40 日开 / 高 / 低 / 收与成交量精确值；本页结论不依赖图表。",
        "chart_vol": "成交量图未渲染，逐日精确值见本节「表」视图。",
        "chart_pe": f"PE-TTM 历史带未渲染。当前 <b>{engine.fmt(vs.get('pe_ttm'), 2, 'x')}</b>，"
                    f"近 {vs['years']} 年分位 <b>{engine.fmt(vs.get('pe_pct'), 1, '%')}</b>；见「估值位置」表。",
        "chart_flow": f"资金流图未渲染。近 5 日主力净流入 <b>{engine.fmt(flow5, 2, ' 亿元')}</b>；见评分明细。",
        "chart_radar": f"雷达图未渲染。五维评分：{radar}；依据见「评分模型明细」表。",
        "chart_growth": "单季趋势图未渲染。精确数值见本节上方「单季拆分」表。",
        "chart_history": (f"历史趋势图未渲染。当前已积累 {hist_count} 个交易日的收盘快照，"
                          f"精确数值见下方「每日更新记录」表。") if hist_count else
                         "暂无历史快照，服务会在每个交易日收盘后自动记录第一笔。",
    }


# ---------------------------------------------------------------------------
# 页面渲染
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="/static/echarts.min.js"></script>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b2330;--sub:#5b6675;--line:#e3e7ed;--red:#c0392b;--green:#1e7d4f;--amber:#b7791f;--blue:#2559a8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:25px;margin:0 0 4px}h2{font-size:19px;margin:32px 0 14px;padding-left:11px;border-left:4px solid var(--blue)}
h3{font-size:15px;margin:18px 0 10px;color:var(--sub)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:16px}
.topbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:16px}
.topbar .grow{flex:1;min-width:200px}
.badge-live{display:inline-block;padding:2px 9px;border-radius:12px;background:#e8f2ea;color:var(--green);font-size:12px;font-weight:600}
.badge-idle{display:inline-block;padding:2px 9px;border-radius:12px;background:#fdf3e0;color:var(--amber);font-size:12px;font-weight:600}
.btn{border:1px solid var(--line);background:#fff;border-radius:6px;padding:6px 13px;cursor:pointer;font-size:13px;color:var(--ink)}
.btn:hover{border-color:var(--blue)}.btn:disabled{opacity:.55;cursor:not-allowed}
.btn-primary{background:var(--blue);color:#fff;border-color:var(--blue)}
.inp{border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12px;width:130px}
.hero{display:flex;gap:26px;align-items:center;flex-wrap:wrap}
.score{font-size:54px;font-weight:750;line-height:1;color:__TAGCOLOR__}
.badge{display:inline-block;padding:5px 15px;border-radius:20px;color:#fff;font-weight:650;background:__TAGCOLOR__;font-size:15px}
.hero-txt{flex:1;min-width:280px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin-top:14px}
.kpi{background:#fafbfc;border:1px solid var(--line);border-radius:8px;padding:11px 13px}
.kpi-l{font-size:12px;color:var(--sub)}.kpi-v{font-size:20px;font-weight:680;margin-top:2px}.kpi-v span{font-size:12px;font-weight:400;color:var(--sub);margin-left:4px}
table{width:100%;border-collapse:collapse;font-size:13px;background:#fff}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f0f3f7;font-weight:620;color:#33404f}
td.pos{color:var(--red);font-weight:640}td.neg{color:var(--green);font-weight:640}
.small{font-size:12px;color:var(--sub);line-height:1.6}
.chart{width:100%;height:330px}.chart-sm{width:100%;height:220px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.plan li{margin:8px 0}.plan b{color:var(--blue)}
ul{margin:8px 0;padding-left:20px}
.disc{background:#fff8f0;border:1px solid #f0dcc0;border-radius:8px;padding:15px;font-size:12.5px;color:#6b5535}
.chart-fallback{padding:13px 15px;background:#fafbfc;border:1px dashed var(--line);border-radius:8px;font-size:13px;color:var(--sub);line-height:1.9}
.chart-fallback b{color:var(--ink)}
.foot{color:var(--sub);font-size:12px;margin-top:26px;text-align:center}
.err{background:#fff;border:1px solid var(--line);border-radius:10px;padding:40px;text-align:center}
</style>
</head>
<body><div class="wrap">
<h1>__TITLE__</h1>
<div style="margin:0 0 12px"><a href="/portfolio" style="display:inline-block;padding:7px 14px;border:1px solid #2f5fd0;border-radius:8px;background:#2f5fd0;color:#fff;text-decoration:none;font-size:13px">→ 多标的投研看板（17 个标的 · 每个都有研报级详情）</a>
<span class="small" style="margin-left:10px">横向对比 · 七节依据 · 风险点位 · 条件化买入建议</span></div>
<div class="meta small" style="margin-bottom:14px">__NAME__（__CODE__.SZ，A 股 / 人民币）｜ 数据时点 __ASOF__ ｜ __STATE__ ｜ 页面生成 __GEN__</div>

<div class="topbar">
  <span id="live_badge" class="__BADGECLASS__">__SCHEDSTATE__</span>
  <div class="grow small">
    定时更新：<b>__SCHEDULE__</b> ｜ 下次：<span id="next_run">__NEXT__</span> ｜
    上次成功：<span id="last_run">__LASTRUN__</span> ｜ 累计刷新 __RUNCOUNT__ 次
  </div>
  <input id="token" class="inp" type="password" placeholder="刷新令牌（可选）" autocomplete="off">
  <button id="btn_refresh" class="btn btn-primary" onclick="doRefresh()">立即刷新数据</button>
</div>
<div id="refresh_msg" class="small" style="margin:-8px 0 14px;min-height:18px">__LASTERROR__</div>

__MAIN__

<div class="card"><h2 style="margin-top:0">数据来源与口径</h2>
<table><tr><th>数据源</th><th>接口</th><th>数据时点 / 抓取时间</th></tr>__SOURCEROWS__</table>
<h3>双源交叉校验</h3><ul>__CHECKHTML__</ul>
<h3>数据缺口</h3><ul>__WARNHTML__</ul>
<p class="small">所有数值由服务端在刷新时动态抓取，未取到的字段显示 N/A 且不计入评分。行情商与第三方数据平台属非一手来源，正式决策前请核对交易所公告与公司定期报告原文。</p>
</div>

<div class="disc"><b>⚠️ 风险提示与免责声明</b><br>__DISC_TRADE__<br><br><b>免责声明</b><br>__DISC_MAIN__</div>
<div class="foot">BYD Monitor Server · 端口 __PORT__ · __SCHEDULE__ · 仅供研究参考</div>
</div>

<script>
const D = __DATAJS__;
const F = __FALLBACKS__;
function setMsg(t){ document.getElementById('refresh_msg').textContent = t || ''; }
function toggleK(showChart){
  document.getElementById('k_chart').style.display = showChart ? 'block' : 'none';
  document.getElementById('k_table').style.display = showChart ? 'none' : 'block';
  document.getElementById('btn_chart').className = showChart ? 'btn btn-primary' : 'btn';
  document.getElementById('btn_table').className = showChart ? 'btn' : 'btn btn-primary';
  if(showChart){ window.dispatchEvent(new Event('resize')); }
}
async function pollRefresh(){
  try{
    const r = await fetch('/api/refresh/status');
    const j = await r.json();
    if(j.refreshing){ setMsg('正在抓取行情、估值与财报数据，请稍候…'); setTimeout(pollRefresh, 1500); }
    else { setMsg(j.last_error ? ('刷新完成但有错误：' + j.last_error) : '刷新完成，正在重新载入页面…'); setTimeout(function(){ location.reload(); }, 700); }
  }catch(e){ setMsg('状态查询失败：' + e); }
}
async function doRefresh(){
  const btn = document.getElementById('btn_refresh');
  btn.disabled = true; btn.textContent = '刷新中…'; setMsg('已提交刷新请求…');
  const headers = {'Content-Type': 'application/json'};
  const token = document.getElementById('token').value.trim();
  if(token){ headers['X-Refresh-Token'] = token; }
  try{
    const r = await fetch('/api/refresh', {method:'POST', headers: headers});
    const j = await r.json();
    if(r.status === 401){ setMsg('已启用令牌保护：请填入 REFRESH_TOKEN 后重试，或用 curl 携带 X-Refresh-Token 请求头。'); btn.disabled=false; btn.textContent='立即刷新数据'; return; }
    if(r.status === 409){ setMsg('已有刷新任务进行中，正在等待…'); }
    else { setMsg(j.message || '已提交刷新请求…'); }
    setTimeout(pollRefresh, 1200);
  }catch(e){ setMsg('请求失败：' + e); btn.disabled=false; btn.textContent='立即刷新数据'; }
}
setInterval(async function(){
  try{
    const r = await fetch('/api/status'); const j = await r.json();
    document.getElementById('next_run').textContent = j.next_run_text || '—';
    document.getElementById('last_run').textContent = j.last_run_at || '—';
  }catch(e){}
}, 60000);

const RED='#c0392b', GREEN='#1e7d4f';
if(typeof echarts !== 'undefined'){
  echarts.init(document.getElementById('chart_kline')).setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
    legend:{data:['日K','MA5','MA20','MA60']},
    grid:{left:55,right:20,top:40,bottom:40},
    xAxis:{type:'category',data:D.dates,boundaryGap:true},
    yAxis:{scale:true,splitLine:{lineStyle:{color:'#eef1f5'}}},
    dataZoom:[{type:'inside',start:40,end:100}],
    series:[
      {name:'日K',type:'candlestick',data:D.ohlc,
       itemStyle:{color:RED,color0:GREEN,borderColor:RED,borderColor0:GREEN}},
      {name:'MA5',type:'line',data:D.ma5,smooth:true,showSymbol:false,lineStyle:{width:1.2}},
      {name:'MA20',type:'line',data:D.ma20,smooth:true,showSymbol:false,lineStyle:{width:1.2}},
      {name:'MA60',type:'line',data:D.ma60,smooth:true,showSymbol:false,lineStyle:{width:1.2}}
    ]
  });
  echarts.init(document.getElementById('chart_vol')).setOption({
    tooltip:{trigger:'axis'},grid:{left:55,right:20,top:12,bottom:26},
    xAxis:{type:'category',data:D.dates},
    yAxis:{name:'成交量(手)',splitLine:{lineStyle:{color:'#eef1f5'}}},
    series:[{name:'成交量',type:'bar',data:D.vols.map(function(v){
      return {value:v[1],itemStyle:{color:v[2]>0?RED:GREEN}};
    })}]
  });
  echarts.init(document.getElementById('chart_pe')).setOption({
    tooltip:{trigger:'axis'},grid:{left:55,right:24,top:24,bottom:40},
    xAxis:{type:'category',data:D.peDates},
    yAxis:{name:'PE-TTM(倍)',scale:true,splitLine:{lineStyle:{color:'#eef1f5'}}},
    series:[{name:'PE-TTM',type:'line',data:D.peVals,showSymbol:false,
      lineStyle:{width:1.6,color:'#2559a8'},areaStyle:{color:'rgba(37,89,168,0.08)'},
      markLine:{silent:true,symbol:'none',label:{formatter:'{b}'},data:[
        {name:'25%',yAxis:D.peQ.q25,lineStyle:{color:GREEN,type:'dashed'}},
        {name:'50%',yAxis:D.peQ.q50,lineStyle:{color:'#b7791f',type:'dashed'}},
        {name:'75%',yAxis:D.peQ.q75,lineStyle:{color:RED,type:'dashed'}}
      ]}}]
  });
  echarts.init(document.getElementById('chart_flow')).setOption({
    tooltip:{trigger:'axis'},legend:{data:['当日主力净流入','累计净流入']},
    grid:{left:58,right:58,top:36,bottom:40},
    xAxis:{type:'category',data:D.flowDates},
    yAxis:[{name:'当日(亿)',splitLine:{lineStyle:{color:'#eef1f5'}}},{name:'累计(亿)'}],
    series:[
      {name:'当日主力净流入',type:'bar',data:D.flowMain.map(function(v){
        return {value:v,itemStyle:{color:v>=0?RED:GREEN}};
      })},
      {name:'累计净流入',type:'line',yAxisIndex:1,data:D.flowCum,smooth:true,
       showSymbol:false,lineStyle:{width:2,color:'#2559a8'}}
    ]
  });
  echarts.init(document.getElementById('chart_radar')).setOption({
    tooltip:{},
    radar:{indicator:[
      {name:'估值',max:100,min:-100},{name:'成长质量',max:100,min:-100},
      {name:'趋势技术',max:100,min:-100},{name:'资金情绪',max:100,min:-100},
      {name:'风险',max:100,min:-100}
    ],radius:'65%'},
    series:[{type:'radar',data:[{name:'五维评分',value:D.radar,
      areaStyle:{color:'rgba(37,89,168,0.18)'},lineStyle:{color:'#2559a8'}}]}]
  });
  echarts.init(document.getElementById('chart_growth')).setOption({
    tooltip:{trigger:'axis'},legend:{data:['单季营收','单季归母净利','净利同比']},
    grid:{left:58,right:58,top:30,bottom:34},
    xAxis:{type:'category',data:D.qLabels},
    yAxis:[{name:'亿元'},{name:'同比(%)'}],
    series:[
      {name:'单季营收',type:'bar',data:D.qRev,itemStyle:{color:'#9db7de'}},
      {name:'单季归母净利',type:'bar',data:D.qNp,itemStyle:{color:'#2559a8'}},
      {name:'净利同比',type:'line',yAxisIndex:1,data:D.qNpYoy,
       showSymbol:true,lineStyle:{width:2,color:RED}}
    ]
  });
  if(document.getElementById('chart_history')){
    echarts.init(document.getElementById('chart_history')).setOption({
      tooltip:{trigger:'axis'},legend:{data:['收盘价','综合评分']},
      grid:{left:55,right:55,top:32,bottom:36},
      xAxis:{type:'category',data:D.histDates},
      yAxis:[{name:'元',scale:true},{name:'评分',min:-100,max:100}],
      series:[
        {name:'收盘价',type:'line',data:D.histPrice,smooth:true,
         lineStyle:{width:2,color:'#2559a8'}},
        {name:'综合评分',type:'bar',yAxisIndex:1,data:D.histScore.map(function(v){
          return {value:v,itemStyle:{color:v>=0?RED:GREEN}};
        })}
      ]
    });
  }
} else {
  document.querySelectorAll('.chart,.chart-sm').forEach(function(el){
    el.style.height='auto'; el.style.minHeight='0';
    el.innerHTML='<div class="chart-fallback">'+(F[el.id]||'图表未渲染，精确数值见本页表格。')+'</div>';
  });
}
</script>
</body></html>"""


def esc(x):
    return engine.html_mod.escape(str(x if x is not None else "N/A"))


def render_error_page():
    return INDEX_HTML.replace("__MAIN__", """
<div class="card err">
  <h2 style="border:0;margin:0 0 10px">数据尚未就绪</h2>
  <p class="small">服务端还没有成功抓取过一次数据。请点击上方「立即刷新数据」，
  或检查服务器到行情数据源的出站 HTTPS 网络后重试。</p>
  <p class="small">最近错误：__LASTERROR__</p>
</div>""").replace("__TITLE__", "比亚迪监控 · 数据未就绪") \
        .replace("__ASOF__", "N/A").replace("__STATE__", engine.trading_state()[0]) \
        .replace("__GEN__", f"{engine.now_cn():%Y-%m-%d %H:%M}") \
        .replace("__NAME__", "比亚迪").replace("__CODE__", str(engine.load_config()["symbol"])) \
        .replace("__SCHEDULE__", SCHEDULE_TEXT).replace("__SCHEDSTATE__", "等待首次抓取") \
        .replace("__BADGECLASS__", "badge-idle").replace("__NEXT__", S["next_run_at"] or "—") \
        .replace("__LASTRUN__", S["last_run_at"] or "—") \
        .replace("__RUNCOUNT__", str(S["run_count"])) \
        .replace("__LASTERROR__", esc(S["last_error"] or "无")) \
        .replace("__TAGCOLOR__", "#5b6675").replace("__PORT__", str(PORT)) \
        .replace("__SOURCEROWS__", "").replace("__CHECKHTML__", "<li>尚未抓取</li>") \
        .replace("__WARNHTML__", "<li>尚未抓取</li>") \
        .replace("__DISC_TRADE__", esc(engine.DISCLAIMER_TRADE)) \
        .replace("__DISC_MAIN__", esc(engine.DISCLAIMER_MAIN)) \
        .replace("__DATAJS__", "{}").replace("__FALLBACKS__", "{}") \
        .replace("if(typeof echarts !== 'undefined'){", "if(false){")


def render_index(ctx, history):
    q, vs, ind, sc = ctx["quote"], ctx["val_stat"], ctx["ind"], ctx["scores"]
    fmt, total = engine.fmt, ctx["total"]
    tag = ctx["decision"]["tag"]
    tag_color = {"buy": "#c0392b", "hold": "#b7791f", "sell": "#1e7d4f"}[tag]
    kl = ctx["kline"][-120:]
    fin = ctx["financials"][-1] if ctx["financials"] else {}
    singles = ctx["singles"][-8:]

    cards = [
        ("最新价", fmt(q.get("price")), "元"),
        ("当日涨跌", fmt(q.get("pct"), 2, "%"), ""),
        ("PE-TTM", fmt(q.get("pe_ttm")), "x"),
        ("PE 5年分位", fmt(vs.get("pe_pct"), 1, "%"), ""),
        ("PB", fmt(q.get("pb")), "x"),
        ("总市值", fmt(q.get("total_mv_yi"), 0, " 亿"), ""),
    ]
    card_html = "".join(
        f'<div class="kpi"><div class="kpi-l">{esc(a)}</div>'
        f'<div class="kpi-v">{esc(b)}<span>{esc(c)}</span></div></div>' for a, b, c in cards)

    score_rows = "".join(
        f"<tr><td><b>{esc(v['label'])}</b></td><td>{v['weight']*100:.0f}%</td>"
        f"<td class='{'pos' if v['score'] >= 0 else 'neg'}'>{v['score']:+.1f}</td>"
        f"<td class='{'pos' if v['weighted'] >= 0 else 'neg'}'>{v['weighted']:+.1f}</td>"
        f"<td class='small'>{'；'.join(esc(x) for x in v['detail'])}</td></tr>"
        for v in sc.values())

    scen_rows = "".join(
        f"<tr><td>{esc(s['label'])}</td><td>{fmt(s['pe'])}x</td><td>{fmt(s['price'])} 元</td>"
        f"<td>{fmt((s['price'] / q['price'] - 1) * 100, 1, '%') if q.get('price') else 'N/A'}</td></tr>"
        for s in ctx["plan"]["scenarios"]) or \
        "<tr><td colspan='4'>历史估值数据不足</td></tr>"

    qrows = "".join(
        f"<tr><td>{esc(x['date'])} 单季</td><td>{fmt(x['rev']/1e8, 1)} 亿</td>"
        f"<td>{fmt(x['np']/1e8, 1)} 亿</td><td>{fmt(x.get('np_yoy'), 1, '%')}</td></tr>"
        for x in singles)

    fin_rows = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in (
        ("最新报告期", fin.get("date")), ("披露日", fin.get("notice")),
        ("累计营业收入", fmt(fin.get("revenue") and fin["revenue"] / 1e8, 2, " 亿元")),
        ("累计归母净利润", fmt(fin.get("net_profit") and fin["net_profit"] / 1e8, 2, " 亿元")),
        ("营收同比", fmt(fin.get("rev_yoy"), 2, "%")),
        ("归母净利同比", fmt(fin.get("np_yoy"), 2, "%")),
        ("毛利率", fmt(fin.get("gross_margin"), 2, "%")),
        ("加权 ROE", fmt(fin.get("roe"), 2, "%")),
    ))

    kl_rows = "".join(
        f"<tr><td>{esc(x['date'])}</td><td>{fmt(x['open'])}</td><td>{fmt(x['close'])}</td>"
        f"<td>{fmt(x['high'])}</td><td>{fmt(x['low'])}</td>"
        f"<td>{fmt(x['volume'], 0)}</td><td>{fmt(x['pct'], 2, '%')}</td></tr>"
        for x in reversed(kl[-40:]))

    hist_rows = "".join(
        f"<tr><td>{esc(x.get('date'))}</td><td>{fmt(x.get('price'))} 元</td>"
        f"<td>{fmt(x.get('pct'), 2, '%')}</td><td>{fmt(x.get('pe_ttm'))}x</td>"
        f"<td>{fmt(x.get('pe_pct'), 1, '%')}</td>"
        f"<td class='{'pos' if (x.get('total') or 0) >= 0 else 'neg'}'>{fmt(x.get('total'), 1)}</td>"
        f"<td>{esc(x.get('signal'))}</td></tr>"
        for x in reversed(history[-30:])) or \
        "<tr><td colspan='7'>暂无历史记录，每个交易日收盘后自动生成第一条</td></tr>"

    source_rows = "".join(
        f"<tr><td>{esc(s['name'])}</td><td class='small'>{esc(s['url'])}</td>"
        f"<td>{esc(s['time'])}</td></tr>" for s in ctx["sources"])
    check_html = "".join(f"<li>{esc(x)}</li>" for x in ctx["cross_notes"]) or "<li>无</li>"
    warn_html = "".join(f"<li>{esc(x)}</li>" for x in ctx["warnings"]) or "<li>无</li>"

    main = f"""
<div class="card hero">
  <div><div class="score">{total:+.1f}</div><div class="small" style="text-align:center">综合评分 / ±100</div></div>
  <div class="hero-txt"><span class="badge">{esc(ctx['decision']['signal'])}</span>
    <p style="margin:12px 0 0">{esc(engine.narrative(ctx))}</p></div>
</div>
<div class="kpis">__CARDHTML__</div>

<h2>一、评分模型明细</h2>
<div class="card"><table>
<tr><th>维度</th><th>权重</th><th>原始分</th><th>加权分</th><th>打分依据（逐项可追溯）</th></tr>
__SCOREROWS__
<tr><th>合计</th><th>100%</th><th>—</th><th class="{'pos' if total >= 0 else 'neg'}">{total:+.1f}</th>
<th>≥35 买入｜15~35 偏买入｜-15~15 观望｜-35~-15 减仓｜≤-35 卖出</th></tr>
</table><p class="small">趋势闸门：趋势技术分与资金情绪分同时 &lt; -20 时，买入信号强制降级为观望。本次触发：{esc('是' if ctx['decision']['gated'] else '否')}。</p></div>

<div class="grid2">
  <div class="card"><h3>五维评分雷达</h3><div id="chart_radar" class="chart"></div></div>
  <div class="card"><h3>估值位置（PE-TTM 近 {vs['years']} 年）</h3><table>
    <tr><th>指标</th><th>数值</th><th>说明</th></tr>
    <tr><td>当前 PE-TTM</td><td>{fmt(vs.get('pe_ttm'), 2, 'x')}</td><td>腾讯行情，{esc(q.get('asof'))}</td></tr>
    <tr><td>近 {vs['years']} 年分位</td><td>{fmt(vs.get('pe_pct'), 1, '%')}</td><td>样本 {vs['samples']} 个交易日</td></tr>
    <tr><td>近 3 年分位</td><td>{fmt(vs.get('pe_pct3'), 1, '%')}</td><td>短周期校验</td></tr>
    <tr><td>历史中位数</td><td>{fmt(vs.get('pe_med'), 2, 'x')}</td><td>估值中枢</td></tr>
    <tr><td>历史区间</td><td>{fmt(vs.get('pe_min'), 2, 'x')} ~ {fmt(vs.get('pe_max'), 2, 'x')}</td><td>最小 ~ 最大</td></tr>
    <tr><td>PB / 分位</td><td>{fmt(q.get('pb'), 2, 'x')} / {fmt(vs.get('pb_pct'), 1, '%')}</td><td>市净率</td></tr>
    <tr><td>PS-TTM 分位</td><td>{fmt(vs.get('ps_pct'), 1, '%')}</td><td>市销率</td></tr>
    <tr><td>PEG</td><td>{fmt(vs.get('peg'), 2) if vs.get('peg') else 'N/A（净利同比为负）'}</td><td>负增速时不适用</td></tr>
    <tr><td>前瞻 PE</td><td>{fmt(vs.get('fwd_pe'), 2, 'x') if vs.get('fwd_pe') else 'N/A（未填 EPS 预测）'}</td><td>程序不编造预测</td></tr>
  </table></div>
</div>

<h2>二、价格走势与技术面</h2>
<div class="card">
  <div style="margin-bottom:10px">
    <button class="btn btn-primary" id="btn_chart" onclick="toggleK(true)">图</button>
    <button class="btn" id="btn_table" onclick="toggleK(false)">表（近 40 日精确值）</button>
    <span class="small" style="margin-left:10px">「图 / 表」只切换本节 K 线与成交量</span>
  </div>
  <div id="k_chart"><div id="chart_kline" class="chart"></div><div id="chart_vol" class="chart-sm"></div></div>
  <div id="k_table" style="display:none;max-height:420px;overflow:auto"><table>
  <tr><th>日期</th><th>开盘</th><th>收盘</th><th>最高</th><th>最低</th><th>成交量(手)</th><th>涨跌幅</th></tr>__KLROWS__</table></div>
  <p class="small">前复权日 K，来源：东方财富 push2his，最新交易日 {esc(kl[-1]['date'] if kl else 'N/A')}。A 股习惯：红涨绿跌。图表库由本站 /static 同源提供，断网可用。</p>
</div>
<div class="card"><table><tr><th>技术指标</th><th>数值</th><th>技术指标</th><th>数值</th></tr>
<tr><td>MA5 / MA10 / MA20</td><td>{fmt(ind.get('ma5'))} / {fmt(ind.get('ma10'))} / {fmt(ind.get('ma20'))}</td>
<td>MA60 / MA120 / MA250</td><td>{fmt(ind.get('ma60'))} / {fmt(ind.get('ma120'))} / {fmt(ind.get('ma250'))}</td></tr>
<tr><td>MACD 柱 / DIF / DEA</td><td>{fmt(ind.get('macd_hist'), 3)} / {fmt(ind.get('dif'), 3)} / {fmt(ind.get('dea'), 3)}</td>
<td>RSI6 / RSI12 / RSI24</td><td>{fmt(ind.get('rsi6'), 1)} / {fmt(ind.get('rsi12'), 1)} / {fmt(ind.get('rsi24'), 1)}</td></tr>
<tr><td>KDJ K / D / J</td><td>{fmt(ind.get('k'), 1)} / {fmt(ind.get('d'), 1)} / {fmt(ind.get('j'), 1)}</td>
<td>BOLL 上 / 中 / 下</td><td>{fmt(ind.get('boll_upper'))} / {fmt(ind.get('boll_mid'))} / {fmt(ind.get('boll_lower'))}</td></tr>
<tr><td>近 20 日高 / 低</td><td>{fmt(ind.get('high20'))} / {fmt(ind.get('low20'))}</td>
<td>52 周区间位置</td><td>{fmt(ctx.get('pos52'), 1, '%')}</td></tr>
<tr><td>ATR14 / 年化波动率</td><td>{fmt(ind.get('atr14'))} / {fmt(ind.get('volatility_annual'), 1, '%')}</td>
<td>量比（自算 / 行情商）</td><td>{fmt(ind.get('vol_ratio_calc'))} / {fmt(q.get('volume_ratio'))}</td></tr>
</table></div>

<h2>三、估值带、资金流与每日更新趋势</h2>
<div class="card"><h3>PE-TTM 历史带（虚线为 25% / 50% / 75% 分位）</h3>
<div id="chart_pe" class="chart"></div>
<p class="small">来源：东方财富 datacenter RPT_VALUEANALYSIS_DET，{esc(ctx['valuation'][0]['date'] if ctx['valuation'] else 'N/A')} ~ {esc(ctx['valuation'][-1]['date'] if ctx['valuation'] else 'N/A')}。</p></div>
<div class="card"><h3>主力资金净流入（亿元，近 30 交易日；主力 = 大单 + 超大单）</h3>
<div id="chart_flow" class="chart"></div>
<p class="small">来源：东方财富 push2his fflow/daykline，{esc(ctx['flow'][0]['date'] if ctx['flow'] else 'N/A')} ~ {esc(ctx['flow'][-1]['date'] if ctx['flow'] else 'N/A')}。</p></div>
<div class="card"><h3>每日定时更新记录（收盘价 / 综合评分）</h3>
<div id="chart_history" class="chart-sm"></div>
<div style="max-height:300px;overflow:auto;margin-top:12px"><table>
<tr><th>交易日</th><th>收盘价</th><th>涨跌</th><th>PE-TTM</th><th>PE 分位</th><th>综合评分</th><th>信号</th></tr>__HISTROWS__</table></div>
<p class="small">每交易日 {UPDATE_HOUR:02d}:{UPDATE_MINUTE:02d} 自动抓取后写入 data/daily/*.json，重启服务不丢失。</p></div>

<h2>四、财务与业绩拐点</h2>
<div class="grid2">
  <div class="card"><h3>最新财报（累计口径）</h3><table>__FINROWS__</table>
  <p class="small">来源：东方财富 datacenter RPT_LICO_FN_CPD（公司季报/年报的二次整理，需核实公告原文）。</p></div>
  <div class="card"><h3>单季拆分（累计值差分，识别同比拐点）</h3><table>
  <tr><th>单季</th><th>营业收入</th><th>归母净利润</th><th>净利同比</th></tr>__QROWS__</table>
  <div id="chart_growth" class="chart-sm" style="margin-top:12px"></div>
  <p class="small">由程序按「本期累计 − 上期累计」差分计算，含会计调整，与公告单季数可能有细微出入。</p></div>
</div>

<h2>五、条件化交易计划</h2>
<div class="card plan"><ul>
<li><b>入场（右侧确认）</b>：{esc(ctx['plan']['entry'])}</li>
<li><b>加仓</b>：{esc(ctx['plan']['add'])}</li>
<li><b>止损</b>：{esc(ctx['plan']['stop'])}</li>
<li><b>失效条件</b>：{esc(ctx['plan']['invalidate'])}</li>
</ul>
<h3>估值情景区间（非目标价预测）</h3><table>
<tr><th>情景</th><th>对应 PE</th><th>对应价格</th><th>相对现价</th></tr>__SCENROWS__</table>
<p class="small">{esc(ctx['plan']['note'])}</p>
<p class="small">适用前提：平衡型风险偏好、3-12 个月波段、A 股境内交易；未提供个人资金量与现有仓位，故不给出具体仓位金额。可在 config.json 调整后重跑。</p>
</div>
"""

    repl = {
        "__TITLE__": f"{q.get('name', '比亚迪')}（{ctx['cfg']['symbol']}）监控看板",
        "__TAGCOLOR__": tag_color,
        "__ASOF__": q.get("asof", "N/A"),
        "__STATE__": engine.trading_state()[0],
        "__GEN__": f"{engine.now_cn():%Y-%m-%d %H:%M}（北京时间）",
        "__NAME__": q.get("name", ""),
        "__CODE__": str(ctx["cfg"]["symbol"]),
        "__SCHEDULE__": SCHEDULE_TEXT,
        "__SCHEDSTATE__": "刷新中" if S["refreshing"] else "服务运行中",
        "__BADGECLASS__": "badge-idle" if S["refreshing"] else "badge-live",
        "__NEXT__": S["next_run_at"] or "—",
        "__LASTRUN__": S["last_run_at"] or "—",
        "__RUNCOUNT__": str(S["run_count"]),
        "__LASTERROR__": ("最近一次刷新失败：" + esc(S["last_error"])) if S["last_error"] else "",
        "__PORT__": str(PORT),
        "__SOURCEROWS__": source_rows,
        "__CHECKHTML__": check_html,
        "__WARNHTML__": warn_html,
        "__DISC_TRADE__": esc(engine.DISCLAIMER_TRADE),
        "__DISC_MAIN__": esc(engine.DISCLAIMER_MAIN),
        "__DATAJS__": json.dumps(chart_payload(ctx, history), ensure_ascii=False, allow_nan=False),
        "__FALLBACKS__": json.dumps(engine.clean_json(fallback_messages(ctx, len(history))),
                                    ensure_ascii=False, allow_nan=False),
    }
    out = INDEX_HTML.replace("__MAIN__", main)
    for k, v in repl.items():
        out = out.replace(k, str(v))
    # 主模板内部残留的表格占位符
    for k, v in {
        "__CARDHTML__": card_html, "__SCOREROWS__": score_rows,
        "__KLROWS__": kl_rows, "__FINROWS__": fin_rows, "__QROWS__": qrows,
        "__HISTROWS__": hist_rows, "__SCENROWS__": scen_rows,
    }.items():
        out = out.replace(k, str(v))
    return out


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------
class BydRequestHandler(BaseHTTPRequestHandler):
    server_version = "BydMonitorServer/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        path = parsed.path
        try:
            if path == "/":
                ctx = S["ctx"]
                if ctx is None:
                    self.send_html(render_error_page())
                else:
                    self.send_html(render_index(ctx, load_history()))
                return
            if path == "/health":
                self.send_json({
                    "ok": S["ctx"] is not None,
                    "data_ready": S["ctx"] is not None,
                    "source_exists": LATEST_FILE.exists(),
                    "source_file": str(LATEST_FILE),
                    "asof": (S["ctx"] or {}).get("quote", {}).get("asof"),
                    "last_success_date": S["last_success_date"],
                    "next_run_at": S["next_run_at"],
                    "refresh_in_progress": S["refreshing"],
                    "schedule": SCHEDULE_TEXT,
                    "timezone": "Asia/Shanghai (UTC+8)",
                    "portfolio": {"module": "ok" if pf else "unavailable",
                                  "cache_exists": portfolio_data() is not None,
                                  "refreshing": PF["refreshing"],
                                  "generated_at": PF["generated_at"],
                                  "count": PF["count"]},
                })
                return
            if path == "/api/status":
                nxt = compute_next_run()
                self.send_json({
                    **{k: S[k] for k in ("refreshing", "last_run_at", "last_error",
                                          "last_success_date", "run_count", "last_trigger",
                                          "started_at")},
                    "next_run_at": S["next_run_at"],
                    "next_run_text": f"{nxt:%Y-%m-%d %H:%M}",
                    "schedule": SCHEDULE_TEXT,
                    "market_state": engine.trading_state()[0],
                    "logs": S["logs"][-20:],
                })
                return
            if path == "/api/refresh/status":
                self.send_json({
                    "refreshing": S["refreshing"],
                    "last_run_at": S["last_run_at"],
                    "last_error": S["last_error"],
                    "last_success_date": S["last_success_date"],
                })
                return
            if path == "/api/analysis":
                ctx = S["ctx"]
                if ctx is None:
                    self.send_json({"error": "数据未就绪"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self.send_json(engine.clean_json({
                    "asof": ctx["quote"].get("asof"),
                    "name": ctx["quote"].get("name"),
                    "symbol": ctx["cfg"]["symbol"],
                    "quote": {k: ctx["quote"].get(k) for k in
                              ("price", "pct", "pe_ttm", "pe_static", "pb", "total_mv_yi",
                               "turnover", "volume_ratio", "high52", "low52")},
                    "valuation": ctx["val_stat"],
                    "scores": {v["label"]: {"score": v["score"], "weight": v["weight"],
                                            "weighted": v["weighted"]}
                               for v in ctx["scores"].values()},
                    "total_score": ctx["total"],
                    "signal": ctx["decision"]["signal"],
                    "gated": ctx["decision"]["gated"],
                    "narrative": engine.narrative(ctx),
                    "indicators": {k: ctx["ind"].get(k) for k in
                                   ("ma5", "ma20", "ma60", "rsi6", "macd_hist", "j",
                                    "boll_upper", "boll_lower")},
                    "sources": ctx["sources"],
                }))
                return
            if path == "/api/history":
                days = engine.num(q.get("days", ["60"])[0], 60)
                self.send_json({"count": len(load_history(int(days or 60))),
                                "items": load_history(int(days or 60))})
                return
            if path == "/portfolio":
                if pf is None:
                    self.send_json({"error": f"多标的模块不可用：{PF_IMPORT_ERROR}"},
                                   status=HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                d = portfolio_data()
                if not d:
                    portfolio_async("on-demand")
                    self.send_html(pf.render_page(
                        {"generated_at": f"{engine.now_cn():%Y-%m-%d %H:%M:%S}",
                         "asof": "数据抓取中", "count": 0, "failed": 0,
                         "items": [], "errors": [], "disclaimer": pf.DISCLAIMER},
                        standalone=False))
                    return
                self.send_html(pf.render_page(d, standalone=False))
                return
            if path == "/api/portfolio":
                d = portfolio_data()
                if d is None:
                    self.send_json({"error": "多标的数据未就绪",
                                    "import_error": PF_IMPORT_ERROR},
                                   status=HTTPStatus.SERVICE_UNAVAILABLE)
                else:
                    self.send_json(d)
                return
            if path == "/api/portfolio/status":
                self.send_json({**PF, "module": "ok" if pf else "unavailable",
                                "import_error": PF_IMPORT_ERROR,
                                "auto": PORTFOLIO_AUTO,
                                "cache_exists": portfolio_data() is not None})
                return
            if path == "/report":
                ctx = S["ctx"]
                if ctx is None:
                    self.send_json({"error": "数据未就绪"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                else:
                    self.send_html(engine.render_report(ctx))
                return
            if path == "/static/echarts.min.js":
                if not engine.ECHARTS_LOCAL.exists():
                    self.send_error(HTTPStatus.NOT_FOUND, "echarts asset missing")
                    return
                payload = engine.ECHARTS_LOCAL.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as e:      # noqa: BLE001
            log(f"处理 GET {path} 异常：{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}", "ERROR")
            try:
                self.send_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            except OSError:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path not in ("/api/refresh", "/api/portfolio/refresh"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        # 无论客户端是否带 body，都读完，避免 HTTP/1.1 连接复用串包
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
        if REFRESH_TOKEN:
            supplied = self.headers.get("X-Refresh-Token", "")
            if supplied != REFRESH_TOKEN:
                self.send_json({"error": "刷新令牌无效，请在 X-Refresh-Token 请求头中携带 REFRESH_TOKEN"},
                               status=HTTPStatus.UNAUTHORIZED)
                return
        if path == "/api/portfolio/refresh":
            started = portfolio_async("manual")
            self.send_json({
                "started": started, "status": {**PF},
                "message": "已在后台启动多标的抓取，完成后刷新 /portfolio 页面" if started
                           else "已有刷新任务在运行，请稍候",
            }, status=HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
            return
        started = refresh_async("manual")
        self.send_json({
            "started": started,
            "message": "已在后台启动刷新，请轮询 /api/refresh/status" if started
                       else "已有刷新任务在运行，请稍候",
            "status": {k: S[k] for k in ("refreshing", "last_run_at", "last_error",
                                          "last_success_date", "run_count")},
        }, status=HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)

    def do_HEAD(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_html(self, text):
        payload = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data, status=HTTPStatus.OK):
        payload = json.dumps(engine.clean_json(data), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        # 访问日志统一进 stdout / journald，避免每请求一行造成噪音可在此降级
        pass


class BydHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    load_persisted()
    pf_cached = portfolio_data()
    if pf_cached:
        PF["generated_at"] = pf_cached.get("generated_at")
        PF["count"] = pf_cached.get("count", 0)
    S["next_run_at"] = compute_next_run().isoformat(timespec="seconds")
    if RUN_ON_START and S["ctx"] is None:
        log("未找到分析缓存，启动时先执行一次全量抓取")
        run_refresh("startup")

    threading.Thread(target=scheduler_loop, daemon=True).start()
    if PORTFOLIO_AUTO and pf is not None and portfolio_data() is None:
        portfolio_async("startup")

    try:
        server = BydHTTPServer((HOST, PORT), BydRequestHandler)
    except OSError as e:
        raise SystemExit(f"无法启动服务：{HOST}:{PORT} 不可用（{e}）。"
                         f"可通过 PORT=其他端口 python -u byd_web_server.py 避让。")
    log(f"服务已启动：http://{HOST}:{PORT}/  |  {SCHEDULE_TEXT}")
    log(f"健康检查：/health  状态：/api/status  分析数据：/api/analysis  完整研报：/report")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("正在关闭服务…")
    finally:
        stop_event.set()
        server.server_close()
        log("服务已停止")


if __name__ == "__main__":
    main()
