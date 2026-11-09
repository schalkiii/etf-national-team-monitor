# -*- coding: utf-8 -*-
"""网页图表截图：报告 HTML → 各图表 PNG（小红书/公众号发布素材）。

用途：
  每日报告更新后，用 Edge 原生 headless CLI 渲染报告页，按图表容器
  裁剪输出 PNG。产出的图片供 xiaohongshu-mcp（publish_content 本地
  图片路径）与 wenyan-mcp（公众号草稿配图）消费。

技术路线（2026-11-09 实测定型，三条坑）：
  ① Playwright（Chromium/Edge channel、headless/有头）在本机 launch
     握手一律挂起 → 弃用，改 Edge 原生 headless CLI。
  ② Edge headless 的 stdout 在「python 子进程」环境下被吞（bash 工具
     直调正常）→ dump-dom 拿 DOM 的路不可走，一切数据走文件。
  ③ --screenshot 写文件在 python 子进程下完全正常（已验证 442KB）。
  坐标测量方案：给每个图表容器注入独特颜色 outline（不占布局空间），
  截全页后按颜色扫描像素定位边框矩形 → 精确裁剪。

用法：
  python scripts/snapshot_charts.py                     # 最新报告 → reports/charts/
  python scripts/snapshot_charts.py --date 2026-11-05   # 指定日期
  python scripts/snapshot_charts.py --only mainChart    # 只截主图
  python scripts/snapshot_charts.py --out D:\\xhs       # 指定输出目录

前置条件：
  - Edge（默认安装路径，x86 与 x64 都探测）
  - 报告由新代码生成（内联 echarts；CDN 版在无网 headless 下图不渲染）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import REPORT_DIR  # noqa: E402

# 图表容器 → (输出文件名, marker 颜色 RGB)
# 颜色为高饱和荧光色，报告浅底深字配色下不存在，扫描零误报
# 截图含标题：marker 打在「标题+按钮+图表」的包裹容器上（见 inject_markers），
# 结构/轮动为双列内嵌容器，marker 打在外层 div（含 h3 小标题）。
CHART_TARGETS: list[tuple[str, str, tuple[int, int, int]]] = [
    ("mainChart", "01_main", (255, 0, 254)),      # 主图：国家队估算+份额+指数
    ("etfChart", "02_etf", (0, 255, 254)),        # 单基金详情
    ("structChart", "03_struct", (255, 254, 0)),  # 份额结构对比
    ("rotationChart", "04_rotation", (255, 0, 127)),  # N日轮动
    ("marginChart", "05_margin", (0, 127, 255)),  # 融资杠杆
    ("heatChart", "06_heat", (127, 255, 0)),      # 信号热力图
    ("sentChart", "07_sent", (127, 0, 255)),      # 市场热度
]

# 截图时把 marker 打到哪个包裹元素（含标题）：
#   默认向上找 section 的 sec-head 之后的整个内容块；对双列小图
#   (struct/rotation) 打在各自 grid 列 div（含 h3 小标题）。
# CSS 选择器：从图表容器出发,closest() 向上找包裹层。
WRAPPER_SELECTOR: dict[str, str] = {
    "structChart": "#g-structure .grid2 > div:first-child",
    "rotationChart": "#g-structure .grid2 > div:last-child",
}

VIEWPORT_W = 740        # 与报告 CSS 容器宽度匹配（body max-width 703 + 边距）
PAGE_H_DEFAULT = 12000  # 默认页高（实测 ~7400,分档递增防截断;Edge 上限约 16384）
SCALE = 2               # force-device-scale-factor
MARKER_W = 6            # outline 宽度(px)，裁剪时内缩


def find_edge() -> str:
    """定位 msedge.exe（x86/x64 默认路径）。"""
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(c).exists():
            return c
    raise SystemExit("未找到 msedge.exe，请确认 Edge 已安装")


def latest_report() -> Path:
    """最新报告 HTML（etf_ntm_YYYY-MM-DD.html）。

    只认内联 echarts 版（>1MB）；CDN 版（~500KB）是 R10 拆分前的
    旧产物，headless 无网环境下图不渲染，跳过。
    """
    reports = [p for p in sorted(REPORT_DIR.glob("etf_ntm_*.html"))
               if p.stat().st_size > 1_000_000]
    if not reports:
        raise SystemExit(f"未找到内联版报告于 {REPORT_DIR}（CDN 旧版不适用）")
    return reports[-1]


def inject_markers(html: str) -> str:
    """给每个截图目标注入独特颜色 outline CSS（outline 不占布局空间）。

    marker 打在包裹层（含标题/按钮）而非图表容器本身：
      - 默认：图表所在 section 内从 sec-head 到图表容器的整块
        （实现：给 section 加 outline 会带上 footnote,太长;改为
        给「sec-head 的父 section 内、图表的直接父 div」打 marker,
        该 div 已含 sec-head + 按钮行 + 图表容器）。
      - struct/rotation：双列 grid 的列 div（含 h3 小标题）。
    """
    # 各图表的 marker 挂载元素选择器：
    # main/etf/margin/heat/sent 图表容器的直接父 div 恰好含 sec-head+按钮+图
    # （section 结构: sec-head div + 按钮 div + <div id=xxChart> + footnote p,
    #   图表直接父级是 section,故需按 section 打 marker 再在裁剪时剔除 footnote? )
    # 实测结构：section > (div.sec-head, div(按钮), div#chart, p.footnote)
    # → marker 打在 section 上会把 footnote 也截进去。因此对这五个图,
    #   用 JS 在加载时把 sec-head+按钮+chart 包进一个新 div 再打 marker;
    #   struct/rotation 直接打在 grid 列 div 上（无 footnote 混入）。
    rules = []
    for cid, _, color in CHART_TARGETS:
        sel = WRAPPER_SELECTOR.get(cid)
        if sel:
            rules.append(
                f"{sel}{{outline:{MARKER_W}px solid rgb({color[0]},{color[1]},{color[2]});}}")
        else:
            # 通用：注入 JS 包裹脚本（见 WRAP_JS）后,包div获得 id=wrap_<cid>
            rules.append(
                f"#wrap_{cid}{{outline:{MARKER_W}px solid rgb({color[0]},{color[1]},{color[2]});}}")
    style = "<style>" + "\n".join(rules) + "</style>"
    return html.replace("</head>", style + WRAP_JS + "</head>", 1)


# 通用图(main/etf/margin/heat/sent)：运行时把 sec-head、按钮行、图表容器
# 包进 #wrap_<cid>（不含 footnote p），结构小图无需包裹。
WRAP_JS = """<script>
document.addEventListener('DOMContentLoaded', function(){
  // 注意: 本脚本注入在 <head> 内,同步执行时 body 尚未解析,
  // getElementById 全 null —— 必须挂 DOMContentLoaded
  ['mainChart','etfChart','marginChart','heatChart','sentChart'].forEach(function(cid){
    var chart = document.getElementById(cid);
    if (!chart) return;
    var section = chart.closest('section');
    if (!section) return;
    var wrap = document.createElement('div');
    wrap.id = 'wrap_' + cid;
    var anchor = section.querySelector('.sec-head');
    if (!anchor) { return; }
    section.insertBefore(wrap, anchor);
    var node = anchor;
    while (node && node !== chart) {
      var next = node.nextSibling;
      wrap.appendChild(node);
      node = next;
    }
    wrap.appendChild(chart);
  });
});
</script>"""


def edge_screenshot(edge: str, html_path: Path, out_png: Path, page_h: int) -> None:
    """Edge CLI 全页截图到文件（python 子进程下唯一验证可行的输出通道）。"""
    r = subprocess.run(
        [edge, "--headless", "--disable-gpu",
         f"--screenshot={out_png}",
         f"--window-size={VIEWPORT_W},{page_h}",
         "--force-device-scale-factor=2",
         "--virtual-time-budget=12000",
         html_path.resolve().as_uri()],
        capture_output=True, timeout=120)
    if not out_png.exists() or out_png.stat().st_size < 10000:
        raise SystemExit(f"截图失败: {r.stderr.decode('utf-8', errors='replace')[:300]}")


def scan_markers(full_png: Path) -> dict[str, dict]:
    """按颜色扫描 marker 边框矩形。返回 {容器名: {x,y,w,h}}（CSS px）。"""
    from PIL import Image

    img = Image.open(full_png).convert("RGB")
    px = img.load()
    w_css, h_css = img.width // SCALE, img.height // SCALE

    results: dict[str, dict] = {}
    for cid, _, color in CHART_TARGETS:
        min_x = min_y = 10**9
        max_x = max_y = -1
        # 隔行扫描（marker 6px*2=12px 宽,隔 4 行不漏）
        for y in range(0, img.height, 4):
            for x in range(0, img.width, 4):
                p = px[x, y]
                if (abs(p[0] - color[0]) <= 12 and abs(p[1] - color[1]) <= 12
                        and abs(p[2] - color[2]) <= 12):
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
        if max_x < 0:
            continue
        # 像素坐标 → CSS 坐标，内缩 marker 宽度
        x0 = min_x // SCALE + MARKER_W
        y0 = min_y // SCALE + MARKER_W
        x1 = min((max_x + 1) // SCALE - MARKER_W, w_css)
        y1 = min((max_y + 1) // SCALE - MARKER_W, h_css)
        if x1 - x0 < 50 or y1 - y0 < 50:
            continue  # 标记不完整(页面超长被截断)
        results[cid] = {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
    return results


def crop_charts(full_png: Path, coords: dict[str, dict], html_stem: str,
                out_dir: Path, only: str | None = None) -> list[Path]:
    """从全页图裁剪各图表（coords 已是 CSS px，图是 2x）。"""
    from PIL import Image

    img = Image.open(full_png)
    outputs: list[Path] = []
    for cid, name, _ in CHART_TARGETS:
        if only and cid != only:
            continue
        c = coords.get(cid)
        if not c:
            print(f"[skip] {cid}: marker 未找到或页面截断")
            continue
        box = (c["x"] * SCALE, c["y"] * SCALE,
               (c["x"] + c["w"]) * SCALE, (c["y"] + c["h"]) * SCALE)
        box = (max(0, box[0]), max(0, box[1]),
               min(img.width, box[2]), min(img.height, box[3]))
        out_file = out_dir / f"{html_stem}_{name}.png"
        img.crop(box).save(out_file)
        outputs.append(out_file)
        print(f"[ok] {cid} {c['w']}x{c['h']} -> {out_file.name}")
    return outputs


def main() -> None:
    ap = argparse.ArgumentParser(description="报告图表截图（发布素材）")
    ap.add_argument("--date", help="报告日期 YYYY-MM-DD（默认最新）")
    ap.add_argument("--only", help="只截指定容器（如 mainChart）")
    ap.add_argument("--out", help="输出目录（默认 reports/charts/）")
    args = ap.parse_args()

    if args.date:
        html = REPORT_DIR / f"etf_ntm_{args.date}.html"
        if not html.exists():
            raise SystemExit(f"报告不存在: {html}")
    else:
        html = latest_report()

    out_dir = Path(args.out) if args.out else html.parent / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    edge = find_edge()

    # 带 marker 的临时页（报告只读不动）
    marked = out_dir / f"_marked_{html.stem}.html"
    marked.write_text(inject_markers(html.read_text(encoding="utf-8")),
                      encoding="utf-8")

    print(f"[1/2] Edge 全页截图（marker 定位版,{VIEWPORT_W}x{PAGE_H_DEFAULT} @2x）...")
    marked_png = out_dir / "_marked_full.png"
    edge_screenshot(edge, marked, marked_png, PAGE_H_DEFAULT)

    print(f"[2/2] 扫描 marker 坐标并裁剪 ...")
    coords = scan_markers(marked_png)
    n_charts = len(coords)
    print(f"      定位到 {n_charts}/{len(CHART_TARGETS)} 个图表")
    files = crop_charts(marked_png, coords, html.stem, out_dir, args.only)

    # 干净版全页图（无 marker，长图发布场景）
    clean_png = out_dir / "_fullpage.png"
    edge_screenshot(edge, html, clean_png, PAGE_H_DEFAULT)

    # 清理临时文件
    marked.unlink(missing_ok=True)
    marked_png.unlink(missing_ok=True)

    print(f"\n完成：{len(files)} 张图表图 + 全页图 -> {out_dir}")


if __name__ == "__main__":
    main()
