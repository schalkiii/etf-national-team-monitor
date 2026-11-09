# ETF 国家队资金监控 · 审计报告（2026-11-09）

> **执行状态（2026-11-09 第二轮）：全部问题已清零。** R1-R9 全部落地（R10 同步完成）：
> P1-1/P1-2/P1-3 文档口径已同步且空头封顶已实现；P2-1 部署流程入 README；P2-2/P5-1~P5-4
> 补齐 splits/backtest/db/空头封顶/nt 纯函数 55 个新测试（总计 74 用例全绿）；
> P3-1/P3-2 拆分为 report.py(数据) + report_html.py(模板) + nt_estimate.py(算法)；
> P4-1~P4-4 死代码/残留文件已清理；P2-3 anchor_data 已接入 rebuild（seed_anchors 幂等 upsert）。
> 额外修复：estimate_nt_series 首差分日负索引回绕 bug（i=0 时 date_series[-1] 取到末日指数，
> 曾致首日误标活跃）——修复后活跃日 195→194、累计 1497.4→1493.1 亿（2020-01-02 的 4.3 亿误标剔除）。

> 审计范围：全仓库代码 + 文档 + 测试 + 部署工作流。审计方式：只读排查，本报告不改任何代码。
> 读者：执行修改的 AI Agent。每条建议包含做法、涉及文件、验证方式，可直接照做。
> 项目基线：19 个测试全通过；Pages 部署 success（commit 804b598）；Python 3.13；无第三方测试框架依赖（仅 pytest）。

---

## 一、现状描述

### 规模

| 项 | 值 |
|---|---|
| Python 源码 | 24 个文件，约 3,034 行（不含测试） |
| 测试 | 1 个测试文件 + conftest，19 用例，0.33s 全绿 |
| 最大的文件 | `core/report.py` 1,337 行（其中 HTML/CSS/JS 模板约 770 行，566~1329 行） |
| 配置 | `core/config.py` 集中管理，103 行 |
| 依赖 | akshare、pandas（运行时）；pytest（测试） |
| 部署 | GitHub Pages（静态快照 `site/index.html`），workflow 每工作日 19:10 重部署 |

### 结构（分层清晰，与 AGENTS.md 声明一致）

```
fetch/   采集（sse/szse 份额、腾讯K线、融资、日历）— 只做请求与解析
store/   SQLite 存储（WAL + 原子写入 + schema）
core/    纯函数信号引擎 / 校准 / 回测 / 折算清洗 / 报告生成
cli.py   仅组装子命令
scripts/ daily_job.py 每日自动化入口
tests/   合成数据驱动的算法回归测试
```

### 质量水平（可观察事实）

- 分层纪律执行良好：`signal_engine.py` 无 I/O；fetch 层无业务逻辑
- 魔法数字全部集中在 `config.py` 与 `splits.py` 顶部，与声明一致
- 测试覆盖了国家队估算核心算法（活跃日/θ/invalid_delta/CD 结构），但 fetch/store/cli/push/splits/backtest 零测试覆盖
- 文档（README/AGENTS.md/PLAN.md）总体准确，个别口径与代码有出入（见问题清单 P1-1、P1-2）

---

## 二、问题清单

严重程度：高=产生错误结果或违反自身红线；中=误导/维护陷阱；低=改进空间。

### A. 文档一致性

**P1-1｜AGENTS.md「融资杠杆灯暂为灰灯」已过时**　严重程度：中
- 位置：`AGENTS.md` 第 34 行
- 描述：写的是「margin_daily 表尚未接数据源，L5 融资杠杆灯暂为灰灯」。实际生产库 `margin_daily` 已有 954 行（2022-11-28 ~ 2026-11-05），`_margin_chg` 列名 bug 修复后 L5 灯已正常工作。
- 影响：下个 Agent 读 AGENTS.md 会误以为 L5 无数据，可能跳过 L5 相关维护或重复接数据源。

**P1-2｜AGENTS.md「空头封顶」红线未在代码实现**　严重程度：高
- 位置：`AGENTS.md` 第 6 行（只降不升规则隐含空头封顶）、`core/signal_engine.py` 161 行
- 描述：`apply_confidence_rules` 中「空头封顶：SHORT 不超过中」只有注释，没有实现代码。SHORT 信号路径（`analysis.py` 147 行）在 4 红灯时 base=「高」，无任何封顶。当前生产数据恰好无 SHORT|高（red≥4 且 delta<0 未同时出现），但规则路径是敞开的——一旦出现即违反自身红线。README 61-64 行把「空头封顶」当作已有纪律来引用回测结论。
- 影响：违反 AGENTS.md 自身红线第 2 条；未来某日 SHORT|高 信号会以高置信输出，而回测已证明高置信档不可靠。

**P1-3｜README 回测章节未注明活跃日判定口径变更**　严重程度：低
- 位置：`README.md` 61-66 行
- 描述：回测结论（4500 信号）基于旧信号逻辑生成。2026-11-09 活跃日判定加语境约束后，国家队估算部分（图1）口径已变，但回测用的三层信号（direction/confidence）未受影响——此点 README 未说明，读者无法判断回测结论是否仍适用。
- 影响：低。信号引擎本身未改，回测仍有效；但报告的 nt 部分与回测基于不同口径。

### B. 文档缺漏

**P2-1｜部署快照流程无文档**　严重程度：中
- 位置：根目录（README「部署」章节）
- 描述：workflow 只静态部署 `site/` 目录、不重新构建报告。改代码后必须本地 `cli.py report` → 复制到 `site/index.html` → 提交推送，线上才会更新。这个关键流程只存在于开发者的会话记忆中（本次已踩过一次：线上停留在旧版 3 天）。
- 影响：任何接手者改代码后忘记同步快照，线上数据静默过期，用户看到旧报告无任何提示。

**P2-2｜fetch/store/splits/backtest/push 层零测试且无测试文档说明取舍**　严重程度：低
- 位置：`tests/`
- 描述：现有 19 用例全部围绕国家队估算算法。fetch 层（网络 IO）不测合理，但 `splits.py`（折算判定纯函数、逻辑最复杂）、`store/db.py`（atomic_write）、`core/backtest.py`（回测统计）都是可离线测试的纯逻辑，未覆盖也无「为什么只测 nt 算法」的说明。
- 影响：回归风险集中在最复杂的折算判定模块（其注释自称「实证得出，非拍脑袋」——但没有任何回归测试守护这些实证结论）。

**P2-3｜`core/anchor_data.py` 的 ANCHORS 数组无调用方**　严重程度：中
- 位置：`core/anchor_data.py`
- 描述：文件头写「录入后 θ 校准即可工作」，但 `ANCHORS` 列表没有任何代码 import 或消费（grep 全仓库无调用方）。生产库锚点是手工 INSERT 的，这个文件成了「看起来会被加载、实际是死数据」的误导源。
- 影响：下个 Agent 可能把新锚点录入 ANCHORS 数组然后以为校准生效，实际不会。

### C. 架构

**P3-1｜`core/report.py` 单文件 1337 行，HTML 模板占 57%**　严重程度：中
- 位置：`core/report.py` 566~1329 行
- 描述：数据采集（约 440 行）+ HTML/CSS/JS 模板（约 770 行）+ 写入混在一个文件。模板部分是 f-string，`{{`/`}}` 转义充斥全文，每次改前端都要在 Python 转义迷宫里操作（本次 legend 修复即如此）。
- 影响：前端迭代成本高、易错；数据逻辑与展示逻辑耦合，测试只能通过解析生成的 HTML 来断言 CD 结构（间接且脆）。

**P3-2｜国家队估算逻辑内嵌在报告生成器中**　严重程度：中
- 位置：`core/report.py` 224~313 行
- 描述：活跃日判定（p90 + 语境约束）、θ_active 计算、网格对齐——这是核心业务算法，却只能在生成报告时通过解析 HTML 才能测试（现有测试正是这么做的）。`calibration.py` 有干净的纯函数层，但活跃日判定没有对应物。
- 影响：算法与展示绑定；复用（如推送到飞书的摘要想用同一判定）需复制逻辑。

### D. 代码写法

**P4-1｜死代码三处**　严重程度：低
- 位置与明细：
  - `core/signal_engine.py:171` `combined_signal()`：无调用方，函数体只是 return 参数
  - `core/models.py:31` `EtfDailyRow` dataclass：全仓库无使用
  - `tests/conftest.py:117` `core_codes_pair()`：定义后无测试引用
- 影响：低。但违反用户「严禁冗余」红线。

**P4-2｜`analysis.py` 中 `from datetime import date as _date` 局部重复导入两次**　严重程度：低
- 位置：`core/analysis.py` 105、115 行
- 描述：在 `compute_signals_for_date` 函数体内两处 `from datetime import date as _date`，应放模块顶部。
- 影响：可读性。

**P4-3｜`FlowSource` 导入后未使用（analysis.py）**　严重程度：低
- 位置：`core/analysis.py` 13 行
- 描述：`from core.models import Direction, FlowSource, ...` 中 FlowSource 在该文件无使用。
- 影响：lint 噪音。

**P4-4｜根目录残留三个调试输出文件**　严重程度：低
- 位置：`push_err.txt`、`push_out.txt`、`sysgit_out.txt`（均为 git push 诊断时的重定向产物，未入库 untracked）
- 影响：工作区整洁；`.gitignore` 未覆盖 `.txt`，有误提交风险。

### E. 测试覆盖率（缺口清单）

**P5-1｜`core/splits.py` 零覆盖**　严重程度：中
- 描述：折算判定是全项目最精巧的纯函数逻辑（持有人资产守恒、价格连续性、SPLIT_RATIOS 指纹、次日反向回归），118 行无一行测试。它直接决定 θ 锚定和国家队估算的数据质量。
- 影响：改阈值或重构时无回归保护。

**P5-2｜`core/backtest.py` 零覆盖**　严重程度：低
- 描述：forward-return 与事件研究是 README 核心结论的来源，118 行无测试。
- 影响：回测口径变化无回归保护。

**P5-3｜`store/db.py` atomic_write 零覆盖**　严重程度：低
- 描述：原子写入是并发正确性关键（README 引以为卖点），无测试。
- 影响：低（实现简单稳定）。

**P5-4｜活跃日判定边界场景缺失**　严重程度：中
- 位置：`tests/test_nt_estimation.py`
- 描述：现有用例覆盖了「牛市抢筹不误标」「护盘日标记」，但缺：① 卖出日路径（反弹日大额净赎回）无任何用例——本次修复新增的 39 个卖出日完全没有回归保护；② `p90` 数据不足 60 日时判定关闭的降级路径；③ 指数数据缺失日（idx_chg=None）的静默路径。
- 影响：卖出日判定是最新改动，恰是最无保护的代码。

### F. 测试严谨性

**P6-1｜`test_html_card_shows_last_active_not_zero` 断言依赖真实卡片段落**　严重程度：低
- 位置：`tests/test_nt_estimation.py` 200-208 行
- 描述：用正则匹配 HTML 卡片文本，若模板微调（如空格、标签顺序变化）即脆断。属可接受的契约测试，但断言文案 `+0`/`0` 的枚举不完全（如 `+0 亿` 带单位的情况未覆盖——实际模板单位在另一个 span，当前安全）。
- 影响：低。

**P6-2｜θ 区间断言宽于实现约束**　严重程度：低
- 位置：`tests/test_nt_estimation.py` 104-110 行
- 描述：`test_cd_theta_in_range` 断言 [0.5, 1.0]，与实现的 clamp 一致，但未覆盖 clamp 边界的构造用例（θ=1.5 的锚点应被截到 1.0）。
- 影响：低。

### G. 工程规范

**P7-1｜`site/index.html` 二进制快照进 git，diff 噪音大**　严重程度：低
- 位置：`site/index.html`（约 1.6MB，含内联 echarts）
- 描述：每次部署快照变更都是整文件替换，git 历史膨胀（约 3MB/5 次）。对单仓项目可接受，但可改用 workflow 现场构建或 Release 附件消除。
- 影响：仓库体积增长；diff 无可读性。

**P7-2｜`__pycache__` 状态**　严重程度：无（核实后无问题）
- 位置：各 `__pycache__/` 目录
- 描述：`git ls-files` 确认 0 个 pyc 入库，`.gitignore` 生效正常。磁盘上的 pyc 是本地运行产物，不入库。
- 影响：无。

---

## 三、改进建议（按优先级排序）

| # | 建议 | 涉及文件 | 验证方式 | 优先级 | 并行性 |
|---|---|---|---|---|---|
| R1 | **实现空头封顶**：`apply_confidence_rules` 增加 `direction` 参数，SHORT 时 `idx = min(idx, order.index("中"))`，理由写「空头封顶」 | `core/signal_engine.py`、`core/analysis.py`（调用点传 direction）、`tests/test_nt_estimation.py`（新增用例：4红灯+delta<0 → conf≤中） | `pytest tests/ -q` 全绿；构造信号单测断言 SHORT 封顶 | 高（违反红线） | 串行（涉及 signal 签名变更） |
| R2 | **同步 AGENTS.md 口径**：① L5 融资灯改为「已接数据源（margin_daily 2022-11 起），_margin_chg 读 fin_balance_yi」；② 空头封顶注明「代码实现于 signal_engine.apply_confidence_rules」 | `AGENTS.md` | 通读比对代码事实 | 高（文档误导） | 可与 R1 并行（不同文件） |
| R3 | **README 补部署快照流程**：部署章节加「改代码后：`cli.py report` → `cp ~/.etf-ntm/reports/etf_ntm_*.html site/index.html` → 提交推送」三行命令 | `README.md` | 新会话按文档操作能更新线上 | 中 | 可并行 |
| R4 | **国家队估算算法抽离**：把 report.py 224~313 的活跃日判定/θ_active/网格对齐抽为 `core/nt_estimate.py` 纯函数（输入 by_date/idx_map/anchors，输出 daily/cum/flags），report.py 调用之；测试改从纯函数直接断言 | `core/report.py`、新建 `core/nt_estimate.py`、`tests/` | pytest 全绿 + 报告 CD 数值与抽离前一致（可用生成报告 diff 验证） | 中 | 串行（大改） |
| R5 | **补 splits 回归测试**：构造合成序列覆盖：① 干净 2:1 折算日被识别；② 熔断日真申购（ wealth≈2.18 非干净比）不被误杀；③ 次日反向回归噪声被标记 | `tests/test_splits.py`（新建） | pytest 全绿 | 中 | 可与 R4 并行（不同文件） |
| R6 | **补活跃日卖出/边界用例**：① 反弹日（指数+2%）大额净赎回 → 卖出标记且 daily<0；② 数据 <60 日 → p90=None 无活跃日；③ idx_chg=None 日静默 | `tests/test_nt_estimation.py` | pytest 全绿 | 中 | 可与 R5 并行 |
| R7 | **anchor_data 处置**：二选一——① 写一个 `load_anchors(con)` 函数在 rebuild 时 upsert ANCHORS（让文件履行注释承诺）；② 删除该文件并把锚点录入说明并进 README「数据源」表 | `core/anchor_data.py`、`fetch/collector.py` 或 `README.md` | 方案①：rebuild 后 fund_holders 行数含 ANCHORS；方案②：grep 无残留引用 | 中 | 可并行 |
| R8 | **删除死代码**：`combined_signal`、`EtfDailyRow`、`core_codes_pair`、analysis.py 的 `FlowSource` 导入、局部 `_date` 导入上提至模块顶 | `core/signal_engine.py`、`core/models.py`、`tests/conftest.py`、`core/analysis.py` | pytest 全绿 + grep 无残留引用 | 低 | 可与 R2/R3 并行 |
| R9 | **清理工作区残留**：删除根目录 `push_err.txt`/`push_out.txt`/`sysgit_out.txt`；`.gitignore` 加 `*.txt`（或仅这三个文件名） | 根目录、`.gitignore` | `ls *.txt` 为空 | 低 | 可并行 |
| R10 | **（可选）report.py 模板外置**：HTML 模板移到 `core/report_template.html`，用占位符注入数据，Python 侧只留数据组装 | `core/report.py`、新建模板文件 | pytest 全绿（现有 CD 解析测试天然验证） | 低 | 串行（建议在 R4 之后） |

**并行分组说明**：
- 组 A（可并行）：R2、R3、R8、R9 —— 纯文档/清理，文件互不重叠
- 组 B（可并行）：R5、R6、R7 —— 新增测试/锚点处置，互不触碰对方文件
- 串行链：R1 →（R4 → R10 可选）；R1 改 signal 签名，应最先做避免组 B 测试写完后返工

---

## 四、给执行 Agent 的注意事项

1. **测试基线**：动手前先跑 `python -m pytest tests/ -q`，确认 19 passed 再开始；每完成一个改动点立即全量重跑。
2. **黄金守恒**：改动 report.py 数据逻辑时，用「生成报告 → 解析 CD → 与改动前快照对比」验证数值不变（R4 尤其如此）。
3. **AGENTS.md 红线仍然生效**：诊断不动手（本报告即方案）；估算不冒充；注释同步（改代码必须同步注释，禁改无关注释）。
4. **环境坑**：本机 shell 直接跑 `git push` 会报 Bad file descriptor（本地代理拦截大 POST body），用 `python -c "subprocess.run([...])"` 或 SSH remote；gh CLI 查状态正常。
5. **site 快照**：任何影响报告输出的代码改动，收尾时必须重新生成并更新 `site/index.html`，否则线上数据静默过期。
