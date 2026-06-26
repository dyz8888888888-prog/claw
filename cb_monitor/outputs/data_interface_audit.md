# cb_monitor 数据接口层全景审查

> 审查时间: 2026-06-23 22:47 | 审查范围: 全部 6 类数据源 + 5 个缓存文件

---

## 一、总览：6 类数据源架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        DashBoard (Flask :5000)                  │
│  /api/battle  /api/signals  /api/concepts  /api/concept_sectors│
└────────────────────────────┬────────────────────────────────────┘
                             │ state.update_cycle() 每3秒
┌────────────────────────────▼────────────────────────────────────┐
│                     Scheduler (主循环 3s/tick)                   │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │  Fuyao   │  │   TDX    │  │ akshare  │  │   pywencai   │   │
│  │ (核心)   │  │ (实时)   │  │ (备源)   │  │  (兜底)      │   │
│  │          │  │          │  │          │  │              │   │
│  │涨停池    │  │转债行情  │  │全量池    │  │涨停股查询    │   │
│  │连板天梯  │  │正股行情  │  │转债spot  │  │炸板股查询    │   │
│  │概念指数  │  │三大指数  │  │炸板池    │  │              │   │
│  │概念目录  │  │概念抽样  │  │强赎数据  │  │              │   │
│  └──────────┘  └──────────┘  │交易日历  │  └──────────────┘   │
│                               └──────────┘                     │
│  ┌──────────┐  ┌──────────────────────────────┐                │
│  │   KPL    │  │         本地缓存               │                │
│  │ (离线)   │  │ concept_stocks.json (388概念)  │                │
│  │          │  │ concept_leaders.json (问财龙头) │               │
│  │日级概况  │  │ kpl_cache.json (开盘啦日级)    │                │
│  │六维情绪  │  │ trading_calendar.json (交易日) │                │
│  └──────────┘  └──────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、逐源分析

### 2.1 Fuyao (同花顺扶摇) — 核心数据源 ★★★★★

| 属性 | 值 |
|------|-----|
| 接口 | `https://fuyao.aicubes.cn` REST API |
| 认证 | `X-api-key` Header |
| 连接 | `core/fuyao_client.py` — 全局单例 `get_fuyao_client()` |
| 超时 | 10s (无重试) |

| 端点 | 调用频率 | 用途 | 调用位置 |
|------|----------|------|----------|
| `limit-up-pool` | 每5分钟 | 涨停家数 (市场状态) | `market_state.py::_try_fuyao_pool()` |
| `limit-up-pool` (分页) | API端点 | 涨停龙一列表 | `server.py::_get_dragons_if_available()` |
| `limit-up-ladder` | 每5分钟 | 晋级率计算 | `market_state.py::_fetch_promotion_rate()` |
| `catalog/ths-index-list` | 启动时1次 | 概念板块目录(388个) | `consensus_tracker.py::ConceptIndexFeed.load_catalog()` |
| `prices/snapshot` | 每60秒 | 388概念实时行情 | `consensus_tracker.py::ConceptIndexFeed.refresh()` |

**评价**: ✅ 设计合理，单例封装干净，批量snapshot(80只/批)优化好。⚠️ 无重试/无熔断。

---

### 2.2 TDX (通达信 mootdx) — 实时行情主力 ★★★★★

| 属性 | 值 |
|------|-----|
| 协议 | `mootdx.quotes.Quotes` TCP直连 |
| 实例 | `TdxClient` 单例 + 健康检查 |
| 超时 | mootdx内置 (约5-10s) |

| 查询 | 每轮调用 | 数据量 | 调用位置 |
|------|----------|--------|----------|
| 转债行情 | 1次/tick | ~80只债券 | `data_fusion.py::_fetch_bonds_from_tdx()` |
| 正股行情 | 1次/tick | ~75只正股 | `data_fusion.py::_fetch_stocks_from_tdx()` |
| 三大指数 | 1次/5min | 3只指数 | `market_state.py::_fetch_indices()` |
| 概念抽样 | 每轮1概念 | ~12只正股 | `consensus_tracker.py::_fetch_batch_snapshot()` |

**健康检查机制**:
- 连接 >30s 未使用 → 重建
- 连续3次错误 → 重建
- 每次 `get()` 时 ping `000001` 验证

**评价**: ✅ 健康检查完善。⚠️ 单TCP连接，网络闪断需等30s才重建。⚠️ `Quotes.factory()` 依赖 TDX 客户端运行。

---

### 2.3 akshare — 全量池 + 备源 ★★★☆☆

| 属性 | 值 |
|------|-----|
| 协议 | `akshare` HTTP库，爬取东方财富/新浪等 |
| 稳定性 | 中 (依赖上游网站) |

| 接口 | 频率 | 用途 | 优先级 |
|------|------|------|--------|
| `bond_zh_cov()` | 开盘前1次/天 | 全量转债池(1023只) | 唯一源 |
| `bond_zh_hs_cov_spot()` | TDX缺失>5只时 | 补充转债行情 | TDX补充 |
| `stock_zt_pool_dtgc_em()` | Fuyao失败后 | 炸板数 | Fuyao备源 |
| `stock_zt_pool_em()` | Fuyao失败后 | 涨停数 | Fuyao备源 |
| `bond_cb_redeem_jsl()` | 开盘前1次/天 | 强赎数据 | 唯一源 |
| `tool_trade_date_hist_sina()` | 年初1次 | 交易日历 | 唯一源 |
| `bond_zh_hs_cov_spot()` | `bond_selector.get_total_active()` | 活跃转债计数 | 辅助 |

**评价**: ✅ 作为唯一的全量池来源(不可替代)。⚠️ 网络依赖，外部网站改版即失效。

---

### 2.4 pywencai (问财) — 三级兜底 ★★☆☆☆

| 属性 | 值 |
|------|-----|
| 协议 | `pywencai` HTTP库 |
| 使用场景 | Fuyao和akshare都失败时的兜底 |

| 查询 | 位置 |
|------|------|
| `'今日涨停股'` | `market_state.py::_try_wencai()` |
| `'今日炸板股'` | `market_state.py::_try_wencai()` |
| 概念龙头查询 | `scripts/query_concept_leaders.py` (离线脚本) |

**评价**: ✅ 三级兜底设计好。⚠️ 很少被调用(仅在双源失败时)，实际未充分测试。

---

### 2.5 KPL (开盘啦) — 离线日级分析 ★★☆☆☆

| 属性 | 值 |
|------|-----|
| 协议 | 本地爬虫 `kaipanla-crawler` |
| 缓存 | `data/kpl_cache.json` (JSON, 日级) |
| 使用场景 | 非实时，六维情绪/日概况 |

| 接口 | 用途 |
|------|------|
| `get_daily_summary()` | 涨跌停/涨跌家数/上证指数 |
| `get_limit_up_sectors()` | 涨停原因板块 |
| `get_limit_up_ladder_stats()` | 连板梯队/炸板率/晋级率 |
| `get_consecutive_limit_up()` | 连板详情 |
| `get_new_high_count()` | 百日新高 |
| `get_broken_limit_up()` | 历史炸板 |
| `get_abnormal_stocks()` | 实时异动 |

**特别说明**: 涨停池和晋级率已从KPL迁移到Fuyao (`limit-up-pool` + `limit-up-ladder`)，KPL现在主要用于 `sentiment_engine.py` 的六维情绪计算。

**评价**: ⚠️ 外部依赖 `kaipanla-crawler` 路径硬编码为 `../kaipanla-crawler`。⚠️ 此爬虫不存在于项目中（`.gitignore`?）。

---

### 2.6 本地缓存层

| 文件 | 大小 | 刷新频率 | 作用 |
|------|------|----------|------|
| `data/concept_stocks.json` | ~2MB | 手动 `build_concept_cache.py` | 388概念×成分股映射 |
| `data/concept_leaders_*.json` | ~50KB | 手动 `query_concept_leaders.py` | 问财概念龙头 |
| `data/kpl_cache.json` | ~200KB | 每次KPL调用 | KPL日级数据 |
| `data/trading_calendar.json` | ~5KB | 年初1次 | 全年交易日 |

---

## 三、数据流时序 (每轮 tick，约3s)

```
t=0ms    DataFusion.merge()
           ├── TDX 批量查转债 (80只, ~0.1s)
           ├── TDX 批量查正股 (75只, ~0.1s)
           ├── akshare spot 补充 (缺失>5只才触发, ~2s)
           └── 构造 Snapshot + 计算溢价率
t=500ms  SignalEngine.analyze()  ← 纯内存计算
t=800ms  MarketStateClassifier (每5分钟触发)
           ├── Fuyao limit-up-pool: 涨停数
           ├── Fuyao limit-up-ladder: 晋级率
           ├── TDX 三大指数
           └── akshare → pywencai 备源 (Fuyao失败时)
t=1200ms ConsensusTracker.update()
           ├── 池内正股聚合阶段
           ├── TDX 概念抽样 (1概念/轮)
           └── ConceptIndexFeed.refresh() (60秒节流)
t=1500ms AlertManager.process() / 决策管道
t=1800ms Dashboard 状态更新 + 输出渲染
```

**平均单轮耗时**: ~1.5-2.0s (TDX正常) / ~5s (akshare补充触发时)

---

## 四、降级链 (以涨停数为例)

```
1. Fuyao limit-up-pool      ←  主源 (最快/最全)
   ↓ 失败
2. akshare stock_zt_pool_em ←  备源 (HTTP爬虫)
   ↓ 失败
3. pywencai '今日涨停股'     ←  兜底 (自然语言)
   ↓ 全部失败
4. limit_up=0 (降级为中性态)
```

**炸板数的降级链不同**:
```
1. akshare stock_zt_pool_dtgc_em   ←  主源 (Fuyao不返回炸板数)
   ↓ 失败
2. pywencai '今日炸板股'            ←  兜底
```

**注意**: 这是一个设计缺陷——Fuyao `_try_fuyao_pool()` 返回的炸板数永远是 `0`，而炸板数是后来才通过 akshare 补充的。当 Fuyao 成功但 akshare 失败时，`broke_limit` 为 0，可能导致市场状态判定偏差。

---

## 五、接口层存在的问题

### P0 (严重 — 可能影响核心功能)

| # | 问题 | 影响 | 文件 |
|---|------|------|------|
| 1 | **炸板数数据源割裂**: Fuyao 取涨停时 `broke_limit` 写死为0，炸板数由 akshare 独立取，两者不同源可能导致数据不一致 | 市场状态误判（如涨停200但炸板0→判定为"高潮"而非"退潮"） | `market_state.py:143-157` |
| 2 | **Fuyao 无重试/无熔断**: `_get()` 超时10s后直接返回 None，无指数退避 | 网络抖动时每5分钟重试一次就放弃 | `fuyao_client.py:205-215` |
| 3 | **TDX 健康检查盲区**: 30s 空闲才重建+连续3次错误才重建，网络中瞬间可能拉长到 ~5轮 tick（15s）无数据 | 盘中短暂断连后恢复慢 | `data_fusion.py:67-91` |
| 4 | **KPL kaipanla-crawler 依赖缺失**: 路径硬编码 `../kaipanla-crawler` 但目录不存在 | `sentiment_engine.py` 六维情绪模块可能导入失败 | `kpl_client.py:26-29` |

### P1 (中等 — 影响健壮性)

| # | 问题 | 影响 | 建议 |
|---|------|------|------|
| 5 | **akshare `bond_zh_cov()` 不可替代**: 如果东方财富网站改版，无备源 | 全量池无法刷新 | 增加 `_last_good` 缓存兜底 |
| 6 | **概念快照全量刷新**: 每次 388÷80=5批 HTTP 请求，约2-3s | 每60秒阻塞一次循环 | 考虑增量刷新 (仅活跃概念) |
| 7 | **环境变量 API_KEY 不一致**: FuyaoClient 用 `config.py` 里的 `CONFIG.ext_api.fuyao_api_key`，但 `build_concept_cache.py` 和 `consensus_tracker.py` 读 `os.environ.get("FUYAO_API_KEY")` | 可能导致脚本独立运行失败 | 统一 API_KEY 获取方式 |
| 8 | **pywencai 兜底未测试**: 理论上的三级降级在实际中极少触发，行为未知 | 双源同时故障时表现不确定 | 手动模拟测试 |

### P2 (轻微 — 代码质量)

| # | 问题 | 位置 |
|---|------|------|
| 9 | `fuyao_client.py` 顶部定义了 `_CONCEPT_CACHE_PATH` 和 `_CONCEPT_CACHE` 但未使用 | 死代码 |
| 10 | `build_concept_cache.py` 自行实现了 `api_get()` 而非复用 `FuyaoClient` | 代码重复 |
| 11 | `bond_selector.load_redeem_data()` 内部 `import akshare as ak` 重复导入（顶部已有） | 冗余 |
| 12 | `market_state.py` 中 `_get_recent_trade_date` 和晋级率逻辑依赖"昨天是交易日"假设 | 周一/长假后首日可能失效 |

---

## 六、API 调用频率统计 (盘中每小时)

| 数据源 | 接口 | 频率 | 次数/小时 |
|--------|------|------|-----------|
| Fuyao | limit-up-pool (分页) | 5分钟 | 12 |
| Fuyao | limit-up-ladder | 5分钟 | 12 |
| Fuyao | concept-snapshot (5批) | 60秒 | 60×5=300 |
| TDX | 转债 quotes (~80只) | 3秒 | 1200 |
| TDX | 正股 quotes (~75只) | 3秒 | 1200 |
| TDX | 三大指数 | 5分钟 | 12 |
| akshare | stock_zt_pool (备源, 几乎不触发) | Fuyao失败时 | 0-12 |
| akshare | bond_zh_hs_cov_spot (TDX缺失>5只时) | 按需 | 0-5 |

**总 HTTP 请求/小时**: Fuyao ~324次 + akshare ~0-17次  
**总 TCP 请求/小时**: TDX ~2412次

**Fuyao API 费率**: 概念快照占 300/324=92.6%。这是主要开销。每次 5批×80只，每批 HTTP 请求独立发。

---

## 七、优化建议

### 优先级排序

1. **🔴 P0-1: 统一炸板数获取** — Fuyao `limit-up-pool` 不返回炸板信息，建议同步到 Fuyao 文档确认是否有 `broken_limit` 字段，或者将 `_fetch_limit_up_pools()` 重构为总是同时获取涨停和炸板（a+b 合并而非 a→b 串行）。

2. **🔴 P0-2: Fuyao 加重试** — `_get()` 增加 `@retry(max_retries=2, backoff=1s)`，参考业界标准。

3. **🟡 P1-5: akshare 全量池增加本地缓存兜底** — 将 `bond_zh_cov()` 结果缓存到 `data/cov_pool_cache.json`，API 失败时使用最近一次缓存。

4. **🟡 P1-6: 概念快照改为增量** — 仅刷新 `ConsensusTracker` 中 stage ≥ 1 的活跃概念 (通常<50个而非388个)，减少80%请求。

5. **🟢 P2: 清理死代码 + 统一 API_KEY 获取** — 合并 `build_concept_cache.py` 的 HTTP 调用到 `FuyaoClient`，统一用 `CONFIG.ext_api.fuyao_api_key`。

---

## 八、总结

| 维度 | 评分 | 说明 |
|------|------|------|
| 数据源多样性 | ⭐⭐⭐⭐⭐ | 6类数据源，主/备/兜底三级降级 |
| 接口封装 | ⭐⭐⭐⭐ | FuyaoClient 干净，但 KPL/概念缓存有重复 |
| 容错/降级 | ⭐⭐⭐ | 三级降级链存在，但炸板数有设计缺陷 |
| 调用频率控制 | ⭐⭐⭐ | 概念全量刷新浪费资源，可优化 |
| 可维护性 | ⭐⭐⭐ | 部分 API_KEY 分散多处，缓存路径硬编码 |

**整体评价**: 数据接口层设计思路清晰——Fuyao(核心)+TDX(实时)+akshare(备源) 三层架构合理。核心短板在**炸板数数据源割裂**和**概念快照全量刷新过多**两个点上。其余为常规代码改进项。
