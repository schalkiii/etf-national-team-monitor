# -*- coding: utf-8 -*-
"""份额折算与披露噪声的识别、标注、清洗（纯函数 + 存量修正编排）。

背景（学 A-Resonance share_adjust_jobs 踩坑 + 本库实证）：
  1. 份额折算（拆分/合并）按比例 k 改变总份额但不涉及资金申赎。
     差分 Δshares 会把折算日记成天量申购/赎回，污染 θ 锚定与国家队估算。
  2. 交易所披露噪声：份额披露存在口径断裂（如 512100 2022-07~09 份额虚高 2.7 倍后
     骤降；159845 深市序列 21% 的天数日变化 >5%），差分后同样是假申赎。

判定方法论（实证得出，非拍脑袋）：
  - 折算铁证 = 「持有人资产守恒」：折算不改变持有人财富，
    (1+份额变化)×(1+复权价变化) ≈ k（k 为折算比，通常 0.5/2/3 等干净比例），
    且 qfq 复权价格前后连续（|价格变化| < 3%）。幅度阈值法会误杀
    512100 2024-02-05 +132.8% 熔断日真申购（复权价 -6.3%，wealth=2.18 非干净比例）。
  - 披露噪声指纹 = 份额单日剧变后次日反向大幅回归（|次日变化| >10% 且方向相反）——
    真实资金流不会今天 +25% 明天 -20%。
  - 区间口径断裂 = 份额台阶前后规模（份额×复权价）无公告依据地翻倍/腰斩，
    以「区间禁用清单」人工确认（当前仅 512100 2022-07-22~09-02 一处）。
"""
from __future__ import annotations

import sqlite3

# ----------------------------------------------------------------
# 判定参数（全部集中于此，core 层不得散落魔法数字）
# ----------------------------------------------------------------
CANDIDATE_JUMP_PCT = 0.15        # 单日份额变化超过 ±15% 进入候选
WEALTH_CONSERVE_TOL = 0.08       # wealth 与折算比的相对容差 8%
PRICE_CONTINUITY_PCT = 0.03      # 折算要求复权价变化 < 3%
SPLIT_RATIOS = (0.25, 0.333, 0.5, 2.0, 3.0, 4.0)   # 常见折算比指纹
NEXT_DAY_REVERSAL_PCT = 0.10     # 次日反向跳变 >10% 视为披露噪声

# 人工确认的区间口径断裂（code, 起始日, 结束日, 原因）
# 512100: 2022-07-22~09-02 份额虚高约 2.7 倍（折算披露口径混乱），期间 Δshares 不可信
MANUAL_DEAD_ZONES: list[tuple[str, str, str, str]] = [
    ("512100", "2022-07-22", "2022-09-02", "折算披露口径混乱,份额虚高约2.7倍后骤降"),
]


def classify_jump(shares_chg: float, price_chg: float, next_chg: float | None) -> str:
    """单点跳变分类（纯函数）：返回 'split' / 'noise' / 'real'。

    - split: 资产守恒命中折算比指纹 + 价格连续
    - noise: 次日反向大幅回归（披露口径噪声）
    - real : 其余（真实申赎或无法判定,保留不清洗）
    """
    wealth = (1.0 + shares_chg) * (1.0 + price_chg)
    is_split_ratio = any(abs(wealth - k) / k < WEALTH_CONSERVE_TOL for k in SPLIT_RATIOS)
    if is_split_ratio and abs(price_chg) < PRICE_CONTINUITY_PCT:
        return "split"
    if next_chg is not None and next_chg * shares_chg < 0 and abs(next_chg) > NEXT_DAY_REVERSAL_PCT:
        return "noise"
    return "real"


def scan_anomalies(con: sqlite3.Connection) -> list[dict]:
    """全库扫描份额跳变点并分类。返回 [{code,date,type,shares_chg_pct,next_chg_pct}]。"""
    rows = con.execute(
        """SELECT code, date, shares, close,
                  LAG(shares) OVER (PARTITION BY code ORDER BY date) prev_s,
                  LEAD(shares) OVER (PARTITION BY code ORDER BY date) next_s,
                  LAG(close)  OVER (PARTITION BY code ORDER BY date) prev_c
           FROM etf_daily WHERE shares IS NOT NULL ORDER BY code, date"""
    ).fetchall()
    out = []
    for r in rows:
        if not (r["prev_s"] and r["shares"] and r["close"] and r["prev_c"]):
            continue
        schg = r["shares"] / r["prev_s"] - 1.0
        if abs(schg) < CANDIDATE_JUMP_PCT:
            continue
        pchg = (r["close"] / r["prev_c"] - 1.0) if r["prev_c"] else 0.0
        nchg = (r["next_s"] / r["shares"] - 1.0) if (r["next_s"] and r["shares"]) else None
        out.append({
            "code": r["code"], "date": r["date"], "type": classify_jump(schg, pchg, nchg),
            "shares_chg_pct": round(schg * 100, 2),
            "next_chg_pct": round(nchg * 100, 2) if nchg is not None else None,
        })
    return out


def build_split_registry(con: sqlite3.Connection) -> list[dict]:
    """从扫描结果构建折算登记表行（仅 split 类）。"""
    return [{"code": a["code"], "date": a["date"], "shares_chg_pct": a["shares_chg_pct"],
             "note": "资产守恒+价格连续确证折算"}
            for a in scan_anomalies(con) if a["type"] == "split"]


def repair_existing(con: sqlite3.Connection, dry_run: bool = True) -> dict:
    """存量修正：
    1. 建 share_splits 表并写入确证折算；
    2. 折算日与披露噪声日：该日 Δshares 无申赎含义 → 标记为无效
       （实现为写 invalid_delta=1，读侧 join 过滤，绝不改原始 shares 值——原始披露永不多touch）；
    3. 区间口径断裂：MANUAL_DEAD_ZONES 内全部日期标记无效。
    dry_run=True 只统计不动库。"""
    anomalies = scan_anomalies(con)
    splits = [a for a in anomalies if a["type"] == "split"]
    noises = [a for a in anomalies if a["type"] == "noise"]

    dead_rows = 0
    for code, d0, d1, _why in MANUAL_DEAD_ZONES:
        dead_rows += con.execute(
            "SELECT COUNT(*) FROM etf_daily WHERE code=? AND date BETWEEN ? AND ? AND shares IS NOT NULL",
            (code, d0, d1)).fetchone()[0]

    stats = {"splits": len(splits), "noise_days": len(noises), "dead_zone_rows": dead_rows}
    if dry_run:
        return stats

    con.executescript(
        """CREATE TABLE IF NOT EXISTS share_splits (
               code TEXT NOT NULL, date TEXT NOT NULL,
               shares_chg_pct REAL, note TEXT,
               PRIMARY KEY (code, date));
           CREATE TABLE IF NOT EXISTS invalid_delta (
               code TEXT NOT NULL, date TEXT NOT NULL,
               reason TEXT NOT NULL,
               PRIMARY KEY (code, date));"""
    )
    con.execute("DELETE FROM share_splits")
    for s in splits:
        con.execute("INSERT OR REPLACE INTO share_splits VALUES (?,?,?,?)",
                    (s["code"], s["date"], s["shares_chg_pct"], "资产守恒+价格连续确证折算"))
    con.execute("DELETE FROM invalid_delta")
    for a in [*splits, *noises]:
        reason = "折算日,无申赎含义" if a["type"] == "split" else "披露噪声,次日反向回归"
        con.execute("INSERT OR REPLACE INTO invalid_delta VALUES (?,?,?)",
                    (a["code"], a["date"], reason))
    for code, d0, d1, why in MANUAL_DEAD_ZONES:
        con.execute(
            """INSERT OR REPLACE INTO invalid_delta (code, date, reason)
               SELECT code, date, ? FROM etf_daily
               WHERE code=? AND date BETWEEN ? AND ? AND shares IS NOT NULL""",
            (why, code, d0, d1))
    con.commit()
    stats["invalid_rows"] = con.execute("SELECT COUNT(*) FROM invalid_delta").fetchone()[0]
    log_rows = con.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
    con.execute("INSERT INTO fetch_log (source, task, ok, rows, detail) VALUES (?,?,?,?,?)",
                ("internal", "repair_share_splits", 1, stats.get("invalid_rows", 0),
                 f"splits={stats['splits']} noise={stats['noise_days']} dead={stats['dead_zone_rows']}"))
    con.commit()
    return stats


def valid_flow_join_sql(code_col: str = "code", date_col: str = "date") -> str:
    """读侧过滤片段：AND NOT EXISTS (SELECT 1 FROM invalid_delta ...)。
    invalid_delta 表可能不存在（未跑修正前），调用方需 try/except 兜底。"""
    return (f"AND NOT EXISTS (SELECT 1 FROM invalid_delta iv "
            f"WHERE iv.code={code_col} AND iv.date={date_col})")
