# -*- coding: utf-8 -*-
"""融资余额采集（复用 A-Resonance 验证过的方案：akshare stock_margin_sse 区间接口）。
学 A-Resonance：东财合并源 + SSE 区间源,失败优雅降级返回空。"""
from __future__ import annotations

import sqlite3

from fetch.sources import _retry_call, _warn
from store.db import log_fetch


def _norm_date(d: str) -> str:
    """akshare 日期可能是 YYYYMMDD 或 YYYY-MM-DD,统一为 YYYY-MM-DD。"""
    d = str(d)[:10]
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


def fetch_margin_sse(start: str, end: str) -> list[dict]:
    """上交所融资余额区间。start/end: 'YYYYMMDD'。返回 [{date, fin_balance_yi, loan_balance_yi}]。"""
    df = _retry_call(
        lambda s, e: __import__("akshare").stock_margin_sse(start_date=s, end_date=e), start, end
    )
    if df is None or df.empty or "融资余额" not in df.columns:
        if df is not None and df.empty:
            _warn(f"fetch_margin_sse({start}-{end}) 返回空 DataFrame")
        return []
    rows = []
    for _, row in df.iterrows():
        date = _norm_date(row["信用交易日期"])
        try:
            fin = float(row.get("融资余额"))
        except (TypeError, ValueError):
            continue
        # akshare stock_margin_sse 融资余额单位为「元」,统一换算为「亿」入库
        if fin > 1e9:  # >10亿说明原始单位是元（万亿级数据 1e12）
            fin /= 1e8
        try:
            loan = float(row.get("融券余量金额"))
        except (TypeError, ValueError):
            loan = None
        else:
            if loan and loan > 1e9:
                loan /= 1e8
        rows.append({"date": date, "fin_balance_yi": fin, "loan_balance_yi": loan, "source": "sse"})
    return sorted(rows, key=lambda r: r["date"])


def fetch_margin_szse(date_q: str) -> list[dict]:
    """深交所某月融资余额（akshare stock_margin_szse 按月查询）。

    2026-11-17 实测接口返回空（失效或已变更），已从 backfill_margin 移除调用。
    保留函数供接口恢复后重新接入：恢复时在 backfill_margin 尾部补逐月循环即可。
    """
    try:
        import akshare as ak
        df = ak.stock_margin_szse(date=date_q)
        if df is None or df.empty:
            return []
        rows = []
        for _, row in df.iterrows():
            date = _norm_date(str(row.get("日期") or row.get("信用交易日期") or ""))
            if not date:
                continue
            try:
                fin = float(row.get("融资余额"))
            except (TypeError, ValueError):
                continue
            if fin > 1e9:  # 元 → 亿
                fin /= 1e8
            rows.append({"date": date, "fin_balance_yi": fin, "loan_balance_yi": None, "source": "szse"})
        return rows
    except Exception:
        return []


def upsert_margin(con: sqlite3.Connection, rows: list[dict]) -> int:
    n = 0
    for r in rows:
        con.execute(
            """INSERT INTO margin_daily (date, fin_balance_yi, loan_balance_yi, source)
               VALUES (?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                 fin_balance_yi=excluded.fin_balance_yi,
                 loan_balance_yi=excluded.loan_balance_yi,
                 source=excluded.source""",
            (r["date"], r["fin_balance_yi"], r["loan_balance_yi"], r["source"]),
        )
        n += 1
    con.commit()
    return n


def backfill_margin(con: sqlite3.Connection, months: int = 36) -> int:
    """回填近 N 个月融资余额（沪市区间接口;深市接口 2026-11 实测失效,单边口径）。

    沪市融资余额占两市大头（~55%），其 5 日变化率与两市口径高度同步，
    L5 灯与图 4 以「沪市」单边口径运行——幅度近似可用,绝对值有偏。
    """
    from datetime import datetime, timedelta
    end_d = datetime.now()
    start_d = end_d - timedelta(days=30 * months)
    total = 0
    rows = fetch_margin_sse(start_d.strftime("%Y%m%d"), end_d.strftime("%Y%m%d"))
    if rows:
        total += upsert_margin(con, rows)
    log_fetch(con, "akshare", "backfill_margin_sse_only", total > 0, total)
    return total
