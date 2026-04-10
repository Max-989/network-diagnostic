const HOST_NAME = 'com.network_diagnostic.host';
const DIAG_ICONS = {dns:'🔍',gateway:'🌐',external:'🌍',websites:'💻',ports:'🔌',proxy:'🔗',wifi:'📶',bandwidth:'⚡',traceroute:'🗺️'};
const REPAIR_NAMES = {
  flush_dns:'刷新DNS',reset_winsock:'重置Winsock',reset_tcp:'重置TCP/IP',
  release_renew:'释放重获IP',flush_arp:'清空ARP',reset_proxy:'关闭代理',
  set_dns:'设置阿里DNS',reset_adapter:'重置网卡',repair_all:'一键全部修复'
};
const REPAIR_DESCS = {
  flush_dns:'清除DNS缓存',reset_winsock:'修复网络层',reset_tcp:'重置协议栈',
  release_renew:'重新获取IP',flush_arp:'清除地址缓存',reset_proxy:'禁用系统代理',
  set_dns:'切换DNS服务器',reset_adapter:'重启网卡'
};

let isDiagnosing = false;
let lastResults = [];
let lastFailed = [];

function statusBadge(s){
  if(s==='ok') return '<span class="badge badge-ok">正常</span>';
  if(s==='warn') return '<span class="badge badge-warn">警告</span>';
  return '<span class="badge badge-error">异常</span>';
}

function renderCard(item){
  const detail = item.detail
    ? '<div class="card-detail">'+item.detail+'</div>'
    : item.items
      ? '<div class="card-detail">'+item.items.map(i=>
          '<div>'+(i.status==='ok'?'✅':i.status==='warn'?'⚠️':'❌')+' '+i.label+': '+i.detail+' <span style="color:var(--text3)">'+i.cost_ms+'ms</span></div>'
        ).join('')+'</div>'
      : '';
  return '<div class="card"><div class="card-header"><div class="card-name">'+(DIAG_ICONS[item.key]||'📋')+' '+item.name+'</div>'+statusBadge(item.status)+'</div>'+detail+'</div>';
}

function showSummary(results, totalTime){
  const el = document.getElementById('summary');
  const ok=results.filter(r=>r.status==='ok').length, warn=results.filter(r=>r.status==='warn').length, err=results.filter(r=>r.status==='error').length;
  let advice=[];
  if(err>0) advice.push('异常: '+results.filter(r=>r.status==='error').map(r=>r.name).join('、')+'，建议使用修复工具');
  if(err===0&&warn===0) advice.push('✅ 网络状态良好');
  el.innerHTML='<div class="section-title">📊 诊断报告</div><div class="summary-grid"><div><div class="summary-value">'+results.length+'</div><div class="summary-label">检测项</div></div><div><div class="summary-value" style="color:var(--green)">'+ok+'</div><div class="summary-label">正常</div></div><div><div class="summary-value" style="color:var(--yellow)">'+warn+'</div><div class="summary-label">警告</div></div><div><div class="summary-value" style="color:var(--red)">'+err+'</div><div class="summary-label">异常</div></div><div><div class="summary-value">'+totalTime+'</div><div class="summary-label">耗时ms</div></div></div><div class="summary-advice">'+advice.map(a=>'<div>'+a+'</div>').join('')+'</div>';
  el.className='summary active';
}

function openModal(id){document.getElementById(id).classList.add('active');}
function closeModal(id){document.getElementById(id).classList.remove('active');}

function doRepair(key, failedItems){
  closeModal('confirmModal');
  document.querySelectorAll('.repair-btn').forEach(b=>b.disabled=true);
  document.getElementById('btnRepairAll').disabled=true;
  const msg={action:'repair',repair_action:key};
  if(failedItems) msg.failed_items=failedItems;
  chrome.runtime.sendNativeMessage(HOST_NAME, msg, function(resp){
    document.querySelectorAll('.repair-btn').forEach(b=>b.disabled=false);
    document.getElementById('btnRepairAll').disabled=false;
    if(chrome.runtime.lastError){
      document.getElementById('resultTitle').textContent='执行失败';
      document.getElementById('resultContent').textContent=chrome.runtime.lastError.message;
      openModal('resultModal');return;
    }
    if(resp&&resp.type==='repair_result'){
      const d=resp.data;
      if(d.details){
        const names=(d.matched_repairs||[]).map(k=>REPAIR_NAMES[k]||k);
        let html='🔧 执行: '+names.join(', ')+'\n\n'+d.details.map(r=>'['+(r.status==='ok'?'✓':'✗')+'] '+(REPAIR_NAMES[r.action]||r.action)+'\n'+r.output).join('\n\n');
        document.getElementById('resultTitle').textContent='修复结果';
        document.getElementById('resultContent').textContent=html;
      } else {
        document.getElementById('resultTitle').textContent=(REPAIR_NAMES[key]||key)+' - '+(d.status==='ok'?'成功':'失败');
        document.getElementById('resultContent').textContent=d.output||'';
      }
      openModal('resultModal');
    }
  });
}

function initRepairGrid(){
  const grid=document.getElementById('repairGrid');
  ['flush_dns','reset_winsock','reset_tcp','release_renew','flush_arp','reset_proxy','set_dns','reset_adapter'].forEach(k=>{
    const div=document.createElement('div');
    div.className='repair-btn';
    div.innerHTML='<div class="repair-btn-name">'+REPAIR_NAMES[k]+'</div><div class="repair-btn-desc">'+REPAIR_DESCS[k]+'</div>';
    div.addEventListener('click',function(){doRepair(k);});
    grid.appendChild(div);
  });
}

document.getElementById('btnRepairAll').addEventListener('click',function(){
  if(!lastFailed.length){
    document.getElementById('confirmTitle').textContent='无需修复';
    document.getElementById('confirmMsg').textContent='当前没有异常项。';
    document.getElementById('confirmBtn').onclick=function(){closeModal('confirmModal');};
    openModal('confirmModal');return;
  }
  const names=lastResults.filter(r=>r.status!=='ok').map(r=>r.name);
  document.getElementById('confirmTitle').textContent='🤖 智能修复';
  document.getElementById('confirmMsg').textContent='发现'+names.length+'项异常：'+names.join('、')+'\n\n是否立即修复？';
  document.getElementById('confirmBtn').onclick=function(){doRepair('repair_all',lastFailed);};
  openModal('confirmModal');
});

document.getElementById('confirmCancel').addEventListener('click',function(){closeModal('confirmModal');});
document.getElementById('resultClose').addEventListener('click',function(){closeModal('resultModal');});

document.getElementById('btnDiag').addEventListener('click',function(){
  if(isDiagnosing) return;
  isDiagnosing=true;
  const btn=this;
  btn.disabled=true;
  document.getElementById('diagIcon').innerHTML='<div class="spinner"></div>';
  document.getElementById('diagText').textContent='诊断中...';
  document.getElementById('progressFill').classList.add('active');
  document.getElementById('progressFill').style.width='0%';
  document.getElementById('offlineMsg').style.display='none';
  document.getElementById('diagSection').style.display='block';
  document.getElementById('repairSection').style.display='none';
  document.getElementById('summary').className='summary';

  const keys=['dns','gateway','external','websites','ports','proxy','wifi','bandwidth','traceroute'];
  const cards=document.getElementById('diagCards');
  cards.innerHTML=keys.map(k=>'<div class="card running" id="card_'+k+'"><div class="card-header"><div class="card-name">⏳ '+k+'</div><span class="badge badge-loading">检测中...</span></div></div>').join('');

  lastResults=[]; lastFailed=[];

  chrome.runtime.sendNativeMessage(HOST_NAME, {action:'diagnose'}, function(resp){
    btn.disabled=false;
    document.getElementById('diagIcon').textContent='🔍';
    document.getElementById('diagText').textContent='重新诊断';
    document.getElementById('progressFill').classList.remove('active');
    isDiagnosing=false;

    if(chrome.runtime.lastError){
      cards.innerHTML='<div class="card" style="text-align:center;padding:16px"><div style="color:var(--red);font-size:.9rem">❌ 诊断引擎启动失败</div><div style="color:var(--text3);font-size:.75rem;margin-top:6px">'+chrome.runtime.lastError.message+'</div><div style="color:var(--text3);font-size:.72rem;margin-top:4px">请确保 Python 已安装</div></div>';
      return;
    }

    if(!resp){cards.innerHTML='<div class="card" style="color:var(--red);padding:12px;text-align:center">❌ 无响应</div>';return;}

    // resp 是所有结果的数组（native host 一次性返回）
    if(Array.isArray(resp.items)){
      lastResults=resp.items;
      lastResults.forEach(function(item){
        const card=document.getElementById('card_'+item.key);
        if(card) card.outerHTML=renderCard(item);
      });
      lastFailed=lastResults.filter(r=>r.status==='error'||r.status==='warn').map(r=>r.key);
      showSummary(lastResults, resp.total_time_ms||0);
      document.getElementById('diagSection').style.display='block';
      document.getElementById('repairSection').style.display='block';
      if(lastFailed.length>0){
        setTimeout(function(){
          const names=lastResults.filter(r=>r.status!=='ok').map(r=>r.name);
          document.getElementById('confirmTitle').textContent='🤖 诊断完成';
          document.getElementById('confirmMsg').textContent='发现'+names.length+'项异常：'+names.join('、')+'\n\n是否立即修复？';
          document.getElementById('confirmBtn').onclick=function(){doRepair('repair_all',lastFailed);};
          openModal('confirmModal');
        },400);
      }
    }
  });
});

initRepairGrid();
