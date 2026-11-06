# -*- coding: utf-8 -*-
"""CLI 入口：采集 / 重建 / 分析 / 报告 / 回测 / 校准 / 推送。
用法：
  python cli.py rebuild            # 一键重建全量数据
  python cli.py daily              # 每日增量采集+信号+报告
  python cli.py report [--date YYYY-MM-DD]
  python cli.py status             # 数据库状态
  python cli.py backtest
  python cli.py calibrate
  python cli.py push [--force]
退出码：0=正常 1=采集失败 2=全量BLOCKED 3=参数错误
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import DATA_DIR, DB_PATH, ETF_POOL  # noqa: E402
from store.db import connect, init_db  # noqa: E402


def cmd_status(con) -> None:
    n_daily = con.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0]
    n_shares = con.execute("SELECT COUNT(*) FROM etf_daily WHERE shares IS NOT NULL").fetchone()[0]
    n_idx = con.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    n_cal = con.execute("SELECT COUNT(*) FROM calendar WHERE is_trading=1").fetchone()[0]
    n_sig = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    last = con.execute("SELECT date FROM calendar WHERE is_trading=1 ORDER BY date DESC LIMIT 1").fetchone()
    print(f"DB: {DB_PATH}")
    print(f"交易日历: {n_cal} (最新: {last['date'] if last else '-'})")
    print(f"etf_daily: {n_daily} 行 (含份额 {n_shares})")
    print(f"index_daily: {n_idx} 行")
    print(f"signals: {n_sig} 条")
    for code in list(ETF_POOL)[:20]:
        r = con.execute(
            "SELECT MIN(date) a, MAX(date) b, COUNT(*) n FROM etf_daily WHERE code=? AND shares IS NOT NULL",
            (code,),
        ).fetchone()
        if r and r["n"]:
            print(f"  {code} {ETF_POOL[code]['name']}: 份额 {r['a']} ~ {r['b']} ({r['n']} 日)")


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 国家队资金监控")
    parser.add_argument("command", choices=["rebuild", "daily", "report", "status", "backtest", "calibrate", "push", "events"])
    parser.add_argument("--date", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    init_db(con)

    if args.command == "status":
        cmd_status(con)
        return 0

    if args.command == "rebuild":
        from fetch.collector import rebuild_all
        stats = rebuild_all(con)
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        return 0 if stats["shares"]["rows"] > 0 else 1

    if args.command == "daily":
        from fetch.collector import daily_incremental
        from core.report import run_daily
        inc = daily_incremental(con)
        result = run_daily(con, push=False)
        print(json.dumps({"incremental": inc, "daily": result}, ensure_ascii=False, indent=2, default=str))
        if result.get("blocked_all"):
            return 2
        return 0 if inc["share_rows"] >= 0 else 1

    if args.command == "report":
        from core.report import generate_report
        out = generate_report(con, args.date)
        print(out)
        return 0

    if args.command == "backtest":
        from core.backtest import run_backtest
        stats = run_backtest(con)
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "calibrate":
        from core.calibration import run_calibration
        stats = run_calibration(con)
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "events":
        from core.backtest import run_event_study
        stats = run_event_study(con)
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "push":
        from core.push import push_daily
        ok = push_daily(con, force=args.force)
        return 0 if ok else 1

    parser.print_help()
    return 3


if __name__ == "__main__":
    sys.exit(main())
