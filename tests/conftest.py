# -*- coding: utf-8 -*-
"""共用测试夹具：内存数据库 + 合成数据种子。

合成数据设计要点（与真实数据形态对齐）：
- etf_daily: 2 只核心 ETF × ~90 交易日，份额走势含一次"回撤期大额申购"与一次"牛市抢筹"
- index_daily: sh000001 与份额差分同网格，含对应涨跌
- margin_daily: fin_balance_yi 列（修复后 _margin_chg 读取该列）
- fund_holders: 510300 一条国家队锚点，用于 θ 标定链路
"""
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from store.db import init_db  # noqa: E402
from core.config import CORE_CODES  # noqa: E402

N_DAYS = 90  # ≥60 保证 p90 分位可用


def _dates(n: int) -> list[str]:
    """n 个连续工作日（跳过周末），日期字符串 YYYY-MM-DD。"""
    import datetime as dt
    d = dt.date(2026, 6, 1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


@pytest.fixture()
def con(tmp_path):
    """内存库 + schema + 合成数据 + 报告输出目录隔离。返回 (连接, 报告输出目录)。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    seed(conn)
    yield conn, tmp_path
    conn.close()


def seed(conn: sqlite3.Connection) -> None:
    dates = _dates(N_DAYS)
    # DB 单位约定：shares 为"份"（生产库 ~4.6e10），读取侧 /1e8 转亿份
    base_share = {"510300": 100.0e8, "510500": 80.0e8}  # 份
    base_close = {"510300": 4.0, "510500": 6.5}

    # 指数走势：平稳微涨，i=75 处大跌 -2%（强护盘语境），i=60 处大涨 +3%（牛市抢筹）
    idx = 3000.0
    idx_map = {}
    for i, d in enumerate(dates):
        if i == 75:
            idx *= 0.98   # -2%
        elif i == 60:
            idx *= 1.03   # +3%
        else:
            idx *= 1.0005
        idx_map[d] = round(idx, 2)
    for d, c in idx_map.items():
        conn.execute(
            "INSERT OR REPLACE INTO index_daily (index_code, date, close, amount) VALUES (?,?,?,?)",
            ("sh000001", d, c, 3e11),
        )

    for code in ("510300", "510500"):
        share = base_share[code]
        for i, d in enumerate(dates):
            if i == 75:
                # 回撤期大额申购：单日 +8 亿份（全历史最高量级之一）
                share += 8.0e8
            elif i == 60:
                # 牛市抢筹：单日 +9 亿份（量级更大，但指数 +3% 上涨 → 不应标国家队）
                share += 9.0e8
            else:
                share += 0.05e8  # 常态微增
            close = base_close[code] * (idx_map[d] / 3000.0)
            conn.execute(
                "INSERT OR REPLACE INTO etf_daily (code, date, close, shares, adjust_factor) VALUES (?,?,?,?,1.0)",
                (code, d, round(close, 4), round(share, 0)),
            )

    # 融资余额：6 个点递增，_margin_chg 应为正
    margin_dates = dates[-6:]
    for k, d in enumerate(margin_dates):
        conn.execute(
            "INSERT OR REPLACE INTO margin_daily (date, fin_balance_yi) VALUES (?,?)",
            (d, 18000.0 + k * 100.0),
        )

    # 国家队锚点：510300 两个报告期（θ 差分需要 ≥2 期）
    # 两期都必须落在 etf_daily 数据窗口内（2026-06 ~ 2026-10），
    # 否则 total_shares_at 返回 None → θ 无法标定
    conn.execute(
        "INSERT OR REPLACE INTO fund_holders (code, report_date, entity, shares_yi) VALUES (?,?,?,?)",
        ("510300", "2026-06-02", "中央汇金投资有限责任公司", 50.0),
    )
    conn.execute(
        "INSERT OR REPLACE INTO fund_holders (code, report_date, entity, shares_yi) VALUES (?,?,?,?)",
        ("510300", "2026-09-30", "中央汇金投资有限责任公司", 58.0),
    )
    # invalid_delta 表（生产库由 core/splits.py 的 repair 流程创建,schema 本体无此表;
    # 测试中预建空表以匹配生产就绪状态,表缺失场景另有专门测试覆盖）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS invalid_delta ("
        " code TEXT NOT NULL, date TEXT NOT NULL, reason TEXT, PRIMARY KEY (code, date))"
    )
    conn.commit()

