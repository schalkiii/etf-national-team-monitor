# -*- coding: utf-8 -*-
"""HTML 报告生成 v2（浅底深字研报风,结论先行,ECharts）。
新增（对标五家项目补齐）：
- 核心8宽基份额 vs 上证指数 双轴叠加图（national-team-position 双轴思想 + quantdata.uk 背景叙事）
- 单 ETF 份额结构条形图（份额落点分析: 申购集中在哪些指数）
- 结构轮动 60 日变化图（区分真减仓 vs 换仓轮动）
- 融资余额走势图（A-Resonance 情绪页）
- 指数距年线距离指标（quantdata.uk 背景三件套）
- 多周期切换（全部/2年/1年/半年）+ dataZoom"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from core.config import ETF_POOL, REPORT_DIR, CORE_CODES, PROJECT_ROOT
from core.analysis import combined_core_description, compute_signals_for_date, persist_signals
from core.nt_estimate import estimate_theta_active, estimate_nt_series, align_to_grid, pick_card_value
from store.db import atomic_write

_DISCLAIMER = "以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。"

_LIGHT_COLOR_MAP = {"红": "#d63031", "绿": "#00a65a", "灰": "#95a5a6"}


def _latest_data_date(con: sqlite3.Connection) -> str:
    """最新「有份额数据」的交易日（份额 T+1 披露,日历最新日可能还没有数据）。"""
    row = con.execute(
        """SELECT MAX(date) d FROM etf_daily
           WHERE code IN (SELECT code FROM etf_daily WHERE shares IS NOT NULL)
             AND shares IS NOT NULL"""
    ).fetchone()
    if row and row["d"]:
        return row["d"]
    return con.execute("SELECT date FROM calendar WHERE is_trading=1 ORDER BY date DESC LIMIT 1").fetchone()["date"]


def run_daily(con: sqlite3.Connection, push: bool = False) -> dict:
    """每日主流程：最新有数据交易日信号 → 落库 → 报告。"""
    date = _latest_data_date(con)
    signals = compute_signals_for_date(con, date)
    persist_signals(con, signals)
    combined = combined_core_description(con, date)
    report_path = _write_html(con, date, signals, combined)
    result = {
        "date": date,
        "combined_5d": combined,
        "n_long": sum(1 for s in signals if s.direction.value == "LONG"),
        "n_short": sum(1 for s in signals if s.direction.value == "SHORT"),
        "n_blocked": sum(1 for s in signals if s.direction.value == "BLOCKED"),
        "blocked_all": all(s.direction.value == "BLOCKED" for s in signals),
        "report": str(report_path),
    }
    if push:
        from core.push import push_daily as _push
        result["pushed"] = _push(con)
    return result


def generate_report(con: sqlite3.Connection, date: str | None = None, out_dir=None) -> str:
    if not date:
        date = _latest_data_date(con)
    signals = compute_signals_for_date(con, date)
    combined = combined_core_description(con, date)
    return str(_write_html(con, date, signals, combined, out_dir=out_dir))


def _invalid_delta_join(con: sqlite3.Connection) -> tuple[set[tuple[str, str]], bool]:
    """返回 (无效Δshares集合, 表是否存在)。"""
    try:
        rows = con.execute("SELECT code, date FROM invalid_delta").fetchall()
        return {(r["code"], r["date"]) for r in rows}, True
    except sqlite3.OperationalError:
        return set(), False


def _sentiment_series(con: sqlite3.Connection, date: str) -> dict:
    """市场热度序列：两市成交额的 60 日滚动分位（学 A-Resonance sentiment/core）。
    样本 <20 记 None（前端灰灯）。融资余额不在此重复——绝对值走势见图 4。"""
    WINDOW, MIN_PTS = 60, 20
    # 两市成交额
    amt_rows = con.execute(
        """SELECT date, amount FROM index_daily WHERE index_code='sh000001'
           AND amount IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 400""", (date,)).fetchall()
    amt_map = {r["date"]: r["amount"] for r in amt_rows}
    sz_rows = con.execute(
        """SELECT date, amount FROM index_daily WHERE index_code='sz399001'
           AND amount IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 400""", (date,)).fetchall()
    for r in sz_rows:
        amt_map[r["date"]] = amt_map.get(r["date"], 0.0) + (r["amount"] or 0.0)
    amt_dates = sorted(amt_map)
    amt_vals = [amt_map[d] for d in amt_dates]

    out_p = []
    for i in range(len(amt_vals)):
        win = amt_vals[max(0, i - WINDOW + 1): i + 1]
        if len(win) < MIN_PTS:
            out_p.append(None)
            continue
        below = sum(1 for v in win if v <= amt_vals[i])
        out_p.append(round(100.0 * below / len(win), 1))

    return {
        "dates": amt_dates, "amt_pct": out_p,
        "amt_last": out_p[-1] if out_p else None,
    }


def _coverage_ledger(con: sqlite3.Connection, date: str, days: int = 15) -> list[dict]:
    """数据覆盖台账（学 A-Resonance 数据槽位）：近 N 交易日各数据源覆盖情况。"""
    rows = con.execute(
        "SELECT date FROM calendar WHERE is_trading=1 AND date<=? ORDER BY date DESC LIMIT ?",
        (date, days)).fetchall()
    ledger = []
    for r in rows:
        d = r["date"]
        ledger.append({
            "date": d,
            "kline": con.execute("SELECT COUNT(*) FROM etf_daily WHERE date=? AND close IS NOT NULL", (d,)).fetchone()[0],
            "shares": con.execute("SELECT COUNT(*) FROM etf_daily WHERE date=? AND shares IS NOT NULL", (d,)).fetchone()[0],
            "margin": con.execute("SELECT COUNT(*) FROM margin_daily WHERE date=?", (d,)).fetchone()[0],
            "signals": con.execute("SELECT COUNT(*) FROM signals WHERE date=?", (d,)).fetchone()[0],
        })
    return ledger


def _fetch_chart_data(con: sqlite3.Connection, date: str) -> dict:
    """图表数据：核心合计份额 / 上证指数 / 融资余额 / 结构分布 / 距年线 / 热力图 / 情绪 / 台账。"""
    invalid_delta, has_invalid = _invalid_delta_join(con)
    share_rows = con.execute(
        """SELECT date, SUM(shares*adjust_factor) total FROM etf_daily
           WHERE code IN (%s) AND shares IS NOT NULL AND date<=?
           GROUP BY date ORDER BY date""" % ",".join("?" * len(CORE_CODES)),
        (*CORE_CODES, date),
    ).fetchall()
    share_dates = [r["date"] for r in share_rows]
    share_vals = [round(r["total"] / 1e8, 1) for r in share_rows] if share_rows else []

    idx_rows = con.execute(
        "SELECT date, close FROM index_daily WHERE index_code='sh000001' AND date<=? ORDER BY date",
        (date,),
    ).fetchall()
    idx_map = {r["date"]: r["close"] for r in idx_rows}

    margin_rows = con.execute(
        "SELECT date, fin_balance_yi FROM margin_daily ORDER BY date"
    ).fetchall()
    margin_map = {r["date"]: round(r["fin_balance_yi"], 0) for r in margin_rows if r["fin_balance_yi"]}

    # 指数距年线（250日均线）
    closes = [r["close"] for r in idx_rows if r["close"]]
    yearline = round(sum(closes[-250:]) / min(len(closes), 250), 1) if len(closes) >= 30 else None
    last_close = closes[-1] if closes else None
    dist_yearline = round((last_close / yearline - 1) * 100, 2) if (last_close and yearline) else None

    # 对齐三序列（份额日为基准,缺数用 null 不入图）
    dates_out, shares_out, index_out, margin_out = [], [], [], []
    for d, v in zip(share_dates, share_vals):
        dates_out.append(d)
        shares_out.append(v)
        index_out.append(idx_map.get(d))
        margin_out.append(margin_map.get(d))

    structure = []
    for code in CORE_CODES:
        r = con.execute(
            "SELECT shares, adjust_factor FROM etf_daily WHERE code=? AND shares IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 1",
            (code, date)).fetchone()
        if r:
            structure.append({"code": code, "name": ETF_POOL[code]["name"].replace("ETF", ""),
                              "value": round(r["shares"] * r["adjust_factor"] / 1e8, 1)})

    # 结构轮动：多窗口（60/20/120/250 日）份额变化%，前端可切换
    ROT_WINDOWS = [60, 20, 120, 250]
    rotation = {str(w): [] for w in ROT_WINDOWS}
    for code in CORE_CODES:
        for w in ROT_WINDOWS:
            rows = con.execute(
                """SELECT date, shares*adjust_factor s FROM etf_daily
                   WHERE code=? AND shares IS NOT NULL AND date<=? ORDER BY date DESC LIMIT ?""",
                (code, date, w + 1)).fetchall()
            if len(rows) >= min(w + 1, 30) and rows[-1]["s"]:
                chg = (rows[0]["s"] / rows[-1]["s"] - 1) * 100
                rotation[str(w)].append({"code": code, "name": ETF_POOL[code]["name"].replace("ETF", ""),
                                         "chg": round(chg, 2)})

    # 单 ETF 详情数据（学 A-Resonance K线多面板: K线+份额日增量柱+指数对照）
    # K线对齐 fallback（学 alignKlineToDates）：缺失行情日以「最近真实收盘」平推,
    # 绝不用 0/null——0 会压扁 y 轴,null 会让蜡烛图断裂。
    detail = {}
    for code in ["510300", "512100", "588000"]:
        rows = con.execute(
            """SELECT date, open, close, high, low, shares, adjust_factor FROM etf_daily
               WHERE code=? AND date<=? AND close IS NOT NULL ORDER BY date""",
            (code, date)).fetchall()
        if len(rows) < 60:
            continue
        dts, kline, flow_bar, split_flags = [], [], [], []
        prev_adj = None
        last_px = rows[0]["close"]  # fallback 起点真实收盘
        for r in rows:
            dts.append(r["date"])
            o, c = r["open"], r["close"]
            if c is None or o is None:
                # 缺口日: OHLC 全部平推为最近真实收盘(高=低=开=收,蜡烛退化为横线)
                o = c = r["close"] or last_px
                kline.append([o, c, o, c])
            else:
                last_px = c
                kline.append([o, c, r["low"], r["high"]])
            adj = r["shares"] * r["adjust_factor"] if r["shares"] else None
            if adj is not None and prev_adj is not None:
                flow_bar.append(round((adj - prev_adj) / 1e8, 2))
                split_flags.append(abs(r["adjust_factor"] - 1.0) > 1e-9 and r["date"])
            else:
                flow_bar.append(None)
                split_flags.append(False)
            if adj is not None:
                prev_adj = adj
        idx_aligned = [idx_map.get(d) for d in dts]
        detail[code] = {"name": ETF_POOL[code]["name"], "dates": dts, "kline": kline,
                        "flow": flow_bar, "index": idx_aligned, "splits": split_flags}

    # 国家队每日增减持估算——算法已抽离至 core/nt_estimate.py（纯函数,测试直接断言）。
    # report 侧只负责：取数（invalid_delta 过滤）→ 调用估算 → 网格对齐 → 装配 CD。
    from core.calibration import estimate_theta

    # θ_active: 各锚点区间里 ΔNT>0 的正建仓期 θ 均值(截断保守区间;无锚点=1.0 全归因)
    theta_by_period: dict[str, dict] = {}
    for code in CORE_CODES:
        try:
            for period, v in estimate_theta(con, code).items():
                theta_by_period[period] = v
        except Exception:
            continue
    theta_active, theta_n_periods = estimate_theta_active(theta_by_period)

    # 每日各核心标的 Δ份额×当日真实收盘价 → 总净申购(亿,真实口径)
    # 折算/披露噪声日: 该标的当日份额不参与差分(读侧过滤 invalid_delta)
    daily_sql = (
        "SELECT date, code, shares*adjust_factor AS s, close FROM etf_daily "
        "WHERE code IN (%s) AND shares IS NOT NULL AND date<=? ORDER BY date"
        % ",".join("?" * len(CORE_CODES))
    )
    all_rows = [r for r in con.execute(daily_sql, (*CORE_CODES, date)).fetchall()
                if not has_invalid or (r["code"], r["date"]) not in invalid_delta]
    by_date: dict[str, dict[str, tuple[float, float]]] = {}
    for r in all_rows:
        by_date.setdefault(r["date"], {})[r["code"]] = (r["s"], r["close"] or 0.0)

    # 估算 + 差分网格 → 披露日网格对齐
    nt_est = estimate_nt_series(by_date, idx_map, theta_active)
    nt_aligned = align_to_grid(nt_est, dates_out)
    nt_theta_used = theta_active

    # 事件标记（官宣/护盘事件,markLine 用）
    event_rows = con.execute(
        "SELECT name, event_date FROM events ORDER BY event_date"
    ).fetchall()
    events_out = [{"name": r["name"], "date": r["event_date"]} for r in event_rows]

    # 历史共振日（主图背景带用）
    res_rows = con.execute(
        "SELECT DISTINCT date, resonance FROM signals WHERE resonance != '中性' ORDER BY date"
    ).fetchall()
    resonance_days = [{"date": r["date"], "type": r["resonance"]} for r in res_rows]

    # 申购落点明细（自事件锚定日起各标的变化——quantdata.uk "Where the creation landed" 口径）
    landing_anchor = next((e["date"] for e in events_out if e["date"] >= "2026-01-01"),
                          events_out[0]["date"] if events_out else None)
    landing = []
    if landing_anchor:
        for code in CORE_CODES:
            base = con.execute(
                """SELECT shares*adjust_factor s FROM etf_daily
                   WHERE code=? AND shares IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 1""",
                (code, landing_anchor)).fetchone()
            last = con.execute(
                """SELECT shares*adjust_factor s FROM etf_daily
                   WHERE code=? AND shares IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 1""",
                (code, date)).fetchone()
            px = con.execute(
                "SELECT close FROM etf_daily WHERE code=? AND close IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 1",
                (code, date)).fetchone()
            if base and last and base["s"]:
                chg = (last["s"] / base["s"] - 1) * 100
                amt = (last["s"] - base["s"]) / 1e8 * ((px["close"] or 0.0) if px else 0.0)
                landing.append({
                    "code": code, "name": ETF_POOL[code]["name"].replace("ETF", ""),
                    "index": ETF_POOL[code].get("index_name", ""),
                    "base": round(base["s"] / 1e8, 1), "last": round(last["s"] / 1e8, 1),
                    "chg": round(chg, 1), "amt": round(amt, 0),
                })
        landing.sort(key=lambda x: -x["chg"])

    # ---- 热力图数据层：全部信号日 × 核心标的信号格（学 A-Resonance ResonanceHeatmap）----
    # 格值: direction 映射 +2 LONG / -2 SHORT / 0 NEUTRAL / None BLOCKED
    DIR_VALUE = {"LONG": 2, "SHORT": -2, "NEUTRAL": 0}
    hm_rows = con.execute(
        """SELECT s.date, s.code, s.direction, s.net_flow_yi, s.resonance, s.payload
           FROM signals s JOIN calendar c ON c.date=s.date AND c.is_trading=1
           WHERE s.code IN (%s) AND s.date<=? ORDER BY s.date DESC LIMIT ?
             """ % ",".join("?" * len(CORE_CODES)),
        (*CORE_CODES, date, 400 * len(CORE_CODES))).fetchall()
    hm_by_date: dict[str, dict] = {}
    for r in hm_rows:
        hm_by_date.setdefault(r["date"], {})[r["code"]] = r
    hm_dates = sorted(hm_by_date)
    heatmap = {
        "dates": hm_dates,
        "codes": CORE_CODES,
        "names": {c: ETF_POOL[c]["name"] for c in CORE_CODES},
        # value[date][codeIdx]: 2/-2/0/null
        "values": [[DIR_VALUE.get(hm_by_date[d][c]["direction"]) if c in hm_by_date[d] else None
                    for c in CORE_CODES] for d in hm_dates],
        # 证据层: 每格 payload 解析(供证据卡)
        "evidence": {d: {c: (hm_by_date[d][c]["payload"] or "") for c in hm_by_date[d]} for d in hm_dates},
        "flows": {d: {c: hm_by_date[d][c]["net_flow_yi"] for c in hm_by_date[d]} for d in hm_dates},
    }

    # ---- 情绪分区 + 覆盖台账 + 海外仪表 ----
    sentiment = _sentiment_series(con, date)
    coverage = _coverage_ledger(con, date)
    overseas = _overseas_snapshot(con)

    return {
        "dates": dates_out, "shares": shares_out, "index": index_out, "margin": margin_out,
        "dist_yearline": dist_yearline, "yearline": yearline, "last_close": last_close,
        "structure": structure, "rotation": rotation, "detail": detail,
        "nt": {"daily": nt_aligned["daily"], "cum": nt_aligned["cum"], "total": nt_aligned["total"],
               "active": nt_est["active"], "dates": dates_out,
               "theta": nt_theta_used, "n_active": nt_est["n_active"], "events": events_out,
               "n_buy": nt_est["n_buy"],
               "n_sell": nt_est["n_sell"],
               "theta_n_anchor_periods": theta_n_periods,
               "last_active": nt_est["last_active"]},
        "resonance_days": resonance_days,
        "landing": landing, "landing_anchor": landing_anchor,
        "heatmap": heatmap, "sentiment": sentiment, "coverage": coverage,
        "overseas": overseas,
    }


def _core_lights_summary(signals) -> dict:
    """核心层五灯汇总：对 5 个灯位逐一投票（红>绿→红,绿>红→绿,否则灰）。"""
    core = [s for s in signals if s.code in CORE_CODES and s.lights]
    if not core:
        return {"lights": [], "verdict": "数据不足"}
    names = [l.name for l in core[0].lights]
    lights = []
    for i, name in enumerate(names):
        red = sum(1 for s in core if s.lights[i].color.value == "红")
        green = sum(1 for s in core if s.lights[i].color.value == "绿")
        color = "红" if red > green else ("绿" if green > red else "灰")
        sample = next((s.lights[i] for s in core if s.lights[i].color.value == color), core[0].lights[i])
        lights.append({"name": name, "color": color, "value": sample.value, "detail": sample.detail,
                       "red": red, "green": green})
    verdict = "中性"
    if sum(1 for l in lights if l["color"] == "绿") >= 3:
        verdict = "机会共振"
    elif sum(1 for l in lights if l["color"] == "红") >= 3:
        verdict = "危险共振"
    return {"lights": lights, "verdict": verdict}


def _overseas_snapshot(con: sqlite3.Connection) -> dict:
    """海外市场背景（尽力而为）：KOSPI/纳指近60日涨跌。数据缺失返回空(前端隐藏)。
    指数代码若有采集则用,否则优雅降级。"""
    out = {}
    for key, label in [("kospi200", "KOSPI200"), ("ndx", "纳斯达克100")]:
        try:
            rows = con.execute(
                "SELECT date, close FROM index_daily WHERE index_code=? AND close IS NOT NULL ORDER BY date DESC LIMIT 61",
                (key,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if len(rows) >= 30:
            chg = (rows[0]["close"] / rows[-1]["close"] - 1) * 100
            out[key] = {"name": label, "chg": round(chg, 2)}
    return out


def _write_html(con, date: str, signals, combined, out_dir=None) -> Path:
    """HTML 组装委托 core/report_html.py（R10 拆分：数据与本模块在此,模板在外）。"""
    from core.report_html import _write_html as _render
    cd = _fetch_chart_data(con, date)
    cd["lights_summary"] = _core_lights_summary(signals)
    return _render(con, date, signals, combined, cd, out_dir=out_dir)