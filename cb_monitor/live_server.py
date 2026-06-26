#!/usr/bin/env python3
"""
可转债作战台 - 独立轻量服务 (支持隧道外链)
用法: python live_server.py --port 8090
"""

import json
import sys
import os
import time
import http.server
import threading
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# --- 作战台 HTML (内嵌, 无模板依赖) ---
_BATTLE_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>可转债作战台</title>
<style>
:root{--bg:#0a0e17;--card:#111827;--border:#1e293b;--text:#e2e8f0;--dim:#64748b;--green:#22c55e;--red:#ef4444;--orange:#f59e0b;--blue:#3b82f6;--purple:#a855f7;--amber:#fbbf24}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);padding:10px 10px 80px;max-width:480px;margin:0 auto;-webkit-tap-highlight-color:transparent}
.bar{display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--card);border-radius:10px;border:1px solid var(--border);margin-bottom:8px;font-size:12px}
.bar .time{font-weight:700;color:#fff;font-size:14px}
.bar .market-state{padding:2px 10px;border-radius:12px;font-size:12px;font-weight:800}
.bar .market-state.进攻{background:rgba(239,68,68,0.2);color:var(--red)}
.bar .market-state.震荡{background:rgba(245,158,11,0.2);color:var(--orange)}
.bar .market-state.退潮{background:rgba(59,130,246,0.2);color:var(--blue)}
.bar .market-state.冰点{background:rgba(100,116,139,0.3);color:var(--dim)}
.bar .sentiment{padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;margin-left:auto}
.bar .sentiment.进攻{background:rgba(239,68,68,0.2);color:var(--red)}
.bar .sentiment.震荡{background:rgba(245,158,11,0.2);color:var(--orange)}
.bar .sentiment.震荡偏强{background:rgba(251,191,36,0.2);color:var(--amber)}
.bar .sentiment.修复{background:rgba(59,130,246,0.2);color:var(--blue)}
.bar .sentiment.退潮{background:rgba(100,116,139,0.3);color:var(--dim)}
.stats-row{display:flex;gap:6px;margin-bottom:8px}
.stat{flex:1;text-align:center;padding:8px 4px;background:var(--card);border-radius:8px;border:1px solid var(--border)}
.stat .val{font-size:18px;font-weight:800}
.stat .lbl{font-size:9px;color:var(--dim);margin-top:2px}
.stat.ambush .val{color:var(--red)}
.stat.sell .val{color:var(--orange)}
.stat.forbid .val{color:var(--dim)}
.themes{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}
.theme-tag{padding:3px 8px;background:rgba(59,130,246,0.15);border:1px solid rgba(59,130,246,0.3);border-radius:12px;font-size:10px;color:var(--blue)}
.sec-title{font-size:12px;font-weight:700;color:var(--dim);margin:10px 0 6px;text-transform:uppercase;letter-spacing:.5px}
.card{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:12px;margin-bottom:8px;position:relative}
.card.ambush{border-left:3px solid var(--red)}
.card.sell{border-left:3px solid var(--orange)}
.card.forbid{border-left:3px solid var(--dim);opacity:.8}
.card .head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.card .action-tag{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:800;color:#fff}
.card.ambush .action-tag{background:var(--red)}
.card.sell .action-tag{background:var(--orange)}
.card.forbid .action-tag{background:var(--dim)}
.card .name{font-size:15px;font-weight:700;color:#fff}
.card .code{font-size:11px;color:var(--dim)}
.card .concept{font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(59,130,246,0.15);color:var(--blue);margin-left:4px}
.card .reason{font-size:11px;color:var(--text);line-height:1.5;margin-bottom:6px;padding:6px 8px;background:rgba(255,255,255,0.03);border-radius:6px}
.card .data-row{display:flex;gap:12px;font-size:11px;margin-bottom:4px;flex-wrap:wrap}
.card .data-item{color:var(--dim)}
.card .data-item span{color:#fff;font-weight:600}
.card .data-item.up{color:var(--red)}
.card .data-item.down{color:var(--green)}
.card .footer{display:flex;gap:12px;font-size:9px;color:var(--dim);margin-top:6px;padding-top:6px;border-top:1px solid var(--border);flex-wrap:wrap}
.card .footer .risk{color:var(--orange)}
.chain{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:10px 12px;margin-bottom:8px}
.chain .line{font-size:11px;padding:3px 0;display:flex;align-items:center;gap:6px}
.chain .line .dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.chain .line .dot.s2{background:var(--red)}
.chain .line .dot.s3{background:var(--orange)}
.chain .line .dot.s4{background:var(--blue)}
.forbid-list{font-size:10px;color:var(--dim)}
.forbid-list .item{padding:4px 8px;margin-bottom:2px;background:rgba(239,68,68,0.05);border-radius:4px;border-left:2px solid var(--red)}
.tabs{display:flex;margin-bottom:12px;background:var(--card);border-radius:10px;padding:3px}
.tab{flex:1;text-align:center;padding:8px;font-size:12px;font-weight:600;color:var(--dim);border-radius:8px;cursor:pointer}
.tab.active{background:rgba(59,130,246,0.2);color:var(--blue)}
.page{display:none}
.page.active{display:block}
.bk-table{width:100%;font-size:10px;border-collapse:collapse}
.bk-table th{color:var(--dim);text-align:left;padding:4px 6px;border-bottom:1px solid var(--border);font-weight:600}
.bk-table td{padding:5px 6px;border-bottom:1px solid rgba(255,255,255,.04)}
.bk-table .pos{color:var(--red)}
.bk-table .neg{color:var(--green)}
.live-dot{display:inline-block;width:6px;height:6px;background:var(--green);border-radius:50%;margin-right:4px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.refresh-info{text-align:center;font-size:9px;color:var(--dim);margin-top:12px}
.empty{text-align:center;padding:30px;color:var(--dim);font-size:13px}
</style>
</head>
<body>
<div class="tabs">
  <div class="tab active" onclick="switchPage('battle')">战情</div>
  <div class="tab" onclick="switchPage('review')">复盘</div>
</div>
<div class="page active" id="battlePage">
  <div class="bar">
    <span class="live-dot"></span>
    <span class="time" id="timeDisplay">--:--:--</span>
    <span class="market-state" id="marketState">--</span>
    <span class="sentiment" id="sentBadge">--</span>
  </div>
  <div class="stats-row">
    <div class="stat ambush"><div class="val" id="ambushCount">-</div><div class="lbl">埋伏</div></div>
    <div class="stat sell"><div class="val" id="sellCount">-</div><div class="lbl">卖出</div></div>
    <div class="stat forbid"><div class="val" id="forbidCount">-</div><div class="lbl">禁入</div></div>
  </div>
  <div class="themes" id="themesRow"></div>
  <div class="sec-title">埋伏</div>
  <div id="ambushCards"><div class="empty">加载中...</div></div>
  <div class="sec-title">卖出提醒</div>
  <div id="sellCards"><div class="empty">加载中...</div></div>
  <div class="sec-title">概念传导</div>
  <div id="conceptChain"><div class="empty">加载中...</div></div>
  <div class="sec-title">禁入</div>
  <div id="forbiddenList"><div class="empty">加载中...</div></div>
</div>
<div class="page" id="reviewPage">
  <div class="sec-title">后验统计</div>
  <div id="reviewStats"><div class="empty">暂无数据</div></div>
  <div class="sec-title">活跃追踪</div>
  <div id="reviewTable"><div class="empty">暂无</div></div>
</div>
<div class="refresh-info"><span class="live-dot"></span> 3秒自动刷新</div>
<script>
function esc(s){return String(s).replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function switchPage(p){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.textContent.includes(p==='battle'?'战情':'复盘'))});
  document.getElementById('battlePage').classList.toggle('active',p==='battle');
  document.getElementById('reviewPage').classList.toggle('active',p!=='battle');
}
function renderCards(id,cards){
  var el=document.getElementById(id);
  if(!cards||!cards.length){el.innerHTML='<div class="empty">暂无</div>';return}
  el.innerHTML=cards.map(function(c){
    var cls=c.action==='埋伏'?'ambush':c.action==='卖出'?'sell':'forbid';
    return '<div class="card '+cls+'">'+
      '<div class="head"><span class="action-tag">'+c.action+'</span><span class="name">'+esc(c.name)+'</span><span class="code">'+c.code+'</span>'+(c.concept?'<span class="concept">'+esc(c.concept)+'</span>':'')+'</div>'+
      '<div class="reason">'+esc(c.reason||'')+'</div>'+
      '<div class="data-row">'+
        '<span class="data-item '+(c.cb_pct>=0?'up':'down')+'">转债 <span>'+(c.cb_pct>0?'+':'')+(c.cb_pct||0).toFixed(2)+'%</span></span>'+
        '<span class="data-item '+(c.stock_pct>=0?'up':'down')+'">正股 <span>'+(c.stock_pct>0?'+':'')+(c.stock_pct||0).toFixed(2)+'%</span></span>'+
        '<span class="data-item">溢价 <span>'+(c.premium||0).toFixed(1)+'%</span></span>'+
        '<span class="data-item">成交 <span>'+(c.amount||0).toFixed(2)+'亿</span></span>'+
      '</div>'+
      '<div class="footer"><span>接盘:'+esc(c.buyer||'')+'</span><span>'+esc(c.hold_time||'')+'</span><span>止损 '+(c.stop_loss_pct||0)+'%</span><span>止盈 +'+(c.take_profit_pct||0)+'%</span>'+
        (c.invalid_if?'<span class="risk">'+esc(c.invalid_if)+'</span>':'')+
        ((c.risk_tags||[]).map(function(r){return '<span class="risk">'+esc(r)+'</span>'}).join(''))+
      '</div></div>';
  }).join('');
}
function renderBattle(d){
  document.getElementById('timeDisplay').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  var sb=document.getElementById('sentBadge');sb.textContent=d.sentiment&&d.sentiment.status||'--';sb.className='sentiment '+(d.sentiment&&d.sentiment.status||'');
  document.getElementById('ambushCount').textContent=d.ambush_count||0;
  document.getElementById('sellCount').textContent=d.sell_count||0;
  document.getElementById('forbidCount').textContent=d.forbidden_count||0;
  var th=document.getElementById('themesRow');
  th.innerHTML=(d.main_themes||[]).map(function(t){return '<span class="theme-tag">'+esc(t.name)+' '+esc(t.stage_name||'')+'</span>'}).join('')||'<span style="font-size:10px;color:var(--dim)">暂无主线</span>';
  renderCards('ambushCards',d.ambush||[]);
  renderCards('sellCards',d.sell||[]);
  var cc=document.getElementById('conceptChain');
  if(!d.concept_chain||!d.concept_chain.length){cc.innerHTML='<div class="empty">暂无传导</div>';return}
  cc.innerHTML=d.concept_chain.map(function(c){
    var dhtml='';
    if(c.dragons&&c.dragons.length){
      dhtml=c.dragons.map(function(dr){
        var emoji={1:'L1',2:'L2',3:'L3'}[dr.rank]||'';
        var lbl=dr.label==='滞后'?'<span style="color:#22c55e;font-size:9px">'+dr.label+'</span>':dr.label==='高溢价'?'<span style="color:#f59e0b;font-size:9px">'+dr.label+'</span>':'';
        return emoji+esc(dr.name)+' +'+dr.stock_pct+'% '+lbl;
      }).join(' | ');
    }
    return '<div class="chain"><div class="line"><span class="dot s'+c.stage+'"></span><b>'+esc(c.concept)+'</b> <span style="color:var(--dim);font-size:10px">阶段'+c.stage+'</span> <span style="color:var(--dim);font-size:9px">涨停'+c.limit_up+'</span></div>'+(dhtml?'<div class="line" style="padding-left:12px;font-size:10px;color:var(--dim)">'+dhtml+'</div>':'')+'</div>';
  }).join('');
  var fb=document.getElementById('forbiddenList');
  fb.innerHTML=(d.forbidden||[]).map(function(f){return '<div class="forbid-list"><div class="item">'+esc(f.name)+'('+f.code+') - '+esc(f.reason)+' '+((f.risks||[]).map(function(r){return esc(r)}).join(' '))+'</div></div>'}).join('')||'<div class="empty">暂无</div>';
}
function fetchBattle(){
  fetch('/api/battle').then(function(r){return r.json()}).then(function(d){
    if(d&&d.sentiment) renderBattle(d);
  }).catch(function(){});
}
fetchBattle();setInterval(fetchBattle,3000);
setInterval(function(){document.getElementById('timeDisplay').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false})},1000);
</script>
</body>
</html>'''

# --- 模拟数据生成 (TDX未连接时) ---
def _generate_demo_battle():
    """返回模拟的战情数据 (带实时时间戳)"""
    now = time.strftime('%H:%M:%S')
    return {
        'sentiment': {'status': '震荡偏强', 'advance': 285, 'decline': 198, 'ratio': 1.44},
        'main_themes': [
            {'name': 'AI算力', 'stage': 3, 'stage_name': '封板', 'limit_up': 8, 'dragon': '浪潮信息', 'dragon_pct': 10.01},
            {'name': '机器人', 'stage': 2, 'stage_name': '冲锋', 'limit_up': 5, 'dragon': '埃斯顿', 'dragon_pct': 7.52},
            {'name': '低空经济', 'stage': 2, 'stage_name': '冲锋', 'limit_up': 3, 'dragon': '万丰奥威', 'dragon_pct': 6.15},
        ],
        'ambush_count': 3, 'sell_count': 2, 'forbidden_count': 5,
        'ambush': [
            {'action': '埋伏', 'name': '银轮转债', 'code': '127037', 'concept': 'AI算力', 'reason': '正股涨停突破+转债溢价率压缩至5%以内，板块主线阶段3共振', 'cb_pct': 3.82, 'stock_pct': 10.02, 'premium': 4.5, 'amount': 8.32, 'buyer': '游资接力', 'hold_time': '1-3天', 'stop_loss_pct': 5, 'take_profit_pct': 15, 'invalid_if': '正股炸板', 'risk_tags': ['强赎计数中']},
            {'action': '埋伏', 'name': '拓斯转债', 'code': '123201', 'concept': '机器人', 'reason': '转债折价-1.2%+正股趋势向上，板块阶段2扩散初期性价比高', 'cb_pct': 2.15, 'stock_pct': 4.38, 'premium': -1.2, 'amount': 5.67, 'buyer': '趋势资金', 'hold_time': '2-5天', 'stop_loss_pct': 3, 'take_profit_pct': 10, 'invalid_if': '正股跌破5日线', 'risk_tags': []},
            {'action': '埋伏', 'name': '航新转债', 'code': '123061', 'concept': '低空经济', 'reason': '低空经济政策催化+转债微盘活跃，振幅8%以上弹性标的', 'cb_pct': 1.05, 'stock_pct': 2.88, 'premium': 8.2, 'amount': 3.15, 'buyer': '题材资金', 'hold_time': '1-3天', 'stop_loss_pct': 5, 'take_profit_pct': 12, 'invalid_if': '概念退潮', 'risk_tags': ['微盘波动大']},
        ],
        'sell': [
            {'action': '卖出', 'name': '惠城转债', 'code': '123118', 'concept': '环保', 'reason': '连涨3日累计+15%，溢价率已拉至25%，性价比大幅下降', 'cb_pct': 5.20, 'stock_pct': 3.10, 'premium': 25.0, 'amount': 12.45, 'buyer': '-', 'hold_time': '已持3天', 'stop_loss_pct': 0, 'take_profit_pct': 0, 'invalid_if': '', 'risk_tags': ['高位放量']},
            {'action': '卖出', 'name': '中旗转债', 'code': '127081', 'concept': '建材', 'reason': '正股冲高回落+转债跟跌，趋势走弱建议减仓', 'cb_pct': -2.35, 'stock_pct': -4.12, 'premium': 18.0, 'amount': 2.88, 'buyer': '-', 'hold_time': '已持5天', 'stop_loss_pct': 0, 'take_profit_pct': 0, 'invalid_if': '', 'risk_tags': ['跌破支撑']},
        ],
        'forbidden': [
            {'name': '横河转债', 'code': '123013', 'reason': '溢价率>100%妖债', 'risks': ['妖债', '高溢价']},
            {'name': '盛路转债', 'code': '123041', 'reason': '价格>800元妖债', 'risks': ['妖债', '高价']},
            {'name': '晶瑞转债', 'code': '123031', 'reason': '强赎公告期，溢价归零风险', 'risks': ['强赎', '风险高']},
        ],
        'concept_chain': [
            {'concept': 'AI算力', 'stage': 3, 'limit_up': 8, 'dragons': [
                {'rank': 1, 'name': '浪潮信息', 'stock_pct': 10.01, 'label': ''},
                {'rank': 2, 'name': '中科曙光', 'stock_pct': 7.52, 'label': '滞后'},
                {'rank': 3, 'name': '拓尔思', 'stock_pct': 5.30, 'label': ''},
            ]},
            {'concept': '机器人', 'stage': 2, 'limit_up': 5, 'dragons': [
                {'rank': 1, 'name': '埃斯顿', 'stock_pct': 7.52, 'label': ''},
                {'rank': 2, 'name': '绿的谐波', 'stock_pct': 4.15, 'label': '高溢价'},
            ]},
            {'concept': '低空经济', 'stage': 2, 'limit_up': 3, 'dragons': [
                {'rank': 1, 'name': '万丰奥威', 'stock_pct': 6.15, 'label': ''},
            ]},
        ],
        '_generated': now, '_source': 'demo',
    }


def _generate_demo_review():
    return {
        'active': [
            {'trigger_time': '09:35:12', 'level': 'S', 'name': '银轮转债', 'code': '127037', 'type': '涨停突破', 'trigger_price': 152.30, 'peak_price': 165.80, 'current_pnl': 8.86},
            {'trigger_time': '09:32:08', 'level': 'A', 'name': '拓斯转债', 'code': '123201', 'type': '折价套利', 'trigger_price': 138.50, 'peak_price': 145.20, 'current_pnl': 3.75},
        ],
        'stats': {
            'total': 47, 'win_rate': 57.4, 'avg_pnl': 2.35,
            'by_strategy': {'chase': {'total': 28, 'win_rate': 60.7, 'avg_pnl': 3.15, 'best': {'current_pnl': 18.5}, 'worst': {'current_pnl': -5.2}}, 'dip': {'total': 19, 'win_rate': 52.6, 'avg_pnl': 1.22, 'best': {'current_pnl': 8.3}, 'worst': {'current_pnl': -7.5}}},
            'by_checkpoint': {},
        }
    }


# --- 尝试加载真实 pipeline ---
_real_pipeline = None

def _try_load_pipeline():
    """尝试加载真实决策管道 (需要TDX运行)"""
    global _real_pipeline
    if _real_pipeline is not None:
        return _real_pipeline
    try:
        from core.decision_pipeline import DecisionPipeline
        import core.consensus_tracker as ct
        pipeline = DecisionPipeline()
        if ct.consensus_tracker and ct.consensus_tracker.get_all():
            stages = {c: s.get('stage', 0) for c, s in ct.consensus_tracker.get_all().items()}
            heat = {c: s.get('dragon_sc_pct', 0) for c, s in ct.consensus_tracker.get_all().items()}
            pipeline.set_mainlines(stages, heat)
        _real_pipeline = pipeline
        return pipeline
    except Exception:
        return None


class BattleHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 安静模式

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/' or path == '/index.html':
            self._send_html(_BATTLE_HTML)
        elif path == '/api/battle':
            # 尝试真实数据, 无数据或失败则回退模拟数据
            try:
                from dashboard.shared_state import state
                import core.consensus_tracker as ct
                from core.decision_pipeline import DecisionPipeline
                with state._lock:
                    snapshots = dict(state.snapshots)
                if not snapshots:
                    raise RuntimeError("no tdxy data")
                up = sum(1 for s in snapshots.values() if getattr(s, 'change_pct', 0) > 0)
                down = sum(1 for s in snapshots.values() if getattr(s, 'change_pct', 0) < 0)
                ratio = round(up / max(down, 1), 1)
                if ratio > 2.5: st = '进攻'
                elif ratio > 1.5: st = '震荡偏强'
                elif ratio > 0.8: st = '震荡'
                elif ratio > 0.5: st = '修复'
                else: st = '退潮'
                sentiment = {'status': st, 'advance': up, 'decline': down, 'ratio': ratio}
                consensus_stages = {}
                concept_heat = {}
                if ct.consensus_tracker:
                    all_states = ct.consensus_tracker.get_all()
                    consensus_stages = {c: s.get('stage', 0) for c, s in all_states.items()}
                    concept_heat = {c: s.get('dragon_sc_pct', 0) for c, s in all_states.items()}
                pipeline = DecisionPipeline()
                pipeline.set_mainlines(consensus_stages, concept_heat)
                redeem_map = getattr(state, 'redeem_map', {}) or {}
                decisions = pipeline.evaluate_batch(snapshots, {}, consensus_stages, redeem_map)
                ambush = [d for d in decisions if d.action == '埋伏']
                sell = [d for d in decisions if d.action == '卖出']
                result = {
                    'sentiment': sentiment,
                    'main_themes': [],
                    'ambush_count': len(ambush), 'sell_count': len(sell),
                    'forbidden_count': 0, 'ambush': [d.to_dict() for d in ambush[:5]],
                    'sell': [d.to_dict() for d in sell[:5]], 'forbidden': [],
                    'concept_chain': [], '_source': 'tdx',
                }
                if ct.consensus_tracker:
                    themes = ct.consensus_tracker.get_concepts_by_stage(min_stage=2)
                    result['main_themes'] = [{'name': t['concept'], 'stage': t['stage'], 'stage_name': t['stage_name'], 'limit_up': t.get('limit_up_count', 0), 'dragon': t.get('dragon_name', '')} for t in themes[:5]]
                    chain = ct.consensus_tracker.get_concepts_by_stage(min_stage=1)[:5]
                    result['concept_chain'] = [{'concept': c['concept'], 'stage': c['stage'], 'limit_up': c.get('limit_up_count', 0), 'dragons': c.get('dragons', [])} for c in chain]
                self._send_json(result)
            except Exception:
                self._send_json(_generate_demo_battle())
        elif path == '/api/backtest':
            try:
                from backtest.tracker import tracker
                self._send_json({'active': tracker.get_active(), 'stats': tracker.get_stats()})
            except Exception:
                self._send_json(_generate_demo_review())
        elif path == '/api/state':
            try:
                from dashboard.shared_state import state
                self._send_json(state.to_dict())
            except Exception:
                self._send_json({'status': 'demo', 'message': 'TDX未连接，显示模拟数据'})
        else:
            self._send_json({'error': 'not found'}, 404)


_CLOUD_URL = "https://6c11f531a142445b8b22fe8cbc88ad92.app.codebuddy.work"
_LT_URL = "https://dyz-cb-battle.loca.lt"

def start(port=8090):
    server = http.server.HTTPServer(('0.0.0.0', port), BattleHandler)
    print(f"作战台已启动 → http://localhost:{port}")
    print(f"📱 固定快照版: {_CLOUD_URL}")
    print(f"📱 实时隧道版: {_LT_URL} (需先启动隧道)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("\n已停止")


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    start(port)
