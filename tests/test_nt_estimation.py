# -*- coding: utf-8 -*-
"""国家队估算算法测试：活跃日判定 / θ 标定 / CD 数据结构 / invalid_delta 过滤。

夹具说明：con 返回 (连接, 报告输出目录)；generate_report 写入隔离目录，
测试通过 Path(text).read_text() 读取 HTML 再解析 CD 数据块。
"""
import json
import re
from pathlib import Path

import pytest

from core.report import generate_report, _invalid_delta_join
from core.calibration import estimate_theta, is_nt_entity
from tests.conftest import _dates


def _render(con, tmp_path) -> str:
    """生成报告并返回 HTML 全文。"""
    out = generate_report(con, out_dir=tmp_path)
    return Path(out).read_text(encoding="utf-8")


def _parse_cd(html: str) -> dict:
    m = re.search(r"var CD = (\{.*?\});\nvar C", html, re.S)
    assert m, "报告 HTML 中未找到 CD 数据块"
    return json.loads(m.group(1))


def _nt_flow_by_date(cd: dict) -> dict[str, float]:
    nt = cd["nt"]
    return {d: v for d, v in zip(nt["dates"], nt["daily"]) if v != 0}


class TestActiveDayClassification:
    """活跃日判定：量级 + 语境缺一不可（修复后的行为约束）。"""

    def test_bull_run_buy_not_flagged(self, con):
        """牛市抢筹日（指数 +3% 且大额净申购）不得标为国家队买入。"""
        conn, tmp = con
        html = _render(conn, tmp)
        cd = _parse_cd(html)
        flows = _nt_flow_by_date(cd)
        bull_day = _dates(90)[60]  # 种子：i=60 抢筹 +9 亿份，指数 +3%
        assert bull_day not in flows, f"牛市抢筹日 {bull_day} 被误标为国家队活跃日"

    def test_drawdown_buy_flagged(self, con):
        """回撤期大额申购日（指数 -2% 且大额净申购）应标为国家队买入。"""
        conn, tmp = con
        html = _render(conn, tmp)
        cd = _parse_cd(html)
        flows = _nt_flow_by_date(cd)
        protect_day = _dates(90)[75]  # 种子：i=75 护盘 +8 亿份，指数 -2%
        assert protect_day in flows, "回撤期护盘日未被标记"
        assert flows[protect_day] > 0, "护盘日估算应为正（买入）"

    def test_cum_matches_daily_sum(self, con):
        """累计线末值 = 活跃日估算逐日累加（自洽性）。"""
        conn, tmp = con
        nt = _parse_cd(_render(conn, tmp))["nt"]
        assert len(nt["dates"]) == len(nt["daily"]) == len(nt["cum"])
        assert nt["cum"][-1] == pytest.approx(
            sum(v for v in nt["daily"] if v), abs=0.5
        )

    def test_n_buy_n_sell_consistent(self, con):
        """n_buy / n_sell 与 daily 符号统计一致；n_active = n_buy + n_sell。"""
        conn, tmp = con
        nt = _parse_cd(_render(conn, tmp))["nt"]
        n_buy = sum(1 for v in nt["daily"] if v > 0)
        n_sell = sum(1 for v in nt["daily"] if v < 0)
        assert nt["n_buy"] == n_buy
        assert nt["n_sell"] == n_sell
        assert nt["n_active"] == n_buy + n_sell

    def test_last_active_points_to_nonzero(self, con):
        """last_active 指向最近一个非零估算日，且该日 daily 值非零。"""
        conn, tmp = con
        nt = _parse_cd(_render(conn, tmp))["nt"]
        if nt.get("last_active") is None:
            pytest.skip("种子数据无活跃日")
        idx = nt["dates"].index(nt["last_active"])
        assert nt["daily"][idx] != 0


class TestThetaCalibration:
    """θ 标定链路：锚点 → estimate_theta → CD.theta。"""

    def test_is_nt_entity(self):
        assert is_nt_entity("中央汇金投资有限责任公司")
        assert is_nt_entity("中国证券金融股份有限公司")
        assert not is_nt_entity("华夏基金管理有限公司")
        assert not is_nt_entity("香港中央结算有限公司(陆股通)")

    def test_estimate_theta_from_anchor(self, con):
        """锚点持有 50 亿份 / 总份额 100+ 亿份 → theta ∈ (0, 1.5]。"""
        conn, _ = con
        est = estimate_theta(conn, "510300")
        assert est, "有锚点却无估算结果"
        for period, v in est.items():
            if v.get("valid"):
                assert 0 < v["theta"] <= 1.5

    def test_cd_theta_in_range(self, con):
        """CD.nt.theta ∈ [0.5, 1.0]（report 侧 clamp 区间）。"""
        conn, tmp = con
        nt = _parse_cd(_render(conn, tmp))["nt"]
        if nt.get("theta") is None:
            pytest.skip("θ 未标定")
        assert 0.5 <= nt["theta"] <= 1.0

    def test_theta_note_mentions_anchor_count(self, con):
        """theta_n_anchor_periods 应存在且 ≥ 1（有有效锚点时）。"""
        conn, tmp = con
        nt = _parse_cd(_render(conn, tmp))["nt"]
        if nt.get("theta") is None:
            pytest.skip("θ 未标定")
        assert nt.get("theta_n_anchor_periods", 0) >= 1


class TestMarginChgFix:
    """_margin_chg 列名修复回归：必须读 fin_balance_yi（此前读 close 永远灰灯）。"""

    def test_margin_chg_positive(self, con):
        conn, _ = con
        from core.analysis import _margin_chg
        last_day = _dates(90)[-1]
        val = _margin_chg(conn, last_day)
        assert val is not None, "有 6 个月融资数据却返回 None（列名 bug 复发？）"
        assert val > 0, "递增融资余额应得正变化率"

    def test_margin_chg_insufficient_data(self, con):
        conn, _ = con
        from core.analysis import _margin_chg
        assert _margin_chg(conn, "2020-01-01") is None

    def test_margin_chg_missing_table(self, con):
        conn, _ = con
        from core.analysis import _margin_chg
        conn.execute("DROP TABLE margin_daily")
        assert _margin_chg(conn, "2026-12-01") is None


class TestInvalidDeltaFilter:
    """invalid_delta 读侧过滤：被标记日的份额不参与差分。"""

    def test_join_returns_marked(self, con):
        conn, _ = con
        conn.execute(
            "INSERT OR REPLACE INTO invalid_delta (code, date, reason) VALUES (?,?,?)",
            ("510300", "2026-06-02", "折算"),
        )
        conn.commit()
        marked, has = _invalid_delta_join(conn)
        assert has and ("510300", "2026-06-02") in marked

    def test_join_graceful_without_table(self, con):
        conn, _ = con
        conn.execute("DROP TABLE invalid_delta")
        marked, has = _invalid_delta_join(conn)
        assert marked == set() and has is False

    def test_filtered_day_excluded_from_diff(self, con):
        """被标记日之后一天的差分不使用被标记日份额（无跳变）。"""
        conn, tmp = con
        dates = _dates(90)
        conn.execute(
            "INSERT OR REPLACE INTO invalid_delta (code, date, reason) VALUES (?,?,?)",
            ("510300", dates[10], "折算"),
        )
        conn.commit()
        cd = _parse_cd(_render(conn, tmp))
        total = {d: v for d, v in zip(cd["nt"]["dates"], cd["nt"]["total"])}
        d11 = dates[11]  # 差分基准被过滤 → 只剩 510500 小额增量
        assert d11 in total
        assert abs(total[d11]) < 5.0, f"过滤后 {d11} 差分仍异常大: {total[d11]}"


class TestReportCdStructure:
    """报告 CD 数据结构完整性（前端绑定契约）。"""

    def test_cd_required_keys(self, con):
        conn, tmp = con
        cd = _parse_cd(_render(conn, tmp))
        for key in ("dates", "shares", "index", "nt", "margin"):
            assert key in cd, f"CD 缺少 {key}"

    def test_nt_grid_aligned_with_dates(self, con):
        """nt 全序列与 CD.dates 网格对齐（差分日≠披露日的对齐修复回归）。"""
        conn, tmp = con
        cd = _parse_cd(_render(conn, tmp))
        n = len(cd["dates"])
        nt = cd["nt"]
        assert len(nt["dates"]) == n, "nt.dates 未与主网格对齐"
        assert len(nt["daily"]) == n
        assert len(nt["cum"]) == n
        cums = [v for v in nt["cum"] if v is not None]
        assert all(isinstance(v, (int, float)) for v in cums)

    def test_html_card_shows_last_active_not_zero(self, con):
        """卡片在非活跃日应显示最近活跃日估算，而非误导性的 +0 亿。"""
        conn, tmp = con
        html = _render(conn, tmp)
        assert "国家队估算净买入" in html
        m = re.search(r'国家队估算净买入（活跃日口径）</div>\s*<div class="value">([^<]*)', html)
        assert m, "未找到国家队卡片值"
        val_text = m.group(1).strip()
        assert val_text not in ("+0", "+0.0", "0", "0.0"), f"卡片显示误导性零值: {val_text}"

    def test_fig_numbers_sequential(self, con):
        """图编号 1-6 连续无跳号（图1 合并进图2 后重排的回归）。"""
        conn, tmp = con
        html = _render(conn, tmp)
        nums = sorted(set(int(x) for x in re.findall(r"图\s*(\d)", html) if 0 < int(x) < 10))
        assert nums == list(range(1, len(nums) + 1)), f"图编号不连续: {nums}"
