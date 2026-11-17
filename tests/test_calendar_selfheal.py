# -*- coding: utf-8 -*-
"""daily_incremental 日历自愈回归（2026-11-17 踩坑）：
calendar 停在旧日期时，daily_incremental 必须先自愈日历再采沪市份额，
否则 trading_days 返回空 → 沪市份额静默停采。"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

from fetch.collector import daily_incremental


def _mk_con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    return con


class TestDailyIncrementalCalendarSelfHeal:
    def test_stale_calendar_self_heals(self, tmp_path):
        """calendar 停在 2026-11-06、当日为 11-17：日历必须在份额采集前刷新。"""
        import sys
        sys.path.insert(0, "tests")
        from store.db import init_db
        import conftest

        con = _mk_con()
        init_db(con)
        conftest.seed(con)  # 种子日历/指数/份额数据

        # conftest 不种 calendar: 先种入 seed 同窗的日历行,再砍到旧日期模拟停跑
        days = [r["date"] for r in con.execute(
            "SELECT DISTINCT date FROM etf_daily ORDER BY date")]
        for d in days:
            con.execute("INSERT OR REPLACE INTO calendar (date, is_trading) VALUES (?,1)", (d,))
        con.execute("DELETE FROM calendar WHERE date > '2026-06-10'")
        con.commit()
        assert con.execute("SELECT MAX(date) FROM calendar").fetchone()[0] <= "2026-06-10"

        rebuild_calls = []
        with patch("fetch.calendar.rebuild_calendar", side_effect=lambda c, lookback_days=120: rebuild_calls.append(lookback_days) or 0), \
             patch("fetch.collector.backfill_klines", return_value=0), \
             patch("fetch.collector.sse_shares", return_value={}), \
             patch("fetch.collector.szse_shares", return_value={}), \
             patch("fetch.margin.backfill_margin", return_value=0), \
             patch("core.splits.repair_existing", return_value={}):
            daily_incremental(con, days=5)

        assert rebuild_calls, "daily_incremental 必须调用 rebuild_calendar 自愈日历"
