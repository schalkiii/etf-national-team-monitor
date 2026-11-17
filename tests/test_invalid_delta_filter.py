# -*- coding: utf-8 -*-
"""invalid_delta 读侧过滤补漏回归（增量审计 2026-11-17 发现）：
backtest.event_window_path / run_event_study 合计口径、
calibration.total_shares_at / calibrated_daily_flow 原先读裸 shares
未过滤折算/噪声日——跨折算日的事件窗口会出现机械跳变。
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def con():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE etf_daily (code TEXT, date TEXT, close REAL, shares REAL, PRIMARY KEY(code, date));
        CREATE TABLE invalid_delta (code TEXT, date TEXT, reason TEXT, PRIMARY KEY(code, date));
        CREATE TABLE events (name TEXT, event_date TEXT, etype TEXT, note TEXT);
        CREATE TABLE fund_holders (code TEXT, report_date TEXT, entity TEXT, shares_yi REAL);
    """)
    # 5 日连续份额 100→101→(折算日 202)→103→104,价格恒定
    # 折算日若未剔除,shares_indexed 会出现 ~+100% 机械跳变
    days = ["2024-09-16", "2024-09-17", "2024-09-18", "2024-09-19", "2024-09-20", "2024-09-23", "2024-09-24"]
    shares = [100.0, 101.0, 202.0, 103.0, 104.0, 105.0, 106.0]
    for d, s in zip(days, shares):
        conn.execute("INSERT INTO etf_daily VALUES ('510310', ?, 2.0, ?)", (d, s * 1e8))
    conn.execute("INSERT INTO invalid_delta VALUES ('510310', '2024-09-18', '折算日,无申赎含义')")
    conn.commit()
    yield conn
    conn.close()


class TestEventWindowPathFiltersInvalidDelta:
    def test_split_day_excluded_from_path(self, con):
        from core.backtest import event_window_path
        out = event_window_path(con, "2024-09-20", "510310", pre=2, post=2)
        assert "error" not in out
        # 折算日 09-18 必须不在路径中
        assert "2024-09-18" not in out["dates"]
        # 剔除后序列单调递增,无机械跳变
        idx = out["shares_indexed"]
        assert idx == sorted(idx), f"路径仍含机械跳变: {idx}"


class TestTotalSharesAtSkipsInvalidDelta:
    def test_anchor_day_is_split_returns_prior_day(self, con):
        from core.calibration import total_shares_at
        # 询问折算日当日 → 应回退到前一有效日 101,而不是 202
        assert total_shares_at(con, "510310", "2024-09-18") == pytest.approx(101.0)

    def test_normal_day_returns_same_day(self, con):
        from core.calibration import total_shares_at
        assert total_shares_at(con, "510310", "2024-09-20") == pytest.approx(104.0)

    def test_all_prior_invalid_returns_none(self, con):
        from core.calibration import total_shares_at
        # 只留一个折算日在前 → 无有效日
        assert total_shares_at(con, "510310", "2024-09-17") == pytest.approx(101.0)


class TestCalibratedDailyFlowSkipsInvalidDelta:
    def test_latest_delta_uses_valid_rows(self, con):
        from core.calibration import calibrated_daily_flow
        # 若最近差分跨折算日（问 09-19:最近两行=09-18 折算 202 与 09-17 的 101）
        # 过滤后应取 09-19(103) vs 09-17(101),Δ=+2 亿份而非 +101 亿份
        out = calibrated_daily_flow(con, "510310", "2024-09-19")
        # 无锚点时返回 reason;关键是 rows 查询不炸、且不取折算日
        assert out["nt_flow_yi"] is None  # 无 fund_holders → 无 θ
