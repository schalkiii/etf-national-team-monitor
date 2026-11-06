# -*- coding: utf-8 -*-
"""融资余额采集（复用 A-Resonance 验证过的方案：akshare stock_margin_sse 区间接口）。
学 A-Resonance：东财合并源 + SSE 区间源,失败优雅降级返回空。"""
from __future__ import annotations

import sqlite3

from store.db import log_fetch


def _norm_date(d: str) -> str:
    """akshare 日期可能是 YYYYMMDD 或 YYYY-MM-DD,统一为 YYYY-MM-DD。"""
    d = str(d)[:10]
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


def fetch_margin_sse(start: str, end: str) -> list[dict]:
    """上交所融资余额区间。start/end: 'YYYYMMDD'。返回 [{date, fin_balance_yi, loan_balance_yi}]。"""
    try:
        import akshare as ak
        df = ak.stock_margin_sse(start_date=start, end_date=end)
        if df is None or df.empty or "融资余额" not in df.columns:
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
    except Exception:
        return []


def fetch_margin_szse(date_q: str) -> list[dict]:
    """深交所某月融资余额（akshare stock_margin_szse 按月查询）。date_q: 'YYYYMM'。"""
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
    """回填近 N 个月融资余额（沪市区间接口为主,深市按月补）。"""
    from datetime import datetime, timedelta
    end_d = datetime.now()
    start_d = end_d - timedelta(days=30 * months)
    total = 0
    # 沪市：区间接口一次拉全
    rows = fetch_margin_sse(start_d.strftime("%Y%m%d"), end_d.strftime("%Y%m%d"))
    if rows:
        total += upsert_margin(con, rows)
    # 深市：逐月
    cur = start_d
    while cur <= end_d:
        rows = fetch_margin_szse(cur.strftime("%Y%m"))
        if rows:
            total += upsert_margin(con, rows)
        # 跳到下个月
        cur = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
    log_fetch(con, "akshare", "backfill_margin", total > 0, total)
    return total
