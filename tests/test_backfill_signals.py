# -*- coding: utf-8 -*-
"""历史信号回填测试：幂等性、无未来数据泄漏、失败隔离。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.analysis import backfill_signals  # noqa: E402
from core.config import ETF_POOL  # noqa: E402

N_CODES = len(ETF_POOL)


def _rows(con):
    return con.execute(
        "SELECT code, date, direction FROM signals ORDER BY date, code"
    ).fetchall()


def test_backfill_populates_and_is_idempotent(con):
    """回填写入 N 日 × 标的数条；重复执行行数不变（upsert 幂等）。"""
    conn, _ = con
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM etf_daily ORDER BY date LIMIT 6"
    )]
    stats = backfill_signals(conn, dates, progress_every=0)
    assert stats["days"] == len(dates)
    # compute_signals_for_date 对全标的池逐只产出信号（缺数据者为 BLOCKED）
    assert stats["signals"] == len(dates) * N_CODES
    assert stats["failed"] == []
    first = _rows(conn)
    assert len(first) == len(dates) * N_CODES

    again = backfill_signals(conn, dates, progress_every=0)
    assert again["days"] == len(dates)
    assert _rows(conn) == first  # 无重复、无变化


def test_backfill_is_point_in_time(con):
    """回填早日期时只用到该日及之前的数据：同日结果与单独计算一致。"""
    from core.analysis import compute_signals_for_date

    conn, _ = con
    dates = sorted(r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM etf_daily ORDER BY date LIMIT 5"
    ))
    target = dates[2]
    expected = [
        (s.code, s.direction.value)
        for s in compute_signals_for_date(conn, target)
    ]
    backfill_signals(conn, [target], progress_every=0)
    actual = [
        (r["code"], r["direction"])
        for r in conn.execute("SELECT code, direction FROM signals WHERE date=?", (target,))
    ]
    assert sorted(actual) == sorted(expected)


def test_backfill_isolates_bad_date(con, monkeypatch):
    """单日异常不中断整体；其余日期仍写入。"""
    import core.analysis as analysis

    conn, _ = con
    dates = sorted(r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM etf_daily ORDER BY date LIMIT 4"
    ))
    bad = dates[1]
    original = analysis.compute_signals_for_date

    def raising(c, d):
        if d == bad:
            raise RuntimeError("injected failure")
        return original(c, d)

    monkeypatch.setattr(analysis, "compute_signals_for_date", raising)
    stats = backfill_signals(conn, dates[:3], progress_every=0)
    assert stats["days"] == 2
    assert len(stats["failed"]) == 1
    assert bad in stats["failed"][0]
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2 * N_CODES
