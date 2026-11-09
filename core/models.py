# -*- coding: utf-8 -*-
"""数据模型：dataclass + 枚举。core 层通用词汇表。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
class FlowSource(str, Enum):
    """口径分级（学 etf-tracker）：估算值永不冒充真实值。"""
    REAL_DAILY = "真实锚定-每日"       # 交易所每日份额(T+1)
    REAL_QUARTER = "真实锚定-季报"     # 基金定期报告(滞后~60交易日)
    ESTIMATE = "估算-量价"             # 成交额×涨跌幅回退
    CALIBRATED = "锚点推算"            # θ 校准后的国家队估算


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    BLOCKED = "BLOCKED"   # 数据不可信,严禁降级为 NEUTRAL


class LightColor(str, Enum):
    RED = "红"    # 风险/出货
    GREEN = "绿"  # 机会/吸筹
    GRAY = "灰"   # 中性


@dataclass
class LightEvidence:
    """单灯判定依据链：值/分位/阈值全可审计。"""
    name: str
    color: LightColor
    value: float | None = None
    detail: str = ""


@dataclass
class Signal:
    """单标的信号。"""
    code: str
    date: str
    direction: Direction
    confidence: str                  # 高/中/低
    net_flow_yi: float | None = None     # 净申购估算(亿)
    flow_source: FlowSource | None = None
    lights: list[LightEvidence] = field(default_factory=list)
    resonance: str = "中性"          # 机会共振/危险共振/中性
    anomaly: bool = False
    anomaly_sigma: float | None = None
    grade_reasons: list[str] = field(default_factory=list)
    blocked_reason: str = ""


@dataclass
class HolderAnchor:
    """季报锚点：某报告期某国家队主体持有某 ETF 的名义份额(亿份)。"""
    code: str
    report_date: str        # 报告期截止日(如 2026-06-30)
    entity: str             # 主体名(中央汇金投资 等)
    shares_yi: float        # 亿份
    source: str = "基金定期报告"
