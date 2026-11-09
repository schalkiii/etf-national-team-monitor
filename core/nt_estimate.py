# -*- coding: utf-8 -*-
"""国家队每日净买入估算（纯函数,无 I/O）——report 与推送摘要共用的算法层。

双口径设计（口径诚实原则,学 etf-tracker BLOCKED 纪律:不确定就不给数字）：
  ① 活跃日口径：当日核心宽基总净申购处于历史高分位(≥90分位)且处于护盘语境
     （指数回撤 or 强护盘）→ 判定国家队活跃,估算 = 总净申购 × θ_active
     θ_active 取锚点标定过的建仓期 θ（无锚点时保守取 1.0,即全归因,并在图注声明）
  ② 静默日：估算 = 0,不冒充。

活跃日判定（行为模式约束：量级 + 语境缺一不可,拒绝把牛市抢筹标成国家队）：
  买入日: |总净申购| ≥ p90 且 当日指数下跌（回撤期放量）; 或 强护盘语境(指数跌>1% 且净申购>0)
  卖出日: 反弹日大额净赎回（指数上涨且总净赎回 ≥ p90,疑似撤退兑现）
"""
from __future__ import annotations

# ----------------------------------------------------------------
# 判定参数（集中管理,与 report 图注口径一致）
# ----------------------------------------------------------------
ACTIVE_P90_MIN_DAYS = 60     # 样本 <60 日时 p90 不可估,活跃日判定整体关闭
ACTIVE_QUANTILE = 0.90       # 总净申购量级分位门槛
STRONG_PROTECT_DROP_PCT = -1.0   # 强护盘语境: 指数单日跌幅 ≤ -1%
THETA_ACTIVE_CLAMP = (0.5, 1.0)  # θ_active 保守截断区间
THETA_ACTIVE_MIN_DELTA_NT = 5.0  # 建仓期国家队增量 <5 亿份的锚点区间不参与均值
THETA_ACTIVE_MAX_RAW = 1.5       # 原始 θ >1.5 的区间不参与均值(异常外推)


def estimate_theta_active(theta_by_period: dict[str, dict]) -> tuple[float, int]:
    """θ_active：取所有锚点区间里 ΔNT>0 的正建仓期 θ 均值（截断到保守区间）。

    参数为 calibration.estimate_theta 的输出（{period: {theta, delta_nt_yi, valid,...}}）。
    返回 (theta_active, 参与均值的建仓期数)。无有效区间时返回 (1.0, 0)——全归因,
    不冒充已标定。"""
    active_thetas = []
    for v in theta_by_period.values():
        if (v.get("valid") and v.get("delta_nt_yi", 0) > THETA_ACTIVE_MIN_DELTA_NT
                and v.get("theta") and 0 < v["theta"] <= THETA_ACTIVE_MAX_RAW):
            active_thetas.append(min(v["theta"], 1.0))
    if not active_thetas:
        return 1.0, 0
    lo, hi = THETA_ACTIVE_CLAMP
    return round(min(max(sum(active_thetas) / len(active_thetas), lo), hi), 3), len(active_thetas)


def _p90_threshold(total_series: list[float]) -> float | None:
    """|总净申购| 的 90 分位门槛；样本不足时返回 None（判定关闭）。"""
    abs_series = [abs(v) for v in total_series]
    if len(abs_series) < ACTIVE_P90_MIN_DAYS:
        return None
    return sorted(abs_series)[int(len(abs_series) * ACTIVE_QUANTILE)]


def is_active_day(total_flow: float, idx_chg: float | None, p90: float | None) -> bool:
    """单日活跃判定（买入或卖出方向通用）。idx_chg=None（指数数据缺失）→ 静默日。"""
    if idx_chg is None:
        return False
    if total_flow > 0:
        if p90 is not None and abs(total_flow) >= p90 and idx_chg < 0:
            return True  # 回撤期放量申购
        if idx_chg <= STRONG_PROTECT_DROP_PCT:
            return True  # 强护盘语境(跌>1%仍净申购)
    elif total_flow < 0:
        if idx_chg > 0 and p90 is not None and abs(total_flow) >= p90:
            return True  # 反弹日大额净赎回(疑似撤退兑现)
    return False


def estimate_nt_series(by_date: dict[str, dict[str, tuple[float, float]]],
                       idx_map: dict[str, float],
                       theta_active: float) -> dict:
    """国家队估算主入口（纯函数）。

    参数：
      by_date: {date: {code: (复权份额, 当日收盘)}} —— 调用方已过滤 invalid_delta
      idx_map: {date: 上证收盘}（涨跌计算用）
      theta_active: estimate_theta_active 输出

    返回：
      daily/cum/total/active/date 系列（差分日网格,与披露日网格不同）
      n_buy/n_sell/last_active 汇总字段
    """
    sorted_dates = sorted(by_date)
    total_series: list[float] = []
    date_series: list[str] = []
    for i in range(1, len(sorted_dates)):
        d0, d1 = sorted_dates[i - 1], sorted_dates[i]
        prev_map, cur_map = by_date[d0], by_date[d1]
        total_flow = 0.0
        for code, (s, px) in cur_map.items():
            if code in prev_map:
                total_flow += (s - prev_map[code][0]) / 1e8 * px
        total_series.append(total_flow)
        date_series.append(d1)

    p90 = _p90_threshold(total_series)
    nt_daily, nt_cum, nt_total, nt_flags = [], [], [], []
    cum = 0.0
    for i, d1 in enumerate(date_series):
        tf = total_series[i]
        # 首差分日（i=0）前一交易日在差分网格之外 → idx_chg=None（静默,不冒充）。
        # 注意不能用 date_series[i-1]：i=0 时 Python 负索引会回绕到末日（曾致首日误标活跃）。
        idx_prev = sorted_dates[i] if i > 0 else None
        prev_c = idx_map.get(idx_prev) if idx_prev else None
        cur_c = idx_map.get(d1)
        idx_chg = (cur_c / prev_c - 1) * 100 if (prev_c and cur_c) else None
        active = is_active_day(tf, idx_chg, p90)
        nt_flow = round(tf * theta_active, 1) if active else 0.0
        nt_total.append(round(tf, 1))
        nt_daily.append(round(nt_flow, 1))
        nt_flags.append(1 if active else 0)
        cum += nt_flow
        nt_cum.append(round(cum, 1))

    return {
        "daily": nt_daily, "cum": nt_cum, "total": nt_total, "active": nt_flags,
        "dates": date_series,
        "n_active": sum(nt_flags),
        "n_buy": sum(1 for v in nt_daily if v > 0),
        "n_sell": sum(1 for v in nt_daily if v < 0),
        "last_active": next((d for d, v in reversed(list(zip(date_series, nt_daily))) if v != 0), None),
    }


def align_to_grid(nt_est: dict, grid_dates: list[str]) -> dict[str, list]:
    """差分日网格 → 披露日网格对齐（缺失日补 0：非活跃日本就是零,不冒充）。

    cum 缺失日用前值填充（阶梯连续,避免折线断裂）。"""
    nt_by_date = dict(zip(nt_est["dates"], zip(nt_est["daily"], nt_est["cum"],
                                               nt_est["total"], nt_est["active"])))
    daily_a = [nt_by_date.get(d, (0.0, None, 0.0, 0))[0] for d in grid_dates]
    cum_a = [nt_by_date.get(d, (0.0, None, 0.0, 0))[1] for d in grid_dates]
    total_a = [nt_by_date.get(d, (0.0, None, 0.0, 0))[2] for d in grid_dates]
    prev = 0.0
    for i, v in enumerate(cum_a):
        if v is None:
            cum_a[i] = prev
        else:
            prev = v
    return {"daily": daily_a, "cum": cum_a, "total": total_a}


def pick_card_value(daily: list, dates: list[str], last_active: str | None) -> tuple[float | None, str]:
    """卡片显示口径：当日非活跃日时展示最近活跃日估算（标日期）,避免误导性 +0 亿。

    参数 daily/dates 为网格对齐后的序列,last_active 来自估算汇总字段。
    返回 (显示值, 标签)。"""
    last = daily[-1] if daily else None
    if (last or 0) == 0 and last_active:
        d_idx = {d: i for i, d in enumerate(dates)}
        ai = d_idx.get(last_active)
        if ai is not None:
            return daily[ai], f"最近活跃日 {last_active}"
    return last, "当日"
