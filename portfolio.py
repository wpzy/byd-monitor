#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多标的投研看板（16 个 ETF / 个股）
=====================================================================
与 byd_monitor.py 共用数据层与技术指标，但区分两类分析口径：

  · 个股：完全沿用比亚迪五维模型（估值 / 成长质量 / 趋势技术 / 资金情绪 / 风险）
  · ETF ：无 PE/PB/财报，改用「价格历史分位 / 趋势技术 / 波动回撤 /
          资金情绪 / 短期动量」五维，其中价格分位替代估值分位

统一输出风险点位（支撑 / 压力 / 止损 / 回撤 / 波动）与条件化买入建议，
并沿用趋势闸门：趋势与资金同时转弱时不允许输出买入信号。

用法：
    python portfolio.py --once            # 抓数 + 生成独立 HTML 看板
    python portfolio.py --summary         # 只打印汇总表
    python portfolio.py --serve           # 独立启动看板服务（默认 8768）
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import html as html_mod
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

import byd_monitor as engine

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "report"
PORTFOLIO_JSON = DATA_DIR / "portfolio.json"

# symbol, market, kind, 简称, 板块
UNIVERSE = [
    ("518880", "sh", "ETF",   "黄金ETF",      "商品/避险"),
    ("512010", "sh", "ETF",   "医药ETF",      "医药"),
    ("512000", "sh", "ETF",   "券商ETF",      "证券"),
    ("588400", "sh", "ETF",   "双创ETF",      "科创创业"),
    ("159227", "sz", "ETF",   "航空航天ETF",  "军工"),
    ("159338", "sz", "ETF",   "中证A500ETF",  "宽基"),
    ("159669", "sz", "ETF",   "绿电ETF",      "电力/新能源"),
    ("159690", "sz", "ETF",   "有色ETF",      "有色金属"),
    ("516010", "sh", "ETF",   "游戏ETF",      "传媒/游戏"),
    ("159558", "sz", "ETF",   "半导体ETF",    "半导体设备"),
    ("588200", "sh", "ETF",   "科创芯片ETF",  "科创芯片"),
    ("002463", "sz", "STOCK", "沪电股份",     "PCB/算力"),
    ("601138", "sh", "STOCK", "工业富联",     "AI服务器"),
    ("300502", "sz", "STOCK", "新易盛",       "光模块"),
    ("000933", "sz", "STOCK", "神火股份",     "煤炭/电解铝"),
    ("603986", "sh", "STOCK", "兆易创新",     "存储芯片"),
]

ETF_WEIGHTS = {
    "position": 0.25, "trend": 0.30, "volatility": 0.20,
    "fund": 0.15, "momentum": 0.10,
}
ETF_LABELS = {
    "position": "价格位置", "trend": "趋势技术", "volatility": "波动回撤",
    "fund": "资金情绪", "momentum": "短期动量",
}
STOCK_LABELS = {
    "valuation": "估值", "growth": "成长质量", "trend": "趋势技术",
    "fund": "资金情绪", "risk": "风险",
}

DISCLAIMER = (engine.DISCLAIMER_MAIN + " " + engine.DISCLAIMER_TRADE).strip()


# ---------------------------------------------------------------------------
# 基础计算
# ---------------------------------------------------------------------------
def pct_rank(series, value):
    if not series or value is None:
        return None
    r = engine.percentile_rank(list(series), value)
    return r[0] if r else None


def max_drawdown(closes):
    """近 N 日最大回撤（负数，百分数）。"""
    if len(closes) < 2:
        return None
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            mdd = min(mdd, (c / peak - 1) * 100)
    return mdd


def ma_series(closes, n):
    out = []
    for i in range(len(closes)):
        out.append(round(sum(closes[i - n + 1:i + 1]) / n, 4) if i >= n - 1 else None)
    return out


def fmt(v, digits=2, suffix="", na="N/A"):
    return engine.fmt(v, digits, suffix, na)


# ---------------------------------------------------------------------------
# ETF 专属评分
# ---------------------------------------------------------------------------
def score_position(closes, price, high52, low52):
    """价格历史分位替代 PE 分位：低位有利，高位不利。"""
    detail, s = [], 0.0
    p3 = pct_rank(closes[-750:], price)
    if p3 is not None:
        v = (0.5 - p3 / 100) * 120
        s += v
        detail.append(f"现价处于近 3 年价格 {p3:.1f}% 分位 → {v:+.1f}")
    if high52 and low52 and high52 > low52:
        pos = (price - low52) / (high52 - low52) * 100
        v = (0.5 - pos / 100) * 40
        s += v
        detail.append(f"52 周区间位置 {pos:.1f}% → {v:+.1f}")
        dd = (price / high52 - 1) * 100
        v = 15 if dd < -30 else (5 if dd < -15 else (-10 if dd > -3 else 0))
        s += v
        detail.append(f"距 52 周高点回撤 {dd:.1f}% → {v:+d}")
    return engine.clamp(s), detail


def score_volatility(ind, closes, price):
    detail, s = [], 0.0
    vol = ind.get("volatility_annual")
    if vol is not None:
        v = 25 if vol < 20 else (10 if vol < 35 else (-15 if vol < 50 else -30))
        s += v
        detail.append(f"近 60 日年化波动率 {vol:.1f}% → {v:+d}")
    atr = ind.get("atr14")
    if atr and price:
        ap = atr / price * 100
        v = 10 if ap < 1.5 else (0 if ap < 3 else -15)
        s += v
        detail.append(f"ATR14 / 现价 = {ap:.2f}% → {v:+d}")
    mdd = max_drawdown(closes[-250:])
    if mdd is not None:
        v = 15 if mdd > -15 else (5 if mdd > -30 else (-15 if mdd > -45 else -25))
        s += v
        detail.append(f"近 250 日最大回撤 {mdd:.1f}% → {v:+d}")
    return engine.clamp(s), detail


def score_fund_etf(flow, kl, q):
    """按「主力净流 / 同期成交额」衡量，规避 ETF 规模口径差异。"""
    detail, s = [], 0.0
    if flow and kl:
        amts = [x.get("amount") or 0 for x in kl[-20:]]
        amt = sum(amts) / len(amts) if amts else 0
        if amt > 0:
            for n, weight, label in ((5, 35, "近5日"), (20, 25, "近20日")):
                seg = [x["main_net"] for x in flow[-n:] if x.get("main_net") is not None]
                if not seg:
                    continue
                val = sum(seg)
                r = val / (amt * n) * 100
                v = weight if r > 1 else (weight / 3 if r > 0 else
                                         (-weight / 3 if r > -1 else -weight))
                s += v
                detail.append(f"{label}主力净额 {val/1e8:+.2f}亿（占同期成交额 {r:+.2f}%）→ {v:+.1f}")
    vr = q.get("volume_ratio")
    pct = q.get("pct") or 0
    if vr:
        v = 10 if (vr > 1.5 and pct > 0) else (-10 if (vr > 1.5 and pct < 0) else 0)
        s += v
        detail.append(f"实时量比 {vr:.2f} + 当日 {pct:+.2f}% → {v:+d}")
    vr2 = ind_ratio(kl)
    if vr2:
        v = 8 if vr2 > 1.2 else (-8 if vr2 < 0.7 else 0)
        s += v
        detail.append(f"最新成交量 / 5 日均量 = {vr2:.2f} → {v:+d}")
    return engine.clamp(s), detail


def ind_ratio(kl):
    vols = [x.get("volume") or 0 for x in kl[-6:]]
    if len(vols) < 6:
        return None
    base = sum(vols[-6:-1]) / 5
    return vols[-1] / base if base else None


def score_momentum(ind, q):
    detail, s = [], 0.0
    r6 = ind.get("rsi6")
    if r6 is not None:
        v = 25 if r6 < 25 else (10 if r6 < 40 else (-20 if r6 > 75 else (-8 if r6 > 65 else 0)))
        s += v
        detail.append(f"RSI6={r6:.1f} → {v:+d}")
    j = ind.get("j")
    if j is not None:
        v = 15 if j < 0 else (-15 if j > 100 else 0)
        s += v
        detail.append(f"KDJ-J={j:.1f} → {v:+d}")
    c5 = q.get("chg_5d")
    c20 = q.get("chg_20d")
    if c20 is not None:
        v = 12 if c20 > 8 else (4 if c20 > 0 else (-15 if c20 < -15 else -5))
        s += v
        detail.append(f"近 20 日涨跌 {c20:+.2f}% → {v:+d}")
    if c5 is not None and r6 is not None and r6 < 35:
        v = 10 if c5 > 0 else -5
        s += v
        detail.append(f"超卖区近 5 日 {'已止跌' if c5 > 0 else '仍在下行'}（{c5:+.2f}%）→ {v:+d}")
    return engine.clamp(s), detail


# ---------------------------------------------------------------------------
# 风险点位与建议（两类标的统一口径）
# ---------------------------------------------------------------------------
def compute_levels(kl, ind, q, price):
    highs = [x["high"] for x in kl if x.get("high")]
    lows = [x["low"] for x in kl if x.get("low")]
    closes = [x["close"] for x in kl if x.get("close")]
    h52 = max(highs[-250:]) if len(highs) >= 20 else (q.get("high52") or None)
    l52 = min(lows[-250:]) if len(lows) >= 20 else (q.get("low52") or None)
    h20 = max(highs[-20:]) if len(highs) >= 20 else None
    l20 = min(lows[-20:]) if len(lows) >= 20 else None
    ma20, ma60 = ind.get("ma20"), ind.get("ma60")
    bl, bu = ind.get("boll_lower"), ind.get("boll_upper")

    sup_candidates = sorted([x for x in (ma20, l20, bl, l52) if x and x < price], reverse=True)
    res_candidates = sorted([x for x in (ma60, h20, bu, h52) if x and x > price])
    support = sup_candidates[0] if sup_candidates else l52
    resistance = res_candidates[0] if res_candidates else h52
    stop_tech = support * 0.985 if support else None
    stop_pct = price * 0.92
    stop = max([x for x in (stop_tech, stop_pct) if x]) if (stop_tech or stop_pct) else None

    dd = (price / h52 - 1) * 100 if h52 else None
    atr_pct = (ind.get("atr14") / price * 100) if (ind.get("atr14") and price) else None
    vol = ind.get("volatility_annual")
    mdd = max_drawdown(closes[-250:])
    pos52 = (price - l52) / (h52 - l52) * 100 if (h52 and l52 and h52 > l52) else None
    p3 = pct_rank(closes[-750:], price)

    reasons, risk_pts = [], 0
    if vol is not None:
        if vol > 40: risk_pts += 2; reasons.append(f"年化波动率 {vol:.1f}% 偏高")
        elif vol > 30: risk_pts += 1; reasons.append(f"年化波动率 {vol:.1f}% 中等偏高")
    if dd is not None:
        if dd < -40: risk_pts += 2; reasons.append(f"距 52 周高点回撤 {dd:.1f}%")
        elif dd < -25: risk_pts += 1; reasons.append(f"距 52 周高点回撤 {dd:.1f}%")
    if mdd is not None and mdd < -35:
        risk_pts += 1; reasons.append(f"近 250 日最大回撤 {mdd:.1f}%")
    tr = ind.get("trend_score")
    if tr is not None and tr < -20:
        risk_pts += 2; reasons.append(f"趋势评分 {tr:+.1f}（空头排列）")
    elif tr is not None and tr < 0:
        risk_pts += 1; reasons.append(f"趋势评分 {tr:+.1f}")
    r6 = ind.get("rsi6")
    if r6 is not None and r6 > 75:
        risk_pts += 1; reasons.append(f"RSI6={r6:.1f} 超买")
    level = "高" if risk_pts >= 4 else ("中" if risk_pts >= 2 else "低")
    if not reasons:
        reasons.append("波动、回撤与趋势均未见显著异常")

    out = {
        "price": price, "support": support, "support2": l52,
        "resistance": resistance, "resistance2": h52, "stop": stop,
        "high20": h20, "low20": l20, "high52": h52, "low52": l52,
        "ma20": ma20, "ma60": ma60,
        "drawdown_pct": dd, "atr_pct": atr_pct, "vol_annual": vol,
        "max_drawdown_pct": mdd, "pos52": pos52, "price_pct3": p3,
        "risk_level": level, "risk_reasons": reasons,
    }
    # 统一精度，避免 JSON 与页面出现 111.27533333333334 这类长浮点
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in out.items()}


def apply_risk_gate(decision, levels, ind):
    """风险闸门：高风险 + 趋势仍偏弱时不允许输出买入。

    延续 byd_monitor.decide 的「趋势闸门」思路：估值/成长再好，也不在
    空头排列 + 高波动 + 深度回撤的位置直接给买入信号。
    """
    if (decision["tag"] == "buy" and levels["risk_level"] == "高"
            and (ind.get("trend_score") or 0) < 0):
        return {"signal": "观望（高风险，等右侧确认）", "tag": "hold", "gated": True}
    return decision


def build_advice(decision, levels, kind):
    tag = decision["tag"]
    p = levels["price"]
    sup, res = levels["support"], levels["resistance"]
    stop = levels["stop"]
    ma20 = levels["ma20"]
    entry = (f"收盘站上 MA20（{fmt(ma20)}）且成交量 ≥ 5 日均量 ×1.2，"
             f"同时 MACD 柱由负转正 —— 三条件同时满足才考虑试错仓")
    stop_txt = (f"跌破近 20 日低点 {fmt(levels['low20'])} 或成本回撤 8%"
                f"（参考止损 {fmt(stop)}），收盘确认后执行")
    invalidate = (f"主力 20 日净流出继续扩大 / 放量跌破 52 周低点 {fmt(levels['low52'])} / "
                  f"单季净利同比再度转负（个股）")
    if tag == "buy":
        return {
            "action": "可分批建仓" if kind == "ETF" else "可小仓试探",
            "trigger": entry, "position": "首笔 10–20%，站稳 MA60 后再考虑加仓",
            "stop": stop_txt, "invalidate": invalidate,
            "note": "评分达标不代表当日追买；以上为条件化触发，未满足前维持观望。",
        }
    if tag == "hold":
        return {
            "action": "观望 / 持有",
            "trigger": entry, "position": "已有仓位可持有，不新增；空仓者等待右侧确认",
            "stop": stop_txt, "invalidate": invalidate,
            "note": "趋势与资金尚未共振，左侧不逆势加仓。",
        }
    return {
        "action": "减仓 / 回避",
        "trigger": f"先收复 {fmt(res)} 并站稳，再重新评估；在此之前不接飞刀",
        "position": "不建议新增；已有仓位按纪律控制回撤",
        "stop": stop_txt, "invalidate": invalidate,
        "note": "趋势与资金同时偏弱，评分模型已触发降级。",
    }


# ---------------------------------------------------------------------------
# 单标的完整分析
# ---------------------------------------------------------------------------
def analyze_one(item, quiet=True):
    symbol, market, kind, alias, sector = item
    cfg = copy.deepcopy(engine.DEFAULT_CONFIG)
    cfg.update({"symbol": symbol, "name": alias, "market": market,
                "cost_price": None, "risk_profile": "balanced"})
    ds = engine.DataSource(cfg)
    q = ds.quote_tencent()
    try:
        em = ds.quote_em()
        notes = ds.cross_check(q, em)
    except Exception as e:      # noqa: BLE001
        notes = [f"东财交叉校验不可用（{type(e).__name__}），本次仅用腾讯行情单源"]
        ds.warnings.append("交叉校验降级为单源")

    ctx = {
        "cfg": cfg, "quote": q, "cross_notes": notes,
        "state": engine.trading_state(), "sources": ds.sources,
        "warnings": list(ds.warnings),
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
            ctx["warnings"].append(f"{name} 数据源失败（{type(e).__name__}），相关维度标注 N/A")
    ctx["sources"] = ds.sources

    kl = ctx["kline"]
    if len(kl) < 60:
        raise engine.DataError(f"{symbol} 有效 K 线不足（{len(kl)} 根）")

    # 先跑一次标准 analyze：得到 ind / val_stat / 个股五维 / 计划
    engine.analyze(ctx)
    ind = dict(ctx["ind"])
    price = q.get("price") or ind.get("close")
    closes = [x["close"] for x in kl]
    h52 = max([x["high"] for x in kl[-250:]]) if len(kl) >= 20 else q.get("high52")
    l52 = min([x["low"] for x in kl[-250:]]) if len(kl) >= 20 else q.get("low52")
    ind["trend_score"] = ctx["scores"]["trend"]["score"]

    if kind == "ETF":
        sp, dp = score_position(closes, price, h52, l52)
        st, dt_ = ctx["scores"]["trend"]["score"], ctx["scores"]["trend"]["detail"]
        sv, dv = score_volatility(ind, closes, price)
        sf, df = score_fund_etf(ctx["flow"], kl, q)
        sm, dm = score_momentum(ind, q)
        raw = {"position": sp, "trend": st, "volatility": sv, "fund": sf, "momentum": sm}
        det = {"position": dp, "trend": dt_, "volatility": dv, "fund": df, "momentum": dm}
        scores = {
            k: {"score": raw[k], "weight": ETF_WEIGHTS[k],
                "weighted": raw[k] * ETF_WEIGHTS[k],
                "label": ETF_LABELS[k], "detail": det[k]}
            for k in ETF_WEIGHTS
        }
        total = sum(v["weighted"] for v in scores.values())
        decision = engine.decide(total, ETF_WEIGHTS, st, sf, cfg)
        model = "ETF 五维（价格位置 / 趋势 / 波动回撤 / 资金 / 动量）"
        ctx["scores"], ctx["total"], ctx["decision"] = scores, total, decision
        pe_ttm = pb = pe_pct = None
    else:
        scores = ctx["scores"]
        total = ctx["total"]
        decision = ctx["decision"]
        model = "个股五维（估值 / 成长质量 / 趋势 / 资金 / 风险）"
        for k, v in scores.items():
            v["label"] = STOCK_LABELS.get(k, k)
        pe_ttm, pb = q.get("pe_ttm"), q.get("pb")
        pe_pct = ctx["val_stat"].get("pe_pct")

    levels = compute_levels(kl, ind, q, price)
    decision = apply_risk_gate(decision, levels, ind)
    advice = build_advice(decision, levels, kind)
    if levels["risk_level"] == "高":
        advice["note"] += (" 当前风险等级为「高」（波动/回撤偏大）：即使触发条件全部满足，"
                           "首笔仓位也建议压到 5–10%，并严格执行止损。")
    tail = kl[-120:]
    all_closes = [x["close"] for x in kl]
    ma20_all, ma60_all = ma_series(all_closes, 20), ma_series(all_closes, 60)

    return {
        "symbol": symbol, "name": q.get("name") or alias, "alias": alias,
        "market": market, "kind": kind, "sector": sector, "model": model,
        "asof": q.get("asof"), "price": price, "pct": q.get("pct"),
        "pe_ttm": pe_ttm, "pb": pb, "pe_pct": pe_pct,
        "amount_yi": (q.get("amount_wan") or 0) / 1e4 if q.get("amount_wan") else None,
        "total": round(total, 2), "signal": decision["signal"],
        "tag": decision["tag"], "gated": decision["gated"],
        "risk_level": levels["risk_level"],
        "scores": {k: {"label": v["label"], "score": round(v["score"], 2),
                       "weight": v["weight"], "weighted": round(v["weighted"], 2),
                       "detail": v["detail"]} for k, v in scores.items()},
        "levels": engine.clean_json(levels),
        "advice": advice,
        "chart": {
            "dates": [x["date"] for x in tail],
            "ohlc": [[x["open"], x["close"], x["low"], x["high"]] for x in tail],
            "vols": [x.get("volume") or 0 for x in tail],
            "ma20": ma20_all[-120:], "ma60": ma60_all[-120:],
            "flowDates": [x["date"] for x in ctx["flow"][-30:]],
            "flowMain": [round((x.get("main_net") or 0) / 1e8, 4) for x in ctx["flow"][-30:]],
        },
        "warnings": ctx["warnings"],
        "sources": [s["name"] for s in ctx["sources"]],
        "ok": True,
    }


def run_all(workers=5, quiet=True):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    results, errors = [], []
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        fut = {pool.submit(analyze_one, item, quiet): item for item in UNIVERSE}
        for f in concurrent.futures.as_completed(fut):
            item = fut[f]
            try:
                r = f.result()
                results.append(r)
                if not quiet:
                    print(f"  {r['symbol']} {r['alias']:<12} {r['total']:+7.2f} "
                          f"{r['signal']:<14} 风险 {r['risk_level']}")
            except Exception as e:      # noqa: BLE001
                errors.append({"symbol": item[0], "alias": item[3],
                               "error": f"{type(e).__name__}: {e}"})
                if not quiet:
                    print(f"  {item[0]} {item[3]:<12} FAILED {type(e).__name__}: {e}")
    order = {item[0]: i for i, item in enumerate(UNIVERSE)}
    results.sort(key=lambda r: order.get(r["symbol"], 999))
    data = {
        "generated_at": f"{engine.now_cn():%Y-%m-%d %H:%M:%S}",
        "asof": max((r["asof"] for r in results if r.get("asof")), default=None),
        "count": len(results), "failed": len(errors),
        "workers": workers, "elapsed": round(time.time() - t0, 1),
        "disclaimer": DISCLAIMER,
        "items": results, "errors": errors,
    }
    PORTFOLIO_JSON.write_text(json.dumps(engine.clean_json(data), ensure_ascii=False),
                              encoding="utf-8")
    return data


def load_cached():
    if PORTFOLIO_JSON.exists():
        try:
            return json.loads(PORTFOLIO_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return None


def print_summary(data):
    print("\n" + "=" * 108)
    print(f"  多标的投研汇总 | 数据时点 {data.get('asof')} | 生成 {data.get('generated_at')} "
          f"| 成功 {data['count']}/{data['count']+data['failed']}")
    print("=" * 108)
    print(f"{'代码':<8}{'名称':<14}{'类型':<7}{'现价':>10}{'涨跌%':>9}{'评分':>9}"
          f"{'信号':<18}{'风险':<5}{'支撑':>10}{'压力':>10}{'止损':>10}{'回撤%':>9}")
    print("-" * 108)
    for r in sorted(data["items"], key=lambda x: -x["total"]):
        L = r["levels"]
        print(f"{r['symbol']:<8}{r['alias']:<14}{r['kind']:<7}{fmt(r['price'],3):>10}"
              f"{fmt(r['pct'],2):>9}{r['total']:>+9.1f}  {r['signal']:<18}"
              f"{r['risk_level']:<5}{fmt(L.get('support'),3):>10}{fmt(L.get('resistance'),3):>10}"
              f"{fmt(L.get('stop'),3):>10}{fmt(L.get('drawdown_pct'),1):>9}")
    if data["errors"]:
        print("\n失败标的：", data["errors"])
    print("=" * 108)
    print("说明：个股沿用比亚迪五维模型；ETF 用价格历史分位替代估值分位。"
          "情景与点位均为历史数据推演，不是目标价预测。")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# HTML 看板（单页多 tab + 下拉框，图表同源离线）
# ---------------------------------------------------------------------------
PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>多标的投研看板 · __TIME__</title>
__ECHARTS__
<style>
:root{--bg:#f5f7fa;--card:#fff;--ink:#1c2430;--sub:#667085;--line:#e4e8ef;
--up:#d64545;--down:#2e9e6b;--hold:#8a6d3b;--accent:#2f5fd0;--soft:#eef2f9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1400px;margin:0 auto;padding:22px 20px 60px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:26px 0 12px}
h3{font-size:15px;margin:18px 0 10px}p{margin:6px 0}
.small{font-size:12px;color:var(--sub)}.muted{color:var(--sub)}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;
box-shadow:0 1px 3px rgba(20,30,50,.04)}
.btn{border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:8px;
padding:7px 14px;cursor:pointer;font-size:13px}.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.picker{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
select{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;
color:var(--ink);font-size:14px;min-width:230px}
.chips{display:flex;gap:6px;overflow-x:auto;padding:4px 0;max-width:100%}
.chip{white-space:nowrap;border:1px solid var(--line);background:#fff;border-radius:20px;
padding:5px 12px;font-size:12px;cursor:pointer;color:var(--sub)}
.chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:12px}
.tile{border:1px solid var(--line);border-radius:10px;padding:13px;background:#fff;cursor:pointer;
transition:transform .12s,box-shadow .12s}.tile:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(30,50,90,.10)}
.tile .nm{font-weight:600;font-size:14px}.tile .cd{color:var(--sub);font-size:12px}
.tile .pr{font-size:20px;font-weight:700;margin:6px 0 2px}
.tile .sg{font-size:12px;padding:2px 8px;border-radius:12px;display:inline-block;margin-top:6px}
.up{color:var(--up)}.down{color:var(--down)}
.sg-buy{background:#fdecec;color:var(--up)}.sg-hold{background:#f7f2e7;color:var(--hold)}
.sg-sell{background:#e8f6ef;color:var(--down)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:14px 0}
.kpi{background:var(--soft);border-radius:9px;padding:10px 12px}
.kpi .k{font-size:11px;color:var(--sub)}.kpi .v{font-size:17px;font-weight:700;margin-top:2px}
.hero{display:flex;flex-wrap:wrap;gap:18px;align-items:center;
background:linear-gradient(135deg,#fdfefe,#eef3fb);border:1px solid var(--line);border-radius:12px;padding:18px}
.hero .score{font-size:42px;font-weight:800;line-height:1}
.badge{padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--sub);font-weight:600;background:#fafbfe}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.chart{height:340px;width:100%}.chart-sm{height:250px;width:100%}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.two{grid-template-columns:1fr}.chart{height:300px}}
.box{border-left:3px solid var(--accent);background:#f8faff;padding:12px 14px;border-radius:0 8px 8px 0;margin:10px 0}
.warn{border-left-color:#e0a22c;background:#fffaf0}
.risk-hi{color:var(--up);font-weight:700}.risk-mid{color:var(--hold);font-weight:700}
.risk-lo{color:var(--down);font-weight:700}
footer{margin-top:28px;padding:16px;border:1px solid var(--line);border-radius:10px;
background:#fff;color:var(--sub);font-size:12px}
[hidden]{display:none!important}
</style></head><body><div class="wrap">
<header>
 <div><h1>多标的投研看板</h1>
 <div class="small">数据时点 __ASOF__ ｜ 生成 __TIME__ ｜ 成功 __COUNT__ 个标的 ｜
 个股沿用比亚迪五维模型，ETF 以价格历史分位替代估值分位</div></div>
 <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
  <input id="pfToken" type="password" placeholder="刷新令牌（可选）" autocomplete="off"
   style="padding:7px 10px;border:1px solid var(--line);border-radius:8px;font-size:13px;width:165px">
  <button class="btn" id="btnRefresh" onclick="refresh()">立即刷新数据</button>
  <span class="small" id="refreshMsg"></span></div>
</header>
<nav class="tabs" id="mainTabs">
 <button class="btn on" data-tab="overview">总览（__COUNT__）</button>
 <button class="btn" data-tab="detail">标的详情</button>
 <button class="btn" data-tab="risk">风险与买点清单</button>
 <button class="btn" data-tab="method">口径与来源</button>
</nav>

<section id="tab-overview">
 <div class="card"><h2 style="margin-top:0">综合评分排序</h2>
  <div id="chartOverview" class="chart"></div></div>
 <h2>全部标的</h2><div class="grid" id="tiles"></div>
</section>

<section id="tab-detail" hidden>
 <div class="card">
  <div class="picker">
   <select id="symbolSelect" onchange="selectSymbol(this.value)"></select>
   <span class="small">下拉框与下方标签联动，点击总览卡片也可直达</span>
  </div>
  <div class="chips" id="chips"></div>
 </div>
 <div id="detail"></div>
</section>

<section id="tab-risk" hidden>
 <div class="card"><h2 style="margin-top:0">风险点位与条件化买入建议</h2>
 <p class="small">支撑 / 压力 / 止损均由前复权日 K 与均线、布林带计算，为历史区间推演，不是目标价预测；
 止损位取「支撑下方 1.5%」与「现价 −8%」中更靠近现价者。</p>
 <div style="overflow-x:auto"><table id="riskTable"></table></div></div>
</section>

<section id="tab-method" hidden>
 <div class="card"><h2 style="margin-top:0">分析口径</h2>
 <div class="box"><b>个股（__STOCK_COUNT__ 只）</b>：沿用比亚迪五维模型 ——
 估值 25%（PE/PB/PS 历史分位）、成长质量 25%（单季同比/ROE/毛利率）、
 趋势技术 25%、资金情绪 15%、风险 10%。</div>
 <div class="box"><b>ETF（__ETF_COUNT__ 只）</b>：ETF 无 PE/PB 与财报，改用
 价格位置 25%（近 3 年收盘价分位 + 52 周区间 + 回撤）、趋势技术 30%、
 波动回撤 20%、资金情绪 15%（主力净额 / 同期成交额）、短期动量 10%。</div>
 <div class="box warn"><b>两道闸门</b>：① <b>趋势闸门</b>（沿用比亚迪）——
 趋势分与资金分同时 &lt; −20 时，即使总分达标也把「买入」降级为观望；
 ② <b>风险闸门</b>（本看板新增）——风险等级为「高」且趋势分 &lt; 0 时，
 同样不允许输出买入，避免在空头排列 + 高波动 + 深度回撤的位置逆势建仓。
 被降级后仍会给出条件化买点、仓位与止损，只是信号标记为「观望（等右侧确认）」。</div>
 <h3>数据源</h3><p class="small">腾讯行情（实时报价 / PE / PB / 52 周高低）、
 东方财富 push2（交叉校验）、push2his（前复权日 K、每日主力资金流）、
 datacenter（个股历史每日 PE/PB/PS、季度财报、资产负债表）。所有数值运行时抓取，代码不内置行情数字。</p>
 <h3>数据时点</h3><p class="small">__ASOF__；周末与节假日沿用最近交易日收盘数据，页面明确标注。</p>
 </div>
</section>

<footer><b>免责声明</b><br>__DISC__</footer>
</div>
<script>
const P = __DATA__;
const ITEMS = P.items || [];
const charts = {};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n = (v, d) => (v === null || v === undefined || Number.isNaN(Number(v))) ? 'N/A' : Number(v).toFixed(d === undefined ? 2 : d);
const cls = (v) => Number(v) > 0 ? 'up' : (Number(v) < 0 ? 'down' : '');
const tagCls = (t) => t === 'buy' ? 'sg-buy' : (t === 'sell' ? 'sg-sell' : 'sg-hold');
const riskCls = (l) => l === '高' ? 'risk-hi' : (l === '中' ? 'risk-mid' : 'risk-lo');
const byCode = (c) => ITEMS.find(x => x.symbol === c) || ITEMS[0];

function chart(id, option) {
  if (!window.echarts) return;
  if (!charts[id]) charts[id] = echarts.init($(id));
  charts[id].setOption(option, true);
}

function showTab(name) {
  ['overview','detail','risk','method'].forEach(t => $('tab-' + t).hidden = (t !== name));
  document.querySelectorAll('#mainTabs .btn').forEach(b =>
    b.classList.toggle('on', b.dataset.tab === name));
  if (name === 'overview') drawOverview();
  if (name === 'risk') drawRisk();
  if (name === 'detail') drawDetail();
  setTimeout(() => Object.values(charts).forEach(c => c.resize()), 30);
}

function drawOverview() {
  const sorted = ITEMS.slice().sort((a,b) => b.total - a.total);
  chart('chartOverview', {
    animation:false,
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},
      formatter:(p)=>{const r=sorted[p[0].dataIndex];
        return `${esc(r.alias)}（${r.symbol}）<br/>综合评分 ${n(r.total,1)}<br/>${esc(r.signal)}<br/>风险 ${esc(r.risk_level)}`}},
    grid:{left:120,right:40,top:10,bottom:26},
    xAxis:{type:'value',min:-100,max:100,splitLine:{lineStyle:{color:'#e9edf4'}}},
    yAxis:{type:'category',data:sorted.map(r=>r.alias),axisLabel:{color:'#475467',fontSize:11}},
    series:[{type:'bar',data:sorted.map(r=>({value:r.total,
      itemStyle:{color:r.tag==='buy'?'#d64545':(r.tag==='sell'?'#2e9e6b':'#8a94a6'),borderRadius:3}})),
      barWidth:'62%',label:{show:true,position:'right',formatter:(p)=>n(p.value,1),fontSize:10,color:'#475467'}}]
  });
  $('tiles').innerHTML = sorted.map(r => `
    <div class="tile" onclick="goDetail('${r.symbol}')">
      <div class="nm">${esc(r.alias)}</div>
      <div class="cd">${r.symbol} · ${esc(r.kind)} · ${esc(r.sector)}</div>
      <div class="pr ${cls(r.pct)}">${n(r.price,3)}</div>
      <div class="small ${cls(r.pct)}">${Number(r.pct)>0?'+':''}${n(r.pct,2)}%
        ｜ 评分 <b>${n(r.total,1)}</b> ｜ 风险 <span class="${riskCls(r.risk_level)}">${esc(r.risk_level)}</span></div>
      <div class="sg ${tagCls(r.tag)}">${esc(r.signal)}</div>
    </div>`).join('');
}

function goDetail(code){ $('symbolSelect').value = code; current = code; showTab('detail'); }

function drawRisk() {
  const rows = ITEMS.slice().sort((a,b)=>b.total-a.total).map(r=>{
    const L=r.levels, A=r.advice;
    return `<tr><td><b>${esc(r.alias)}</b><br><span class="small">${r.symbol} · ${esc(r.kind)}</span></td>
      <td class="num ${cls(r.pct)}">${n(r.price,3)}<br><span class="small">${n(r.pct,2)}%</span></td>
      <td class="num"><b>${n(r.total,1)}</b></td>
      <td><span class="sg ${tagCls(r.tag)}">${esc(r.signal)}</span><br>
        <span class="small ${riskCls(r.risk_level)}">风险 ${esc(r.risk_level)}</span></td>
      <td class="num">${n(L.support,3)}<br><span class="small">52周低 ${n(L.support2,3)}</span></td>
      <td class="num">${n(L.resistance,3)}<br><span class="small">52周高 ${n(L.resistance2,3)}</span></td>
      <td class="num"><b>${n(L.stop,3)}</b></td>
      <td class="num">${n(L.drawdown_pct,1)}%<br><span class="small">年化波动 ${n(L.vol_annual,1)}%</span></td>
      <td><b>${esc(A.action)}</b><br><span class="small">${esc(A.position)}</span></td></tr>`;
  }).join('');
  $('riskTable').innerHTML = `<thead><tr><th>标的</th><th class="num">现价</th><th class="num">评分</th>
    <th>信号 / 风险</th><th class="num">支撑</th><th class="num">压力</th><th class="num">参考止损</th>
    <th class="num">回撤 / 波动</th><th>买入建议</th></tr></thead><tbody>${rows}</tbody>`;
}

let current = ITEMS.length ? ITEMS[0].symbol : '';

function drawDetail() {
  const r = byCode(current); if (!r) return;
  document.querySelectorAll('#chips .chip').forEach(c => c.classList.toggle('on', c.dataset.code === current));
  const L = r.levels, A = r.advice, C = r.chart;
  const scoreRows = Object.values(r.scores).map(s => `
    <tr><td><b>${esc(s.label)}</b> <span class="small">×${n(s.weight*100,0)}%</span></td>
    <td class="num ${cls(s.score)}"><b>${n(s.score,1)}</b></td>
    <td class="num">${n(s.weighted,1)}</td>
    <td class="small">${s.detail.map(esc).join('<br>')}</td></tr>`).join('');
  $('detail').innerHTML = `
   <div class="card" style="margin-top:14px">
    <div class="hero">
      <div><div class="small">${esc(r.name)}（${r.symbol}） · ${esc(r.kind)} · ${esc(r.sector)}</div>
        <div style="font-size:30px;font-weight:700" class="${cls(r.pct)}">${n(r.price,3)}
          <span style="font-size:16px">${Number(r.pct)>0?'+':''}${n(r.pct,2)}%</span></div>
        <div class="small">数据时点 ${esc(r.asof)} ｜ ${esc(r.model)}</div></div>
      <div><div class="small">综合评分（±100）</div><div class="score ${cls(r.total)}">${n(r.total,1)}</div></div>
      <div><span class="badge ${tagCls(r.tag)}">${esc(r.signal)}</span>
        <div class="small" style="margin-top:8px">风险等级
          <b class="${riskCls(r.risk_level)}">${esc(r.risk_level)}</b></div>
        ${r.gated?'<div class="small">闸门已触发：买入降级为观望（等右侧确认）</div>':''}</div>
    </div>
    <div class="kpis">
      <div class="kpi"><div class="k">PE-TTM / 价格分位</div><div class="v">${r.pe_ttm?n(r.pe_ttm,2)+'x':n(L.price_pct3,1)+'%'}</div></div>
      <div class="kpi"><div class="k">PB</div><div class="v">${r.pb?n(r.pb,2)+'x':'N/A'}</div></div>
      <div class="kpi"><div class="k">52周区间位置</div><div class="v">${n(L.pos52,1)}%</div></div>
      <div class="kpi"><div class="k">距52周高点</div><div class="v down">${n(L.drawdown_pct,1)}%</div></div>
      <div class="kpi"><div class="k">年化波动率</div><div class="v">${n(L.vol_annual,1)}%</div></div>
      <div class="kpi"><div class="k">近250日最大回撤</div><div class="v">${n(L.max_drawdown_pct,1)}%</div></div>
      <div class="kpi"><div class="k">MA20 / MA60</div><div class="v" style="font-size:14px">${n(L.ma20,3)} / ${n(L.ma60,3)}</div></div>
      <div class="kpi"><div class="k">ATR / 现价</div><div class="v">${n(L.atr_pct,2)}%</div></div>
    </div>
   </div>
   <div class="card" style="margin-top:14px"><h2 style="margin-top:0">风险点位</h2>
    <div class="two">
      <div><table><thead><tr><th>项目</th><th class="num">数值</th><th>说明</th></tr></thead><tbody>
        <tr><td>最近支撑</td><td class="num"><b>${n(L.support,3)}</b></td><td class="small">MA20 / 近20日低点 / 布林下轨 中最近者</td></tr>
        <tr><td>次级支撑</td><td class="num">${n(L.support2,3)}</td><td class="small">近 250 日最低（前复权）</td></tr>
        <tr><td>最近压力</td><td class="num"><b>${n(L.resistance,3)}</b></td><td class="small">MA60 / 近20日高点 / 布林上轨 中最近者</td></tr>
        <tr><td>次级压力</td><td class="num">${n(L.resistance2,3)}</td><td class="small">近 250 日最高（前复权）</td></tr>
        <tr><td>参考止损</td><td class="num up"><b>${n(L.stop,3)}</b></td><td class="small">支撑下方 1.5% 与现价 −8% 取近者</td></tr>
      </tbody></table></div>
      <div><h3>风险成因</h3><div class="box warn">${L.risk_reasons.map(esc).join('<br>')}</div>
        <h3>逻辑失效条件</h3><div class="box">${esc(A.invalidate)}</div></div>
    </div>
   </div>
   <div class="card" style="margin-top:14px"><h2 style="margin-top:0">买入建议（条件化）</h2>
    <div class="box"><b>${esc(A.action)}</b><br>${esc(A.trigger)}<br>
      <span class="small">仓位：${esc(A.position)}</span></div>
    <div class="box warn"><b>止损纪律</b><br>${esc(A.stop)}</div>
    <p class="small">${esc(A.note)}</p>
   </div>
   <div class="card" style="margin-top:14px"><h2 style="margin-top:0">K 线 / 均线 / 成交量</h2>
     <div id="chartK" class="chart"></div>
     <p class="small">前复权日 K，最近 120 个交易日。A 股习惯：红涨绿跌。</p></div>
   <div class="two" style="margin-top:14px">
     <div class="card"><h2 style="margin-top:0">五维评分</h2><div id="chartRadar" class="chart-sm"></div></div>
     <div class="card"><h2 style="margin-top:0">主力资金流（近30日 / 亿元）</h2><div id="chartFlow" class="chart-sm"></div></div>
   </div>
   <div class="card" style="margin-top:14px"><h2 style="margin-top:0">评分明细（逐项留痕）</h2>
     <table><thead><tr><th>维度</th><th class="num">得分</th><th class="num">加权</th><th>依据</th></tr></thead>
     <tbody>${scoreRows}</tbody></table>
     ${r.warnings.length?'<div class="box warn"><b>数据缺口</b><br>'+r.warnings.map(esc).join('<br>')+'</div>':''}
   </div>`;
  drawCharts(r);
}

function drawCharts(r){
  const C = r.chart;
  chart('chartK', {
    animation:false,
    tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
    legend:{data:['K线','MA20','MA60','成交量'],top:0,textStyle:{fontSize:11}},
    grid:[{left:58,right:18,top:30,height:'56%'},{left:58,right:18,top:'72%',height:'19%'}],
    xAxis:[{type:'category',data:C.dates,axisLabel:{fontSize:10,color:'#667085'},boundaryGap:true},
           {type:'category',gridIndex:1,data:C.dates,axisLabel:{show:false}}],
    yAxis:[{scale:true,splitLine:{lineStyle:{color:'#eef1f6'}},axisLabel:{fontSize:10}},
           {scale:true,gridIndex:1,splitNumber:2,axisLabel:{fontSize:10}}],
    series:[
      {name:'K线',type:'candlestick',data:C.ohlc,
       itemStyle:{color:'#d64545',color0:'#2e9e6b',borderColor:'#d64545',borderColor0:'#2e9e6b'}},
      {name:'MA20',type:'line',data:C.ma20,smooth:true,showSymbol:false,lineStyle:{width:1.4,color:'#e0952f'}},
      {name:'MA60',type:'line',data:C.ma60,smooth:true,showSymbol:false,lineStyle:{width:1.4,color:'#2f5fd0'}},
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:C.vols,
       itemStyle:{color:(p)=>C.ohlc[p.dataIndex][1]>=C.ohlc[p.dataIndex][0]?'#d64545':'#2e9e6b'}}
    ]
  });
  const sc = Object.values(r.scores);
  chart('chartRadar', {
    animation:false,
    tooltip:{},
    radar:{indicator:sc.map(s=>({name:s.label,min:-100,max:100})),radius:'64%',
      axisName:{fontSize:11,color:'#475467'},splitLine:{lineStyle:{color:'#e4e8ef'}},
      splitArea:{areaStyle:{color:['#fbfcfe','#f4f7fc']}}},
    series:[{type:'radar',data:[{value:sc.map(s=>s.score),name:'评分',
      areaStyle:{color:'rgba(47,95,208,.18)'},lineStyle:{color:'#2f5fd0',width:2},
      itemStyle:{color:'#2f5fd0'}}]}]
  });
  chart('chartFlow', {
    animation:false,
    tooltip:{trigger:'axis'},
    grid:{left:52,right:16,top:16,bottom:46},
    xAxis:{type:'category',data:C.flowDates,axisLabel:{fontSize:9,rotate:45,color:'#667085'}},
    yAxis:{type:'value',splitLine:{lineStyle:{color:'#eef1f6'}},axisLabel:{fontSize:10}},
    series:[{type:'bar',data:C.flowMain.map(v=>({value:v,
      itemStyle:{color:v>=0?'#d64545':'#2e9e6b'}})),barWidth:'62%'}]
  });
}

function selectSymbol(code){ current = code; $('symbolSelect').value = code; drawDetail(); }

async function refresh(){
  const btn = $('btnRefresh'); const msg = $('refreshMsg');
  btn.disabled = true; msg.textContent = ' 正在后台抓取，请稍候…';
  try {
    const tok = $('pfToken') ? $('pfToken').value.trim() : '';
    const headers = tok ? {'X-Refresh-Token': tok} : {};
    const r = await fetch('/api/portfolio/refresh', {method:'POST', headers:headers});
    const j = await r.json();
    if (r.status === 401) { msg.textContent = ' 刷新令牌无效，请在输入框填写 REFRESH_TOKEN'; }
    else { msg.textContent = j.started ? ' 已启动，约 1 分钟后刷新页面' :
           (j.message || ' 已有刷新任务在运行'); }
  } catch(e) { msg.textContent = ' 刷新接口不可用（独立 HTML 模式请重跑脚本）'; }
  finally { btn.disabled = false; setTimeout(()=>msg.textContent='',8000); }
}

function init(){
  const sorted = ITEMS.slice().sort((a,b)=>b.total-a.total);
  $('symbolSelect').innerHTML = ITEMS.map(r=>
    `<option value="${r.symbol}">${esc(r.alias)}（${r.symbol}）</option>`).join('');
  $('chips').innerHTML = ITEMS.map(r=>
    `<span class="chip" data-code="${r.symbol}" onclick="selectSymbol('${r.symbol}')">${esc(r.alias)}</span>`).join('');
  document.querySelectorAll('#mainTabs .btn').forEach(b =>
    b.onclick = () => showTab(b.dataset.tab));
  if (current) $('symbolSelect').value = current;
  showTab('overview');
  window.addEventListener('resize', () => Object.values(charts).forEach(c=>c.resize()));
}
init();
</script></body></html>"""


def render_page(data, standalone=False):
    echarts = engine.echarts_script_tag() if standalone else \
        '<script src="/static/echarts.min.js"></script>'
    etf = sum(1 for r in data["items"] if r["kind"] == "ETF")
    stock = data["count"] - etf
    repl = {
        "__TIME__": html_mod.escape(data["generated_at"]),
        "__ASOF__": html_mod.escape(data.get("asof") or "N/A"),
        "__COUNT__": str(data["count"]),
        "__ETF_COUNT__": str(etf), "__STOCK_COUNT__": str(stock),
        "__DISC__": html_mod.escape(DISCLAIMER),
        "__ECHARTS__": echarts,
        "__DATA__": json.dumps(engine.clean_json(data), ensure_ascii=False, allow_nan=False),
    }
    out = PAGE_HTML
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def write_standalone(data):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / f"portfolio-{engine.now_cn():%Y%m%d}.html"
    p.write_text(render_page(data, standalone=True), encoding="utf-8")
    return p


def main():
    ap = argparse.ArgumentParser(description="多标的投研看板")
    ap.add_argument("--once", action="store_true", help="抓数并生成独立 HTML 看板")
    ap.add_argument("--summary", action="store_true", help="只打印汇总表")
    ap.add_argument("--serve", action="store_true", help="独立启动看板服务")
    ap.add_argument("--workers", type=int, default=5, help="并发抓取线程数")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--cached", action="store_true", help="用已有缓存渲染，不重新抓数")
    args = ap.parse_args()

    if args.cached:
        data = load_cached()
        if not data:
            print("无缓存，请先运行 python portfolio.py --once")
            return 1
    else:
        print(f"开始抓取 {len(UNIVERSE)} 个标的（并发 {args.workers}）…")
        data = run_all(workers=args.workers, quiet=False)
        print(f"完成：成功 {data['count']}，失败 {data['failed']}，耗时 {data['elapsed']}s")

    print_summary(data)
    if args.once or args.cached:
        p = write_standalone(data)
        print(f"\n独立看板：{p}")
    if args.serve:
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        page = render_page(data, standalone=False).encode("utf-8")
        lib = engine.ECHARTS_LOCAL.read_bytes() if engine.ECHARTS_LOCAL.exists() else b""

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/static/"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript")
                    self.end_headers(); self.wfile.write(lib); return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers(); self.wfile.write(page)

            def log_message(self, *a):
                pass

        print(f"\n看板服务：http://127.0.0.1:{args.port}/")
        ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
