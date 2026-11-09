# -*- coding: utf-8 -*-
"""回测与存储层测试（P5-2/P5-3）：forward-return 统计 + atomic_write 原子性。

R1 空头封顶（apply_confidence_rules direction 参数）测试同置于此文件的
TestShortCap 类——信号引擎纯函数,无需数据库。
"""
import os

import pytest

from core.signal_engine import apply_confidence_rules
from core.models import Direction, LightColor, LightEvidence


def _lights(gray: int = 0) -> list[LightEvidence]:
    """构造 gray 盏灰灯（灰灯≥4 触发降一级规则）。"""
    return [LightEvidence("灯", LightColor.GRAY, None, "测试") for _ in range(gray)]


class TestShortCap:
    """R1 空头封顶红线：SHORT 置信度不超过「中」。"""

    def test_short_high_capped_to_medium(self):
        """4 红灯 base=高 + SHORT → 封顶中（此前红线敞开,回归守护）。"""
        out = apply_confidence_rules("高", 5.0, _lights(0), [], direction=Direction.SHORT)
        assert out == "中"

    def test_short_already_medium_unchanged(self):
        out = apply_confidence_rules("中", 5.0, _lights(0), [], direction=Direction.SHORT)
        assert out == "中"

    def test_short_low_unchanged(self):
        out = apply_confidence_rules("低", 5.0, _lights(0), [], direction=Direction.SHORT)
        assert out == "低"

    def test_long_not_capped(self):
        """LONG 不受封顶影响：4 绿灯 base=高 + LONG → 高。"""
        out = apply_confidence_rules("高", 5.0, _lights(0), [], direction=Direction.LONG)
        assert out == "高"

    def test_neutral_not_capped(self):
        out = apply_confidence_rules("高", 5.0, _lights(0), [], direction=Direction.NEUTRAL)
        assert out == "高"

    def test_reason_recorded(self):
        reasons: list[str] = []
        apply_confidence_rules("高", 5.0, _lights(0), reasons, direction=Direction.SHORT)
        assert any("空头封顶" in r for r in reasons), "封顶必须写入理由链（可审计红线）"

    def test_short_cap_then_exhaustion_still_demotes(self):
        """封顶后耗尽闸门仍生效：SHORT|高 + 净流 300 亿 → 低。"""
        out = apply_confidence_rules("高", 350.0, _lights(0), [], direction=Direction.SHORT)
        assert out == "低"

    def test_default_direction_backward_compatible(self):
        """缺省 direction=NEUTRAL：旧调用方签名兼容,行为不变。"""
        assert apply_confidence_rules("高", 5.0, _lights(0), []) == "高"
        assert apply_confidence_rules("高", 350.0, _lights(0), []) == "低"


class TestApplyConfidenceGrayDemotion:
    """灰灯降级路径（既有规则的守护测试）。"""

    def test_four_gray_lights_demote(self):
        assert apply_confidence_rules("高", 5.0, _lights(4), []) == "中"
        assert apply_confidence_rules("中", 5.0, _lights(4), []) == "低"

    def test_three_gray_lights_no_demotion(self):
        assert apply_confidence_rules("高", 5.0, _lights(3), []) == "高"


class TestBacktest:
    """run_backtest forward-return 统计（合成 3 日序列,可控结果）。"""

    def test_forward_return_bucketed(self, tmp_path):
        from core.backtest import run_backtest
        import sqlite3
        from store.db import init_db
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        # 510300 三日价格 4.0 → 4.4 → 4.8：T+1 收益 +10%
        for d, c in [("2026-01-05", 4.0), ("2026-01-06", 4.4), ("2026-01-07", 4.8)]:
            conn.execute("INSERT INTO etf_daily (code, date, close) VALUES ('510300',?,?)", (d, c))
        # LONG|高 信号挂在首日
        conn.execute(
            "INSERT INTO signals (code, date, direction, confidence, resonance, net_flow_yi)"
            " VALUES ('510300','2026-01-05','LONG','高','机会共振',5.0)")
        conn.commit()
        out = run_backtest(conn)
        assert out["total_signals"] == 1
        b = out["buckets"]["LONG|高"]
        assert b["n"] == 1
        assert b["ret1"]["mean"] == pytest.approx(10.0, abs=0.01)
        assert b["ret1"]["win"] == 100.0
        assert b["ret20"] is None, "样本窗口不足 20 日应返回 None,不得冒充"

    def test_empty_signals(self, tmp_path):
        from core.backtest import run_backtest
        import sqlite3
        from store.db import init_db
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        out = run_backtest(conn)
        assert out["total_signals"] == 0 and out["buckets"] == {}


class TestAtomicWrite:
    """store.db.atomic_write：临时文件 + os.replace 原子性。"""

    def test_write_and_readback(self, tmp_path):
        from store.db import atomic_write
        target = tmp_path / "out" / "report.html"
        atomic_write(target, b"hello atomic")
        assert target.read_bytes() == b"hello atomic"

    def test_overwrite_replaces(self, tmp_path):
        from store.db import atomic_write
        target = tmp_path / "f.txt"
        atomic_write(target, b"v1")
        atomic_write(target, b"v2-longer-content")
        assert target.read_text() == "v2-longer-content"
        # 无临时文件残留
        assert [p.name for p in target.parent.iterdir()] == ["f.txt"]

    def test_no_tmp_left_on_failure(self, tmp_path, monkeypatch):
        """写入中途异常 → 临时文件清理,原文件不动。"""
        import store.db as dbmod
        from store.db import atomic_write
        target = tmp_path / "f.txt"
        atomic_write(target, b"keep-me")
        real_replace = os.replace
        def boom(a, b):
            raise OSError("replace failed")
        monkeypatch.setattr(dbmod.os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write(target, b"new-data")
        assert target.read_bytes() == b"keep-me"
        assert not [p for p in target.parent.iterdir() if p.suffix == ".tmp"]
