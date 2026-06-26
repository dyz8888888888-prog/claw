# 可转债日报 — 项目长期记忆

## 系统架构
- **主数据源**: 通达信 V7.73 PYPlugins (tq API) — CB行情/规模/正股/概念/市场广度
- **概念评分**: i问财缓存 (cb_concept_map.json + cb_concept_heat.json) + TDX补充
- 筛选条件: 剩余规模<120亿, 溢价率<30%, 候选池83只
- 快照时间: 09:40/10:00/10:32/11:40/13:30/14:15
- 运行要求: TDX客户端必须运行且已登录

## 行情类型识别 (V5.1 新增)
- 6种行情类型: A强势普涨 / B温和偏强 / C微涨分化 / D横盘震荡 / E微跌分化 / F弱势普跌
- 三级识别: 09:40粗判(50%) → 10:00确认(80%) → 10:32定型(90%)
- 核心指标: 涨跌比、涨停数、指数偏离、量能比
- 权重动态调整: A(80/20) B(70/30) C(50/50) D(30/70) E(0/100) F(0/100)
- 代码位置: scripts/market_classifier.py, scripts/scorer.py, scripts/pipeline.py

## 评分算法
- **候选分 (2026-06-16 V7 波动优先)**:
  - 振幅 25% + 规模 20% + 流动性 20% + 溢价 15% + 方向 20% + 加分项
  - 加分项: 微盘(scale<2亿)+15, 小盘(scale<5亿)+8; 活跃(amp>10%)+15, 较活跃(amp>5%)+8; 超低溢价(prem<5%)+10, 低溢价(prem<10%)+5
  - 核心理念: 有波动才有收益，溢价是约束不是目标，顺势而为
  - 规模数据源: TDX get_kzz_info() → RestScope (万元→亿元)
  - 代码: scripts/tdx_pipeline.py → compute_candidate_scores_tdx() (唯一真相源)
  - 规模数据源: 东方财富 mx-finance-data → 未转股余额
  - 代码: scripts/scorer.py → compute_candidate_scores() (唯一真相源)
- **概念分**: Σ概念热度/√n → i问财 pywencai 动态计算（无数据时回退基准40）
- **最终 = 候选分 × 候选权重 + 概念分 × 概念权重** (权重由行情类型决定)
- **命中率**: D型行情实测 40% (4/10)

## cb_monitor 作战台 (2026-06-23 V8)

### 数据源
- **TDX 行情**: 通达信 V7.73 PYPlugins — 转债快照 + 正股涨跌
- **东方财富**: 转股价/溢价率/强赎数据
- **开盘啦 (KPL)**: kaipanla-crawler — 盘后涨停原因/龙头/情绪 (免费MIT)
- **问财/akshare**: 盘中涨停池/炸板池 (市场状态分类器备选)

### 核心模块
- `cb_monitor/core/kpl_client.py` — KPL数据封装 (日级JSON缓存, 自动回退)
- `cb_monitor/core/dragon_ranker.py` — 三维龙头排序 (封板时间35%+封单35%+连板30%)
- `cb_monitor/core/sentiment_engine.py` — 六维情绪引擎 (涨停强度/打板质量/接力/亏钱/赚钱/广度)
- `cb_monitor/core/market_state.py` — 5阶段市场周期 (冰点/启动/发酵/高潮/退潮)
- `cb_monitor/core/signal_engine.py` — S/A/B/C/D 五级信号
- `cb_monitor/core/decision_pipeline.py` — 四阶段决策管道
- `cb_monitor/core/consensus_tracker.py` — 概念共识追踪

### 前端 (4 Tab)
- 战情: 情绪标签 + 埋伏/卖出/禁入 + 概念传导链 + RRG
- 板块: 龙一~龙三排行 + 板块资金流向
- 情绪: 六维指标卡片 + 条形图
- 复盘: 后验统计 + 信号追踪

### API 端点
- GET /api/battle (作战台全量), /api/dragons (龙头), /api/sentiment_detail (六维)
- GET /api/sector_flow (资金流向), /api/concepts, /api/state
- kaipanla-crawler 路径: `../kaipanla-crawler/`

### 关键决策
- 盘中用TDX快照+转债池 (轻量实时)
- 盘后用KPL全量 (炸板率/晋级率/封单完整)
- 两者通过 SentimentEngine.merge() 混合
- 盘中缺少炸板/晋级时标注"(盘中估算)", 15:30后自动切换
- 妖债判定: 溢价>100% 或 价格>800元 → 不参与正常排名
- 价格作为规模代理: push2 API 无真实剩余规模，用价格替代 (低价≈微盘，高价≈大盘)
- 妖债不惩罚，单独列为妖债预警
- 停牌过滤: now=0 AND Volume=0 跳过
- **真实规模数据源**: TDX get_kzz_info() → RestScope (万元→亿元) — 与mx-finance 100%吻合
- CandidateScore 字段: name/code/cand_score/conc_score/final/premium/scale/price/amp/amount
- 评分唯一真相源: scripts/scorer.py → compute_candidate_scores()
- **市场状态唯一源**: SentimentEngine (情绪引擎) → SENTIMENT_TO_MARKET映射 → state.market_state → DecisionPipeline
  - MarketStateClassifier降级为备源
  - 代码: core/sentiment_engine.py (SENTIMENT_TO_MARKET), dashboard/server.py (_build_battle_data), scheduler/scheduler.py
- **gen_live_html.py V2 (2026-06-25)**: 完整4Tab自包含HTML (战情/板块/情绪/复盘)，嵌入6个API全量数据（battle/state/dragons/sector_flow/sentiment_detail/backtest），15秒自动刷新，部署至CloudStudio外链

### 板块涨停数据当日化 (2026-06-26 V8.1)
- **数据源**: `/api/sector_flow` 盘中优先 Fuyao 涨停池实时聚合, KPL 盘后全量回退
- **聚合逻辑**: `_build_fuyao_sector_flows()` — limit_up_reason 拆分 → 模糊匹配标准化 → ≥2只涨停门槛
- **调度器**: Fuyao 刷新间隔 90s (原60s, 缓解429限流); ConceptIndex 节流 120s (原60s)
- **日期**: 盘中显示当日 YYYY-MM-DD + source=fuyao(盘中实时)

### Fuyao 429 限流修复 (2026-06-26)
- **问题**: 06-25 全天 Fuyao 返回 429, 调度器直接丢弃缓存 → API 端点回退到 KPL 旧数据 (06-24)
- **根因**: 3 层并发: `concept_index.refresh(60s)` + `_refresh_fuyao_cache(60s)` + 概念快照, 超过免费版速率限制
- **修复 (4处)**:
  1. `core/fuyao_client.py::_get()`: HTTP 429 指数退避 (30/60/120s) 重试, 不再直接返回 None
  2. `core/consensus_tracker.py::ConceptIndexFeed`: 节流 60s→120s
  3. `scheduler/scheduler.py::_refresh_fuyao_cache()`: 节流 60s→90s; 新交易日首次强制刷新+清理昨日缓存
  4. `dashboard/shared_state.py::get_fuyao_pool()`: 跨日检测, 昨日缓存返回空 (避免误显昨日数据)
- **兜底**: `dashboard/server.py::_build_fuyao_dragons()`: 缓存空时自动直拉 Fuyao (带429重试)
- **Bug修复**: `server.py:1625` `state.get(...)` → `st.get(...)` (共识追踪器回退时的 typo, 触发 `DashboardState has no attribute 'get'`)

### 外链部署 (2026-06-25)
- 🔗 **永久快照**: https://6c11f531a142445b8b22fe8cbc88ad92.app.codebuddy.work (CloudStudio, 4Tab完整版, 每次 gen_live_html.py 后自动更新)
- 🔗 **实时隧道**: https://dyz-cb-battle.loca.lt (localtunnel, 需Flask 5000 + TDX 运行)
- 部署脚本: `scripts/gen_live_html.py` → `outputs/deploy/index.html` → CloudStudio deploy
- 刷新快照: 先启动 Flask `python main.py`, 再运行 `python scripts/gen_live_html.py`, 然后 deploy
