# -*- coding: utf-8 -*-
"""网页图表截图（kimi-webbridge 驱动真实 Edge 版 · scroll+视口裁剪法）。

做法（已实测可行）：
  ① 本地 HTTP 服务喂报告页（file:// 被扩展禁止，127.0.0.1 走本地回环不经代理）；
  ② 逐个图表 scrollTo(absTop) 把图表顶对齐视口顶 + 派发 resize 触发 ECharts 渲染；
  ③ 截当前视口（不卡死）→ 用 evaluate 取 #<cid> getBoundingClientRect → Pillow 裁剪；
     图表高于视口时自动竖向拼接；
  ④ 输出文件名与 publish_xhs.py 的 glob（etf_ntm_<date>_*.png）严格一致。

用法：
  python scripts/snapshot_charts_kwb.py                  # 最新报告
  python scripts/snapshot_charts_kwb.py --date 2026-11-13
  python scripts/snapshot_charts_kwb.py --only mainChart
  python scripts/snapshot_charts_kwb.py --out D:\\xhs
"""
from __future__ import annotations

import argparse
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path

KWB_BASE = "http://127.0.0.1:10086/command"
SESSION = "etf-ntm-shot"
GROUP_TITLE = "ETF国家队日报截图"

CHART_TARGETS: list[tuple[str, str]] = [
    ("mainChart", "01_main"),
    ("etfChart", "02_etf"),
    ("structChart", "03_struct"),
    ("rotationChart", "04_rotation"),
    ("marginChart", "05_margin"),
    ("heatChart", "06_heat"),
    ("sentChart", "07_sent"),
]

REPORT_DIR = Path(os.environ.get("ETF_NTM_REPORT_DIR",
                                 os.path.expanduser("~/.etf-ntm/reports")))


# ---------- kimi-webbridge 调用（curl 子进程，--noproxy 杜绝代理）----------
def kwb(action: str, args: dict | None = None, timeout: int = 60) -> dict:
    payload = json.dumps({"action": action, "args": args or {}, "session": SESSION})
    cmd = ["curl", "-sS", "-m", str(timeout), "-X", "POST", KWB_BASE,
           "-H", "Content-Type: application/json",
           "-H", "Accept: application/json, text/event-stream",
           "--noproxy", "*", "-d", payload]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"kimi-webbridge 调用失败 (curl rc={proc.returncode}): {proc.stderr.strip()[:160]}")
    if not proc.stdout.strip():
        raise RuntimeError("kimi-webbridge 返回空响应")
    return json.loads(proc.stdout)


def kwb_ok(resp: dict) -> bool:
    return bool(resp.get("ok"))


def kwb_value(resp: dict):
    """evaluate 的返回值嵌套在 resp['data']['value']。"""
    return resp.get("data", {}).get("value")


def check_health() -> None:
    try:
        resp = kwb("list_tabs", timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"[!] 无法连接 kimi-webbridge 守护进程 ({KWB_BASE}): {e}")
        raise SystemExit(1)
    if not kwb_ok(resp):
        err = resp.get("error", {})
        print(f"[!] kimi-webbridge 未就绪: {err.get('code')} — {err.get('message')}")
        print("    请打开已安装 Kimi WebBridge 扩展的 Edge 浏览器，等待其自动重连（~30s）后重试。")
        raise SystemExit(1)
    print(f"[ok] kimi-webbridge 已连接，当前标签: {len(resp.get('tabs', []))}")


# ---------- 报告选取 ----------
def latest_report() -> Path:
    reports = [p for p in sorted(REPORT_DIR.glob("etf_ntm_*.html"))
               if p.stat().st_size > 1_000_000]
    if not reports:
        raise SystemExit(f"未找到内联版报告于 {REPORT_DIR}（CDN 旧版不适用）")
    return reports[-1]


# ---------- 本地静态服务（绕开 file:// 限制 + 代理）----------
def serve_dir(directory: Path) -> tuple[socketserver.TCPServer, int]:
    import http.server
    os.chdir(directory)
    srv = socketserver.TCPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ---------- 逐图：滚动渲染 → 视口截图 → 裁剪/拼接 ----------
def capture_chart(cid: str, out_path: Path, dpr: float) -> bool:
    """直接裁剪 #<cid> 图表容器（ECharts 已设好其宽度，避免包裹层坍塌成 0 宽）。

    把图表顶部对齐到视口顶部（scrollTo(absTop)，避免 scrollIntoView 在平滑
    滚动/嵌套滚动容器下把图表顶出视口导致负裁剪框），再按视口逐段截图拼接。
    图表高度 ≤ 视口时单段即可；> 视口时自动竖向拼接成整图。
    """
    from PIL import Image
    try:
        # ① 把图表顶对齐视口顶 + 触发 ECharts 渲染（仅首次派发 resize）
        init = kwb("evaluate", {"code": (
            f"(function(){{var e=document.getElementById('{cid}');"
            "if(!e)return JSON.stringify({err:'no-el'});"
            "document.documentElement.style.scrollBehavior='auto';"
            "var r=e.getBoundingClientRect();var absTop=window.scrollY+r.top;"
            "window.scrollTo(0,absTop);window.dispatchEvent(new Event('resize'));"
            "return JSON.stringify({absTop:Math.round(absTop)});})()"
        )}, timeout=20)
        ival = json.loads(kwb_value(init) or "{}")
        if "err" in ival:
            print(f"    [!] {cid}: 未找到图表容器 #{cid}")
            return False
        time.sleep(2.2)  # 等 ECharts 绘制

        # ② 重测，拿真实视口像素与图表 css 尺寸
        rect_resp = kwb("evaluate", {"code": (
            f"(function(){{var b=document.getElementById('{cid}').getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(b.x),y:Math.round(b.y),"
            "w:Math.round(b.width),h:Math.round(b.height)});})()"
        )}, timeout=20)
        rect = json.loads(kwb_value(rect_resp))
        if rect["h"] < 50 or rect["w"] < 50:
            print(f"    [!] {cid}: 尺寸异常 {rect}")
            return False

        # 中间视口截图写到系统 TEMP（避免 safe-delete 守卫拦截非临时目录删除）
        _tdir = Path(tempfile.gettempdir())
        _base = out_path.stem
        vp0 = _tdir / f"{_base}.vp0.png"
        sc = kwb("screenshot", {"format": "png", "path": str(vp0)}, timeout=60)
        if not kwb_ok(sc) or not vp0.exists():
            print(f"    [!] {cid} 视口截图失败: {sc.get('error')}")
            return False
        with Image.open(vp0) as _im0:
            im0 = _im0.copy()  # 复制后关闭文件句柄，避免 WinError 32
        W, H = im0.size
        rdp = float(dpr)  # 真实设备像素比（截图像素 = css 像素 × dpr）；不能用 W/rect_w（W 是视口宽）
        full_w = int(round(rect["w"] * rdp))
        full_h = int(round(rect["h"] * rdp))
        x_off = int(round(rect["x"] * rdp))
        if full_w < 50 or full_h < 50:
            print(f"    [!] {cid}: 计算尺寸过小 ({full_w}x{full_h})")
            vp0.unlink(missing_ok=True)
            return False

        out_im = Image.new("RGB", (full_w, full_h), (255, 255, 255))
        copied = 0
        seg = 0
        while copied < full_h:
            # 把图表第 copied 像素处滚到视口顶
            kwb("evaluate", {"code": (
                f"(function(){{var e=document.getElementById('{cid}');"
                "var r=e.getBoundingClientRect();"
                f"window.scrollTo(0,window.scrollY+r.top+{copied/rdp:.2f});"
                "window.dispatchEvent(new Event('resize'));}})()"
            )}, timeout=20)
            time.sleep(1.4)
            vp = _tdir / f"{_base}.vp{seg}.png"
            kwb("screenshot", {"format": "png", "path": str(vp)}, timeout=60)
            r2 = json.loads(kwb_value(kwb("evaluate", {"code": (
                f"(function(){{var b=document.getElementById('{cid}').getBoundingClientRect();"
                "return JSON.stringify({y:Math.round(b.y)});}})()"
            )}, timeout=20)) or '{"y":0}')
            y2 = max(0, int(round(r2.get("y", 0) * rdp)))
            avail = H - y2
            need = full_h - copied
            take = min(avail, need)
            if take <= 0:
                vp.unlink(missing_ok=True)
                break
            with Image.open(vp) as _im:
                seg_img = _im.crop((x_off, y2, x_off + full_w, y2 + take))
            out_im.paste(seg_img, (0, copied))
            vp.unlink(missing_ok=True)
            copied += take
            seg += 1
        vp0.unlink(missing_ok=True)
        out_im.save(out_path)
        print(f"    [ok] {cid} -> {out_path.name} ({out_path.stat().st_size//1024}KB, {seg}段拼接)")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    [!] {cid} 异常: {e}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="报告图表截图（kimi-webbridge 驱动真实 Edge）")
    ap.add_argument("--date", help="报告日期 YYYY-MM-DD（默认最新）")
    ap.add_argument("--only", help="只截指定容器（如 mainChart）")
    ap.add_argument("--out", help="输出目录（默认 reports/charts/）")
    args = ap.parse_args()

    check_health()

    if args.date:
        html = REPORT_DIR / f"etf_ntm_{args.date}.html"
        if not html.exists():
            raise SystemExit(f"报告不存在: {html}")
    else:
        html = latest_report()

    out_dir = Path(args.out) if args.out else html.parent / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 直接喂原始报告（无需注入包裹层；裁剪目标即 #<cid> 图表容器）
    tmp = Path(tempfile.mkdtemp(prefix="etf-shot-"))
    marked = tmp / html.name
    marked.write_text(html.read_text(encoding="utf-8"), encoding="utf-8")
    srv, port = serve_dir(tmp)
    url = f"http://127.0.0.1:{port}/{marked.name}"

    try:
        print(f"[1/3] 导航真实 Edge 到报告页: {url}")
        nav = kwb("navigate", {"url": url, "newTab": True, "group_title": GROUP_TITLE}, timeout=60)
        if not kwb_ok(nav):
            raise SystemExit(f"导航失败: {nav.get('error')}")
        print(f"      标签 {nav.get('tabId')} 已打开")

        print("[2/3] 等待主图 ECharts 渲染 ...")
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                r = kwb("evaluate", {"code": (
                    "JSON.stringify({rs:document.readyState,m:(function(){"
                    "var c=document.getElementById('mainChart').querySelector('canvas');"
                    "return c?Math.round(c.getBoundingClientRect().height):0;})()})"
                )}, timeout=20)
                v = json.loads(kwb_value(r) or "{}")
                if v.get("m") and v["m"] > 50:
                    print(f"    主图已渲染（高 {v['m']}px, readyState={v.get('rs')}）")
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        else:
            print("    [!] 主图渲染等待超时，仍尝试截图")

        dpr_resp = kwb("evaluate", {"code": "JSON.stringify({dpr:window.devicePixelRatio})"}, timeout=20)
        dpr = float(json.loads(kwb_value(dpr_resp) or "{}").get("dpr", 1) or 1)
        print(f"    设备像素比 dpr={dpr}")

        print("[3/3] 逐图滚动渲染+视口裁剪 ...")
        ok = 0
        for cid, name in CHART_TARGETS:
            if args.only and cid != args.only:
                continue
            if capture_chart(cid, out_dir / f"{html.stem}_{name}.png", dpr):
                ok += 1
        print(f"\n完成：{ok}/{len(CHART_TARGETS)} 张图表 -> {out_dir}")
    finally:
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        for f in tmp.glob("*"):
            try:
                f.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            tmp.rmdir()
        except Exception:  # noqa: BLE001
            pass
        try:
            kwb("close_session", timeout=15)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
