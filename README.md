# ETF 国家队资金监控 · 集百家之长版

本地化、全自动、证据链完整的「国家队 ETF 资金动向」监控与信号系统。
集五个现有项目之长：A-Resonance（五灯共振框架+份额复权+K线多面板+融资余额数据源）、national-team-position（份额而非规模+结构轮动+双轴图）、
etf-three-factor v7（akshare 官方数据源路线）、etf-tracker（口径分级/只降不升/回测标定纪律）、
quantdata.uk（背景指标+描述统计克制），并补上所有项目缺失的**季度持仓锚点校准**。

## 参考项目索引（XERJ）

三个参考项目已克隆到 `D:\workspace\` 并用 XERJ 建立代码索引（节点 `http://127.0.0.1:9200`）：

```bash
export PATH="$PATH:/c/Users/qi.shao/AppData/Local/Programs/xerj"
export NO_PROXY="127.0.0.1,localhost"
xerj search "双轴 份额 指数" --url http://127.0.0.1:9200   # 检索参考实现
```

- `D:\workspace\A-Resonance`（prefix: ax）
- `D:\workspace\national-team-position`（prefix: national-team-position）
- `D:\workspace\etf-three-factor`（prefix: etf-three-factor）
- 本项目（prefix: etf-ntm）

数据回溯深度：**2020-01 至今**（约 6.8 年,覆盖 2020 疫情护盘 / 2024-02 微盘流动性危机 / 2025 汇金万亿建仓 / 2026-07 官宣护盘全部周期）。

## 快速开始

```bash
pip install akshare pandas
python cli.py rebuild     # 一键重建全量数据（日历→K线→份额→复权→融资余额）
python cli.py report      # 生成最新日报 HTML（双轴叠加+K线多面板+结构轮动）
python cli.py calibrate   # θ 锚点校准
python cli.py backtest    # 信号回测（forward return）
python cli.py events      # 事件研究
python cli.py daily       # 每日增量采集+信号+报告（推送可选）
python scripts/daily_job.py   # 每日自动化入口（含飞书推送分支）
```

## 数据源

| 数据 | 接口 | 更新周期 / 实际可得时间 |
|---|---|---|
| 沪市 ETF 份额 | akshare `fund_etf_scale_sse(date)` | 日度,交易所盘后披露,当日傍晚（约 17:00–20:00）可查 T 日;可回溯任意历史日 |
| 深市 ETF 份额 | akshare `fund_scale_daily_szse(start,end)` | 日度,同上;区间上限约185自然日,超限自动分段 |
| K 线/成交额 | 腾讯 `web.ifzq.gtimg.cn` | 日度,收盘后当日数据即出 |
| 融资余额 | 交易所两融日度披露（akshare） | 日度但 **T+1 披露**（沪市）/按月批次（深市）;`margin_daily` 表,2022-11 起 |
| 季报锚点 | 基金定期报告前十大持有人 | **季度**,手工录入 `core/anchor_data.py` 的 ANCHORS 后跑 `cli.py rebuild`（`seed_anchors` 幂等 upsert）；或运行时 `calibration.upsert_anchor` |

### 采集频率结论（2026-11-13 定稿）

全部核心数据源均为**日频**，无小时级/分钟级数据；信号粒度（5日合计、MAD 异常、五灯共振）也是日频设计。因此：

- **每个交易日跑一次，19:05 是最优档**：晚于份额披露时间（傍晚可查），当日数据命中率最高。
- **无需盘中多次跑**：盘中只会拿到不完整数据，无增益。
- **两融 T+1 / 晚披露兜底**：`daily_incremental` 自带近 5 自然日回补，次日运行自动追赶，无需额外补跑任务。
- **周末/节假日**：日历由上证指数日线推导，非交易日空转无害但浪费，调度可只排周一~周五。
- **季报期（4/8/10 月末）**：锚点是唯一不自动的数据源，季报披露后需手工更新 `core/anchor_data.py` 并 `cli.py rebuild`。

## 核心设计

- **三层信号**：描述层（5日合计±标签）→ MAD 稳健 3σ 异常 → 分位数五灯共振（阈值从数据学出）
- **算法分层**：国家队估算为纯函数 `core/nt_estimate.py`（活跃日判定/θ_active/网格对齐，74+ 测试直接断言）；报告数据在 `core/report.py`；HTML 模板在 `core/report_html.py`
- **报告图表 v2**：
  - 核心宽基合计份额 vs 上证指数**双轴叠加**（多周期切换 + dataZoom）
  - 单 ETF 详情：K 线 + 指数对照（右轴）+ **份额日增量柱**（复权后,拆分日已处理）
  - 份额落点分析 + 结构轮动 60 日变化（区分真减仓 vs 换仓）
  - 融资余额走势（L5 灯数据源,沪市区间接口 2022-11 起全覆盖）
  - 指数距年线距离（quantdata.uk 背景三件套）
- **口径纪律**：真实份额锚定优先，估算并列输出永不冒充；BLOCKED 严禁降级 NEUTRAL；置信度只降不升
- **锚点校准（本项目灵魂）**：θ = Δ国家队持仓/Δ总份额，θ>1 语义=国家队增持超总增量（其他净赎回）
- **份额复权**：拆分/折算自动探测（单日跳变>25%且价格同向跳变），全部历史拆分已正确处理
- **原子写入**：临时文件+os.replace+fsync，并发读不会读到半截文件

## 回测实证（250 交易日 / 4500 信号，2026-11-06）

> 口径说明：回测基于三层信号（direction/confidence，`signal_engine`），该引擎自
> 2026-11-06 回测后未改动，结论仍适用。2026-11-09 活跃日判定加语境约束只影响
> 报告「国家队估算」（图 1）口径，与回测信号无关。

- **机会共振信号后 20 日平均 -3.3%，胜率 38%** → 护盘期买入=接下落的刀，左侧布局需分批
- **SHORT|中 20 日胜率仅 31%** → 空头封顶纪律有数据支撑（etf-tracker 结论复现，
  已实现于 `signal_engine.apply_confidence_rules` 的 direction 参数）
- **LONG|高（24例）20 日 -20.2%** → 高确信多头信号集中在恐慌顶点，是唯一要避免的信号档
- 事件研究：7/13 百股跌停后核心宽基份额 +38%，官方护盘足迹清晰可测

## 部署

每日自动化：WorkBuddy automation（工作日 19:05）调用 `python scripts/daily_job.py`，绝对路径。
频率依据见上文「采集频率结论」。

GitHub Pages 只静态部署 `site/` 目录、**不重新构建报告**。改代码后更新线上的三步：

```bash
python cli.py report                                    # 1. 本地生成最新报告
cp ~/.etf-ntm/reports/etf_ntm_*.html site/index.html    # 2. 覆盖部署快照（Git Bash 路径）
git add site/index.html && git commit && git push       # 3. 推送触发 workflow（site/** 变更即部署）
```

忘记第 2 步 = 线上数据静默过期（无任何提示）。

飞书推送：设置环境变量 `ETF_NTM_FEISHU_WEBHOOK`（严禁硬编码/写日志），
相邻有效运行间方向翻转才推送（webhook 状态机）。

## 发布（小红书 / 公众号）

报告图表可自动发布到社交平台，发布链路（2026-11-09 打通）：

```bash
python scripts/snapshot_charts.py    # ① 报告 HTML → 7 张图表 PNG（Edge headless + marker 像素定位）
python scripts/publish_xhs.py        # ② 组装文案 → xiaohongshu-mcp 发布（默认仅自己可见试发）
python scripts/publish_xhs.py --public   # ③ App 内人工验收图片无误后，正式公开发布
```

- **小红书（全自动）**：xiaohongshu-mcp（部署于 `~/.etf-ntm/xhs-mcp/`，HTTP 端点
  `127.0.0.1:18060`）。首次使用需运行 `xiaohongshu-login-windows-amd64.exe` 扫码
  登录（cookies 落地后长期有效）。标题 ≤20 字、正文 ≤1000 字由脚本自动截断合规。
- **公众号（半自动）**：个人订阅号 API 只能建草稿不能自动发布（微信官方限制）。
  wenyan-mcp 已配置于 `~/.workbuddy/mcp.json`，填入 WECHAT_APP_ID/SECRET 并
  IP 白名单后可自动推草稿，最后一步在公众号后台手动点发布。
- 截图脚本技术细节（Playwright 三坑、Edge CLI 定型）见 `scripts/snapshot_charts.py` 文件头注释。

### 小红书方法学（2026-11-09 首发实测沉淀）

1. **先试发后公开**：默认 `visibility=仅自己可见`，在 App 里人工验收图片与文案，
   确认无误才 `--public`。visibility 参数是**中文枚举**（公开可见/仅自己可见/
   仅互关好友可见），英文值会被拒。
2. **没有编辑/删除 API**：MCP 工具只有 `publish_content` / `check_login_status` /
   `get_my_profile`（2026-11-09 实测）。**更新已发笔记的图片 = 重发一篇新笔记**，
   旧笔记在 App 里手动删。
3. **截图必须含标题**：读者在小红书看不到网页章节结构，每张 PNG 必须自带
   「图 N + 标题」。实现：marker outline 打到「标题+按钮+图表」的包裹层而非
   图表容器（见 `snapshot_charts.py` 的 `WRAPPER_SELECTOR` + `WRAP_JS`）。
4. **截图质量验证走像素**：发布前对每张 PNG 做四边贴边扫描（背景色差阈值），
   轴标签/图例溢出容器会在图缘留下文字像素。注意 CDP `storage.traverse` 量
   包围盒会把未渲染的隐藏元素（axisPointer 等）算进来产生假阳性，**以像素为准**。
5. **窗口默认最短档**：主图 120 日、K 线 120 日、轮动 20 日、热力 60 日、两融
   120 日——社交读者不需要全历史，全历史会稀释近期信号且加大文字渲染密度。
6. **echarts 图例配色坑**：某系列的 `itemStyle.color` 用显式常量时不消耗调色板
   名额，后续系列会错位取色（图例灰、线条紫对不上）。多系列图凡手工指定过
   一个系列颜色，其余系列全部显式指定，不依赖 palette 自动分配。

## 数据位置

- 数据库：`~/.etf-ntm/etf_ntm.db`（SQLite WAL）
- 报告：`~/.etf-ntm/reports/etf_ntm_YYYY-MM-DD.html`
- 图表素材：`~/.etf-ntm/reports/charts/`（发布用 PNG,自动生成勿手编）

## 免责声明

以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。
过往表现不预示未来收益。
