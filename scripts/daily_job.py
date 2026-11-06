# -*- coding: utf-8 -*-
"""每日盘后自动化入口（供 WorkBuddy automation / Windows 计划任务调用）。
调用链：增量采集 → 信号 → 报告 → 飞书推送（方向翻转才推）。
退出码: 0=正常 1=采集失败 2=全量BLOCKED
注意: Windows 计划任务调用时使用绝对路径; webhook 走环境变量。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from store.db import connect, init_db  # noqa: E402


def main() -> int:
    con = connect()
    init_db(con)
    from fetch.collector import daily_incremental
    from core.report import run_daily
    inc = daily_incremental(con)
    result = run_daily(con, push=True)
    print(f"incremental: {inc}")
    print(f"daily: { {k: v for k, v in result.items() if k != 'report'} }")
    print(f"report: {result.get('report')}")
    if result.get("blocked_all"):
        return 2
    if inc.get("share_rows", 0) == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
