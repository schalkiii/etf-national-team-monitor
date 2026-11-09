# -*- coding: utf-8 -*-
"""每日 ETF 报告 → 小红书图文发布。

流程：
  ① scripts/snapshot_charts.py 产出图表 PNG（前置,已单独运行）
  ② 解析最新报告 CD,组装小红书文案（标题≤20字,正文≤1000字）
  ③ 调 xiaohongshu-mcp REST API (localhost:18060) 发布图文
     - 未登录时引导先跑 login 工具扫码
     - 默认仅自己可见（试发验证）,--public 正式发布

用法：
  python scripts/publish_xhs.py                    # 试发（仅自己可见）
  python scripts/publish_xhs.py --public           # 正式公开发布
  python scripts/publish_xhs.py --title "..." --body "..."   # 自定义文案
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import REPORT_DIR  # noqa: E402

XHS_API = "http://127.0.0.1:18060/mcp"   # MCP JSON-RPC 端点（本机代理需 --noproxy）
TAGS = ["ETF", "国家队", "A股", "量化", "理财"]


def _mcp_call(name: str, arguments: dict | None = None, timeout: int = 30) -> dict:
    """调 xiaohongshu-mcp 的 MCP 工具（JSON-RPC over Streamable HTTP）。"""
    # ① urllib 默认走 HTTP_PROXY 环境变量,本地服务必须绕过
    # ② Streamable HTTP 要求 Accept 同时含 json 与 text/event-stream
    handlers = [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": arguments or {}}}
    req = urllib.request.Request(
        XHS_API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST")
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def latest_report() -> Path:
    reports = [p for p in sorted(REPORT_DIR.glob("etf_ntm_*.html"))
               if p.stat().st_size > 1_000_000]
    if not reports:
        raise SystemExit("未找到内联版报告")
    return reports[-1]


def extract_cd(html_path: Path) -> dict:
    """从报告 HTML 解析 var CD = {...}。"""
    html = html_path.read_text(encoding="utf-8")
    m = re.search(r"var CD = (\{.*?\});\n", html, re.S)
    if not m:
        raise SystemExit("报告里未找到 CD 数据块")
    return json.loads(m.group(1))


def build_content(cd: dict, date: str) -> tuple[str, str]:
    """组装小红书文案（标题≤20字,正文≤1000字）。"""
    nt = cd.get("nt", {})
    cum = nt.get("cum", [0])[-1] or 0
    n_buy = nt.get("n_buy", 0)
    n_sell = nt.get("n_sell", 0)
    theta = nt.get("theta", 1.0)
    dates = nt.get("dates", [])
    last_active = nt.get("last_active") or (dates[-1] if dates else date)

    title = f"国家队ETF追踪 {date[5:].replace('-', '/')}"  # ≤20字

    body_lines = [
        f"📊 国家队 ETF 份额监控日报 · {date}",
        "",
        f"· 窗口累计估算净{'增持' if cum >= 0 else '减持'}：{abs(cum):,.0f} 亿",
        f"· 活跃交易日：{n_buy + n_sell} 天（买入 {n_buy} / 减持 {n_sell}）",
        f"· 最近活跃日：{last_active}",
        f"· 归因系数 θ = {theta}（核心宽基锚点标定）",
        "",
        "图1：国家队估算净买入 + 核心宽基合计份额 + 上证指数",
        "图2：单基金详情（K线 + 份额日增量）",
        "图3-4：份额结构与 N 日轮动",
        "图5：两融余额（杠杆出清背景）",
        "图6-7：信号热力图 + 市场热度",
        "",
        "口径说明：活跃日 = 总净申购量级进入历史 P90 分位且处于护盘语境"
        "（回撤放量或强护盘），静默日估算为 0，不冒充。数据源：交易所官方份额披露。",
        "",
        "#ETF #国家队 #A股 #量化 #理财",
    ]
    return title, "\n".join(body_lines)


def check_login() -> bool:
    try:
        result = _mcp_call("check_login_status")
        for item in result.get("result", {}).get("content", []):
            text = item.get("text", "")
            if "未登录" in text or "❌" in text:
                return False
        return True
    except Exception:
        return False


def publish(title: str, body: str, images: list[Path], public: bool) -> dict:
    """调 xiaohongshu-mcp MCP 工具发布图文。"""
    arguments = {
        "title": title,
        "content": body,
        "images": [str(p) for p in images],
        "tags": TAGS,
        "visibility": "public" if public else "private",
    }
    return _mcp_call("publish_content", arguments, timeout=180)


def main() -> None:
    ap = argparse.ArgumentParser(description="报告发布到小红书")
    ap.add_argument("--public", action="store_true", help="公开发布（默认仅自己可见）")
    ap.add_argument("--title", help="自定义标题")
    ap.add_argument("--body", help="自定义正文")
    ap.add_argument("--charts", help="图表目录（默认最新报告的 charts/）")
    args = ap.parse_args()

    report = latest_report()
    date = report.stem.replace("etf_ntm_", "")
    charts_dir = Path(args.charts) if args.charts else report.parent / "charts"
    images = sorted(charts_dir.glob(f"{report.stem}_*.png"))
    if not images:
        raise SystemExit(f"未找到图表 PNG,先运行: python scripts/snapshot_charts.py")

    if args.title and args.body:
        title, body = args.title, args.body
    else:
        cd = extract_cd(report)
        title, body = build_content(cd, date)

    # 长度合规校验（平台硬约束）
    if len(title) > 20:
        title = title[:20]
    if len(body) > 1000:
        body = body[:997] + "..."

    print(f"标题({len(title)}字): {title}")
    print(f"正文({len(body)}字) 图片({len(images)}张)")
    print(f"可见性: {'公开' if args.public else '仅自己'}")

    if not check_login():
        print("\n[!] 未登录。先运行登录工具扫码:")
        print(r"    C:\Users\qi.shao\.etf-ntm\xhs-mcp\xiaohongshu-login-windows-amd64.exe")
        raise SystemExit(1)

    result = publish(title, body, images, args.public)
    print(f"\n发布结果: {json.dumps(result, ensure_ascii=False)[:300]}")


if __name__ == "__main__":
    main()
