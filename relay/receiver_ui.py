"""接收端控制台页面：展示接收事实、重复投递计数与回执签发记录。"""

RECEIVER_UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>接收系统控制台</title>
<style>
body{margin:0;font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
background:#f5f6f8;color:#1f2733;font-size:14px}
header{background:#0f3d2e;color:#fff;padding:14px 24px;display:flex;gap:16px;align-items:center}
header h1{font-size:17px;margin:0}
header a{color:#a7e9cd;text-decoration:none;font-size:13px}
main{max-width:1180px;margin:20px auto;padding:0 20px;display:grid;gap:16px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #e3e6ea;vertical-align:top}
th{color:#6b7480;background:#fafbfc}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;
background:#dcfce7;color:#0e7c49}
.dup{background:#fef3c7;color:#92600a}
button{border:1px solid #e3e6ea;background:#fff;border-radius:7px;padding:6px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:#0e7c49;color:#0e7c49}
.muted{color:#6b7480}
pre{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:8px;font-size:12px;
overflow:auto;max-height:220px;margin:6px 0}
</style>
</head>
<body>
<header>
  <h1>接收系统控制台（接收端事实库）</h1>
  <a href="/app" target="_blank">↗ 打开发送对账台</a>
  <button onclick="load()">刷新</button>
</header>
<main>
  <section class="card">
    <h2 style="margin:0 0 10px;font-size:14px;color:#6b7480">接收事实（按投递 ID 幂等，重复提交只计数不重复接收）</h2>
    <table><thead><tr>
      <th>投递ID</th><th>归档</th><th>目的地</th><th>接收指纹</th>
      <th>重复次数</th><th>接收时间</th><th>内容</th></tr></thead>
      <tbody id="rows"></tbody></table>
  </section>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'—';
async function load(){
  const r=await fetch('/api/facts');const data=await r.json();
  document.querySelector('#rows').innerHTML=(data.facts||[]).map(f=>`
    <tr><td class="mono">${f.delivery_id}</td><td>${f.archive_id}</td>
    <td class="mono">${f.destination}</td>
    <td class="mono">${f.fingerprint.slice(0,20)}…</td>
    <td>${f.duplicate_count>0?`<span class="pill dup">重复 ${f.duplicate_count} 次（未重复接收）</span>`:'<span class="pill">首次接收</span>'}</td>
    <td>${fmt(f.accepted_at)}</td>
    <td><details><summary>查看原件</summary><pre>${esc(f.content)}</pre></details></td></tr>`).join('')
      ||'<tr><td colspan="7" class="muted">暂无接收事实</td></tr>';
}
load();setInterval(load,4000);
</script>
</body>
</html>
"""
