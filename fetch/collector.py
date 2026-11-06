# -*- coding: utf-8 -*-
"""采集与回填：K 线 / 份额 / 复权因子 / 一键重建。
份额复权（学 A-Resonance 159740 踩坑）：拆分造成份额跳变必须探测,
否则拆分会被误判为巨额申购。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from core.config import ETF_POOL, INDEX_CODES, SHARE_LOOKBACK_DAYS, KLINE_LOOKBACK_DAYS
from fetch.calendar import daterange, rebuild_calendar, trading_days
from fetch.sources import sse_shares, szse_shares, tencent_kline
from store.db import log_fetch

# 拆分探测阈值：单日份额变化超过 25% 且与价格跳变同向 → 视为拆分/折算
SPLIT_DETECT_RATIO = 0.25


def backfill_klines(con: sqlite3.Connection, start: str = "2020-01-01") -> int:
    """全部 ETF + 指数 K 线回填（区间分页全量,2020 起）。返回成功标的数。"""
    from datetime import datetime
    from fetch.sources import tencent_kline_full
    end = datetime.now().strftime("%Y-%m-%d")
    ok = 0
    symbols = [(code, ("sh" if v["market"] == "sh" else "sz") + code) for code, v in ETF_POOL.items()]
    symbols += [(idx, idx) for idx in INDEX_CODES]
    for key, sym in symbols:
        rows = tencent_kline_full(sym, start, end)
        if not rows:
            continue
        is_index = sym in INDEX_CODES
        table = "index_daily" if is_index else "etf_daily"
        for (d, o, c, h, low, vol, amt) in rows:
            if is_index:
                con.execute(
                    "INSERT OR REPLACE INTO index_daily (index_code, date, close, amount) VALUES (?,?,?,?)",
                    (key, d, c, amt),
                )
            else:
                con.execute(
                    """INSERT INTO etf_daily (code, date, open, close, high, low, volume, amount)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(code, date) DO UPDATE SET
                         open=excluded.open, close=excluded.close, high=excluded.high,
                         low=excluded.low, volume=excluded.volume, amount=excluded.amount""",
                    (key, d, o, c, h, low, vol, amt),
                )
        con.commit()
        ok += 1
    log_fetch(con, "tencent", "backfill_klines", ok > 0, ok)
    return ok


def backfill_shares(con: sqlite3.Connection, lookback_days: int = SHARE_LOOKBACK_DAYS) -> dict:
    """份额回填：沪市逐日 / 深市区间。返回统计。"""
    stats = {"sse_days": 0, "szse_days": 0, "rows": 0}
    # 定位回填窗口：以日历中最近交易日为锚
    last = con.execute("SELECT date FROM calendar WHERE is_trading=1 ORDER BY date DESC LIMIT 1").fetchone()
    if not last:
        return stats
    end_d = datetime.strptime(last["date"], "%Y-%m-%d").date()
    start_d = end_d - timedelta(days=int(lookback_days * 1.6))  # 自然日冗余
    tdays = trading_days(con, start_d.isoformat(), end_d.isoformat())
    if not tdays:
        return stats

    # --- 深市：区间接口一次拉全 ---
    # 深交所接口区间上限约 185 自然日（实测 20260505 起可查,20260504 即空）,
    # 超限分段抓取。
    from datetime import date as _dt
    seg_start = start_d
    while seg_start <= end_d:
        seg_end = min(seg_start + timedelta(days=180), end_d)
        seg = szse_shares(seg_start.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"))
        for code, per in seg.items():
            if code not in ETF_POOL:
                continue
            for d, shares in per.items():
                con.execute(
                    """INSERT INTO etf_daily (code, date, shares) VALUES (?,?,?)
                       ON CONFLICT(code, date) DO UPDATE SET shares=excluded.shares""",
                    (code, d, shares),
                )
                stats["rows"] += 1
        sz_dates_seg = sorted({d for per in seg.values() for d in per})
        stats["szse_days"] = max(stats["szse_days"], len(sz_dates_seg))
        if not sz_dates_seg:
            pass  # 该段无数据(未来日期/全失败),继续下一段
        seg_start = seg_end + timedelta(days=1)

    # --- 沪市：逐日接口（限流温和,逐日循环） ---
    for d in tdays:
        per = sse_shares(d.replace("-", ""))
        if not per:
            continue
        stats["sse_days"] += 1
        for code in ETF_POOL:
            if ETF_POOL[code]["market"] != "sh":
                continue
            if code in per:
                con.execute(
                    """INSERT INTO etf_daily (code, date, shares) VALUES (?,?,?)
                       ON CONFLICT(code, date) DO UPDATE SET shares=excluded.shares""",
                    (code, d, per[code]),
                )
                stats["rows"] += 1
    con.commit()
    log_fetch(con, "akshare", "backfill_shares", stats["rows"] > 0,
              stats["rows"], f"sse_days={stats['sse_days']} szse_days={stats['szse_days']}")
    return stats


def detect_splits_and_adjust(con: sqlite3.Connection, code: str) -> list[dict]:
    """拆分探测与复权：份额单日跳变 >25% 且价格反向跳变 → 拆分。
    写入 adjust_factor,使复权份额序列连续。返回事件列表。"""
    rows = con.execute(
        "SELECT date, close, shares FROM etf_daily WHERE code=? AND shares IS NOT NULL ORDER BY date",
        (code,),
    ).fetchall()
    events = []
    if len(rows) < 2:
        return events
    cum_factor = 1.0
    prev = rows[0]
    factors = {rows[0]["date"]: 1.0}
    for cur in rows[1:]:
        ratio = cur["shares"] / prev["shares"] if prev["shares"] else 1.0
        price_ratio = (prev["close"] / cur["close"]) if (prev["close"] and cur["close"]) else 1.0
        # 份额跳升>25% 且价格近似同比例下跌 → 拆分
        if abs(ratio - 1.0) > SPLIT_DETECT_RATIO and 0.7 < (ratio * price_ratio) < 1.4:
            cum_factor *= ratio
            events.append({"code": code, "date": cur["date"], "ratio": round(ratio, 4)})
        factors[cur["date"]] = cum_factor
        prev = cur
    for d, f in factors.items():
        con.execute("UPDATE etf_daily SET adjust_factor=? WHERE code=? AND date=?", (f, code, d))
    con.commit()
    return events


def rebuild_all(con: sqlite3.Connection) -> dict:
    """一键重建（学 A-Resonance 承重顺序）：日历 → K线 → 份额 → 复权 → 融资余额。"""
    n_cal = rebuild_calendar(con, lookback_days=2600)
    n_kline = backfill_klines(con)
    share_stats = backfill_shares(con)
    splits = []
    for code in ETF_POOL:
        splits.extend(detect_splits_and_adjust(con, code))
    from fetch.margin import backfill_margin
    n_margin = backfill_margin(con, months=48)
    return {"calendar": n_cal, "klines": n_kline, "shares": share_stats, "splits": splits, "margin_rows": n_margin}


def daily_incremental(con: sqlite3.Connection, days: int = 5) -> dict:
    """每日增量：近 days 自然日的 K 线增量 + 当日沪市份额 + 深市区间 + 融资余额增量。"""
    end_d = datetime.now().date()
    start_d = end_d - timedelta(days=days + 3)
    # K 线增量（腾讯接口直接覆盖式写入）
    n = backfill_klines(con)
    # 份额增量
    tdays = trading_days(con, start_d.isoformat(), end_d.isoformat())
    rows = 0
    sz_all = szse_shares(start_d.strftime("%Y%m%d"), end_d.strftime("%Y%m%d"))
    for code, per in sz_all.items():
        if code in ETF_POOL:
            for d, shares in per.items():
                con.execute(
                    """INSERT INTO etf_daily (code, date, shares) VALUES (?,?,?)
                       ON CONFLICT(code, date) DO UPDATE SET shares=excluded.shares""",
                    (code, d, shares),
                )
                rows += 1
    for d in tdays[-days:]:
        per = sse_shares(d.replace("-", ""))
        if not per:
            continue
        for code in ETF_POOL:
            if ETF_POOL[code]["market"] == "sh" and code in per:
                con.execute(
                    """INSERT INTO etf_daily (code, date, shares) VALUES (?,?,?)
                       ON CONFLICT(code, date) DO UPDATE SET shares=excluded.shares""",
                    (code, d, per[code]),
                )
                rows += 1
    con.commit()
    # 融资余额增量（近10天）
    from fetch.margin import backfill_margin
    n_margin = backfill_margin(con, months=1)
    log_fetch(con, "akshare", "daily_incremental", rows > 0, rows)
    for code in ETF_POOL:
        detect_splits_and_adjust(con, code)
    return {"klines": n, "share_rows": rows, "margin_rows": n_margin}
