# -*- coding: utf-8 -*-
"""飞书 webhook 推送（学 etf-tracker webhook 状态机：相邻有效运行间方向翻转才推送）。
密钥走环境变量 ETF_NTM_FEISHU_WEBHOOK,严禁硬编码/写日志。"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from pathlib import Path

from core.config import FEISHU_WEBHOOK_ENV, PUSH_ON_FLIP_ONLY, DATA_DIR

_STATE_FILE = DATA_DIR / "last_signal_state.json"


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    os.replace(tmp, _STATE_FILE)


def _direction_signature(con: sqlite3.Connection) -> dict:
    """当日各标的方向 + 核心层合计标签 → 推送状态指纹。"""
    last = con.execute("SELECT date FROM calendar WHERE is_trading=1 ORDER BY date DESC LIMIT 1").fetchone()
    if not last:
        return {}
    rows = con.execute(
        "SELECT code, direction, resonance FROM signals WHERE date=?", (last["date"],)
    ).fetchall()
    return {"date": last["date"], "dirs": {r["code"]: [r["direction"], r["resonance"]] for r in rows}}


def _build_message(con: sqlite3.Connection) -> str | None:
    sig = _direction_signature(con)
    if not sig:
        return None
    lines = [f"📊 ETF 国家队监控 · {sig['date']}", ""]
    longs = [c for c, (d, _) in sig["dirs"].items() if d == "LONG"]
    shorts = [c for c, (d, _) in sig["dirs"].items() if d == "SHORT"]
    resonances = [(c, r) for c, (_, r) in sig["dirs"].items() if r != "中性"]
    if resonances:
        lines.append("共振信号：")
        lines += [f"  {c} {r}" for c, r in resonances[:8]]
    if longs:
        lines.append(f"看多标的方向: {', '.join(longs[:10])}")
    if shorts:
        lines.append(f"看空标的方向: {', '.join(shorts[:10])}")
    if not resonances and not longs and not shorts:
        lines.append("今日无共振/方向信号（中性）。")
    lines.append("")
    lines.append("（份额变化仅证明大资金申赎，归因国家队需官宣+季报锚点验证；不构成投资建议）")
    return "\n".join(lines)


def push_daily(con: sqlite3.Connection, force: bool = False) -> bool:
    """推送主入口。方向翻转才推（PUSH_ON_FLIP_ONLY）,force 可强制。"""
    webhook = os.environ.get(FEISHU_WEBHOOK_ENV, "").strip()
    if not webhook:
        print(f"[push] 未配置 {FEISHU_WEBHOOK_ENV} 环境变量,跳过推送（本地报告仍生成）")
        return False
    cur = _direction_signature(con)
    if not cur:
        return False
    prev = _load_state()
    if PUSH_ON_FLIP_ONLY and not force:
        if prev.get("dirs") == cur.get("dirs"):
            print("[push] 方向无变化,按状态机跳过")
            return True  # 无变化不算失败
    msg = _build_message(con)
    if not msg:
        return False
    payload = {"msg_type": "text", "content": {"text": msg}}
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        _save_state(cur)
        print("[push] 已推送飞书")
        return True
    except Exception as e:
        # 严禁把 webhook URL 打进日志;只打错误类型
        print(f"[push] 推送失败: {type(e).__name__}")
        return False
