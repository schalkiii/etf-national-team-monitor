# -*- coding: utf-8 -*-
"""折算判定与披露噪声测试（R5）：守护 splits.py 的「实证结论」。

方法论对应（见 core/splits.py 模块注释）：
- split   = 资产守恒命中折算比指纹 + 复权价连续
- noise   = 次日反向大幅回归
- real    = 熔断日真申购（wealth 非干净比例,绝不能误杀）
"""
import pytest

from core.splits import (
    classify_jump, scan_anomalies, repair_existing,
    CANDIDATE_JUMP_PCT, SPLIT_RATIOS, NEXT_DAY_REVERSAL_PCT,
)


class TestClassifyJump:
    """classify_jump 三分类：纯函数行为契约。"""

    def test_clean_2to1_split_detected(self):
        """干净 2:1 折算：份额翻倍 + 价格减半（复权价连续）→ split。"""
        # shares_chg=+1.0, price_chg=-0.5 → wealth=(2)(0.5)=1.0 ≠ 2 …
        # 注意: 折算比指纹里 2.0 对应「份额翻倍+复权价不变」的真实折算形态
        # （价格用 qfq 复权后连续）。此处 shares +100%, 复权价 -1% → wealth≈1.98≈2.0
        assert classify_jump(1.0, -0.01, None) == "split"

    def test_half_split_detected(self):
        """0.5 折算（合并份额）：份额 -50% + 复权价连续 → split。"""
        assert classify_jump(-0.5, 0.01, None) == "split"

    def test_circuit_breaker_real_buy_not_killed(self):
        """512100 2024-02-05 熔断日真申购：+132.8% 份额、复权价 -6.3%、wealth≈2.18
        非干净比例 → real（绝不能误杀,这是阈值法的踩坑教训）。"""
        assert classify_jump(1.328, -0.063, None) == "real"

    def test_next_day_reversal_noise(self):
        """披露噪声：今日 +25%,明日 -20% 反向回归 → noise。"""
        assert classify_jump(0.25, 0.0, -0.20) == "noise"

    def test_real_flow_no_reversal(self):
        """真实申赎：大幅变化但次日同向延续 → real。"""
        assert classify_jump(0.25, 0.0, 0.05) == "real"

    def test_small_reversal_is_real(self):
        """次日反向但幅度 <10% 阈值 → real（正常波动不算噪声）。"""
        assert classify_jump(0.25, 0.0, -0.08) == "real"

    def test_reversal_beats_ratio_if_price_broken(self):
        """命中折算比但价格不连续（>3%）→ 落到 noise/real 判定,不得判 split。"""
        # wealth≈2.0 但复权价 -8%（不连续）→ 不可能是折算
        assert classify_jump(1.0, -0.08, None) == "real"


class TestScanAnomalies:
    """scan_anomalies 全库扫描：SQL 窗口函数 + 分类集成。"""

    @pytest.fixture()
    def db(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE etf_daily (
            code TEXT, date TEXT, close REAL, shares REAL,
            PRIMARY KEY (code, date))""")
        # 510300: 正常 → 2:1 折算日 → 折算后正常
        rows = [
            ("510300", "2026-01-01", 4.0, 100.0e8),
            ("510300", "2026-01-02", 4.0, 100.5e8),
            ("510300", "2026-01-05", 2.0, 201.0e8),   # 折算: 份额翻倍, 价格减半(qfq 连续视角: pchg=-50%)
            ("510300", "2026-01-06", 2.0, 201.5e8),
        ]
        conn.executemany("INSERT INTO etf_daily VALUES (?,?,?,?)", rows)
        yield conn
        conn.close()

    def test_scan_finds_jump_candidate(self, db):
        out = scan_anomalies(db)
        # 注意: 本例折算日价格直接减半(未复权原始序列),wealth=(2)(0.5)=1.0 不命中
        # 折算比指纹,但会作为候选进入输出(量级 >15%)
        assert len(out) == 1
        assert out[0]["code"] == "510300"
        assert out[0]["date"] == "2026-01-05"
        assert out[0]["shares_chg_pct"] == pytest.approx(100.0, abs=0.1)

    def test_quiet_series_no_candidates(self, db):
        # 份额平稳 → 无候选
        conn = db
        conn.execute("DELETE FROM etf_daily WHERE code='510300'")
        dates = ["2026-01-01", "2026-01-02", "2026-01-05"]
        for i, d in enumerate(dates):
            conn.execute("INSERT INTO etf_daily VALUES (?,?,?,?)",
                         ("510300", d, 4.0, 100.0e8 + i * 0.1e8))
        assert scan_anomalies(conn) == []


class TestRepairExisting:
    """repair_existing 存量修正：dry_run 只统计,实跑写 invalid_delta。"""

    @pytest.fixture()
    def db(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE etf_daily (
            code TEXT, date TEXT, close REAL, shares REAL,
            PRIMARY KEY (code, date))""")
        conn.execute("""CREATE TABLE fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
            source TEXT, task TEXT, ok INTEGER, rows INTEGER, detail TEXT)""")
        # 次日反向回归噪声序列: +30% 然后 -25%（反向 >10%）
        rows = [
            ("512100", "2026-02-02", 3.0, 100.0e8),
            ("512100", "2026-02-03", 3.0, 130.0e8),   # +30%
            ("512100", "2026-02-04", 3.0, 97.5e8),    # -25% 反向
            ("512100", "2026-02-05", 3.0, 97.6e8),
        ]
        conn.executemany("INSERT INTO etf_daily VALUES (?,?,?,?)", rows)
        yield conn
        conn.close()

    def test_dry_run_counts_only(self, db):
        stats = repair_existing(db, dry_run=True)
        assert stats["noise_days"] >= 1
        # dry_run 不建表
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "invalid_delta" not in tables

    def test_repair_marks_invalid_delta(self, db):
        stats = repair_existing(db, dry_run=False)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "invalid_delta" in tables
        n = db.execute("SELECT COUNT(*) FROM invalid_delta").fetchone()[0]
        assert n == stats["invalid_rows"]
        # 噪声日 2026-02-03 必须被标记
        marked = db.execute(
            "SELECT date FROM invalid_delta WHERE code='512100'").fetchall()
        assert any(r["date"] == "2026-02-03" for r in marked)

    def test_manual_dead_zone_marked(self, db):
        """MANUAL_DEAD_ZONES 区间全部标记（512100 2022-07-22~09-02）。"""
        import sqlite3
        db.execute("DELETE FROM etf_daily")
        for d in ["2022-07-21", "2022-07-22", "2022-08-01", "2022-09-01", "2022-09-02", "2022-09-05"]:
            db.execute("INSERT INTO etf_daily VALUES (?,?,?,?)",
                       ("512100", d, 3.0, 100.0e8))
        repair_existing(db, dry_run=False)
        in_zone = db.execute(
            "SELECT COUNT(*) FROM invalid_delta WHERE code='512100' AND date BETWEEN '2022-07-22' AND '2022-09-02'"
        ).fetchone()[0]
        assert in_zone == 4, "死区 4 个交易日应全部标记 invalid"
        out_zone = db.execute(
            "SELECT COUNT(*) FROM invalid_delta WHERE code='512100' AND date NOT BETWEEN '2022-07-22' AND '2022-09-02'"
        ).fetchone()[0]
        assert out_zone == 0, "死区外日期不应被死区规则误标"

    def test_original_shares_never_touched(self, db):
        """原始披露值永不修改（只写 invalid_delta,不动 etf_daily.shares）。"""
        before = db.execute("SELECT code, date, shares FROM etf_daily ORDER BY date").fetchall()
        repair_existing(db, dry_run=False)
        after = db.execute("SELECT code, date, shares FROM etf_daily ORDER BY date").fetchall()
        assert [(r["code"], r["date"], r["shares"]) for r in before] == \
               [(r["code"], r["date"], r["shares"]) for r in after]
