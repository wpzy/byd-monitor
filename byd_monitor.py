#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BYD Monitor - 股价监控 / 估值统计 / 专业分析与买卖信号

零第三方依赖，仅用 Python 标准库。所有行情、财务、估值数值均在运行时
从公开接口动态获取，代码中不内置任何行情数值。

用法:
    python byd_monitor.py --once                  单次全量分析 + 生成 HTML 研报
    python byd_monitor.py --watch                常驻监控（交易时段 60s / 其余 300s）
    python byd_monitor.py --watch --interval 30  自定义轮询秒数
    python byd_monitor.py --once --cost 85.0     覆盖配置里的持仓成本价

数据源:
    腾讯行情 qt.gtimg.cn                          实时报价 / PE / PB / 市值 / 52周区间
    东方财富 push2                                实时报价交叉校验
    东方财富 push2his kline                       前复权日 K
    东方财富 datacenter RPT_VALUEANALYSIS_DET     历史每日 PE_TTM / PB / PS
    东方财富 datacenter RPT_LICO_FN_CPD          季度财报摘要
    东方财富 datacenter RPT_DMSK_FN_BALANCE      资产负债表
    东方财富 push2his fflow/daykline             每日主力资金流

输出:
    report/BYD-<code>-<date>.html   专业分析研报（含图表与评分明细）
    data/snapshots.csv              每次分析的时间序列快照
    data/alerts.log                 告警日志
    data/monitor.log                运行日志
    data/state.json                 告警去重状态
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html as html_mod
import json
import math
import re
import ssl
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "report"
CACHE_DIR = DATA_DIR / "cache"
CONFIG_PATH = ROOT / "config.json"
for _d in (DATA_DIR, REPORT_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CN_TZ = dt.timezone(dt.timedelta(hours=8))
ASSETS_DIR = ROOT / "assets"
ECHARTS_LOCAL = ASSETS_DIR / "echarts.min.js"


def echarts_script_tag():
    """把 ECharts 内嵌进 HTML，报告完全离线可用；本地库缺失时才回退 CDN。

    内嵌优先是刻意为之：预览面板 / 内网 / 断网环境下 CDN 常常不可达，
    外链 script 加载失败会让整页图表全废。
    """
    try:
        lib = ECHARTS_LOCAL.read_text(encoding="utf-8")
        if len(lib) > 100000 and "</script" not in lib.lower():
            return "<script>\n" + lib + "\n</script>"
    except (OSError, UnicodeError):
        pass
    return ('<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>\n'
            '<!-- 本地 assets/echarts.min.js 缺失，已回退 CDN；离线环境下图表可能不渲染 -->')


DISCLAIMER_MAIN = (
    "免责声明：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。"
    "市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，"
    "必要时咨询持牌专业机构。过往表现不预示未来收益。"
)
DISCLAIMER_TRADE = (
    "风险提示与免责声明：本文/本对话内容仅为基于公开信息的分析讨论，不构成任何投资建议、"
    "理财建议或交易邀约。市场有风险，投资需谨慎。文中所涉标的、价位、策略均仅供研究参考，"
    "不保证分析准确性或完整性，不代表对任何投资结果的承诺。投资者应根据自身风险承受能力、"
    "投资目标和财务状况独立做出决策并承担相应风险。过往业绩不预示未来表现。"
)

DEFAULT_CONFIG = {
    "symbol": "002594",
    "name": "比亚迪",
    "cost_price": None,
    "risk_profile": "balanced",
    "holding_horizon": "medium",
    "analyst_eps_forecast": None,
    "valuation_years": 5,
    "kline_days": 420,
    "weights": {"valuation": 0.25, "growth": 0.25, "trend": 0.25, "fund": 0.15, "risk": 0.10},
    "alerts": {
        "pct_change": 4.0,
        "gap_pct": 2.0,
        "volume_ratio": 2.0,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "main_flow_threshold_yuan": 100000000,
        "take_profit_pct": 15.0,
        "stop_loss_pct": -8.0,
        "trailing_start_pct": 5.0,
        "trailing_drawdown_pct": 10.0,
        "pe_low_percentile": 20,
        "pe_high_percentile": 80,
        "key_levels": [],
    },
    "watch": {
        "interval_trading": 60,
        "interval_idle": 300,
        "full_refresh_minutes": 30,
        "webhook": "",
        "webhook_type": "wecom",
    },
}


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------
class DataError(Exception):
    pass


_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def http_get(url: str, encoding: str = "utf-8", retries: int = 3, timeout: int = 15) -> str:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
                    "Referer": "https://data.eastmoney.com/",
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                return resp.read().decode(encoding, "ignore")
        except Exception as e:      # noqa: BLE001 - 网络错误一律重试后降级
            last = e
            time.sleep(0.5 * (i + 1))
    raise DataError(f"网络请求失败（重试{retries}次）: {type(last).__name__}: {last}")


def post_json(url: str, payload: dict, timeout: int = 8) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        resp.read()


def clamp(v, lo=-100.0, hi=100.0):
    if v is None:
        return 0.0
    return max(lo, min(hi, v))


def safe_div(a, b, default=None):
    try:
        if a is None or b in (None, 0):
            return default
        return a / b
    except (TypeError, ZeroDivisionError):
        return default


def num(v, default=None):
    try:
        if v is None or v == "" or v == "-":
            return default
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def fmt(v, digits=2, suffix="", na="N/A"):
    if v is None:
        return na
    try:
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return na


def clean_json(obj):
    """递归清掉 NaN / Infinity，保证 json.dumps(allow_nan=False) 不会炸。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
    return obj


def percentile_rank(series, value):
    """返回 (百分位 0-100, 中位数, 标准差)。只纳入有效正数。"""
    vals = [x for x in series if isinstance(x, (int, float)) and math.isfinite(x) and x > 0]
    if not vals or value is None or value <= 0:
        return None, (round(statistics.median(vals), 3) if vals else None), \
               (round(statistics.pstdev(vals), 3) if len(vals) > 1 else None)
    below = sum(1 for x in vals if x <= value)
    return round(100.0 * below / len(vals), 1), round(statistics.median(vals), 3), \
        round(statistics.pstdev(vals), 3)


def quantile(vals, q):
    s = sorted(v for v in vals if isinstance(v, (int, float)) and math.isfinite(v) and v > 0)
    if not s:
        return None
    pos = (len(s) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (pos - lo)


def now_cn():
    return dt.datetime.now(CN_TZ)


def trading_state(t=None):
    t = t or now_cn()
    if t.weekday() >= 5:
        return "休市（周末）", False
    hm = t.hour * 60 + t.minute
    if 9 * 60 + 15 <= hm < 9 * 60 + 30:
        return "集合竞价", False
    if 9 * 60 + 30 <= hm < 11 * 60 + 30:
        return "盘中（上午）", True
    if 11 * 60 + 30 <= hm < 13 * 60:
        return "午间休市", False
    if 13 * 60 <= hm < 15 * 60:
        return "盘中（下午）", True
    if hm < 9 * 60 + 15:
        return "盘前", False
    return "盘后（已收盘）", False


def log(msg, level="INFO", echo=True):
    line = f"[{now_cn():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
    if echo:
        print(line, flush=True)
    try:
        with (DATA_DIR / "monitor.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (OSError, ValueError) as e:
            log(f"配置文件读取失败，使用默认配置: {e}", "WARN")
    return cfg


# ----------------------------------------------------------------------------
# 数据层
# ----------------------------------------------------------------------------
class DataSource:
    def __init__(self, cfg):
        self.cfg = cfg
        self.symbol = str(cfg["symbol"])
        self.sources = []
        self.warnings = []

    def _mark(self, name, url, ts=None):
        self.sources.append({"name": name, "url": url.split("?")[0], "time": ts or f"{now_cn():%Y-%m-%d %H:%M:%S}"})

    def _cached(self, key, ttl_min, producer):
        p = CACHE_DIR / (hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")
        if p.exists() and ttl_min > 0:
            try:
                age = time.time() - p.stat().st_mtime
                if age < ttl_min * 60:
                    return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        data = producer()
        try:
            p.write_text(json.dumps(clean_json(data), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return data

    @property
    def _is_sh(self):
        m = self.cfg.get("market")
        if m:
            return str(m).lower() in ("sh", "1", "sse")
        # 沪市：6 开头股票 / 5 开头 ETF·LOF / 9 开头 B 股；深市：0、1(ETF)、2、3 开头
        return self.symbol.startswith(("5", "6", "9"))

    @property
    def secid(self):
        return ("1." if self._is_sh else "0.") + self.symbol

    @property
    def txcode(self):
        return ("sh" if self._is_sh else "sz") + self.symbol

    # ---- 实时报价：腾讯主源 ----
    def quote_tencent(self):
        url = f"https://qt.gtimg.cn/q={self.txcode}"
        raw = http_get(url, encoding="gbk")
        m = re.search(r'="(.*?)"', raw)
        if not m:
            raise DataError("腾讯行情返回格式异常")
        f = m.group(1).split("~")
        if len(f) < 75:
            raise DataError("腾讯行情字段不足")
        ts_raw = f[30]
        try:
            ts = dt.datetime.strptime(ts_raw, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = ts_raw
        q = {
            "name": f[1], "code": f[2],
            "price": num(f[3]), "prev_close": num(f[4]), "open": num(f[5]),
            "volume_lots": num(f[6]),
            "asof": ts,
            "change": num(f[31]), "pct": num(f[32]),
            "high": num(f[33]), "low": num(f[34]),
            "amount_wan": num(f[37]), "turnover": num(f[38]),
            "pe_ttm": num(f[39]), "amplitude": num(f[43]),
            "float_mv_yi": num(f[44]), "total_mv_yi": num(f[45]),
            "pb": num(f[46]), "limit_up": num(f[47]), "limit_down": num(f[48]),
            "volume_ratio": num(f[49]), "avg_price": num(f[51]),
            "pe_fwd": num(f[52]), "pe_static": num(f[53]),
            "chg_ytd": num(f[62]), "chg_5d": num(f[63]),
            "high52": num(f[67]), "low52": num(f[68]),
            "chg_10d": num(f[69]), "chg_20d": num(f[70]), "chg_60d": num(f[71]),
            "float_shares": num(f[72]), "total_shares": num(f[73]),
        }
        self._mark("腾讯行情（实时报价/估值）", url, ts)
        return q

    # ---- 实时报价：东财交叉校验 ----
    def quote_em(self):
        fields = "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f84,f85,f86,f116,f117," \
                 "f162,f163,f164,f167,f168,f169,f170,f171"
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={self.secid}&fields={fields}"
        j = json.loads(http_get(url))
        d = (j.get("data") or {})
        if not d:
            raise DataError("东财实时行情无数据")
        scale = 10 ** int(d.get("f59") or 2)

        def sc(key):
            v = num(d.get(key))
            return v / scale if v is not None else None

        out = {
            "price": sc("f43"), "prev_close": sc("f60"), "pe_ttm": sc("f164"),
            "pe_static": sc("f163"), "pe_fwd": sc("f162"), "pb": sc("f167"),
            "pct": sc("f170"), "turnover": sc("f168"),
            "total_mv": num(d.get("f116")), "float_mv": num(d.get("f117")),
            "total_shares": num(d.get("f84")), "float_shares": num(d.get("f85")),
        }
        self._mark("东方财富 push2（交叉校验）", url)
        return out

    def cross_check(self, tx, em):
        notes = []
        for label, a, b, unit in (
            ("现价", tx.get("price"), em.get("price"), ""),
            ("PE-TTM", tx.get("pe_ttm"), em.get("pe_ttm"), "x"),
            ("PB", tx.get("pb"), em.get("pb"), "x"),
        ):
            if a is None or b is None:
                continue
            diff = abs(a - b) / max(abs(b), 1e-9) * 100
            if diff > 0.5:
                msg = f"{label}双源分歧：腾讯 {fmt(a)}{unit} / 东财 {fmt(b)}{unit}（相差 {diff:.2f}%），采用腾讯口径"
                notes.append(msg)
                self.warnings.append(msg)
        if not notes:
            notes.append("现价 / PE-TTM / PB 已通过腾讯行情与东方财富双源交叉校验，口径一致")
        return notes

    # ---- 前复权日 K（东财主源 + 腾讯回退，多源冗余）----
    # 云服务器上常见「东财 push2his 返回 data:null / 连接被拒」，单一数据源会让
    # 全部标的直接失败。这里按「东财 HTTPS → 东财 HTTP → 东财备用节点 →
    # 腾讯前复权 → 腾讯不复权」逐级降级；任一级拿到 >=60 根有效 K 线即采用。
    def kline(self, days=420):
        days = int(days)
        beg = (now_cn() - dt.timedelta(days=int(days / 0.68) + 60)).strftime("%Y%m%d")
        em_q = (f"/api/qt/stock/kline/get?secid={self.secid}"
                f"&fields1=f1,f2,f3,f4,f5,f6"
                f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                f"&klt=101&fqt=1&beg={beg}&end=20500101&lmt=1000")
        tx_n = max(days, 420)
        tx_fq = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                 f"?param={self.txcode},day,,,{tx_n},qfq")
        tx_raw = (f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
                  f"?param={self.txcode},day,,,{tx_n}")

        def parse_em(raw):
            j = json.loads(raw)
            out = []
            for line in ((j.get("data") or {}).get("klines") or []):
                p = line.split(",")
                if len(p) < 11:
                    continue
                out.append({
                    "date": p[0], "open": num(p[1]), "close": num(p[2]),
                    "high": num(p[3]), "low": num(p[4]), "volume": num(p[5]),
                    "amount": num(p[6]), "amplitude": num(p[7]),
                    "pct": num(p[8]), "change": num(p[9]), "turnover": num(p[10]),
                })
            return out, True

        def parse_tx(raw):
            j = json.loads(raw)
            node = (j.get("data") or {}).get(self.txcode) or {}
            # 个股一般返回 qfqday（前复权），ETF 通常只有 day（不复权）
            fq = bool(node.get("qfqday"))
            arr = node.get("qfqday") or node.get("day") or []
            out, prev = [], None
            for p in arr:
                if not isinstance(p, (list, tuple)) or len(p) < 6:
                    continue
                o, c, h, l = num(p[1]), num(p[2]), num(p[3]), num(p[4])
                if None in (o, c, h, l) or c <= 0:
                    continue
                out.append({
                    "date": str(p[0]), "open": o, "close": c, "high": h, "low": l,
                    "volume": num(p[5]),
                    # 腾讯 6 列只有 OHLCV，成交额/换手率不编造，统一留空 → 页面显示 N/A
                    "amount": num(p[6]) if len(p) > 6 else None,
                    "amplitude": round((h - l) / prev * 100, 2) if prev else None,
                    "pct": round((c / prev - 1) * 100, 2) if prev else None,
                    "change": round(c - prev, 3) if prev else None,
                    "turnover": None,
                })
                prev = c
            return out, fq

        sources = (
            ("东方财富 push2his（前复权日K）", "https://push2his.eastmoney.com" + em_q,
             parse_em, 2, 12),
            ("东方财富 push2his（HTTP 备用）", "http://push2his.eastmoney.com" + em_q,
             parse_em, 1, 8),
            ("东方财富 33 节点（前复权日K）", "https://33.push2his.eastmoney.com" + em_q,
             parse_em, 1, 8),
            ("腾讯 fqkline（前复权日K）", tx_fq, parse_tx, 2, 10),
            ("腾讯 kline（不复权日K）", tx_raw, parse_tx, 1, 8),
        )
        used = {}

        def produce():
            failures = []
            for label, url, parser, retries, timeout in sources:
                try:
                    data, fq = parser(http_get(url, timeout=timeout, retries=retries))
                    data = [x for x in data if x.get("date") and (x.get("close") or 0) > 0]
                    if len(data) < 60:
                        failures.append(f"{label}：仅 {len(data)} 根有效 K 线")
                        continue
                    used.update({"name": label, "url": url, "fq": fq,
                                 "failures": failures})
                    return data[-days:] if len(data) > days else data
                except Exception as e:      # noqa: BLE001 - 单源失败继续下一源
                    failures.append(f"{label}：{type(e).__name__}: {e}")
            # 全部失败时抛异常：_cached 不会把空结果写进 30 分钟缓存，下次会立即重试
            raise DataError("全部日K数据源不可用 → " + "；".join(failures[-4:]))

        # 换 key 可绕开旧版可能已落盘的空 K 线缓存
        data = self._cached(f"kline-v3-{self.symbol}-{days}", 30, produce)
        if used:
            name = used["name"]
            if used["failures"]:
                self.warnings.append("日K主源不可用，已回退 " + name +
                                     "（" + "；".join(used["failures"][:2]) + "）")
            if not used["fq"]:
                self.warnings.append("日K为不复权数据（腾讯回退口径），除权缺口可能影响长周期指标")
        else:
            name = "日K缓存（30 分钟内复用）"
        self._mark(name, used.get("url") or tx_fq,
                   data[-1]["date"] if data else None)
        return data

    # ---- 历史每日估值 ----
    def valuation_history(self, years=5):
        start = (now_cn() - dt.timedelta(days=int(365.25 * years) + 5)).strftime("%Y-%m-%d")
        cols = "SECURITY_CODE,TRADE_DATE,CLOSE_PRICE,PE_TTM,PE_LAR,PB_MRQ,PS_TTM,TOTAL_MARKET_CAP"
        flt = urllib.parse.quote(f'(SECURITY_CODE="{self.symbol}")(TRADE_DATE>=\'{start}\')')
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               f"reportName=RPT_VALUEANALYSIS_DET&columns={cols}&filter={flt}"
               "&pageNumber=1&pageSize=2000&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB")

        def produce():
            j = json.loads(http_get(url, timeout=25))
            rows = ((j.get("result") or {}).get("data") or [])
            out = []
            for r in rows:
                out.append({
                    "date": (r.get("TRADE_DATE") or "")[:10],
                    "close": num(r.get("CLOSE_PRICE")),
                    "pe_ttm": num(r.get("PE_TTM")),
                    "pe_lar": num(r.get("PE_LAR")),
                    "pb": num(r.get("PB_MRQ")),
                    "ps": num(r.get("PS_TTM")),
                    "mv": num(r.get("TOTAL_MARKET_CAP")),
                })
            return list(reversed(out))   # 转为时间升序

        data = self._cached("val-" + url, 720, produce)
        self._mark("东方财富 datacenter（历史每日 PE_TTM/PB/PS）", url,
                   data[-1]["date"] if data else None)
        return data

    # ---- 季度财报摘要 ----
    def financials(self, limit=16):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               "reportName=RPT_LICO_FN_CPD&columns=ALL"
               f"&filter={urllib.parse.quote(f'(SECURITY_CODE=\"{self.symbol}\")')}"
               f"&pageNumber=1&pageSize={limit}&sortColumns=REPORTDATE&sortTypes=-1"
               "&source=WEB&client=WEB")

        def produce():
            j = json.loads(http_get(url, timeout=20))
            rows = ((j.get("result") or {}).get("data") or [])
            out = []
            for r in rows:
                out.append({
                    "date": (r.get("REPORTDATE") or "")[:10],
                    "notice": (r.get("NOTICE_DATE") or "")[:10],
                    "label": r.get("DATATYPE") or "",
                    "eps": num(r.get("BASIC_EPS")),
                    "revenue": num(r.get("TOTAL_OPERATE_INCOME")),
                    "net_profit": num(r.get("PARENT_NETPROFIT")),
                    "roe": num(r.get("WEIGHTAVG_ROE")),
                    "rev_yoy": num(r.get("YSTZ")),
                    "np_yoy": num(r.get("SJLTZ")),
                    "gross_margin": num(r.get("XSMLL")),
                    "bps": num(r.get("BPS")),
                    "ocf_ps": num(r.get("MGJYXJJE")),
                })
            return list(reversed([r for r in out if r["date"]]))

        data = self._cached("fin-" + url, 720, produce)
        self._mark("东方财富 datacenter（季度财报摘要）", url,
                   data[-1]["date"] if data else None)
        return data

    # ---- 资产负债表 ----
    def balance(self, limit=10):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               "reportName=RPT_DMSK_FN_BALANCE&columns=ALL"
               f"&filter={urllib.parse.quote(f'(SECURITY_CODE=\"{self.symbol}\")')}"
               f"&pageNumber=1&pageSize={limit}&sortColumns=REPORT_DATE&sortTypes=-1"
               "&source=WEB&client=WEB")

        def pick(d, *keys):
            for k in keys:
                if num(d.get(k)) is not None:
                    return num(d.get(k))
            return None

        def produce():
            j = json.loads(http_get(url, timeout=20))
            rows = ((j.get("result") or {}).get("data") or [])
            out = []
            for r in rows:
                out.append({
                    "date": (r.get("REPORT_DATE") or "")[:10],
                    "total_assets": pick(r, "TOTAL_ASSETS"),
                    "total_liab": pick(r, "TOTAL_LIABILITIES", "TOTAL_LIABILITY", "TOTAL_LIAB"),
                    "debt_ratio": pick(r, "DEBT_ASSET_RATIO", "TOTAL_LIABILITIES_RATIO", "DEBT_RATIO"),
                    "inventory": pick(r, "INVENTORY"),
                    "receivable": pick(r, "ACCOUNTS_RECE"),
                    "cash": pick(r, "MONETARYFUNDS"),
                })
            return list(reversed([r for r in out if r["date"]]))

        data = self._cached("bal-" + url, 720, produce)
        self._mark("东方财富 datacenter（资产负债表）", url,
                   data[-1]["date"] if data else None)
        return data

    # ---- 每日主力资金流（东财多节点冗余）----
    # 云服务器上常见「push2his 连接被远端直接关闭」，单一数据源会让全部标的的
    # 资金维度一起失效。这里按「主 HTTPS → HTTP → 数字节点」逐级降级；任一级
    # 拿到 >=5 条有效记录即采用，并把实际来源写进第七节数据源清单。
    def fundflow(self, lmt=60):
        q = (f"/api/qt/stock/fflow/daykline/get?secid={self.secid}"
             f"&fields1=f1,f2,f3,f7"
             f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
             f"&klt=101&lmt={lmt}")

        def parse(raw):
            j = json.loads(raw)
            lines = ((j.get("data") or {}).get("klines") or [])
            out = []
            for line in lines:
                p = line.split(",")
                if len(p) < 13:
                    continue
                out.append({
                    "date": p[0],
                    "main_net": num(p[1]),      # 主力 = 大单 + 超大单
                    "small_net": num(p[2]),
                    "mid_net": num(p[3]),
                    "big_net": num(p[4]),
                    "jumbo_net": num(p[5]),
                    "main_pct": num(p[6]),
                    "close": num(p[11]),
                    "pct": num(p[12]),
                })
            return [x for x in out if x["date"] and x.get("main_net") is not None]

        sources = (
            ("东方财富 push2his（每日主力资金流）", "https://push2his.eastmoney.com" + q, 2, 12),
            ("东方财富 push2his（HTTP 备用）", "http://push2his.eastmoney.com" + q, 1, 8),
            ("东方财富 33 节点（每日主力资金流）", "https://33.push2his.eastmoney.com" + q, 1, 8),
            ("东方财富 82 节点（每日主力资金流）", "https://82.push2his.eastmoney.com" + q, 1, 8),
            ("东方财富 46 节点（每日主力资金流）", "https://46.push2his.eastmoney.com" + q, 1, 8),
        )
        used = {}

        def produce():
            failures = []
            for label, url, retries, timeout in sources:
                try:
                    data = parse(http_get(url, timeout=timeout, retries=retries))
                    if len(data) < 5:
                        failures.append(f"{label}：仅 {len(data)} 条有效记录")
                        continue
                    used.update({"name": label, "url": url, "failures": failures})
                    return data
                except Exception as e:      # noqa: BLE001 - 单源失败继续下一源
                    failures.append(f"{label}：{type(e).__name__}: {e}")
            # 全失败抛异常：_cached 不会把空结果写进 20 分钟缓存，下次立即重试
            raise DataError("全部资金流数据源不可用 → " + "；".join(failures[-4:]))

        # 换 key 可绕开旧版可能已落盘的空资金流缓存
        data = self._cached(f"flow-v3-{self.symbol}-{lmt}", 20, produce)
        if used:
            if used["failures"]:
                self.warnings.append("资金流主源不可用，已回退 " + used["name"] +
                                     "（" + "；".join(used["failures"][:2]) + "）")
            name = used["name"]
        else:
            name = "每日资金流缓存（20 分钟内复用）"
        self._mark(name, used.get("url") or ("https://push2his.eastmoney.com" + q),
                   data[-1]["date"] if data else None)
        return data


# ----------------------------------------------------------------------------
# 技术指标（纯 Python，无第三方依赖）
# ----------------------------------------------------------------------------
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema_series(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_series(closes, n):
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n

    def calc(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[n] = calc(ag, al)
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = calc(ag, al)
    return out


def kdj_series(highs, lows, closes, n=9):
    k, d = 50.0, 50.0
    out = []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - n + 1):i + 1])
        hi = max(highs[max(0, i - n + 1):i + 1])
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100
        k = 2.0 / 3.0 * k + 1.0 / 3.0 * rsv
        d = 2.0 / 3.0 * d + 1.0 / 3.0 * k
        out.append((k, d, 3 * k - 2 * d))
    return out


def indicators_at(kl, offset=0):
    """offset=0 取最新；offset=-1 取前一交易日（用于金叉/死叉判定）。"""
    s = kl[:len(kl) + offset] if offset < 0 else kl
    if len(s) < 30:
        return {}
    closes = [x["close"] for x in s]
    highs = [x["high"] for x in s]
    lows = [x["low"] for x in s]
    vols = [x["volume"] or 0 for x in s]
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema_series(dif, 9)
    hist = [2 * (a - b) for a, b in zip(dif, dea)]
    kdj = kdj_series(highs, lows, closes)
    r6, r12, r24 = rsi_series(closes, 6), rsi_series(closes, 12), rsi_series(closes, 24)
    seg = closes[-20:]
    mid = sum(seg) / 20
    sd = statistics.pstdev(seg)
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, len(closes))]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    obv = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += vols[i]
        elif closes[i] < closes[i - 1]:
            obv -= vols[i]
    return {
        "date": s[-1]["date"], "close": closes[-1], "prev_close": closes[-2],
        "ma5": sma(closes, 5), "ma10": sma(closes, 10), "ma20": sma(closes, 20),
        "ma60": sma(closes, 60), "ma120": sma(closes, 120), "ma250": sma(closes, 250),
        "dif": dif[-1], "dea": dea[-1], "macd_hist": hist[-1],
        "macd_hist_prev": hist[-2] if len(hist) > 1 else None,
        "rsi6": r6[-1], "rsi12": r12[-1], "rsi24": r24[-1],
        "k": kdj[-1][0], "d": kdj[-1][1], "j": kdj[-1][2],
        "boll_upper": mid + 2 * sd, "boll_mid": mid, "boll_lower": mid - 2 * sd,
        "atr14": sum(trs[-14:]) / 14 if len(trs) >= 14 else None,
        "vol_ma5": sma(vols, 5), "vol_ratio_calc": safe_div(vols[-1], sma(vols, 5)),
        "volatility_annual": statistics.pstdev(rets[-60:]) * math.sqrt(244) * 100
                             if len(rets) >= 20 else None,
        "high20": max(highs[-20:]), "low20": min(lows[-20:]),
        "high60": max(highs[-60:]) if len(highs) >= 60 else max(highs),
        "low60": min(lows[-60:]) if len(lows) >= 60 else min(lows),
        "obv": obv,
        "obv_prev": obv - (vols[-1] if closes[-1] > closes[-2] else
                           (-vols[-1] if closes[-1] < closes[-2] else 0)),
    }


def single_quarters(fins):
    """把累计口径财报差分成单季，并计算单季同比（用于识别业绩拐点）。"""
    groups = {}
    for r in fins:
        try:
            y = int(r["date"][:4])
            m = int(r["date"][5:7])
        except (ValueError, IndexError):
            continue
        groups.setdefault(y, []).append((m, r))
    singles = {}
    for y, items in groups.items():
        prev_rev = prev_np = 0.0
        for m, r in sorted(items, key=lambda x: x[0]):
            q = {3: 1, 6: 2, 9: 3, 12: 4}.get(m)
            if not q or r["revenue"] is None or r["net_profit"] is None:
                continue
            singles[(y, q)] = {
                "year": y, "q": q, "date": r["date"],
                "rev": r["revenue"] - prev_rev,
                "np": r["net_profit"] - prev_np,
                "margin": r["gross_margin"],
            }
            prev_rev, prev_np = r["revenue"], r["net_profit"]
    out = []
    for key in sorted(singles):
        y, q = key
        v = dict(singles[key])
        prev = singles.get((y - 1, q))
        v["rev_yoy"] = (v["rev"] / abs(prev["rev"]) - 1) * 100 if prev and prev["rev"] else None
        v["np_yoy"] = (v["np"] / abs(prev["np"]) - 1) * 100 if prev and prev["np"] else None
        out.append(v)
    return out


# ----------------------------------------------------------------------------
# 五维评分引擎
# ----------------------------------------------------------------------------
def score_valuation(vs):
    """估值维：PE / PB / PS 的 5 年历史分位越低越有利。"""
    detail, s = [], 0.0
    pe_pct, pb_pct, ps_pct = vs.get("pe_pct"), vs.get("pb_pct"), vs.get("ps_pct")
    if pe_pct is not None:
        v = (0.5 - pe_pct / 100) * 130
        s += v
        detail.append(f"PE-TTM 处于 5 年 {fmt(pe_pct,1)}% 分位（{fmt(vs.get('pe_ttm'),2,'x')}）→ {v:+.1f}")
    if pb_pct is not None:
        v = (0.5 - pb_pct / 100) * 45
        s += v
        detail.append(f"PB 处于 5 年 {fmt(pb_pct,1)}% 分位 → {v:+.1f}")
    if ps_pct is not None:
        v = (0.5 - ps_pct / 100) * 25
        s += v
        detail.append(f"PS-TTM 处于 5 年 {fmt(ps_pct,1)}% 分位 → {v:+.1f}")
    peg = vs.get("peg")
    if peg is not None and peg > 0:
        v = 20.0 if peg < 1 else (8.0 if peg < 1.5 else (-8.0 if peg > 3 else 0.0))
        s += v
        detail.append(f"PEG={peg:.2f} → {v:+.1f}")
    else:
        detail.append("PEG 不适用（净利同比为负或无有效预测增速），不计分")
    return clamp(s), detail


def score_growth(fins, singles):
    """成长质量维：累计同比 / 单季同比拐点 / ROE / 毛利率 / 现金含量。"""
    if not fins:
        return 0.0, ["财报数据缺失，本维度不计分"]
    f = fins[-1]
    detail, s = [], 0.0
    np_yoy, rev_yoy = f.get("np_yoy"), f.get("rev_yoy")
    if np_yoy is not None:
        v = 25 if np_yoy > 20 else (10 if np_yoy > 0 else (-10 if np_yoy > -20 else -30))
        s += v
        detail.append(f"最新累计归母净利同比 {np_yoy:+.2f}% → {v:+.1f}")
    if rev_yoy is not None:
        v = 15 if rev_yoy > 15 else (5 if rev_yoy > 0 else -10)
        s += v
        detail.append(f"最新累计营收同比 {rev_yoy:+.2f}% → {v:+.1f}")
    if singles:
        sq = singles[-1]
        yoy = sq.get("np_yoy")
        if yoy is not None:
            v = 25 if yoy > 20 else (12 if yoy > 0 else (-10 if yoy > -20 else -25))
            s += v
            detail.append(f"{sq['year']}Q{sq['q']} 单季净利同比 {yoy:+.2f}% → {v:+.1f}")
            if len(singles) > 1:
                prev_yoy = singles[-2].get("np_yoy")
                if prev_yoy is not None and prev_yoy < 0 <= yoy:
                    s += 15
                    detail.append("单季净利同比由负转正（业绩拐点）→ +15.0")
    roe = f.get("roe")
    if roe is not None:
        q = {3: 1, 6: 2, 9: 3, 12: 4}.get(int(f["date"][5:7]), 4)
        annual = roe * 4 / q
        v = 20 if annual > 15 else (10 if annual > 8 else (-5 if annual > 0 else -15))
        s += v
        detail.append(f"加权 ROE {roe:.2f}%（年化约 {annual:.1f}%）→ {v:+.1f}")
    gm = f.get("gross_margin")
    prev_gm = None
    for r in reversed(fins[:-1]):
        if r["date"][5:7] == f["date"][5:7]:
            prev_gm = r.get("gross_margin")
            break
    if gm is not None and prev_gm is not None:
        delta = gm - prev_gm
        v = 10 if delta > 1 else (3 if delta > 0 else (-10 if delta < -1 else 0))
        s += v
        detail.append(f"毛利率同比 {delta:+.2f}pct（{gm:.2f}%）→ {v:+.1f}")
    ocf, eps = f.get("ocf_ps"), f.get("eps")
    ratio = safe_div(ocf, eps)
    if ratio is not None:
        v = 10 if ratio > 1.5 else (5 if ratio > 1 else (-10 if ratio < 0.5 else 0))
        s += v
        detail.append(f"每股经营现金流 / EPS = {ratio:.2f} → {v:+.1f}")
    return clamp(s), detail


def score_trend(ind, q):
    """趋势技术维：均线结构优先，其次动能，最后超卖反弹。"""
    if not ind:
        return 0.0, ["K 线数据不足，本维度不计分"]
    detail, s = [], 0.0
    p = ind["close"]
    mas = [ind.get(k) for k in ("ma5", "ma10", "ma20", "ma60", "ma120", "ma250")]
    valid = [x for x in mas if x is not None]
    if len(valid) >= 4:
        if all(valid[i] >= valid[i + 1] for i in range(len(valid) - 1)) and p > valid[0]:
            s += 25
            detail.append("均线完全多头排列 → +25.0")
        elif all(valid[i] <= valid[i + 1] for i in range(len(valid) - 1)) and p < valid[0]:
            s -= 25
            detail.append("均线完全空头排列 → -25.0")
        else:
            detail.append("均线交织（无明确排列）→ 0.0")
    for key, w, label in (("ma20", 10, "MA20"), ("ma60", 8, "MA60"), ("ma250", 8, "MA250")):
        v = ind.get(key)
        if v:
            delta = 1 if p > v else -1
            s += w * delta
            detail.append(f"价格{'高于' if delta > 0 else '低于'} {label}（{v:.2f}）→ {w*delta:+d}")
    dif, dea, hist = ind.get("dif"), ind.get("dea"), ind.get("macd_hist")
    if None not in (dif, dea, hist):
        v = 15 if (hist > 0 and dif > dea) else (-15 if (hist < 0 and dif < dea) else 0)
        s += v
        detail.append(f"MACD 柱 {hist:+.3f} / DIF {dif:+.3f} / DEA {dea:+.3f} → {v:+.1f}")
        hp = ind.get("macd_hist_prev")
        if hp is not None:
            v2 = 5 if hist > hp else -5
            s += v2
            detail.append(f"MACD 柱{'走强' if hist > hp else '走弱'}（前值 {hp:+.3f}）→ {v2:+d}")
    r6 = ind.get("rsi6")
    if r6 is not None:
        v = 10 if r6 < 30 else (-10 if r6 > 70 else 0)
        s += v
        detail.append(f"RSI6={r6:.1f}{'（超卖反弹需求）' if v > 0 else ''} → {v:+d}")
    j = ind.get("j")
    if j is not None:
        v = 8 if j < 0 else (-8 if j > 100 else 0)
        s += v
        detail.append(f"KDJ-J={j:.1f} → {v:+d}")
    up, lo = ind.get("boll_upper"), ind.get("boll_lower")
    if up and lo and up > lo:
        pos = (p - lo) / (up - lo)
        v = 5 if pos < 0.2 else (-5 if pos > 0.8 else 0)
        s += v
        detail.append(f"布林带位置 {pos*100:.0f}% → {v:+d}")
    h52, l52 = q.get("high52"), q.get("low52")
    if h52 and l52 and h52 > l52:
        pos52 = (p - l52) / (h52 - l52)
        v = -10 if pos52 < 0.2 else (8 if pos52 > 0.8 else 0)
        s += v
        detail.append(f"处于 52 周区间 {pos52*100:.0f}% 位置 → {v:+d}")
    vr = ind.get("vol_ratio_calc")
    chg = q.get("pct") or 0
    if vr:
        v = 8 if (vr > 1.3 and chg > 0) else (-8 if (vr > 1.3 and chg < 0) else (3 if vr < 0.8 and chg < 0 else 0))
        s += v
        detail.append(f"量比(自算) {vr:.2f} + 当日 {chg:+.2f}% 量价配合 → {v:+d}")
    return clamp(s), detail


def score_fund(flow, q):
    """资金情绪维：主力净流入相对流通市值，避免用绝对金额造成规模偏差。"""
    if not flow:
        return 0.0, ["资金流数据缺失，本维度不计分"]
    mv = (q.get("float_mv_yi") or 0) * 1e8
    detail, s = [], 0.0
    for n, weight, label in ((5, 30, "近5日"), (20, 20, "近20日")):
        seg = [x["main_net"] for x in flow[-n:] if x.get("main_net") is not None]
        if not seg or not mv:
            continue
        val = sum(seg)
        r = val / mv * 100
        v = weight if r > 0.5 else (weight / 3 if r > 0 else
                                   (-weight / 3 if r > -0.5 else -weight))
        s += v
        detail.append(f"{label}主力净流入 {val/1e8:+.2f}亿（占流通市值 {r:+.3f}%）→ {v:+.1f}")
    vr = q.get("volume_ratio")
    pct = q.get("pct") or 0
    if vr:
        v = 10 if (vr > 1.5 and pct > 0) else (-10 if (vr > 1.5 and pct < 0) else 0)
        s += v
        detail.append(f"实时量比 {vr:.2f} + 涨跌 {pct:+.2f}% → {v:+d}")
    c20 = q.get("chg_20d")
    if c20 is not None:
        v = 10 if c20 > 5 else (-10 if c20 < -10 else 0)
        s += v
        detail.append(f"近20日涨跌 {c20:+.2f}% → {v:+d}")
    return clamp(s), detail


def score_risk(bal, fins, ind, q):
    """风险维：起始 0 分，只扣分或小幅加分，代表组合下行风险。"""
    detail, s = [], 0.0
    if bal:
        b = bal[-1]
        dr = b.get("debt_ratio")
        if dr is None and b.get("total_liab") and b.get("total_assets"):
            dr = b["total_liab"] / b["total_assets"] * 100
        if dr is not None:
            v = -20 if dr > 70 else (-10 if dr > 60 else 10)
            s += v
            detail.append(f"资产负债率 {dr:.2f}%（{b['date']}）→ {v:+d}")
        inv = b.get("inventory")
        base = None
        for r in reversed(bal[:-1]):
            if r["date"].endswith("-12-31"):
                base = r.get("inventory")
                break
        if inv is not None and base:
            chg = (inv / base - 1) * 100
            v = -15 if chg > 40 else (-8 if chg > 20 else 0)
            s += v
            detail.append(f"存货较上年末 {chg:+.1f}% → {v:+d}")
    if fins:
        yoy = fins[-1].get("np_yoy")
        if yoy is not None and yoy < 0:
            s -= 10
            detail.append(f"累计归母净利同比为负（{yoy:+.2f}%）→ -10")
    h52, p = q.get("high52"), q.get("price")
    if h52 and p:
        dd = (p / h52 - 1) * 100
        v = -10 if dd < -25 else (5 if dd > -5 else 0)
        s += v
        detail.append(f"距 52 周高点回撤 {dd:.1f}% → {v:+d}")
    vol = ind.get("volatility_annual") if ind else None
    if vol is not None:
        v = -10 if vol > 45 else (5 if vol < 25 else 0)
        s += v
        detail.append(f"近 60 日年化波动率 {vol:.1f}% → {v:+d}")
    return clamp(s), detail


def decide(total, weights, trend_score, fund_score, cfg):
    """评分 → 信号。趋势与资金同时转弱时启动闸门：不允许输出买入。"""
    if total >= 35:
        sig, tag = "买入", "buy"
    elif total >= 15:
        sig, tag = "偏买入（小仓试探）", "buy"
    elif total > -15:
        sig, tag = "观望 / 持有", "hold"
    elif total > -35:
        sig, tag = "减仓", "sell"
    else:
        sig, tag = "卖出 / 回避", "sell"
    gate = False
    if tag == "buy" and trend_score < -20 and fund_score < -20:
        sig, tag, gate = "观望（等右侧确认）", "hold", True
    profile = cfg.get("risk_profile", "balanced")
    if profile == "conservative" and tag == "buy":
        sig += "（保守型：仅试错仓）"
    return {"signal": sig, "tag": tag, "gated": gate}


def build_plan(ctx):
    """条件 → 动作 → 风险提示，不做点位预测。"""
    q, ind, vs = ctx["quote"], ctx["ind"], ctx["val_stat"]
    p = q.get("price")
    ma20, ma60 = ind.get("ma20"), ind.get("ma60")
    low20, low52 = ind.get("low20"), q.get("low52")
    eps_ttm = safe_div(p, q.get("pe_ttm"))
    pe_q = vs.get("pe_quantiles") or {}
    scen = []
    for label, key in (("悲观 25% 分位", "q25"), ("中性 50% 分位", "q50"), ("乐观 75% 分位", "q75")):
        pe_v = pe_q.get(key)
        if pe_v and eps_ttm:
            scen.append({"label": label, "pe": pe_v, "price": pe_v * eps_ttm})
    stop_tech = min([x for x in (low20, low52) if x] or [None])
    stop_pct = (q.get("cost_price") or p or 0)
    stop_amt = stop_pct * (1 + (ctx["cfg"]["alerts"]["stop_loss_pct"] or -8) / 100) if stop_pct else None
    return {
        "entry": f"收盘价站上 MA20（{fmt(ma20)} 元）且当日成交量 ≥ 5 日均量 ×1.2，"
                 f"同时 MACD 柱由负转正 —— 三条件同时满足才开试错仓 10-20%",
        "add": f"站稳 MA60（{fmt(ma60)} 元）且主力资金 5 日净流入转正 → 加仓 20-30%；"
               f"突破近 60 日高点（{fmt(ind.get('high60'))} 元）→ 再加 20-30%",
        "stop": f"跌破近 20 日低点 {fmt(stop_tech)} 元，或持仓成本回撤 8%（{fmt(stop_amt)} 元），"
                f"二者取其近者，收盘确认后执行",
        "invalidate": "主力 20 日净流出继续扩大 / 单季归母净利同比再度转负 / "
                      "放量跌破 52 周低点 {0} 元".format(fmt(low52)),
        "scenarios": scen,
        "eps_ttm": eps_ttm,
        "note": "情景价 = 历史 PE 分位 × 当前 EPS(TTM)，是估值区间推演，不是目标价预测；"
                "EPS 不变假设下才成立，盈利下修时整条区间同步下移。",
    }


def analyze(ctx):
    cfg, q = ctx["cfg"], ctx["quote"]
    years = int(cfg.get("valuation_years", 5))
    val = ctx["valuation"]
    cutoff = (now_cn() - dt.timedelta(days=365.25 * years)).strftime("%Y-%m-%d")
    pe_series = [x["pe_ttm"] for x in val if x["date"] >= cutoff]
    pb_series = [x["pb"] for x in val if x["date"] >= cutoff]
    ps_series = [x["ps"] for x in val if x["date"] >= cutoff]
    pe3 = [x["pe_ttm"] for x in val if x["date"] >=
           (now_cn() - dt.timedelta(days=365.25 * 3)).strftime("%Y-%m-%d")]
    pe_pct, pe_med, pe_sd = percentile_rank(pe_series, q.get("pe_ttm"))
    pb_pct, pb_med, _ = percentile_rank(pb_series, q.get("pb"))
    ps_pct, ps_med, _ = percentile_rank(ps_series, None)
    if ps_pct is None and ps_series:
        last_ps = next((x["ps"] for x in reversed(val) if x["ps"]), None)
        ps_pct = percentile_rank(ps_series, last_ps)[0]
    pe3_pct = percentile_rank(pe3, q.get("pe_ttm"))[0]
    np_yoy = ctx["financials"][-1].get("np_yoy") if ctx["financials"] else None
    peg = safe_div(q.get("pe_ttm"), np_yoy) if (np_yoy and np_yoy > 0) else None
    vs = {
        "years": years, "samples": len(pe_series),
        "pe_ttm": q.get("pe_ttm"), "pe_pct": pe_pct, "pe_pct3": pe3_pct,
        "pe_med": pe_med, "pe_sd": pe_sd,
        "pb_pct": pb_pct, "pb_med": pb_med, "ps_pct": ps_pct,
        "peg": peg,
        "pe_quantiles": {"q25": quantile(pe_series, 0.25), "q50": quantile(pe_series, 0.5),
                         "q75": quantile(pe_series, 0.75)},
        "pe_min": min(pe_series) if pe_series else None,
        "pe_max": max(pe_series) if pe_series else None,
        "fwd_pe": safe_div(q.get("price"), cfg.get("analyst_eps_forecast")),
    }
    ctx["val_stat"] = vs

    kl = ctx["kline"]
    ind = indicators_at(kl, 0)
    ind_prev = indicators_at(kl, -1)
    ctx["ind"], ctx["ind_prev"] = ind, ind_prev
    if q.get("high52") and q.get("low52") and q.get("price"):
        ctx["pos52"] = (q["price"] - q["low52"]) / (q["high52"] - q["low52"]) * 100
    else:
        ctx["pos52"] = None

    singles = single_quarters(ctx["financials"])
    ctx["singles"] = singles
    sv, dv = score_valuation(vs)
    sg, dg = score_growth(ctx["financials"], singles)
    st, dt_ = score_trend(ind, q)
    sf, df = score_fund(ctx["flow"], q)
    sr, dr = score_risk(ctx["balance"], ctx["financials"], ind, q)
    w = cfg["weights"]
    scores = {
        "valuation": {"score": sv, "weight": w["valuation"], "detail": dv, "label": "估值"},
        "growth": {"score": sg, "weight": w["growth"], "detail": dg, "label": "成长质量"},
        "trend": {"score": st, "weight": w["trend"], "detail": dt_, "label": "趋势技术"},
        "fund": {"score": sf, "weight": w["fund"], "detail": df, "label": "资金情绪"},
        "risk": {"score": sr, "weight": w["risk"], "detail": dr, "label": "风险"},
    }
    for v in scores.values():
        v["weighted"] = v["score"] * v["weight"]
    total = sum(v["weighted"] for v in scores.values())
    ctx["scores"] = scores
    ctx["total"] = total
    ctx["decision"] = decide(total, w, st, sf, cfg)
    ctx["plan"] = build_plan(ctx)
    return ctx


# ----------------------------------------------------------------------------
# 结论文本
# ----------------------------------------------------------------------------
def narrative(ctx):
    q, vs, ind, sc = ctx["quote"], ctx["val_stat"], ctx["ind"], ctx["scores"]
    tag = ctx["decision"]["tag"]
    bits = []
    bits.append(
        f"综合评分 {ctx['total']:+.1f} 分（-100~+100），结论：{ctx['decision']['signal']}。"
    )
    bits.append(
        f"截至 {q.get('asof','N/A')}（{ctx['state'][0]}），{q.get('name','')} 报 {fmt(q.get('price'))} 元，"
        f"当日 {fmt(q.get('pct'),2,'%')}，PE-TTM {fmt(q.get('pe_ttm'),2,'x')}、PB {fmt(q.get('pb'),2,'x')}。"
    )
    if vs.get("pe_pct") is not None:
        bits.append(
            f"PE-TTM 处于近 {vs['years']} 年 {vs['pe_pct']:.1f}% 分位"
            f"（中位数 {fmt(vs['pe_med'],2,'x')}），估值维度 {sc['valuation']['score']:+.1f} 分。"
        )
    if ctx["singles"]:
        sq = ctx["singles"][-1]
        if sq.get("np_yoy") is not None:
            bits.append(
                f"最新单季（{sq['year']}Q{sq['q']}）归母净利同比 {sq['np_yoy']:+.2f}%，"
                f"成长质量维度 {sc['growth']['score']:+.1f} 分。"
            )
    bits.append(
        f"但趋势技术 {sc['trend']['score']:+.1f} 分、资金情绪 {sc['fund']['score']:+.1f} 分，"
        f"风险维度 {sc['risk']['score']:+.1f} 分。"
    )
    if tag == "hold":
        bits.append(
            "核心矛盾是「基本面边际改善、股价与资金仍在左侧」：估值不贵、单季利润已现拐点，"
            "但均线空头排列、主力资金持续净流出，右侧确认前不宜追买，也不必在超卖区恐慌卖出。"
            "动作上以持有观察为主，等待量价确认，跌破关键支撑再减仓。"
        )
    elif tag == "buy":
        bits.append("估值、成长与趋势资金形成共振，可按分仓路径逐步建立头寸，并严格执行止损。")
    else:
        bits.append("估值与趋势资金同时转弱，优先控制回撤，按计划减仓并等待新的右侧信号。")
    if ctx["decision"]["gated"]:
        bits.append("【趋势闸门】趋势与资金维度同时低于 -20 分，已强制把买入信号降级为观望。")
    return "".join(bits)


# ----------------------------------------------------------------------------
# HTML 研报
# ----------------------------------------------------------------------------
def render_report(ctx):
    q, vs, ind, sc = ctx["quote"], ctx["val_stat"], ctx["ind"], ctx["scores"]
    kl = ctx["kline"][-120:]
    val = ctx["valuation"]
    step = max(1, len(val) // 400)
    val_s = val[::step]
    flow = ctx["flow"][-30:]
    singles = ctx["singles"][-8:]

    payload = {
        "dates": [x["date"] for x in kl],
        "ohlc": [[x["open"], x["close"], x["low"], x["high"]] for x in kl],
        "vols": [[i, x["volume"], 1 if (x["close"] or 0) >= (x["open"] or 0) else -1]
                 for i, x in enumerate(kl)],
        "ma5": [round(sma([y["close"] for y in kl[:i + 1]], 5) or 0, 2) if i >= 4 else None
                for i in range(len(kl))],
        "ma20": [round(sma([y["close"] for y in kl[:i + 1]], 20) or 0, 2) if i >= 19 else None
                 for i in range(len(kl))],
        "ma60": [round(sma([y["close"] for y in kl[:i + 1]], 60) or 0, 2) if i >= 59 else None
                 for i in range(len(kl))],
        "peDates": [x["date"] for x in val_s],
        "peVals": [x["pe_ttm"] for x in val_s],
        "peQ": vs.get("pe_quantiles") or {},
        "flowDates": [x["date"] for x in flow],
        "flowMain": [round((x["main_net"] or 0) / 1e8, 3) for x in flow],
        "flowCum": None,
        "radar": [round(sc[k]["score"], 1) for k in ("valuation", "growth", "trend", "fund", "risk")],
        "qLabels": [f"{x['year']}Q{x['q']}" for x in singles],
        "qRev": [round(x["rev"] / 1e8, 1) for x in singles],
        "qNp": [round(x["np"] / 1e8, 1) for x in singles],
        "qNpYoy": [round(x["np_yoy"], 1) if x.get("np_yoy") is not None else None for x in singles],
    }
    cum, acc = [], 0.0
    for x in flow:
        acc += (x["main_net"] or 0) / 1e8
        cum.append(round(acc, 3))
    payload["flowCum"] = cum
    data_js = json.dumps(clean_json(payload), ensure_ascii=False, allow_nan=False)

    # 图表库万一加载失败时，每个图容器显示「本图对应的关键数值 + 该去哪张表查」，
    # 不再统一套用「点击表」这句（只有 K 线区才有图/表切换），也不留大片空白。
    radar_txt = " ｜ ".join(f"{v['label']} <b>{v['score']:+.1f}</b>" for v in sc.values())
    flow5 = sum((x["main_net"] or 0) for x in ctx["flow"][-5:]) / 1e8 if ctx["flow"] else None
    fallbacks = {
        "chart_kline": "K 线图未渲染。<b>点击本节上方「表」按钮</b>可查看近 40 日每日开 / 高 / 低 / 收与成交量的精确值；本页结论与数值不依赖图表。",
        "chart_vol": "成交量图未渲染，逐日成交量精确值见本节「表」视图。",
        "chart_pe": (f"PE-TTM 历史带未渲染。当前 <b>{fmt(vs.get('pe_ttm'), 2, 'x')}</b>，"
                     f"近 {vs['years']} 年分位 <b>{fmt(vs.get('pe_pct'), 1, '%')}</b>，"
                     f"中位数 {fmt(vs.get('pe_med'), 2, 'x')}；精确数值见「估值位置」表。"),
        "chart_flow": (f"资金流图未渲染。近 5 日主力净流入 <b>{fmt(flow5, 2, ' 亿元')}</b>；"
                       "逐日口径见评分明细「资金情绪」依据。"),
        "chart_radar": f"雷达图未渲染。五维评分：{radar_txt}。逐项打分依据见「评分模型明细」表。",
        "chart_growth": "单季趋势图未渲染。各季营收 / 归母净利 / 同比的精确数值见本节上方「单季拆分」表。",
    }
    fallbacks_js = json.dumps(clean_json(fallbacks), ensure_ascii=False, allow_nan=False)

    def esc(x):
        return html_mod.escape(str(x if x is not None else "N/A"))

    tag = ctx["decision"]["tag"]
    tag_color = {"buy": "#c0392b", "hold": "#b7791f", "sell": "#1e7d4f"}[tag]
    total = ctx["total"]

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
        f'<div class="kpi-v">{esc(b)}<span>{esc(c)}</span></div></div>' for a, b, c in cards
    )

    score_rows = "".join(
        f"<tr><td><b>{esc(v['label'])}</b></td><td>{v['weight']*100:.0f}%</td>"
        f"<td class='{'pos' if v['score']>=0 else 'neg'}'>{v['score']:+.1f}</td>"
        f"<td class='{'pos' if v['weighted']>=0 else 'neg'}'>{v['weighted']:+.1f}</td>"
        f"<td class='small'>{'；'.join(esc(x) for x in v['detail'])}</td></tr>"
        for v in sc.values()
    )

    scen_rows = "".join(
        f"<tr><td>{esc(s['label'])}</td><td>{fmt(s['pe'])}x</td><td>{fmt(s['price'])} 元</td>"
        f"<td>{fmt((s['price']/q['price']-1)*100,1,'%') if q.get('price') else 'N/A'}</td></tr>"
        for s in ctx["plan"]["scenarios"]
    ) or "<tr><td colspan='4'>历史估值数据不足，无法生成情景区间</td></tr>"

    qrows = "".join(
        f"<tr><td>{esc(x['date'])} 单季</td><td>{fmt(x['rev']/1e8,1)} 亿</td>"
        f"<td>{fmt(x['np']/1e8,1)} 亿</td><td>{fmt(x.get('np_yoy'),1,'%')}</td></tr>"
        for x in singles
    )

    source_rows = "".join(
        f"<tr><td>{esc(s['name'])}</td><td class='small'>{esc(s['url'])}</td>"
        f"<td>{esc(s['time'])}</td></tr>" for s in ctx["sources"]
    )
    check_html = "".join(f"<li>{esc(x)}</li>" for x in ctx["cross_notes"])
    warn_html = "".join(f"<li>{esc(x)}</li>" for x in ctx["warnings"]) or "<li>无</li>"

    fin = ctx["financials"][-1] if ctx["financials"] else {}
    fin_rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in (
            ("最新报告期", fin.get("date")), ("披露日", fin.get("notice")),
            ("累计营业收入", fmt(fin.get("revenue") and fin["revenue"] / 1e8, 2, " 亿元")),
            ("累计归母净利润", fmt(fin.get("net_profit") and fin["net_profit"] / 1e8, 2, " 亿元")),
            ("营收同比", fmt(fin.get("rev_yoy"), 2, "%")),
            ("归母净利同比", fmt(fin.get("np_yoy"), 2, "%")),
            ("毛利率", fmt(fin.get("gross_margin"), 2, "%")),
            ("加权 ROE", fmt(fin.get("roe"), 2, "%")),
            ("基本 EPS", fmt(fin.get("eps"), 2, " 元")),
            ("每股经营现金流", fmt(fin.get("ocf_ps"), 2, " 元")),
        )
    )
    bal = ctx["balance"][-1] if ctx["balance"] else {}
    dr = bal.get("debt_ratio")
    if dr is None and bal.get("total_liab") and bal.get("total_assets"):
        dr = bal["total_liab"] / bal["total_assets"] * 100
    bal_rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in (
            ("报告期", bal.get("date")),
            ("资产负债率", fmt(dr, 2, "%")),
            ("存货", fmt(bal.get("inventory") and bal["inventory"] / 1e8, 1, " 亿元")),
            ("货币资金", fmt(bal.get("cash") and bal["cash"] / 1e8, 1, " 亿元")),
            ("应收账款", fmt(bal.get("receivable") and bal["receivable"] / 1e8, 1, " 亿元")),
        )
    )
    kl_rows = "".join(
        f"<tr><td>{esc(x['date'])}</td><td>{fmt(x['open'])}</td><td>{fmt(x['close'])}</td>"
        f"<td>{fmt(x['high'])}</td><td>{fmt(x['low'])}</td>"
        f"<td>{fmt(x['volume'],0)}</td><td>{fmt(x['pct'],2,'%')}</td></tr>"
        for x in reversed(kl[-40:])
    )

    tpl = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
__ECHARTS_LIB__
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b2330;--sub:#5b6675;--line:#e3e7ed;--red:#c0392b;--green:#1e7d4f;--amber:#b7791f;--blue:#2559a8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0 0 6px}h2{font-size:19px;margin:34px 0 14px;padding-left:11px;border-left:4px solid var(--blue)}
h3{font-size:15px;margin:20px 0 10px;color:var(--sub)}
.meta{color:var(--sub);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
.hero{display:flex;gap:26px;align-items:center;flex-wrap:wrap}
.score{font-size:56px;font-weight:750;line-height:1;color:__TAGCOLOR__}
.badge{display:inline-block;padding:5px 14px;border-radius:20px;color:#fff;font-weight:650;background:__TAGCOLOR__;font-size:15px}
.hero-txt{flex:1;min-width:280px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px}
.kpi{background:#fafbfc;border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.kpi-l{font-size:12px;color:var(--sub)}.kpi-v{font-size:21px;font-weight:680;margin-top:3px}.kpi-v span{font-size:12px;font-weight:400;color:var(--sub);margin-left:4px}
table{width:100%;border-collapse:collapse;font-size:13px;background:#fff}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f0f3f7;font-weight:620;color:#33404f}
td.pos,span.pos{color:var(--red);font-weight:640}td.neg,span.neg{color:var(--green);font-weight:640}
.small{font-size:12px;color:var(--sub);line-height:1.6}
.chart{width:100%;height:340px}.chart-sm{width:100%;height:230px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.btn{border:1px solid var(--line);background:#fff;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;color:var(--sub)}
.btn.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.plan li{margin:8px 0}.plan b{color:var(--blue)}
ul{margin:8px 0;padding-left:20px}.disc{background:#fff8f0;border:1px solid #f0dcc0;border-radius:8px;padding:16px;font-size:12.5px;color:#6b5535}
.foot{color:var(--sub);font-size:12px;margin-top:26px;text-align:center}
.chart-fallback{padding:14px 16px;background:#fafbfc;border:1px dashed var(--line);border-radius:8px;font-size:13px;color:var(--sub);line-height:1.9}
.chart-fallback b{color:var(--ink)}
</style>
</head>
<body><div class="wrap">
<h1>__TITLE__</h1>
<div class="meta">数据时点：__ASOF__ ｜ 市场状态：__STATE__ ｜ 报告生成：__GEN__ ｜ 标的：__NAME__（__CODE__.SZ，A 股，人民币计价）</div>

<div class="card hero">
  <div><div class="score">__TOTAL__</div><div class="small" style="text-align:center">综合评分 / ±100</div></div>
  <div class="hero-txt"><span class="badge">__SIGNAL__</span>
    <p style="margin:12px 0 0">__NARRATIVE__</p>
  </div>
</div>
<div class="kpis">__CARDS__</div>

<h2>一、评分模型明细（五维加权，可解释）</h2>
<div class="card"><table>
<tr><th>维度</th><th>权重</th><th>原始分</th><th>加权分</th><th>打分依据（逐项可追溯）</th></tr>
__SCOREROWS__
<tr><th>合计</th><th>100%</th><th>—</th><th class="__TOTALCLS__">__TOTAL__</th><th>评分 ∈ [-100, +100]；≥35 买入，15~35 偏买入，-15~15 观望，-35~-15 减仓，≤-35 卖出</th></tr>
</table>
<p class="small">趋势闸门：当趋势技术分 &lt; -20 且资金情绪分 &lt; -20 时，即便综合分达到买入阈值也强制降级为观望，避免在空头排列 + 主力持续流出时逆势抄底。本次是否触发：__GATE__。</p>
</div>

<div class="grid2">
  <div class="card"><h3>五维评分雷达</h3><div id="chart_radar" class="chart"></div></div>
  <div class="card"><h3>估值位置（PE-TTM 近 __YEARS__ 年）</h3>
    <table><tr><th>指标</th><th>数值</th><th>说明</th></tr>
    <tr><td>当前 PE-TTM</td><td>__PE__</td><td>腾讯行情，__ASOF__</td></tr>
    <tr><td>近 __YEARS__ 年分位</td><td>__PEPCT__</td><td>样本 __SAMPLES__ 个交易日</td></tr>
    <tr><td>近 3 年分位</td><td>__PEPCT3__</td><td>短周期敏感性校验</td></tr>
    <tr><td>近 __YEARS__ 年中位数</td><td>__PEMED__</td><td>估值中枢</td></tr>
    <tr><td>近 __YEARS__ 年区间</td><td>__PEMIN__ ~ __PEMAX__</td><td>最小 ~ 最大</td></tr>
    <tr><td>PB / 分位</td><td>__PB__ / __PBPCT__</td><td>市净率</td></tr>
    <tr><td>PS-TTM 分位</td><td>__PSPCT__</td><td>市销率</td></tr>
    <tr><td>PEG</td><td>__PEG__</td><td>净利同比为负时不适用</td></tr>
    <tr><td>前瞻 PE</td><td>__FWDPE__</td><td>需用户在 config 自填 EPS 预测，程序不编造</td></tr>
    </table>
  </div>
</div>

<h2>二、价格走势与技术面</h2>
<div class="card">
  <div style="margin-bottom:10px"><button class="btn on" id="btn_chart" onclick="toggleK(true)">图</button>
  <button class="btn" id="btn_table" onclick="toggleK(false)">表（近 40 日精确值）</button>
  <span class="small" style="margin-left:10px">「图 / 表」只切换本节的 K 线与成交量；点「表」看每日开高低收、成交量的精确数字</span></div>
  <div id="k_chart"><div id="chart_kline" class="chart"></div><div id="chart_vol" class="chart-sm"></div></div>
  <div id="k_table" style="display:none;max-height:420px;overflow:auto"><table>
  <tr><th>日期</th><th>开盘</th><th>收盘</th><th>最高</th><th>最低</th><th>成交量(手)</th><th>涨跌幅</th></tr>__KLROWS__</table></div>
  <p class="small">前复权日 K，来源：东方财富 push2his，最新交易日 __KLDATE__。A 股习惯：红涨绿跌。图表库已内嵌在本文件中，断网也能正常显示。</p>
</div>
<div class="card"><table><tr><th>技术指标</th><th>数值</th><th>技术指标</th><th>数值</th></tr>
<tr><td>MA5 / MA10 / MA20</td><td>__MA5__ / __MA10__ / __MA20__</td><td>MA60 / MA120 / MA250</td><td>__MA60__ / __MA120__ / __MA250__</td></tr>
<tr><td>MACD 柱 / DIF / DEA</td><td>__MACD__ / __DIF__ / __DEA__</td><td>RSI6 / RSI12 / RSI24</td><td>__RSI__</td></tr>
<tr><td>KDJ  K / D / J</td><td>__KDJ__</td><td>BOLL 上 / 中 / 下</td><td>__BOLL__</td></tr>
<tr><td>近 20 日高 / 低</td><td>__H20__ / __L20__</td><td>52 周区间位置</td><td>__POS52__</td></tr>
<tr><td>ATR14 / 年化波动率</td><td>__ATR__ / __VOLA__</td><td>量比（自算 / 行情商）</td><td>__VR__ / __VRQ__</td></tr>
</table><p class="small">指标由本地前复权日 K 自行计算，最新交易日 __KLDATE__；多指标交叉验证，单一超卖信号不作为买入依据。</p></div>

<h2>三、估值带与资金流</h2>
<div class="card"><h3>PE-TTM 历史带（虚线为 25% / 50% / 75% 分位）</h3><div id="chart_pe" class="chart"></div>
<p class="small">来源：东方财富 datacenter RPT_VALUEANALYSIS_DET，近 __YEARS__ 年每日 PE-TTM，__VSTART__ ~ __VEND__。</p></div>
<div class="card"><h3>主力资金净流入（亿元，近 30 交易日；主力 = 大单 + 超大单）</h3><div id="chart_flow" class="chart"></div>
<p class="small">来源：东方财富 push2his fflow/daykline，口径为当日主力净流入与区间累计，__FSTART__ ~ __FEND__。</p></div>

<h2>四、财务与业绩拐点</h2>
<div class="grid2">
  <div class="card"><h3>最新财报（累计口径）</h3><table>__FINROWS__</table>
  <p class="small">来源：东方财富 datacenter RPT_LICO_FN_CPD（原始披露为公司季报/年报，非一手原文，需核实公告）。</p></div>
  <div class="card"><h3>资产负债与风险项</h3><table>__BALROWS__</table>
  <p class="small">来源：东方财富 datacenter RPT_DMSK_FN_BALANCE；字段缺失时以资产/负债反推或直接标注 N/A。</p></div>
</div>
<div class="card"><h3>单季拆分（累计值差分，用于识别同比拐点）</h3><table>
<tr><th>单季</th><th>营业收入</th><th>归母净利润</th><th>净利同比</th></tr>__QROWS__</table>
<div id="chart_growth" class="chart-sm" style="margin-top:14px"></div>
<p class="small">来源：东方财富季度财报摘要，由程序按「本期累计 − 上期累计」差分计算单季值，再与去年同季比对；含会计调整，与公告单季数可能有细微出入，需核实原文。</p></div>

<h2>五、条件化交易计划</h2>
<div class="card plan"><ul>
<li><b>入场（右侧确认）</b>：__ENTRY__</li>
<li><b>加仓</b>：__ADD__</li>
<li><b>止损</b>：__STOP__</li>
<li><b>失效条件</b>：__INVALID__</li>
</ul>
<h3>估值情景区间（非目标价预测）</h3><table>
<tr><th>情景</th><th>对应 PE</th><th>对应价格</th><th>相对现价</th></tr>__SCENROWS__</table>
<p class="small">__PLANNOTE__</p>
<p class="small">适用前提：__PROFILE__ 风险偏好、__HORIZON__ 持有周期、A 股境内交易；未提供个人资金量、仓位与税费约束，故不给出具体仓位金额。前提变化请修改 config.json 后重跑。</p>
</div>

<h2>六、数据来源与口径校验</h2>
<div class="card"><table><tr><th>数据源</th><th>接口</th><th>数据时点 / 抓取时间</th></tr>__SOURCEROWS__</table>
<h3>双源交叉校验</h3><ul>__CHECKHTML__</ul>
<h3>数据缺口与告警</h3><ul>__WARNHTML__</ul>
<p class="small">所有数值均在程序运行时动态抓取；未取到的字段统一显示 N/A 且不计入评分，不做估算填充。行情商与第三方数据平台属非一手来源，正式决策前请核对交易所公告与公司定期报告原文。</p>
</div>

<div class="disc"><b>⚠️ __DISC_TRADE_TITLE__</b><br>__DISC_TRADE__<br><br><b>__DISC_MAIN_TITLE__</b><br>__DISC_MAIN__</div>
<div class="foot">BYD Monitor · 自动生成于 __GEN__ · 仅供研究参考</div>
</div>

<script>
const D = __DATAJS__;
const F = __FALLBACKS__;
function toggleK(showChart){
  document.getElementById('k_chart').style.display = showChart ? 'block' : 'none';
  document.getElementById('k_table').style.display = showChart ? 'none' : 'block';
  document.getElementById('btn_chart').className = showChart ? 'btn on' : 'btn';
  document.getElementById('btn_table').className = showChart ? 'btn' : 'btn on';
  if(showChart){ window.dispatchEvent(new Event('resize')); }
}
const RED = '#c0392b', GREEN = '#1e7d4f';
if(typeof echarts !== 'undefined'){
  echarts.init(document.getElementById('chart_kline')).setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    legend: { data: ['日K','MA5','MA20','MA60'] },
    grid: { left: 55, right: 20, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: D.dates, boundaryGap: true },
    yAxis: { scale: true, splitLine: { lineStyle: { color: '#eef1f5' } } },
    dataZoom: [{ type: 'inside', start: 40, end: 100 }],
    series: [
      { name: '日K', type: 'candlestick', data: D.ohlc,
        itemStyle: { color: RED, color0: GREEN, borderColor: RED, borderColor0: GREEN } },
      { name: 'MA5', type: 'line', data: D.ma5, smooth: true, showSymbol: false, lineStyle: { width: 1.2 } },
      { name: 'MA20', type: 'line', data: D.ma20, smooth: true, showSymbol: false, lineStyle: { width: 1.2 } },
      { name: 'MA60', type: 'line', data: D.ma60, smooth: true, showSymbol: false, lineStyle: { width: 1.2 } }
    ]
  });
  echarts.init(document.getElementById('chart_vol')).setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 55, right: 20, top: 12, bottom: 26 },
    xAxis: { type: 'category', data: D.dates },
    yAxis: { name: '成交量(手)', splitLine: { lineStyle: { color: '#eef1f5' } } },
    series: [{ name: '成交量', type: 'bar', data: D.vols.map(function(v){
      return { value: v[1], itemStyle: { color: v[2] > 0 ? RED : GREEN } };
    }) }]
  });
  echarts.init(document.getElementById('chart_pe')).setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 55, right: 24, top: 24, bottom: 40 },
    xAxis: { type: 'category', data: D.peDates },
    yAxis: { name: 'PE-TTM(倍)', scale: true, splitLine: { lineStyle: { color: '#eef1f5' } } },
    series: [{ name: 'PE-TTM', type: 'line', data: D.peVals, showSymbol: false,
      lineStyle: { width: 1.6, color: '#2559a8' }, areaStyle: { color: 'rgba(37,89,168,0.08)' },
      markLine: { silent: true, symbol: 'none', label: { formatter: '{b}' }, data: [
        { name: '25%', yAxis: D.peQ.q25, lineStyle: { color: GREEN, type: 'dashed' } },
        { name: '50%', yAxis: D.peQ.q50, lineStyle: { color: '#b7791f', type: 'dashed' } },
        { name: '75%', yAxis: D.peQ.q75, lineStyle: { color: RED, type: 'dashed' } }
      ] } }]
  });
  echarts.init(document.getElementById('chart_flow')).setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['当日主力净流入','累计净流入'] },
    grid: { left: 58, right: 58, top: 36, bottom: 40 },
    xAxis: { type: 'category', data: D.flowDates },
    yAxis: [ { name: '当日(亿)', splitLine: { lineStyle: { color: '#eef1f5' } } },
             { name: '累计(亿)' } ],
    series: [
      { name: '当日主力净流入', type: 'bar', data: D.flowMain.map(function(v){
          return { value: v, itemStyle: { color: v >= 0 ? RED : GREEN } };
        }) },
      { name: '累计净流入', type: 'line', yAxisIndex: 1, data: D.flowCum,
        smooth: true, showSymbol: false, lineStyle: { width: 2, color: '#2559a8' } }
    ]
  });
  echarts.init(document.getElementById('chart_radar')).setOption({
    tooltip: {},
    radar: { indicator: [
      { name: '估值', max: 100, min: -100 }, { name: '成长质量', max: 100, min: -100 },
      { name: '趋势技术', max: 100, min: -100 }, { name: '资金情绪', max: 100, min: -100 },
      { name: '风险', max: 100, min: -100 }
    ], radius: '65%' },
    series: [{ type: 'radar', data: [ { name: '五维评分', value: D.radar,
      areaStyle: { color: 'rgba(37,89,168,0.18)' }, lineStyle: { color: '#2559a8' } } ] }]
  });
  echarts.init(document.getElementById('chart_growth')).setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['单季营收','单季归母净利','净利同比'] },
    grid: { left: 58, right: 58, top: 30, bottom: 34 },
    xAxis: { type: 'category', data: D.qLabels },
    yAxis: [ { name: '亿元' }, { name: '同比(%)' } ],
    series: [
      { name: '单季营收', type: 'bar', data: D.qRev, itemStyle: { color: '#9db7de' } },
      { name: '单季归母净利', type: 'bar', data: D.qNp, itemStyle: { color: '#2559a8' } },
      { name: '净利同比', type: 'line', yAxisIndex: 1, data: D.qNpYoy,
        showSymbol: true, lineStyle: { width: 2, color: RED } }
    ]
  });
} else {
  document.querySelectorAll('.chart,.chart-sm').forEach(function(el){
    el.style.height = 'auto';
    el.style.minHeight = '0';
    el.innerHTML = '<div class="chart-fallback">' + (F[el.id] || '图表未渲染，精确数值见本页表格。') + '</div>';
  });
}
</script>
</body></html>"""

    repl = {
        "__TITLE__": f"{q.get('name','比亚迪')}（{ctx['cfg']['symbol']}）监控分析报告 · {now_cn():%Y-%m-%d}",
        "__TAGCOLOR__": tag_color,
        "__TOTAL__": f"{total:+.1f}",
        "__TOTALCLS__": "pos" if total >= 0 else "neg",
        "__SIGNAL__": ctx["decision"]["signal"],
        "__NARRATIVE__": html_mod.escape(narrative(ctx)),
        "__CARDS__": card_html,
        "__SCOREROWS__": score_rows,
        "__GATE__": "是" if ctx["decision"]["gated"] else "否",
        "__ASOF__": q.get("asof", "N/A"),
        "__STATE__": ctx["state"][0],
        "__GEN__": f"{now_cn():%Y-%m-%d %H:%M}（北京时间）",
        "__NAME__": q.get("name", ""),
        "__CODE__": ctx["cfg"]["symbol"],
        "__PE__": fmt(vs.get("pe_ttm"), 2, "x"),
        "__PEPCT__": fmt(vs.get("pe_pct"), 1, "%"),
        "__PEPCT3__": fmt(vs.get("pe_pct3"), 1, "%"),
        "__PEMED__": fmt(vs.get("pe_med"), 2, "x"),
        "__PEMIN__": fmt(vs.get("pe_min"), 2, "x"),
        "__PEMAX__": fmt(vs.get("pe_max"), 2, "x"),
        "__PBPCT__": fmt(vs.get("pb_pct"), 1, "%"),
        "__PSPCT__": fmt(vs.get("ps_pct"), 1, "%"),
        "__PB__": fmt(q.get("pb"), 2, "x"),
        "__PEG__": fmt(vs.get("peg"), 2) if vs.get("peg") else "N/A（净利同比为负）",
        "__FWDPE__": fmt(vs.get("fwd_pe"), 2, "x") if vs.get("fwd_pe") else "N/A（未填 EPS 预测）",
        "__YEARS__": str(vs["years"]),
        "__SAMPLES__": str(vs["samples"]),
        "__KLROWS__": kl_rows,
        "__KLDATE__": kl[-1]["date"] if kl else "N/A",
        "__MA5__": fmt(ind.get("ma5")), "__MA10__": fmt(ind.get("ma10")),
        "__MA20__": fmt(ind.get("ma20")), "__MA60__": fmt(ind.get("ma60")),
        "__MA120__": fmt(ind.get("ma120")), "__MA250__": fmt(ind.get("ma250")),
        "__MACD__": fmt(ind.get("macd_hist"), 3), "__DIF__": fmt(ind.get("dif"), 3),
        "__DEA__": fmt(ind.get("dea"), 3),
        "__RSI__": f"{fmt(ind.get('rsi6'),1)} / {fmt(ind.get('rsi12'),1)} / {fmt(ind.get('rsi24'),1)}",
        "__KDJ__": f"{fmt(ind.get('k'),1)} / {fmt(ind.get('d'),1)} / {fmt(ind.get('j'),1)}",
        "__BOLL__": f"{fmt(ind.get('boll_upper'))} / {fmt(ind.get('boll_mid'))} / {fmt(ind.get('boll_lower'))}",
        "__H20__": fmt(ind.get("high20")), "__L20__": fmt(ind.get("low20")),
        "__POS52__": fmt(ctx.get("pos52"), 1, "%"),
        "__ATR__": fmt(ind.get("atr14")), "__VOLA__": fmt(ind.get("volatility_annual"), 1, "%"),
        "__VR__": fmt(ind.get("vol_ratio_calc")), "__VRQ__": fmt(q.get("volume_ratio")),
        "__VSTART__": val[0]["date"] if val else "N/A", "__VEND__": val[-1]["date"] if val else "N/A",
        "__FSTART__": flow[0]["date"] if flow else "N/A", "__FEND__": flow[-1]["date"] if flow else "N/A",
        "__FINROWS__": fin_rows, "__BALROWS__": bal_rows, "__QROWS__": qrows,
        "__ENTRY__": html_mod.escape(ctx["plan"]["entry"]),
        "__ADD__": html_mod.escape(ctx["plan"]["add"]),
        "__STOP__": html_mod.escape(ctx["plan"]["stop"]),
        "__INVALID__": html_mod.escape(ctx["plan"]["invalidate"]),
        "__SCENROWS__": scen_rows,
        "__PLANNOTE__": html_mod.escape(ctx["plan"]["note"]),
        "__PROFILE__": {"conservative": "保守型", "balanced": "平衡型", "aggressive": "进取型"}.get(
            ctx["cfg"].get("risk_profile"), "平衡型"),
        "__HORIZON__": {"short": "1-3 个月短线", "medium": "3-12 个月波段", "long": "1 年以上"}.get(
            ctx["cfg"].get("holding_horizon"), "3-12 个月波段"),
        "__SOURCEROWS__": source_rows,
        "__CHECKHTML__": check_html,
        "__WARNHTML__": warn_html,
        "__DISC_TRADE_TITLE__": "风险提示与免责声明",
        "__DISC_TRADE__": html_mod.escape(DISCLAIMER_TRADE),
        "__DISC_MAIN_TITLE__": "免责声明",
        "__DISC_MAIN__": html_mod.escape(DISCLAIMER_MAIN),
        "__DATAJS__": data_js,
        "__FALLBACKS__": fallbacks_js,
        # 大体积内嵌库必须最后替换，避免后续 replace 扫描/误伤库内容
        "__ECHARTS_LIB__": echarts_script_tag(),
    }
    out = tpl
    for k, v in repl.items():
        out = out.replace(k, str(v))
    return out


# ----------------------------------------------------------------------------
# 告警
# ----------------------------------------------------------------------------
def load_state():
    p = DATA_DIR / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_state(state):
    try:
        (DATA_DIR / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def fire(alerts, level, rule, msg, cfg, state):
    day = f"{now_cn():%Y-%m-%d}"
    bucket = state.setdefault("fired", {}).setdefault(day, [])
    key = f"{rule}"
    if key in bucket:
        return
    bucket.append(key)
    line = f"[{level}] {msg}"
    print(f"  >>> {line}", flush=True)
    try:
        with (DATA_DIR / "alerts.log").open("a", encoding="utf-8") as f:
            f.write(f"[{now_cn():%Y-%m-%d %H:%M:%S}] {line}\n")
    except OSError:
        pass
    alerts.append({"level": level, "rule": rule, "msg": msg})
    hook = cfg["watch"].get("webhook", "")
    if hook:
        try:
            text = f"【BYD Monitor / {level}】{msg}"
            if cfg["watch"].get("webhook_type") == "dingtalk":
                post_json(hook, {"msgtype": "text", "text": {"content": text}})
            else:
                post_json(hook, {"msgtype": "text", "text": {"content": text}})
        except Exception as e:      # noqa: BLE001
            log(f"webhook 推送失败: {e}", "WARN", echo=False)


def dynamic_levels(ctx):
    q, ind = ctx["quote"], ctx["ind"]
    lv = [("52周低点", q.get("low52")), ("MA20", ind.get("ma20")),
          ("MA60", ind.get("ma60")), ("MA250", ind.get("ma250")),
          ("近20日低点", ind.get("low20"))]
    return [(n, v) for n, v in lv if v]


def check_alerts(ctx, cfg, state, include_daily=False):
    a = ctx["cfg"]["alerts"]
    q, ind, indp, vs = ctx["quote"], ctx["ind"], ctx["ind_prev"], ctx["val_stat"]
    p, pct = q.get("price"), q.get("pct") or 0
    alerts = []

    def emit(level, rule, msg):
        fire(alerts, level, rule, msg, ctx["cfg"], state)

    if p is None:
        return alerts
    # 1 日内涨跌幅异动
    if abs(pct) >= a["pct_change"]:
        emit("CRITICAL" if abs(pct) >= a["pct_change"] * 1.5 else "WARNING",
             f"pct_move_{'+' if pct > 0 else '-'}",
             f"日内涨跌 {pct:+.2f}%（阈值 ±{a['pct_change']}%），现价 {p:.2f} 元")
    # 2 跳空缺口
    op, pc = q.get("open"), q.get("prev_close")
    if op and pc:
        gap = (op / pc - 1) * 100
        if abs(gap) >= a["gap_pct"]:
            emit("WARNING", f"gap_{'+' if gap > 0 else '-'}",
                 f"跳空{'高开' if gap > 0 else '低开'} {gap:+.2f}%（今开 {op:.2f} / 昨收 {pc:.2f}）")
    # 3 放量
    vr = q.get("volume_ratio")
    if vr and vr >= a["volume_ratio"]:
        emit("WARNING", "volume_spike", f"量比 {vr:.2f} ≥ {a['volume_ratio']}，成交量显著放大")
    # 4 52 周新低 / 新高
    l52, h52 = q.get("low52"), q.get("high52")
    if l52 and p <= l52 * 1.002:
        emit("CRITICAL", "low52", f"触及 52 周低点 {l52:.2f}，现价 {p:.2f}，需按止损纪律处理")
    if h52 and p >= h52 * 0.998:
        emit("INFO", "high52", f"接近 / 创 52 周新高 {h52:.2f}，重新评估上限")
    # 5 关键位
    for name, v in dynamic_levels(ctx):
        if abs(p / v - 1) <= 0.003:
            emit("INFO", f"level_{name}", f"价格 {p:.2f} 贴近关键位 {name} {v:.2f}（±0.3%），关注收盘确认")
    # 6 RSI
    r6 = ind.get("rsi6")
    if r6 is not None:
        if r6 < a["rsi_oversold"]:
            emit("WARNING", "rsi_oversold", f"RSI6={r6:.1f} < {a['rsi_oversold']}，进入超卖区（非买入信号，需右侧确认）")
        elif r6 > a["rsi_overbought"]:
            emit("WARNING", "rsi_overbought", f"RSI6={r6:.1f} > {a['rsi_overbought']}，进入超买区")
    # 7 均线金叉 / 死叉
    if all(x is not None for x in (ind.get("ma5"), ind.get("ma10"), indp.get("ma5"), indp.get("ma10"))):
        if indp["ma5"] <= indp["ma10"] and ind["ma5"] > ind["ma10"]:
            emit("WARNING", "ma_golden", "MA5 上穿 MA10（金叉），短线动能转强，需量能配合确认")
        if indp["ma5"] >= indp["ma10"] and ind["ma5"] < ind["ma10"]:
            emit("WARNING", "ma_dead", "MA5 下穿 MA10（死叉），短线动能转弱")
    # 8 MACD 金叉 / 死叉
    if all(x is not None for x in (ind.get("dif"), ind.get("dea"), indp.get("dif"), indp.get("dea"))):
        if indp["dif"] <= indp["dea"] and ind["dif"] > ind["dea"]:
            emit("WARNING", "macd_golden", "MACD DIF 上穿 DEA（金叉），趋势动能可能修复")
        if indp["dif"] >= indp["dea"] and ind["dif"] < ind["dea"]:
            emit("WARNING", "macd_dead", "MACD DIF 下穿 DEA（死叉），趋势动能转弱")
    # 9 布林带突破
    up, lo = ind.get("boll_upper"), ind.get("boll_lower")
    if up and p > up:
        emit("WARNING", "boll_up", f"收盘/现价 {p:.2f} 突破布林上轨 {up:.2f}")
    if lo and p < lo:
        emit("WARNING", "boll_low", f"收盘/现价 {p:.2f} 跌破布林下轨 {lo:.2f}")
    # 10 主力资金流
    if ctx["flow"]:
        mf = ctx["flow"][-1].get("main_net")
        if mf is not None and abs(mf) >= a["main_flow_threshold_yuan"]:
            emit("WARNING", "main_flow",
                 f"{ctx['flow'][-1]['date']} 主力净{'流入' if mf > 0 else '流出'} {abs(mf)/1e8:.2f} 亿元"
                 f"（阈值 {a['main_flow_threshold_yuan']/1e8:.1f} 亿）")
    # 11 估值分位
    pep = vs.get("pe_pct")
    if pep is not None:
        if pep <= a["pe_low_percentile"]:
            emit("INFO", "pe_low", f"PE-TTM 处于 5 年 {pep:.1f}% 分位（≤{a['pe_low_percentile']}%），估值偏低")
        elif pep >= a["pe_high_percentile"]:
            emit("WARNING", "pe_high", f"PE-TTM 处于 5 年 {pep:.1f}% 分位（≥{a['pe_high_percentile']}%），估值偏高")
    # 12 成本止盈 / 止损
    cost = ctx["cfg"].get("cost_price")
    if cost:
        r = (p / cost - 1) * 100
        if r >= a["take_profit_pct"]:
            emit("CRITICAL", "take_profit",
                 f"相对成本 {cost:.2f} 浮盈 {r:+.2f}%（≥{a['take_profit_pct']}%），可考虑分批止盈 1/3")
        if r <= a["stop_loss_pct"]:
            emit("CRITICAL", "stop_loss",
                 f"相对成本 {cost:.2f} 浮亏 {r:+.2f}%（≤{a['stop_loss_pct']}%），触发止损纪律")
        # 13 动态止盈（追踪止损）
        trail = state.setdefault("trail", {})
        peak = max(trail.get("peak", cost), p)
        trail["peak"] = peak
        if (peak / cost - 1) * 100 >= a["trailing_start_pct"]:
            dd = (p / peak - 1) * 100
            if dd <= -a["trailing_drawdown_pct"]:
                emit("CRITICAL", "trailing_stop",
                     f"追踪止盈：自峰值 {peak:.2f} 回撤 {dd:.2f}%（阈值 -{a['trailing_drawdown_pct']}%）")
    # 14 每日收盘复盘
    if include_daily:
        emit("INFO", "daily_review",
             f"收盘复盘：{p:.2f} 元（{pct:+.2f}%），PE-TTM {fmt(q.get('pe_ttm'),2,'x')}，"
             f"综合评分 {ctx['total']:+.1f}，信号「{ctx['decision']['signal']}」")
    return alerts


# ----------------------------------------------------------------------------
# 快照 / 单次分析
# ----------------------------------------------------------------------------
def write_snapshot(ctx):
    p = DATA_DIR / "snapshots.csv"
    q, vs = ctx["quote"], ctx["val_stat"]
    row = {
        "ts": f"{now_cn():%Y-%m-%d %H:%M:%S}", "asof": q.get("asof"),
        "price": q.get("price"), "pct": q.get("pct"),
        "pe_ttm": q.get("pe_ttm"), "pe_pct5": vs.get("pe_pct"), "pb": q.get("pb"),
        "score": round(ctx["total"], 2), "signal": ctx["decision"]["signal"],
        "ma20": ctx["ind"].get("ma20"), "rsi6": ctx["ind"].get("rsi6"),
        "main5_yi": round(sum(x["main_net"] or 0 for x in ctx["flow"][-5:]) / 1e8, 3) if ctx["flow"] else None,
        "volume_ratio": q.get("volume_ratio"),
    }
    exists = p.exists()
    try:
        with p.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if not exists:
                w.writeheader()
            w.writerow(row)
    except OSError as e:
        log(f"快照写入失败: {e}", "WARN", echo=False)


def full_refresh(cfg, quiet=False):
    ds = DataSource(cfg)
    q = ds.quote_tencent()
    try:
        em = ds.quote_em()
        notes = ds.cross_check(q, em)
    except Exception as e:      # noqa: BLE001
        notes = [f"东财交叉校验不可用（{type(e).__name__}），本次仅采用腾讯行情单源"]
        ds.warnings.append("交叉校验降级为单源")
    ctx = {
        "cfg": cfg, "quote": q, "cross_notes": notes,
        "state": trading_state(), "sources": ds.sources, "warnings": ds.warnings,
    }
    for name, fn, args in (
        ("kline", ds.kline, (int(cfg.get("kline_days", 420)),)),
        ("valuation", ds.valuation_history, (int(cfg.get("valuation_years", 5)),)),
        ("financials", ds.financials, ()),
        ("balance", ds.balance, ()),
        ("flow", ds.fundflow, ()),
    ):
        try:
            ctx[name] = fn(*args)
        except Exception as e:      # noqa: BLE001
            ctx[name] = []
            msg = f"{name} 数据源失败（{type(e).__name__}: {e}），相关维度不计分并标注 N/A"
            ds.warnings.append(msg)
            log(msg, "WARN", echo=not quiet)
    ctx["sources"] = ds.sources
    ctx["warnings"] = ds.warnings
    analyze(ctx)
    write_snapshot(ctx)
    html_text = render_report(ctx)
    out = REPORT_DIR / f"BYD-{cfg['symbol']}-{now_cn():%Y%m%d}.html"
    out.write_text(html_text, encoding="utf-8")
    ctx["report_path"] = str(out)
    return ctx


def print_summary(ctx):
    q, vs = ctx["quote"], ctx["val_stat"]
    print("\n" + "=" * 78)
    print(f"  {q.get('name','')}（{ctx['cfg']['symbol']}）  数据时点 {q.get('asof')}  [{ctx['state'][0]}]")
    print("=" * 78)
    print(f"  最新价 {fmt(q.get('price'))} 元  当日 {fmt(q.get('pct'),2,'%')}  "
          f"PE-TTM {fmt(q.get('pe_ttm'),2,'x')}（5年分位 {fmt(vs.get('pe_pct'),1,'%')}）  "
          f"PB {fmt(q.get('pb'),2,'x')}")
    print(f"  综合评分 {ctx['total']:+.1f} / ±100    →    信号：{ctx['decision']['signal']}")
    print("-" * 78)
    for k, v in ctx["scores"].items():
        print(f"  {v['label']:<6} 权重 {v['weight']*100:>4.0f}%   得分 {v['score']:+7.1f}   "
              f"加权 {v['weighted']:+7.1f}")
    print("-" * 78)
    print(f"  MA5/20/60: {fmt(ctx['ind'].get('ma5'))} / {fmt(ctx['ind'].get('ma20'))} / "
          f"{fmt(ctx['ind'].get('ma60'))}   RSI6 {fmt(ctx['ind'].get('rsi6'),1)}   "
          f"MACD柱 {fmt(ctx['ind'].get('macd_hist'),3)}")
    if ctx["singles"]:
        sq = ctx["singles"][-1]
        print(f"  {sq['year']}Q{sq['q']} 单季归母净利 {fmt(sq['np']/1e8,2)} 亿，"
              f"同比 {fmt(sq.get('np_yoy'),2,'%')}")
    print("-" * 78)
    print("  结论：" + narrative(ctx))
    print(f"\n  HTML 研报：{ctx['report_path']}")
    print(f"  快照 CSV ：{DATA_DIR / 'snapshots.csv'}")
    print("=" * 78 + "\n")


# ----------------------------------------------------------------------------
# 监控主循环
# ----------------------------------------------------------------------------
def watch(cfg, interval_override=None):
    log("启动常驻监控，Ctrl+C 退出")
    ctx = None
    last_full = 0.0
    last_daily = ""
    state = load_state()
    refresh_sec = cfg["watch"].get("full_refresh_minutes", 30) * 60
    try:
        while True:
            st, is_trading = trading_state()
            t = now_cn()
            need_full = ctx is None or (time.time() - last_full >= refresh_sec)
            # 收盘后生成当日复盘（只做一次）
            do_daily = (t.hour * 60 + t.minute >= 15 * 60 + 5) and \
                       (t.hour * 60 + t.minute < 23 * 60) and last_daily != f"{t:%Y-%m-%d}"
            if need_full or do_daily:
                try:
                    ctx = full_refresh(cfg, quiet=True)
                    last_full = time.time()
                    log(f"全量刷新完成 | {st} | 评分 {ctx['total']:+.1f} | "
                        f"信号 {ctx['decision']['signal']} | 报告 {Path(ctx['report_path']).name}")
                except Exception as e:      # noqa: BLE001
                    log(f"全量刷新失败: {type(e).__name__}: {e}", "ERROR")
                    time.sleep(30)
                    continue
            else:
                # 轻量刷新：只取实时报价，指标沿用上次全量结果
                try:
                    fresh = DataSource(cfg).quote_tencent()
                    ctx["quote"].update(fresh)
                    ctx["state"] = trading_state()
                except Exception as e:      # noqa: BLE001
                    log(f"实时报价刷新失败: {type(e).__name__}: {e}", "WARN")
            try:
                alerts = check_alerts(ctx, cfg, state, include_daily=do_daily)
                if alerts:
                    log(f"本轮触发 {len(alerts)} 条告警", "ALERT", echo=False)
                if do_daily:
                    last_daily = f"{t:%Y-%m-%d}"
                save_state(state)
            except Exception as e:      # noqa: BLE001
                log(f"告警检查异常: {type(e).__name__}: {e}", "ERROR")
            wait = interval_override or (
                cfg["watch"]["interval_trading"] if is_trading else cfg["watch"]["interval_idle"])
            log(f"{st} | 下次扫描 {wait}s 后 | 现价 {fmt(ctx['quote'].get('price'))} 元",
                echo=False)
            time.sleep(wait)
    except KeyboardInterrupt:
        log("监控已停止")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="比亚迪股价监控 / 估值统计 / 买卖信号分析")
    ap.add_argument("--once", action="store_true", help="单次全量分析并生成 HTML 研报")
    ap.add_argument("--watch", action="store_true", help="常驻监控与分级告警")
    ap.add_argument("--interval", type=int, help="覆盖轮询间隔（秒）")
    ap.add_argument("--symbol", help="覆盖股票代码，默认 002594")
    ap.add_argument("--cost", type=float, help="覆盖持仓成本价，启用止盈/止损告警")
    ap.add_argument("--config", help="指定配置文件路径")
    args = ap.parse_args()

    cfg = load_config()
    if args.config:
        try:
            user = json.loads(Path(args.config).read_text(encoding="utf-8"))
            for k, v in user.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (OSError, ValueError) as e:
            log(f"指定配置读取失败: {e}", "WARN")
    if args.symbol:
        cfg["symbol"] = args.symbol
    if args.cost is not None:
        cfg["cost_price"] = args.cost
    if not args.watch:
        args.once = True

    if args.once:
        try:
            ctx = full_refresh(cfg)
        except Exception as e:      # noqa: BLE001
            log(f"分析失败: {type(e).__name__}: {e}", "ERROR")
            sys.exit(1)
        print_summary(ctx)
        state = load_state()
        alerts = check_alerts(ctx, cfg, state, include_daily=False)
        save_state(state)
        if alerts:
            print(f"本轮触发 {len(alerts)} 条告警（明细见 data/alerts.log）：")
            for a in alerts:
                print(f"  [{a['level']}] {a['msg']}")
        print("\n" + DISCLAIMER_TRADE)
        print(DISCLAIMER_MAIN)
        if args.watch:
            watch(cfg, args.interval)
    else:
        watch(cfg, args.interval)


if __name__ == "__main__":
    main()
