# -*- coding: utf-8 -*-
"""采集与回填：K 线 / 份额 / 一键重建。

份额折算/披露噪声识别（单一权威 core/splits.py）：rebuild 与 daily 均调用
repair_existing 幂等重建 share_splits/invalid_delta；读侧（analysis/report/
calibration）用裸 shares + invalid_delta 过滤。adjust_factor 复权因子法已废弃
（qfq 价格序列上「价格反向验证」永不成立，曾把真实巨额申购误判为拆分，
见 AUDIT-REPORT A1/A2）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from core.config import ETF_POOL, INDEX_CODES, SHARE_LOOKBACK_DAYS, KLINE_LOOKBACK_DAYS
from fetch.calendar import daterange, rebuild_calendar, trading_days
from fetch.sources import sse_shares, szse_shares, tencent_kline
from store.db import log_fetch


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


def rebuild_all(con: sqlite3.Connection) -> dict:
    """一键重建（学 A-Resonance 承重顺序）：日历 → K线 → 份额 → 折算清洗 → 融资余额 → 锚点。"""
    n_cal = rebuild_calendar(con, lookback_days=2600)
    n_kline = backfill_klines(con)
    share_stats = backfill_shares(con)
    # 折算/噪声清洗（单一权威 core/splits.py,幂等重建 invalid_delta）
    from core.splits import repair_existing
    repair_stats = repair_existing(con, dry_run=False)
    from fetch.margin import backfill_margin
    n_margin = backfill_margin(con, months=48)
    # 已知锚点幂等 seed（core/anchor_data.py：新锚点录入 ANCHORS 后 rebuild 即生效）
    from core.anchor_data import seed_anchors
    n_anchor = seed_anchors(con)
    return {"calendar": n_cal, "klines": n_kline, "shares": share_stats, "repair": repair_stats,
            "margin_rows": n_margin, "anchors_seeded": n_anchor}


def daily_incremental(con: sqlite3.Connection, days: int = 5) -> dict:
    """每日增量：近 days 自然日的 K 线增量 + 当日沪市份额 + 深市区间 + 融资余额增量。
    日历先自愈（K线拉新后从 index_daily 重建到最新）——日历过期会使 trading_days
    返回空、沪市份额静默停采（2026-11-17 实测踩坑）。"""
    end_d = datetime.now().date()
    start_d = end_d - timedelta(days=days + 3)
    # K 线增量（腾讯接口直接覆盖式写入）
    n = backfill_klines(con)
    # 日历自愈：以库内最新指数日线补齐 calendar 到当下
    from fetch.calendar import rebuild_calendar
    rebuild_calendar(con, lookback_days=120)
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
    # 折算/噪声清洗（幂等,新增披露日的 Δshares 标记随之刷新）
    from core.splits import repair_existing
    repair_stats = repair_existing(con, dry_run=False)
    return {"klines": n, "share_rows": rows, "margin_rows": n_margin, "repair": repair_stats}
