#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多标的投研看板（比亚迪 + 16 个 ETF / 个股，共 17 个标的）
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
# 最近一次「完整成功」的备份、最近一次失败诊断、随仓库分发的内置离线快照。
# 云服务器首次部署 / 数据源临时不可达时，页面至少能显示一份标注清楚的旧数据。
LAST_GOOD_JSON = DATA_DIR / "portfolio_last_good.json"
REFRESH_ERROR_JSON = DATA_DIR / "portfolio_errors.json"
SEED_JSON = ROOT / "deploy" / "seed" / "portfolio-snapshot.json"

# symbol, market, kind, 简称, 板块
UNIVERSE = [
    ("002594", "sz", "STOCK", "比亚迪",       "新能源车"),
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
# 研报级证据包：让每个标的都保留与比亚迪全景研报同等级的可追溯依据
# ---------------------------------------------------------------------------
ETF_NO_FUNDAMENTALS = (
    "ETF 是基金份额而非上市公司，本身不披露个股口径的 PE/PB、营业收入、归母净利与资产负债表；"
    "东方财富 RPT_VALUEANALYSIS_DET / RPT_LICO_FN_CPD 对 ETF 无有效记录。"
    "因此本看板不把空数据伪装成 0 分，也不套用个股财报模型：改用近 3 年前复权价格分位替代估值分位，"
    "并以波动回撤、量能、主力资金占同期成交额比、短期动量构成五维。"
    "ETF 另有的跟踪误差、折溢价、成份集中度与申赎流动性风险，历史行情无法完全刻画，"
    "需另查基金定期报告与交易所申赎清单，本页不给出未经核实的估算值。"
)


def _r(v, d=4):
    """统一浮点精度，避免页面出现长小数。"""
    return round(v, d) if isinstance(v, float) and math.isfinite(v) else v


def _sample(seq, cap):
    """等间隔降采样，保留末点，控制内嵌 JSON 体积。"""
    seq = list(seq)
    if len(seq) <= cap:
        return seq
    step = math.ceil(len(seq) / cap)
    out = seq[::step]
    if out[-1] is not seq[-1]:
        out.append(seq[-1])
    return out


def _chg(closes, n):
    """由前复权日 K 自算的区间涨跌幅（%）。"""
    if len(closes) <= n or not closes[-n - 1]:
        return None
    return (closes[-1] / closes[-n - 1] - 1) * 100


def _yi(v):
    return _r((v or 0) / 1e8, 4)


def build_scenarios(ctx, kind, kl, price):
    """情景区间：个股用 PE 分位 × EPS(TTM)，ETF 用近 3 年收盘价分位。"""
    if kind == "STOCK":
        plan = ctx.get("plan") or {}
        eps = plan.get("eps_ttm")
        out = []
        for s in plan.get("scenarios", []):
            pe, pv = s.get("pe"), s.get("price")
            if pe is None or pv is None:
                continue
            out.append({
                "label": s["label"],
                "basis": f"PE {pe:.2f}x × EPS(TTM) {fmt(eps, 3)} 元",
                "price": _r(pv, 4),
                "delta": _r((pv / price - 1) * 100, 2) if price else None,
            })
        note = ("情景价 = 历史 PE 分位 × 当前 EPS(TTM)，为估值区间推演，不是目标价预测；"
                "EPS 不变假设下才成立，盈利下修时整条区间同步下移。")
        sq = (ctx.get("singles") or [])[-1] if ctx.get("singles") else {}
        jump = sq.get("np_yoy")
        extreme = any(abs(s["delta"] or 0) > 150 for s in out)
        if extreme or (jump is not None and jump > 100):
            note += (f" ⚠️ 本次最新单季（{sq.get('year','')}Q{sq.get('q','')}）归母净利同比 "
                     f"{fmt(jump, 1, '%')}，当前 PE 处于历史极低分位；历史 PE 中枢对应的是旧盈利水平，"
                     "把它直接套到当前 EPS 上会显著放大情景价。请同步参考下方「近 3 年价格分位」交叉锚，"
                     "并核实盈利可持续性，切勿把该区间当作目标价。")
        return out, note
    seg = [x["close"] for x in kl[-750:] if x.get("close")]
    out = []
    for label, q in (("低位 25% 分位", 0.25), ("中枢 50% 分位", 0.50), ("高位 75% 分位", 0.75)):
        v = engine.quantile(seg, q) if seg else None
        if v is None:
            continue
        out.append({
            "label": label,
            "basis": "近 3 年前复权收盘价分位（不含盈利假设）",
            "price": _r(v, 4),
            "delta": _r((v / price - 1) * 100, 2) if price else None,
        })
    return out, "情景价 = 近 3 年历史价格分位对应的成交密集价位，是区间推演，不是目标价预测；ETF 无 EPS，不使用 PE 情景。"


def build_narrative(ctx, kind, price, scores, total, decision, levels, flow_ev, ph):
    """逐标的生成结论叙述：只陈述本次抓到的数字，不套用比亚迪的模板文案。"""
    q, ind = ctx["quote"], ctx["ind"]
    sc = {k: v for k, v in scores.items()}
    head = (f"截至 {q.get('asof', 'N/A')}，{q.get('name', '')} 报 {fmt(price, 3)} 元，"
            f"当日 {fmt(q.get('pct'), 2, '%')}；综合评分 {total:+.2f} 分（-100~+100），"
            f"结论「{decision['signal']}」，风险等级「{levels['risk_level']}」。")
    dims = "、".join(f"{v['label']} {v['score']:+.1f}" for v in sc.values())
    bits = [head, f"五维得分：{dims}。"]
    if kind == "STOCK":
        vs = ctx["val_stat"]
        if vs.get("pe_pct") is not None:
            bits.append(f"PE-TTM {fmt(q.get('pe_ttm'), 2, 'x')} 处于近 {vs['years']} 年 "
                        f"{vs['pe_pct']:.1f}% 分位（中位数 {fmt(vs.get('pe_med'), 2, 'x')}），"
                        f"PB 分位 {fmt(vs.get('pb_pct'), 1, '%')}。")
        sq = ctx["singles"][-1] if ctx.get("singles") else {}
        if sq.get("np_yoy") is not None:
            bits.append(f"最新单季（{sq['year']}Q{sq['q']}）归母净利 {fmt(sq['np'] / 1e8, 2)} 亿元、"
                        f"同比 {sq['np_yoy']:+.2f}%，营收同比 {fmt(sq.get('rev_yoy'), 2, '%')}。")
    else:
        bits.append(f"现价处于近 3 年口径价格 {fmt(levels.get('price_pct3'), 1, '%')} 分位"
                    f"（实际样本 {ph.get('bars')} 根 / 约 {fmt(ph.get('span_years'), 1)} 年，"
                    f"{ph.get('start')} ~ {ph.get('end')}）、"
                    f"52 周区间 {fmt(levels.get('pos52'), 1, '%')} 位置，"
                    f"距 52 周高点 {fmt(levels.get('drawdown_pct'), 1, '%')}，"
                    f"年化波动率 {fmt(levels.get('vol_annual'), 1, '%')}。")
        bits.append(f"ETF 无 PE/PB 与财报，估值维度以价格历史分位替代；"
                    f"近 20 日涨跌 {fmt(q.get('chg_20d'), 2, '%')}，实时量比 {fmt(q.get('volume_ratio'))}。")
    bits.append(f"主力资金近 5 日净额 {fmt(flow_ev['sum5_yi'], 2, ' 亿元')}、"
                f"近 20 日 {fmt(flow_ev['sum20_yi'], 2, ' 亿元')}"
                f"（占同期成交额 {fmt(flow_ev['sum20_pct'], 2, '%')}）；"
                f"MA20 {fmt(ind.get('ma20'))} / MA60 {fmt(ind.get('ma60'))}，"
                f"RSI6 {fmt(ind.get('rsi6'), 1)}，MACD 柱 {fmt(ind.get('macd_hist'), 3)}。")
    if decision["tag"] == "hold":
        bits.append("当前矛盾是「位置/估值不贵但量价与资金仍在左侧」：右侧确认前不追买，"
                    "已有仓位按止损纪律持有，空仓者等待站上 MA20 且资金转正。")
    elif decision["tag"] == "buy":
        bits.append("多维共振已达标，但仍按条件化触发分批建仓，不在信号日一次性追高。")
    else:
        bits.append("趋势与资金同时偏弱，优先控制回撤，收复关键压力位前不逆势加仓。")
    if decision.get("gated"):
        bits.append("【闸门】趋势与资金同时转弱或风险等级偏高，买入信号已被强制降级为观望。")
    if levels.get("risk_reasons"):
        bits.append("风险成因：" + "；".join(levels["risk_reasons"]) + "。")
    return "".join(bits)


def build_evidence(ctx, kind, price, levels, advice, total, decision, scores):
    """把 analyze() 的中间结果完整打包，供详情页逐项追溯。"""
    q, ind, kl = ctx["quote"], ctx["ind"], ctx["kline"]
    closes = [x["close"] for x in kl]
    flow = ctx["flow"][-30:]
    val, fins, bal = ctx["valuation"], ctx["financials"], ctx["balance"]
    vs = ctx["val_stat"]

    quote = {k: _r(q.get(k)) for k in (
        "name", "asof", "price", "prev_close", "open", "high", "low", "pct", "change",
        "amount_wan", "turnover", "volume_ratio", "amplitude", "pe_ttm", "pb",
        "pe_static", "pe_fwd", "total_mv_yi", "float_mv_yi", "high52", "low52",
        "chg_5d", "chg_10d", "chg_20d", "chg_60d", "chg_ytd", "avg_price",
        "limit_up", "limit_down", "volume_lots",
    )}
    quote["amount_yi"] = _yi(q.get("amount_wan") and q["amount_wan"] * 1e4)

    if kind == "STOCK":
        valuation = {
            "available": bool(val), "years": vs.get("years"), "samples": vs.get("samples"),
            "pe_ttm": _r(q.get("pe_ttm")), "pe_pct": _r(vs.get("pe_pct"), 2),
            "pe_pct3": _r(vs.get("pe_pct3"), 2), "pe_med": _r(vs.get("pe_med")),
            "pe_sd": _r(vs.get("pe_sd")), "pe_min": _r(vs.get("pe_min")),
            "pe_max": _r(vs.get("pe_max")), "pe_quantiles": engine.clean_json(vs.get("pe_quantiles") or {}),
            "pb": _r(q.get("pb")), "pb_pct": _r(vs.get("pb_pct"), 2),
            "pb_med": _r(vs.get("pb_med")), "ps_pct": _r(vs.get("ps_pct"), 2),
            "peg": _r(vs.get("peg")), "fwd_pe": _r(vs.get("fwd_pe")),
            "eps_ttm": _r((ctx.get("plan") or {}).get("eps_ttm")),
            "start": val[0]["date"] if val else None, "end": val[-1]["date"] if val else None,
            "series": [{"d": x["date"], "pe": _r(x["pe_ttm"], 3), "pb": _r(x["pb"], 3),
                        "ps": _r(x["ps"], 3)} for x in _sample(val, 320)],
        }
    else:
        valuation = {"available": False, "reason": ETF_NO_FUNDAMENTALS, "series": []}

    technical = {k: _r(v) for k, v in ind.items()}
    prev = ctx.get("ind_prev") or {}
    technical["prev"] = {k: _r(prev.get(k)) for k in (
        "date", "close", "ma20", "ma60", "macd_hist", "rsi6", "k", "d", "j", "boll_mid",
    )}
    technical["returns"] = {f"d{n}": _r(_chg(closes, n), 2) for n in (5, 20, 60, 120, 250)}
    technical["ma_stack"] = ("多头排列" if all(ind.get(a) and ind.get(b) and ind[a] > ind[b]
                                       for a, b in (("ma5", "ma10"), ("ma10", "ma20"), ("ma20", "ma60")))
                             else "空头排列" if all(ind.get(a) and ind.get(b) and ind[a] < ind[b]
                                       for a, b in (("ma5", "ma10"), ("ma10", "ma20"), ("ma20", "ma60")))
                             else "均线交织")

    bal_last = bal[-1] if bal else {}
    dr = bal_last.get("debt_ratio")
    if dr is None and bal_last.get("total_liab") and bal_last.get("total_assets"):
        dr = bal_last["total_liab"] / bal_last["total_assets"] * 100
    financials = {
        "available": bool(fins) and kind == "STOCK",
        "reason": ETF_NO_FUNDAMENTALS if kind == "ETF" else "财报数据源本次未返回有效记录，相关字段标注 N/A。",
        "latest": engine.clean_json(fins[-1]) if fins else {},
        "debt_ratio": _r(dr, 2),
        "reports": [engine.clean_json(x) for x in fins[-4:]],
        "quarters": [engine.clean_json(x) for x in ctx["singles"][-8:]],
        "balance": [engine.clean_json(x) for x in bal[-4:]],
    }

    cum, acc = [], 0.0
    for x in flow:
        acc += x.get("main_net") or 0
        cum.append(_r(acc / 1e8, 4))
    amt20 = sum((x.get("amount") or 0) for x in kl[-20:])
    s5 = sum((x.get("main_net") or 0) for x in flow[-5:])
    s20 = sum((x.get("main_net") or 0) for x in flow[-20:])
    flow_ev = {
        "dates": [x["date"] for x in flow],
        "main": [_yi(x.get("main_net")) for x in flow],
        "jumbo": [_yi(x.get("jumbo_net")) for x in flow],
        "big": [_yi(x.get("big_net")) for x in flow],
        "mid": [_yi(x.get("mid_net")) for x in flow],
        "small": [_yi(x.get("small_net")) for x in flow],
        "cum": cum,
        "sum5_yi": _r(s5 / 1e8, 4), "sum20_yi": _r(s20 / 1e8, 4),
        "sum20_pct": _r(s20 / amt20 * 100, 2) if amt20 else None,
        "start": flow[0]["date"] if flow else None,
        "end": flow[-1]["date"] if flow else None,
        "table": [{
            "d": x["date"], "main": _yi(x.get("main_net")), "jumbo": _yi(x.get("jumbo_net")),
            "big": _yi(x.get("big_net")), "mid": _yi(x.get("mid_net")),
            "small": _yi(x.get("small_net")), "pct": _r(x.get("main_pct"), 2),
            "close": _r(x.get("close")), "chg": _r(x.get("pct"), 2),
        } for x in reversed(flow[-20:])],
    }

    seg = [x for x in kl[-750:]]
    pts = _sample([(x["date"], x["close"]) for x in seg], 240)
    seg_closes = [x["close"] for x in seg]
    price_history = {
        "dates": [p[0] for p in pts], "closes": [_r(p[1], 4) for p in pts],
        "q25": _r(engine.quantile(seg_closes, 0.25), 4),
        "q50": _r(engine.quantile(seg_closes, 0.50), 4),
        "q75": _r(engine.quantile(seg_closes, 0.75), 4),
        "high": _r(max(seg_closes), 4) if seg_closes else None,
        "low": _r(min(seg_closes), 4) if seg_closes else None,
        "pct3": levels.get("price_pct3"),
        "pct1": _r(pct_rank(closes[-250:], price), 2),
        "start": seg[0]["date"] if seg else None, "end": seg[-1]["date"] if seg else None,
        "bars": len(seg), "span_years": _r(len(seg) / 244, 1),
    }

    scen, basis = build_scenarios(ctx, kind, kl, price)
    add = ((ctx.get("plan") or {}).get("add") if kind == "STOCK" else
           f"站稳 MA60（{fmt(levels.get('ma60'), 3)}）且近 5 日主力净额转正 → 加仓 20–30%；"
           f"突破近 20 日高点 {fmt(levels.get('high20'), 3)} → 再加 20–30%")
    plan = {
        "scenarios": scen, "basis": basis,
        "entry": advice["trigger"], "add": add or "数据不足，暂不给出加仓路径",
        "stop": advice["stop"], "invalidate": advice["invalidate"],
        "note": advice["note"],
    }

    ktable = [{
        "d": x["date"], "o": x["open"], "h": x["high"], "l": x["low"], "c": x["close"],
        "v": x.get("volume"), "pct": _r(x.get("pct"), 2),
        "amt": _r((x.get("amount") or 0) / 1e8, 4), "to": _r(x.get("turnover"), 2),
    } for x in reversed(kl[-40:])]

    evidence = {
        "quote": quote, "valuation": valuation, "technical": technical,
        "financials": financials, "flow": flow_ev, "price_history": price_history,
        "plan": plan, "ktable": ktable,
        "quality": {
            "sources": ctx["sources"], "cross_notes": ctx["cross_notes"],
            "warnings": ctx["warnings"],
            "coverage": {
                "kline": len(kl), "valuation": len(val), "financials": len(fins),
                "balance": len(bal), "flow": len(ctx["flow"]),
                "kline_start": kl[0]["date"], "kline_end": kl[-1]["date"],
            },
        },
    }
    evidence["narrative"] = build_narrative(ctx, kind, price, scores, total,
                                            decision, levels, flow_ev, price_history)
    return engine.clean_json(evidence)


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
            ctx["warnings"].append(
                f"{name} 数据源失败（{type(e).__name__}: {str(e)[:240]}），相关维度标注 N/A")
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
    evidence = build_evidence(ctx, kind, price, levels, advice, total, decision, scores)

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
        "narrative": evidence["narrative"],
        "evidence": evidence,
        "ok": True,
    }


def _read_json(path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return None


def _write_json(path, obj):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(engine.clean_json(obj), ensure_ascii=False),
                        encoding="utf-8")
        return True
    except (OSError, ValueError, TypeError) as e:
        print(f"[warn] 写入 {path.name} 失败：{e}", file=sys.stderr)
        return False


def run_all(workers=5, quiet=True):
    """抓取全部标的。

    关键保护：数据源整体不可达时，绝不把「0 个标的」写进主缓存。
    否则一次失败刷新就会把上一份完整数据冲掉，页面直接变成空壳
    （百度云服务器上曾出现：17/17 报「有效 K 线不足（0 根）」）。
    """
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
    fresh = {
        "generated_at": f"{engine.now_cn():%Y-%m-%d %H:%M:%S}",
        "asof": max((r["asof"] for r in results if r.get("asof")), default=None),
        "count": len(results), "failed": len(errors),
        "workers": workers, "elapsed": round(time.time() - t0, 1),
        "disclaimer": DISCLAIMER, "refresh_ok": True,
        "items": results, "errors": errors,
    }
    min_ok = max(1, math.ceil(len(UNIVERSE) * 0.5))
    if len(results) >= min_ok:
        if _write_json(PORTFOLIO_JSON, fresh):
            _write_json(LAST_GOOD_JSON, fresh)      # 保留一份完整成功快照
        try:
            REFRESH_ERROR_JSON.unlink(missing_ok=True)
        except OSError:
            pass
        return fresh

    # ---- 抓取大面积失败：只落诊断，保留已有数据 ----
    diag = {
        "generated_at": fresh["generated_at"], "total": len(UNIVERSE),
        "success": len(results), "failed": len(errors), "errors": errors,
        "hint": "行情/日K 数据源不可达。程序已启用腾讯日K回退；若仍失败，"
                "请访问 /api/diagnostics 逐源探测，确认出站网络后点击「立即刷新数据」。",
    }
    _write_json(REFRESH_ERROR_JSON, diag)
    main = _read_json(PORTFOLIO_JSON)
    base, origin, seed = None, "", False
    if main and main.get("items") and main.get("count", 0) > 0:
        base, origin = main, "上次成功缓存"
    for path, label, is_seed in ((LAST_GOOD_JSON, "上次成功备份", False),
                                 (SEED_JSON, "内置离线快照", True)):
        if base:
            break
        d = _read_json(path)
        if d and d.get("items"):
            base, origin, seed = d, label, is_seed
    if not quiet:
        print(f"[warn] 仅 {len(results)}/{len(UNIVERSE)} 个标的抓取成功，"
              f"已放弃写入主缓存；{'回退 ' + origin if base else '无可用回退数据'}")
    if base:
        stale = dict(base)
        stale.update({
            "stale": True, "seed": seed, "refresh_ok": False,
            "fallback_source": origin, "count": len(base["items"]),
            "last_attempt_at": fresh["generated_at"],
            "refresh_failed": len(errors), "refresh_errors": errors,
            "refresh_hint": diag["hint"],
        })
        return stale
    # 确实没有任何历史/内置数据，才写空主文件；页面会显示明确的失败面板
    fresh.update({"refresh_ok": False, "stale": True, "fallback_source": "无",
                  "refresh_failed": len(errors), "refresh_hint": diag["hint"]})
    _write_json(PORTFOLIO_JSON, fresh)
    return fresh


def load_cached():
    """主缓存无效时，依次回退「上次成功备份」与随仓库分发的内置离线快照。"""
    main = _read_json(PORTFOLIO_JSON)
    if main and main.get("items") and main.get("count", 0) > 0:
        return main
    for path, origin, is_seed in ((LAST_GOOD_JSON, "上次成功备份", False),
                                  (SEED_JSON, "内置离线快照", True)):
        d = _read_json(path)
        if not (d and d.get("items")):
            continue
        out = dict(d)
        out.update({
            "stale": True, "seed": is_seed, "fallback_source": origin,
            "count": len(d["items"]),
            "refresh_hint": (main or {}).get("refresh_hint")
                            or "实时抓取尚未成功，请点击「立即刷新数据」重试。",
        })
        if main:
            out["refresh_errors"] = main.get("errors") or []
            out["last_attempt_at"] = main.get("generated_at")
            out["refresh_failed"] = main.get("failed")
        return out
    return main


def print_summary(data):
    print("\n" + "=" * 108)
    print(f"  多标的投研汇总 | 数据时点 {data.get('asof')} | 生成 {data.get('generated_at')} "
          f"| 成功 {data['count']}/{data['count']+data['failed']}")
    print("=" * 108)
    if data.get("stale"):
        print(f"  注意：最近一次刷新失败（{data.get('refresh_failed', '?')} 个标的），"
              f"当前展示{data.get('fallback_source', '旧数据')}，非实时数据。")
        print(f"  {data.get('refresh_hint', '')}")
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
.scroll{max-height:430px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.scroll table{font-size:12px}.scroll th{position:sticky;top:0;z-index:1;background:#f0f3f9}
.tbl-sm{font-size:12px}.tbl-sm th,.tbl-sm td{padding:6px 8px}
.chart-mid{height:290px;width:100%}
.sect{margin-top:18px}.sect h2{margin:0 0 10px;font-size:17px}
.tagline{display:inline-block;background:#eef2f9;color:#2f5fd0;border-radius:6px;
padding:2px 8px;font-size:11px;margin-right:6px}
.hint{background:#f8faff;border-left:3px solid var(--accent);padding:10px 12px;
border-radius:0 8px 8px 0;font-size:12px;color:#475467;margin:10px 0}
.warnbox{background:#fffaf0;border-left-color:#e0a22c}
.narr{font-size:13.5px;line-height:1.85;margin:10px 0 0}
.kpi .k{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.alert{margin:0 0 14px;padding:12px 16px;border-radius:10px;border:1px solid #edd9a6;
background:#fffaf0;color:#7a5a12;font-size:13px;line-height:1.8}
.alert.err{border-color:#e9b6b6;background:#fff5f5;color:#8c2f2f}
.alert code{background:#f3f4f8;padding:1px 6px;border-radius:4px;font-size:12px}
.alert ul{margin:6px 0 0;padding-left:20px}
.empty{padding:28px 20px;text-align:center;color:#667085;font-size:13px;line-height:2}
</style></head><body><div class="wrap">
<header>
 <div><h1>多标的投研看板</h1>
 <div class="small">数据时点 __ASOF__ ｜ 生成 __TIME__ ｜ 成功 __COUNT__ 个标的（含比亚迪）｜
 每个标的均有七节研报级详情：个股沿用比亚迪五维模型，ETF 以价格历史分位替代估值分位</div></div>
 <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
  __BYD_LINK__
  <input id="pfToken" type="password" placeholder="刷新令牌（可选）" autocomplete="off"
   style="padding:7px 10px;border:1px solid var(--line);border-radius:8px;font-size:13px;width:165px">
  <button class="btn" id="btnRefresh" onclick="refresh()">立即刷新数据</button>
  <span class="small" id="refreshMsg"></span></div>
</header>
__ALERT__
<nav class="tabs" id="mainTabs">
 <button class="btn on" data-tab="overview">总览（__COUNT__）</button>
 <button class="btn" data-tab="detail">标的详情（研报级）</button>
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
 <div class="box"><b>个股（__STOCK_COUNT__ 只，含比亚迪 002594）</b>：沿用比亚迪五维模型 ——
 估值 25%（PE/PB/PS 历史分位）、成长质量 25%（单季同比/ROE/毛利率）、
 趋势技术 25%、资金情绪 15%、风险 10%。</div>
 <div class="box"><b>ETF（__ETF_COUNT__ 只）</b>：ETF 无 PE/PB 与财报，改用
 价格位置 25%（近 3 年收盘价分位 + 52 周区间 + 回撤）、趋势技术 30%、
 波动回撤 20%、资金情绪 15%（主力净额 / 同期成交额）、短期动量 10%。</div>
 <div class="box"><b>每个标的的研报级详情（七节，深度一致）</b>：① 五维评分逐项打分依据与雷达；
 ② 估值 / 价格位置与历史分位带；③ K 线图 + 近 40 日精确表 + 技术指标全表 + 多周期涨跌；
 ④ 最新财报、单季拆分、资产负债（ETF 改为替代依据清单与特有风险）；⑤ 近 20 日逐日资金分档与累计；
 ⑥ 风险点位、条件化交易计划与情景区间；⑦ 数据源接口、双源交叉校验与本次抓数覆盖。
 个股与 ETF 使用同一展示深度，不因标的类型而省略分析依据；ETF 无 PE/PB/财报时明确标注原因，不填 0、不编造数值。</div>
 <div class="box warn"><b>情景推演的局限</b>：个股情景价 = 历史 PE 分位 × 当前 EPS(TTM)。
 若最新单季净利同比大幅跳升、当前 PE 处于历史极低分位，历史 PE 中枢对应的是旧盈利水平，
 直接套用会显著放大价格；页面会对 |相对现价| &gt; 150% 的行标注「极端推演」，
 并同时给出近 3 年价格分位作为交叉锚。所有情景均为历史区间推演，不是目标价预测。</div>
 <div class="box warn"><b>两道闸门</b>：① <b>趋势闸门</b>（沿用比亚迪）——
 趋势分与资金分同时 &lt; −20 时，即使总分达标也把「买入」降级为观望；
 ② <b>风险闸门</b>（本看板新增）——风险等级为「高」且趋势分 &lt; 0 时，
 同样不允许输出买入，避免在空头排列 + 高波动 + 深度回撤的位置逆势建仓。
 被降级后仍会给出条件化买点、仓位与止损，只是信号标记为「观望（等右侧确认）」。</div>
 <h3>数据源与可用性</h3><p class="small">腾讯行情（实时报价 / PE / PB / 52 周高低）、
 东方财富 push2（交叉校验）、push2his（前复权日 K、每日主力资金流）、
 datacenter（个股历史每日 PE/PB/PS、季度财报、资产负债表）。所有数值运行时抓取，代码不内置行情数字。</p>
 <div class="box warn"><b>日 K 多源回退与失败保护</b>：日 K 依次尝试东财 HTTPS → 东财 HTTP → 东财备用节点 →
 腾讯 fqkline（前复权）→ 腾讯 kline（不复权），实际使用来源写入每个标的第七节的数据源表；
 腾讯回退仅含 OHLCV，成交额 / 换手率标 N/A 而不编造，ETF 走不复权口径时额外告警。
 若某次刷新成功标的数量不足半数，程序保留上一份完整数据、只落失败诊断，绝不把空结果覆盖到页面；
 主缓存缺失时回退到随仓库分发的内置离线快照，并在页面顶部显著标注「非实时」。
 服务端 <code>/api/diagnostics</code> 可从服务器自身网络逐源探测连通性。</div>
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

function chart(id, option, spec) {
  const el = $(id);
  if (!el) return;
  // drawDetail() 会整体替换 #detail.innerHTML。若沿用缓存的 ECharts 实例，
  // 实例仍绑定已被删除的旧 DOM 节点，切换标的后图表就会整片空白。
  // 因此每次渲染都先 dispose，再对「当前」节点重新 init。
  if (window.echarts) {
    try {
      if (charts[id]) { charts[id].dispose(); delete charts[id]; }
      charts[id] = echarts.init(el);
      charts[id].setOption(option, true);
      return;
    } catch (e) {
      try { if (charts[id]) charts[id].dispose(); } catch (_) {}
      delete charts[id];
    }
  }
  el.innerHTML = svgChart(spec, el);
}

// 无第三方库依赖的内联 SVG 绘图：图表库不可用时兜底，页面永远不会留白。
function svgChart(s, el) {
  if (!s || !s.type) return '<div style="padding:18px;color:#667085;font-size:13px">' +
    '图表数据不可用；上方表格已列出全部数值。</div>';
  const W = Math.max(360, Math.round((el && el.clientWidth) || 900));
  const H = Math.max(180, Math.round((el && el.clientHeight) ||
    (s.type === 'kline' ? 340 : 250)));
  const L = 62, R = 20, T = 20, B = 38;
  const iw = W - L - R, ih = H - T - B;
  const RED = '#d64545', GREEN = '#2e9e6b', BLUE = '#2f5fd0',
        ORANGE = '#e0952f', GREY = '#8a94a6';
  const T2 = (x, y, t, sz, anchor, color) => '<text x="' + Number(x).toFixed(1) +
    '" y="' + Number(y).toFixed(1) + '" font-size="' + (sz || 10) +
    '" fill="' + (color || '#667085') + '" text-anchor="' + (anchor || 'start') +
    '" font-family="Arial,Microsoft YaHei,sans-serif">' + esc(t) + '</text>';
  const LN = (x1, y1, x2, y2, c, w) => '<line x1="' + Number(x1).toFixed(1) +
    '" y1="' + Number(y1).toFixed(1) + '" x2="' + Number(x2).toFixed(1) +
    '" y2="' + Number(y2).toFixed(1) + '" stroke="' + (c || '#e4e8ef') +
    '" stroke-width="' + (w || 1) + '"/>';
  const RC = (x, y, w, h, c, r) => '<rect x="' + Math.max(0, Number(x)).toFixed(1) +
    '" y="' + Math.max(0, Number(y)).toFixed(1) + '" width="' + Math.max(0.6, Number(w)).toFixed(1) +
    '" height="' + Math.max(0.6, Number(h)).toFixed(1) + '" rx="' + (r || 0) +
    '" fill="' + c + '"/>';
  let g = '<rect x="0" y="0" width="' + W + '" height="' + H + '" fill="#fff"/>';

  if (s.type === 'lines') {
    const ss = (s.series || []).filter(x => x.values && x.values.length);
    const nums = [];
    ss.forEach(x => x.values.forEach(v => {
      if (v !== null && v !== undefined && isFinite(Number(v))) nums.push(Number(v));
    }));
    (s.marks || []).forEach(m => { if (isFinite(Number(m.value))) nums.push(Number(m.value)); });
    let hi = Math.max(...(nums.length ? nums : [1])), lo = Math.min(...(nums.length ? nums : [0]));
    if (hi <= lo) hi = lo + 1;
    const pad = (hi - lo) * 0.08; hi += pad; lo -= pad;
    let maxN = 1; ss.forEach(x => maxN = Math.max(maxN, x.values.length));
    const X = i => L + iw * (i / Math.max(1, maxN - 1));
    const Y = v => T + ih * (1 - (Number(v) - lo) / (hi - lo));
    for (let k = 0; k <= 4; k++) {
      const yy = T + ih * k / 4;
      g += LN(L, yy, W - R, yy, '#eef1f6');
      g += T2(L - 6, yy + 3, n(hi - (hi - lo) * k / 4, 1), 9, 'end');
    }
    (s.marks || []).forEach(m => {
      const yy = Y(m.value), c = m.color || GREY;
      g += LN(L, yy, W - R, yy, c, 1.1) + T2(W - R - 3, yy - 4, m.label, 9, 'end', c);
    });
    ss.forEach((se, si) => {
      const pts = [];
      se.values.forEach((v, i) => {
        if (v !== null && v !== undefined && isFinite(Number(v)))
          pts.push(X(i).toFixed(1) + ',' + Y(v).toFixed(1));
      });
      const c = se.color || BLUE;
      if (pts.length > 1) g += '<polyline points="' + pts.join(' ') +
        '" fill="none" stroke="' + c + '" stroke-width="1.7"/>';
      g += T2(L + si * 96, T - 6, se.name, 10, 'start', c);
    });
    const lab = s.labels || [];
    for (let k = 0; k < 6; k++) {
      const i = Math.round(k * (lab.length - 1) / 5);
      if (lab.length) g += T2(X(i), H - 10, String(lab[i] || '').slice(5), 9, 'middle');
    }
  } else if (s.type === 'combo') {
    const bs = s.bars || [], ln = s.line || null, N = Math.max(1, (s.labels || []).length);
    const step = iw / N, bw = Math.max(1, Math.min(18, step * 0.72 / Math.max(1, bs.length)));
    const av = [];
    bs.forEach(b => b.values.forEach(v => { if (isFinite(Number(v))) av.push(Math.abs(Number(v))); }));
    const m = Math.max(...(av.length ? av : [1]), 0.0001) * 1.15;
    const zy = T + ih / 2, half = ih / 2 * 0.9;
    g += LN(L, zy, W - R, zy, '#c9d2e0', 1.2);
    bs.forEach((b, bi) => b.values.forEach((v, i) => {
      const val = Number(v) || 0, h = Math.abs(val) / m * half;
      const x = L + (i + 0.5) * step + (bi - (bs.length - 1) / 2) * bw;
      g += RC(x - bw / 2, val >= 0 ? zy - h : zy, bw, h,
              b.color || (val >= 0 ? RED : GREEN), 1);
    }));
    if (ln && ln.values && ln.values.length) {
      const lv = ln.values.filter(v => v !== null && v !== undefined && isFinite(Number(v))).map(Number);
      if (lv.length) {
        let lhi = Math.max(...lv), llo = Math.min(...lv);
        if (lhi <= llo) lhi = llo + 1;
        const LY = v => T + ih * (1 - (Number(v) - llo) / (lhi - llo));
        const pts = [];
        ln.values.forEach((v, i) => {
          if (v !== null && v !== undefined && isFinite(Number(v)))
            pts.push((L + (i + 0.5) * step).toFixed(1) + ',' + LY(v).toFixed(1));
        });
        if (pts.length > 1) g += '<polyline points="' + pts.join(' ') +
          '" fill="none" stroke="' + (ln.color || BLUE) + '" stroke-width="1.8"/>';
        g += T2(L, T - 6, ln.name + '（右轴示意）', 10, 'start', ln.color || BLUE);
      }
    }
    const lab = s.labels || [];
    for (let k = 0; k < 6; k++) {
      const i = Math.round(k * (lab.length - 1) / Math.max(1, 5));
      if (lab.length) g += T2(L + (i + 0.5) * step, H - 10, String(lab[i] || '').slice(5), 9, 'middle');
    }
    g += T2(L - 6, T + 10, n(m, 1), 9, 'end') + T2(L - 6, zy - 4, '0', 9, 'end') +
         T2(L - 6, T + ih, n(-m, 1), 9, 'end');
  } else if (s.type === 'hbar') {
    const v = s.values.map(Number);
    const m = Math.max(20, ...v.map(x => Math.abs(x))) * 1.12;
    const zx = L + iw / 2, sc = (iw / 2) / m, row = ih / Math.max(1, v.length);
    for (let k = 0; k <= 4; k++) g += LN(L + iw * k / 4, T, L + iw * k / 4, T + ih, '#eef1f6');
    v.forEach((val, i) => {
      const y = T + i * row + row * 0.16, hh = row * 0.68;
      const w = Math.abs(val) * sc, x = val >= 0 ? zx : zx - w;
      g += RC(x, y, w, hh, (s.colors && s.colors[i]) || GREY, 2);
      g += T2(L - 8, y + hh * 0.78, s.labels[i], 10, 'end', '#475467');
      g += T2(val >= 0 ? Math.min(W - R - 26, x + w + 4) : Math.max(L + 2, x - 4),
              y + hh * 0.78, n(val, 1), 9, val >= 0 ? 'start' : 'end', '#475467');
    });
    g += LN(zx, T, zx, T + ih, '#c9d2e0', 1.2);
    g += T2(L, H - 10, n(-m, 0), 9, 'start') + T2(zx, H - 10, '0', 9, 'middle') +
         T2(W - R, H - 10, n(m, 0), 9, 'end');
  } else if (s.type === 'bars') {
    const v = s.values.map(Number);
    const m = Math.max(0.001, ...v.map(x => Math.abs(x)));
    const zy = T + ih / 2, half = ih / 2 * 0.92;
    const step = iw / Math.max(1, v.length), bw = Math.min(26, step * 0.66);
    g += LN(L, zy, W - R, zy, '#c9d2e0', 1.2);
    v.forEach((val, i) => {
      const h = Math.abs(val) / m * half;
      g += RC(L + (i + 0.5) * step - bw / 2, val >= 0 ? zy - h : zy, bw, h,
              val >= 0 ? RED : GREEN, 1);
    });
    const ticks = Math.min(6, s.labels.length);
    for (let k = 0; k < ticks; k++) {
      const i = Math.round(k * (s.labels.length - 1) / Math.max(1, ticks - 1));
      g += T2(L + (i + 0.5) * step, H - 12, String(s.labels[i]).slice(5), 9, 'middle');
    }
    g += T2(L - 6, T + 10, n(m, 1), 9, 'end') + T2(L - 6, zy - 4, '0', 9, 'end') +
         T2(L - 6, T + ih, n(-m, 1), 9, 'end');
  } else if (s.type === 'radar') {
    const v = s.values.map(Number), lab = s.labels, N = Math.max(1, lab.length);
    const cx = W / 2, cy = H / 2 + 4, rad = Math.min(iw, ih) * 0.44;
    const pt = (i, r) => [cx + r * Math.sin(2 * Math.PI * i / N),
                          cy - r * Math.cos(2 * Math.PI * i / N)];
    [0.25, 0.5, 0.75, 1].forEach(f => {
      const poly = [];
      for (let i = 0; i < N; i++) { const p = pt(i, rad * f); poly.push(p[0].toFixed(1) + ',' + p[1].toFixed(1)); }
      g += '<polygon points="' + poly.join(' ') + '" fill="' + (f === 1 ? '#fbfcfe' : 'none') +
           '" stroke="#e4e8ef" stroke-width="1"/>';
    });
    const dp = [];
    for (let i = 0; i < N; i++) {
      const p = pt(i, rad), q = pt(i, rad * 1.2);
      g += LN(cx, cy, p[0], p[1], '#e4e8ef');
      g += T2(q[0], q[1] + 3, lab[i], 10, 'middle', '#475467');
      const r = rad * Math.max(0, Math.min(1, (v[i] + 100) / 200)), d = pt(i, r);
      dp.push(d[0].toFixed(1) + ',' + d[1].toFixed(1));
      g += '<circle cx="' + d[0].toFixed(1) + '" cy="' + d[1].toFixed(1) + '" r="2.6" fill="' + BLUE + '"/>';
    }
    g += '<polygon points="' + dp.join(' ') + '" fill="rgba(47,95,208,.22)" stroke="' +
         BLUE + '" stroke-width="2"/>';
  } else if (s.type === 'kline') {
    const o = s.ohlc || [], d = s.dates || [], N = o.length;
    if (!N) return '<div style="padding:18px;color:#667085;font-size:13px">K 线数据不可用。</div>';
    const ma = (s.ma20 || []).concat(s.ma60 || [])
      .filter(x => x !== null && x !== undefined && isFinite(Number(x))).map(Number);
    let hi = Math.max(...o.map(x => Number(x[3])), ...ma);
    let lo = Math.min(...o.map(x => Number(x[2])), ...ma);
    if (!(hi > lo)) hi = lo + 1;
    const pad = (hi - lo) * 0.06; hi += pad; lo -= pad;
    const priceH = ih * 0.70, volTop = T + ih * 0.76, volH = ih * 0.22;
    const yP = v => T + priceH * (1 - (v - lo) / (hi - lo));
    const step = iw / N, bw = Math.max(1, Math.min(9, step * 0.62));
    const vmax = Math.max(1, ...(s.vols || []).map(Number));
    for (let k = 0; k <= 4; k++) {
      const yy = T + priceH * k / 4;
      g += LN(L, yy, W - R, yy, '#eef1f6');
      g += T2(L - 6, yy + 3, n(hi - (hi - lo) * k / 4, 2), 9, 'end');
    }
    g += LN(L, volTop + volH, W - R, volTop + volH, '#e4e8ef');
    o.forEach((c, i) => {
      const op = Number(c[0]), cl = Number(c[1]), lw = Number(c[2]), hg = Number(c[3]);
      const x = L + (i + 0.5) * step, col = cl >= op ? RED : GREEN;
      g += LN(x, yP(hg), x, yP(lw), col, 1);
      const ya = yP(Math.max(op, cl)), yb = yP(Math.min(op, cl));
      g += RC(x - bw / 2, ya, bw, yb - ya, col, 0.5);
      const vh = Math.max(0, Number((s.vols || [])[i] || 0) / vmax * volH);
      g += RC(x - bw / 2, volTop + volH - vh, bw, vh, col, 0.5);
    });
    [[s.ma20, ORANGE], [s.ma60, BLUE]].forEach(pair => {
      const pts = [];
      (pair[0] || []).forEach((v, i) => {
        if (v !== null && v !== undefined && isFinite(Number(v)))
          pts.push((L + (i + 0.5) * step).toFixed(1) + ',' + yP(Number(v)).toFixed(1));
      });
      if (pts.length > 1) g += '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' +
        pair[1] + '" stroke-width="1.4"/>';
    });
    const ticks = Math.min(6, N);
    for (let k = 0; k < ticks; k++) {
      const i = Math.round(k * (N - 1) / Math.max(1, ticks - 1));
      g += T2(L + (i + 0.5) * step, H - 10, String(d[i] || '').slice(5), 9, 'middle');
    }
    g += T2(L, T - 6, 'K线', 10, 'start', '#475467') + T2(L + 40, T - 6, 'MA20', 10, 'start', ORANGE) +
         T2(L + 92, T - 6, 'MA60', 10, 'start', BLUE) + T2(L + 144, T - 6, '成交量', 10, 'start', GREEN);
  }
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="100%" ' +
    'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">' + g + '</svg>';
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
  if (!ITEMS.length) {
    const c = $('chartOverview');
    if (c) c.innerHTML = '<div class="empty"><b>暂无综合评分排序图</b><br>' +
      '本次抓取没有成功标的，图表与标的卡片会在刷新成功后自动出现。<br>' +
      '页面顶部已列出失败原因、数据源诊断入口与重试步骤。</div>';
    if ($('tiles')) $('tiles').innerHTML = '<div class="card empty">暂无标的卡片。' +
      '请点击右上角「立即刷新数据」，或访问 <code>/api/diagnostics</code> 检查数据源连通性。</div>';
    return;
  }
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
  }, {type:'hbar', labels:sorted.map(r=>r.alias), values:sorted.map(r=>r.total),
      colors:sorted.map(r=>r.tag==='buy'?'#d64545':(r.tag==='sell'?'#2e9e6b':'#8a94a6'))});
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

function vOr(v, d, suf) {
  return (v === null || v === undefined || v === '' || !isFinite(Number(v)))
    ? 'N/A' : Number(v).toFixed(d === undefined ? 2 : d) + (suf || '');
}
function mkTable(heads, rows, cls) {
  return '<table class="' + (cls || '') + '"><thead><tr>' +
    heads.map(h => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>' +
    rows.join('') + '</tbody></table>';
}
function tr(cells, numCols) {
  return '<tr>' + cells.map((c, i) =>
    '<td' + (numCols && numCols.indexOf(i) >= 0 ? ' class="num"' : '') + '>' + c + '</td>').join('') + '</tr>';
}
function yi(v, d) {
  return (v === null || v === undefined || !isFinite(Number(v))) ? 'N/A' : n(Number(v) / 1e8, d === undefined ? 2 : d);
}
function sign(v, d, suf) {
  if (v === null || v === undefined || !isFinite(Number(v))) return 'N/A';
  return (Number(v) > 0 ? '+' : '') + Number(v).toFixed(d === undefined ? 2 : d) + (suf || '');
}

function drawDetail() {
  const r = byCode(current);
  if (!r) {
    $('detail').innerHTML = '<div class="card" style="margin-top:14px"><div class="empty">' +
      '<b>暂无标的详情</b><br>本次抓取没有成功标的，研报级详情会在刷新成功后自动出现。' +
      '<br>详见页面顶部的失败原因与 <code>/api/diagnostics</code> 排查指引。</div></div>';
    return;
  }
  document.querySelectorAll('#chips .chip').forEach(c => c.classList.toggle('on', c.dataset.code === current));
  if (!r.evidence) {
    $('detail').innerHTML = '<div class="card" style="margin-top:14px"><h2 style="margin-top:0">' +
      esc(r.alias) + '（' + r.symbol + '）</h2><div class="box warn">该标的来自旧版缓存，尚未包含研报级证据包。' +
      '请点击页面右上角「立即刷新数据」重新抓取（或运行 python portfolio.py --once），' +
      '刷新完成后即可看到与比亚迪同等级的完整分析依据。</div>' +
      '<div class="kpis"><div class="kpi"><div class="k">综合评分</div><div class="v">' + n(r.total,1) +
      '</div></div><div class="kpi"><div class="k">信号</div><div class="v">' + esc(r.signal) +
      '</div></div><div class="kpi"><div class="k">风险</div><div class="v">' + esc(r.risk_level) +
      '</div></div></div></div>';
    return;
  }
  const E = r.evidence, Q = E.quote || {}, V = E.valuation || {}, T = E.technical || {},
        F = E.financials || {}, FL = E.flow || {}, PH = E.price_history || {},
        PL = E.plan || {}, QU = E.quality || {};
  const L = r.levels, A = r.advice, C = r.chart, stock = r.kind === 'STOCK';
  const kpiPairs = stock ? [
    ['最新价', vOr(r.price,3,' 元')], ['当日涨跌', sign(r.pct,2,'%')],
    ['PE-TTM', vOr(V.pe_ttm,2,'x')], ['PE 历史分位', vOr(V.pe_pct,1,'%')],
    ['PB / 分位', vOr(V.pb,2,'x') + ' / ' + vOr(V.pb_pct,1,'%')],
    ['总市值', vOr(Q.total_mv_yi,0,' 亿')], ['成交额', vOr(Q.amount_yi,2,' 亿')],
    ['换手率 / 量比', vOr(Q.turnover,2,'%') + ' / ' + vOr(Q.volume_ratio,2)],
    ['52 周区间位置', vOr(L.pos52,1,'%')], ['年化波动率', vOr(L.vol_annual,1,'%')]
  ] : [
    ['最新价', vOr(r.price,3,' 元')], ['当日涨跌', sign(r.pct,2,'%')],
    ['近 3 年价格分位', vOr(PH.pct3,1,'%')], ['近 1 年价格分位', vOr(PH.pct1,1,'%')],
    ['52 周区间位置', vOr(L.pos52,1,'%')], ['距 52 周高点', vOr(L.drawdown_pct,1,'%')],
    ['年化波动率', vOr(L.vol_annual,1,'%')], ['成交额', vOr(Q.amount_yi,2,' 亿')],
    ['换手率 / 量比', vOr(Q.turnover,2,'%') + ' / ' + vOr(Q.volume_ratio,2)],
    ['近 250 日最大回撤', vOr(L.max_drawdown_pct,1,'%')]
  ];
  const kpiHtml = kpiPairs.map(x =>
    '<div class="kpi"><div class="k">' + x[0] + '</div><div class="v">' + x[1] + '</div></div>').join('');

  // 一、评分模型明细
  const scoreRows = Object.values(r.scores).map(s =>
    tr(['<b>' + esc(s.label) + '</b> <span class="small">×' + n(s.weight*100,0) + '%</span>',
        '<b class="' + cls(s.score) + '">' + sign(s.score,1) + '</b>',
        sign(s.weighted,1), '<span class="small">' + s.detail.map(esc).join('<br>') + '</span>'], [1,2])).join('');
  const sec1 = '<div class="card sect"><h2>一、评分模型明细（' + esc(r.model) + '）</h2>' +
    '<div class="two"><div>' + mkTable(['维度','原始分','加权分','打分依据（逐项可追溯）'],
      [scoreRows, tr(['<b>合计</b>','—','<b class="' + cls(r.total) + '">' + sign(r.total,2) + '</b>',
        '<span class="small">评分 ∈ [-100,+100]：≥35 买入，15~35 偏买入，−15~15 观望，−35~−15 减仓，≤−35 卖出</span>'], [1,2])]) +
      '<div class="hint">趋势闸门：趋势技术分 &lt; −20 且资金情绪分 &lt; −20 时，即使综合分达标也强制降级为观望；' +
      '风险闸门：风险等级「高」且趋势分 &lt; 0 时同样不允许输出买入。本次是否触发：<b>' +
      (r.gated ? '是' : '否') + '</b>。</div></div>' +
      '<div><h3>五维评分雷达</h3><div id="chartRadar" class="chart-mid"></div></div></div></div>';

  // 二、估值位置 / 价格位置
  let sec2 = '';
  if (stock) {
    const vq = V.pe_quantiles || {};
    const vr = [
      tr(['当前 PE-TTM', vOr(V.pe_ttm,2,'x'), '腾讯行情 + 东财 push2 双源一致口径']),
      tr(['近 ' + vOr(V.years,0) + ' 年分位', vOr(V.pe_pct,1,'%'), '样本 ' + vOr(V.samples,0) + ' 个交易日']),
      tr(['近 3 年分位', vOr(V.pe_pct3,1,'%'), '短周期敏感性校验']),
      tr(['历史中位数', vOr(V.pe_med,2,'x'), '估值中枢']),
      tr(['历史区间', vOr(V.pe_min,2,'x') + ' ~ ' + vOr(V.pe_max,2,'x'), '最小 ~ 最大']),
      tr(['25% / 50% / 75% 分位', vOr(vq.q25,2,'x') + ' / ' + vOr(vq.q50,2,'x') + ' / ' + vOr(vq.q75,2,'x'), '情景推演基准']),
      tr(['PB / PB 分位', vOr(V.pb,2,'x') + ' / ' + vOr(V.pb_pct,1,'%'), 'PB 中位数 ' + vOr(V.pb_med,2,'x')]),
      tr(['PS-TTM 分位', vOr(V.ps_pct,1,'%'), '市销率历史位置']),
      tr(['PEG', vOr(V.peg,2), '净利同比为负时不适用']),
      tr(['前瞻 PE', vOr(V.fwd_pe,2,'x'), '需自填分析师 EPS 预测，程序不编造']),
      tr(['EPS(TTM)', vOr(V.eps_ttm,3,' 元'), '现价 ÷ PE-TTM 反推']),
      tr(['历史区间起止', esc(V.start || 'N/A') + ' ~ ' + esc(V.end || 'N/A'), '东财 datacenter RPT_VALUEANALYSIS_DET'])
    ].join('');
    sec2 = '<div class="card sect"><h2>二、估值位置（PE / PB / PS 历史分位）</h2><div class="two">' +
      '<div>' + mkTable(['指标','数值','说明'], [vr]) + '</div>' +
      '<div><h3>PE-TTM 历史带（虚线为 25% / 50% / 75% 分位）</h3>' +
      '<div id="chartVal" class="chart-mid"></div>' +
      '<p class="small">前复权收盘价与每日 PE-TTM，同源东财 datacenter；虚线为历史分位，最后一列为当前值。</p></div></div>' +
      '<div class="hint">价格位置交叉锚：现价处于近 3 年口径价格 <b>' + vOr(PH.pct3,1,'%') +
      '</b> 分位（实际样本 ' + vOr(PH.bars,0) + ' 根 / 约 ' + vOr(PH.span_years,1) + ' 年，' +
      esc(PH.start||'N/A') + ' ~ ' + esc(PH.end||'N/A') + '）；25% / 50% / 75% 分位价 ' +
      vOr(PH.q25,3) + ' / ' + vOr(PH.q50,3) + ' / ' + vOr(PH.q75,3) + ' 元。' +
      'PE 分位与价格分位互为校验，避免单一估值口径误导。</div></div>';
  } else {
    const pr = [
      tr(['近 3 年口径价格分位', vOr(PH.pct3,1,'%'), '替代个股 PE 分位，越低越有利']),
      tr(['近 1 年（250 日）分位', vOr(PH.pct1,1,'%'), '短周期位置校验']),
      tr(['52 周区间位置', vOr(L.pos52,1,'%'), '（现价 − 52 周低）/（高 − 低）']),
      tr(['52 周高 / 低', vOr(L.high52,3) + ' / ' + vOr(L.low52,3), '前复权日 K 近 250 日']),
      tr(['近 3 年高 / 低', vOr(PH.high,3) + ' / ' + vOr(PH.low,3), '实际样本 ' + vOr(PH.bars,0) + ' 根']),
      tr(['25% / 50% / 75% 分位价', vOr(PH.q25,3) + ' / ' + vOr(PH.q50,3) + ' / ' + vOr(PH.q75,3), '历史成交密集价位']),
      tr(['距 52 周高点', vOr(L.drawdown_pct,1,'%'), '回撤深度']),
      tr(['近 250 日最大回撤', vOr(L.max_drawdown_pct,1,'%'), '峰值到谷底']),
      tr(['年化波动率 / ATR 占比', vOr(L.vol_annual,1,'%') + ' / ' + vOr(L.atr_pct,2,'%'), '近 60 日 / ATR14']),
      tr(['区间涨跌 5/20/60/120/250 日',
          [5,20,60,120,250].map(k => sign(T.returns && T.returns['d'+k],1,'%')).join(' / '), '由前复权日 K 自算'])
    ].join('');
    sec2 = '<div class="card sect"><h2>二、价格位置（替代估值分位）</h2><div class="two">' +
      '<div>' + mkTable(['指标','数值','说明'], [pr]) + '</div>' +
      '<div><h3>近 3 年价格带（虚线为 25% / 50% / 75% 分位）</h3>' +
      '<div id="chartVal" class="chart-mid"></div>' +
      '<p class="small">前复权日 K 收盘价，' + esc(PH.start||'N/A') + ' ~ ' + esc(PH.end||'N/A') +
      '，共 ' + vOr(PH.bars,0) + ' 根（约 ' + vOr(PH.span_years,1) + ' 年）。</p></div></div>' +
      '<div class="hint warnbox"><b>为什么没有 PE / PB / 财报？</b><br>' + esc(V.reason || '') + '</div></div>';
  }

  // 三、价格走势与技术面
  const klRows = (E.ktable || []).map(x =>
    tr([esc(x.d), vOr(x.o,3), vOr(x.h,3), vOr(x.l,3), vOr(x.c,3),
        vOr(x.v,0), sign(x.pct,2,'%'), vOr(x.amt,2), vOr(x.to,2,'%')])).join('');
  const tRows = [
    tr(['MA5 / MA10 / MA20', [5,10,20].map(k => vOr(T['ma'+k],3)).join(' / '),
        'MA60 / MA120 / MA250', [60,120,250].map(k => vOr(T['ma'+k],3)).join(' / ')]),
    tr(['均线形态', '<b>' + esc(T.ma_stack || 'N/A') + '</b>',
        'MACD 柱 / DIF / DEA', [T.macd_hist,T.dif,T.dea].map(x=>vOr(x,3)).join(' / ')]),
    tr(['前一日 MACD 柱', vOr(T.prev && T.prev.macd_hist,3),
        'RSI6 / RSI12 / RSI24', [T.rsi6,T.rsi12,T.rsi24].map(x=>vOr(x,1)).join(' / ')]),
    tr(['KDJ  K / D / J', [T.k,T.d,T.j].map(x=>vOr(x,1)).join(' / '),
        'BOLL 上 / 中 / 下', [T.boll_upper,T.boll_mid,T.boll_lower].map(x=>vOr(x,3)).join(' / ')]),
    tr(['近 20 日高 / 低', vOr(T.high20,3) + ' / ' + vOr(T.low20,3),
        '近 60 日高 / 低', vOr(T.high60,3) + ' / ' + vOr(T.low60,3)]),
    tr(['ATR14 / 年化波动率', vOr(T.atr14,3) + ' / ' + vOr(T.volatility_annual,1,'%'),
        '量比（自算 / 行情商）', vOr(T.vol_ratio_calc,2) + ' / ' + vOr(Q.volume_ratio,2)]),
    tr(['最新量 / 5 日均量', vOr(T.vol_ma5,0) + ' 手（5 日均）',
        'OBV / 前一日', vOr(T.obv,0) + ' / ' + vOr(T.obv_prev,0)]),
    tr(['52 周高 / 低', vOr(Q.high52,3) + ' / ' + vOr(Q.low52,3),
        '当日振幅 / 均价', vOr(Q.amplitude,2,'%') + ' / ' + vOr(Q.avg_price,3)])
  ].join('');
  const ret = T.returns || {};
  const retRows = [5,20,60,120,250].map(k =>
    tr(['近 ' + k + ' 个交易日', '<b class="' + cls(ret['d'+k]) + '">' + sign(ret['d'+k],2,'%') + '</b>',
        '由前复权日 K 收盘价自算（非行情商字段）'])).join('');
  const sec3 = '<div class="card sect"><h2>三、价格走势与技术面</h2>' +
    '<div style="margin-bottom:10px"><button class="btn on" id="btnChart" onclick="toggleK(true)">图</button> ' +
    '<button class="btn" id="btnTable" onclick="toggleK(false)">表（近 40 日精确值）</button>' +
    '<span class="small" style="margin-left:10px">「图 / 表」只切换本节的 K 线与成交量；精确数值不依赖图表</span></div>' +
    '<div id="k_chart"><div id="chartK" class="chart"></div></div>' +
    '<div id="k_table" style="display:none"><div class="scroll">' +
    mkTable(['日期','开盘','最高','最低','收盘','成交量(手)','涨跌','成交额(亿)','换手'], [klRows], 'tbl-sm') +
    '</div></div>' +
    '<p class="small">前复权日 K，最近 ' + vOr(C.dates.length,0) + ' 个交易日；最新交易日 ' +
    esc((C.dates||[]).slice(-1)[0] || 'N/A') + '。A 股习惯：红涨绿跌。图表库同源内嵌，断网也能显示。</p></div>' +
    '<div class="card sect"><div class="two"><div><h3>技术指标全表</h3>' +
    mkTable(['指标','数值','指标','数值'], [tRows], 'tbl-sm') + '</div>' +
    '<div><h3>区间涨跌（自算）</h3>' + mkTable(['周期','涨跌幅','口径'], [retRows], 'tbl-sm') +
    '<div class="hint">指标全部由本地前复权日 K 计算，多指标交叉验证，单一超卖信号不作为买入依据。</div></div></div></div>';

  // 四、财务与业绩拐点 / ETF 替代依据
  let sec4 = '';
  if (stock && F.available) {
    const f = F.latest || {}, b = (F.balance || []).slice(-1)[0] || {};
    const fr = [
      tr(['最新报告期', esc(f.date || 'N/A')]), tr(['披露日', esc(f.notice || 'N/A')]),
      tr(['累计营业收入', yi(f.revenue,2) + ' 亿元']), tr(['累计归母净利润', yi(f.net_profit,2) + ' 亿元']),
      tr(['营收同比', sign(f.rev_yoy,2,'%')]), tr(['归母净利同比', sign(f.np_yoy,2,'%')]),
      tr(['毛利率', vOr(f.gross_margin,2,'%')]), tr(['加权 ROE', vOr(f.roe,2,'%')]),
      tr(['基本 EPS', vOr(f.eps,3,' 元')]), tr(['每股经营现金流', vOr(f.ocf_ps,3,' 元')]),
      tr(['每股净资产 BPS', vOr(f.bps,3,' 元')]), tr(['资产负债率', vOr(F.debt_ratio,2,'%')])
    ].join('');
    const br = [
      tr(['报告期', esc(b.date || 'N/A')]), tr(['总资产', yi(b.total_assets,1) + ' 亿元']),
      tr(['总负债', yi(b.total_liab,1) + ' 亿元']), tr(['存货', yi(b.inventory,1) + ' 亿元']),
      tr(['货币资金', yi(b.cash,1) + ' 亿元']), tr(['应收账款', yi(b.receivable,1) + ' 亿元'])
    ].join('');
    const qs = F.quarters || [];
    const qr = qs.map(x => tr([esc(String(x.year) + 'Q' + String(x.q) + '（' + esc(x.date||'') + '）'),
      yi(x.rev,2), yi(x.np,2), sign(x.rev_yoy,1,'%'),
      '<b class="' + cls(x.np_yoy) + '">' + sign(x.np_yoy,1,'%') + '</b>',
      vOr(x.margin,2,'%')], [1,2,3,4,5])).join('');
    const rep = (F.reports || []).map(x => tr([esc(x.date||''), yi(x.revenue,2), yi(x.net_profit,2),
      sign(x.rev_yoy,1,'%'), sign(x.np_yoy,1,'%'), vOr(x.roe,2,'%')], [1,2,3,4,5])).join('');
    sec4 = '<div class="card sect"><h2>四、财务与业绩拐点</h2><div class="two">' +
      '<div><h3>最新财报（累计口径）</h3>' + mkTable(['项目','数值'], [fr], 'tbl-sm') + '</div>' +
      '<div><h3>资产负债与风险项</h3>' + mkTable(['项目','数值'], [br], 'tbl-sm') +
      '<p class="small">字段缺失时以资产 / 负债反推，或直接标注 N/A，不做估算填充。</p></div></div>' +
      '<h3>单季拆分（累计值差分，用于识别同比拐点）</h3><div class="scroll">' +
      mkTable(['单季','营收(亿)','归母净利(亿)','营收同比','净利同比','当季毛利率'], [qr], 'tbl-sm') + '</div>' +
      '<div id="' + 'chartGrowth" class="chart-mid"></div>' +
      '<h3>近 4 期累计口径（对照）</h3>' +
      mkTable(['报告期','累计营收(亿)','累计净利(亿)','营收同比','净利同比','ROE'], [rep], 'tbl-sm') +
      '<p class="small">来源：东财 datacenter RPT_LICO_FN_CPD / RPT_DMSK_FN_BALANCE（原始披露为公司定期报告，非一手原文）；' +
      '单季值由「本期累计 − 上期累计」差分并与去年同季比对，含会计调整，正式决策请核对公告原文。</p></div>';
  } else {
    const alt = [
      tr(['近 3 年价格分位', vOr(PH.pct3,1,'%'), '替代 PE 分位，权重 25%（价格位置维）']),
      tr(['近 250 日最大回撤', vOr(L.max_drawdown_pct,1,'%'), '波动回撤维，权重 20%']),
      tr(['年化波动率 / ATR 占比', vOr(L.vol_annual,1,'%') + ' / ' + vOr(L.atr_pct,2,'%'), '波动回撤维']),
      tr(['近 20 日主力净额占成交额', vOr(FL.sum20_pct,2,'%'), '资金情绪维，权重 15%']),
      tr(['最新量 / 5 日均量', vOr(T.vol_ratio_calc,2), '资金情绪 + 短期动量']),
      tr(['RSI6 / KDJ-J / 近 20 日涨跌', vOr(T.rsi6,1) + ' / ' + vOr(T.j,1) + ' / ' + sign(Q.chg_20d,2,'%'), '短期动量维，权重 10%']),
      tr(['MA20 / MA60 / 均线形态', vOr(T.ma20,3) + ' / ' + vOr(T.ma60,3) + ' / ' + esc(T.ma_stack||''), '趋势技术维，权重 30%']),
      tr(['成交额 / 换手率', vOr(Q.amount_yi,2,' 亿') + ' / ' + vOr(Q.turnover,2,'%'), '流动性观察（不单独打分）'])
    ].join('');
    sec4 = '<div class="card sect"><h2>四、' +
      (stock ? '财务数据缺口与替代依据' : 'ETF 的替代分析依据（不套用个股财报模型）') + '</h2>' +
      '<div class="hint warnbox"><b>口径说明</b><br>' + esc(F.reason || V.reason || '') + '</div>' +
      '<div class="two"><div><h3>本页实际使用的量化依据</h3>' +
      mkTable(['证据项','数值','在模型中的作用'], [alt], 'tbl-sm') + '</div>' +
      '<div><h3>历史价格下的替代「估值带」</h3><div id="chartGrowth" class="chart-mid"></div>' +
      '<p class="small">柱为近 5 / 20 / 60 / 120 / 250 交易日区间涨跌，用于判断 ETF 所处周期位置，' +
      '不引入任何未经核实的基金净值 / 折溢价估算。</p></div></div>' +
      '<div class="hint">' + (stock
        ? '<b>数据缺口处理</b>：本次财报未取到，成长质量与风险维度中依赖财务的打分项按 N/A 处理、不计入评分；'
          + '下方给出价格 / 量能 / 资金维度的替代证据，请核对公司定期报告后再做判断。'
        : '<b>ETF 特有风险（方法论提示，非本页数值）</b>：跟踪误差、二级市场折溢价、'
          + '成份股集中与行业景气、申赎流动性与规模变化、跨境 / 商品标的的汇率与隔夜跳空。'
          + '这些风险需结合基金定期报告与交易所申赎清单判断，本页不给出未核实的估算值。') + '</div></div>';
  }

  // 五、资金情绪
  const flRows = (FL.table || []).map(x => tr([esc(x.d),
    '<b class="' + cls(x.main) + '">' + sign(x.main,3) + '</b>', sign(x.jumbo,3),
    sign(x.big,3), sign(x.mid,3), sign(x.small,3),
    sign(x.pct,2,'%'), vOr(x.close,3), sign(x.chg,2,'%')], [1,2,3,4,5,6,7,8])).join('');
  const sec5 = '<div class="card sect"><h2>五、资金情绪（主力 = 超大单 + 大单）</h2>' +
    '<div class="kpis">' +
    '<div class="kpi"><div class="k">近 5 日主力净额</div><div class="v ' + cls(FL.sum5_yi) + '">' + sign(FL.sum5_yi,2,' 亿') + '</div></div>' +
    '<div class="kpi"><div class="k">近 20 日主力净额</div><div class="v ' + cls(FL.sum20_yi) + '">' + sign(FL.sum20_yi,2,' 亿') + '</div></div>' +
    '<div class="kpi"><div class="k">20 日净额 / 同期成交额</div><div class="v ' + cls(FL.sum20_pct) + '">' + sign(FL.sum20_pct,2,'%') + '</div></div>' +
    '<div class="kpi"><div class="k">区间</div><div class="v" style="font-size:14px">' + esc(FL.start||'N/A') + ' ~ ' + esc(FL.end||'N/A') + '</div></div></div>' +
    '<div id="chartFlow" class="chart-mid"></div>' +
    '<h3>逐日资金流（近 20 交易日，单位亿元）</h3><div class="scroll">' +
    mkTable(['日期','主力净额','超大单','大单','中单','小单','主力占比','收盘','当日涨跌'], [flRows], 'tbl-sm') +
    '</div><p class="small">来源：东财 push2his fflow/daykline；分档口径由接口返回，累计线为区间主力净额累加。</p></div>';

  // 六、风险点位与条件化交易计划
  const lvRows = [
    tr(['最近支撑', '<b>' + vOr(L.support,3) + '</b>', 'MA20 / 近 20 日低点 / 布林下轨 中最近者']),
    tr(['次级支撑', vOr(L.support2,3), '近 250 日最低（前复权）']),
    tr(['最近压力', '<b>' + vOr(L.resistance,3) + '</b>', 'MA60 / 近 20 日高点 / 布林上轨 中最近者']),
    tr(['次级压力', vOr(L.resistance2,3), '近 250 日最高（前复权）']),
    tr(['参考止损', '<b class="up">' + vOr(L.stop,3) + '</b>', '支撑下方 1.5% 与现价 −8% 取近者']),
    tr(['近 20 日高 / 低', vOr(L.high20,3) + ' / ' + vOr(L.low20,3), '短期箱体']),
    tr(['ATR / 现价', vOr(L.atr_pct,2,'%'), '单日波动参考'])
  ].join('');
  const scen = PL.scenarios || [];
  const scenRows = scen.map(x => tr([esc(x.label), esc(x.basis), vOr(x.price,3),
    '<b class="' + cls(x.delta) + '">' + sign(x.delta,2,'%') + '</b>' +
    (Math.abs(Number(x.delta)||0) > 150 ? ' <span class="small">⚠️ 极端推演</span>' : '')], [2,3])).join('') ||
    tr(['历史样本不足，不生成情景区间','—','—','—']);
  const sec6 = '<div class="card sect"><h2>六、风险点位与条件化交易计划</h2><div class="two">' +
    '<div>' + mkTable(['项目','数值','说明'], [lvRows], 'tbl-sm') + '</div>' +
    '<div><h3>风险成因</h3><div class="box warn">' + (L.risk_reasons||[]).map(esc).join('<br>') + '</div>' +
    '<h3>逻辑失效条件</h3><div class="box">' + esc(A.invalidate) + '</div>' +
    '<div class="hint">风险等级 <b class="' + riskCls(r.risk_level) + '">' + esc(r.risk_level) +
    '</b>；' + (r.gated ? '本次闸门已触发，买入信号降级为观望。' : '本次未触发闸门。') + '</div></div></div>' +
    '<h3>动作路径（条件化，不是当日买卖指令）</h3>' +
    '<div class="box"><b>入场：</b>' + esc(PL.entry || A.trigger) + '<br><b>加仓：</b>' + esc(PL.add || '—') +
    '<br><b>止损：</b>' + esc(PL.stop || A.stop) + '<br><b>失效：</b>' + esc(PL.invalidate || A.invalidate) +
    '<br><span class="small">仓位：' + esc(A.position) + '</span></div>' +
    '<h3>情景区间（' + (stock ? 'PE 分位推演' : '价格分位推演') + '，非目标价预测）</h3>' +
    mkTable(['情景','推算依据','对应价格','相对现价'], [scenRows], 'tbl-sm') +
    '<div class="hint ' + (/⚠/.test(PL.note || '') ? 'warnbox' : '') + '">' + esc(PL.note || '') + '</div>' +
    '<p class="small">未提供个人资金量、持仓成本、税费与风险承受度，故不给出具体仓位金额；' +
    '适用前提为平衡型风险偏好、3–12 个月持有周期、A 股境内交易。</p></div>';

  // 七、数据来源与口径校验
  const srcRows = (QU.sources || []).map(x =>
    tr([esc(x.name), '<span class="small">' + esc(x.url) + '</span>', esc(x.time)])).join('');
  const cov = QU.coverage || {};
  const sec7 = '<div class="card sect"><h2>七、数据来源与口径校验</h2>' +
    mkTable(['数据源','接口','数据时点 / 抓取时间'], [srcRows], 'tbl-sm') +
    '<div class="two" style="margin-top:12px"><div><h3>双源交叉校验</h3><ul class="small">' +
    (QU.cross_notes || []).map(x => '<li>' + esc(x) + '</li>').join('') + '</ul>' +
    '<h3>数据缺口与告警</h3><ul class="small">' +
    ((QU.warnings || []).length ? (QU.warnings||[]).map(x => '<li>' + esc(x) + '</li>').join('') : '<li>无</li>') +
    '</ul></div><div><h3>本次抓数覆盖</h3>' +
    mkTable(['数据集','样本量'], [
      tr(['前复权日 K', vOr(cov.kline,0) + ' 根（' + esc(cov.kline_start||'') + ' ~ ' + esc(cov.kline_end||'') + '）']),
      tr(['历史每日估值', vOr(cov.valuation,0) + ' 条' + (stock ? '' : '（ETF 不适用）')]),
      tr(['季度财报', vOr(cov.financials,0) + ' 期' + (stock ? '' : '（ETF 不适用）')]),
      tr(['资产负债表', vOr(cov.balance,0) + ' 期']),
      tr(['每日资金流', vOr(cov.flow,0) + ' 日'])
    ], 'tbl-sm') +
    '<div class="hint">所有数值运行时抓取，未取到的字段统一显示 N/A 且不计入评分，不做估算填充；' +
    '行情商与第三方数据平台属非一手来源，正式决策前请核对交易所公告与基金 / 公司定期报告原文。</div>' +
    '</div></div><p class="small">' + esc(P.disclaimer || '') + '</p></div>';

  $('detail').innerHTML =
    '<div class="card" style="margin-top:14px"><div class="hero">' +
      '<div><div class="small">' + esc(r.name) + '（' + r.symbol + '） · ' + esc(r.kind) +
      ' · ' + esc(r.sector) + '</div><div style="font-size:30px;font-weight:700" class="' + cls(r.pct) + '">' +
      vOr(r.price,3) + ' <span style="font-size:16px">' + sign(r.pct,2,'%') + '</span></div>' +
      '<div class="small">数据时点 ' + esc(r.asof) + ' ｜ ' + esc(r.model) + '</div></div>' +
      '<div><div class="small">综合评分（±100）</div><div class="score ' + cls(r.total) + '">' + sign(r.total,1) + '</div></div>' +
      '<div><span class="badge ' + tagCls(r.tag) + '">' + esc(r.signal) + '</span>' +
      '<div class="small" style="margin-top:8px">风险等级 <b class="' + riskCls(r.risk_level) + '">' +
      esc(r.risk_level) + '</b></div>' +
      (r.gated ? '<div class="small">闸门已触发：买入降级为观望</div>' : '') + '</div></div>' +
      '<div class="kpis">' + kpiHtml + '</div>' +
      '<h3 style="margin-top:14px">结论与依据摘要</h3><p class="narr">' + esc(r.narrative || '') + '</p></div>' +
    sec1 + sec2 + sec3 + sec4 + sec5 + sec6 + sec7;
  drawCharts(r);
}

function toggleK(show) {
  const a = $('k_chart'), b = $('k_table'), bc = $('btnChart'), bt = $('btnTable');
  if (!a || !b || !bc || !bt) return;
  a.style.display = show ? 'block' : 'none';
  b.style.display = show ? 'none' : 'block';
  bc.className = show ? 'btn on' : 'btn';
  bt.className = show ? 'btn' : 'btn on';
  if (show && charts['chartK']) setTimeout(() => charts['chartK'].resize(), 20);
}

function drawCharts(r) {
  const E = r.evidence, Q = E.quote || {}, V = E.valuation || {}, T = E.technical || {},
        F = E.financials || {}, FL = E.flow || {}, PH = E.price_history || {}, C = r.chart;
  const stock = r.kind === 'STOCK';

  // 1. 五维雷达
  const sc = Object.values(r.scores);
  chart('chartRadar', {
    animation:false, tooltip:{},
    radar:{indicator:sc.map(s=>({name:s.label,min:-100,max:100})),radius:'64%',
      axisName:{fontSize:11,color:'#475467'},splitLine:{lineStyle:{color:'#e4e8ef'}},
      splitArea:{areaStyle:{color:['#fbfcfe','#f4f7fc']}}},
    series:[{type:'radar',data:[{value:sc.map(s=>s.score),name:'评分',
      areaStyle:{color:'rgba(47,95,208,.18)'},lineStyle:{color:'#2f5fd0',width:2},
      itemStyle:{color:'#2f5fd0'}}]}]
  }, {type:'radar', labels:sc.map(s=>s.label), values:sc.map(s=>s.score)});

  // 2. 估值带（个股）/ 价格带（ETF）
  const vq = V.pe_quantiles || {};
  const valDates = stock ? (V.series||[]).map(x=>x.d) : (PH.dates||[]);
  const valVals = stock ? (V.series||[]).map(x=>x.pe) : (PH.closes||[]);
  const marks = (stock ? [
    {name:'25%', yAxis:vq.q25, lineStyle:{color:'#2e9e6b',type:'dashed'}},
    {name:'50%', yAxis:vq.q50, lineStyle:{color:'#8a6d3b',type:'dashed'}},
    {name:'75%', yAxis:vq.q75, lineStyle:{color:'#d64545',type:'dashed'}},
    {name:'当前', yAxis:V.pe_ttm, lineStyle:{color:'#2f5fd0'}}
  ] : [
    {name:'25%', yAxis:PH.q25, lineStyle:{color:'#2e9e6b',type:'dashed'}},
    {name:'50%', yAxis:PH.q50, lineStyle:{color:'#8a6d3b',type:'dashed'}},
    {name:'75%', yAxis:PH.q75, lineStyle:{color:'#d64545',type:'dashed'}},
    {name:'现价', yAxis:r.price, lineStyle:{color:'#2f5fd0'}}
  ]).filter(m => m.yAxis !== null && m.yAxis !== undefined && isFinite(Number(m.yAxis)));
  const specVal = {type:'lines', labels:valDates,
    series:[{name:stock?'PE-TTM':'收盘价', values:valVals, color:'#2f5fd0'}],
    marks: marks.map(m=>({value:m.yAxis,label:m.name,color:m.lineStyle.color}))};
  chart('chartVal', {
    animation:false, tooltip:{trigger:'axis'},
    grid:{left:58,right:22,top:24,bottom:40},
    xAxis:{type:'category',data:valDates,axisLabel:{fontSize:9,color:'#667085'},boundaryGap:false},
    yAxis:{type:'value',scale:true,name:stock?'PE-TTM(倍)':'价格(元)',
      splitLine:{lineStyle:{color:'#eef1f6'}},axisLabel:{fontSize:10}},
    series:[{name:stock?'PE-TTM':'收盘价',type:'line',data:valVals,showSymbol:false,
      lineStyle:{width:1.7,color:'#2f5fd0'},areaStyle:{color:'rgba(47,95,208,.08)'},
      markLine:{silent:true,symbol:'none',label:{formatter:'{b}',fontSize:10},data:marks}}]
  }, specVal);

  // 3. K 线 + 成交量
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
  }, {type:'kline', dates:C.dates, ohlc:C.ohlc, vols:C.vols, ma20:C.ma20, ma60:C.ma60});

  // 4. 单季趋势（个股）/ 区间涨跌（ETF）
  if (stock && (F.quarters||[]).length) {
    const qs = F.quarters || [];
    chart('chartGrowth', {
      animation:false, tooltip:{trigger:'axis'},
      legend:{data:['单季营收','单季归母净利','净利同比'],top:0,textStyle:{fontSize:10}},
      grid:{left:56,right:52,top:30,bottom:34},
      xAxis:{type:'category',data:qs.map(x=>String(x.year)+'Q'+String(x.q)),axisLabel:{fontSize:10}},
      yAxis:[{name:'亿元',splitLine:{lineStyle:{color:'#eef1f6'}}},{name:'同比%'}],
      series:[
        {name:'单季营收',type:'bar',data:qs.map(x=>Number(x.rev)/1e8),itemStyle:{color:'#9db7de'}},
        {name:'单季归母净利',type:'bar',data:qs.map(x=>Number(x.np)/1e8),itemStyle:{color:'#2f5fd0'}},
        {name:'净利同比',type:'line',yAxisIndex:1,data:qs.map(x=>x.np_yoy),
         showSymbol:true,lineStyle:{width:2,color:'#d64545'}}
      ]
    }, {type:'combo', labels:qs.map(x=>String(x.year)+'Q'+String(x.q)),
        bars:[{name:'营收',values:qs.map(x=>Number(x.rev)/1e8),color:'#9db7de'},
              {name:'净利',values:qs.map(x=>Number(x.np)/1e8),color:'#2f5fd0'}],
        line:{name:'净利同比',values:qs.map(x=>x.np_yoy),color:'#d64545'}});
  } else {
    const ks = [5,20,60,120,250], rt = T.returns || {};
    const vals = ks.map(k=>Number(rt['d'+k])||0);
    chart('chartGrowth', {
      animation:false, tooltip:{trigger:'axis'},
      grid:{left:52,right:18,top:24,bottom:34},
      xAxis:{type:'category',data:ks.map(k=>k+'日'),axisLabel:{fontSize:10}},
      yAxis:{type:'value',name:'涨跌%',splitLine:{lineStyle:{color:'#eef1f6'}}},
      series:[{type:'bar',data:vals.map(v=>({value:v,itemStyle:{color:v>=0?'#d64545':'#2e9e6b'}})),
        barWidth:'52%',label:{show:true,position:'top',formatter:(p)=>n(p.value,1)+'%',fontSize:10}}]
    }, {type:'bars', labels:ks.map(k=>k+'日'), values:vals});
  }

  // 5. 资金流：当日柱 + 累计线
  const fd = FL.dates || [], fm = FL.main || [], fc = FL.cum || [];
  chart('chartFlow', {
    animation:false, tooltip:{trigger:'axis'},
    legend:{data:['当日主力净额','累计净额'],top:0,textStyle:{fontSize:10}},
    grid:{left:54,right:54,top:30,bottom:44},
    xAxis:{type:'category',data:fd,axisLabel:{fontSize:9,rotate:45,color:'#667085'}},
    yAxis:[{type:'value',name:'当日(亿)',splitLine:{lineStyle:{color:'#eef1f6'}}},
           {type:'value',name:'累计(亿)'}],
    series:[
      {name:'当日主力净额',type:'bar',data:fm.map(v=>({value:v,
        itemStyle:{color:v>=0?'#d64545':'#2e9e6b'}})),barWidth:'60%'},
      {name:'累计净额',type:'line',yAxisIndex:1,data:fc,smooth:true,showSymbol:false,
        lineStyle:{width:2,color:'#2f5fd0'}}
    ]
  }, {type:'combo', labels:fd,
      bars:[{name:'主力',values:fm,color:'#2f5fd0'}],
      line:{name:'累计',values:fc,color:'#e0952f'}});
}

function selectSymbol(code){ current = code; $('symbolSelect').value = code; drawDetail(); }

async function pollRefresh(){
  // 后台抓数结束后自动刷新页面，避免用户以为「点了没反应」
  let tries = 0;
  const timer = setInterval(async () => {
    tries++;
    try {
      const r = await fetch('/api/portfolio/status', {cache:'no-store'});
      const s = await r.json();
      if ((!s.refreshing && tries >= 2) || tries > 60) {
        clearInterval(timer); location.reload();
      }
    } catch (e) { if (tries > 60) clearInterval(timer); }
  }, 3000);
}

async function refresh(){
  const btn = $('btnRefresh'); const msg = $('refreshMsg');
  btn.disabled = true; msg.textContent = ' 正在后台抓取，请稍候…';
  let started = false;
  try {
    const tok = $('pfToken') ? $('pfToken').value.trim() : '';
    const headers = tok ? {'X-Refresh-Token': tok} : {};
    const r = await fetch('/api/portfolio/refresh', {method:'POST', headers:headers});
    const j = await r.json();
    if (r.status === 401) { msg.textContent = ' 刷新令牌无效，请在输入框填写 REFRESH_TOKEN'; }
    else if (j.started) {
      started = true;
      msg.textContent = ' 已启动，抓取完成后页面会自动刷新…';
    } else { msg.textContent = j.message || ' 已有刷新任务在运行'; }
  } catch(e) { msg.textContent = ' 刷新接口不可用（独立 HTML 模式请重跑脚本）'; }
  finally {
    btn.disabled = false;
    if (started) pollRefresh();
    else setTimeout(()=>msg.textContent='',8000);
  }
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


def _alert_html(data):
    """把「刷新失败 / 正在用旧数据」直接写在页面顶部，避免用户只看到空坐标轴。"""
    esc = html_mod.escape
    items = data.get("items") or []
    if not items:
        errs = data.get("refresh_errors") or data.get("errors") or []
        rows = "".join(
            "<li>" + esc(f"{e.get('symbol', '?')} {e.get('alias', '')}") + "：" +
            esc(str(e.get("error", ""))[:160]) + "</li>" for e in errs[:6])
        return (
            '<div class="alert err"><b>暂时没有可展示的标的数据</b><br>'
            f'最近一次抓取（{esc(str(data.get("generated_at") or "N/A"))}）没有成功标的，'
            '最常见原因是服务器到行情数据源的 HTTPS 出站被限流或拦截，导致日 K 全为空。'
            '<br>排查步骤：① 打开 <code>/api/diagnostics</code> 查看每个数据源的连通性；'
            '② 确认云主机安全组与防火墙允许 HTTPS 出站、DNS 正常；'
            '③ 点击右上角「立即刷新数据」重试（新版已内置腾讯日 K 回退与离线快照）。'
            + ("<ul>" + rows + "</ul>" if rows else "") + "</div>")
    if data.get("stale"):
        src = esc(str(data.get("fallback_source") or "旧数据"))
        hint = esc(str(data.get("refresh_hint") or ""))
        return (
            '<div class="alert"><b>当前展示的不是实时数据</b><br>'
            f'数据来源：{src}；数据时点 {esc(str(data.get("asof") or "N/A"))}；'
            f'快照生成于 {esc(str(data.get("generated_at") or "N/A"))}；'
            f'最近一次刷新 {esc(str(data.get("last_attempt_at") or "N/A"))} '
            f'失败 {esc(str(data.get("refresh_failed") or "?"))} 个标的。'
            f'<br>{hint} 可打开 <code>/api/diagnostics</code> 逐源排查；'
            '点击右上角「立即刷新数据」重试，抓取成功后本提示会自动消失。</div>')
    return ""


def render_page(data, standalone=False):
    data = dict(data or {})
    data.setdefault("items", [])
    data.setdefault("count", len(data["items"]))
    data.setdefault("generated_at", f"{engine.now_cn():%Y-%m-%d %H:%M:%S}")
    data.setdefault("asof", None)
    data.setdefault("errors", [])
    echarts = engine.echarts_script_tag() if standalone else \
        '<script src="/static/echarts.min.js"></script>'
    if standalone:
        byd_reports = sorted(REPORT_DIR.glob("BYD-002594-*.html"))
        byd_link = (f'<a class="btn" style="text-decoration:none;display:inline-block;line-height:1.5" '
                    f'href="./{html_mod.escape(byd_reports[-1].name)}">比亚迪全景研报</a>') \
            if byd_reports else ""
    else:
        byd_link = ('<a class="btn" style="text-decoration:none;display:inline-block;line-height:1.5" '
                    'href="/">比亚迪全景看板</a>')
    etf = sum(1 for r in data["items"] if r["kind"] == "ETF")
    stock = data["count"] - etf
    repl = {
        "__TIME__": html_mod.escape(data["generated_at"]),
        "__ASOF__": html_mod.escape(data.get("asof") or "N/A"),
        "__COUNT__": str(data["count"]),
        "__ETF_COUNT__": str(etf), "__STOCK_COUNT__": str(stock),
        "__DISC__": html_mod.escape(DISCLAIMER),
        "__ALERT__": _alert_html(data),
        "__ECHARTS__": echarts,
        "__BYD_LINK__": byd_link,
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
