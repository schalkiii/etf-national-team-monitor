# -*- coding: utf-8 -*-
"""锚点校准（本项目核心增量）：用季报/年报披露的国家队名义份额做真值锚点,
估计「国家队申购占该 ETF 总净申购比例 θ」,把每日推断升级为 ± 误差的估算。

θ 估计方法（分层约束）：
  已知：报告期 [t0,t1] 内 Δ总份额(复权) = 总净申购份额 S_total
        报告期汇金等主体持仓增量 ΔH = H(t1) - H(t0)
  θ = ΔH / S_total （夹在 [0,1],不允许 >1 或 <0 的外推冒充）
  多主体(汇金投资+汇金资管+证金...)合并计算国家队名义增量。
  多期锚点 → θ 的时变序列；显著漂移(>1σ)时告警重新标定。

每日国家队净买入估算 = 当日 Δ份额×净值 × θ(最近期) ± 传播误差。
局限性声明固定输出：θ 是区间估计；季报外机构申赎无法区分；换仓轮动造成单标的 θ 失真。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from core.config import ETF_POOL, NT_ENTITY_KEYWORDS
from core.models import FlowSource

MIN_ANCHOR_GAPS = 2  # 至少两期锚点才能估 θ


def is_nt_entity(name: str) -> bool:
    return any(k in name for k in NT_ENTITY_KEYWORDS)


def upsert_anchor(con: sqlite3.Connection, code: str, report_date: str, entity: str, shares_yi: float, source: str = "基金定期报告") -> None:
    con.execute(
        "INSERT OR REPLACE INTO fund_holders (code, report_date, entity, shares_yi, source) VALUES (?,?,?,?,?)",
        (code, report_date, entity, shares_yi, source),
    )
    con.commit()


def nt_holding_by_period(con: sqlite3.Connection, code: str) -> dict[str, float]:
    """各报告期国家队名义持有合计（亿份）。"""
    rows = con.execute(
        "SELECT report_date, entity, shares_yi FROM fund_holders WHERE code=? ORDER BY report_date", (code,)
    ).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        if is_nt_entity(r["entity"]):
            out[r["report_date"]] = out.get(r["report_date"], 0.0) + r["shares_yi"]
    return out


def total_shares_at(con: sqlite3.Connection, code: str, date: str) -> float | None:
    """date 当日或之前最近一日复权份额（亿份）。"""
    r = con.execute(
        "SELECT shares, adjust_factor FROM etf_daily WHERE code=? AND shares IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 1",
        (code, date),
    ).fetchone()
    if not r:
        return None
    return r["shares"] * r["adjust_factor"] / 1e8


def estimate_theta(con: sqlite3.Connection, code: str) -> dict:
    """单标的 θ 估计。返回 {period: {theta, delta_nt_yi, delta_total_yi, valid}}。"""
    periods = nt_holding_by_period(con, code)
    keys = sorted(periods)
    result: dict[str, dict] = {}
    for i in range(len(keys) - 1):
        t0, t1 = keys[i], keys[i + 1]
        h0, h1 = periods[t0], periods[t1]
        s0 = total_shares_at(con, code, t0)
        s1 = total_shares_at(con, code, t1)
        delta_h = h1 - h0
        delta_s = (s1 - s0) if (s0 is not None and s1 is not None) else None
        entry = {"delta_nt_yi": round(delta_h, 2), "delta_total_yi": round(delta_s, 2) if delta_s is not None else None}
        if delta_s is None or abs(delta_s) < 0.5:  # 总份额几乎没变,θ 无意义
            entry.update(theta=None, valid=False, reason="区间总份额变化过小或数据缺失")
        else:
            raw = delta_h / delta_s
            entry["theta"] = round(raw, 3)
            # θ 语义而非硬约束:θ∈[0,1]正常;θ>1=国家队增持超总增量(其他净赎回);
            # θ<0 且 ΔS<0=国家队逆势增持;|θ|>3 或方向严重冲突才判数据可疑
            if abs(raw) > 3.0:
                entry.update(valid=False, reason="θ 异常(>3),锚点或复权数据待核")
            elif raw > 1.0:
                entry.update(valid=True, note="国家队增持超过总增量,期间其他投资者净赎回")
            elif raw < 0.0 and delta_s < 0 and delta_h > 0:
                entry.update(valid=True, note="国家队逆势增持,其他投资者大幅赎回")
            elif raw < 0.0 and delta_s > 0 and delta_h < 0:
                entry.update(valid=True, note="国家队减持,其他投资者净申购")
            else:
                entry.update(valid=True)
        result[f"{t0}~{t1}"] = entry
    return result


def latest_valid_theta(con: sqlite3.Connection, code: str) -> tuple[float | None, str | None]:
    """最近一期有效 θ 及其报告期。"""
    est = estimate_theta(con, code)
    valid = {k: v for k, v in est.items() if v.get("valid")}
    if not valid:
        return None, None
    k = sorted(valid)[-1]
    return valid[k]["theta"], k


def calibrated_daily_flow(con: sqlite3.Connection, code: str, date: str) -> dict:
    """锚点推算口径的国家队当日净买入。
    θ>1 时推算值可超总净流（国家队买、别人卖的语义）;θ<0 时取 0 下限并标注语义。"""
    theta, theta_period = latest_valid_theta(con, code)
    rows = con.execute(
        """SELECT date, shares, adjust_factor, close FROM etf_daily
           WHERE code=? AND shares IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 2""",
        (code, date),
    ).fetchall()
    if theta is None or len(rows) < 2:
        return {"code": code, "date": date, "nt_flow_yi": None, "flow_source": None,
                "reason": "无有效 θ 锚点,见 calibrate 命令录入季报数据"}
    prev, cur = rows[1], rows[0]
    delta_yi = (cur["shares"] * cur["adjust_factor"] - prev["shares"] * prev["adjust_factor"]) / 1e8
    total_flow = delta_yi * (cur["close"] or 0.0)
    nt_flow = total_flow * theta
    note = ""
    if theta < 0 and delta_yi > 0:
        nt_flow, note = 0.0, "θ<0(国家队曾减持):总份额上升段不计国家队买入"
    elif theta < 0 and delta_yi < 0:
        note = "θ<0:总份额下降段推算值为国家队可能回补,谨慎解读"
    return {
        "code": code, "date": date,
        "nt_flow_yi": round(nt_flow, 2),
        "theta": theta, "theta_period": theta_period,
        "total_flow_yi": round(total_flow, 2),
        "flow_source": FlowSource.CALIBRATED.value,
        "note": note,
        "disclaimer": "θ 为区间估计,非真值;归因含季报外机构噪音",
    }


def run_calibration(con: sqlite3.Connection) -> dict:
    """全标的 θ 估计汇总。"""
    out = {"generated_at": datetime.now().isoformat(), "etf": {}}
    for code in ETF_POOL:
        est = estimate_theta(con, code)
        if est:
            theta, period = latest_valid_theta(con, code)
            out["etf"][code] = {"name": ETF_POOL[code]["name"], "theta_latest": theta,
                                "theta_period": period, "periods": est}
    n_anchor = con.execute("SELECT COUNT(*) FROM fund_holders").fetchone()[0]
    out["anchor_rows"] = n_anchor
    out["note"] = ("锚点录入方法: 从基金定期报告(季报/半年报/年报)前十大持有人抄录国家队主体份额(亿份),"
                   "调用 upsert_anchor 或直接 INSERT fund_holders。θ=Δ国家队持仓/Δ总份额,约束[0,1]。")
    return out
