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

    # 国家队每日增减持估算——双口径设计（口径诚实原则）：
    # ① 活跃日口径：当日核心宽基总净申购处于历史高分位(≥90分位)且处于护盘语境
    #    （指数回撤 or 5日合计份额激增）→ 判定国家队活跃,估算=总净申购×θ_active
    #    θ_active 取锚点标定过的建仓期 θ（无锚点时保守取 1.0,即全归因,并在图注声明）
    # ② 静默日：估算=0,不冒充（学 etf-tracker BLOCKED 纪律:不确定就不给数字）
    from core.calibration import latest_valid_theta
    from core.signal_engine import mad_sigma, pct_rank
    from statistics import median

    # θ_active: 取所有锚点区间里 ΔNT>0 的正建仓期 θ 均值(截断到[0.5,1.0]保守区间)
    active_thetas = []
    for code in CORE_CODES:
        try:
            from core.calibration import estimate_theta
            for period, v in estimate_theta(con, code).items():
                if v.get("valid") and v.get("delta_nt_yi", 0) > 5 and v.get("theta") and 0 < v["theta"] <= 1.5:
                    active_thetas.append(min(v["theta"], 1.0))
        except Exception:
            continue
    theta_active = round(min(max(sum(active_thetas) / len(active_thetas), 0.5), 1.0), 3) if active_thetas else 1.0

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
    sorted_dates = sorted(by_date)
    nt_daily, nt_cum, nt_total, nt_active_flags = [], [], [], []
    cum = 0.0
    total_series = []
    date_series = []
    for i in range(1, len(sorted_dates)):
        d0, d1 = sorted_dates[i - 1], sorted_dates[i]
        prev_map, cur_map = by_date[d0], by_date[d1]
        total_flow = 0.0
        for code, (s, px) in cur_map.items():
            if code in prev_map:
                total_flow += (s - prev_map[code][0]) / 1e8 * px
        total_series.append(total_flow)
        date_series.append(d1)
    # 活跃日判定（行为模式约束：量级 + 语境缺一不可,拒绝把牛市抢筹标成国家队）:
    #   买入日: |总净申购|≥90分位 且 当日指数下跌（回撤期放量）; 或 强护盘语境(指数跌>1% 且净申购>0)
    #   卖出日: 总净赎回≥90分位 且 当日指数上涨（反弹兑现语境）
    abs_series = [abs(v) for v in total_series]
    p90 = None
    if len(abs_series) >= 60:
        sorted_abs = sorted(abs_series)
        p90 = sorted_abs[int(len(sorted_abs) * 0.9)]
    for i, d1 in enumerate(date_series):
        tf = total_series[i]
        idx_chg = None
        # 指数当日涨跌
        prev_c, cur_c = idx_map.get(date_series[i - 1]), idx_map.get(d1)
        if prev_c and cur_c:
            idx_chg = (cur_c / prev_c - 1) * 100
        active = False
        if tf > 0 and idx_chg is not None:
            if p90 is not None and abs(tf) >= p90 and idx_chg < 0:
                active = True  # 回撤期放量申购
            if idx_chg <= -1.0:
                active = True  # 强护盘语境(跌>1%仍净申购)
        elif tf < 0 and idx_chg is not None and idx_chg > 0 and p90 is not None and abs(tf) >= p90:
            active = True  # 反弹日大额净赎回(疑似撤退兑现)
        nt_flow = round(tf * theta_active, 1) if active else 0.0
        nt_total.append(round(tf, 1))
        nt_daily.append(round(nt_flow, 1))
        nt_active_flags.append(1 if active else 0)
        cum += nt_flow
        nt_cum.append(round(cum, 1))
    nt_theta_used = theta_active
    # 国家队估算序列与合计份额序列日期网格不同（前者是差分日,后者为份额披露日）。
    # 合并图后需与 dates_out 对齐（缺失日补 0：非活跃日本就是零,不冒充）。
    nt_by_date = dict(zip(date_series, zip(nt_daily, nt_cum, nt_total, nt_active_flags)))
    nt_daily_a = [nt_by_date.get(d, (0.0, None, 0.0, 0))[0] for d in dates_out]
    nt_cum_a = [nt_by_date.get(d, (0.0, None, 0.0, 0))[1] for d in dates_out]
    nt_total_a = [nt_by_date.get(d, (0.0, None, 0.0, 0))[2] for d in dates_out]
    # cum 缺失日用前值填充（阶梯连续,避免折线断裂）
    prev = 0.0
    for i, v in enumerate(nt_cum_a):
        if v is None:
            nt_cum_a[i] = prev
        else:
            prev = v

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
        "nt": {"daily": nt_daily_a, "cum": nt_cum_a, "total": nt_total_a,
               "active": nt_active_flags, "dates": dates_out,
               "theta": nt_theta_used, "n_active": sum(nt_active_flags), "events": events_out,
               "n_buy": sum(1 for v in nt_daily if v > 0),
               "n_sell": sum(1 for v in nt_daily if v < 0),
               "theta_n_anchor_periods": len(active_thetas),
               "last_active": next((d for d, v in reversed(list(zip(date_series, nt_daily))) if v != 0), None)},
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
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    cd = _fetch_chart_data(con, date)
    # 当日核心层五灯汇总（顶部灯组,核心标的灯投票）
    cd["lights_summary"] = _core_lights_summary(signals)
    # echarts 直接内联进 HTML(本地自洽原则):报告单文件离线可复现,
    # 不依赖 CDN(被墙/离线即空白图)。echarts.min.js 已确认不含 </script> 字样,可安全内联。
    echarts_src = (PROJECT_ROOT / "assets" / "echarts.min.js").read_text(encoding="utf-8")

    top_flows = sorted([s for s in signals if s.net_flow_yi is not None],
                       key=lambda s: abs(s.net_flow_yi), reverse=True)[:10]

    def light_dots(s):
        if not s.lights:
            return "—"
        return " ".join(
            f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
            f'background:{_LIGHT_COLOR_MAP[l.color.value]};margin-right:2px;" title="{l.name}:{l.detail}"></span>'
            for l in s.lights)

    flow_rows = "".join(
        f"<tr><td>{s.code}</td><td>{ETF_POOL.get(s.code,{}).get('name',s.code)}</td>"
        f"<td style='text-align:right'>{s.net_flow_yi:+.1f}</td>"
        f"<td>{s.resonance}</td><td>{s.confidence}</td>"
        f"<td>{light_dots(s)}</td>"
        f"<td>{'⚠'+str(s.anomaly_sigma)+'σ' if s.anomaly else ''}</td></tr>"
        for s in top_flows)

    label = combined.get("label", "-")
    chg5d = combined.get("chg5d")
    headline = f"核心宽基合计份额 5 日 {chg5d:+.2f}%（{label}）" if chg5d is not None else "核心宽基合计份额数据不足"
    dyl = cd.get("dist_yearline")
    last_close_val = cd.get("last_close") or "—"
    dyl_html = (f'<span class="metric">指数距年线 <b>{dyl:+.2f}%</b></span>' if dyl is not None
                else '<span class="metric">指数距年线 —</span>')
    margin_last = cd["margin"][-1] if cd["margin"] else None
    margin_html = (f'<span class="metric">融资余额 <b>{margin_last:,.0f}亿</b></span>' if margin_last
                   else '<span class="metric">融资余额 —</span>')

    chart_json = json.dumps(cd, ensure_ascii=False)
    n_long = sum(1 for s in signals if s.direction.value == "LONG")
    n_neu = sum(1 for s in signals if s.direction.value == "NEUTRAL")
    n_short = sum(1 for s in signals if s.direction.value == "SHORT")
    n_blocked = sum(1 for s in signals if s.direction.value == "BLOCKED")

    nt = cd.get("nt", {})
    nt_days_total = len(nt.get("daily", []))
    nt_n_active = nt.get("n_active") or 0
    nt_theta = nt.get("theta")
    nt_last = nt["daily"][-1] if nt.get("daily") else None
    nt_cum_last = nt["cum"][-1] if nt.get("cum") else None
    # 卡片显示口径: 当日非活跃日时展示最近活跃日估算(标日期),避免误导性的 "+0 亿"
    nt_last_active_date = nt.get("last_active")
    if (nt_last or 0) == 0 and nt_last_active_date:
        d_idx = {d: i for i, d in enumerate(nt.get("dates", []))}
        ai = d_idx.get(nt_last_active_date)
        if ai is not None:
            nt_card_val = nt["daily"][ai]
            nt_card_label = f"最近活跃日 {nt_last_active_date}"
        else:
            nt_card_val, nt_card_label = nt_last, "当日"
    else:
        nt_card_val, nt_card_label = nt_last, "当日"
    theta_note = (f"θ={nt_theta}（核心宽基锚点均值）" if nt_theta is not None else "θ 未标定（无有效锚点）")
    if nt_theta is not None:
        # 诚实披露 θ 样本量: 锚点区间极少时不能自称"均值"
        theta_n = nt.get("theta_n_anchor_periods")
        theta_note = (f"θ={nt_theta}（{theta_n} 个建仓期锚点均值）" if theta_n
                      else f"θ={nt_theta}（单一建仓期锚点,样本待补）")
    nt_head = (f"国家队昨日估算净{'增持' if (nt_last or 0) >= 0 else '减持'} <b>{abs(nt_last):.0f} 亿</b>"
               f"（窗口累计 {nt_cum_last:+,.0f} 亿）") if nt_last is not None else "国家队估算数据不足"

    # ---- 五灯共振横幅（P3）----
    ls = cd.get("lights_summary", {})
    ls_verdict = ls.get("verdict", "数据不足")
    _v_style = {"机会共振": ("rgba(30,132,73,.14)", "#1e8449"),
                "危险共振": ("rgba(176,58,46,.14)", "#b03a2e")}.get(
        ls_verdict, ("var(--paper)", "var(--ink-2)"))
    ls_color_bg, ls_color_fg = _v_style
    lights_date = date
    light_banner_dots = "".join(
        f'<span class="lb-light" title="{l["detail"]}（红{l["red"]}/绿{l["green"]}票）">'
        f'<span class="lb-dot" style="background:{_LIGHT_COLOR_MAP[l["color"]]}"></span>'
        f'<span class="lb-name">{l["name"]}</span></span>'
        for l in ls.get("lights", [])) or '<span style="font-size:12px;color:var(--ink-3)">灯数据不足</span>'

    # ---- 海外仪表（P10,数据缺失自动隐藏）----
    overseas_items = list((cd.get("overseas") or {}).values())
    overseas_gauges = "".join(
        f'''<div class="gauge">
      <div class="label">{o["name"]} · 60日</div>
      <div class="value" style="color:{"var(--up)" if o["chg"] >= 0 else "var(--dn)"}">{o["chg"]:+.2f}%</div>
      <div class="sub">海外风险偏好背景（弱参考）</div>
    </div>'''
        for o in overseas_items)

    # ---- 覆盖台账行（P9）----
    coverage_rows = ""
    n_pool = len(ETF_POOL)
    for row in reversed(cd.get("coverage", [])):
        cells = f"<td class='num'>{row['kline']}</td><td class='num'>{row['shares']}</td><td class='num'>{row['margin']}</td><td class='num'>{row['signals']}</td>"
        if row["shares"] == 0:
            status = "<span class='dn'>份额断档</span>"
        elif row["kline"] == 0:
            status = "<span class='dn'>行情缺失</span>"
        else:
            status = "<span style='color:var(--ink-3)'>正常</span>"
        coverage_rows += f"<tr><td class='mono'>{row['date']}</td>{cells}<td>{status}</td></tr>\n"

    # ---- 申购落点表（quantdata.uk "Where the creation landed"）----
    landing_anchor = cd.get("landing_anchor") or "—"
    landing_rows = ""
    for item in cd.get("landing", []):
        pos = item["chg"] >= 0
        landing_rows += (
            f"<tr>"
            f"<td><b>{item['code']}</b> {item['name']}</td>"
            f"<td class='num'>{item['base']:,}</td>"
            f"<td class='num'>{item['last']:,}</td>"
            f"<td class='num'><b class='{'up' if pos else 'dn'}'>{item['chg']:+.1f}%</b></td>"
            f"<td class='num'>{item['amt']:+,.0f}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>国家队 ETF 份额监控 · {date}</title>
<script>/*__ECHARTS_INLINE__*/</script>
<style>
:root {{
  --ink: #1c2833;
  --ink-2: #5d6d7e;
  --ink-3: #85929e;
  --paper: #fbfaf7;
  --paper-2: #f4f2ec;
  --rule: #d5d0c5;
  --rule-soft: #e8e4da;
  --up: #b03a2e;
  --dn: #1e8449;
  --accent: #8e44ad;
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0;
  font-family: "Source Han Serif SC", "Noto Serif SC", "Songti SC", SimSun, serif;
  background: var(--paper);
  color: var(--ink);
  line-height: 1.75;
}}
.mono {{ font-family: "JetBrains Mono", Consolas, "Courier New", monospace; }}
.num {{ font-family: Consolas, "Courier New", monospace; font-variant-numeric: tabular-nums; text-align: right; }}

/* ---- 顶部横幅 ---- */
header {{
  max-width: 1080px; margin: 0 auto; padding: 44px 32px 8px;
}}
.kicker {{
  font-size: 12px; letter-spacing: .35em; color: var(--ink-3);
  text-transform: uppercase; font-family: Georgia, serif;
}}
h1 {{
  font-size: 30px; font-weight: 700; margin: 6px 0 4px; letter-spacing: .02em;
  font-family: "Source Han Serif SC", "Noto Serif SC", SimSun, serif;
}}
.subtitle {{ font-size: 14px; color: var(--ink-2); }}
.subtitle .stamp {{ color: var(--accent); font-weight: 600; }}

/* ---- 导航 ---- */
nav {{
  position: sticky; top: 0; z-index: 50;
  background: rgba(251,250,247,.92); backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--rule);
  margin: 18px 0 0;
}}
nav .inner {{
  max-width: 1080px; margin: 0 auto; padding: 10px 32px;
  display: flex; gap: 22px; flex-wrap: wrap;
}}
nav a {{
  font-size: 13px; color: var(--ink-2); text-decoration: none;
  padding-bottom: 2px; border-bottom: 2px solid transparent;
  font-family: "Source Han Sans SC", "Microsoft YaHei", sans-serif;
}}
nav a:hover {{ color: var(--ink); border-bottom-color: var(--accent); }}

main {{ max-width: 1080px; margin: 0 auto; padding: 8px 32px 60px; }}

/* ---- 章节体例 ---- */
section {{ margin: 44px 0 0; }}
.sec-head {{
  display: flex; align-items: baseline; gap: 14px;
  border-bottom: 1px solid var(--rule); padding-bottom: 8px; margin-bottom: 18px;
}}
.sec-no {{
  font-family: Georgia, serif; font-size: 15px; color: var(--accent);
  font-style: italic; letter-spacing: .05em;
}}
.sec-title {{ font-size: 19px; font-weight: 700; margin: 0; }}
.sec-note {{ font-size: 12px; color: var(--ink-3); margin-left: auto; }}

/* ---- 大数字指标行 ---- */
.gauges {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1px; background: var(--rule);
  border-top: 2px solid var(--ink); border-bottom: 1px solid var(--rule);
}}
.gauge {{
  background: var(--paper); padding: 16px 18px 14px;
}}
.gauge .label {{ font-size: 12px; color: var(--ink-2); letter-spacing: .08em; }}
.gauge .value {{
  font-size: 27px; font-weight: 700; margin: 2px 0;
  font-variant-numeric: tabular-nums;
}}
.gauge .sub {{ font-size: 12px; color: var(--ink-3); line-height: 1.5; }}

/* ---- 图表容器 ---- */
.chart {{ width: 100%; }}
#mainChart {{ height: 560px; }}
#etfChart {{ height: 430px; }}
#structChart {{ height: 300px; }} #rotationChart {{ height: 300px; }}
#marginChart {{ height: 240px; }}
#heatChart {{ height: 420px; }}
#sentChart {{ height: 260px; }}

/* ---- 五灯共振横幅 ---- */
.light-banner {{
  display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
  margin-top: 14px; padding: 12px 16px;
  background: var(--paper-2); border: 1px solid var(--rule-soft); border-radius: 4px;
}}
.lb-verdict {{ display: flex; align-items: center; gap: 12px; }}
.lb-title {{ font-size: 12px; color: var(--ink-2); letter-spacing: .08em; }}
.lb-state {{
  font-size: 13px; font-weight: 700; padding: 3px 14px; border-radius: 999px;
  font-family: "Source Han Sans SC", "Microsoft YaHei", sans-serif;
}}
.lb-lights {{ display: flex; gap: 14px; flex-wrap: wrap; margin-left: auto; }}
.lb-light {{
  display: flex; align-items: center; gap: 6px; cursor: default;
  font-family: "Source Han Sans SC", "Microsoft YaHei", sans-serif;
}}
.lb-dot {{ width: 13px; height: 13px; border-radius: 50%; box-shadow: inset 0 -2px 3px rgba(0,0,0,.18); }}
.lb-light .lb-name {{ font-size: 12px; color: var(--ink-2); }}

/* ---- 证据卡面板 ---- */
.ev-panel {{
  margin-top: 12px; padding: 14px 16px;
  background: var(--paper-2); border: 1px solid var(--rule-soft); border-radius: 4px;
  font-family: "Source Han Sans SC", "Microsoft YaHei", sans-serif;
  font-size: 12.5px;
}}
.ev-panel h4 {{ margin: 0 0 8px; font-size: 14px; }}
.ev-panel .ev-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px; }}
.ev-panel .ev-item {{ background: var(--paper); border: 1px solid var(--rule-soft); border-radius: 3px; padding: 8px 10px; }}
.ev-panel .ev-item .n {{ font-size: 11px; color: var(--ink-3); }}
.ev-panel .ev-item .v {{ font-size: 12.5px; margin-top: 2px; line-height: 1.6; }}
.ev-close {{ float: right; cursor: pointer; color: var(--ink-3); font-size: 16px; line-height: 1; }}
.ev-close:hover {{ color: var(--ink); }}

/* ---- 方法折叠表 ---- */
.method-details {{ margin: 6px 0 16px; border: 1px solid var(--rule-soft); border-radius: 4px; background: var(--paper-2); }}
.method-details summary {{
  cursor: pointer; padding: 10px 16px; font-size: 13px; color: var(--ink-2);
  font-family: "Source Han Sans SC", "Microsoft YaHei", sans-serif; user-select: none;
}}
.method-details summary:hover {{ color: var(--ink); }}
.method-details .overflow-x {{ overflow-x: auto; padding: 0 16px 14px; }}
.method-details table {{ font-size: 12.5px; }}
.method-details th {{ border-top: none; padding: 6px 10px; }}

/* ---- 时间范围与 ETF 切换 ---- */
.range-btn {{
  display: inline-block; padding: 2px 14px; font-size: 12.5px;
  border: 1px solid var(--rule); border-radius: 999px; margin: 0 6px 8px 0;
  cursor: pointer; color: var(--ink-2); user-select: none;
  font-family: "Source Han Sans SC", "Microsoft YaHei", sans-serif;
  transition: all .15s;
}}
.range-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
.range-btn.active {{ background: var(--ink); color: var(--paper); border-color: var(--ink); }}

/* ---- 表格 ---- */
table {{
  width: 100%; border-collapse: collapse; font-size: 13.5px;
  font-family: "Source Han Sans SC", "Microsoft YaHei", sans-serif;
}}
th {{
  font-weight: 600; color: var(--ink-2); text-align: left;
  border-top: 2px solid var(--ink); border-bottom: 1px solid var(--rule);
  padding: 8px 10px; font-size: 12.5px; letter-spacing: .03em;
}}
td {{ padding: 8px 10px; border-bottom: 1px solid var(--rule-soft); }}
tr:hover td {{ background: var(--paper-2); }}
.up {{ color: var(--up); }} .dn {{ color: var(--dn); }}
.light-dot {{
  display: inline-block; width: 9px; height: 9px; border-radius: 50%;
  margin-right: 3px; vertical-align: middle;
}}

/* ---- 依据卡（点五灯看依据,A-Resonance EvidenceCard 思想） ---- */
.ev-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin: 14px 0; }}
.ev-card {{
  background: var(--paper-2); border: 1px solid var(--rule-soft); border-radius: 4px;
  padding: 12px 14px;
}}
.ev-card .ev-name {{ font-size: 13px; font-weight: 600; }}
.ev-card .ev-state {{ font-size: 11px; margin-left: 8px; }}
.ev-card .ev-val {{ font-size: 20px; font-weight: 700; margin: 4px 0 2px; font-variant-numeric: tabular-nums; }}
.ev-card .ev-note {{ font-size: 11.5px; color: var(--ink-3); line-height: 1.6; }}

/* ---- 脚注与方法 ---- */
.footnote {{
  font-size: 12.5px; color: var(--ink-3); line-height: 1.8;
  margin-top: 10px; padding-left: 14px; border-left: 2px solid var(--rule-soft);
}}
.method li {{ margin-bottom: 10px; }}
.method {{ font-size: 13.5px; }}
.method b {{ color: var(--ink); }}

.disclosure {{
  margin: 48px 0 0; padding: 18px 20px; background: var(--paper-2);
  border: 1px solid var(--rule-soft); border-radius: 4px;
  font-size: 12.5px; color: var(--ink-2); line-height: 1.9;
  font-family: "Source Han Sans SC", "Microsoft YaHei", sans-serif;
}}
.disclosure b {{ color: var(--ink); }}

@media (max-width: 760px) {{
  header, main {{ padding-left: 18px; padding-right: 18px; }}
  h1 {{ font-size: 24px; }}
}}
</style>
</head>
<body>

<header>
  <div class="kicker">Market Structure &middot; Daily Research Note</div>
  <h1>国家队 ETF 份额监控</h1>
  <div class="subtitle">
    沪深核心宽基每日份额登记 &middot; 数据截至 <span class="stamp">{date}</span> &middot; 由交易所披露重建（T+1）
  </div>
</header>

<nav><div class="inner">
  <a href="#g-main">份额·指数·增减持</a>
  <a href="#g-landing">申购落点</a>
  <a href="#g-structure">结构轮动</a>
  <a href="#g-detail">单基金详情</a>
  <a href="#g-flow">当日净申购</a>
  <a href="#g-heat">信号热力图</a>
  <a href="#g-sentiment">情绪分区</a>
  <a href="#g-method">方法与限度</a>
</div></nav>

<main>

<section>
  <div class="gauges">
    <div class="gauge">
      <div class="label">核心八宽基份额合计</div>
      <div class="value">{combined.get('total_yi') or '—'}<span style="font-size:14px;color:var(--ink-3)"> 亿份</span></div>
      <div class="sub">5 日 {chg5d:+.2f}% · {label}</div>
    </div>
    <div class="gauge">
      <div class="label">国家队估算净买入（活跃日口径）</div>
      <div class="value">{('+' + f"{nt_card_val:,.0f}") if (nt_card_val or 0) >= 0 else f"{nt_card_val:,.0f}"}<span style="font-size:14px;color:var(--ink-3)"> 亿</span></div>
      <div class="sub">{theta_note} · 累计 {nt_cum_last:+,.0f} 亿 · {nt_card_label}</div>
    </div>
    <div class="gauge">
      <div class="label">上证指数距年线</div>
      <div class="value">{f"{dyl:+.2f}%" if dyl is not None else '—'}</div>
      <div class="sub">收盘 {last_close_val} · 年线 {cd.get('yearline') or '—'}</div>
    </div>
    <div class="gauge">
      <div class="label">两融余额</div>
      <div class="value">{f"{margin_last:,.0f}" if margin_last else '—'}<span style="font-size:14px;color:var(--ink-3)"> 亿</span></div>
      <div class="sub">融资余额，交易所日度披露</div>
    </div>
    {overseas_gauges}
  </div>

  <div class="light-banner" id="lightBanner">
    <div class="lb-verdict">
      <span class="lb-title">五灯共振 · {lights_date}</span>
      <span class="lb-state" style="background:{ls_color_bg};color:{ls_color_fg};">{ls_verdict}</span>
    </div>
    <div class="lb-lights">
      {light_banner_dots}
    </div>
  </div>
  <p class="footnote">份额为交易所登记口径，非估算。五日阈值（+7.5% 大规模申购 / +2% 偏高 / −2% 净赎回）为描述性分档，无预测含义。涨红跌绿，全文同。五灯为八只核心宽基逐灯投票（红&gt;绿记红，绿&gt;红记绿），同色 ≥3 盏判共振；悬停可见各灯判定依据。</p>
</section>

<section id="g-main">
  <div class="sec-head">
    <span class="sec-no">图 1</span><h2 class="sec-title">合计份额 · 上证指数 · 国家队增减持估算</h2>
    <span class="sec-note">上：份额 vs 指数 &middot; 下：当日估算 &middot; 竖虚线：官宣/护盘事件</span>
  </div>
  <div>
    <span class="range-btn active" data-range="all" onclick="setRange(this)">全部</span>
    <span class="range-btn" data-range="520" onclick="setRange(this)">近2年</span>
    <span class="range-btn" data-range="250" onclick="setRange(this)">近1年</span>
    <span class="range-btn" data-range="120" onclick="setRange(this)">近半年</span>
  </div>
  <p style="font-size:14px; margin:8px 0 10px;">最近活跃日（{nt_last_active_date or '—'}）国家队估算净{'增持' if (nt_card_val or 0) >= 0 else '减持'} <b>{abs(nt_card_val or 0):.0f} 亿</b>（{nt_days_total} 个交易日中 {nt_n_active} 天标记为国家队活跃，窗口累计 {nt_cum_last:+,.0f} 亿）。</p>
  <div id="mainChart" class="chart"></div>
  <p class="footnote">上图左轴：八只核心宽基合计份额（亿份，交易所登记值）；右轴：上证指数；绿色底带为历史「机会共振」日；<b>紫色竖虚线为官宣/护盘事件</b>（如 2026-07-20 汇金/国新/诚通官宣增持、07-13 急跌保卫战、07-16 科创杀跌日，悬停可看日期）。下图柱：当日估算净买卖——<b>红柱为买入、绿柱为卖出</b>（卖出=总净赎回达历史 90 分位的「疑似撤退日」，同样 ×θ 折算）；灰色对照柱为全市场总净申购（未归因）；紫色累计线（右轴）为活跃日估算的逐日累加，<b>可升可降</b>——净卖出日会下拉累计线，反映的是估算净买入存量的变化，仍不代表当前持仓市值（θ 为区间估计）。份额在指数下行段阶梯式跳增，是一级市场大额申购的直接足迹。估算口径：当日核心宽基总净申购 × θ（θ 由季报前十大持有人锚点标定，建仓期均值 {nt_theta}）。仅当 |总净申购| 位于历史 90 分位以上、或指数单日跌幅超过 1% 且出现净申购时，才标记为「国家队活跃日」并折算；其余交易日计为零，不冒充。</p>
</section>

<section id="g-landing">
  <div class="sec-head">
    <span class="sec-no">表 1</span><h2 class="sec-title">申购落点：资金去了哪些指数</h2>
    <span class="sec-note">自 {landing_anchor} 起</span>
  </div>
  <table>
    <thead><tr><th>基金</th><th style="text-align:right">起点（亿份）</th><th style="text-align:right">现值（亿份）</th><th style="text-align:right">变化</th><th style="text-align:right">估算金额（亿）</th></tr></thead>
    <tbody>{landing_rows}</tbody>
  </table>
  <p class="footnote">申购集中在科创与小盘，和只落在大盘蓝筹，含义完全不同。本表按交易所登记份额计算，金额 = Δ份额 × 当日收盘价（近似）。</p>
</section>

<section id="g-structure">
  <div class="sec-head">
    <span class="sec-no">图 2</span><h2 class="sec-title">份额结构与 N 日轮动</h2>
    <span class="sec-note">区分真减仓与换仓 &middot; 窗口可调</span>
  </div>
  <div>
    <span style="font-size:12.5px; color:var(--ink-3); margin-right:8px;">轮动窗口</span>
    <span class="range-btn rot-btn active" data-rot="60" onclick="setRot(this)">60日</span>
    <span class="range-btn rot-btn" data-rot="20" onclick="setRot(this)">20日</span>
    <span class="range-btn rot-btn" data-rot="120" onclick="setRot(this)">120日</span>
    <span class="range-btn rot-btn" data-rot="250" onclick="setRot(this)">250日</span>
  </div>
  <div class="grid2" style="display:grid; grid-template-columns:1fr 1fr; gap:20px;">
    <div>
      <h3 style="font-size:14px; margin:0 0 8px; color:var(--ink-2);">当前份额落点（亿份）</h3>
      <div id="structChart" class="chart"></div>
    </div>
    <div>
      <h3 style="font-size:14px; margin:0 0 8px; color:var(--ink-2);"><span id="rotWindowLabel">60</span> 日变化（%）</h3>
      <div id="rotationChart" class="chart"></div>
    </div>
  </div>
  <p class="footnote">某宽基减、另一宽基同步增，是换仓而非撤离；八只同减才读作整体撤退信号。</p>
</section>

<section id="g-detail">
  <div class="sec-head">
    <span class="sec-no">图 3</span><h2 class="sec-title">单基金详情：K 线、跟踪指数与份额日增量</h2>
    <span class="sec-note">份额拆分已复权</span>
  </div>
  <div>
    <span class="range-btn etf-btn active" data-etf="510300" onclick="setEtf(this)">510300 沪深300</span>
    <span class="range-btn etf-btn" data-etf="512100" onclick="setEtf(this)">512100 中证1000</span>
    <span class="range-btn etf-btn" data-etf="588000" onclick="setEtf(this)">588000 科创50</span>
  </div>
  <div id="etfChart" class="chart"></div>
  <p class="footnote">上栏 K 线（红涨绿跌）叠跟踪指数（右轴）；下栏份额日增量柱，正为净申购。份额拆分折算产生的机械跳变已按复权因子处理，不会误读为申购。</p>
</section>

<section id="g-flow">
  <div class="sec-head">
    <span class="sec-no">表 2</span><h2 class="sec-title">当日净申购居前的基金</h2>
    <span class="sec-note">真实份额口径 &middot; 点灯看依据</span>
  </div>
  <table>
    <thead><tr><th>基金</th><th style="text-align:right">净申购（亿）</th><th>共振</th><th>置信度</th><th>五灯</th><th>异常</th></tr></thead>
    <tbody>{flow_rows}</tbody>
  </table>
  <p class="footnote">五灯：价格位置 / 份额流向 / 指数回撤 / 市场热度 / 融资杠杆。红为风险、绿为机会、灰为中性；悬停灯点可见判定依据。</p>
</section>

<section id="g-margin">
  <div class="sec-head">
    <span class="sec-no">图 4</span><h2 class="sec-title">两融余额：杠杆出清的背景</h2>
    <span class="sec-note">绝对值口径 &middot; 快速变化率见五灯「融资杠杆」</span>
  </div>
  <div id="marginChart" class="chart"></div>
  <p class="footnote">融资余额快速下降记录杠杆仓位被强平出清；回升则记录杠杆回归。国家队申购历史上与指数回撤期相伴。</p>
</section>

<section id="g-heat">
  <div class="sec-head">
    <span class="sec-no">图 5</span><h2 class="sec-title">信号热力图：逐日 × 逐基金</h2>
    <span class="sec-note">窗口可调 &middot; 点击格子看判定依据</span>
  </div>
  <div>
    <span style="font-size:12.5px; color:var(--ink-3); margin-right:8px;">时间窗口</span>
    <span class="range-btn heat-btn" data-heat="60" onclick="setHeat(this)">60日</span>
    <span class="range-btn heat-btn active" data-heat="90" onclick="setHeat(this)">90日</span>
    <span class="range-btn heat-btn" data-heat="250" onclick="setHeat(this)">250日</span>
    <span class="range-btn heat-btn" data-heat="0" onclick="setHeat(this)">全部</span>
  </div>
  <div id="heatChart" class="chart"></div>
  <div id="evidenceCard" class="ev-panel" style="display:none;"></div>
  <p class="footnote">每格为一基金一日的方向信号：红 = SHORT（高位净赎回语境）、绿 = LONG（吸筹语境）、灰 = 中性、浅格 = 数据不足（BLOCKED，不冒充）。点击任意格在此展开该日该基金的五灯判定依据（指标值 · 判定理由 · 净申购）。</p>
</section>

<section id="g-sentiment">
  <div class="sec-head">
    <span class="sec-no">图 6</span><h2 class="sec-title">市场热度：两市成交额 60 日分位</h2>
    <span class="sec-note">≤20 冷清 · ≥80 过热 · 样本不足灰</span>
  </div>
  <div id="sentChart" class="chart"></div>
  <p class="footnote">两市成交额（沪+深）取 60 日滚动分位。过热（红区）常与危险共振相伴，冷清（绿区）常与机会共振相伴。样本不足 20 日时不判定。融资余额的绝对值走势见图 4，此处不再重复绘制分位版。</p>
</section>

<section id="g-coverage">
  <div class="sec-head">
    <span class="sec-no">表 3</span><h2 class="sec-title">数据覆盖台账：近 15 个交易日</h2>
    <span class="sec-note">份额 T+1 披露 &middot; 断档一眼可见</span>
  </div>
  <table>
    <thead><tr><th>日期</th><th style="text-align:right">K 线</th><th style="text-align:right">份额</th><th style="text-align:right">融资</th><th style="text-align:right">信号</th><th>状态</th></tr></thead>
    <tbody>{coverage_rows}</tbody>
  </table>
  <p class="footnote">K 线/份额列数字为当日有数据的基金数（池内共 18 只）。份额比 K 线晚一天属正常（T+1 披露）；份额连续缺失或为零的日期提示披露断档。折算日与披露噪声日的份额差分已标记无效，不参与估算。</p>
</section>

<section id="g-method">
  <div class="sec-head">
    <span class="sec-no">&sect;</span><h2 class="sec-title">方法、来源与限度</h2>
  </div>
  <details class="method-details" open>
    <summary>指标计算方法与阈值（点击展开/收起）</summary>
    <div class="overflow-x">
    <table>
      <thead><tr><th>指标</th><th>计算方法</th><th>红灯（风险）</th><th>绿灯（机会）</th></tr></thead>
      <tbody>
        <tr><td>价格位置</td><td>收盘价在近 250 日中的分位</td><td class="dn">分位 ≥80%（高位）</td><td class="up">分位 ≤20%（低位）</td></tr>
        <tr><td>份额流向</td><td>当日 Δ份额×净值 在近 300 日净申购历史中的分位</td><td class="dn">分位 ≤5%（净赎回极端）</td><td class="up">分位 ≥95%（净申购极端）</td></tr>
        <tr><td>指数回撤</td><td>跟踪指数当日涨跌幅</td><td>—</td><td class="up">指数跌 ≥1.5%（护盘语境）</td></tr>
        <tr><td>市场热度</td><td>两市成交额（沪+深）60 日滚动分位</td><td class="dn">分位 ≥80%（过热）</td><td class="up">分位 ≤20%（冷清）</td></tr>
        <tr><td>融资杠杆</td><td>融资余额 5 日变化率</td><td class="dn">≥+1%（快速加杠杆）</td><td class="up">≤−1%（快速去杠杆）</td></tr>
        <tr><td>共振判定</td><td colspan="3">同色灯 ≥3 盏 → 危险共振（红）/ 机会共振（绿），否则中性。灰灯 = 中性区间或数据缺失。</td></tr>
        <tr><td>国家队活跃日</td><td colspan="3">当日核心宽基总净申购 ≥ 历史 90 分位，或指数跌 &gt;1% 且净申购为正 → 估算 = 总净申购 × θ；其余日记 0，不冒充。</td></tr>
        <tr><td>份额折算清洗</td><td colspan="3">份额单日跳变 &gt;15% 进入候选：资产守恒（(1+Δ份额)×(1+Δ价) ≈ 折算比）且复权价连续 → 判折算，差分作废；次日反向大幅回归 → 判披露噪声，差分作废。原始披露值永不修改。</td></tr>
      </tbody>
    </table>
    </div>
  </details>
  <ol class="method">
    <li><b>为什么份额是正确的序列。</b> 二级市场买卖只改价格、不改 ETF 总份额；只有一级市场申赎才改。交易所每日披露全部 ETF 的登记份额（T+1）。本报告读取的正是这份登记数据，不是资金流估算。</li>
    <li><b>申购能证明什么、不能证明什么。</b> 份额跳增证明有人大额申购，但不证明是谁。归因国家队需要三个证据合流：官方公告、季报前十大持有人（滞后约一季度）、以及行为模式吻合（回撤期、多宽基同步、量级匹配）。本报告的「估算」仅在证据合流时给出，且始终标注为估算。</li>
    <li><b>θ 锚点校准。</b> 季报披露的汇金名义持仓变化（ΔNT）除以同区间总份额变化（ΔS）得到 θ。θ&gt;1 表示国家队增持超过总增量——同期有其他投资者净赎回。θ 为区间估计，非逐日真值。</li>
    <li><b>数据来源。</b> ETF 份额：上交所/深交所日度披露（经 akshare）。行情与指数：腾讯行情公开接口。融资余额：交易所两融日度披露。全部为免费公开数据，无付费 feed。</li>
    <li><b>分档是标签，不是信号。</b> 五日 +7.5% / +2% / −2% 等阈值用于描述性分档，便于逐日比较。回测显示：历史「机会共振」信号后 20 日平均收益为负——护盘信号是市场压力的标记，不构成买入依据。</li>
    <li><b>覆盖范围。</b> 核心层为 2026 年 7 月官方公告点名的沪深宽基；扩展层含国新/诚通风格探头；观察层为散户情绪对照。深市数据自 2025 年中起可回溯。</li>
  </ol>
</section>

<div class="disclosure">
  <b>披露与日期戳纪律。</b> 本报告全部数字盖到披露日戳，引用时请附日期。数据可能因交易所修订而后向修正。本项目为描述性市场结构研究，不构成投资建议、推荐或预测；市场有风险，决策请独立判断或咨询持牌机构。生成于 {gen_time}，由本地数据管道自动重建，可用 <span class="mono">python cli.py report --date {date}</span> 复现任意历史日期。
</div>

</main>

<script>
var CD = {chart_json};
var C = {{ ink2:'#5d6d7e', ink3:'#85929e', rule:'#e8e4da', up:'#b03a2e', dn:'#1e8449', accent:'#8e44ad' }};
var mainChart = echarts.init(document.getElementById('mainChart'));
var structChart = echarts.init(document.getElementById('structChart'));
var rotationChart = echarts.init(document.getElementById('rotationChart'));
var marginChart = echarts.init(document.getElementById('marginChart'));
var etfChart = echarts.init(document.getElementById('etfChart'));

var axisStyle = {{
  axisLine: {{lineStyle:{{color:'#b8b2a6'}}}},
  axisLabel: {{color:'#5d6d7e', fontSize:11}},
  splitLine: {{lineStyle:{{color:'#eeebe2'}}}},
}};
var tipStyle = {{
  trigger:'axis',
  backgroundColor:'rgba(28,40,51,.94)', borderColor:'#5d6d7e',
  textStyle:{{color:'#fbfaf7', fontSize:12}},
}};

// 事件竖线（不显示文字标签——密集事件竖排文字会重叠,日期含义放 tooltip 与图注）
function eventLines(dates) {{
  if (!CD.nt || !CD.nt.events) return [];
  return CD.nt.events.filter(function(e) {{ return dates.indexOf(e.date) >= 0; }}).map(function(e) {{
    return {{ xAxis: e.date,
             lineStyle: {{color:'#8e44ad', type:'dashed', width:1.2, opacity:0.8}} }};
  }});
}}
// 共振背景带（机会共振日）
function resoAreas(dates) {{
  if (!CD.resonance_days) return [];
  return CD.resonance_days.filter(function(r) {{ return dates.indexOf(r.date) >= 0 && r.type == '机会共振'; }})
    .map(function(r) {{ return [{{xAxis:r.date}}, {{xAxis:r.date}}]; }});
}}

// ---- 图1: 主图双 grid（上:份额vs指数 下:国家队增减持估算,共享日期轴联动）----
function renderMain(nDays) {{
  var dates = CD.dates, shares = CD.shares, index = CD.index;
  var nd = CD.nt ? CD.nt.daily : [], cum = CD.nt ? CD.nt.cum : [], total = CD.nt ? CD.nt.total : [];
  if (nDays && nDays < dates.length) {{
    dates = dates.slice(-nDays); shares = shares.slice(-nDays); index = index.slice(-nDays);
    nd = nd.slice(-nDays); cum = cum.slice(-nDays); total = total.slice(-nDays);
  }}
  var idxOf = {{}}; dates.forEach(function(d, i) {{ idxOf[d] = i; }});
  // 双 grid: 上 52%~78% 主图,下 82%~96% 增减持;dataZoom 归属上 grid
  mainChart.setOption({{
    animation: false,
    color: ['#b03a2e', '#85929e', '#b03a2e', '#c8d0d4', '#8e44ad'],
    tooltip: Object.assign({{axisPointer:{{type:'cross'}}}}, tipStyle),
    axisPointer: {{ link: [{{xAxisIndex: 'all'}}] }},
    legend: {{ data:[{{name:'合计份额(亿份,左轴)', icon:'rect'}}, {{name:'上证指数(右轴)', icon:'rect'}},
                    {{name:'当日估算净买入(亿)', icon:'rect'}}, {{name:'全市场总净申购(对照)', icon:'rect'}},
                    {{name:'累计估算(亿,右轴)', icon:'rect'}}],
      top: 0, left: 60, itemGap: 18, itemWidth: 14,
      textStyle:{{color:'#5d6d7e', fontSize:11}} }},
    grid: [
      {{ left: 64, right: 64, top: 52, height: '30%' }},
      {{ left: 64, right: 64, top: '72%', bottom: 56 }}
    ],
    dataZoom: [ {{type:'slider', xAxisIndex:[0,1], start:60, end:100, bottom:8, height:16,
                    borderColor:'#d5d0c5', fillerColor:'rgba(142,68,173,.08)'}},
                {{type:'inside', xAxisIndex:[0,1]}} ],
    xAxis: [
      Object.assign({{type:'category', gridIndex:0, data:dates}}, axisStyle),
      Object.assign({{type:'category', gridIndex:1, data:dates}}, axisStyle,
                    {{axisLabel:{{show:false}}}})
    ],
    yAxis: [
      Object.assign({{type:'value', scale:true, position:'left', gridIndex:0}}, axisStyle),
      Object.assign({{type:'value', scale:true, position:'right', gridIndex:0, splitLine:{{show:false}}}}, axisStyle),
      Object.assign({{type:'value', scale:true, position:'left', gridIndex:1}}, axisStyle),
      Object.assign({{type:'value', scale:true, position:'right', gridIndex:1, splitLine:{{show:false}}}}, axisStyle)
    ],
    series: [
      {{ name:'合计份额(亿份,左轴)', type:'line', xAxisIndex:0, yAxisIndex:0, data:shares, showSymbol:false, progressive:0,
         lineStyle:{{width:2, color:'#b03a2e'}}, areaStyle:{{color:'rgba(176,58,46,0.05)'}},
         markArea: {{ silent:true, itemStyle:{{color:'rgba(30,132,73,0.06)'}}, data: resoAreas(dates) }},
         markLine: {{ silent:true, symbol:['none','none'],
           lineStyle:{{color:'#8e44ad', type:'dashed', width:1.2, opacity:0.8}},
           label:{{show:false}},
           data: eventLines(dates) }} }},
      {{ name:'上证指数(右轴)', type:'line', xAxisIndex:0, yAxisIndex:1, data:index, showSymbol:false, progressive:0,
         lineStyle:{{width:1.4, color:'#85929e'}}, opacity:0.85 }},
      {{ name:'当日估算净买入(亿)', type:'bar', xAxisIndex:1, yAxisIndex:2, data:nd, barMaxWidth: 5, progressive:0,
         itemStyle:{{color: function(p) {{ return p.value>=0 ? '#b03a2e' : '#1e8449'; }} }},
         markLine: {{ silent:true, symbol:['none','none'],
           lineStyle:{{color:'#8e44ad', type:'dashed', width:1.2, opacity:0.8}},
           label:{{show:false}}, data: eventLines(dates) }} }},
      {{ name:'全市场总净申购(对照)', type:'bar', xAxisIndex:1, yAxisIndex:2, data:total, barMaxWidth: 5, progressive:0,
         barGap: '-100%', itemStyle:{{color:'rgba(133,146,158,0.3)'}} }},
      {{ name:'累计估算(亿,右轴)', type:'line', xAxisIndex:1, yAxisIndex:3, data:cum, showSymbol:false, progressive:0,
         lineStyle:{{width:1.8, color:'#8e44ad'}}, markLine: {{ silent:true, symbol:'none',
           lineStyle:{{color:'#8e44ad', type:'dashed', width:1, opacity:0.5}},
           data:[{{yAxis:0}}], label:{{show:false}} }} }}
    ]
  }}, true);
}}
// 范围切换改为驱动 dataZoom（不切片:全部序列常驻,联动缩放,且全部档仍可回看）
var RANGE_ZOOM = {{ '520': [74, 100], '250': [85, 100], '120': [93, 100], 'all': [0, 100] }};
function setRange(btn) {{
  document.querySelectorAll('#g-main .range-btn').forEach(function(b){{ b.classList.remove('active'); }});
  btn.classList.add('active');
  var r = btn.getAttribute('data-range');
  var z = RANGE_ZOOM[r] || [0, 100];
  mainChart.dispatchAction({{ type: 'dataZoom', start: z[0], end: z[1] }});
}}
renderMain(null);

// ---- 结构与轮动（轮动窗口可调: 60/20/120/250 日）----
structChart.setOption({{
  tooltip: {{ backgroundColor:'rgba(28,40,51,.94)', textStyle:{{color:'#fbfaf7', fontSize:12}} }},
  grid: {{ left: 120, right: 50, top: 12, bottom: 30 }},
  xAxis: Object.assign({{type:'value'}}, axisStyle),
  yAxis: {{ type:'category', data:CD.structure.map(function(s){{return s.code+' '+s.name;}}).reverse(),
    axisLabel:{{fontSize:11, color:'#5d6d7e'}}, axisLine:{{lineStyle:{{color:'#b8b2a6'}}}} }},
  series: [{{ type:'bar', data:CD.structure.map(function(s){{return s.value;}}).reverse(),
    itemStyle:{{color:'#7d6d8f'}}, barMaxWidth: 16,
    label:{{show:true, position:'right', formatter:'{{c}}', fontSize:10.5, color:'#5d6d7e'}} }}]
}});
function renderRot(win) {{
  var data = (CD.rotation && CD.rotation[win]) || [];
  if (!data.length) return;
  rotationChart.setOption({{
    tooltip: {{ backgroundColor:'rgba(28,40,51,.94)', textStyle:{{color:'#fbfaf7', fontSize:12}} }},
    grid: {{ left: 120, right: 55, top: 12, bottom: 30 }},
    xAxis: Object.assign({{type:'value'}}, axisStyle),
    yAxis: {{ type:'category', data:data.map(function(s){{return s.code+' '+s.name;}}).reverse(),
      axisLabel:{{fontSize:11, color:'#5d6d7e'}}, axisLine:{{lineStyle:{{color:'#b8b2a6'}}}} }},
    series: [{{ type:'bar', data:data.map(function(s){{return s.chg;}}).reverse(),
      itemStyle:{{color:function(p){{return p.value>=0?'#b03a2e':'#1e8449';}}}}, barMaxWidth: 16,
      label:{{show:true, position:'right', formatter:'{{c}}%', fontSize:10.5, color:'#5d6d7e'}} }}]
  }});
  var lbl = document.getElementById('rotWindowLabel');
  if (lbl) lbl.textContent = win;
}}
function setRot(btn) {{
  document.querySelectorAll('.rot-btn').forEach(function(b){{ b.classList.remove('active'); }});
  btn.classList.add('active');
  renderRot(btn.getAttribute('data-rot'));
}}
renderRot('60');

// ---- 图5: 两融 ----
marginChart.setOption({{
  tooltip: tipStyle,
  grid: {{ left: 70, right: 20, top: 14, bottom: 32 }},
  xAxis: Object.assign({{type:'category', data:CD.dates, axisLabel:{{formatter:function(v){{return v.slice(0,7);}}, color:'#5d6d7e', fontSize:10}}}}, axisStyle),
  yAxis: Object.assign({{type:'value', scale:true}}, axisStyle),
  series: [{{ type:'line', data:CD.margin, showSymbol:false, connectNulls:true,
    lineStyle:{{width:1.5, color:'#7d6d8f'}} }}]
}});

// ---- 图4: 单ETF（双 panel + 共享 dataZoom,下 panel 日期轴在缩略框上方可见）----
function renderEtf(code) {{
  var D = CD.detail[code];
  if (!D) return;
  var last = Math.min(D.dates.length, 520);
  var dates = D.dates.slice(-last), kline = D.kline.slice(-last),
      flow = D.flow.slice(-last), index = D.index.slice(-last);
  etfChart.setOption({{
    tooltip: Object.assign({{axisPointer:{{type:'cross'}}}}, tipStyle),
    legend: {{data:['日K(左轴)','跟踪指数(右轴)','份额日增量(亿份)'], top: 0, itemGap: 20, textStyle:{{color:'#5d6d7e', fontSize:11}}}},
    axisPointer: {{link: [{{xAxisIndex: 'all'}}], label: {{backgroundColor: '#5d6d7e'}}}},
    grid: [
      {{ left: 56, right: 60, top: 52, height: '54%' }},
      {{ left: 56, right: 60, top: '74%', height: '17%' }}
    ],
    dataZoom: [ {{type:'slider', xAxisIndex:[0,1], start:0, end:100, bottom:2, height:18,
                  borderColor:'#d5d0c5', fillerColor:'rgba(142,68,173,.08)',
                  labelFormatter: function(v) {{ return String(v == null ? '' : v).slice(0,10); }} }},
                {{type:'inside', xAxisIndex:[0,1]}} ],
    xAxis: [
      Object.assign({{type:'category', data:dates, gridIndex:0, boundaryGap:true, axisLabel:{{show:false}}}}, axisStyle),
      Object.assign({{type:'category', data:dates, gridIndex:1, boundaryGap:true,
        axisLabel:{{show:true, fontSize:9, color:'#85929e'}}}}, axisStyle)
    ],
    yAxis: [
      Object.assign({{scale:true, gridIndex:0, position:'left', splitLine:{{show:false}}}}, axisStyle),
      Object.assign({{scale:true, gridIndex:0, position:'right', splitLine:{{show:false}}}}, axisStyle),
      Object.assign({{scale:true, gridIndex:1, splitLine:{{show:false}}, axisLabel:{{fontSize:9, color:'#85929e'}}}}, axisStyle)
    ],
    series: [
      {{ name:'日K(左轴)', type:'candlestick', data:kline, xAxisIndex:0, yAxisIndex:0,
         itemStyle:{{color:'#b03a2e', color0:'#1e8449', borderColor:'#b03a2e', borderColor0:'#1e8449'}} }},
      {{ name:'跟踪指数(右轴)', type:'line', data:index, xAxisIndex:0, yAxisIndex:1,
         showSymbol:false, lineStyle:{{width:1.1, color:'#85929e'}}, opacity:0.7 }},
      {{ name:'份额日增量(亿份)', type:'bar', data:flow, xAxisIndex:1, yAxisIndex:2, barMaxWidth: 4,
         itemStyle:{{color:function(p){{return p.value>=0?'#b03a2e':'#1e8449';}}}} }}
    ]
  }}, true);
}}
function setEtf(btn) {{
  document.querySelectorAll('.etf-btn').forEach(function(b){{ b.classList.remove('active'); }});
  btn.classList.add('active');
  renderEtf(btn.getAttribute('data-etf'));
}}
renderEtf('510300');

// ---- 图5: 信号热力图（学 A-Resonance ResonanceHeatmap,窗口可调,点击格出证据卡）----
var heatChart = echarts.init(document.getElementById('heatChart'));
function renderHeat(nDays) {{
  var HM = CD.heatmap;
  if (!HM || !HM.dates || !HM.dates.length) return;
  var dates = (nDays && nDays > 0 && nDays < HM.dates.length) ? HM.dates.slice(-nDays) : HM.dates;
  // 宽窗口时图表加高，避免 250 日格子过窄
  var el = document.getElementById('heatChart');
  el.style.height = (dates.length > 160 ? 520 : 420) + 'px';
  heatChart.resize();
  var dIdx = {{}}; HM.dates.forEach(function(d, i) {{ dIdx[d] = i; }});
  var data = [];
  for (var di = 0; di < dates.length; di++) {{
    for (var ci = 0; ci < HM.codes.length; ci++) {{
      var v = HM.values[dIdx[dates[di]]][ci];
      data.push([di, ci, v == null ? '-' : v]);
    }}
  }}
  heatChart.setOption({{
    tooltip: {{ position: 'top',
      backgroundColor:'rgba(28,40,51,.94)', borderColor:'#5d6d7e',
      textStyle:{{color:'#fbfaf7', fontSize:12}},
      formatter: function(p) {{
        var code = HM.codes[p.value[1]], name = HM.names[code] || code;
        var d = dates[p.value[0]];
        var flow = (CD.heatmap.flows[d] && CD.heatmap.flows[d][code]);
        var dir = p.value[2] === 2 ? 'LONG 吸筹' : p.value[2] === -2 ? 'SHORT 出货' : p.value[2] === 0 ? '中性' : '数据不足';
        return '<b>' + d + ' · ' + name + '</b><br/>信号: ' + dir +
               (flow != null ? '<br/>净申购: ' + flow + ' 亿' : '<br/>净申购: 数据不足');
      }} }},
    grid: {{ left: 150, right: 30, top: 10, bottom: 60 }},
    xAxis: {{ type:'category', data: dates, splitArea:{{show:false}},
      axisLine:{{lineStyle:{{color:'#b8b2a6'}}}}, axisTick:{{show:false}},
      axisLabel:{{fontSize:9, color:'#85929e', interval: Math.max(0, Math.floor(dates.length/8)),
        formatter:function(v){{return v.slice(2,7);}}}} }},
    yAxis: {{ type:'category', data: HM.codes.map(function(c){{ return c + ' ' + (HM.names[c]||'').replace('ETF',''); }}),
      axisLine:{{lineStyle:{{color:'#b8b2a6'}}}}, axisTick:{{show:false}}, axisLabel:{{fontSize:10.5, color:'#5d6d7e'}} }},
    visualMap: {{ show:false, min:-2, max:2,
      inRange: {{ color:['#c8e6c9', '#f4f2ec', '#f5c6c1'] }} }},
    series: [{{ type:'heatmap', data: data,
      itemStyle:{{ borderColor:'#fbfaf7', borderWidth:1, borderRadius:2,
        color: function(p) {{
          var v = p.value[2];
          if (v === '-') return '#eeebe2';
          return v === 2 ? '#4a9d6e' : v === -2 ? '#c0504a' : '#d8d4c8';
        }} }},
      emphasis:{{ itemStyle:{{ shadowBlur:4, shadowColor:'rgba(0,0,0,.35)' }} }} }}]
  }}, true);
  // 点击格子 → 证据卡（P6）——窗口切换后重绑（setOption notMerge=true 会清旧 handler）
  heatChart.off('click');
  heatChart.on('click', function(p) {{
    if (p.componentType !== 'series') return;
    var code = HM.codes[p.value[1]], d = dates[p.value[0]];
    showEvidence(d, code);
  }});
}}
function setHeat(btn) {{
  document.querySelectorAll('.heat-btn').forEach(function(b){{ b.classList.remove('active'); }});
  btn.classList.add('active');
  renderHeat(parseInt(btn.getAttribute('data-heat'), 10));
}}
renderHeat(90);

// ---- 证据卡（学 A-Resonance EvidenceCard: 计算方法/原始数据/判定理由）----
function showEvidence(date, code) {{
  var HM = CD.heatmap;
  var panel = document.getElementById('evidenceCard');
  var name = (HM.names[code] || code);
  var raw = (HM.evidence[date] && HM.evidence[date][code]) || '';
  var flow = (HM.flows[date] && HM.flows[date][code]);
  var html = '<span class="ev-close" onclick="this.parentNode.style.display=&quot;none&quot;">✕</span>';
  html += '<h4>' + date + ' · ' + code + ' ' + name + '</h4>';
  html += '<div class="ev-grid">';
  html += '<div class="ev-item"><div class="n">净申购（亿）</div><div class="v">' +
          (flow != null ? flow : '数据不足（BLOCKED,不冒充）') + '</div></div>';
  if (raw) {{
    try {{
      var pj = JSON.parse(raw);
      (pj.lights || []).forEach(function(l) {{
        html += '<div class="ev-item"><div class="n">' + l.name + '</div><div class="v">' +
                (l.value != null ? l.value : '—') + '　' + (l.detail || '') + '</div></div>';
      }});
      (pj.reasons || []).forEach(function(r) {{
        html += '<div class="ev-item"><div class="n">置信度调整</div><div class="v">' + r + '</div></div>';
      }});
      if (pj.blocked_reason) {{
        html += '<div class="ev-item"><div class="n">数据不足原因</div><div class="v">' + pj.blocked_reason + '</div></div>';
      }}
    }} catch(e) {{
      html += '<div class="ev-item"><div class="n">原始证据</div><div class="v">' + raw.slice(0, 300) + '</div></div>';
    }}
  }} else {{
    html += '<div class="ev-item"><div class="n">证据</div><div class="v">该日无信号记录（未生成或 BLOCKED）</div></div>';
  }}
  html += '</div>';
  panel.innerHTML = html;
  panel.style.display = 'block';
}}

// ---- 图6: 市场热度（成交额 60 日分位,融资余额绝对值见图 4 不重复）----
var sentChart = echarts.init(document.getElementById('sentChart'));
(function() {{
  var S = CD.sentiment;
  if (!S || !S.dates || !S.dates.length) return;
  sentChart.setOption({{
    tooltip: Object.assign({{valueFormatter: function(v){{ return v == null ? '—' : v + ' 分位'; }}}}, tipStyle),
    grid: {{ left: 50, right: 20, top: 20, bottom: 30 }},
    xAxis: Object.assign({{type:'category', data:S.dates,
      axisLabel:{{formatter:function(v){{return v.slice(0,7);}}, color:'#5d6d7e', fontSize:10}}}}, axisStyle),
    yAxis: Object.assign({{type:'value', min:0, max:100}}, axisStyle),
    series: [
      {{ name:'两市成交额分位', type:'line', data:S.amt_pct, showSymbol:false, connectNulls:true,
        lineStyle:{{width:1.5, color:'#b03a2e'}},
        markArea: {{ silent:true, itemStyle:{{color:'rgba(176,58,46,.06)'}},
          data: [[{{yAxis:80}}, {{yAxis:100}}]] }} }}
    ]
  }});
}})();

// ---- 多图联动（学 A-Resonance echarts.connect 思想：日期轴共用的图同步缩放/hover）----
// 注: ntChart 已合并进 mainChart 双 grid（内建 axisPointer 联动）,不再单独 connect
try {{ echarts.connect([mainChart, marginChart]); }} catch(e) {{}}

window.addEventListener('resize', function(){{ mainChart.resize(); structChart.resize();
  rotationChart.resize(); marginChart.resize(); etfChart.resize();
  heatChart.resize(); sentChart.resize(); }});
</script>
</body></html>"""

    # echarts 内联注入（f-string 无法直接嵌入含大量 {} 的 JS 源码,用占位符替换）
    html = html.replace("/*__ECHARTS_INLINE__*/", echarts_src)

    out = Path(out_dir) if out_dir else REPORT_DIR
    out = out / f"etf_ntm_{date}.html"
    atomic_write(out, html.encode("utf-8"))
    return out
