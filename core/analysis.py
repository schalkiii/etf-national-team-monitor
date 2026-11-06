# -*- coding: utf-8 -*-
"""每日分析编排：读取库数据 → 三层信号 → 落库 signals。
把 core 纯函数与 store 组装起来的编排层（学 A-Resonance scheduler 组装哲学）。"""
from __future__ import annotations

import json
import sqlite3

from core.config import (
    COMBINED_5D_ELEVATED, COMBINED_5D_LARGE, COMBINED_5D_REDEEM,
    CORE_CODES, ETF_POOL,
)
from core.models import Direction, FlowSource, LightColor, LightEvidence, Signal
from core.signal_engine import (
    apply_confidence_rules, describe_flow, detect_anomaly,
    light_index_drawdown, light_margin, light_price_position,
    light_share_flow, light_market_heat, resonance_verdict,
)


def _index_change_pct(con, index_code: str, date: str, lookback_days: int = 10) -> float | None:
    rows = con.execute(
        "SELECT date, close FROM index_daily WHERE index_code=? AND date<=? ORDER BY date DESC LIMIT ?",
        (index_code, date, lookback_days),
    ).fetchall()
    if len(rows) < 2:
        return None
    prev, cur = rows[1], rows[0]
    if not prev["close"]:
        return None
    return (cur["close"] / prev["close"] - 1.0) * 100.0


def _market_amount(con, date: str) -> tuple[float | None, list[float]]:
    """两市成交额背景：sh000001 + sz399001 合计。返回 (当日, 历史)。"""
    idx = ["sh000001", "sz399001"]
    dates = [r["date"] for r in con.execute(
        "SELECT date FROM index_daily WHERE index_code='sh000001' AND date<=? ORDER BY date DESC LIMIT 250",
        (date,))]
    if not dates:
        return None, []
    dates_set = set(dates)
    per_day = {}
    for ic in idx:
        for r in con.execute(
            "SELECT date, amount FROM index_daily WHERE index_code=? AND date<=? ORDER BY date DESC LIMIT 250",
            (ic, date)):
            if r["date"] in dates_set:
                per_day[r["date"]] = per_day.get(r["date"], 0.0) + (r["amount"] or 0.0)
    hist = [per_day[d] for d in sorted(per_day)]
    cur_total = per_day.get(max(per_day)) if per_day else None
    return cur_total, hist


def _margin_chg(con, date: str) -> float | None:
    """融资余额 5 日变化%。表缺失或数据不足返回 None（优雅降级,L5 灰灯）。"""
    try:
        rows = con.execute(
            "SELECT date, close FROM margin_daily WHERE date<=? ORDER BY date DESC LIMIT 6", (date,)
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if len(rows) < 6:
        return None
    latest, oldest = rows[0], rows[-1]
    if not oldest["close"]:
        return None
    return (latest["close"] / oldest["close"] - 1.0) * 100.0


def _invalid_delta_set(con: sqlite3.Connection) -> set[tuple[str, str]]:
    """读取无效 Δshares 标记（折算日/披露噪声/口径断裂）。表不存在时返回空集。"""
    try:
        rows = con.execute("SELECT code, date FROM invalid_delta").fetchall()
        return {(r["code"], r["date"]) for r in rows}
    except sqlite3.OperationalError:
        return set()


def compute_signals_for_date(con: sqlite3.Connection, date: str) -> list[Signal]:
    """对全部标的计算某日三层信号。"""
    signals = []
    mkt_amount, mkt_hist = _market_amount(con, date)
    invalid_delta = _invalid_delta_set(con)
    for code, meta in ETF_POOL.items():
        rows = con.execute(
            """SELECT date, close, amount, shares, adjust_factor FROM etf_daily
               WHERE code=? AND date<=? ORDER BY date DESC LIMIT 300""",
            (code, date),
        ).fetchall()
        if len(rows) < 30:
            signals.append(Signal(code, date, Direction.BLOCKED, "低", blocked_reason=f"历史数据不足({len(rows)}日)"))
            continue
        rows = list(reversed(rows))
        cur = rows[-1]
        if cur["close"] is None:
            signals.append(Signal(code, date, Direction.BLOCKED, "低", blocked_reason="当日无价格数据"))
            continue
        share_rows = [r for r in rows if r["shares"] is not None
                      and (code, r["date"]) not in invalid_delta]
        delta_shares_yi = None
        if len(share_rows) >= 2 and share_rows[-1]["date"] == cur["date"]:
            # 前一有效点距当日 >10 天（跨无效区）→ Δshares 不可信,弃用不冒充
            from datetime import date as _date
            gap_days = (_date.fromisoformat(share_rows[-1]["date"])
                        - _date.fromisoformat(share_rows[-2]["date"])).days
            if gap_days <= 10:
                prev_s = share_rows[-2]["shares"] * share_rows[-2]["adjust_factor"]
                cur_s = share_rows[-1]["shares"] * share_rows[-1]["adjust_factor"]
                delta_shares_yi = (cur_s - prev_s) / 1e8
        net_flow_yi, flow_source = describe_flow(delta_shares_yi, cur["close"])

        flow_hist = []
        from datetime import date as _date
        for i in range(1, len(share_rows)):
            a, b = share_rows[i - 1], share_rows[i]
            if a["shares"] and b["shares"]:
                # 相邻两有效点日历间隔 >10 天（跨无效区/长期缺披露）→ 差分不可信,不入历史
                gap_days = (_date.fromisoformat(b["date"]) - _date.fromisoformat(a["date"])).days
                if gap_days > 10:
                    continue
                d = (b["shares"] * b["adjust_factor"] - a["shares"] * a["adjust_factor"]) / 1e8
                v = d * (b["close"] if b["close"] else 0.0)
                flow_hist.append(v)
        price_hist = [r["close"] for r in rows if r["close"]]
        idx_chg = _index_change_pct(con, meta["index"], date)

        lights = [
            light_price_position(cur["close"], price_hist),
            light_share_flow(net_flow_yi, flow_hist),
            light_index_drawdown(idx_chg),
            light_market_heat(mkt_amount, mkt_hist) if mkt_amount else
                LightEvidence("市场热度", LightColor.GRAY, None, "成交额缺失"),
            light_margin(_margin_chg(con, date)),
        ]
        resonance, green, red = resonance_verdict(lights)
        anomaly, z = (False, None)
        if net_flow_yi is not None and flow_hist:
            anomaly, z = detect_anomaly(net_flow_yi, flow_hist)

        direction = Direction.NEUTRAL
        if net_flow_yi is not None and delta_shares_yi is not None:
            if delta_shares_yi > 0 and (green >= 2 or (idx_chg is not None and idx_chg <= -1.0)):
                direction = Direction.LONG
            elif delta_shares_yi < 0 and red >= 2:
                direction = Direction.SHORT

        base = "高" if max(green, red) >= 4 else ("中" if resonance != "中性" else "低")
        reasons = []
        conf = apply_confidence_rules(base, net_flow_yi, lights, reasons)

        signals.append(Signal(
            code, date, direction, conf,
            net_flow_yi=round(net_flow_yi, 2) if net_flow_yi is not None else None,
            flow_source=flow_source, lights=lights, resonance=resonance,
            anomaly=anomaly, anomaly_sigma=z, grade_reasons=reasons,
        ))
    return signals


def persist_signals(con: sqlite3.Connection, signals: list[Signal]) -> None:
    for s in signals:
        payload = json.dumps({
            "lights": [{"name": l.name, "color": l.color.value, "value": l.value, "detail": l.detail} for l in s.lights],
            "reasons": s.grade_reasons,
            "blocked_reason": s.blocked_reason,
            "anomaly": s.anomaly, "anomaly_sigma": s.anomaly_sigma,
        }, ensure_ascii=False)
        con.execute(
            """INSERT INTO signals (code, date, direction, confidence, resonance, net_flow_yi, flow_source, anomaly, anomaly_sigma, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(code, date) DO UPDATE SET
                 direction=excluded.direction, confidence=excluded.confidence, resonance=excluded.resonance,
                 net_flow_yi=excluded.net_flow_yi, flow_source=excluded.flow_source,
                 anomaly=excluded.anomaly, anomaly_sigma=excluded.anomaly_sigma, payload=excluded.payload""",
            (s.code, s.date, s.direction.value, s.confidence, s.resonance, s.net_flow_yi,
             s.flow_source.value if s.flow_source else None, 1 if s.anomaly else 0, s.anomaly_sigma, payload),
        )
    con.commit()


def combined_core_description(con: sqlite3.Connection, date: str) -> dict:
    """核心层合计份额 5 日变化（quantdata.uk 式描述标签）。"""
    rows = con.execute(
        """SELECT date, SUM(shares*adjust_factor) total FROM etf_daily
           WHERE code IN (%s) AND shares IS NOT NULL AND date<=?
           GROUP BY date ORDER BY date DESC LIMIT 6""" % ",".join("?" * len(CORE_CODES)),
        (*CORE_CODES, date),
    ).fetchall()
    if len(rows) < 6:
        return {"label": "数据不足", "chg5d": None}
    rows = list(reversed(rows))
    chg = (rows[-1]["total"] / rows[0]["total"] - 1.0) * 100.0 if rows[0]["total"] else None
    label = "数据不足"
    if chg is not None:
        if chg >= COMBINED_5D_LARGE:
            label = "大规模申购"
        elif chg >= COMBINED_5D_ELEVATED:
            label = "偏高"
        elif chg <= COMBINED_5D_REDEEM:
            label = "净赎回"
        else:
            label = "正常区间"
    return {"label": label, "chg5d": round(chg, 2) if chg is not None else None,
            "total_yi": round(rows[-1]["total"] / 1e8, 1) if rows[-1]["total"] else None}
