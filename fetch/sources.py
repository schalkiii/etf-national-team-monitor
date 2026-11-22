# -*- coding: utf-8 -*-
"""数据源层：统一重试 + 优雅降级（网络失败/非交易日不抛异常到上层）。
fetch 层只做请求与解析，严禁业务逻辑。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from core.config import HTTP_RETRIES, HTTP_TIMEOUT, RETRY_BACKOFF, TENCENT_KLINE_URL


def _warn(msg: str) -> None:
    """诊断可见化：akshare 源失败不再静默吞异常（CI 日志此前无任何线索）。"""
    print(f"[sources] WARNING: {msg}", file=sys.stderr, flush=True)


def _retry_call(fn, *args, **kwargs):
    """统一重试包装：网络抖动/限流时重试 HTTP_RETRIES 次,全败返回 None。
    akshare 上游（sse/szse/东财）无内置重试,单次失败即整批数据丢失
    （2026-11-20/21 CI 实测踩坑）。"""
    last_exc = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 数据源异常统一降级
            last_exc = exc
            if attempt < HTTP_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    _warn(f"{getattr(fn, '__name__', fn)} 失败 {HTTP_RETRIES} 次: "
          f"{type(last_exc).__name__}: {str(last_exc)[:200]}")
    return None


def http_get_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict | None:
    """GET + 重试退避。全败返回 None（优雅降级，不抛异常）。"""
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception:
            if attempt == HTTP_RETRIES:
                return None
            time.sleep(RETRY_BACKOFF * attempt)
    return None


# ----------------------------------------------------------------
# 腾讯 K 线（ETF 与指数共用）
# ----------------------------------------------------------------
TENCENT_KLINE_RANGE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/"
                           "get?param={symbol},day,{start},{end},{count},qfq")


def _parse_kline_rows(rows: list) -> list[tuple]:
    out = []
    for r in rows:
        try:
            date = r[0]
            o, c, h, low = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            vol = float(r[5])
            # amount 缺失即 None（消费方 _market_amount 已容忍 null）;
            # 不做均价×量估算——估算值无标记静默入库,违反口径诚实纪律
            amount = float(r[6]) if len(r) > 6 and r[6] not in ("", None) else None
            out.append((date, o, c, h, low, vol, amount))
        except (ValueError, IndexError):
            continue
    return out


def tencent_kline(symbol: str, count: int) -> list[tuple[str, float, float, float, float, float, float]]:
    """近 count 日 K 线（单次请求,腾讯单次上限约 641 条）。失败返回 []。"""
    data = http_get_json(TENCENT_KLINE_URL.format(symbol=symbol, count=count))
    if not data:
        return []
    node = (data.get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday") or node.get("day") or []
    return _parse_kline_rows(rows)


def tencent_kline_full(symbol: str, start: str, end: str) -> list[tuple[str, float, float, float, float, float, float]]:
    """区间全量 K 线（分页拉取,学 A-Resonance KLINE_URL_RANGE 模式）。
    start/end: 'YYYY-MM-DD'。从 end 往前按 600 天窗口翻页直到覆盖 start。"""
    from datetime import datetime, timedelta
    all_rows: dict[str, tuple] = {}
    end_d = datetime.strptime(end, "%Y-%m-%d")
    start_d = datetime.strptime(start, "%Y-%m-%d")
    cur_end = end_d
    for _ in range(20):  # 最多 20 页保险
        cur_start = cur_end - timedelta(days=620)
        seg_start = max(cur_start, start_d).strftime("%Y-%m-%d")
        url = TENCENT_KLINE_RANGE_URL.format(symbol=symbol, start=seg_start,
                                             end=cur_end.strftime("%Y-%m-%d"), count=620)
        data = http_get_json(url)
        if not data:
            break
        node = (data.get("data") or {}).get(symbol) or {}
        rows = node.get("qfqday") or node.get("day") or []
        if not rows:
            break
        for row in _parse_kline_rows(rows):
            all_rows[row[0]] = row
        first_date = rows[0][0]
        if first_date <= start:
            break
        cur_end = datetime.strptime(first_date, "%Y-%m-%d") - timedelta(days=1)
        if cur_end < start_d:
            break
    return [all_rows[d] for d in sorted(all_rows)]


# ----------------------------------------------------------------
# akshare 份额（导入延迟,失败降级为空）
# ----------------------------------------------------------------
def _akshare():
    import akshare as ak
    return ak


def sse_shares(date: str) -> dict[str, float]:
    """上交所某交易日全部 ETF 份额。date: 'YYYYMMDD'。返回 {code: shares(份)}。失败返回 {}。"""
    df = _retry_call(lambda d: _akshare().fund_etf_scale_sse(date=d), date)
    if df is None or df.empty:
        if df is not None:
            _warn(f"sse_shares({date}) 返回空 DataFrame")
        return {}
    out = {}
    for _, row in df.iterrows():
        code = str(row["基金代码"]).zfill(6)
        try:
            out[code] = float(row["基金份额"])
        except (TypeError, ValueError):
            continue
    return out


def szse_shares(start: str, end: str) -> dict[str, dict[str, float]]:
    """深交所区间份额。start/end: 'YYYYMMDD'。返回 {code: {date: shares}}。失败返回 {}。"""
    df = _retry_call(lambda s, e: _akshare().fund_scale_daily_szse(start_date=s, end_date=e), start, end)
    if df is None or df.empty:
        if df is not None:
            _warn(f"szse_shares({start}-{end}) 返回空 DataFrame")
        return {}
    out: dict[str, dict[str, float]] = {}
    for _, row in df.iterrows():
        code = str(row["基金代码"]).zfill(6)
        date = str(row["日期"])
        try:
            out.setdefault(code, {})[date] = float(row["基金份额"])
        except (TypeError, ValueError):
            continue
    return out


# 注：原「降级链执行器 run_chain」（学 etf-tracker source_chain）从未接线,
# 已删除（AUDIT-REPORT A6）。多源降级需求出现时按 http_get_json 重试模式新建。
