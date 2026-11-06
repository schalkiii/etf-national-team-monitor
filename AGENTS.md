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

- 深交所份额接口区间上限约 185 自然日（2026-11-06 实测），collector 已分段处理
- 510310/510050/512100/563360 历史上有拆分/折算,复权因子已写入
- 指数成交额：上证 sh000001 + 深成 sz399001 合计近似两市
- margin_daily 表尚未接数据源,L5 融资杠杆灯暂为灰灯（优雅降级）
