# AGENTS.md — 协作纪律红线（与用户 RTL 工作流对齐）

## 修改前必读

1. **口径诚实**：估算值永不冒充真实值；BLOCKED 不降级为 NEUTRAL；null 就是 null
2. **只降不升**：所有置信度调整方向一致（验证背离/灰灯过多/耗尽闸门/空头封顶）
3. **可审计**：每个阈值/权重随信号输出；fetch_log 记录数据源健康度
4. **份额口径**：读侧一律用裸 `shares`（raw 披露）；折算/披露噪声日由 `invalid_delta`
   读侧过滤（join / set 过滤），原始披露值永不修改。`adjust_factor` 复权因子法已废弃
   （qfq 价格上判定失效曾致真实申购误判为拆分，见 AUDIT-REPORT A1/A2），
   列保留仅作存储，不再被任何读侧引用
5. **归因克制**：份额变化只证明大资金申赎；归因国家队 = 官宣 + 季报锚点 + 行为特征三角验证
6. **严禁 token 泄露**：webhook 走环境变量 `ETF_NTM_FEISHU_WEBHOOK`，代码/日志/报告零暴露
7. **诊断不动手**：先出方案（潜在收益/改动点/难度）确认再改
8. **增量实现**：每阶段独立验收 → 全回归（rebuild → report → calibrate → backtest → events）→ 文档同步
9. **发布纪律**：小红书默认仅自己可见试发，App 人工验收后才公开；MCP 无编辑/删除，
   更新图片=重发新笔记+手动删旧笔记；截图 PNG 发布前必须四边像素扫描（贴边=溢出）。
   MCP 的 `publish_content` 在本机实测被创作者中心鉴权拒绝（非 cookie 问题），**发布走
   kimi-webbridge 驱动真实 Edge 图形界面**；截图优先 `snapshot_charts_kwb.py`。
   门槛与判据见 `PUBLISHING.md`（README 面向读者，不放维护细节）

## 分层纪律（严禁跨层）

```
fetch/   只做请求与解析,无业务逻辑
store/   只做存取（SQLite WAL + 原子写入）
core/    纯函数信号/校准/回测（signal_engine 不 import fetch/store）
cli.py   仅组装
```

## 验收命令

```bash
python cli.py rebuild && python cli.py report && python cli.py calibrate && python cli.py backtest && python cli.py events
python -m pytest tests/ -q          # 93 用例（默认 python 3.13 即可,无需切环境）
```

## 信号表与热力图

- `signals` 表只由 `run_daily`（当天）和 `backfill-signals`（历史）写入；`cli.py report`
  只算不落库。新库/重建后必须跑一次 `python cli.py backfill-signals --days 400`，
  否则热力图只有当天一列、看起来全空。
- 回填复用 `compute_signals_for_date` + `persist_signals`，`(code,date)` upsert 幂等，
  可重复执行；单日异常隔离不中断。默认 400 交易日覆盖热力图全部窗口。

## 已知数据口径

- 全部数据源为日频（份额/K线日度、两融 T+1、锚点季度）；调度=工作日 19:05 一次即可,
  盘中加跑无增益;晚披露靠 `daily_incremental` 近 5 自然日回补追赶
- **日历自愈**：`daily_incremental` 每次 K 线拉新后自动 `rebuild_calendar`——
  calendar 停在旧日期会使 `trading_days` 返回空、沪市份额静默停采
  （2026-11-17 实测踩坑,本地库曾因此停摆近两周）
- 深交所份额接口区间上限约 185 自然日（2026-11-06 实测），collector 已分段处理
- 510310/510050/512100/563360 等历史上有拆分/折算，由 `core/splits.py` 资产守恒法识别
  （`repair_existing` 幂等重建 share_splits/invalid_delta，rebuild/daily 已内置）；
  存量库迁移用 `python cli.py repair`（清洗 + adjust_factor 复位）
- 512100 2020-06-10 份额 ×0.72 已核实（2026 中报 §9/§11.1）：真实大额机构赎回非折算,
  classify_jump 判 real 正确,该日差分保留为真实净赎回
- 指数成交额：上证 sh000001 + 深成 sz399001 合计近似两市
- margin_daily 已接数据源（2022-11 起,~954 行），L5 融资杠杆灯正常工作；
  `_margin_chg` 读取 `fin_balance_yi` 列（表缺失时优雅降级灰灯）
- 空头封顶红线已代码实现：`signal_engine.apply_confidence_rules(direction=...)`，
  SHORT 置信度封顶「中」，封顶理由写入 reasons 链
- 国家队估算算法在 `core/nt_estimate.py`（纯函数,可直接测试）；
  report.py 只做取数与装配；HTML 模板在 `core/report_html.py`
- 锚点录入：把新报告期数据写入 `core/anchor_data.py` 的 ANCHORS 后跑
  `python cli.py rebuild`，`seed_anchors` 幂等 upsert 生效；或手工
  `calibration.upsert_anchor`
