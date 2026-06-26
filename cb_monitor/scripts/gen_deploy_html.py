#!/usr/bin/env python3
"""Generate a self-contained HTML with live battle data for CloudStudio deploy"""
import json, sys, os

# Read battle data from stdin or file
if len(sys.argv) > 1:
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        data = json.load(f)
else:
    data = json.load(sys.stdin)

data_json = json.dumps(data, ensure_ascii=False)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'outputs', 'deploy')

html = f'''<!DOCTYPE html>
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
.bar{{display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--card);border-radius:10px;border:1px solid var(--border);margin-bottom:8px;font-size:12px}}
.bar .time{{font-weight:700;color:#fff;font-size:14px}}
.bar .market-state{{padding:2px 10px;border-radius:12px;font-size:12px;font-weight:800}}
.bar .market-state.进攻{{background:rgba(239,68,68,0.2);color:var(--red)}}
.bar .market-state.震荡{{background:rgba(245,158,11,0.2);color:var(--orange)}}
.bar .market-state.退潮{{background:rgba(59,130,246,0.2);color:var(--blue)}}
.bar .market-state.冰点{{background:rgba(100,116,139,0.3);color:var(--dim)}}
.bar .sentiment{{padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;margin-left:auto}}
.bar .sentiment.进攻{{background:rgba(239,68,68,0.2);color:var(--red)}}
.bar .sentiment.震荡{{background:rgba(245,158,11,0.2);color:var(--orange)}}
.bar .sentiment.震荡偏强{{background:rgba(251,191,36,0.2);color:var(--amber)}}
.bar .sentiment.修复{{background:rgba(59,130,246,0.2);color:var(--blue)}}
.bar .sentiment.退潮{{background:rgba(100,116,139,0.3);color:var(--dim)}}
.stats-row{{display:flex;gap:6px;margin-bottom:8px}}
.stat{{flex:1;text-align:center;padding:8px 4px;background:var(--card);border-radius:8px;border:1px solid var(--border)}}
.stat .val{{font-size:18px;font-weight:800}}
.stat .lbl{{font-size:9px;color:var(--dim);margin-top:2px}}
.stat.ambush .val{{color:var(--red)}}
.stat.sell .val{{color:var(--orange)}}
.stat.forbid .val{{color:var(--dim)}}
.themes{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}}
.theme-tag{{padding:3px 8px;background:rgba(59,130,246,0.15);border:1px solid rgba(59,130,246,0.3);border-radius:12px;font-size:10px;color:var(--blue)}}
.sec-title{{font-size:12px;font-weight:700;color:var(--dim);margin:10px 0 6px;text-transform:uppercase;letter-spacing:.5px}}
.card{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:12px;margin-bottom:8px;position:relative}}
.card.ambush{{border-left:3px solid var(--red)}}
.card.sell{{border-left:3px solid var(--orange)}}
.card.forbid{{border-left:3px solid var(--dim);opacity:.8}}
.card .head{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.card .action-tag{{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:800;color:#fff}}
.card.ambush .action-tag{{background:var(--red)}}
.card.sell .action-tag{{background:var(--orange)}}
.card.forbid .action-tag{{background:var(--dim)}}
.card .name{{font-size:15px;font-weight:700;color:#fff}}
.card .code{{font-size:11px;color:var(--dim)}}
.card .concept{{font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(59,130,246,0.15);color:var(--blue);margin-left:4px}}
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
.forbid-list{{font-size:10px;color:var(--dim)}}
.forbid-list .item{{padding:4px 8px;margin-bottom:2px;background:rgba(239,68,68,0.05);border-radius:4px;border-left:2px solid var(--red)}}
.tabs{{display:flex;margin-bottom:12px;background:var(--card);border-radius:10px;padding:3px}}
.tab{{flex:1;text-align:center;padding:8px;font-size:12px;font-weight:600;color:var(--dim);border-radius:8px;cursor:pointer}}
.tab.active{{background:rgba(59,130,246,0.2);color:var(--blue)}}
.page{{display:none}}
.page.active{{display:block}}
.bk-table{{width:100%;font-size:10px;border-collapse:collapse}}
.bk-table th{{color:var(--dim);text-align:left;padding:4px 6px;border-bottom:1px solid var(--border);font-weight:600}}
.bk-table td{{padding:5px 6px;border-bottom:1px solid rgba(255,255,255,.04)}}
.bk-table .pos{{color:var(--red)}}
.bk-table .neg{{color:var(--green)}}
.live-badge{{display:inline-block;padding:2px 6px;background:rgba(34,197,94,0.2);color:#22c55e;border-radius:4px;font-size:9px;margin-left:6px;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.3}}}}
.refresh-info{{text-align:center;font-size:9px;color:var(--dim);margin-top:12px}}
.empty{{text-align:center;padding:30px;color:var(--dim);font-size:13px}}
</style>
</head>
<body>
<div class="tabs">
  <div class="tab active">战情</div>
  <div class="tab">复盘</div>
</div>
<div class="page active">
  <div class="bar">
    <span class="time" id="timeDisplay">--:--:--</span>
    <span class="live-badge">LIVE</span>
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
<div class="refresh-info">实时快照 · 每 3 秒本地刷新</div>

<script>
var LIVE_DATA = {data_json};

function esc(s){{return String(s||'').replace(/</g,'&lt;').replace(/>/g,'&gt;')}}
function renderCards(id,cards){{
  var el=document.getElementById(id);
  if(!cards||!cards.length){{el.innerHTML='<div class="empty">暂无</div>';return}}
  el.innerHTML=cards.map(function(c){{
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
        ((c.risk_tags||[]).map(function(r){{return '<span class="risk">'+esc(r)+'</span>'}}).join(''))+
      '</div></div>';
  }}).join('');
}}
function render() {{
  var d=LIVE_DATA;
  var sentiment = d.sentiment||{{}};
  document.getElementById('timeDisplay').textContent=new Date().toLocaleTimeString('zh-CN',{{hour12:false}});
  var sb=document.getElementById('sentBadge');sb.textContent=sentiment.status||'--';sb.className='sentiment '+(sentiment.status||'');
  document.getElementById('ambushCount').textContent=d.ambush_count||0;
  document.getElementById('sellCount').textContent=d.sell_count||0;
  document.getElementById('forbidCount').textContent=d.forbidden_count||0;
  var th=document.getElementById('themesRow');
  th.innerHTML=(d.main_themes||[]).map(function(t){{return '<span class="theme-tag">'+esc(t.name)+' '+esc(t.stage_name||'')+'</span>'}}).join('')||'<span style="font-size:10px;color:var(--dim)">暂无主线</span>';
  renderCards('ambushCards',d.ambush||[]);
  renderCards('sellCards',d.sell||[]);
  var cc=document.getElementById('conceptChain');
  if(!d.concept_chain||!d.concept_chain.length){{cc.innerHTML='<div class="empty">暂无传导</div>'}}
  else{{
    cc.innerHTML=d.concept_chain.map(function(c){{
      var dhtml='';
      if(c.dragons&&c.dragons.length){{
        dhtml=c.dragons.map(function(dr){{
          var emoji={{1:'L1',2:'L2',3:'L3'}}[dr.rank]||'';
          return emoji+esc(dr.name)+' +'+dr.stock_pct+'%';
        }}).join(' | ');
      }}
      return '<div class="chain"><div class="line"><span class="dot s'+c.stage+'"></span><b>'+esc(c.concept)+'</b> <span style="color:var(--dim);font-size:10px">阶段'+c.stage+'</span> <span style="color:var(--dim);font-size:9px">涨停'+c.limit_up+'</span></div>'+(dhtml?'<div class="line" style="padding-left:12px;font-size:10px;color:var(--dim)">'+dhtml+'</div>':'')+'</div>';
    }}).join('');
  }}
  var fb=document.getElementById('forbiddenList');
  fb.innerHTML=(d.forbidden||[]).map(function(f){{return '<div class="forbid-list"><div class="item">'+esc(f.name)+'('+f.code+') - '+esc(f.reason)+' '+((f.risks||[]).map(function(r){{return esc(r)}}).join(' '))+'</div></div>'}}).join('')||'<div class="empty">暂无</div>';
}}
render();
setInterval(function(){{document.getElementById('timeDisplay').textContent=new Date().toLocaleTimeString('zh-CN',{{hour12:false}})}},1000);
</script>
</body>
</html>'''

os.makedirs(OUTPUT_DIR, exist_ok=True)
out_path = os.path.join(OUTPUT_DIR, 'index.html')
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(html)
print(f"Generated: {out_path} ({len(html)} bytes)")
