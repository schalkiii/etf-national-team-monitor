# 审计报告 — etf-national-team-monitor（2026-11-17）

> **执行状态（2026-11-17 17:05 更新）**：全部改进项已落地并推送 main（commit
> `32b1602` P0 + `6b562d7` P1-P3），92 用例全绿（77→92），CI Tests workflow 绿，
> 生产库已迁移（`cli.py repair`：5 拆分/10 噪声/31 死区=46 invalid_delta，
> adjust_factor 全部复位 1.0）。验证快照：510050 11-04 raw 163.34 亿=官方口径、
> 159845 factor 48.7 消失（raw 39.5 亿）、核心八宽基合计 2818 亿=raw 口径
> （原 adj 3161 亿虚高 12% 已消除）。A1 项下 512100 2020-06-10 份额 ×0.72
> 已于 2026-11-17 核实官方定性：**真实大额机构赎回，非折算**（2020 中报 §9
> 份额变动表期初 2.79 亿→期末 2.19 亿、拆分栏为「-」；§11.1 某机构
> 2020-06-05~06-08 申购 2.44 亿后赎回 2.53 亿）。splits.py 资产守恒法未标折算、
> 该日差分保留为真实净赎回——处理正确，无需修复。
>
> **追加修复（commit `3833ce6`，2026-11-17）**：执行中发现并修复审计漏项——
> `daily_incremental` 不自愈日历，calendar 过期后 `trading_days` 返回空导致
> 沪市份额静默停采（本地库停在 11-06 近两周）。已加 `rebuild_calendar`
> 自愈 + 回归测试 `test_calendar_selfheal.py`（93 用例全绿）。site/index.html
> 新口径快照已同步（数据至 11-16，线上 live verified）。遗留清零。
>
> **增量审计追加（commit `7e8fd23`，2026-11-17）**：post-fix 复核发现 P0-1
> 读侧改造遗漏四处——`event_window_path`/`run_event_study` 合计口径/
> `total_shares_at`/`calibrated_daily_flow` 读裸 shares 未过滤 invalid_delta，
> 事件窗口跨折算日（如 510310 2024-09-20 ×2.05）会出现机械跳变。已全部
> 修复并新增 `test_invalid_delta_filter.py` 5 用例（98 用例全绿，CI 绿）。
>
> 审计范围：全部代码与文档（main @ cca9b6b）。审计只诊断不动手；改进项见文末清单，
> 执行阶段按 software-improvement 纪律逐条实施。
> 事实来源：源码通读 + 生产库 `~/.etf-ntm/etf_ntm.db` 只读核查 + 线上 Pages 验证。

## 一、现状描述

- **规模**：Python 27 文件 / 约 5150 行。分层：`fetch/`（4 文件，请求解析）→ `store/`（SQLite WAL）→ `core/`（12 文件，纯函数信号/校准/回测/报告装配）→ `cli.py`（组装）+ `scripts/`（4 个运维/发布脚本）。
- **测试**：5 个测试文件 77 用例，0.56s 全绿；覆盖信号引擎纯函数、回填幂等/时点性、θ 标定、报告 CD 数据块端到端解析。
- **文档**：README（74 行，读者向）、AGENTS.md（58 行，协作纪律）、PUBLISHING.md（61 行，发布手册），三者分工明确无重复。
- **运行态**：GitHub Pages 在线（HTTP 200），数据至 2026-11-16，调度为工作日 19:05 采集 + 19:10 Pages 部署。
- **整体水平**：纪律执行严格（口径诚实/只降不升/份额复权/密钥环境变量均有代码落实），测试文化好。**但本次审计发现 1 个高危数据正确性 bug（复权因子方向错误）与若干中低危问题，详见问题清单。**

## 二、问题清单

> **2026-11-17 16:40 重大更新**：初版报告 A1/A2 的定性有误，经上交所官方数据交叉验证后修正如下。
> 根因不是「探测不到拆分」，而是**反向**：qfq 价格序列在真实拆分日不跳变（已复权），
> collector 的「价格反向验证」永远不成立 → 25% 幅度阈值单独放行 → **把真实巨额申购误判为拆分**。
> 官方证据：上交所官网 510050 2026-11-06 总份额 163.27 亿份，与 raw 披露一致、与复权值 210 亿不符
> → 11-03 的 +27.9% 是真实申购，不是拆分。

### A1【高】collector 拆分探测把「真实巨额申购」误判为拆分，方向性污染复权因子
- **位置**：`fetch/collector.py` `detect_splits_and_adjust()`（L113-138）+ L15-16 阈值
- **描述**：探测条件「份额跳变 >25% 且价格近似同比例反向跳变」在 **qfq 前复权价格**序列上永不成立
  （真实拆分日价格已被复权、连续；实证：生产库全部 28 个 factor 变化点价格变化均在 -6.3%~+6.4%，
  无一出现「同比例反向跳变」）。实际放行的是 0.7~1.4 乘积容忍区间的单边份额跳变——即
  **任何 >25% 的真实申购都被乘上 factor**。随后 `cum_factor *= ratio` 复利累积，越滚越大。
- **实证**（生产库 + 上交所官网交叉验证）：
  - **真实申购被误判**（官方份额 = raw 披露，raw 跳变是真金白银）：
    510050 2026-11-03（+27.9%，官方 11-06 份额 163.27 亿 = raw 口径）、
    510310 2026-10-15（+37.6%）、512100 2024-02-05/06（熔断救市 +132.8%/+30%，
    splits.py 注释自己记录过「财富=2.18 非干净比例→真申购」）；
  - **factor 复利污染**：159845 累积 16 次误判，factor 达 **37.2→48.7 倍**（raw 39 亿份 ×48.7=1924 亿份 adj，荒谬值）；
  - **真拆分反而漏标**：159845 2022 三次、510310 2024-09-20（-48.8% 份额合并）、512100 2020-09-24——这些
    日 collector 均未写 factor（因为方向验证失败），是 `core/splits.py` 的 `repair_existing` 资产守恒法
    正确标到 invalid_delta 的（生产库 46 行）。
  - 另有 512100 2020-06-10 份额 ×0.72：价格连续、无次日回归、官方公告仅持有人大会（改投资范围）——
    需人工核公告定性，当前被 collector 乘了 0.719 factor（若为份额合并，raw 序列在 6-10 出现真实断口；
    splits.py classify_jump 判 real，不处理）。
- **影响范围**：所有消费 `shares*adjust_factor` 的下游——`core/analysis.py`（Δ份额、flow_hist）、
  `core/report.py`（合计份额、结构、轮动、申购落点、θ 输入）、`core/calibration.py`（θ 标定 ΔS）。
  核心八宽基中 510050/510310/512100/563360 受污染（合计 adj 2026-11-04 = 3161 亿 vs raw 2818 亿，虚高 12%）。

### A2【高】两套拆分判定机制并存且结论相反，双轨互相打架
- **位置**：`fetch/collector.py` `detect_splits_and_adjust()` vs `core/splits.py` `classify_jump()/repair_existing()`
- **描述**：splits.py 资产守恒法（wealth≈干净比例 + 价格连续<3% + 次日回归指纹）是**实证正确**的方法论，
  5 个真拆分全部正确识别；collector 幅度阈值法在 qfq 数据上失效且反向误判（见 A1）。两轨并存导致：
  - 同一标的 159845：collector 写 factor=48.7，splits.py 写 invalid_delta 过滤——消费方同时用两者，
    raw 份额先被 ×48.7 再被 invalid_delta 过滤部分日期，结果完全不可预测；
  - `repair_existing()` 无 CLI 入口（生产库 2026-11-06 是手工跑的，fetch_log 可见），rebuild 流程不含该步骤，
    新库重建后 invalid_delta 为空、折算/噪声日全部漏过滤。
- **影响范围**：数据可信度、rebuild 可重复性（AGENTS.md 声称「一键重建」，实际缺 repair 步骤）。

### A4【中】`cli.py daily` 退出码语义颠倒
- **位置**：`cli.py` L100 `return 0 if inc["share_rows"] >= 0 else 1`
- **描述**：`share_rows` 恒 ≥0（累加计数器），条件永真，采集失败（share_rows=0）也返回 0；对照 `scripts/daily_job.py` L29-30 的正确写法 `if inc.get("share_rows", 0) == 0: return 1`。`daily_job.py` 走的是自己的 main()，但任何直接调用 `python cli.py daily` 的运维（文档未禁止）都拿不到失败信号。
- **影响范围**：运维监控（automation 若调 cli.py daily 则失败静默）。

### A5【中】`_parse_kline_rows` 的 amount 兜底估算公式不可靠
- **位置**：`fetch/sources.py` L42-44
- **描述**：amount 缺失时用 `(o+c+h+low)/4 * vol * 100` 估算，其中 `*100` 是对「成交量单位为手」的猜测，且用均价×量近似成交额在 ETF 上系统性偏差（close 与 VWAP 差异可达 1-2%）。生产库当前 amount NULL 为 0 行（腾讯数据稳定带 amount），该分支为死代码路径；但一旦上游字段变化会静默注入估算值且无标记。
- **影响范围**：市场热度灯（L4）与 `_market_amount`，数据源格式变化时引入静默偏差。

### A6【中】`run_chain` 降级链执行器为死代码
- **位置**：`fetch/sources.py` L136-145
- **描述**：全项目无调用方。注释声称「学 etf-tracker source_chain」，但从未接线。
- **影响范围**：代码噪音；读者误以为存在多源降级。

### A7【中】热力图 evidence/flows 字典随窗口无限增长（内存与体积）
- **位置**：`core/report.py` L318-319
- **描述**：`heatmap["evidence"]` 与 `["flows"]` 为 `{date: {code: payload}}` 的全历史字典，400 日 × 8 核心标的的 payload JSON 全量烘进单文件 HTML（约几 MB）。`values` 已限 400 日，evidence 同样限 400 日但 payload 体积大（每条含 5 灯完整 JSON）。当前报告 2.97MB 尚可接受，但随时间线性膨胀（payload 不滚动清理）。
- **影响范围**：报告体积、Pages 加载、移动端体验。

### A8【中】深市融资余额接口实测返回空，文档宣称「两融余额」但实际只有沪市单边
- **位置**：`fetch/margin.py` `backfill_margin()`；README L29「两融余额 | 交易所两融日度披露」
- **描述**：2026-11-17 实测 `fetch_margin_szse('202610')` 返回 0 行（akshare 深市接口失效或已变更），
  生产库 954 行全部 source='sse'。`_margin_chg` 用沪市单边近似「两融 5 日变化」，图 4 标题
  「两融余额」未标注单边口径。深市逐月循环（rebuild 48 次请求）为纯无效开销。
- **影响范围**：L5 融资杠杆灯与图 4 口径名不副实（幅度近似可用）。

### A9【低】`fetch/margin.py` 单位换算启发式脆弱
- **位置**：`fetch/margin.py` L34-36 `if fin > 1e9: fin /= 1e8`
- **描述**：用数值大小猜单位。若某日沪市融资余额真实跌破 10 亿元（理论上不可能但口径变更时可能），会被误除。与 A5 同类的静默启发式。
- **影响范围**：低概率数据损坏。

### A10【低】`nt_estimate.align_to_grid` 的 total 用 0 填充缺失日
- **位置**：`core/nt_estimate.py` L131 `total_a = [... (0.0, None, 0.0, 0)]`
- **描述**：差分日缺失时 `total`（全市场总净申购对照柱）补 0 而非 None，主图下栏会画出高度为 0 的柱——与「不冒充」纪律的精神不符（视觉上无碍，但 0 ≠ 无数据）。
- **影响范围**：图表语义纯度。

### A11【低】README 回测数字硬编码日期，无更新机制
- **位置**：README L58「基于 250 个交易日、4500 个信号（截至 2026-11-06）」及 L60-63 三个结论数字
- **描述**：回测结论随 `python cli.py backtest` 输出变化，README 数字靠人工同步，已滞后（数据至 11-16）。历史结论无「截至」快照机制。
- **影响范围**：文档时效性。

### A12【低】`_market_amount` 的 250 日窗口与 `_sentiment_series` 的 400 日窗口不一致
- **位置**：`core/analysis.py` L40-48（LIMIT 250）vs `core/report.py` L84-89（LIMIT 400）
- **描述**：同一「两市成交额」指标在信号计算（当日热灯）与报告序列（图 6）用不同深度取数。L4 灯的 60 日滚动分位实际窗口受 250 日上限保护，但两处若有窗口口径调整会漂移不同步。
- **影响范围**：口径一致性维护成本。

### A13【低】`conftest.py` 合成数据 `amount` 固定 3e11，L4 灯恒灰
- **位置**：`tests/conftest.py` L69
- **描述**：指数成交额为常数 → 60 日分位恒 100 或 50 → 市场热度灯在测试中从未真正点亮过红/绿分支（测试仍通过是因为只断言其他灯）。测试未覆盖 L4 红/绿分支。
- **影响范围**：测试覆盖缺口（见 D 维度）。

### A14【低】报告 `date` 与 `last_close` 可能错位
- **位置**：`core/report.py` L152-155
- **描述**：`yearline` 用 `closes[-250:]`，`last_close = closes[-1]`——`idx_rows` 是全历史（LIMIT 无），而报告 date 是「最新有份额日」。若指数数据比份额新一天（K 线 T+0、份额 T+1，这是常态），`dist_yearline` 和 `last_close` 展示的是比报告日期**晚一天**的收盘。图 1 的 index 序列已按 share_dates 对齐，但顶部仪表盘没有对齐。
- **影响范围**：仪表盘数字与图表差一天（用户曾反馈过类似「日K vs 跟踪指数」概念问题，此处同源）。

## 三、审计维度结论

### 1. 文档一致性
**发现 A8（两融口径）、A11（回测数字滞后）、A14 相关**。其余：README 结构图表清单、五灯阈值表（+7.5%/250日/95分位等）与 `core/config.py`、`report_html.py` 方法表逐项核对一致；AGENTS.md 验收命令与 cli.py choices 一致；PUBLISHING.md 描述与两个截图脚本行为一致。

### 2. 文档缺漏
- `repair_existing`（invalid_delta 重建）无 CLI 入口无文档，新库重建流程缺步骤（A3）。
- `scripts/snapshot_charts.py` 与 `_kwb.py` 双通道的取舍理由已写 PUBLISHING.md，齐全。
- 数据单位约定（shares 为「份」、margin 为「亿」）只在 conftest 注释里，建议进 AGENTS.md 口径清单。

### 3. 架构优化点
- `core/report.py` 的 `_fetch_chart_data`（260 行）职责过重：取数 + θ 装配 + 网格对齐 + 热力图装配，可拆「取数/装配」两层（低优先级，现状可维护）。
- `fetch/collector.py` 同时承担采集编排与拆分探测（A1/A2 的耦合源头），拆分探测应收敛到 `core/splits.py` 单一权威。
- `report_html.py` 922 行单函数 f-string 模板——已知权衡（离线单文件），不建议拆。

### 4. 代码写法
A1（方向 bug）、A4（永真条件）、A5（魔法估算）、A6（死代码）、A9（单位启发式）、A10（0 填充）。命名与注释质量整体高。

### 5. 测试覆盖率
- **未覆盖模块**：`fetch/` 全部（sources/margin/calendar/collector 网络层，可理解——网络 mock 成本高）、`core/analysis.py` 的 `_market_amount`/`_margin_chg`、`core/report.py` 的 `_sentiment_series`/`_coverage_ledger`/`_overseas_snapshot`、`core/push.py`（webhook 状态机）、`cli.py`（无退出码测试——A4 因此漏网）、`core/calibration.calibrated_daily_flow`。
- **覆盖缺口**：`detect_splits_and_adjust` 无任何测试（A1/A2 因此漏网）；`repair_existing` 有测试但只测 dry_run 统计与写入，未测「与 collector 双轨冲突」场景；L4/L5 灯的红绿分支无测试（A13）。

### 6. 测试严谨性
- 优点：`test_nt_estimation` 用正则解析真实渲染 HTML 的 CD 块做端到端断言（强）；`test_backfill_is_point_in_time` 显式防未来数据泄漏（强）。
- 缺口：`test_backfill_populates_and_is_idempotent` 断言 `_rows(conn) == first` 是**行对象列表全等**，方向/置信度变化也会通过（弱断言，幂等性实际上已由 upsert 语义保证，该断言只是冒烟）；热力图 evidence payload 无 schema 断言。

### 7. 工程规范
- `.gitignore` 完善（pycache/db/site 产物/调试输出全覆盖）；分层纪律（fetch 无业务逻辑、core 不 import fetch/store）抽查无违例；workflow 最小权限（contents:read + pages:write）规范。
- 缺：无 lint 配置（ruff/flake8）、无 CI 跑测试（Pages workflow 只部署）。A4 类 bug 一行 CI 即可拦截。

## 四、改进建议（按优先级排序）

### P0-1 拆分判定收敛到 splits.py 单一权威，废弃 collector 双轨【需串行，A1+A2 合并修】
- **做法**：
  1. 删除 `fetch/collector.py` 的 `detect_splits_and_adjust()` 与 `SPLIT_DETECT_RATIO`；
     `rebuild_all`/`daily_incremental` 中的调用改为 `core/splits.py` 的 `repair_existing(con, dry_run=False)`
     （幂等重建 invalid_delta + share_splits——5 个真拆分它已全部正确识别）。
  2. `adjust_factor` 列废弃（读侧不再引用）：`core/analysis.py`、`core/report.py`、`core/calibration.py`
     中所有 `shares*adjust_factor` 改为裸 `shares` + invalid_delta 读侧过滤（该口径已生产验证）。
     存量库 `UPDATE etf_daily SET adjust_factor=1.0` 一次性复位（写一个 `cli.py repair` 子命令承担
     repair_existing + factor 复位 + 提示重跑 backfill-signals）。
  3. 512100 2020-06-10（份额 ×0.72、价格连续、无回归、无拆分公告实锤）定性为待人工核对项：
     若确认为份额合并，加入 MANUAL_DEAD_ZONES 或 factor 表（当前先记入报告遗留项，不动数据）。
  4. 修完后全量 `rebuild` + `backfill-signals --days 400` + 重生成报告，对比 510050 2026-11-03
     前后合计份额（应与官方 163 亿口径一致）、159845 factor=48.7 消失。
- **文件**：`fetch/collector.py`、`core/splits.py`、`core/analysis.py`、`core/report.py`、`core/calibration.py`、`cli.py`（加 `repair` 子命令）、`AGENTS.md`（更新复权口径条目）
- **验证**：新增测试「真实巨额申购日（份额+28%、价格连续、无回归）不被判 split」+
  「真拆分日（份额×2、价格连续）判 split 且差分作废」；`python -m pytest tests/ -q` 全绿；
  报告 510050 合计份额与上交所官方一致。
- **可并行**：否（全链路口径变更）

### P0-2 修复 `cli.py daily` 退出码【可并行】
- **做法**：L100 改为 `return 0 if inc["share_rows"] > 0 else 1`（与 daily_job.py 对齐）。
- **文件**：`cli.py`
- **验证**：新增测试：mock `daily_incremental` 返回 `share_rows=0` 时 `main()` 返回 1。
- **可并行**：是（与 P0-1 文件不重叠……注意 P0-1 也改 cli.py 加子命令，需串行小 coordination，或 P0-2 先行 5 分钟完成）

### P1-1 深市融资余额：接口已失效，删调用 + 文档明确沪市单边口径【可并行】
- **做法**：实测 `fetch_margin_szse('202610')` 返回 0 行（2026-11-17），akshare 深市接口失效。
  删除 `backfill_margin` 中的深市逐月循环与 `fetch_margin_szse`（无效请求），README 数据源表与
  图 4 标题/图注改为「沪市融资余额」，L5 灯图注同步。留 TODO 注释：深市接口恢复后可重新接入。
- **文件**：`fetch/margin.py`、`README.md`、`core/report_html.py`（图 4 标题）、`core/report.py`（margin gauge 标签）
- **验证**：生产库无 szse 行符合预期；报告图 4 标注「沪市」；pytest 全绿。
- **可并行**：是

### P1-2 删除 `run_chain` 死代码 + amount 兜底改为 None【可并行】
- **做法**：删 `fetch/sources.py` L133-145；`_parse_kline_rows` 的 amount 缺失分支返回 None（消费方 `_market_amount` 已有 None 容忍），删估算公式。
- **文件**：`fetch/sources.py`
- **验证**：pytest 全绿；生产库 amount NULL 仍为 0。
- **可并行**：是

### P1-3 CI：GitHub Actions 跑 pytest【可并行】
- **做法**：新增 `.github/workflows/test.yml`：push/PR 触发 `python -m pytest tests/ -q`（Python 3.13）。
- **文件**：`.github/workflows/test.yml`
- **验证**：push 后 Actions 绿。
- **可并行**：是

### P2-1 补 `detect/repair` 场景与 L4/L5 灯分支测试【P0-1 之后】
- **做法**：a) 合成数据加一个「份额 ×1.25 + 价格不变」的拆分日，断言 repair 后 invalid_delta 命中且差分被过滤；b) conftest 指数 amount 改为有波动的序列，补 L4 红/绿分支测试；c) margin 序列补涨跌分支测试 L5。
- **文件**：`tests/conftest.py`、`tests/test_splits.py`（或新文件）
- **验证**：新增用例红→绿过程留痕；覆盖率提升。
- **可并行**：与 P1 系列可并行（P0-1 后）

### P2-2 热力图 evidence payload 按需加载【可并行】
- **做法**：`heatmap["evidence"]` 只保留最近 400 日（values 同窗），或改为前端点击时从 flows/`signals` 表二次查询（当前单文件离线原则下，取前者：截断即可）。
- **文件**：`core/report.py`
- **验证**：报告体积对比（应 -1MB+）；点 400 日内格子证据卡仍正常。
- **可并行**：是

### P2-3 README 回测数字自动化或标注快照日期【可并行】
- **做法**：最小改：README 加「数字为 2026-11-06 快照，最新见 `python cli.py backtest`」；进阶：backtest 输出写 `site/backtest.json` 供 README 引用。
- **文件**：`README.md`（可选 `core/backtest.py`）
- **验证**：文档审读。
- **可并行**：是

### P3-1 仪表盘 last_close/yearline 对齐报告日期【可并行】
- **做法**：`_fetch_chart_data` 中 `closes` 截断到 `date`（`WHERE date<=?`）再算 yearline/last_close。
- **文件**：`core/report.py`
- **验证**：报告 date 与仪表盘收盘一致（对齐份额日）。
- **可并行**：是

### P3-2 align_to_grid total 缺失日改 None【可并行】
- **做法**：`(0.0, None, 0.0, 0)` 元组中 total 位置改 None，前端 echarts 对 null 柱自动跳过。
- **文件**：`core/nt_estimate.py`、（前端已容忍 null）
- **验证**：主图下栏对照柱无零高度柱；pytest 全绿。
- **可并行**：是

## 五、优先级与并行性总表

| 优先级 | 项 | 涉及文件 | 可并行 |
|---|---|---|---|
| P0-1 | 拆分判定收敛+废弃 factor 双轨 | collector/splits/analysis/report/calibration/cli/AGENTS | 否（主干） |
| P0-2 | daily 退出码 | cli.py | 与 P0-1 串行（同文件），建议先做 |
| P1-1 | 深市两融：删失效调用+单边口径标注 | margin.py/README/report_html/report | 是 |
| P1-2 | 删 run_chain + amount 兜底 | sources.py | 是 |
| P1-3 | CI 测试 workflow | .github/workflows/ | 是 |
| P2-1 | 拆分/灯分支测试 | tests/ | P0-1 后 |
| P2-2 | evidence 截断 | report.py | 是 |
| P2-3 | 回测数字快照标注 | README.md | 是 |
| P3-1 | 仪表盘日期对齐 | report.py | 是 |
| P3-2 | total 填 None | nt_estimate.py | 是 |

**执行建议**：P0-2（5 分钟）→ P0-1（核心，需全量 rebuild 验证）→ P1 三项并行 → P2/P3 按需。
P0-1 完成前，报告中 510050/510310/512100/563360（核心层）与 159845 等（扩展/观察层）的
份额与 θ 数字存在方向性失真（核心合计虚高约 12%），建议尽快执行。

## 六、修复后口径快照（供 rebuild 后对照）

修复后核心八宽基合计份额应与「官方登记份额之和」一致（raw 口径 + invalid_delta 过滤）：
- 2024-02-05 前后：512100 raw 76.92→100.04 亿份（熔断救市真申购，保留跳变）；
- 2026-11-03：510050 raw 128.66→164.61 亿份（真申购，保留）；官方 11-06 合计口径见上交所 ETF 规模页；
- 159845：factor 48.7 消失，raw 序列 39 亿份量级（拆分日 3 处被 invalid_delta 过滤差分）；
- 510310 2024-09-20：raw 1216.8→623.5 亿份（份额合并真事件，差分作废不参与估算）。
