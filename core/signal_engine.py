# -*- coding: utf-8 -*-
"""描述层 + 异常层 + 共振层信号引擎（纯函数,无 I/O）。
三层递进：描述(quantdata.uk 克制) → MAD 异常(etf-tracker 稳健统计) → 分位数五灯共振(A-Resonance 框架)。"""
from __future__ import annotations

import math
from statistics import median

from core.config import (
    ANOMALY_SIGMA, ANOMALY_WINDOW,
    FLOW_BUY_PCT, FLOW_SELL_PCT, INDEX_DROP_PCT,
    MARKET_COLD_PCT, MARKET_HOT_PCT,
    PRICE_POS_HIGH_PCT, PRICE_POS_LOW_PCT, RESONANCE_COUNT,
)
from core.models import Direction, FlowSource, LightColor, LightEvidence, Signal


# ----------------------------------------------------------------
# 统计工具
# ----------------------------------------------------------------
def pct_rank(value: float, series: list[float]) -> float | None:
    """value 在 series 中的百分位(0-100)。series 需非空。"""
    if not series:
        return None
    below = sum(1 for v in series if v <= value)
    return 100.0 * below / len(series)


def mad_sigma(series: list[float]) -> float:
    """MAD 稳健标准差（学 etf-tracker：非标准差,抗事件尖峰污染）。"""
    if len(series) < 2:
        return 0.0
    med = median(series)
    mad = median([abs(v - med) for v in series])
    # 1.4826: MAD→正态一致σ
    return 1.4826 * mad


def zscore_mad(value: float, series: list[float]) -> float | None:
    s = mad_sigma(series)
    if s == 0:
        return None
    return (value - median(series)) / s


# ----------------------------------------------------------------
# 描述层
# ----------------------------------------------------------------
def describe_flow(delta_shares_yi: float | None, nav: float | None) -> tuple[float | None, FlowSource]:
    """净申购估算 = Δ份额 × 净值。份额缺失时返回 (None, ESTIMATE)。"""
    if delta_shares_yi is None or nav is None:
        return None, FlowSource.ESTIMATE
    return delta_shares_yi * nav, FlowSource.REAL_DAILY


# ----------------------------------------------------------------
# 异常层
# ----------------------------------------------------------------
def detect_anomaly(net_flow_yi: float, flow_history: list[float]) -> tuple[bool, float | None]:
    """3σ MAD 异常检测（只标注,不改结论）。"""
    if len(flow_history) < ANOMALY_WINDOW // 2:
        return False, None
    z = zscore_mad(net_flow_yi, flow_history[-ANOMALY_WINDOW:])
    if z is None:
        return False, None
    return abs(z) >= ANOMALY_SIGMA, round(z, 2)


# ----------------------------------------------------------------
# 共振层：五灯
# ----------------------------------------------------------------
def light_price_position(price: float, price_history: list[float]) -> LightEvidence:
    """L1 价格位置灯：近250日分位。"""
    rank = pct_rank(price, price_history[-250:])
    if rank is None:
        return LightEvidence("价格位置", LightColor.GRAY, rank, "历史不足")
    if rank <= PRICE_POS_LOW_PCT:
        return LightEvidence("价格位置", LightColor.GREEN, round(rank, 1), f"近250日分位{rank:.1f}%≤{PRICE_POS_LOW_PCT}% 低位")
    if rank >= PRICE_POS_HIGH_PCT:
        return LightEvidence("价格位置", LightColor.RED, round(rank, 1), f"近250日分位{rank:.1f}%≥{PRICE_POS_HIGH_PCT}% 高位")
    return LightEvidence("价格位置", LightColor.GRAY, round(rank, 1), f"近250日分位{rank:.1f}% 中性区间")


def light_share_flow(net_flow_yi: float | None, flow_history: list[float]) -> LightEvidence:
    """L2 份额流向灯：净申购历史分位。"""
    if net_flow_yi is None or len(flow_history) < 30:
        return LightEvidence("份额流向", LightColor.GRAY, None, "净申购数据不足30日")
    rank = pct_rank(net_flow_yi, flow_history)
    if rank is None:
        return LightEvidence("份额流向", LightColor.GRAY, None, "无历史")
    if rank >= FLOW_BUY_PCT:
        return LightEvidence("份额流向", LightColor.GREEN, round(rank, 1), f"净申购{net_flow_yi:+.1f}亿,处{rank:.0f}分位(≥{FLOW_BUY_PCT})")
    if rank <= FLOW_SELL_PCT:
        return LightEvidence("份额流向", LightColor.RED, round(rank, 1), f"净赎回{net_flow_yi:+.1f}亿,处{rank:.0f}分位(≤{FLOW_SELL_PCT})")
    return LightEvidence("份额流向", LightColor.GRAY, round(rank, 1), f"净申购{net_flow_yi:+.1f}亿,处{rank:.0f}分位")


def light_index_drawdown(index_chg_pct: float | None) -> LightEvidence:
    """L3 指数回撤灯：大跌日的大额申购才是护盘特征。"""
    if index_chg_pct is None:
        return LightEvidence("指数回撤", LightColor.GRAY, None, "指数数据缺失")
    if index_chg_pct <= INDEX_DROP_PCT:
        return LightEvidence("指数回撤", LightColor.GREEN, round(index_chg_pct, 2),
                             f"指数当日{index_chg_pct:+.2f}%≤{INDEX_DROP_PCT}% 护盘语境")
    return LightEvidence("指数回撤", LightColor.GRAY, round(index_chg_pct, 2), f"指数当日{index_chg_pct:+.2f}%")


def light_market_heat(market_amount: float, market_amount_history: list[float]) -> LightEvidence:
    """L4 市场热度灯：两市成交额分位。"""
    rank = pct_rank(market_amount, market_amount_history[-250:])
    if rank is None:
        return LightEvidence("市场热度", LightColor.GRAY, None, "成交额历史不足")
    if rank >= MARKET_HOT_PCT:
        return LightEvidence("市场热度", LightColor.RED, round(rank, 1), f"成交额分位{rank:.0f}%(≥{MARKET_HOT_PCT}) 过热")
    if rank <= MARKET_COLD_PCT:
        return LightEvidence("市场热度", LightColor.GREEN, round(rank, 1), f"成交额分位{rank:.0f}%(≤{MARKET_COLD_PCT}) 冷清")
    return LightEvidence("市场热度", LightColor.GRAY, round(rank, 1), f"成交额分位{rank:.0f}%")


def light_margin(margin_chg_pct: float | None) -> LightEvidence:
    """L5 融资杠杆灯：快速去杠杆=资金压力出清(绿语境)。"""
    if margin_chg_pct is None:
        return LightEvidence("融资杠杆", LightColor.GRAY, None, "融资数据缺失")
    if margin_chg_pct <= -1.0:
        return LightEvidence("融资杠杆", LightColor.GREEN, round(margin_chg_pct, 2), f"融资余额5日{margin_chg_pct:+.2f}% 快速去杠杆")
    if margin_chg_pct >= 1.0:
        return LightEvidence("融资杠杆", LightColor.RED, round(margin_chg_pct, 2), f"融资余额5日{margin_chg_pct:+.2f}% 快速加杠杆")
    return LightEvidence("融资杠杆", LightColor.GRAY, round(margin_chg_pct, 2), f"融资余额5日{margin_chg_pct:+.2f}%")


# ----------------------------------------------------------------
# 共振判定 + 置信度纪律
# ----------------------------------------------------------------
def resonance_verdict(lights: list[LightEvidence]) -> tuple[str, int, int]:
    """返回 (共振结论, 绿灯数, 红灯数)。"""
    green = sum(1 for l in lights if l.color == LightColor.GREEN)
    red = sum(1 for l in lights if l.color == LightColor.RED)
    if green >= RESONANCE_COUNT:
        return "机会共振", green, red
    if red >= RESONANCE_COUNT:
        return "危险共振", green, red
    return "中性", green, red


def apply_confidence_rules(base_conf: str, net_flow_yi: float | None,
                           lights: list[LightEvidence], reasons: list[str]) -> str:
    """置信度只降不升（学 etf-tracker 同构规则）。"""
    order = ["低", "中", "高"]
    conf = base_conf
    idx = order.index(conf)

    def demote():
        nonlocal idx
        if idx > 0:
            idx -= 1

    # 耗尽闸门：|净流|≥300亿 命中率塌陷 → 封顶低
    if net_flow_yi is not None and abs(net_flow_yi) >= 300:
        idx = 0
        reasons.append(f"耗尽闸门:|净流|{abs(net_flow_yi):.0f}亿≥300亿,历史命中率塌陷,封顶低置信")
    # 空头封顶：SHORT 不超过中
    # (方向在 report 层结合,此处只处理灯上下文)
    # 灯数据缺失(灰灯为主) → 降一级
    gray = sum(1 for l in lights if l.color == LightColor.GRAY)
    if gray >= 4:
        demote()
        reasons.append(f"灰灯{gray}盏:证据不足,降一级")
    return order[idx]


def combined_signal(net_flow: float | None, direction_hint: Direction) -> Direction:
    """BLOCKED 严禁降级为 NEUTRAL；样本不足走 BLOCKED。"""
    return direction_hint
