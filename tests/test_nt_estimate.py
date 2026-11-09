# -*- coding: utf-8 -*-
"""国家队估算纯函数测试（R4/R6/P5-4/P6-2）。

R4 抽离后算法层可直接断言,不再依赖 HTML 解析（间接且脆）：
- is_active_day: 买入/卖出/静默三分支 + 边界
- estimate_theta_active: clamp 边界（θ=1.5 截断、无锚点=1.0）
- estimate_nt_series / align_to_grid / pick_card_value: 端到端纯数据链路
"""
import pytest

from core.nt_estimate import (
    is_active_day, estimate_theta_active, estimate_nt_series,
    align_to_grid, pick_card_value,
    ACTIVE_P90_MIN_DAYS, THETA_ACTIVE_CLAMP,
)


class TestIsActiveDay:
    """活跃日判定三分支（量级 + 语境缺一不可）。"""

    def test_drawdown_heavy_buy_active(self):
        """回撤期放量：≥p90 且指数下跌 → 活跃买入。"""
        assert is_active_day(50.0, -0.5, 40.0) is True

    def test_bull_run_buy_not_active(self):
        """牛市抢筹：≥p90 但指数上涨 → 不活跃（修复回归）。"""
        assert is_active_day(50.0, 3.0, 40.0) is False

    def test_strong_protect_active(self):
        """强护盘语境：指数跌>1% 且净申购为正（无需达 p90）→ 活跃。"""
        assert is_active_day(3.0, -1.0, 40.0) is True

    def test_strong_protect_boundary_exactly_minus_one(self):
        """边界：指数恰好 -1.0%（≤-1.0 成立）→ 活跃。"""
        assert is_active_day(3.0, -1.0, None) is True

    def test_strong_protect_minus_point_nine_not_active(self):
        """边界：指数 -0.9% 且量级未达 p90 → 不活跃。"""
        assert is_active_day(3.0, -0.9, 40.0) is False

    def test_rebound_redeem_sell_active(self):
        """卖出日：反弹日（指数上涨）大额净赎回 ≥p90 → 活跃卖出。"""
        assert is_active_day(-50.0, 2.0, 40.0) is True

    def test_drawdown_redeem_not_active(self):
        """下跌日净赎回：不是「反弹兑现」语境 → 不活跃。"""
        assert is_active_day(-50.0, -2.0, 40.0) is False

    def test_small_redeem_on_rebound_not_active(self):
        """反弹日小额赎回（<p90）→ 不活跃。"""
        assert is_active_day(-5.0, 2.0, 40.0) is False

    def test_index_missing_silent(self):
        """指数数据缺失（idx_chg=None）→ 静默日,不冒充。"""
        assert is_active_day(50.0, None, 40.0) is False
        assert is_active_day(-50.0, None, 40.0) is False

    def test_p90_none_only_strong_protect(self):
        """p90=None（样本<60 日）：仅强护盘语境可触发。"""
        assert is_active_day(50.0, -0.5, None) is False
        assert is_active_day(3.0, -1.5, None) is True


class TestEstimateThetaActive:
    """θ_active 均值与 clamp 边界（P6-2 补齐）。"""

    def test_no_anchor_returns_full_attribution(self):
        """无有效锚点 → θ=1.0 全归因 + 计数 0（不冒充已标定）。"""
        theta, n = estimate_theta_active({})
        assert theta == 1.0 and n == 0

    def test_invalid_periods_excluded(self):
        """valid=False 的区间不参与均值。"""
        periods = {
            "p1": {"valid": False, "delta_nt_yi": 50.0, "theta": 0.9},
            "p2": {"valid": True, "delta_nt_yi": 50.0, "theta": 0.8},
        }
        theta, n = estimate_theta_active(periods)
        assert theta == 0.8 and n == 1

    def test_small_delta_nt_excluded(self):
        """建仓期国家队增量 <5 亿份的区间不参与（噪声锚点）。"""
        periods = {
            "p1": {"valid": True, "delta_nt_yi": 3.0, "theta": 0.1},
            "p2": {"valid": True, "delta_nt_yi": 50.0, "theta": 0.9},
        }
        theta, n = estimate_theta_active(periods)
        assert theta == 0.9 and n == 1

    def test_theta_above_15_excluded(self):
        """原始 θ>1.5（异常外推）不参与均值。"""
        periods = {
            "p1": {"valid": True, "delta_nt_yi": 50.0, "theta": 2.5},
            "p2": {"valid": True, "delta_nt_yi": 50.0, "theta": 0.7},
        }
        theta, n = estimate_theta_active(periods)
        assert theta == 0.7 and n == 1

    def test_clamp_upper_bound(self):
        """均值 >1.0 截断到 1.0（clamp 上界守护,P6-2）。"""
        periods = {"p1": {"valid": True, "delta_nt_yi": 50.0, "theta": 1.2}}
        theta, n = estimate_theta_active(periods)
        assert theta == 1.0 and n == 1

    def test_clamp_lower_bound(self):
        """均值 <0.5 截断到 0.5（clamp 下界守护,P6-2）。"""
        periods = {"p1": {"valid": True, "delta_nt_yi": 50.0, "theta": 0.3}}
        theta, n = estimate_theta_active(periods)
        assert theta == 0.5 and n == 1

    def test_multi_period_mean(self):
        """多期均值：θ=0.6 与 0.8 → 0.7。"""
        periods = {
            "p1": {"valid": True, "delta_nt_yi": 50.0, "theta": 0.6},
            "p2": {"valid": True, "delta_nt_yi": 60.0, "theta": 0.8},
        }
        theta, n = estimate_theta_active(periods)
        assert theta == 0.7 and n == 2


class _NtSeriesHarness:
    """构造 estimate_nt_series 的可控输入：n 日,份额只在指定日跳变。"""

    @staticmethod
    def build(n_days: int, jump_day_idx: dict[int, float] = None,
              idx_path: str = "flat") -> tuple[dict, dict]:
        jump_day_idx = jump_day_idx or {}
        dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n_days)]
        share = 100.0e8
        by_date = {}
        idx_map = {}
        px = 4.0
        for i, d in enumerate(dates):
            if i in jump_day_idx:
                share += jump_day_idx[i] * 1e8
            else:
                share += 0.05e8
            if idx_path == "flat":
                pass
            elif idx_path == "up":
                px *= 1.02
            elif idx_path == "down":
                px *= 0.98
            by_date[d] = {"510300": (share, 4.0)}
            idx_map[d] = round(px, 2)
        return by_date, idx_map


class TestEstimateNtSeries:
    """端到端纯数据链路：by_date+idx_map → 估算序列。"""

    def test_quiet_series_all_silent(self):
        """平稳微增 90 日 + 指数持平 → 无活跃日（量级不达 p90,语境不符）。"""
        by_date, idx_map = _NtSeriesHarness.build(90)
        est = estimate_nt_series(by_date, idx_map, 0.8)
        assert est["n_active"] == 0
        assert all(v == 0.0 for v in est["daily"])
        assert est["cum"][-1] == 0.0

    def test_jump_without_context_inactive(self):
        """单一跳变但指数持平 → 量级达 p90 但语境不符 → 不活跃。"""
        by_date, idx_map = _NtSeriesHarness.build(90, {80: 20.0})
        est = estimate_nt_series(by_date, idx_map, 0.8)
        assert est["n_active"] == 0

    def test_theta_scales_active_flow(self):
        """活跃日估算 = 总净申购 × θ_active（数值口径契约）。"""
        # 手工构造: 首日微涨(常态,不构成强护盘) + 末日强护盘（指数 -2%）小额申购
        dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(84)]
        share = 100.0e8
        by_date = {}
        idx = 3000.0
        idx_map = {}
        for i, d in enumerate(dates):
            if i == len(dates) - 1:
                share += 5.0e8
                idx *= 0.98
            else:
                share += 0.05e8
                idx *= 1.0005
            by_date[d] = {"510300": (share, 4.0)}
            idx_map[d] = round(idx, 2)
        est = estimate_nt_series(by_date, idx_map, 0.75)
        # 首日 idx 基准为 3000 → idx_chg=None（静默）,唯末日的强护盘语境触发
        # 末日总净申购 = 5 亿份 × 4 元 = 20 亿; 估算 = 20 × θ(0.75) = 15 亿
        assert est["n_active"] == 1
        assert est["daily"][-1] == pytest.approx(15.0, abs=0.1)

    def test_sell_day_negative_flow(self):
        """卖出日：反弹日大额净赎回 → daily<0 且 cum 下拉。"""
        dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(84)]
        share = 100.0e8
        by_date = {}
        idx = 3000.0
        idx_map = {}
        for i, d in enumerate(dates):
            if i == len(dates) - 1:
                share -= 20.0e8
                idx *= 1.03  # 反弹日
            else:
                share += 0.05e8
                idx *= 1.0005
            by_date[d] = {"510300": (share, 4.0)}
            idx_map[d] = round(idx, 2)
        est = estimate_nt_series(by_date, idx_map, 1.0)
        assert est["n_sell"] == 1
        assert est["daily"][-1] < 0
        assert est["cum"][-1] < 0


class TestAlignToGrid:
    """差分网格 → 披露日网格对齐（缺失日补 0 / cum 前值填充）。"""

    def test_missing_days_filled_zero_and_carry(self):
        nt_est = {
            "dates": ["2026-01-02", "2026-01-05"],
            "daily": [10.0, -4.0],
            "cum": [10.0, 6.0],
            "total": [12.0, -5.0],
            "active": [1, 1],
        }
        grid = ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        out = align_to_grid(nt_est, grid)
        assert out["daily"] == [10.0, 0.0, 0.0, -4.0]
        assert out["cum"] == [10.0, 10.0, 10.0, 6.0], "cum 缺失日用前值填充"
        assert out["total"] == [12.0, 0.0, 0.0, -5.0]

    def test_grid_head_before_first_diff_day(self):
        """披露网格头部（首个差分日之前）→ 全 0,cum 用 0 起点。"""
        nt_est = {
            "dates": ["2026-01-05"],
            "daily": [7.0], "cum": [7.0], "total": [8.0], "active": [1],
        }
        grid = ["2026-01-02", "2026-01-03", "2026-01-05"]
        out = align_to_grid(nt_est, grid)
        assert out["daily"] == [0.0, 0.0, 7.0]
        assert out["cum"] == [0.0, 0.0, 7.0]


class TestPickCardValue:
    """卡片口径：非活跃日显示最近活跃日值并标日期（P6-1 直接断言）。"""

    def test_last_day_zero_shows_last_active(self):
        daily = [5.0, 0.0, 3.0, 0.0]
        dates = ["d1", "d2", "d3", "d4"]
        val, label = pick_card_value(daily, dates, "d3")
        assert val == 3.0
        assert label == "最近活跃日 d3"

    def test_last_day_active_shows_today(self):
        daily = [5.0, 0.0, 3.0, 8.0]
        dates = ["d1", "d2", "d3", "d4"]
        val, label = pick_card_value(daily, dates, "d4")
        assert val == 8.0
        assert label == "当日"

    def test_no_active_day_shows_zero(self):
        """全零序列（无活跃日）→ 显示 0/当日（无 last_active 可回落）。"""
        val, label = pick_card_value([0.0, 0.0], ["d1", "d2"], None)
        assert val == 0.0
        assert label == "当日"

    def test_last_active_not_in_grid(self):
        """last_active 不在披露网格（防御分支）→ 回落当日值。"""
        val, label = pick_card_value([0.0], ["d1"], "2099-01-01")
        assert val == 0.0 and label == "当日"
