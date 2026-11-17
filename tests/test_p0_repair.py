# -*- coding: utf-8 -*-
"""P0-1 口径修复测试：拆分判定收敛到 splits.py 单一权威（AUDIT-REPORT A1/A2）。

守护三条红线：
1. 真实巨额申购日（份额 +28%、价格连续、无次日回归）绝不能被判 split——
   这是 collector 幅度阈值法曾经的方向性错误（510050 2026-11-03 +27.9% 真申购
   被乘上 factor，官方 163.27 亿份 = raw 口径证实）。
2. 真拆分日（份额 ×2、复权价连续）必须判 split 且差分作废。
3. 消费链路（analysis/report/calibration/backtest）读侧零 adjust_factor 引用——
   裸 shares + invalid_delta 过滤是唯一口径。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.splits import classify_jump, repair_existing, scan_anomalies  # noqa: E402


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE etf_daily (
        code TEXT, date TEXT, close REAL, shares REAL,
        PRIMARY KEY (code, date))""")
    conn.execute("""CREATE TABLE fetch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
        source TEXT, task TEXT, ok INTEGER, rows INTEGER, detail TEXT)""")
    return conn


class TestRealSubscriptionNotSplit:
    """真实巨额申购不得误判（A1 方向性 bug 的回归守护）。"""

    def test_510050_20261103_real_subscription(self):
        """复刻生产库实证：+27.9% 份额、复权价 -1.6% 连续、无次日回归 → real。
        wealth=(1.279)(0.984)≈1.259 不命中折算比指纹。"""
        assert classify_jump(0.279, -0.016, 0.01) == "real"

    def test_large_real_subscription_various_sizes(self):
        """+30%/+40%/+50% 真申购（价格连续、无回归）全部 real。"""
        for chg in (0.30, 0.40, 0.50):
            assert classify_jump(chg, 0.01, None) == "real", f"+{chg:.0%} 真申购被误判"

    def test_real_subscription_not_marked_invalid_after_repair(self):
        """端到端：真申购日 repair 后不进 invalid_delta（差分保留,可参与估算）。"""
        conn = _make_db()
        rows = [
            ("510050", "2026-10-30", 3.20, 128.66e8),
            ("510050", "2026-11-03", 3.15, 164.61e8),   # +27.9% 真申购,价格连续
            ("510050", "2026-11-04", 3.14, 165.0e8),
        ]
        conn.executemany("INSERT INTO etf_daily VALUES (?,?,?,?)", rows)
        repair_existing(conn, dry_run=False)
        marked = conn.execute(
            "SELECT COUNT(*) FROM invalid_delta WHERE code='510050'").fetchone()[0]
        assert marked == 0, "真申购日被误标 invalid → A1 方向性 bug 回归"


class TestTrueSplitMarked:
    """真拆分必须识别且差分作废。"""

    def test_clean_split_classified(self):
        """份额 ×2、复权价连续（qfq 已复权）→ split。"""
        assert classify_jump(1.0, 0.005, None) == "split"

    def test_split_marked_invalid_after_repair(self):
        """端到端：真拆分日 repair 后进 invalid_delta + share_splits。"""
        conn = _make_db()
        rows = [
            ("510310", "2024-09-19", 1.20, 1216.8e8),
            ("510310", "2024-09-20", 1.21, 623.5e8),   # 份额 ×0.512 合并,价格连续(qfq)
            ("510310", "2024-09-23", 1.21, 624.0e8),
        ]
        conn.executemany("INSERT INTO etf_daily VALUES (?,?,?,?)", rows)
        repair_existing(conn, dry_run=False)
        in_split = conn.execute(
            "SELECT COUNT(*) FROM share_splits WHERE code='510310' AND date='2024-09-20'"
        ).fetchone()[0]
        in_invalid = conn.execute(
            "SELECT COUNT(*) FROM invalid_delta WHERE code='510310' AND date='2024-09-20'"
        ).fetchone()[0]
        assert in_split == 1, "真拆分未登记 share_splits"
        assert in_invalid == 1, "真拆分日差分未作废"


class TestNoFactorInReadPath:
    """读侧零 adjust_factor 引用（A2 双轨收敛的静态守护）。"""

    READ_MODULES = [
        "core/analysis.py",
        "core/report.py",
        "core/calibration.py",
        "core/backtest.py",
        "fetch/collector.py",
    ]

    def test_read_modules_never_multiply_factor(self):
        """核心读侧模块源码中不得出现 shares*adjust_factor / adjust_factor 乘法。"""
        for rel in self.READ_MODULES:
            src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            assert "shares*adjust_factor" not in src.replace(" ", ""), \
                f"{rel} 仍存在 shares*adjust_factor 乘法（读侧禁用）"
            assert "* adjust_factor" not in src and "adjust_factor *" not in src, \
                f"{rel} 仍存在 adjust_factor 乘法引用"

    def test_collector_split_detection_removed(self):
        """collector 双轨探测函数与阈值已删除。"""
        src = (PROJECT_ROOT / "fetch/collector.py").read_text(encoding="utf-8")
        assert "detect_splits_and_adjust" not in src
        assert "SPLIT_DETECT_RATIO" not in src

    def test_cli_has_repair_command(self):
        """cli.py 提供 repair 子命令（存量库迁移入口）。"""
        src = (PROJECT_ROOT / "cli.py").read_text(encoding="utf-8")
        assert '"repair"' in src
