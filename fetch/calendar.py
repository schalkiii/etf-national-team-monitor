# -*- coding: utf-8 -*-
"""交易日历：由上证指数日线推导（有 K 线即交易日）,不依赖额外接口。
学 A-Resonance：一键重建的第一承重柱。"""
from __future__ import annotations

import sqlite3
from datetime import date as _date, datetime, timedelta

from fetch.sources import tencent_kline, tencent_kline_full


def rebuild_calendar(con: sqlite3.Connection, lookback_days: int = 900) -> int:
    """用上证指数日线重建日历。
    优先从库内 index_daily 恢复（腾讯接口临时限流时不阻塞）；库为空或覆盖不足时才请求腾讯。"""
    from datetime import datetime, timedelta
    want_start = (datetime.now() - timedelta(days=lookback_days * 1.5)).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT DISTINCT date FROM index_daily WHERE index_code='sh000001' ORDER BY date"
    ).fetchall()
    dates = [r["date"] for r in rows if r["date"] >= want_start]
    if not dates:
        # 库内无数据,回退请求腾讯
        if lookback_days > 600:
            end = datetime.now().strftime("%Y-%m-%d")
            start = want_start
            klines = tencent_kline_full("sh000001", start, end)
        else:
            klines = tencent_kline("sh000001", lookback_days)
        if not klines:
            return 0
        dates = [k[0] for k in klines]
    con.execute("DELETE FROM calendar")
    for d in dates:
        con.execute("INSERT OR REPLACE INTO calendar VALUES (?, 1)", (d,))
    con.commit()
    return len(dates)


def trading_days(con: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = con.execute(
        "SELECT date FROM calendar WHERE is_trading=1 AND date>=? AND date<=? ORDER BY date",
        (start, end),
    ).fetchall()
    return [r["date"] for r in rows]


def last_n_trading(con: sqlite3.Connection, end: str, n: int) -> list[str]:
    rows = con.execute(
        "SELECT date FROM calendar WHERE is_trading=1 AND date<=? ORDER BY date DESC LIMIT ?",
        (end, n),
    ).fetchall()
    return sorted(r["date"] for r in rows)


def latest_trading_day(con: sqlite3.Connection) -> str | None:
    row = con.execute("SELECT date FROM calendar WHERE is_trading=1 ORDER BY date DESC LIMIT 1").fetchone()
    return row["date"] if row else None


def daterange(start: str, end: str):
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    while d0 <= d1:
        yield d0.isoformat()
        d0 += timedelta(days=1)
