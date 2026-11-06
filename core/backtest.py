# -*- coding: utf-8 -*-
"""回测与事件研究。

回测（学 etf-tracker 9288 信号纪律）：signals 表全历史 → T+1/T+5/T+20 forward return 检验。
事件研究（核心增量 #2）：事件登记 + 窗口路径分析。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from core.config import ETF_POOL


def _fwd_ret(con, code: str, date: str, n: int) -> float | None:
    rows = con.execute(
        "SELECT date, close FROM etf_daily WHERE code=? AND close IS NOT NULL AND date>=? ORDER BY date LIMIT ?",
        (code, date, n + 1),
    ).fetchall()
    if len(rows) < n + 1:
        return None
    return (rows[n]["close"] / rows[0]["close"] - 1.0) * 100.0


def run_backtest(con: sqlite3.Connection) -> dict:
    """全历史信号 forward-return 检验。"""
    sigs = con.execute(
        "SELECT code, date, direction, confidence, resonance, net_flow_yi FROM signals ORDER BY date"
    ).fetchall()
    stats: dict[str, dict] = {}
    for s in sigs:
        for key in (s["direction"], f"{s['direction']}|{s['confidence']}", s["resonance"]):
            bucket = stats.setdefault(key, {"n": 0, "ret1": [], "ret5": [], "ret20": []})
            bucket["n"] += 1
            r1 = _fwd_ret(con, s["code"], s["date"], 1)
            r5 = _fwd_ret(con, s["code"], s["date"], 5)
            r20 = _fwd_ret(con, s["code"], s["date"], 20)
            if r1 is not None: bucket["ret1"].append(r1)
            if r5 is not None: bucket["ret5"].append(r5)
            if r20 is not None: bucket["ret20"].append(r20)

    def _agg(vals):
        if not vals:
            return None
        return {"mean": round(sum(vals) / len(vals), 3), "n": len(vals),
                "win": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1)}

    out = {"generated_at": datetime.now().isoformat(), "total_signals": len(sigs), "buckets": {}}
    for k, v in stats.items():
        out["buckets"][k] = {"n": v["n"], "ret1": _agg(v["ret1"]), "ret5": _agg(v["ret5"]), "ret20": _agg(v["ret20"])}
    return out


def register_event(con: sqlite3.Connection, name: str, event_date: str, etype: str, note: str = "") -> None:
    con.execute("INSERT INTO events (name, event_date, etype, note) VALUES (?,?,?,?)",
                (name, event_date, etype, note))
    con.commit()


def seed_known_events(con: sqlite3.Connection) -> int:
    """内置已知事件登记（2026-07 官护盘周期,来源公开报道）。"""
    known = [
        ("汇金/国新/诚通官宣增持ETF", "2026-07-20", "official", "中国国新+中国诚通官宣超600亿增持央国企及ETF(新华社2026-07-20)"),
        ("百股跌停·3650保卫战", "2026-07-13", "protective", "7.13-7.17市场急跌,核心宽基份额+208.6亿份(交易所口径)"),
        ("科创杀跌日", "2026-07-16", "protective", "科技股放量杀跌,588000 单日+25.3亿份"),
    ]
    for name, d, t, note in known:
        exists = con.execute("SELECT COUNT(*) FROM events WHERE name=? AND event_date=?", (name, d)).fetchone()[0]
        if not exists:
            register_event(con, name, d, t, note)
    return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def event_window_path(con: sqlite3.Connection, event_date: str, code: str, pre: int = 5, post: int = 20) -> dict:
    """事件窗口 [pre, post] 的份额路径与价格路径。"""
    rows = con.execute(
        """SELECT date, shares*adjust_factor AS shares_adj, close FROM etf_daily
           WHERE code=? AND shares IS NOT NULL AND date>=date(?, '-30 day') AND date<=date(?, '+40 day')
           ORDER BY date""",
        (code, event_date, event_date),
    ).fetchall()
    dates = [r["date"] for r in rows]
    if event_date not in dates:
        return {"code": code, "error": "事件日无份额数据"}
    i0 = dates.index(event_date)
    lo, hi = max(0, i0 - pre), min(len(rows), i0 + post + 1)
    window = rows[lo:hi]
    base = window[0]["shares_adj"]
    return {
        "code": code,
        "dates": [r["date"] for r in window],
        "shares_indexed": [round(r["shares_adj"] / base * 100, 2) for r in window],
        "close": [r["close"] for r in window],
        "event_date": event_date,
    }


def run_event_study(con: sqlite3.Connection) -> dict:
    n = seed_known_events(con)
    events = con.execute("SELECT name, event_date, etype, note FROM events ORDER BY event_date").fetchall()
    out = {"events_total": n, "studies": []}
    from core.config import CORE_CODES
    for ev in events:
        study = {"name": ev["name"], "date": ev["event_date"], "etype": ev["etype"], "paths": {}}
        for code in ["510300", "510050", "512100", "588000"]:
            study["paths"][code] = event_window_path(con, ev["event_date"], code)
        # 事件后5日核心层合计份额变化
        rows = con.execute(
            """SELECT date, SUM(shares*adjust_factor) t FROM etf_daily
               WHERE code IN (%s) AND shares IS NOT NULL AND date BETWEEN date(?, '-7 day') AND date(?, '+7 day')
               GROUP BY date ORDER BY date""" % ",".join("?" * len(CORE_CODES)),
            (*CORE_CODES, ev["event_date"], ev["event_date"]),
        ).fetchall()
        if rows:
            base = rows[0]["t"]
            study["core_combined_chg"] = {r["date"]: round((r["t"] / base - 1) * 100, 2) for r in rows}
        out["studies"].append(study)
    return out
