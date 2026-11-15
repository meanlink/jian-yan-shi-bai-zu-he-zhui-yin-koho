"""发送端单页界面（原生 HTML/CSS/JS，无打包步骤）。"""

SENDER_UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>归档简报投递对账台</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--line:#e3e6ea;--ink:#1f2733;--muted:#6b7480;
--blue:#2563eb;--green:#0e7c49;--amber:#92600a;--red:#b42318;--violet:#6d3fc4;}
*{box-sizing:border-box}
body{margin:0;font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
background:var(--bg);color:var(--ink);font-size:14px}
header{background:#111827;color:#fff;padding:14px 24px;display:flex;
align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:600}
header a{color:#9cc0ff;text-decoration:none;font-size:13px}
main{max-width:1280px;margin:20px auto;padding:0 20px;display:grid;gap:16px}
.grid{display:grid;gap:12px}
.cards{grid-template-columns:repeat(4,1fr)}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:14px;margin:0 0 10px;color:var(--muted);font-weight:600}
.metric{font-size:26px;font-weight:700}
.metric small{font-size:12px;color:var(--muted);font-weight:400;margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;background:#fafbfc}
tr.clickable{cursor:pointer}
tr.clickable:hover{background:#f8faff}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.b-PENDING{background:#eef2f7;color:#475569}
.b-IN_FLIGHT{background:#fef3c7;color:var(--amber)}
.b-RECEIVED{background:#dcfce7;color:var(--green)}
.b-CONFLICT{background:#fee4e2;color:var(--red)}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
button{border:1px solid var(--line);background:#fff;border-radius:7px;padding:6px 12px;
cursor:pointer;font-size:13px;color:var(--ink)}
button:hover{border-color:var(--blue);color:var(--blue)}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
button.danger{color:var(--red);border-color:#f0b8b2}
button:disabled{opacity:.45;cursor:not-allowed}
input,select,textarea{border:1px solid var(--line);border-radius:7px;padding:6px 9px;font-size:13px;width:100%}
textarea{min-height:70px;font-family:ui-monospace,Menlo,Consolas,monospace}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
.row>div{flex:1;min-width:140px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
.switch{display:flex;align-items:center;gap:7px;font-size:13px}
.switch input{width:auto}
.fp-ok{color:var(--green);font-weight:600}
.fp-bad{color:var(--red);font-weight:600}
.muted{color:var(--muted)}
.dialog-back{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;
align-items:center;justify-content:center;z-index:20;padding:20px}
.dialog-back.open{display:flex}
.dialog{background:#fff;border-radius:12px;width:min(980px,95vw);max-height:92vh;overflow:auto;padding:20px}
.dialog h3{margin:0 0 12px}
.kv{display:grid;grid-template-columns:110px 1fr;gap:6px 12px;font-size:13px;margin-bottom:12px}
.kv .k{color:var(--muted)}
pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;font-size:12px;
overflow:auto;max-height:260px;margin:6px 0}
.diff-add{background:rgba(34,197,94,.25);display:block}
.diff-del{background:rgba(239,68,68,.25);display:block}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
background:#111827;color:#fff;padding:9px 18px;border-radius:8px;font-size:13px;
opacity:0;transition:opacity .2s;z-index:30;max-width:80vw}
.toast.show{opacity:1}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:800px){.two{grid-template-columns:1fr}}
.pill{font-size:11px;padding:1px 7px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.chaos-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px}
.chaos-item{border:1px solid var(--line);border-radius:8px;padding:9px 11px}
.chaos-item b{display:block;margin-bottom:4px}
.chaos-item small{color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>归档简报投递对账台（发送端）</h1>
  <a href="/ui" target="_blank">↗ 打开接收端控制台</a>
  <span class="pill" id="clock"></span>
  <button onclick="loadAll()">刷新</button>
  <button class="primary" onclick="tick()">手动调度一次</button>
  <button onclick="recover()">启动恢复对账</button>
</header>

<main>
  <section class="grid cards">
    <div class="card"><h2>待发 PENDING</h2><div class="metric" id="m-PENDING">0</div></div>
    <div class="card"><h2>待确认 IN_FLIGHT</h2><div class="metric" id="m-IN_FLIGHT">0</div></div>
    <div class="card"><h2>已接收 RECEIVED</h2><div class="metric" id="m-RECEIVED">0</div></div>
    <div class="card"><h2>冲突隔离 CONFLICT</h2><div class="metric" id="m-CONFLICT">0</div></div>
  </section>

  <section class="card">
    <h2>归档（旧版本原件永久保留，新版本另存）</h2>
    <div class="row">
      <div style="flex:2"><label>简报标题</label><input id="arc-title" placeholder="周度质量复核简报"></div>
      <div style="flex:4"><label>内容 payload（JSON 对象）</label>
        <textarea id="arc-payload">{"week":"2026-W46","rules":12,"lift_max":2.71,"note":"常规归档"}</textarea></div>
      <div style="flex:0 0 auto"><button class="primary" onclick="createArchive()">写入归档</button></div>
    </div>
    <table style="margin-top:10px"><thead><tr>
      <th>归档ID</th><th>标题</th><th>版本</th><th>指纹 SHA256</th><th>替代关系</th></tr></thead>
      <tbody id="archive-rows"></tbody></table>
  </section>

  <section class="card">
    <h2>投递计划（暂停停止新投递；改目的地不挪动在途记录）</h2>
    <div class="row">
      <div><label>归档</label><select id="plan-arc"></select></div>
      <div><label>目的地</label><select id="plan-dest"></select></div>
      <div style="flex:0 0 auto"><button class="primary" onclick="createPlan()">建计划</button></div>
    </div>
    <table style="margin-top:10px"><thead><tr>
      <th>计划ID</th><th>归档</th><th>当前目的地</th><th>状态</th><th>操作</th></tr></thead>
      <tbody id="plan-rows"></tbody></table>
  </section>

  <section class="card">
    <h2>投递记录（按归档与目的地跟踪，点击查看双方指纹/尝试/差异）</h2>
    <table><thead><tr>
      <th>投递ID</th><th>归档/版本</th><th>目的地（快照）</th><th>状态</th>
      <th>尝试</th><th>本地指纹</th><th>接收端指纹</th><th>更新时间</th></tr></thead>
      <tbody id="delivery-rows"></tbody></table>
  </section>

  <section class="card">
    <h2>演示故障注入</h2>
    <div class="chaas-grid chaos-grid" id="chaos-box"></div>
    <p class="muted" style="margin-bottom:0">drop_receive：接收端丢弃投递报文（超时→查事实→不重复重试）；
    drop_facts：接收端事实查询失败；mutate_payload：接收端内容被改写（指纹冲突隔离）；
    crash_after_accept：发送端在对端接收成功后、本地记账前崩溃。</p>
  </section>

  <section class="card">
    <h2>审计轨迹</h2>
    <table><thead><tr><th>时间</th><th>动作</th><th>对象</th><th>详情</th></tr></thead>
    <tbody id="audit-rows"></tbody></table>
  </section>
</main>

<div class="dialog-back" id="dialog-back" onclick="if(event.target===this)closeDialog()">
  <div class="dialog" id="dialog"></div>
</div>
<div class="toast" id="toast"></div>

<script>
const $=s=>document.querySelector(s);
const fmt=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'—';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let overview={},archives=[],deliveries=[],audits=[];

function toast(msg,bad){const t=$('#toast');t.textContent=msg;t.style.background=bad?'#b42318':'#111827';
t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600)}
async function api(method,url,body){
  const opt={method,headers:{'Content-Type':'application/json'}};
  if(body)opt.body=JSON.stringify(body);
  const r=await fetch(url,opt);const data=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));
  return data;
}
async function loadAll(){
  overview=await api('GET','/api/overview');
  for(const k of ['PENDING','IN_FLIGHT','RECEIVED','CONFLICT']){
    $('#m-'+k).textContent=overview.counts[k]||0;
  }
  archives=(await api('GET','/api/archives')).archives;
  deliveries=(await api('GET','/api/deliveries')).deliveries;
  audits=(await api('GET','/api/audits')).audits;
  renderArchives();renderPlans();renderDeliveries();renderChaos();renderAudits();
  $('#clock').textContent='数据时间 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
}
function renderArchives(){
  $('#archive-rows').innerHTML=archives.map(a=>`<tr>
    <td class="mono">${a.id}</td><td>${esc(a.title)}</td><td>v${a.version}</td>
    <td class="mono">${a.fingerprint.slice(0,16)}…</td>
    <td>${a.superseded_by?'<span class="pill">被 '+a.superseded_by+' 替代</span>':'<span class="pill">当前版</span>'}</td>
  </tr>`).join('');
  $('#plan-arc').innerHTML=archives.map(a=>`<option value="${a.id}">${esc(a.title)} v${a.version} (${a.id})</option>`).join('');
}
function renderPlans(){
  const dests=overview.destinations||[];
  $('#plan-dest').innerHTML=dests.map(d=>`<option value="${d.code}">${esc(d.name)} (${d.code})</option>`).join('');
  $('#plan-rows').innerHTML=(overview.plans||[]).map(p=>{
    const arc=archives.find(a=>a.id===p.archive_id);
    return `<tr>
      <td class="mono">${p.id}</td>
      <td>${arc?esc(arc.title)+' v'+arc.version:p.archive_id}</td>
      <td class="mono">${p.dest_code}</td>
      <td>${p.paused?'<span class="badge b-PENDING">已暂停</span>':'<span class="badge b-RECEIVED">运行中</span>'}</td>
      <td style="display:flex;gap:6px;flex-wrap:wrap">
        <button onclick="enqueue('${p.id}')">生成投递</button>
        ${p.paused
          ?`<button onclick="planPause('${p.id}',false)">恢复</button>`
          :`<button class="danger" onclick="planPause('${p.id}',true)">暂停</button>`}
        <select id="dest-${p.id}" style="width:auto">${dests.map(d=>`<option ${d.code===p.dest_code?'selected':''}>${d.code}</option>`).join('')}</select>
        <button onclick="changeDest('${p.id}')">改目的地</button>
      </td></tr>`;
  }).join('');
}
function renderDeliveries(){
  $('#delivery-rows').innerHTML=deliveries.map(d=>`<tr class="clickable" onclick="openDetail('${d.id}')">
    <td class="mono">${d.id}</td>
    <td>${d.archive_id}<br><span class="pill">v${d.archive_version}</span></td>
    <td class="mono">${d.dest_code}</td>
    <td><span class="badge b-${d.status}">${d.status}</span></td>
    <td>${d.attempt_count} 次</td>
    <td class="mono">${(d.fingerprint||'').slice(0,12)}…</td>
    <td class="mono">${d.remote_fingerprint?d.remote_fingerprint.slice(0,12)+'…':'<span class="muted">未记账</span>'}</td>
    <td>${fmt(d.updated_at)}</td>
  </tr>`).join('');
}
function renderAudits(){
  $('#audit-rows').innerHTML=audits.slice(0,30).map(a=>`<tr>
    <td>${fmt(a.created_at)}</td><td>${esc(a.action)}</td>
    <td class="mono">${esc(a.entity)}</td><td>${esc(a.detail||'')}</td></tr>`).join('');
}
function renderChaos(){
  const c=overview.chaos||{sender:{},receiver_flags:[]};
  const defs=[
    ...c.receiver_flags.map(n=>({side:'receiver',name:n})),
    ...Object.keys(c.sender||{}).map(n=>({side:'sender',name:n}))];
  const labels={drop_receive:['接收端丢弃投递','报文无响应，演示超时后先查事实'],
    lose_receipt:['回执回程丢失','事实已落库但响应丢失，查事实补齐不重收'],
    drop_facts:['接收端查询失败','事实接口不可用，退避重试对账'],
    mutate_payload:['接收端内容改写','同一投递指纹不符 → 冲突隔离'],
    crash_after_accept:['发送端记账前崩溃','对端已接收、本地待确认，恢复补齐']};
  $('#chaos-box').innerHTML=defs.map(d=>{
    const on=d.side==='sender'?c.sender[d.name]:false;
    const[lb,desc]=labels[d.name]||[d.name,''];
    return `<div class="chaos-item"><b>${lb} <span class="pill">${d.side}</span></b>
      <small>${desc}<br>开关：${d.name}</small>
      <label class="switch" style="margin-top:6px"><input type="checkbox" ${on?'checked':''}
        onchange="toggleChaos('${d.side}','${d.name}',this.checked)"><span>${on?'已启用':'已关闭'}</span></label></div>`;
  }).join('');
}
async function toggleChaos(side,name,on){
  try{await api('POST','/api/chaos',{side,name,enabled:on});toast('故障开关已更新');loadAll()}
  catch(e){toast(e.message,true)}
}
async function createArchive(){
  let payload;
  try{payload=JSON.parse($('#arc-payload').value)}catch(e){return toast('payload 不是合法 JSON',true)}
  await api('POST','/api/archives',{title:$('#arc-title').value||'未命名简报',payload});
  toast('归档已写入（旧版本保留原件）');loadAll();
}
async function createPlan(){
  await api('POST','/api/plans',{archive_id:$('#plan-arc').value,dest_code:$('#plan-dest').value});
  toast('投递计划已创建');loadAll();
}
async function enqueue(id){try{await api('POST',`/api/plans/${id}/enqueue`);toast('已生成待发投递');loadAll()}
  catch(e){toast(e.message,true)}}
async function planPause(id,paused){
  await api('POST',`/api/plans/${id}/${paused?'pause':'resume'}`);
  toast(paused?'已暂停：停止新投递，在途记录仍可对账':'已恢复');loadAll();
}
async function changeDest(id){
  const code=$('#dest-'+id).value;
  await api('POST',`/api/plans/${id}/destination`,{dest_code:code});
  toast('目的地已变更，在途记录保留原目的地快照');loadAll();
}
async function tick(){try{const r=await api('POST','/api/tick');
  toast(r.ran?'已执行一个调度周期':'当前无到期投递');loadAll()}catch(e){toast(e.message,true)}}
async function recover(){const r=await api('POST','/api/recover');
  toast(r.recovered.length?('恢复对账补齐：'+r.recovered.join(', ')):'无待补齐记录');loadAll()}

// ---------- 详情弹窗：双方指纹 + 尝试记录 + 内容差异 ----------
function jsonDiff(a,b){
  const keys=[...new Set([...Object.keys(a||{}),...Object.keys(b||{})])].sort();
  return keys.map(k=>{
    const va=a?a[k]:undefined,vb=b?b[k]:undefined;
    if(JSON.stringify(va)===JSON.stringify(vb))return null;
    return {k,va,vb};
  }).filter(Boolean);
}
async function openDetail(id){
  const data=await api('GET','/api/deliveries/'+id);
  const d=data.delivery,remote=data.remote;
  const localPayload=JSON.parse(d.payload);
  let remotePayload=null;
  if(remote.found){try{remotePayload=JSON.parse(remote.fact.content)}catch(e){}}
  const fpMatch=remote.found&&remote.fact.fingerprint===d.fingerprint;
  const diffs=remotePayload?jsonDiff(localPayload,remotePayload):[];
  $('#dialog').innerHTML=`
    <h3>投递详情 <span class="mono" style="font-size:13px">${d.id}</span>
      <span class="badge b-${d.status}">${d.status}</span></h3>
    <div class="kv">
      <div class="k">归档/版本</div><div>${d.archive_id} v${d.archive_version}</div>
      <div class="k">目的地快照</div><div>${d.dest_code}</div>
      <div class="k">尝试次数</div><div>${d.attempt_count}（下次动作 ${fmt(d.next_attempt_at)}）</div>
      <div class="k">冲突说明</div><div class="${d.status==='CONFLICT'?'fp-bad':''}">${esc(d.conflict_detail||'—')}</div>
    </div>
    <div class="two">
      <div><b>发送端指纹</b><pre class="mono">${d.fingerprint}</pre></div>
      <div><b>接收端指纹</b><pre class="mono">${remote.found?remote.fact.fingerprint:(remote.error?'查询失败：'+esc(remote.error):'无接收事实（未收到）')}</pre>
        ${remote.found?`<div class="${fpMatch?'fp-ok':'fp-bad'}">${fpMatch?'✓ 双方指纹一致':'✗ 指纹不符，已隔离'}</div>`:''}</div>
    </div>
    <h3 style="margin-top:16px">尝试记录（${data.attempts.length}）</h3>
    <table><thead><tr><th>时间</th><th>环节</th><th>结果</th><th>HTTP</th><th>详情</th></tr></thead>
      <tbody>${data.attempts.map(a=>`<tr><td>${fmt(a.created_at)}</td><td>${a.kind}</td>
        <td>${a.outcome}</td><td>${a.http_status||''}</td><td>${esc(a.detail||'')}</td></tr>`).join('')}</tbody></table>
    <h3 style="margin-top:16px">内容比对</h3>
    ${diffs.length?`<table><thead><tr><th>字段</th><th>发送端</th><th>接收端</th></tr></thead><tbody>
      ${diffs.map(x=>`<tr><td class="mono">${x.k}</td>
        <td><span class="diff-del">${esc(JSON.stringify(x.va))}</span></td>
        <td><span class="diff-add">${esc(JSON.stringify(x.vb))}</span></td></tr>`).join('')}</tbody></table>`
      :'<p class="muted">'+(remote.found?'内容一致，无差异':'尚无接收端内容可比对')+'</p>'}
    <div class="two" style="margin-top:12px">
      <div><b>本地原件</b><pre>${esc(JSON.stringify(localPayload,null,2))}</pre></div>
      <div><b>接收端原件</b><pre>${remotePayload?esc(JSON.stringify(remotePayload,null,2)):'—'}</pre></div>
    </div>
    <div class="row" style="margin-top:14px">
      <button class="primary" onclick="reconcileOne('${d.id}')">立即对账（查接收事实）</button>
      <button onclick="simulateLateReceipt('${d.id}')">模拟晚到回执（正确指纹）</button>
      <button class="danger" onclick="simulateWrongReceipt('${d.id}')">模拟晚到回执（错误指纹）</button>
      <button onclick="closeDialog()">关闭</button>
    </div>`;
  $('#dialog-back').classList.add('open');
}
function closeDialog(){$('#dialog-back').classList.remove('open')}
async function reconcileOne(id){
  try{const r=await api('POST',`/api/deliveries/${id}/reconcile`);
    toast(r.settled?'对账完成':'接收端无事实');loadAll();openDetail(id)}
  catch(e){toast(e.message,true);loadAll();openDetail(id)}
}
async function lateReceipt(id,fingerprint){
  try{await api('POST',`/api/deliveries/${id}/late-receipt`,{fingerprint,accepted_at:Date.now()/1000});
    toast('晚到回执已处理');loadAll();openDetail(id)}
  catch(e){toast(e.message,true);loadAll();openDetail(id)}
}
function simulateLateReceipt(id){const d=deliveries.find(x=>x.id===id);lateReceipt(id,d.fingerprint)}
function simulateWrongReceipt(id){lateReceipt(id,'deadbeef'.repeat(8))}
loadAll();setInterval(loadAll,4000);
</script>
</body>
</html>
"""
