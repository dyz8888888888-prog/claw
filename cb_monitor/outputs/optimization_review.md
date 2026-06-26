# cb_monitor 全面优化审查

> 审查时间: 2026-06-23 23:00
> 范围: 全部 18 个 .py 文件 + 1 个 HTML 模板 + 日志

---

## 一、总体评价

| 维度 | 评级 | 说明 |
|------|------|------|
| 架构设计 | ⭐⭐⭐⭐⭐ | 三级降级链路、5阶段情绪周期、4阶段决策管道，设计成熟 |
| 数据接口 | ⭐⭐⭐⭐ | 前两轮修复后大幅改善 (重试+健康检查+P1全部修完) |
| Web前端 | ⭐⭐⭐ | 移动优先设计好，但单文件 518 行、N+1 查询性能问题 |
| 内存/性能 | ⭐⭐⭐ | 每轮 3s 内完成充足，但 API 层有重复计算 |
| 运维/可靠性 | ⭐⭐ | 无日志轮转、无优雅关闭、无熔断 |
| 代码治理 | ⭐⭐ | 硬编码密钥、死代码残留、模块级可变全局状态 |

---

## 二、优化清单 (按优先级排序)

### 🔴 P0 — 直接影响线上稳定性

#### 1. Config API Key 硬编码
**位置**: `config.py` L169
```python
fuyao_api_key: str = "sk-fuyao-ZbW5ky_FP-yQ-xXQPeUT_IrLAA7ZoaS7"
```
**问题**: 密钥明文存在于代码中，一旦提交到 Git 立即泄露。
**建议**: 改为 `os.environ.get("FUYAO_API_KEY", "")` + `.env` 文件 + `.gitignore`

#### 2. 无日志轮转 - 持续增长
**位置**: `logs/` 目录（共 936KB，4个交易日数据）
**问题**: 每个交易日产生 ~250KB 回测 + ~200KB 信号日志，无上限，数月后将达数十 MB。
**建议**: RotatingFileHandler, 保留最近 30 天，或按大小轮转 (10MB/文件)

#### 3. Flask 无优雅关闭
**位置**: `server.py` L848-851
```python
app.run(host=host, port=port, debug=False, threaded=True)
```
**问题**: Ctrl+C 时 TDX 连接不会关闭, 造成连接泄露。
**建议**: 注册 `atexit.register(TdxClient.close)` + signal handler

---

### 🟡 P1 — 性能与代码质量

#### 4. API 请求中重复创建 DecisionPipeline
**位置**: `server.py` L113, `scheduler.py` L53
```python
pipeline = DecisionPipeline()  # 每次请求都 new
```
**问题**: `_build_battle_data()` 和 `_run_decision_pipeline()` 各创建一次，开销虽小但不必要。
**建议**: 全局单例 `_pipeline = DecisionPipeline()`, 每次只 `set_mainlines()` 更新内部状态。

#### 5. DashboardState `_read_lock` 从未使用
**位置**: `shared_state.py` L17
```python
_read_lock: threading.Lock = field(default_factory=threading.Lock)  # 定义了但从未 acquire
```
**建议**: 删除或实现真正的读写分离 (当前只有写锁)。

#### 6. `_detect_lowvol_plunge` 死代码
**位置**: `signal_engine.py` L704-734
**说明**: 该方法从未在 `analyze()` 中调用 — 已被 `_detect_miskill_buy()` (错杀抄底) 完全替代。`lowvol_plunge_*` 配置项仍使用但实际生效的是 `miskill_*`。
**建议**: 删除死代码 + 清理未使用的 config key。

#### 7. `_load_concept_map()` 每次请求读磁盘
**位置**: `server.py` L65-77
**问题**: `/api/concepts` 和 `_build_battle_data()` 每次都 `json.load()` ~300KB 文件。
**建议**: 加内存缓存 (当前有全局 `_concept_map` 但只在 server.py 内，Scheduler 也有自己的 `_load_concept_map`)。统一到一个模块级单例。

#### 8. `_build_battle_data()` 每请求执行决策管道
**位置**: `server.py` L99-178
**问题**: 每次 HTTP GET `/` 或 `/api/battle` 都执行完整的 `evaluate_batch()` — 对 80 只债做 4 阶段判定，即使 Dashboard 没人在看。
**建议**: 增加 3-5 秒的 dashboard 快照缓存，Scheduler 写入 shared_state.battle_snapshot，API 直接读。

---

### 🟢 P2 — 代码整洁与长期维护

#### 9. 模块级可变全局状态
**位置**: `data_fusion.py` L386
```python
_prev_prices: dict[str, float] = {}
```
**问题**: 模块级可变 dict，如果将来多线程访问会出问题。当前安全只是因为只有一个 Scheduler 线程写。
**建议**: 移到 Scheduler 实例属性或 `RollingWindow` 中。

#### 10. `fmt_amount()` 重复定义
**位置**: `data_fusion.py` L411, `decision_pipeline.py` L134
**问题**: 同一个函数在 2 个文件中 `import from core.data_fusion`。
当前 `decision_pipeline.py` 已经正确 import，但 `_detect_volume_spike` 和 `_detect_demon_bond` 也在各自方法内 import。可以统一。

#### 11. HTML 模板 518 行单文件
**位置**: `dashboard/templates/index.html` (30KB+)
**建议**: 分拆为 CSS 文件 + JS 文件，当前 inline 所有样式和脚本。

#### 12. 缺少 `__pycache__` 清理
**位置**: 30 个 `.pyc` 文件散落在各目录
**建议**: `.gitignore` 加 `__pycache__/` 并在 CI 中 `find -name __pycache__ -exec rm -rf {} +`

---

## 三、建议修复优先级路径

```
本周 (30min):
  ✅ P0-1: API Key 环境变量化 (1 行改)
  ✅ P1-4: DecisionPipeline 单例化 (2 处改)
  ✅ P1-5: 删除 _read_lock (1 行删)
  ✅ P1-6: 删除 _detect_lowvol_plunge (30 行删)

本月 (2h):
  □ P1-7: 概念映射统一缓存
  □ P1-8: Dashboard 数据缓存 3-5s
  □ P0-2: 日志轮转

下季度:
  □ P0-3: 优雅关闭
  □ P2 项按需
```

---

## 四、不做优化的理由

| 项目 | 不做原因 |
|------|----------|
| 信号引擎循环 O(N×M) | N=80, M=13 = 1040 ops/轮, 单轮 <0.2s, 不值得优化 |
| akshare 全量 spot 查询 | 只在 missing>5 时触发, 大部分时间不执行 |
| 概念快照 60s 刷新 | 已经做了增量优化, 活跃概念通常 <50 |
| Flask → FastAPI 迁移 | 当前并发量极低 (单人使用), API 数量固定, 收益为 0 |
| 数据库替代 CSV | 日志规模小 (<1MB), 引入 SQLite 增加复杂度但无实质收益 |
