# -*- coding: utf-8 -*-
"""集中配置：标的池 / 阈值 / 窗口 / 数据源。
纪律：全部可调常量收敛于此，core 层严禁散落魔法数字。"""

from pathlib import Path
import os

# ---------------------------------------------------------------
# 路径
# ---------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ETF_NTM_HOME", str(Path.home() / ".etf-ntm")))  # 数据与库文件统一目录(CI 可用环境变量重定向)
DB_PATH = DATA_DIR / "etf_ntm.db"
REPORT_DIR = DATA_DIR / "reports"
LOG_PATH = DATA_DIR / "etf_ntm.log"

# ---------------------------------------------------------------
# 标的池：三层结构
# tier: core=核心(官宣锚定) / ext=扩展(风格探头) / obs=观察(噪音对照)
# index: 跟踪指数腾讯代码; nav_source: 份额估算净申购所用净值来源
# clean: 信号干净度 3=汇金绝对主力 2=中等 1=散户主导(只看增量)
# ---------------------------------------------------------------
ETF_POOL = {
    # --- 核心层：2026-07 官宣点名宽基（沪市） ---
    "510300": dict(name="沪深300ETF华泰柏瑞", market="sh", tier="core", index="sh000300", idx_name="沪深300", clean=3),
    "510310": dict(name="沪深300ETF易方达", market="sh", tier="core", index="sh000300", idx_name="沪深300", clean=3),
    "510330": dict(name="沪深300ETF华夏",   market="sh", tier="core", index="sh000300", idx_name="沪深300", clean=3),
    "510050": dict(name="上证50ETF华夏",    market="sh", tier="core", index="sh000016", idx_name="上证50", clean=3),
    "510500": dict(name="中证500ETF南方",   market="sh", tier="core", index="sh000905", idx_name="中证500", clean=2),
    "512100": dict(name="中证1000ETF南方",  market="sh", tier="core", index="sh000852", idx_name="中证1000", clean=2),
    "588000": dict(name="科创50ETF华夏",    market="sh", tier="core", index="sh000688", idx_name="科创50", clean=1),
    "563360": dict(name="中证A500ETF",      market="sh", tier="core", index="sh000510", idx_name="中证A500", clean=2),
    # --- 扩展层：国新/诚通风格 + 深市宽基 ---
    "159915": dict(name="创业板ETF易方达",  market="sz", tier="ext", index="sz399006", idx_name="创业板指", clean=2),
    "159922": dict(name="中证500ETF嘉实",   market="sz", tier="ext", index="sh000905", idx_name="中证500", clean=2),
    "159845": dict(name="中证1000ETF华安",  market="sz", tier="ext", index="sh000852", idx_name="中证1000", clean=2),
    "159338": dict(name="中证A500ETF嘉实",  market="sz", tier="ext", index="sh000510", idx_name="中证A500", clean=2),
    "512880": dict(name="证券ETF国泰",      market="sh", tier="ext", index="sh399975", idx_name="证券公司", clean=2),
    "510880": dict(name="红利ETF华泰柏瑞",  market="sh", tier="ext", index="sh000015", idx_name="红利指数", clean=2),
    "512890": dict(name="红利低波ETF华泰柏瑞", market="sh", tier="ext", index="sh000015", idx_name="红利指数", clean=2),
    "515790": dict(name="光伏ETF华泰柏瑞",  market="sh", tier="ext", index="sh000015", idx_name="红利指数", clean=1),  # 占位,实际跟踪光伏产业
    # --- 观察层：深市小盘宽基对照 ---
    "159949": dict(name="创业板50ETF易方达", market="sz", tier="obs", index="sz399673", idx_name="创业板50", clean=1),
    "588080": dict(name="科创板50ETF易方达", market="sh", tier="obs", index="sh000688", idx_name="科创50", clean=1),
}

CORE_CODES = [c for c, v in ETF_POOL.items() if v["tier"] == "core"]
ALL_CODES = list(ETF_POOL.keys())

# 估算净申购用的指数集（指数日线抓取去重清单）
INDEX_CODES = sorted({v["index"] for v in ETF_POOL.values()} | {"sh000001"})  # 含上证综指作背景

# ---------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3                  # 学 etf-tracker：无重试=永远失败
RETRY_BACKOFF = 1.5

SHARE_LOOKBACK_DAYS = 1600        # 份额回填深度（交易日,约6.5年,覆盖2020至今全部护盘周期）
KLINE_LOOKBACK_DAYS = 2000        # K 线回填深度

# ---------------------------------------------------------------
# 描述层阈值（quantdata.uk 式描述标签，非预测）
# ---------------------------------------------------------------
COMBINED_5D_LARGE = 7.5           # 核心层合计份额 5 日变化 > +7.5% → 大规模申购
COMBINED_5D_ELEVATED = 2.0        # > +2% → 偏高
COMBINED_5D_REDEEM = -2.0         # < -2% → 净赎回

# ---------------------------------------------------------------
# 信号层（分位数，从数据学出，不拍脑袋）
# ---------------------------------------------------------------
PRICE_POS_LOW_PCT = 20            # 价格位置灯: 近250日分位 <20% 绿
PRICE_POS_HIGH_PCT = 80           # >80% 红
FLOW_BUY_PCT = 95                 # 净申购分位 >95 绿
FLOW_SELL_PCT = 5                 # <5 红
INDEX_DROP_PCT = -1.5             # 指数当日跌 <= -1.5% → 护盘语境灯(绿)
MARKET_HOT_PCT = 80               # 两市成交额 MA5 滚动分位 >=80 红(过热)
MARKET_COLD_PCT = 20              # <=20 绿(冷清)
RESONANCE_COUNT = 3               # 同色灯 >=3 → 共振

# MAD 异常检测
ANOMALY_SIGMA = 3.0
ANOMALY_WINDOW = 60               # 滚动窗口交易日

# 置信度纪律
CONF_DEMOTE_ON_DIVERGENCE = True  # 口径背离只降不升
EXHAUSTION_YI = 300               # |净流| >= 300亿 → 耗尽闸门封顶低置信
EXHAUSTION_HIT_RATE = 0.316       # etf-tracker 回测: 该区间命中率塌陷至 31.6%

# ---------------------------------------------------------------
# 国家队主体名单（锚点校准用）
# ---------------------------------------------------------------
NT_ENTITY_KEYWORDS = ["中央汇金投资", "中央汇金资产管理", "汇金投资", "汇金资管",
                      "中国证券金融", "中证金融", "国新投资", "中国国新",
                      "诚通投资", "中国诚通", "梧桐树", "凤山", "坤藤"]

# ---------------------------------------------------------------
# 推送
# ---------------------------------------------------------------
PUSH_ON_FLIP_ONLY = True          # 相邻有效运行间方向翻转才推送
FEISHU_WEBHOOK_ENV = "ETF_NTM_FEISHU_WEBHOOK"   # 密钥走环境变量,严禁硬编码
