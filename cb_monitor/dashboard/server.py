"""
仪表盘 Web 服务 - Flask API + HTML 仪表盘

提供:
- GET  /api/state           → JSON 数据接口
- GET  /api/concepts         → 概念热度排行
- GET  /api/battle           → 作战台全量数据
- GET  /api/dragons          → 概念板块龙头排行 [新]
- GET  /api/sentiment_detail → 六维情绪详情 [新]
- GET  /api/sector_flow      → 板块资金流向 [新]
- GET  /api/concept_sectors   → 概念板块指数快照 (Fuyao 直出) [新]
- GET  /api/concept_stats     → 概念市场广度统计 [新]
- GET  /api/history          → 历史信号 CSV
- GET  /                     → HTML 仪表盘
"""

import json
import os
import time
from datetime import datetime
import signal
import atexit
import logging
from collections import defaultdict
from flask import Flask, jsonify, request, send_from_directory

from .shared_state import state
from backtest.tracker import tracker as backtest_tracker
import core.consensus_tracker as consensus_mod
from core.decision_pipeline import DecisionPipeline

logger = logging.getLogger(__name__)

# ── DecisionPipeline 模块级单例 (避免每请求 new) ──
_decision_pipeline: DecisionPipeline = None

def _get_pipeline() -> DecisionPipeline:
    global _decision_pipeline
    if _decision_pipeline is None:
        _decision_pipeline = DecisionPipeline()
    return _decision_pipeline

# ── Battle 数据缓存 (同一 tick 复用, 避免 80 只债重复判定) ──
_battle_cache: dict = {}  # {last_update: result}

# ── KPL 引擎 (延迟加载, 避免启动阻塞) ──
_kpl_client = None
_sentiment_engine = None
_dragon_ranker = None

def _get_kpl():
    global _kpl_client
    if _kpl_client is None:
        try:
            from core.kpl_client import KPLClient
            _kpl_client = KPLClient()
        except Exception as e:
            logger.warning(f"KPLClient 初始化失败: {e}")
    return _kpl_client

def _get_sentiment_engine():
    global _sentiment_engine
    if _sentiment_engine is None:
        from core.sentiment_engine import SentimentEngine
        _sentiment_engine = SentimentEngine(kpl_client=_get_kpl())
    return _sentiment_engine

def _get_dragon_ranker():
    global _dragon_ranker
    if _dragon_ranker is None:
        from core.dragon_ranker import DragonRanker
        _dragon_ranker = DragonRanker()
    return _dragon_ranker

# 概念映射缓存 (从可转债日报项目加载)
_concept_map: dict = None
_concept_map_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  '..', '可转债日报', 'cb_concept_map.json')

def _load_concept_map() -> dict:
    global _concept_map
    if _concept_map is not None:
        return _concept_map
    try:
        path = os.path.normpath(os.path.abspath(_concept_map_path))
        with open(path, 'r', encoding='utf-8') as f:
            _concept_map = json.load(f)
        logger.info(f"概念映射加载: {len(_concept_map)} 只转债")
    except Exception as e:
        logger.warning(f"概念映射加载失败: {e}")
        _concept_map = {}
    return _concept_map

# 股票代码→CB代码映射 (从东方财富缓存加载)
_stock_to_cb: dict = None
_cov_cache_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'data', 'cov_pool_cache.json')

def _load_stock_to_cb() -> dict:
    """加载 stock_code → cb_code 映射 (带缓存)"""
    global _stock_to_cb
    if _stock_to_cb is not None:
        return _stock_to_cb
    try:
        path = os.path.normpath(os.path.abspath(_cov_cache_path))
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        items = data.get('items', [])
        _stock_to_cb = {str(item.get('stock_code', '')): str(item.get('code', ''))
                        for item in items if item.get('stock_code') and item.get('code')}
        logger.info(f"股票→CB映射加载: {len(_stock_to_cb)} 条")
    except Exception as e:
        logger.warning(f"股票映射加载失败: {e}")
        _stock_to_cb = {}
    return _stock_to_cb

# 概念→CB列表映射 (从cb_concept_map.json构建)
_concept_to_cb: dict = None

def _load_concept_to_cb() -> dict:
    """加载 concept_name → [cb_code, ...] 映射"""
    global _concept_to_cb
    if _concept_to_cb is not None:
        return _concept_to_cb
    cm = _load_concept_map()
    _concept_to_cb = defaultdict(list)
    for cb_code, info in cm.items():
        for c in info.get('concepts', []):
            _concept_to_cb[c].append(cb_code)
    return _concept_to_cb


# 无意义概念黑名单 (交易所分类/通用标签)
_NOISY_CONCEPTS = {'深股通', '沪股通', '融资融券', '转融券标的', '标普道琼斯A股', 'MSCI概念', '富时罗素概念股', '富时罗素概念'}

# 泛概念黑名单 (太宽泛不可交易: 政策标签/财报事件/综合大盘概念)
_BROAD_CONCEPTS = {'专精特新', '2025年报预增', '央企国企改革', '国企改革'}

# Fuyao → CB 概念别名 (从 concept_alias.json 加载, 可随时增补无需改代码)
_FUZZY_CONCEPT_ALIAS: dict = {}
_ALIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'concept_alias.json')

def _load_concept_alias() -> dict:
    global _FUZZY_CONCEPT_ALIAS
    if _FUZZY_CONCEPT_ALIAS:
        return _FUZZY_CONCEPT_ALIAS
    try:
        with open(os.path.normpath(_ALIAS_PATH), 'r', encoding='utf-8') as f:
            data = json.load(f)
            _FUZZY_CONCEPT_ALIAS = data.get('alias', {})
            logger.info(f"概念别名加载: {len(_FUZZY_CONCEPT_ALIAS)} 条")
    except Exception as e:
        logger.warning(f"概念别名加载失败: {e}, 使用空映射")
        _FUZZY_CONCEPT_ALIAS = {}
    return _FUZZY_CONCEPT_ALIAS


def _fuzzy_match_concept(fuyao_name: str) -> str:
    """将Fuyao概念名映射到cb_concept_map标准名

    优先级: 别名表(concept_alias.json) > 精确匹配 > 子串匹配 > 保留原名
    """
    # 1. 精确别名
    alias_map = _load_concept_alias()
    if fuyao_name in alias_map:
        return alias_map[fuyao_name]

    # 2. 先查精确匹配
    concept_to_cb = _load_concept_to_cb()
    if fuyao_name in concept_to_cb:
        return fuyao_name

    # 3. 子串匹配: Fuyao名是CB标准名的子串 (如 "PCB" in "PCB概念")
    for cb_name in concept_to_cb:
        if fuyao_name in cb_name:
            return cb_name

    # 4. 反向子串: CB标准名是Fuyao名的子串 (如 "创新药" in "创新药概念")
    for cb_name in concept_to_cb:
        if cb_name in fuyao_name:
            return cb_name

    # 5. 保留原名 (CB池中无对应概念, 但仍可能有股票→CB直接映射)
    return fuyao_name

app = Flask(__name__, template_folder='templates')

# 访问认证: Bearer Token 或 ?token=xxx 查询参数
_DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

@app.before_request
def _check_auth():
    # 如果未设置 token 则跳过认证 (开发/内网模式)
    if not _DASHBOARD_TOKEN:
        return None
    # 跳过 OPTIONS (CORS 预检)
    if request.method == "OPTIONS":
        return None
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    token = token or request.args.get("token", "")
    if token != _DASHBOARD_TOKEN:
        return jsonify({"error": "unauthorized", "hint": "需要 ?token= 或 Authorization: Bearer <token>"}), 401
    return None


def _build_battle_data():
    """生成作战台数据 (供 index 嵌入和 api/battle 共用) — 带缓存，同 tick 复用"""
    # 缓存键: last_update 时间戳
    cache_key = state.last_update
    if cache_key and cache_key in _battle_cache:
        return _battle_cache[cache_key]

    with state._lock:
        snapshots = dict(state.snapshots)

    # ─── 六维情绪 (最先计算, 驱动后续决策) ───
    from core.sentiment_engine import SENTIMENT_TO_MARKET
    sentiment_detail = _get_sentiment_detail(snapshots)
    sentiment_phase = sentiment_detail.get('phase', 'active')
    market_ms = SENTIMENT_TO_MARKET.get(sentiment_phase, 'ferment')

    # 市场状态由调度器统一写入，仪表盘只读
    # (不再写 state.market_state，避免 HTTP 请求期覆盖调度器计算结果)

    # 构建简版 sentiment (战情顶部标签)
    pool_up = sentiment_detail.get('indicators', {}).get('pool_up', 0)
    pool_down = sentiment_detail.get('indicators', {}).get('pool_down', 0)
    pool_ratio = pool_up / max(pool_down, 1)
    pool_limit_up = sentiment_detail.get('indicators', {}).get('pool_limit_up', 0)
    sent_source = sentiment_detail.get('source', 'fuyao+kpl')
    sentiment = {
        'status': sentiment_detail.get('phase_cn', '--'),
        'advance': pool_up,
        'decline': pool_down,
        'ratio': round(pool_ratio, 2),
        'limit_up': pool_limit_up,
        'source': sent_source,
        'advice': sentiment_detail.get('advice', ''),
    }

    # ── Fuyao龙头板块 (统一数据源: 战情主线/龙头Tab 共用) ──
    fuyao_sectors, fuyao_dragons = _build_fuyao_dragons()

    # 从Fuyao板块构建共识阶段+热度 (替代旧 ConsensusTracker)
    consensus_stages = {}
    concept_heat = {}
    for s in fuyao_sectors:
        consensus_stages[s['name']] = s['stage']
        concept_heat[s['name']] = s['heat']

    # 构建 CB→Fuyao概念 映射 (每只龙属于哪些板块)
    cb_concept_map = defaultdict(set)
    for dr in fuyao_dragons:
        cb_concept_map[dr['code']].add(dr['concept'])

    pipeline = _get_pipeline()
    pipeline.set_mainlines(consensus_stages, concept_heat)

    # 为每个CB准备概念列表: Fuyao概念优先 (直接映射+概念级交集)
    concept_map = {}
    cm = _load_concept_map()
    fuyao_names = set(consensus_stages.keys())  # 所有Fuyao板块名
    for code, info in (cm or {}).items():
        concepts = info.get('concepts', []) if isinstance(info, dict) else []
        concepts_clean = [c for c in concepts if c not in _NOISY_CONCEPTS]
        fuyao_cs = list(cb_concept_map.get(code, set()))
        extra_fuyao = [c for c in concepts_clean if c in fuyao_names and c not in fuyao_cs]
        concept_map[code] = fuyao_cs + extra_fuyao + [c for c in concepts_clean if c not in fuyao_cs and c not in extra_fuyao]

    redeem_map = getattr(state, 'redeem_map', {}) or {}
    decisions = pipeline.evaluate_batch(snapshots, concept_map, consensus_stages, redeem_map,
                                         market_state=market_ms)

    ambush = [d for d in decisions if d.action == '埋伏']
    sell = [d for d in decisions if d.action == '卖出']
    forbid = [d for d in decisions if d.action == '不做']

    # 主线 = Fuyao板块 Top 5
    main_themes = [{'name': s['name'], 'stage': s['stage'],
                    'stage_name': {0:'休眠',1:'酝酿',2:'冲锋',3:'封板',4:'扩散'}.get(s['stage'],'?'),
                    'dragon': (s['dragons'][0]['name'] if s['dragons'] else ''),
                    'dragon_pct': (s['dragons'][0]['stock_pct'] if s['dragons'] else 0),
                    'limit_up': s['zt_count']} for s in fuyao_sectors[:6]]


    forbidden_list = []
    for d in forbid[:8]:
        if d.risk_level in ('禁入', '谨慎') or d.value_score < 20:
            forbidden_list.append({
                'code': d.code, 'name': d.name,
                'reason': d.reason, 'risks': d.risk_tags,
            })

    concept_diffusion = {}
    concept_rrg = {}
    if consensus_mod.diffusion:
        concept_diffusion = consensus_mod.diffusion.get_all()
    if consensus_mod.rrg:
        concept_rrg = {c: v for c, v in consensus_mod.rrg._current.items()}

    # 龙头排行 (共识追踪器驱动)
    dragons = _get_dragons_if_available()

    # 概念板块指数快照 (Fuyao 直出)
    concept_sectors = []
    concept_sector_stats = {}
    if consensus_mod.concept_index and consensus_mod.concept_index.is_ready:
        concept_sectors = consensus_mod.concept_index.get_top_concepts(12)
        concept_sector_stats = consensus_mod.concept_index.get_stats()

    result = {
        'sentiment': sentiment,
        'sentiment_detail': sentiment_detail,
        'dragons': dragons,
        'concept_sectors': concept_sectors,
        'concept_sector_stats': concept_sector_stats,
        'main_themes': main_themes,
        'ambush_count': len(ambush),
        'sell_count': len(sell),
        'forbidden_count': len(forbidden_list),
        'ambush': [d.to_dict() for d in ambush[:5]],
        'sell': [d.to_dict() for d in sell[:5]],
        'forbidden': forbidden_list[:5],
        'diffusion': concept_diffusion,
        'rrg': concept_rrg,
        'dragon_sectors': fuyao_sectors,    # 给 /mobile 复用
        'dragon_list': fuyao_dragons,       # 给 /mobile 复用
    }

    # 写入缓存 (仅保留最近 2 个 tick, 防内存泄露)
    if cache_key:
        _battle_cache[cache_key] = result
        if len(_battle_cache) > 2:
            oldest = min(_battle_cache.keys())
            del _battle_cache[oldest]

    return result

@app.route('/')
def index():
    """仪表盘首页 — 数据直接嵌入 HTML，避免 localtunnel 拦截 JS fetch"""
    from flask import render_template
    import json as _json
    battle_data = _build_battle_data()
    state_data = state.to_dict()
    dragons_data = _get_dragons_raw()
    sentiment_data = _get_sentiment_raw()
    backtest_data = _get_backtest_raw()
    return render_template('index.html',
                           embedded_battle=_json.dumps(battle_data, ensure_ascii=False),
                           embedded_state=_json.dumps(state_data, ensure_ascii=False),
                           embedded_dragons=_json.dumps(dragons_data, ensure_ascii=False),
                           embedded_sentiment=_json.dumps(sentiment_data, ensure_ascii=False),
                           embedded_backtest=_json.dumps(backtest_data, ensure_ascii=False))


def _get_dragons_raw():
    """获取龙头原始数据 (不包装 JSON)"""
    from flask import jsonify
    resp = api_dragons()
    return resp.get_json() if hasattr(resp, 'get_json') else {}


def _get_sentiment_raw():
    """获取情绪原始数据"""
    resp = api_sentiment_detail()
    return resp.get_json() if hasattr(resp, 'get_json') else {}


def _get_backtest_raw():
    """获取复盘原始数据"""
    resp = api_backtest()
    return resp.get_json() if hasattr(resp, 'get_json') else {}


@app.route('/mobile')
def mobile():
    """手机端专用 — 4 Tab (战情/龙头/情绪/复盘), 数据 AJAX 轮询刷新

    首次加载嵌入数据实现秒开, 后续每 3 秒 fetch() API 增量刷新,
    无页面闪烁。
    """
    import json as _json
    battle_data = _build_battle_data()
    state_data = state.to_dict()

    # ── 龙头数据 (从 battle_data 中取, 与战情主线共用) ──
    dragon_sectors = battle_data.get('dragon_sectors', [])
    dragon_list = battle_data.get('dragon_list', [])

    # ── 六维情绪 ──
    sent_detail = battle_data.get('sentiment_detail', {})
    indicators = sent_detail.get('indicators', {})

    # ── 复盘后验 ──
    bt_active = backtest_tracker.get_active()
    bt_stats = backtest_tracker.get_stats()

    battle_json = _json.dumps(battle_data, ensure_ascii=False)
    dragon_json = _json.dumps({'sectors': dragon_sectors[:8], 'dragons': dragon_list[:15]}, ensure_ascii=False)
    sent_json = _json.dumps(sent_detail, ensure_ascii=False)
    bt_json = _json.dumps({'active': bt_active, 'stats': bt_stats}, ensure_ascii=False)

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>可转债作战台</title>
<style>
:root{{--bg:#0a0e17;--card:#111827;--border:#1e293b;--text:#e2e8f0;--dim:#64748b;--green:#22c55e;--red:#ef4444;--orange:#f59e0b;--blue:#3b82f6;--purple:#a855f7;--amber:#fbbf24}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);padding:10px 10px 80px;max-width:480px;margin:0 auto;-webkit-tap-highlight-color:transparent}}
/* Tab Nav */
.tab-nav{{display:flex;gap:2px;margin-bottom:8px;position:sticky;top:0;z-index:10;background:var(--bg);padding:4px 0}}
.tab-btn{{flex:1;padding:8px 6px;text-align:center;font-size:12px;font-weight:700;color:var(--dim);background:var(--card);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:.15s}}
.tab-btn.active{{background:var(--blue);color:#fff;border-color:var(--blue)}}
.tab-panel{{display:none}}
.tab-panel.active{{display:block}}

/* 共通 */
.bar{{display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--card);border-radius:10px;border:1px solid var(--border);margin-bottom:8px;font-size:12px}}
.bar .time{{font-weight:700;color:#fff;font-size:14px}}
.sentiment{{padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700}}
.sentiment.活跃{{background:rgba(34,197,94,0.2);color:var(--green)}}
.sentiment.温和{{background:rgba(245,158,11,0.2);color:var(--orange)}}
.sentiment.退潮{{background:rgba(59,130,246,0.2);color:var(--blue)}}
.sentiment.冰点{{background:rgba(100,116,139,0.3);color:var(--dim)}}
.sentiment.过热{{background:rgba(239,68,68,0.2);color:var(--red)}}
.sentiment.进攻{{background:rgba(239,68,68,0.2);color:var(--red)}}
.sentiment.震荡{{background:rgba(245,158,11,0.2);color:var(--orange)}}
.sentiment.震荡偏强{{background:rgba(251,191,36,0.2);color:var(--amber)}}
.stats-row{{display:flex;gap:6px;margin-bottom:8px}}
.stat{{flex:1;text-align:center;padding:8px 4px;background:var(--card);border-radius:8px;border:1px solid var(--border)}}
.stat .val{{font-size:18px;font-weight:800}}
.stat .lbl{{font-size:9px;color:var(--dim);margin-top:2px}}
.stat.ambush .val{{color:var(--red)}}
.stat.sell .val{{color:var(--orange)}}
.stat.forbid .val{{color:var(--dim)}}
.themes{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}}
.theme-tag{{padding:3px 8px;background:rgba(59,130,246,0.15);border:1px solid rgba(59,130,246,0.3);border-radius:12px;font-size:10px;color:var(--blue)}}
.sec-title{{font-size:12px;font-weight:700;color:var(--dim);margin:10px 0 6px;letter-spacing:.5px}}
.card{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:12px;margin-bottom:8px}}
.card.ambush{{border-left:3px solid var(--red)}}
.card.sell{{border-left:3px solid var(--orange)}}
.card.forbid{{border-left:3px solid var(--dim);opacity:.8}}
.card .head{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
.card .action-tag{{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:800;color:#fff}}
.card.ambush .action-tag{{background:var(--red)}}
.card.sell .action-tag{{background:var(--orange)}}
.card.forbid .action-tag{{background:var(--dim)}}
.card .name{{font-size:15px;font-weight:700;color:#fff}}
.card .code{{font-size:11px;color:var(--dim)}}
.card .concept{{font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(59,130,246,0.15);color:var(--blue)}}
.card .reason{{font-size:11px;color:var(--text);line-height:1.5;margin-bottom:6px;padding:6px 8px;background:rgba(255,255,255,0.03);border-radius:6px}}
.card .data-row{{display:flex;gap:12px;font-size:11px;margin-bottom:4px;flex-wrap:wrap}}
.card .data-item{{color:var(--dim)}}
.card .data-item span{{color:#fff;font-weight:600}}
.card .data-item.up{{color:var(--red)}}
.card .data-item.down{{color:var(--green)}}
.card .footer{{display:flex;gap:12px;font-size:9px;color:var(--dim);margin-top:6px;padding-top:6px;border-top:1px solid var(--border);flex-wrap:wrap}}
.card .footer .risk{{color:var(--orange)}}
.chain{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:10px 12px;margin-bottom:8px}}
.chain .line{{font-size:11px;padding:3px 0;display:flex;align-items:center;gap:6px}}
.chain .line .dot{{width:6px;height:6px;border-radius:50%;flex-shrink:0}}
.chain .line .dot.s2{{background:var(--red)}}
.chain .line .dot.s3{{background:var(--orange)}}
.chain .line .dot.s4{{background:var(--blue)}}
.chain .line .dot.s5{{background:var(--purple)}}
.forbid-list{{font-size:10px;color:var(--dim)}}
.forbid-list .item{{padding:4px 8px;margin-bottom:2px;background:rgba(239,68,68,0.05);border-radius:4px;border-left:2px solid var(--red)}}

/* 龙头 Tab */
.sector-card{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:0;margin-bottom:8px;overflow:hidden}}
.sector-card.hot{{border-color:var(--red);box-shadow:0 0 12px rgba(239,68,68,0.06)}}
.sector-header{{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.02)}}
.sector-header .sector-title{{font-size:13px;font-weight:700;color:#fff;flex:1}}
.sector-header .stage-badge{{font-size:9px;padding:2px 6px;border-radius:10px;font-weight:700;white-space:nowrap}}
.stage-badge.s0{{background:rgba(100,116,139,0.2);color:var(--dim)}}
.stage-badge.s1{{background:rgba(59,130,246,0.15);color:var(--blue)}}
.stage-badge.s2{{background:rgba(245,158,11,0.15);color:var(--orange)}}
.stage-badge.s3{{background:rgba(239,68,68,0.15);color:var(--red)}}
.stage-badge.s4{{background:rgba(168,85,247,0.15);color:var(--purple)}}
.sector-header .zt-tag{{font-size:9px;padding:2px 5px;border-radius:4px;background:rgba(239,68,68,0.12);color:var(--red)}}
.sector-header .heat-tag{{font-size:9px;color:var(--amber)}}
.dragon-row{{display:flex;align-items:center;gap:6px;padding:8px 12px;font-size:11px;border-bottom:1px solid rgba(30,41,59,0.3)}}
.dragon-row:last-child{{border-bottom:none}}
.dragon-row .dr-rank{{width:16px;height:16px;display:flex;align-items:center;justify-content:center;border-radius:3px;font-size:9px;font-weight:800;color:#000}}
.dragon-row .dr-rank.龍1{{background:var(--amber)}}
.dragon-row .dr-rank.龍2{{background:var(--dim);color:#fff}}
.dragon-row .dr-rank.龍3{{background:rgba(205,127,50,0.6);color:#fff}}
.dragon-row .dr-name{{flex:1;font-size:12px;font-weight:600;color:#fff}}
.dragon-row .dr-code{{font-size:10px;color:var(--dim);width:48px}}
.dragon-row .dr-numbers{{display:flex;flex-direction:column;align-items:flex-end;gap:2px}}
.dragon-row .dr-stk{{font-size:12px;font-weight:700}}
.dragon-row .dr-stk.up{{color:var(--red)}}
.dragon-row .dr-stk.dn{{color:var(--green)}}
.dragon-row .dr-cb2{{font-size:10px;color:var(--dim)}}
.dragon-row .dr-badge{{font-size:8px;padding:1px 4px;border-radius:3px;margin-left:4px}}
.dragon-row .dr-badge.sync{{background:rgba(34,197,94,0.12);color:var(--green)}}
.dragon-row .dr-badge.lag{{background:rgba(239,68,68,0.12);color:var(--red)}}
.dragon-row .dr-badge.hi{{background:rgba(245,158,11,0.12);color:var(--orange)}}
.dragon-row .dr-badge.rel{{background:rgba(59,130,246,0.12);color:var(--blue)}}
.dragon-row .dr-badge.nodata{{background:rgba(100,116,139,0.12);color:var(--dim)}}
.dragon-row .dr-stock-name{{font-size:9px;color:var(--dim);margin:0 2px}}
.dragon-row .dr-seal{{font-size:9px;color:var(--amber);margin-left:4px}}
.sector-sub{{font-size:9px;color:var(--dim);margin:-2px 0 6px 0;padding-left:2px}}


/* 情绪 Tab */
.indicator-card{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:10px 12px;margin-bottom:8px}}
.indicator-card .ind-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}}
.indicator-card .ind-name{{font-size:12px;font-weight:700;color:var(--text)}}
.indicator-card .ind-val{{font-size:14px;font-weight:800;color:#fff}}
.indicator-card .ind-bar{{height:6px;background:rgba(255,255,255,0.05);border-radius:3px;overflow:hidden;margin:4px 0}}
.indicator-card .ind-fill{{height:100%;border-radius:3px;transition:width .3s}}
.ind-fill.high{{background:var(--green)}}
.ind-fill.mid{{background:var(--orange)}}
.ind-fill.low{{background:var(--red)}}
.sent-grid{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}
.phase-info{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:10px 12px;margin-bottom:8px}}
.phase-info .phase-tag{{font-size:16px;font-weight:800;padding:4px 12px;border-radius:8px;display:inline-block;margin-bottom:6px}}
.phase-info .phase-advice{{font-size:12px;color:var(--dim);line-height:1.6}}

/* 复盘 Tab */
.bt-row{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:10px 12px;margin-bottom:6px}}
.bt-row .bt-top{{display:flex;align-items:center;gap:8px;margin-bottom:4px}}
.bt-row .bt-lv{{padding:2px 6px;border-radius:3px;font-size:10px;font-weight:800;color:#fff}}
.bt-row .bt-lv.A{{background:var(--red)}}
.bt-row .bt-lv.B{{background:var(--orange)}}
.bt-row .bt-lv.C{{background:var(--blue)}}
.bt-row .bt-name{{font-size:13px;font-weight:700;color:#fff}}
.bt-row .bt-code{{font-size:10px;color:var(--dim)}}
.bt-row .bt-strat{{font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(168,85,247,0.15);color:var(--purple)}}
.bt-row .bt-info{{display:flex;gap:12px;font-size:10px;flex-wrap:wrap}}
.bt-row .bt-info span{{color:var(--dim)}}
.bt-row .bt-info b{{color:#fff}}
.bt-row .bt-pnl{{font-size:12px;font-weight:800;margin-left:auto}}
.bt-row .bt-peak{{font-size:9px;color:var(--dim)}}
.bt-cps{{display:flex;gap:4px;margin-top:4px;flex-wrap:wrap}}
.bt-cps .cp{{font-size:8px;padding:1px 4px;border-radius:3px;background:rgba(255,255,255,0.05);color:var(--dim)}}

.refresh-info{{text-align:center;font-size:9px;color:var(--dim);margin-top:12px;padding:8px}}
.empty{{text-align:center;padding:30px;color:var(--dim);font-size:13px}}
</style>
</head>
<body>
<!-- Tab Nav -->
<div class="tab-nav">
<div class="tab-btn active" onclick="switchTab(0)">战情</div>
<div class="tab-btn" onclick="switchTab(1)">龙头</div>
<div class="tab-btn" onclick="switchTab(2)">情绪</div>
<div class="tab-btn" onclick="switchTab(3)">复盘</div>
</div>

<!-- 战情 Tab -->
<div class="tab-panel active" id="tab0">
<div class="bar"><span class="time" id="t">--:--:--</span><span class="sentiment" id="sent">--</span></div>
<div class="stats-row">
<div class="stat ambush"><div class="val" id="ambush">-</div><div class="lbl">埋伏</div></div>
<div class="stat sell"><div class="val" id="sell">-</div><div class="lbl">卖出</div></div>
<div class="stat forbid"><div class="val" id="forbid">-</div><div class="lbl">禁入</div></div>
</div>
<div class="themes" id="themes"></div>
<div class="sec-title">埋伏</div><div id="ac"></div>
<div class="sec-title">卖出提醒</div><div id="sc"></div>
<div class="sec-title">禁入</div><div id="fb"></div>
</div>

<!-- 龙头 Tab -->
<div class="tab-panel" id="tab1"><div id="dragons-content"></div></div>

<!-- 情绪 Tab -->
<div class="tab-panel" id="tab2"><div id="sentiment-content"></div></div>

<!-- 复盘 Tab -->
<div class="tab-panel" id="tab3"><div id="backtest-content"></div></div>

<div class="refresh-info"><span id="refresh-status">就绪 | 已刷新 0 次</span></div>

<script>
// ── Data ──
var BATTLE={battle_json};
var DRAGONS={dragon_json};
var SENTIMENT={sent_json};
var BACKTEST={bt_json};
var CUR_TAB=0;

function e(s){{return String(s||'').replace(/</g,'&lt;').replace(/>/g,'&gt;')}}
function pc(v,d){{return v!=null&&v!==undefined?v:d}}
function pm(v,d){{return(v!=null&&v!==undefined?Number(v):d).toFixed(1)}}

// ── 战情渲染 ──
function renderBattle(){{
var D=BATTLE,s=D.sentiment||{{}};
document.getElementById('t').textContent=new Date().toLocaleTimeString('zh-CN',{{hour12:false}});
var sb=document.getElementById('sent');sb.textContent=s.status||'--';sb.className='sentiment '+(s.status||'');
document.getElementById('ambush').textContent=D.ambush_count||0;
document.getElementById('sell').textContent=D.sell_count||0;
document.getElementById('forbid').textContent=D.forbidden_count||0;
document.getElementById('themes').innerHTML=(D.main_themes||[]).map(function(t){{return'<span class="theme-tag">'+e(t.name)+' '+e(t.stage_name||'')+'</span>'}}).join('')||'<span style="font-size:10px;color:var(--dim)">无</span>';
rc('ac',D.ambush||[]);rc('sc',D.sell||[]);
var fb=document.getElementById('fb');
fb.innerHTML=(D.forbidden||[]).map(function(f){{return'<div class="forbid-list"><div class="item">'+e(f.name)+'('+f.code+') - '+e(f.reason)+' '+((f.risks||[]).map(function(r){{return e(r)}}).join(' '))+'</div></div>'}}).join('')||'<div class="empty">暂无</div>';
}}

function rc(id,cards){{
var el=document.getElementById(id);
if(!cards||!cards.length){{el.innerHTML='<div class="empty">暂无</div>';return}}
el.innerHTML=cards.map(function(c){{var cls=c.action==='埋伏'?'ambush':c.action==='卖出'?'sell':'forbid';return'<div class="card '+cls+'"><div class="head"><span class="action-tag">'+c.action+'</span><span class="name">'+e(c.name)+'</span><span class="code">'+c.code+'</span>'+(c.concept?'<span class="concept">'+e(c.concept)+'</span>':'')+'</div><div class="reason">'+e(c.reason||'')+'</div><div class="data-row"><span class="data-item '+(c.cb_pct>=0?'up':'down')+'">转债 <span>'+(c.cb_pct>0?'+':'')+(c.cb_pct||0).toFixed(2)+'%</span></span><span class="data-item '+(c.stock_pct>=0?'up':'down')+'">正股 <span>'+(c.stock_pct>0?'+':'')+(c.stock_pct||0).toFixed(2)+'%</span></span><span class="data-item">溢价 <span>'+(c.premium||0).toFixed(1)+'%</span></span><span class="data-item">成交 <span>'+(c.amount||0).toFixed(2)+'亿</span></span></div><div class="footer"><span>'+e(c.buyer||'')+'</span><span>'+e(c.hold_time||'')+'</span><span>止损 '+(c.stop_loss_pct||0)+'%</span><span>止盈 +'+(c.take_profit_pct||0)+'%</span>'+(c.invalid_if?'<span class="risk">'+e(c.invalid_if)+'</span>':'')+((c.risk_tags||[]).map(function(r){{return'<span class="risk">'+e(r)+'</span>'}}).join(''))+'</div></div>'}}).join('')
}}

// ── 龙头渲染 ──
function renderDragons(){{
var D=DRAGONS,el=document.getElementById('dragons-content');
var sectors=D.sectors||[],dragons=D.dragons||[];
if(!sectors.length&&!dragons.length){{el.innerHTML='<div class="empty">暂无活跃概念</div>';return}}
// KPL fallback data → skip
var isKPL=dragons.length>0&&dragons[0].stock_pct===undefined;
if(isKPL){{el.innerHTML='<div class="empty">等待交易数据<br><span style="font-size:10px;color:var(--dim)">盘中开盘后自动切换</span></div>';return}}
var STAGES={{0:'休眠',1:'酝酿',2:'冲锋',3:'封板',4:'扩散'}};
var html='';
for(var i=0;i<sectors.length;i++){{
var s=sectors[i],hot=s.heat>=20?' hot':'';
html+='<div class="sector-card'+hot+'">';
html+='<div class="sector-header">';
html+='<span class="sector-title">'+e(s.name)+'</span>';
html+='<span class="stage-badge s'+s.stage+'">'+STAGES[s.stage||0]+'</span>';
if(s.zt_count)html+='<span class="zt-tag">涨停'+s.zt_count+'</span>';
html+='<span class="heat-tag">'+s.heat+'</span>';
html+='</div>';
var drs=s.dragons||[];
// 显示板块内涨停股数 vs CB数
html+='<div class="sector-sub">'+s.zt_count+'只涨停 · '+drs.length+'只CB</div>';
for(var j=0;j<Math.min(drs.length,3);j++){{
var dr=drs[j],rks={{1:'龍1',2:'龍2',3:'龍3'}};
var badge='',bcls='';
if(dr.label==='相关'){{badge='相关';bcls='rel'}}
else if(dr.label==='无行情'){{badge='无行情';bcls='nodata'}}
else if(dr.label==='滞后'){{badge='滞后';bcls='lag'}}
else if(dr.label==='高溢价'){{badge='高溢价';bcls='hi'}}
else if(dr.label==='同步'){{badge='同步';bcls='sync'}}
html+='<div class="dragon-row">';
html+='<span class="dr-rank '+rks[dr.rank||0]+'">'+(rks[dr.rank]||dr.rank)+'</span>';
// CB名称 + 原始涨停股名
html+='<span class="dr-name">'+e(dr.name)+'</span>';
if(dr.stock_name&&dr.stock_name!==dr.name)html+='<span class="dr-stock-name">'+e(dr.stock_name)+'</span>';
html+='<span class="dr-code">'+dr.code+'</span>';
// 封板信息 (直连CB才有)
if(dr.seal_time)html+='<span class="dr-seal">'+dr.seal_time+' '+(dr.seal_yi||0)+'亿</span>';
html+='<div class="dr-numbers">';
html+='<span class="dr-stk '+(dr.stock_pct>=0?'up':'dn')+'">'+(dr.stock_pct>0?'+':'')+pm(dr.stock_pct,0)+'%</span>';
if(dr.cb_pct!==undefined)html+='<span class="dr-cb2">转债'+(dr.cb_pct>0?'+':'')+pm(dr.cb_pct,0)+'%</span>';
html+='</div>';
if(badge)html+='<span class="dr-badge '+bcls+'">'+badge+'</span>';
html+='</div>';
}}
html+='</div>';
}}
el.innerHTML=html;
}}

// ── 情绪渲染 ──
function renderSentiment(){{
var D=SENTIMENT,el=document.getElementById('sentiment-content');
var ind=D.indicators||{{}},html='';
var phaseCn=D.phase_cn||'--',advice=D.advice||'',source=D.source||'';
html+='<div class="phase-info"><span class="phase-tag sentiment '+phaseCn+'">'+phaseCn+'</span><div class="phase-advice">'+e(advice)+'</div><div style="font-size:9px;color:var(--dim);margin-top:4px">'+e(source)+'</div></div>';

// ── 今日盘中 ──
html+='<div class="sec-title">今日盘中  '+(source.indexOf('fuyao')>=0?'Fuyao':source)+'</div><div class="sent-grid">';
var todayKeys=[
{{key:'limit_up',name:'涨停',max:80,unit:'只',fmt:'int',today:1}},
{{key:'first_board',name:'首板',max:100,unit:'只',fmt:'int',today:1}},
{{key:'second_board',name:'二板',max:30,unit:'只',fmt:'int',today:1}},
{{key:'third_board',name:'三板',max:15,unit:'只',fmt:'int',today:1}},
{{key:'height_board',name:'高度板',max:10,unit:'只',fmt:'int',today:1}},
{{key:'promotion_rate',name:'晋级率',max:50,unit:'%',fmt:'pct',today:1}},
{{key:'pool_up',name:'转债池涨',max:80,unit:'只',fmt:'int',today:1}},
{{key:'pool_down',name:'转债池跌',max:80,unit:'只',fmt:'int',today:1}},
{{key:'pool_ratio',name:'涨跌比',max:3,unit:'',fmt:'num',today:1}},
{{key:'up_count',name:'全市场涨',max:5000,unit:'只',fmt:'int',today:1}},
{{key:'down_count',name:'全市场跌',max:5000,unit:'只',fmt:'int',today:1}},
{{key:'limit_down',name:'跌停',max:100,unit:'只',fmt:'int',today:1}},
];
var hasPool=(ind.pool_up||0)+(ind.pool_down||0)>0;
for(var i=0;i<todayKeys.length;i++){{
var ki=todayKeys[i],v=ind[ki.key];
if(v===undefined||v===null)continue;
var dn=ki.name;
if(ki.fmt==='pct')dn=v>=0?dn+' +':dn+' ';
var pct=Math.min(100,Math.max(0,(Math.abs(v||0)/ki.max*100)));
var cls=pct>70?'high':pct>35?'mid':'low';
var label=(ki.fmt==='pct'?(v>0?'+':'')+pm(v,0)+ki.unit:(ki.fmt==='num'?pm(v,2):(v||0)));
// 池数据: 标注来源
var subtitle='';
if(ki.key==='pool_up'||ki.key==='pool_down'||ki.key==='pool_ratio'){{
  if(!hasPool){{label='--';subtitle='午休'}}
  else subtitle='池内';
}}
html+='<div class="indicator-card"><div class="ind-head"><span class="ind-name">'+dn+(subtitle?'<span style="font-size:8px;color:var(--dim);margin-left:2px">'+subtitle+'</span>':'')+'</span><span class="ind-val">'+label+'</span></div><div class="ind-bar"><div class="ind-fill '+cls+'" style="width:'+pct+'%"></div></div></div>';
}}
html+='</div>';

// ── 昨日参考 (KPL) ──
html+='<div class="sec-title" style="margin-top:12px">昨日参考  KPL</div><div class="sent-grid">';
var ydayKeys=[
{{key:'seal_success_rate',name:'封板成功率',max:100,unit:'%',fmt:'pct',today:0}},
{{key:'blast_ratio',name:'炸板率',max:50,unit:'%',fmt:'pct',today:0}},
{{key:'yesterday_zt_perf',name:'昨涨停表现',max:10,unit:'%',fmt:'pct',today:0}},
{{key:'yesterday_lb_perf',name:'昨连板表现',max:15,unit:'%',fmt:'pct',today:0}},
{{key:'yesterday_break_perf',name:'昨炸板表现',max:5,unit:'%',fmt:'pct',today:0}},
];
for(var i=0;i<ydayKeys.length;i++){{
var ki=ydayKeys[i],v=ind[ki.key];
if(v===undefined||v===null)continue;
var dn=ki.name;
if(ki.fmt==='pct')dn=v>=0?dn+' +':dn+' ';
var pct=Math.min(100,Math.max(0,(Math.abs(v||0)/ki.max*100)));
var cls=pct>70?'high':pct>35?'mid':'low';
html+='<div class="indicator-card" style="opacity:0.75"><div class="ind-head"><span class="ind-name" style="font-size:10px">'+dn+'</span><span class="ind-val" style="font-size:12px">'+(ki.fmt==='pct'?(v>0?'+':'')+pm(v,0)+ki.unit:(ki.fmt==='num'?pm(v,2):(v||0)))+'</span></div><div class="ind-bar"><div class="ind-fill '+cls+'" style="width:'+pct+'%"></div></div></div>';
}}
html+='</div>';
el.innerHTML=html;
}}

// ── 复盘渲染 ──
function renderBacktest(){{
var D=BACKTEST,el=document.getElementById('backtest-content');
var active=D.active||[],stats=D.stats||{{}};
if(!active.length){{el.innerHTML='<div class="empty">暂无复盘信号</div>';return}}
var html='';
// Stats summary
var win=0,loss=0;
for(var i=0;i<active.length;i++){{if((active[i].current_pnl||0)>0)win++;else loss++}}
html+='<div class="stats-row"><div class="stat ambush"><div class="val">'+active.length+'</div><div class="lbl">追踪信号</div></div><div class="stat sell"><div class="val" style="color:var(--green)">'+win+'</div><div class="lbl">浮盈</div></div><div class="stat forbid"><div class="val" style="color:var(--red)">'+loss+'</div><div class="lbl">浮亏</div></div></div>';

for(var i=0;i<active.length;i++){{
var a=active[i],pnl=a.current_pnl||0,cpnls=[];
var cps=a.checkpoints||[];
for(var j=0;j<cps.length;j++){{
var cp=cps[j],cpnl=cp.current_pnl||0;cpnls.push('+'+(cpnl>0?'+':'')+pm(cpnl,1)+'%');
}}
html+='<div class="bt-row"><div class="bt-top"><span class="bt-lv '+a.level+'">'+a.level+'</span><span class="bt-name">'+e(a.name)+'</span><span class="bt-code">'+a.code+'</span><span class="bt-strat">'+(a.strategy||'')+'</span><span class="bt-pnl '+(pnl>=0?'up':'down')+'">'+(pnl>0?'+':'')+pm(pnl,1)+'%</span></div><div class="bt-info"><span>触发: <b>'+pm(a.trigger_price,2)+'</b></span><span>峰值: <b>'+pm(a.peak_price,2)+'</b></span><span>CP: <b>'+cps.length+'</b></span></div>'+(cpnls.length?'<div class="bt-cps">'+cpnls.map(function(c){{return'<span class="cp">'+c+'</span>'}}).join('')+'</div>':'')+'</div>';
}}
el.innerHTML=html;
}}

// ── Tab 切换 ──
function switchTab(idx){{
CUR_TAB=idx;
var btns=document.querySelectorAll('.tab-btn');
for(var i=0;i<btns.length;i++)btns[i].classList.toggle('active',i===idx);
var panels=document.querySelectorAll('.tab-panel');
for(var j=0;j<panels.length;j++)panels[j].classList.toggle('active',j===idx);
}}

// ── 数据拉取 ──
var FETCH_COUNT=0;
function fetchData(){{
// 并行拉取 4 个 API
Promise.all([
fetch('/api/battle').then(function(r){{return r.json()}}),
fetch('/api/dragons').then(function(r){{return r.json()}}),
fetch('/api/sentiment_detail').then(function(r){{return r.json()}}),
fetch('/api/backtest').then(function(r){{return r.json()}})
]).then(function(results){{
BATTLE=results[0];
DRAGONS=results[1];
SENTIMENT=results[2];
BACKTEST=results[3];
renderBattle();
renderDragons();
renderSentiment();
renderBacktest();
FETCH_COUNT++;
document.getElementById('refresh-status').textContent='已刷新 '+FETCH_COUNT+' 次 | '+new Date().toLocaleTimeString('zh-CN',{{hour12:false}});
}}).catch(function(e){{
document.getElementById('refresh-status').textContent='刷新失败: '+e.message;
}})
}}

// ── Init ──
renderBattle();
renderDragons();
renderSentiment();
renderBacktest();
setInterval(function(){{document.getElementById('t').textContent=new Date().toLocaleTimeString('zh-CN',{{hour12:false}})}},1000);
setInterval(fetchData,3000);
fetchData();
</script>
</body>
</html>'''


@app.route('/api/pending')
def api_pending():
    """信号触发后跟踪: 最近N条信号触发价→现价→峰值→盈亏"""
    n = request.args.get('n', 10, type=int)
    return jsonify(state.get_pending_top(n))


@app.route('/api/backtest')
def api_backtest():
    """后验统计: 当日信号追踪汇总 + 已完成检查点统计"""
    return jsonify({
        'active': backtest_tracker.get_active(),
        'stats': backtest_tracker.get_stats(),
    })


@app.route('/api/consensus')
def api_consensus():
    """共识阶段: 所有概念的共识阶段 (0-7) + Top活跃概念"""
    if not consensus_mod.consensus_tracker:
        return jsonify({'top': [], 'error': '共识追踪器未初始化'})
    return jsonify({
        'top': consensus_mod.consensus_tracker.get_top_consensus(15),
        'all': consensus_mod.consensus_tracker.get_all(),
    })


@app.route('/api/battle')
def api_battle():
    """作战台: 主线→正股→转债→风险 4 阶段管道 → 埋伏/卖出/不做"""
    return jsonify(_build_battle_data())


def _get_sentiment_detail(snapshots: dict) -> dict:
    """获取六维情绪详情 (给 API 和嵌入数据用) — 盘中 Fuyao 优先"""
    engine = _get_sentiment_engine()

    # 从 Fuyao 缓存读取 (调度器每5s刷新, 避免API端点429限流)
    pool_cache = state.get_fuyao_pool()
    ladder_cache = state.get_fuyao_ladder()
    fuyao_items = pool_cache.get('items') if pool_cache.get('fresh') else None
    fuyao_ladder = ladder_cache.get('data') if ladder_cache.get('fresh') else None

    intraday = engine.evaluate_intraday_full(
        snapshots,
        market_state=None,
        fuyao_pool_items=fuyao_items,
        fuyao_ladder_data=fuyao_ladder,
    )

    try:
        kpl = _get_kpl()
        if kpl:
            posthoc = engine.evaluate_from_kpl()
            merged = engine.merge(intraday, posthoc)
            return merged
    except Exception:
        pass

    return intraday


def _get_dragons_if_available() -> list:
    """获取概念板块龙头 — 共识驱动 (含板块效应评分)

    不再机械取 Top 8, 改为:
    1. 从共识追踪器取活跃概念 (stage ≥ 1) 及其龙头
    2. 计算概念热度分: 涨停数×3 + 跟班数×2 + 龙头涨幅×1 + 池内债数×0.3
    3. Fuyao 涨停池补充连板/封单/涨停时间/原因明细
    4. 动态截断: 冷市保留少, 热市保留多 (3~15只)
    """
    dragons = []

    # ── Step 1: 从共识追踪器提取活跃概念及其龙头 ──
    if consensus_mod.consensus_tracker:
        all_states = consensus_mod.consensus_tracker.get_all()
        for concept, st in all_states.items():
            stage = st.get('stage', 0)
            if stage < 1:  # 沉寂概念跳过
                continue

            limit_up = st.get('limit_up_count', 0)
            followers = st.get('follower_count', 0)
            d_sc = st.get('dragon_sc_pct', 0)
            cb_pool = st.get('cb_pool_stocks', 0)
            full_mkt = st.get('full_market_stocks', 0)
            total_bonds = st.get('total_bonds', 0) or (cb_pool + full_mkt)

            # 概念热度分: 有队伍的龙 >> 孤狼
            heat = (limit_up * 3.0 +
                    followers * 2.0 +
                    min(d_sc, 10) * 1.0 +
                    min(total_bonds, 15) * 0.3)

            dragons.append({
                'rank': 0,  # 第二步动态分配
                'concept': concept,
                'stage': stage,
                'stage_name': st.get('stage_name', ''),
                'heat': round(heat, 1),
                'name': st.get('dragon_name', ''),
                'code': st.get('dragon_code', ''),
                'dragon_sc_pct': d_sc,
                'dragon_cb_pct': st.get('dragon_cb_pct', 0),
                'limit_up_count': limit_up,
                'follower_count': followers,
                'total_bonds': total_bonds,
                'leader_match': st.get('leader_match'),
                'consecutive': 0,   # Fuyao 补充
                'seal_money_yi': 0,
                'limit_up_time': '',
                'reason': '',
                'total_score': heat,  # 兼容旧模板
                'sector': concept[:12],
            })

    # ── Step 2: Fuyao 涨停池补充连板/封单/涨停原因 (读缓存) ──
    if dragons:
        pool_cache = state.get_fuyao_pool()
        items = pool_cache.get('items', [])
        if items:
            fuyao_map = {}
            for s in items:
                t = str(s.get("ticker", ""))
                n = str(s.get("name", ""))
                if t:
                    fuyao_map[t] = s
                if n:
                    fuyao_map[n] = s

                for d in dragons:
                    f = fuyao_map.get(str(d['code']),
                                      fuyao_map.get(str(d['name']), {}))
                    if f:
                        d['consecutive'] = f.get('continue_day_cnt', 0) or 0
                        d['seal_money_yi'] = round(
                            (f.get('seal_money', 0) or 0) / 100000000, 2)
                        d['limit_up_time'] = f.get('limit_up_time', '')
                        d['reason'] = (f.get('limit_up_reason', '') or '')[:40]
                        d['sector'] = (f.get('limit_up_reason', '') or
                                       '').split("+")[0][:12]
                        # 连板加分
                        d['heat'] += d['consecutive'] * 1.5
                        d['total_score'] = d['heat']

    # ── Step 3: 按概念热度排序 + 动态截断 ──
    dragons.sort(key=lambda d: -d['heat'])

    if not dragons:
        # 共识追踪器无数据时回退 KPL
        try:
            kpl = _get_kpl()
            if kpl:
                sectors = kpl.get_limit_up_sectors()
                if sectors and sectors.get('sectors'):
                    ranker = _get_dragon_ranker()
                    ranked = ranker.rank_all_sectors(sectors)
                    return ranker.get_top_dragons(ranked, top_n=8)
        except Exception:
            pass
        return []

    total = len(dragons)
    if total <= 5:
        result = dragons
    elif total <= 10:
        result = [d for d in dragons if d['heat'] >= 5.0]
        if len(result) < 3:
            result = dragons[:3]
    else:
        result = [d for d in dragons if d['heat'] >= 8.0]
        if len(result) < 3:
            result = dragons[:3]
        if len(result) > 15:
            result = dragons[:15]

    # 分配最终 rank
    for i, d in enumerate(result):
        d['rank'] = i + 1

    return result


def _get_main_themes(tracker) -> list[dict]:
    """当前主线概念 (共识阶段 >= 2)"""
    active = tracker.get_concepts_by_stage(min_stage=2)
    themes = []
    for item in active[:5]:
        themes.append({
            'name': item['concept'],
            'stage': item['stage'],
            'stage_name': item['stage_name'],
            'dragon': item['dragon_name'],
            'dragon_pct': item['dragon_sc_pct'],
            'limit_up': item['limit_up_count'],
        })
    return themes


def _get_concept_chain(tracker) -> list[dict]:
    """概念传导链: 活跃概念 + 龙一龙二龙三"""
    active = tracker.get_concepts_by_stage(min_stage=1)[:5]
    chain = []
    for item in active:
        chain.append({
            'concept': item['concept'],
            'stage': item['stage'],
            'dragon': f"{item['dragon_name']} +{item['dragon_sc_pct']:.1f}%",
            'followers': item['follower_count'],
            'limit_up': item['limit_up_count'],
            'dragons': item.get('dragons', []),  # 龙一龙二龙三完整明细
        })
    return chain


def _get_enriched_chain(tracker) -> list[dict]:
    """增强传导链: dragon 1/2/3 + CB状态标签"""
    return _get_concept_chain(tracker)


@app.route('/api/state')
def api_state():
    """返回当前状态 JSON"""
    return jsonify(state.to_dict())


@app.route('/api/concepts')
def api_concepts():
    """概念热度排行: 按转债涨跌幅聚合, 返回 Top 概念板块"""
    concept_map = _load_concept_map()
    if not concept_map:
        return jsonify({'concepts': [], 'error': '概念数据不可用'})

    with state._lock:
        snapshots = dict(state.snapshots)

    if not snapshots:
        return jsonify({'concepts': [], 'error': '暂无行情数据'})

    # 按概念聚合: concept_name -> {total_pct, count, surge_count, top_bonds}
    concept_agg = defaultdict(lambda: {'total_pct': 0.0, 'count': 0, 'surge_count': 0, 'top_bonds': []})

    for code, snap in snapshots.items():
        info = concept_map.get(code)
        if not info:
            continue
        pct = getattr(snap, 'change_pct', 0) or 0
        name = getattr(snap, 'name', '') or info.get('name', '')
        concepts = info.get('concepts', [])
        for c in concepts:
            agg = concept_agg[c]
            agg['total_pct'] += pct
            agg['count'] += 1
            if pct >= 2:
                agg['surge_count'] += 1
            agg['top_bonds'].append({'code': code, 'name': name, 'pct': round(pct, 2)})

    # 计算得分: avg_pct 为主 + surge_ratio 为辅
    results = []
    for name, agg in concept_agg.items():
        if agg['count'] < 3:  # 至少3只债才纳入
            continue
        avg_pct = agg['total_pct'] / agg['count']
        surge_ratio = agg['surge_count'] / agg['count']
        score = avg_pct * 0.7 + surge_ratio * 100 * 0.3
        # Top 5 涨幅债
        top = sorted(agg['top_bonds'], key=lambda x: -x['pct'])[:5]
        results.append({
            'name': name,
            'avg_pct': round(avg_pct, 2),
            'bond_count': agg['count'],
            'surge_count': agg['surge_count'],
            'score': round(score, 1),
            'top_bonds': top,
        })

    # 按 score 排序取 Top 12
    results.sort(key=lambda x: -x['score'])
    hot = results[:12]

    # 附加 Fuyao 概念指数数据 (如果有)
    fuyao_concept = {}
    if consensus_mod.concept_index and consensus_mod.concept_index.is_ready:
        for item in hot:
            name = item['name']
            pct = consensus_mod.concept_index.get_concept_change(name)
            if pct:
                fuyao_concept[name] = round(pct, 2)
                item['index_pct'] = round(pct, 2)  # 概念指数真实涨跌

    return jsonify({
        'concepts': hot,
        'total_concepts': len(concept_agg),
        'fuyao_index': fuyao_concept if fuyao_concept else None,
    })


@app.route('/api/concept_signals')
def api_concept_signals():
    """以概念为主线聚合信号: 每只债归到最热概念, 按概念均涨排序
    数据源: 当前轮信号 + signal_history (120s窗口, 始终去重)
    """
    concept_map = _load_concept_map()
    with state._lock:
        signals_raw = list(state.signals)
        snapshots = dict(state.snapshots)
        history = list(state.signal_history)
        now_ts = time.time()

    # 始终从 history 补充 (120s窗口), 去重保留最高等级
    seen = {(s.code, s.level) for s in signals_raw}
    for h in reversed(history):
        ts = h.get('_ts', 0)
        if now_ts - ts > 120:
            continue
        code = h.get('code', '')
        level = h.get('level', '')
        key = (code, level)
        if key in seen:
            continue
        seen.add(key)
        from core.signal_engine import Signal
        s = Signal(
            level=level,
            signal_type=h.get('type', ''),
            code=code,
            name=h.get('name', ''),
            stock_name='',
            description=h.get('desc', ''),
            score=h.get('score', 0),
            timestamp=ts,
        )
        signals_raw.append(s)

    if not concept_map:
        return jsonify({'groups': [], 'ungrouped': _signal_dicts(signals_raw, snapshots)})

    # 1. 实时概念热度 + 每概念下所有债
    concept_heat: dict[str, float] = {}
    concept_pcts: dict[str, list[float]] = defaultdict(list)
    concept_bonds: dict[str, list[dict]] = defaultdict(list)  # concept → [{code, name, price, pct}, ...]
    for code, snap in snapshots.items():
        info = concept_map.get(code)
        if not info:
            continue
        pct = getattr(snap, 'change_pct', 0) or 0
        price = getattr(snap, 'trade', 0) or 0
        name = getattr(snap, 'name', '') or info.get('name', '')
        for c in info.get('concepts', []):
            concept_pcts[c].append(pct)
            concept_bonds[c].append({
                'code': code, 'name': name,
                'price': round(price, 2), 'pct': round(pct, 2),
            })
    for c, pcts in concept_pcts.items():
        if pcts:
            concept_heat[c] = round(sum(pcts) / len(pcts), 2)

    # 2. 每只信号债 → 最热概念
    signal_codes = {s.code for s in signals_raw}
    signal_concepts: dict[str, dict] = {}
    for code in signal_codes:
        info = concept_map.get(code)
        concepts = info.get('concepts', []) if info else []
        if not concepts:
            continue
        best = max(concepts, key=lambda c: concept_heat.get(c, 0))
        signal_concepts[code] = {
            'concept': best,
            'avg_pct': concept_heat.get(best, 0),
            'bond_count': len(concept_pcts.get(best, [])),
        }

    # 3. 板块级增强: 正股涨停计数
    concept_limit_ups: dict[str, int] = defaultdict(int)
    for code, snap in snapshots.items():
        info = concept_map.get(code)
        if not info:
            continue
        sc_pct = getattr(snap, 'stock_change_pct', None)
        if sc_pct is not None and sc_pct >= 9.5:
            for c in info.get('concepts', []):
                concept_limit_ups[c] += 1

    # 4. 按概念分组 (含该概念下所有债)
    grouped: dict[str, dict] = defaultdict(lambda: {'signals': [], 'bonds': [], 'avg_pct': 0, 'bond_count': 0})
    ungrouped = []

    for sig in signals_raw:
        sc = signal_concepts.get(sig.code)
        if sc:
            g = grouped[sc['concept']]
            g['avg_pct'] = sc['avg_pct']
            g['bond_count'] = sc['bond_count']
            g['signals'].append(sig)
            # 该概念下所有债
            g['bonds'] = concept_bonds.get(sc['concept'], [])
        else:
            ungrouped.append(sig)

    # 转换为 JSON
    groups = []
    for concept_name in sorted(grouped, key=lambda c: -grouped[c]['avg_pct']):
        g = grouped[concept_name]
        # 标记哪些债有信号
        sig_code_levels = {(s.code, s.level) for s in g['signals']}
        bonds_data = []
        for b in g['bonds']:
            has_signal = b['code'] in {s.code for s in g['signals']}
            bonds_data.append({**b, 'has_signal': has_signal})
        # 按涨跌幅排序
        bonds_data.sort(key=lambda x: -x['pct'])
        groups.append({
            'concept': concept_name,
            'avg_pct': g['avg_pct'],
            'bond_count': len(bonds_data),
            'signal_count': len(g['signals']),
            'limit_up_count': concept_limit_ups.get(concept_name, 0),
            'diffusion_chain': sorted(
                [{'code': s.code, 'name': s.name,
                  'time': time.strftime('%H:%M:%S', time.localtime(s.timestamp)),
                  'rank': 0} for s in g['signals']],
                key=lambda x: x['time']
            ),
            'bonds': bonds_data,
            'signals': _signal_dicts(g['signals'], snapshots),
        })
        # 标注扩散排名
        chain = groups[-1]['diffusion_chain']
        for i, item in enumerate(chain, 1):
            item['rank'] = i

    return jsonify({
        'groups': groups,
        'ungrouped': _signal_dicts(ungrouped, snapshots),
    })


def _signal_dicts(signals, snapshots: dict) -> list[dict]:
    """将 Signal 对象 + 快照数据转换为字典列表"""
    result = []
    for sig in signals:
        snap = snapshots.get(sig.code)
        s = {
            'level': sig.level,
            'type': sig.signal_type,
            'code': sig.code,
            'name': sig.name,
            'desc': sig.description,
            'score': sig.score,
            'time': time.strftime('%H:%M:%S', time.localtime(sig.timestamp)),
            'price': round(snap.trade, 2) if snap and getattr(snap, 'trade', 0) > 0 else 0,
            'pct': round(snap.change_pct, 2) if snap and getattr(snap, 'change_pct', None) is not None else 0,
            'stock_pct': round(snap.stock_change_pct, 2) if snap and getattr(snap, 'stock_change_pct', None) is not None else 0,
            'premium': round(snap.premium_ratio, 2) if snap and getattr(snap, 'premium_ratio', None) is not None else 0,
        }
        result.append(s)
    return result


@app.route('/api/concept_sectors')
def api_concept_sectors():
    """概念板块指数快照 — Fuyao 同花顺直出, 全量概念实时涨跌

    返回:
      - sectors: 全部概念板块 (按涨幅降序), 含 thscode/name/change_pct/turnover
      - stats: 概念市场广度统计 (涨跌比/均涨)
      - refreshed_at: 上次刷新时间戳
    """
    if not consensus_mod.concept_index:
        return jsonify({'error': '概念指数模块未初始化'}), 500

    ci = consensus_mod.concept_index

    # 冷启动: 首次访问时触发刷新
    if not ci.is_ready:
        ci.load_catalog()
        ci.refresh(force=True)

    if not ci.is_ready:
        return jsonify({
            'sectors': [],
            'stats': {'up': 0, 'down': 0, 'flat': 0, 'total': 0},
            'error': '概念指数数据获取失败 (Fuyao API 不可达)',
        }), 200  # 非交易时段正常返回空 (非服务端异常)
    return jsonify({
        'sectors': ci.get_all_sorted(),
        'stats': ci.get_stats(),
        'top_gainers': ci.get_top_concepts(15, min_change=0.5),
        'top_volume': ci.get_top_volume(10),
        'refreshed_at': ci.last_refresh,
    })


@app.route('/api/concept_stats')
def api_concept_stats():
    """概念市场广度 (领涨/领跌/均涨/涨跌比) — 轻量接口"""
    if not consensus_mod.concept_index:
        return jsonify({'error': '概念指数模块未初始化'}), 500
    ci = consensus_mod.concept_index
    if not ci.is_ready:
        ci.load_catalog()
        ci.refresh(force=True)
    if not ci.is_ready:
        return jsonify({'error': '概念指数数据获取失败'}), 503
    return jsonify(ci.get_stats())


def _build_fuyao_dragons():
    """Fuyao涨停池驱动的龙头板块排行 (读缓存, 缓存空时自动回退到直拉)
    
    核心逻辑:
    1. 从调度器维护的Fuyao缓存获取封板股票, 提取概念(limit_up_reason)
    2. 模糊匹配 → cb_concept_map 标准概念名
    3. 股票→CB直接映射, 无映射时通过概念级回退
    4. 按封板时间排序 = 龙1最早, 输出 sectors + dragons
    """
    # 从缓存读取 (调度器每5s刷新, 避免API端点直接调Fuyao触发429)
    cache = state.get_fuyao_pool()
    items = cache.get('items', [])

    # 仪表盘只读缓存，不触发外部 API (调度器每90s刷新, 避免429限流)
    if not items:
        stale = cache.get('_stale_reason', cache.get('age', ''))
        logger.debug(f"Fuyao 涨停池缓存为空 ({stale}) — 等待调度器刷新")

    if not items:
        return [], []

    logger.debug(f"Fuyao 涨停池缓存命中: {len(items)} 只 (age={cache.get('age', 0)}s)")

    # 加载映射
    stock_to_cb = _load_stock_to_cb()
    concept_to_cb = _load_concept_to_cb()

    # 获取CB快照 (用于补全行情)
    with state._lock:
        snapshots = dict(state.snapshots)

    # 1. 按概念聚合, 同时做模糊匹配标准化
    #    结构: cb_concept_name → {stocks: [...]}
    concept_agg = defaultdict(lambda: {'stocks': [], 'raw_names': set()})
    for s in items:
        reason = str(s.get('limit_up_reason', '') or '')
        concepts_raw = [c.strip() for c in reason.split('+') if c.strip()]
        for raw in concepts_raw:
            if raw in _NOISY_CONCEPTS or raw in _BROAD_CONCEPTS:
                # 屏蔽涨停股票泛概念(交易所标签/宽泛政策概念),
                # 但不屏蔽可转债池概念 — CB概念在回退补龙时仍然可用
                continue
            # 模糊匹配到标准概念名
            matched = _fuzzy_match_concept(raw)
            if not matched:
                continue  # 无匹配 → CB池没有这个概念, 跳过

            ticker = str(s.get('ticker', ''))
            cb_code = stock_to_cb.get(ticker, '')
            concept_agg[matched]['stocks'].append({
                'ticker': ticker,
                'name': str(s.get('name', '')),
                'time': str(s.get('limit_up_time', '')),
                'seal': float(s.get('seal_money', 0) or 0),
                'board': int(s.get('continue_day_cnt', 0) or 0),
                'cb_code': cb_code,
            })
            concept_agg[matched]['raw_names'].add(raw)

    if not concept_agg:
        return [], []

    # 2. 构建 sectors + dragons
    sectors = []
    all_dragons = []

    for cb_concept, agg in sorted(concept_agg.items(), key=lambda kv: -len(kv[1]['stocks'])):
        # 仅剔除交易所分类/通用标签 (泛概念已在 Fuyao 输入阶段过滤)
        if cb_concept in _NOISY_CONCEPTS:
            continue
        stocks = agg['stocks']
        if len(stocks) < 2:
            continue  # 至少2个涨停才有板块效应

        # 按复合得分排序 (封板时间40% + 封单强度35% + 连板高度25%)
        #   而非纯封板时间 — 连板+大封单的股票才是真正龙头
        def _composite(st):
            t = st['time'] or '14:59'
            try:
                h, m = int(t.split(':')[0]), int(t.split(':')[1])
                minutes = h * 60 + m
            except ValueError:
                minutes = 899
            time_score = max(0, 100 - (minutes - 570) * 0.5)  # 09:30=100, 14:59≈0
            seal_score = min(100, st['seal'] / 1e8 * 10)      # 封1亿=10分
            board_score = min(100, st['board'] * 25)           # 1板=25分, 4板=100
            return time_score * 0.40 + seal_score * 0.35 + board_score * 0.25
        stocks.sort(key=_composite, reverse=True)

        zt_count = len(stocks)
        lb_bonus = sum(1 for s in stocks if s['board'] >= 2)
        heat = zt_count * 3 + lb_bonus * 2
        if heat < 6:
            continue  # 热度不足 (4只涨停=12分起)

        # 收集概念下所有CB的行情, 找出表现最好的
        cb_candidates = concept_to_cb.get(cb_concept, [])

        # 补充CB行情: 优先用直接映射, 无映射时取概念下最佳CB
        # 分两轮: 第一轮收集有快照的CB, 第二轮补无快照的 (兜底)
        cb_dragons = []
        seen_cb = set()

        # 第一轮: 有快照数据的CB + 对应的原始涨停股信息
        for st in stocks[:8]:
            cb_code = st['cb_code']
            if cb_code and cb_code in snapshots and cb_code not in seen_cb:
                seen_cb.add(cb_code)
                snap = snapshots[cb_code]
                # 直连: Fuyao股票有对应CB → 显示"股票名 + 封板信息"
                cb_dragons.append({
                    'rank': len(cb_dragons) + 1,
                    'code': cb_code,
                    'name': str(snap.name),
                    'stock_pct': round(getattr(snap, 'stock_change_pct', 0) or 0, 2),
                    'cb_pct': round(getattr(snap, 'change_pct', 0) or 0, 2),
                    'premium': round(getattr(snap, 'premium_ratio', 0) or 0, 1),
                    'label': _compute_dragon_label(
                        getattr(snap, 'stock_change_pct', 0) or 0,
                        getattr(snap, 'change_pct', 0) or 0,
                        getattr(snap, 'premium_ratio', 0) or 0),
                    'board': st['board'],
                    'seal_time': st['time'],
                    'seal_yi': round(st['seal'] / 1e8, 1),
                    'stock_name': st['name'],
                    'link_type': '直连',
                })
            elif cb_code and cb_code not in seen_cb and len(cb_dragons) < 1:
                # 有映射但不在池内 → 仅当实盘龙不足时兜底
                seen_cb.add(cb_code)
                cb_dragons.append({
                    'rank': len(cb_dragons) + 1,
                    'code': cb_code,
                    'name': st['name'],
                    'stock_pct': 0,
                    'cb_pct': 0,
                    'premium': 0,
                    'label': '无行情',
                    'board': st['board'],
                    'seal_time': st['time'],
                    'seal_yi': round(st['seal'] / 1e8, 1),
                    'stock_name': st['name'],
                    'link_type': '直连',
                })

        # 概念级回退: 同概念下其他有快照的CB, 标注"相关" (非Fuyao直接涨停股)
        for cb in cb_candidates:
            if cb not in seen_cb and cb in snapshots and len(cb_dragons) < 5:
                seen_cb.add(cb)
                snap = snapshots[cb]
                cb_dragons.append({
                    'rank': len(cb_dragons) + 1,
                    'code': cb,
                    'name': str(snap.name),
                    'stock_pct': round(getattr(snap, 'stock_change_pct', 0) or 0, 2),
                    'cb_pct': round(getattr(snap, 'change_pct', 0) or 0, 2),
                    'premium': round(getattr(snap, 'premium_ratio', 0) or 0, 1),
                    'label': '相关',
                    'board': 0,
                    'seal_time': '',
                    'seal_yi': 0,
                    'stock_name': '',
                    'link_type': '相关',
                })

        # 第二轮兜底: 无快照的CB (只在仅1只实盘龙时补充, 确保≥2只)
        if len(cb_dragons) < 2:
            for st in stocks[:8]:
                if len(cb_dragons) >= 3:
                    break
                cb_code = st['cb_code']
                if cb_code and cb_code not in seen_cb:
                    seen_cb.add(cb_code)
                    cb_dragons.append({
                        'rank': len(cb_dragons) + 1,
                        'code': cb_code,
                        'name': st['name'],
                        'stock_pct': 0,
                        'cb_pct': 0,
                        'premium': 0,
                        'label': '无数据',
                        'board': st['board'],
                        'seal_time': st['time'],
                        'seal_yi': round(st['seal'] / 1e8, 1),
                        'stock_name': st['name'],
                    })

        if len(cb_dragons) < 1:
            continue  # 无龙概念跳过

        sectors.append({
            'name': cb_concept,
            'code': cb_dragons[0]['code'],
            'zt_count': zt_count,
            'heat': round(heat, 1),
            'stage': _estimate_stage(zt_count, stocks),
            'dragons': cb_dragons[:3],
        })
        for d in cb_dragons[:3]:
            all_dragons.append({
                'rank': d['rank'],
                'concept': cb_concept,
                'stage': sectors[-1]['stage'],
                'name': d['name'],
                'code': d['code'],
                'stock_pct': d['stock_pct'],
                'cb_pct': d['cb_pct'],
                'premium': d['premium'],
                'label': d['label'],
                'seal_time': d['seal_time'],
                'seal_yi': d['seal_yi'],
                'stock_name': d['stock_name'],
                'heat': round(heat, 1),
            })

    # 排序 + 截断
    sectors.sort(key=lambda s: -s['heat'])
    sectors = sectors[:8]
    all_dragons.sort(key=lambda d: -d['heat'])
    all_dragons = all_dragons[:15]
    for i, d in enumerate(all_dragons[:15]):
        d['rank'] = i + 1

    return sectors, all_dragons[:15]


def _compute_dragon_label(stock_pct: float, cb_pct: float, premium: float) -> str:
    """计算转债相对于正股的状态标签"""
    if premium > 40:
        return '高溢价'
    if cb_pct < 2.0 and stock_pct > 3.0:
        return '滞后'
    return '同步'


def _estimate_stage(zt_count: int, stocks: list) -> int:
    """根据涨停数和连板情况估计阶段"""
    multi_board = sum(1 for s in stocks if s['board'] >= 2)
    if zt_count >= 3 and multi_board >= 1:
        return 4  # 扩散
    if zt_count >= 2:
        return 3  # 封板
    if zt_count == 1 and multi_board >= 1:
        return 3
    if zt_count == 1:
        return 1  # 酝酿
    return 0


@app.route('/api/dragons')
def api_dragons():
    """概念板块龙头排行 — Fuyao 涨停池驱动

    从 Fuyao 涨停池获取封板股票, 按概念聚合,
    封板时间早晚定龙1龙2龙3, 交叉匹配转债池。
    """
    try:
        sectors, dragons = _build_fuyao_dragons()
        if sectors:
            return jsonify({
                'dragons': dragons,
                'sectors': sectors,
                'summary': {'total_concepts': len(sectors), 'source': 'fuyao'},
            })
    except Exception as e:
        logger.warning(f"Fuyao龙头构建失败, 回退共识: {e}")

    # ── 回退: 共识追踪器 ──
    try:
        if consensus_mod.consensus_tracker:
            ct = consensus_mod.consensus_tracker
            all_states = ct.get_all()
            sectors_light = []
            active_dragons = []

            for concept, st in all_states.items():
                stage = st.get('stage', 0)
                if stage < 1:
                    continue
                if concept in _NOISY_CONCEPTS:
                    continue   # 跳过交易所分类/通用标签

                limit_up = st.get('limit_up_count', 0)
                followers = st.get('follower_count', 0)
                d_sc = st.get('dragon_sc_pct', 0)

                heat = limit_up * 3 + followers * 2 + min(d_sc, 10) * 1
                if heat < 16:
                    continue   # 热度不足 → 跳过

                dragons = st.get('dragons', [])
                for dr in dragons:
                    dr.setdefault('consecutive_days', 0)
                    dr.setdefault('fengdan_score', 0)
                    dr.setdefault('cb_pct', 0)
                    dr.setdefault('premium', 0)
                    dr.setdefault('label', '同步')
                    dr.setdefault('stock_pct', dr.get('stock_pct', 0) or 0)

                sectors_light.append({
                    'name': concept,
                    'code': st.get('dragon_code', ''),
                    'zt_count': limit_up,
                    'heat': round(heat, 1),
                    'stage': stage,
                    'dragons': dragons[:3],
                })

                if dragons:
                    d = dragons[0]
                    active_dragons.append({
                        'rank': 0,
                        'concept': concept,
                        'stage': stage,
                        'name': d.get('name', ''),
                        'code': d.get('code', ''),
                        'stock_pct': d.get('stock_pct', 0),
                        'cb_pct': d.get('cb_pct', 0),
                        'premium': d.get('premium', 0),
                        'label': d.get('label', '同步'),
                        'heat': round(heat, 1),
                    })

            if sectors_light:
                sectors_light.sort(key=lambda s: -s['heat'])
                # 动态截断
                if len(sectors_light) > 15:
                    sectors_light = [s for s in sectors_light if s['heat'] >= 8.0]
                sectors_light = sectors_light[:12]

                active_dragons.sort(key=lambda d: -d['heat'])
                for i, d in enumerate(active_dragons[:15]):
                    d['rank'] = i + 1

                return jsonify({
                    'dragons': active_dragons[:15],
                    'sectors': sectors_light,
                    'summary': {'total_concepts': len(sectors_light)},
                })

        return jsonify({'dragons': [], 'sectors': [], 'error': '无活跃概念板块'})
    except Exception as e:
        logger.error(f"/api/dragons 异常: {e}")
        return jsonify({'dragons': [], 'sectors': [], 'error': str(e)})


@app.route('/api/sentiment_detail')
def api_sentiment_detail():
    """六维情绪详情: 涨停强度/打板质量/接力情绪/亏钱效应/赚钱效应/市场广度"""
    with state._lock:
        snapshots = dict(state.snapshots)

    detail = _get_sentiment_detail(snapshots)

    # 补充盘后缓存信息 (cached_date 已在 merge 中设为今日)
    try:
        kpl = _get_kpl()
        if kpl:
            cache = kpl._load_cache()
            recent = kpl._get_recent_trade_date()
            if recent in cache:
                detail['cache_keys'] = list(cache[recent].keys())
    except Exception:
        pass

    # 确保 cached_date 存在且为今日 (merge 已设, 此处兜底)
    if not detail.get('cached_date'):
        from datetime import date
        detail['cached_date'] = date.today().isoformat()

    return jsonify(detail)


def _build_fuyao_sector_flows(items: list) -> list:
    """从 Fuyao 涨停池实时聚合板块涨停数据 (当日数据)

    将 limit_up_reason (如 "磷化铟+金刚石概念") 拆分为概念,
    经模糊匹配标准化后聚合, 按涨停数降序输出。
    禁用 CB 概念池过滤: Fuyao 子概念可能在池外,
    但仍是当日有效涨停信号。
    """
    concept_agg = defaultdict(lambda: {'zt_count': 0, 'top_stock': '', 'stocks': []})
    for s in items:
        reason = str(s.get('limit_up_reason', '') or '')
        concepts_raw = [c.strip() for c in reason.split('+') if c.strip()]
        for raw in concepts_raw:
            if raw in _NOISY_CONCEPTS or raw in _BROAD_CONCEPTS:
                continue
            matched = _fuzzy_match_concept(raw)
            if not matched or len(matched) < 2:
                continue
            # 二次过滤: 匹配后的标准化概念名也可能命中黑名单
            if matched in _NOISY_CONCEPTS or matched in _BROAD_CONCEPTS:
                continue
            name = str(s.get('name', ''))
            agg = concept_agg[matched]
            agg['zt_count'] += 1
            if not agg['top_stock']:
                agg['top_stock'] = name
            agg['stocks'].append(name)

    flows = []
    for concept, agg in sorted(concept_agg.items(), key=lambda kv: -kv[1]['zt_count']):
        if agg['zt_count'] < 2:  # 至少2只涨停才有板块效应
            continue
        flows.append({
            'name': concept,
            'code': '',
            'zt_count': agg['zt_count'],
            'main_capital': 0,
            'top_stock': agg['top_stock'],
        })
    return flows


@app.route('/api/sector_flow')
def api_sector_flow():
    """板块资金流向 Top 10 (概念板块主力净流入排行)

    纯 Fuyao 涨停池实时聚合 (当日数据), 无回退数据源
    """
    try:
        # ── 优先: Fuyao 实时涨停池聚合 (当日数据) ──
        pool_cache = state.get_fuyao_pool()
        items = pool_cache.get('items', [])
        
        # 仪表盘只读缓存，不触发外部 API (调度器每90s刷新)
        
        if items and len(items) >= 5:
            flows = _build_fuyao_sector_flows(items)
            if flows:
                return jsonify({
                    'flows': flows[:10],
                    'updated': time.strftime('%H:%M'),
                    'summary': {
                        '日期': datetime.now().strftime('%Y-%m-%d'),
                        '涨停数': len(items),
                        'source': 'fuyao(盘中实时)',
                    },
                })

        # Fuyao 无数据时直接返回空
        return jsonify({'flows': [], 'updated': time.strftime('%H:%M'), 'error': 'Fuyao涨停池暂无数据'})
    except Exception as e:
        logger.error(f"/api/sector_flow 异常: {e}")
        return jsonify({'flows': [], 'error': str(e)})


@app.route('/api/signal_accuracy')
def api_signal_accuracy():
    """信号准确率统计: 从回测CSV按信号类型聚合近N日胜率

    返回:
      { days: int, signals: [{type, count, win_rate_60s, win_rate_300s, avg_pnl_300s, level_weight}] }
    """
    import csv, glob
    from collections import defaultdict, Counter

    days = request.args.get('days', 5, type=int)
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
    pattern = os.path.join(log_dir, 'backtest_*.csv')
    files = sorted(glob.glob(pattern), reverse=True)[:days]

    if not files:
        return jsonify({'error': '无回测数据', 'signals': [], 'days': 0})

    # 聚合: signal_type → {p60, p300, count}
    agg = defaultdict(lambda: {'p60': [], 'p300': [], 'count': 0})

    for fpath in files:
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    st = row.get('signal_type', '')
                    try:
                        p60 = float(row.get('60s_pnl', 0) or 0)
                        p300 = float(row.get('300s_pnl', 0) or 0)
                        agg[st]['p60'].append(p60)
                        agg[st]['p300'].append(p300)
                        agg[st]['count'] += 1
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.warning(f"读取回测文件失败 {fpath}: {e}")

    results = []
    # 胜率权重映射 (用于动态升级)
    level_weights = {
        'S': {'min_wr_60s': 55, 'min_wr_300s': 60},
        'A': {'min_wr_60s': 50, 'min_wr_300s': 52},
        'B': {'min_wr_60s': 45, 'min_wr_300s': 48},
    }

    for st in sorted(agg):
        stats = agg[st]
        n = stats['count']
        if n < 5:
            continue
        wr60 = sum(1 for p in stats['p60'] if p > 0) / n * 100 if n > 0 else 0
        wr300 = sum(1 for p in stats['p300'] if p > 0) / n * 100 if n > 0 else 0
        import statistics
        avg300 = statistics.mean(stats['p300']) if stats['p300'] else 0

        # 推荐等级权重
        recommended_level = None
        for lv, criteria in reversed(level_weights.items()):
            if wr60 >= criteria['min_wr_60s'] and wr300 >= criteria['min_wr_300s']:
                recommended_level = lv
                break

        results.append({
            'type': st,
            'count': n,
            'win_rate_60s': round(wr60, 1),
            'win_rate_300s': round(wr300, 1),
            'avg_pnl_300s': round(avg300, 2),
            'recommended_level': recommended_level or 'B',
            'suppressed': wr300 < 25 and n >= 20,
            'warning_only': 25 <= wr300 < 35 and n >= 20,
            'dynamic_weight': round(max(0.15, min(2.0, wr300 / 50.0)), 2),
        })

    results.sort(key=lambda x: (-x['win_rate_300s'], -x['count']))

    return jsonify({
        'days': len(files),
        'total_signals': sum(r['count'] for r in results),
        'signals': results,
    })
@app.route('/api/history')
def api_history():
    """返回历史信号 (可指定数量)"""
    limit = request.args.get('limit', 200, type=int)
    with state._lock:
        history = state.signal_history[-limit:]
    return jsonify(history)


@app.route('/api/sidecar')
def api_sidecar():
    """新架构旁路运行状态 (只读, 不触发计算)"""
    sc = getattr(state, 'sidecar_state', None) or {}
    return jsonify(sc)


@app.route('/api/compare')
def api_compare():
    """新旧系统对比: 同 tick 下旧信号 vs 新管道评估"""
    with state._lock:
        old_sigs = state.signal_history[-20:] if state.signal_history else []
        old_count = len(state.snapshots)
        last_update = state.last_update
    sc = getattr(state, 'sidecar_state', None) or {}
    return jsonify({
        'ts': time.time(),
        'last_update': last_update,
        'old': {
            'snapshot_count': old_count,
            'recent_signals': len(old_sigs),
        },
        'new': {
            'regime': sc.get('regime', '未知'),
            'trade_mode': sc.get('trade_mode', '未知'),
            'enabled_strategies': sc.get('enabled_strategies', []),
            'machine_state': sc.get('machine_state', 'disabled'),
            'intents': sc.get('intents', 0),
            'candidates': sc.get('candidates', 0),
            'top_candidate': sc.get('top_candidate'),
        },
        'new_status': sc.get('status', 'no_data'),
    })

def start_server(host: str = '0.0.0.0', port: int = 5000):
    """启动 Flask 服务 (阻塞) — signal 由 main.py 主线程处理, 此处用 atexit 兜底"""
    atexit.register(_cleanup_tdx)

    logger.info(f"仪表盘启动: http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


def _cleanup_tdx():
    """释放 TDX 连接资源"""
    try:
        from core.data_fusion import TdxClient
        TdxClient.close()
        logging.getLogger(__name__).info("TDX 连接已释放")
    except Exception:
        pass
