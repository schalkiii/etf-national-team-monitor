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

| 数据 | 接口 | 说明 |
|---|---|---|
| 沪市 ETF 份额 | akshare `fund_etf_scale_sse(date)` | 交易所官方,可回溯任意历史日 |
| 深市 ETF 份额 | akshare `fund_scale_daily_szse(start,end)` | 交易所官方,区间上限约185自然日,超限自动分段 |
| K 线/成交额 | 腾讯 `web.ifzq.gtimg.cn` | ETF 与指数日线 |
| 融资余额 | 交易所两融日度披露（akshare） | `margin_daily` 表,2022-11 起 |
| 季报锚点 | 基金定期报告前十大持有人 | 手工录入 `core/anchor_data.py` 的 ANCHORS 后跑 `cli.py rebuild`（`seed_anchors` 幂等 upsert）；或运行时 `calibration.upsert_anchor` |

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

每日自动化：WorkBuddy automation（每日 19:05）调用 `python scripts/daily_job.py`，绝对路径。

GitHub Pages 只静态部署 `site/` 目录、**不重新构建报告**。改代码后更新线上的三步：

```bash
python cli.py report                                    # 1. 本地生成最新报告
cp ~/.etf-ntm/reports/etf_ntm_*.html site/index.html    # 2. 覆盖部署快照（Git Bash 路径）
git add site/index.html && git commit && git push       # 3. 推送触发 workflow（site/** 变更即部署）
```

忘记第 2 步 = 线上数据静默过期（无任何提示）。

飞书推送：设置环境变量 `ETF_NTM_FEISHU_WEBHOOK`（严禁硬编码/写日志），
相邻有效运行间方向翻转才推送（webhook 状态机）。

## 数据位置

- 数据库：`~/.etf-ntm/etf_ntm.db`（SQLite WAL）
- 报告：`~/.etf-ntm/reports/etf_ntm_YYYY-MM-DD.html`

## 免责声明

以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。
过往表现不预示未来收益。
