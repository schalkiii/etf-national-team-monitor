# AGENTS.md — 协作纪律红线（与用户 RTL 工作流对齐）

## 修改前必读

1. **口径诚实**：估算值永不冒充真实值；BLOCKED 不降级为 NEUTRAL；null 就是 null
2. **只降不升**：所有置信度调整方向一致（验证背离/灰灯过多/耗尽闸门/空头封顶）
3. **可审计**：每个阈值/权重随信号输出；fetch_log 记录数据源健康度
4. **份额复权**：任何份额分析必须用 `shares * adjust_factor`，原始 shares 只用于存储
5. **归因克制**：份额变化只证明大资金申赎；归因国家队 = 官宣 + 季报锚点 + 行为特征三角验证
6. **严禁 token 泄露**：webhook 走环境变量 `ETF_NTM_FEISHU_WEBHOOK`，代码/日志/报告零暴露
7. **诊断不动手**：先出方案（潜在收益/改动点/难度）确认再改
8. **增量实现**：每阶段独立验收 → 全回归（rebuild → report → calibrate → backtest → events）→ 文档同步
9. **发布纪律**：小红书默认仅自己可见试发，App 人工验收后才 --public；MCP 无编辑/删除，
   更新图片=重发新笔记+手动删旧笔记；截图 PNG 发布前必须四边像素扫描（贴边=溢出）

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
```

## 已知数据口径

- 全部数据源为日频（份额/K线日度、两融 T+1、锚点季度）；调度=工作日 19:05 一次即可,
  盘中加跑无增益;晚披露靠 `daily_incremental` 近 5 自然日回补追赶
- 深交所份额接口区间上限约 185 自然日（2026-11-06 实测），collector 已分段处理
- 510310/510050/512100/563360 历史上有拆分/折算,复权因子已写入
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
