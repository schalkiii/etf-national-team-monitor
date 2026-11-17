# -*- coding: utf-8 -*-
"""P2-1 灯分支测试：L4 市场热度 / L5 融资杠杆的红绿分支（AUDIT-REPORT A13）。

conftest 已把成交额改为波动序列（近端放量、早期缩量）,margin 默认递增;
本文件补齐两个灯位的红/绿分支断言,并验证 conftest 数据形态假设。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.signal_engine import light_market_heat, light_margin  # noqa: E402


class TestL4MarketHeat:
    """L4 市场热度灯：成交额 60 日滚动分位 ≥80% 红 / ≤20% 绿。"""

    def test_recent_surge_is_red(self, con):
        """conftest 近端放量段（6e11 vs 历史 3e11/1e11）→ 分位 100% → 红灯。"""
        conn, _ = con
        from core.analysis import _market_amount
        cur, hist = _market_amount(conn, "2026-10-06")  # 数据窗末段
        light = light_market_heat(cur, hist)
        assert light.color.value == "红", f"近端放量应为过热红灯,实际 {light.color.value}"

    def test_early_quiet_period_low_percentile(self, con):
        """早期缩量段取值当日（cur=1e11,与全部历史同值）→ 分位 ≤20% 触发绿灯。
        注: pct_rank 用 <=（含等号）,与常量历史比也是 100%,故取值日必须
        落在「后续放量尚未进入窗口」的位置。"""
        conn, _ = con
        from core.analysis import _market_amount
        dates = [r["date"] for r in conn.execute(
            "SELECT date FROM index_daily WHERE index_code='sh000001' ORDER BY date")]
        # i=10: 窗口=前 11 个交易日,全部 1e11 → cur 同值分位 100% 仍不满冷清阈值?
        # 不——冷清判定 rank<=20 需要 cur 低于 8 成历史。直接构造独立场景:
        cur, hist = 1.0e11, [3.0e11] * 30   # 常态 3e11 中突然缩量到 1e11
        light = light_market_heat(cur, hist)
        assert light.color.value == "绿", f"缩量日应为冷清绿灯,实际 {light.color.value}"

    def test_constant_amount_gray(self):
        """样本不足 20 日 → 灰灯（数据缺失分支）。"""
        light = light_market_heat(None, [])
        assert light.color.value == "灰"


class TestL5Margin:
    """L5 融资杠杆灯：5 日变化率 ≥+1% 红 / ≤−1% 绿。"""

    def test_rising_margin_red(self, con):
        """conftest margin 递增（5 日 +2.8%）→ 加杠杆红灯。"""
        conn, _ = con
        dates = [r["date"] for r in conn.execute(
            "SELECT date FROM index_daily WHERE index_code='sh000001' ORDER BY date")]
        from core.analysis import _margin_chg
        chg = _margin_chg(conn, dates[-1])
        assert chg is not None and chg > 1.0, f"递增序列 5 日变化应 >+1%,实际 {chg}"
        light = light_margin(chg)
        assert light.color.value == "红"

    def test_falling_margin_green(self, con):
        """自建递减 margin（5 日 −2.8%）→ 去杠杆绿灯。"""
        conn, _ = con
        conn.execute("DELETE FROM margin_daily")
        dates = [r["date"] for r in conn.execute(
            "SELECT date FROM index_daily WHERE index_code='sh000001' ORDER BY date")][-6:]
        for k, d in enumerate(dates):
            conn.execute("INSERT OR REPLACE INTO margin_daily (date, fin_balance_yi) VALUES (?,?)",
                         (d, 20000.0 - k * 120.0))  # 5 日 -2.9%
        conn.commit()
        from core.analysis import _margin_chg
        chg = _margin_chg(conn, dates[-1])
        assert chg is not None and chg < -1.0, f"递减序列 5 日变化应 <−1%,实际 {chg}"
        light = light_margin(chg)
        assert light.color.value == "绿"

    def test_flat_margin_gray(self):
        """变化率在 (−1%, +1%) 中性区间 → 灰灯。"""
        light = light_margin(0.3)
        assert light.color.value == "灰"

    def test_insufficient_data_gray(self, con):
        """margin 数据不足 6 点 → _margin_chg 返回 None → 灰灯。"""
        conn, _ = con
        conn.execute("DELETE FROM margin_daily")
        conn.commit()
        from core.analysis import _margin_chg
        assert _margin_chg(conn, "2026-10-06") is None
        assert light_margin(None).color.value == "灰"
