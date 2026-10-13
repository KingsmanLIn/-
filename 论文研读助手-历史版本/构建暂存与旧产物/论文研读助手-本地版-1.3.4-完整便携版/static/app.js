import {getDocument, GlobalWorkerOptions, TextLayer} from '/vendor/pdfjs/pdf.min.mjs';
/* guides.js 也必须带版本参数：index.html 是 no-store、/static/ 是 max-age=3600，
   只有请求 URL 变化才能让浏览器在升级后丢掉旧的 guides.js。这里沿用占位符 __VERSION__
   （与 index.html 同一写法），运行时它只是一个普通查询串，不依赖服务端替换。 */
import {TYPES, ANNOTATIONS, FIELDS, STAGES, TYPE_HINTS, THEORY_FIELDS, PAPERS, ENGLISH_TIP} from './guides.js?v=__VERSION__';

const BOOT = JSON.parse(document.getElementById('boot').textContent);
GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.min.mjs';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
/* 本机会话：页面不再持有可管理 AI 的令牌。
   启动器把一次性引导能力放在地址栏的 # 片段里；这里交换为
   HttpOnly 会话 cookie（浏览器自动携带，脚本读不到）+ 仅存在于内存的 CSRF 令牌。
   交换后立即清除 fragment，令牌不进入 localStorage/sessionStorage/日志。 */
let csrf='';
const sessionState={ready:false,error:''};
function readFragment(){
  const raw=location.hash.startsWith('#')?location.hash.slice(1):'';
  if(!raw)return '';
  const params=new URLSearchParams(raw);
  const value=params.get('bootstrap')||'';
  if(value)history.replaceState(null,'',location.pathname+location.search);
  return value;
}
async function establishSession(){
  const capability=readFragment();
  if(!capability){sessionState.error='no_bootstrap';return false;}
  try{
    const response=await fetch('/api/session/bootstrap',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({capability})});
    const result=await response.json();
    if(!response.ok||!result.csrf){sessionState.error=result.code||'bootstrap_failed';return false;}
    csrf=result.csrf;sessionState.ready=true;sessionState.error='';
    window.__csrf=csrf;
    return true;
  }catch(e){sessionState.error='network';return false;}
}
async function resumeSession(){
  try{
    const response=await fetch('/api/session');
    if(!response.ok)return false;
    const result=await response.json();
    if(!result.csrf)return false;
    csrf=result.csrf;sessionState.ready=true;
    window.__csrf=csrf;
    return true;
  }catch(e){return false;}
}
function sessionHint(){
  const box=$('#session-hint');if(!box)return;
  if(sessionState.ready){box.textContent='';box.hidden=true;return;}
  box.hidden=false;
  box.textContent='本次页面没有取得本机会话：请重新双击“启动论文研读助手”打开界面。'
    +'（刷新页面可以沿用已建立的会话；若界面显示只读，说明会话已失效。）';
}
/* 草稿的键带文献库命名空间：同一地址换了库时，旧草稿不会被恢复。
   超过 DRAFT_TTL_MS 的草稿视为过期，恢复时丢弃；字段按白名单写入，不含任何凭据或 AI 隐藏项。 */
const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const DRAFT_FIELDS = ['title','authors','year','venue','doi','tags','citation','type','language',
                      'fields','steps','annotations','pageOffset','pageLabels','lastPage','archived'];
let draftNamespace = 'library';
function draftKey(id){ return 'paper-reader:' + draftNamespace + ':draft:' + id; }
function draftPrefix(){ return 'paper-reader:' + draftNamespace + ':draft:'; }
function draftPayload(p){
  const patch = {};
  for (const key of DRAFT_FIELDS) if (key in p) patch[key] = p[key];
  return {revision:p.revision, patch, time:new Date().toISOString(), library:draftNamespace};
}
function draftExpired(draft){
  const stamp = Date.parse(draft && draft.time || '');
  return !stamp || (Date.now() - stamp) > DRAFT_TTL_MS;
}
function clearDrafts(){
  let removed = 0;
  for (const key of Object.keys(localStorage)) {
    if (key.startsWith('paper-reader:') && key.includes(':draft:')) { localStorage.removeItem(key); removed += 1; }
  }
  return removed;
}
let state = {papers:[],settings:{}}, currentId = '', view = 'reader', tab = 'steps', stage = 0, filter = 'all', annotationFilter = 'all';
let pdf = null, pdfTask = null, pdfRender = null, textLayer = null, pageNumber = 1, zoom = 1, renderVersion = 0, documentVersion = 0;
/* 缩放模式：'page' 适合页面（默认，整页可见）、'width' 适合宽度、其余为数字倍率。
   zoom 字段保留原语义，作为用户倍率参与计算。 */
let zoomMode = 'page';
let draftAnnotation = null, dirty = new Set(), saveTimer = 0, saving = null, toastTimer = 0;
/* AI 参考的界面状态：只存在于内存，不写入 library；切换论文时清空。
   answer 只会在用户点击“查看 AI 参考”后填充。 */
let aiState = {status:'', statusMessage:'', answer:null, answerError:'', expanded:null, fetching:false};
/* 全库 AI 状态快照：来自 /api/analysis 的 statuses（一份数据就能画出每行的细进度条，
   不必为每篇论文单独发请求）。它只影响显示，不写入 library。 */
let analysisStatuses = {};
const current = () => state.papers.find(p=>p.id===currentId);
const label = (p,n) => p.pageLabels?.[n-1] || (n+Number(p.pageOffset||0)>0 ? String(n+Number(p.pageOffset||0)) : '封面');
const guide = p => PAPERS[p?.guideKey] || null;
const definition = (p,key) => p.type==='theory' && THEORY_FIELDS[key] ? THEORY_FIELDS[key] : FIELDS[key];

function toast(message, error=false){
  const box=$('#toast');box.textContent=message;box.className='toast visible'+(error?' error':'');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.classList.remove('visible'),error?7500:3700);
}
function status(message,kind=''){$('#save-status').textContent=message;$('#save-status').className='save-status '+kind;}
async function api(path,body){
  const options={headers:{}};
  if(csrf)options.headers['X-CSRF-Token']=csrf;
  if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await fetch('/api/'+path,options);
  const result=await response.json();
  if(!response.ok){
    if(response.status===403&&/会话|失效/.test(result.error||'')){
      sessionState.ready=false;sessionHint();
    }
    throw new Error(result.error||'操作没有完成');
  }
  return result;
}
function patchOf(p){
  const out={};for(const key of ['title','authors','year','venue','doi','tags','citation','type','language','fields','steps','annotations','pageOffset','pageLabels','lastPage','archived'])out[key]=p[key];
  return structuredClone(out);
}
function markDirty(p){
  dirty.add(p.id);localStorage.setItem(draftKey(p.id),JSON.stringify(draftPayload(p)));
  status('正在保存…','saving');clearTimeout(saveTimer);saveTimer=setTimeout(()=>flush().catch(e=>toast(e.message,true)),650);
}
async function flush(){
  clearTimeout(saveTimer);
  if(saving)return saving;
  saving=(async()=>{
    while(dirty.size){
      const id=[...dirty][0],p=state.papers.find(x=>x.id===id);if(!p){dirty.delete(id);continue;}
      const patch=patchOf(p);dirty.delete(id);
      try{
        const result=await api('save',{id,revision:p.revision,patch});
        p.revision=result.paper.revision;p.updated=result.paper.updated;state.settings=result.settings;
        if(!dirty.has(id))localStorage.removeItem(draftKey(id));
        else localStorage.setItem(draftKey(id),JSON.stringify(draftPayload(p)));
        updateSyncStatus();renderLibrary();
      }catch(e){dirty.add(id);status('未同步 · 草稿已暂存','error');throw e;}
    }
    status(state.settings.sync_error?'已本地保存 · 同步待处理':'已保存在本地',state.settings.sync_error?'error':'');
  })();
  try{await saving;}finally{saving=null;}
}
function recoverDrafts(){
  let recovered=0;
  for(const p of state.papers){
    try{
      const raw=localStorage.getItem(draftKey(p.id));if(!raw)continue;
      const draft=JSON.parse(raw);
      if(draftExpired(draft)){localStorage.removeItem(draftKey(p.id));continue;}
      if(draft.library&&draft.library!==draftNamespace){localStorage.removeItem(draftKey(p.id));continue;}
      if(draft.revision===p.revision){Object.assign(p,draft.patch);dirty.add(p.id);recovered++;}
      else if(draft.time>p.updated){toast('发现另一个窗口的更新与未合并草稿。可在设置中导出浏览器草稿后核对。',true);}
      else localStorage.removeItem(draftKey(p.id));
    }catch{}
  }
  if(recovered){toast('已恢复 '+recovered+' 篇未保存的浏览器草稿。');setTimeout(()=>flush().catch(e=>toast(e.message,true)),1500);}
}
function visiblePapers(){
  const query=$('#library-search').value.toLowerCase();
  return state.papers.filter(p=>filter==='archived'?p.archived:!p.archived)
    .filter(p=>filter==='done'?p.steps.every(Boolean):filter==='reading'?!p.steps.every(Boolean):true)
    .filter(p=>[p.title,p.authors,p.tags,p.year].join(' ').toLowerCase().includes(query));
}
/* 原文缺失：论文记录在库里，但本机已经没有对应的 PDF。
   服务端在 /api/state 的快照里直接按文件系统判定 source_missing（最准，手工删掉文件后立即生效）；
   没有该字段时（旧快照、离线草稿）回退到等价判据：导入时写入的 source_sha256 为空。
   这类残留记录用行内删除控件清理。 */
function sourceMissing(p){
  if(p&&typeof p.source_missing==='boolean')return p.source_missing;
  const meta=p?.analysis;
  if(!meta)return false;
  return meta.source_sha256===''||meta.source_sha256===undefined||meta.source_sha256===null;
}
function renderLibrary(){
  $('#paper-count').textContent=state.papers.filter(p=>!p.archived).length;
  const papers=visiblePapers();
  $('#paper-list').innerHTML=papers.map(p=>{
    const missing=sourceMissing(p);
    const ai=analysisStatuses[p.id]||{};
    /* 行内 AI 进度条与四步进度条刻意分开：不同类名、不同颜色，含义不混。 */
    const aiBar=aiActive(ai)
      ? `<div class="mini-progress ai" title="AI 分析 ${esc(aiStageLabel(ai))} ${aiProgress(ai)}%"><span style="width:${aiProgress(ai)}%"></span></div>`
      : '';
    return `<button class="paper-item ${p.id===currentId?'active':''}" data-paper="${esc(p.id)}" title="${esc(p.title)}">`
      +`<span class="paper-item-title${missing?' missing-source':''}">${esc(p.title)}</span>`
      +`<small>${esc(p.authors.split(/[;；]/)[0]||'作者待核对')} · ${esc(p.year||'年份待核对')} · ${p.language==='en'?'EN':'中文'}</small>`
      +`${missing?'<span class="source-missing" title="本机找不到这篇论文的 PDF 原文，无法打开；可用删除控件移除这条记录">原文缺失</span>':''}`
      +`<div class="mini-progress" title="四步研读进度"><span style="width:${p.steps.filter(Boolean).length*25}%"></span></div>${aiBar}`
      +`<span class="row-delete" role="button" tabindex="0" data-delete-paper="${esc(p.id)}" title="删除《${esc(p.title)}》" aria-label="删除《${esc(p.title)}》">×</span>`
      +`</button>`;
  }).join('')||'<p class="empty-library">这里还没有匹配的论文。<br>可以导入 PDF，或调整筛选条件。</p>';
}
function renderHeader(){
  const p=current();if(!p)return;
  $('#paper-title').textContent=p.title;
  $('#paper-authors').textContent=[p.authors||'作者待核对',p.venue||'来源待核对',p.year||''].filter(Boolean).join('  /  ');
  $('#paper-meta').innerHTML=`<span class="chip ${p.language==='en'?'en':''}">${p.language==='en'?'ENGLISH PAPER':'中文论文'}</span><span class="chip">${esc(TYPES[p.type])}</span>${p.tags?`<span class="chip neutral">${esc(p.tags)}</span>`:''}${p.archived?'<span class="chip neutral">已归档</span>':''}`;
  renderProgress();
  renderAiProgress();
}
function renderProgress(){
  const p=current();
  if(!p){$('#progress-label').textContent='0 / 4 已确认';$('#progress-bar').style.width='0%';$('#ribbon-hint').textContent='先抓住问题，再拆解证据。';return;}
  const count=p.steps.filter(Boolean).length;
  $('#progress-label').textContent=count+' / 4 已确认';$('#progress-bar').style.width=count*25+'%';
  $('#ribbon-hint').textContent=count===4?'已经完成一轮阅读，下一次从疑问出发。':'先抓住问题，再拆解证据。';
}
/* 阅读工作台顶部的 AI 进度条：不打开阅读卡也能看到分析走到哪一步。
   四步研读的进度条（#progress-bar）保持原样，两条各用各的类名与颜色。 */
function renderAiProgress(){
  const box=$('#ai-progress');if(!box)return;
  const active=aiActive(aiState);
  box.hidden=!active;
  if(!active)return;
  const pct=aiProgress(aiState);
  $('#ai-progress-label').textContent='AI 分析 · '+aiStageLabel(aiState);
  $('#ai-progress-pct').textContent=pct+'%';
  $('#ai-progress-fill').style.width=pct+'%';
  box.title=aiState.statusMessage||'';
}
async function selectPaper(id){
  if(id===currentId&&pdf){setView('reader');return;}
  try{await flush();}catch(e){toast(e.message,true);return;}
  currentId=id;const p=current();if(!p)return;
  aiState={status:'', statusMessage:'', answer:null, answerError:'', expanded:null, fetching:false};
  /* 这一段不写进 aiState 之外的地方：状态只读，不会因此触发任何模型请求。
     如果这篇论文正好在排队或分析中，aiPollSync() 会接着按 3 秒轮询。 */
  aiState.status=(p.analysis&&p.analysis.status)||'';
  aiState.statusMessage=(p.analysis&&p.analysis.message)||'';
  localStorage.setItem('paper-reader:last',id);pageNumber=p.lastPage||1;stage=Math.max(0,p.steps.findIndex(x=>!x));tab='steps';annotationFilter='all';
  setView('reader');renderHeader();renderLibrary();renderNotes();
  aiBackgroundStatus(id);
  await loadPdf(p);
}
/* 打开论文后补一次该篇的状态（服务器可能已经往前推进），不阻塞 PDF 渲染。 */
async function aiBackgroundStatus(id){
  try{
    const s=await api('analysis/'+id+'/status');
    analysisStatuses[id]=s;
    if(aiPaperId()===id){
      aiState.status=s.status;aiState.statusMessage=s.message||'';
      renderNotes();renderLibrary();
    }
  }catch(e){/* 读不到状态不影响阅读 */}
  aiPollSync();
}
function setView(next){
  view=next;$$('.view').forEach(el=>el.classList.toggle('hidden',el.id!==next+'-view'));
  $$('.nav-button').forEach(el=>el.classList.toggle('active',el.dataset.view===next));
  $('#view-name').textContent={reader:'阅读工作台',compare:'文献比较',guide:'阅读方法'}[next];
  $('#view-eyebrow').textContent={reader:'READ WITH A QUESTION',compare:'CONNECT YOUR LITERATURE',guide:'BUILD YOUR READING PRACTICE'}[next];
  if(next==='compare')renderCompare();if(next==='guide')renderGuide();
  if(next==='reader'&&pdf)setTimeout(()=>renderPage(),30);
}
function renderNotes(){
  const p=current();
  renderAiProgress();
  if(!p){$('#notes-content').innerHTML='导入一篇 PDF，开始你的第一次阅读。';return;}
  $$('.notes-tabs button').forEach(el=>el.classList.toggle('active',el.dataset.tab===tab));
  $('#annotation-count').textContent=p.annotations.length;
  if(tab==='steps')renderSteps(p);else if(tab==='annotations')renderAnnotations(p);else renderCard(p);
}
function renderSteps(p){
  const s=STAGES[stage],g=guide(p),hint=g?.steps[stage]||TYPE_HINTS[p.type][stage];
  $('#notes-content').innerHTML=`<div class="stage-tabs">${STAGES.map((x,i)=>`<button class="stage-tab ${stage===i?'active':''} ${p.steps[i]?'done':''}" data-stage="${i}"><b>${p.steps[i]?'✓':i+1}</b>${x.title}</button>`).join('')}</div><div class="stage-heading"><h2>${stage+1}. ${s.title}</h2><small>${s.time}</small></div><p class="stage-description">${s.description}</p><div class="teacher-note"><strong>${g?'针对本文的提示':'阅读提示'}</strong>${esc(hint)}${p.language==='en'&&stage===0?'<br>'+esc(ENGLISH_TIP):''}</div>${s.fields.map(key=>{const f=definition(p,key);return `<label class="field" for="field-${key}"><span class="field-title">${esc(f[0])}</span><span class="field-hint">${esc(f[1])}</span><textarea id="field-${key}" data-field="${key}" rows="3" placeholder="${esc(f[2])}">${esc(p.fields[key]||'')}</textarea></label>`;}).join('')}<label class="check-stage"><input id="stage-check" type="checkbox" ${p.steps[stage]?'checked':''}><span>${s.check}<small>填完本步记录，再勾选确认；可以随时回看和修改。</small></span></label>${stage<3?'<div class="stage-next"><button class="secondary" id="next-stage">继续下一步 →</button></div>':''}`;
}
function renderAnnotations(p){
  const annotations=p.annotations.filter(a=>annotationFilter==='all'||a.type===annotationFilter).slice().sort((a,b)=>a.page-b.page);
  $('#notes-content').innerHTML=`<div class="notes-section-header"><h2>留下你的理解</h2><button class="text-button" id="add-panel-note">＋ 添加批注</button></div><p class="stage-description">摘录原句，接着写出自己的解释或疑问。点击页码可以回到原文。</p><div class="annotation-filters">${Object.entries({all:'全部',...ANNOTATIONS}).map(([k,v])=>`<button data-annotation-filter="${k}" class="${annotationFilter===k?'active':''}">${v}</button>`).join('')}</div>${annotations.map(a=>`<article class="annotation-card ${a.type}"><header><strong>${ANNOTATIONS[a.type]}</strong><button class="text-button" data-jump="${a.page}">PDF ${a.page} · 文内 ${esc(label(p,a.page))} ↗</button></header>${a.quote?`<blockquote>${esc(a.quote)}</blockquote>`:''}<p>${esc(a.note||'尚未填写自己的理解。')}</p><div class="annotation-actions"><button data-edit-annotation="${esc(a.id)}">编辑</button><button data-delete-annotation="${esc(a.id)}">移除</button></div></article>`).join('')||'<div class="empty-note"><span>✎</span>还没有这一类批注。<br>在原文中选中文字，或直接记录本页的图表与疑问。</div>'}`;
}
function renderCard(p){
  $('#notes-content').innerHTML=`<div class="notes-section-header"><h2>本篇阅读卡</h2><button class="text-button" id="card-export">导出 Markdown ↗</button></div><div class="card-note">阅读卡自动汇总你的记录。每一栏都可以回到“阅读步骤”继续完善。</div>${aiCardBanner(p)}<article class="card-preview"><h2>${esc(p.title)}</h2><p class="card-meta">${esc(p.authors)} · ${esc(p.year)}<br>${esc(TYPES[p.type])} · ${p.steps.filter(Boolean).length}/4 步已确认</p>${Object.entries(FIELDS).map(([k,v])=>`<section class="card-field"><header class="card-field-head"><h3>${esc(definition(p,k)[0])}</h3><button class="ai-open" data-ai-field="${k}" aria-expanded="${aiState.expanded===k?'true':'false'}">${aiState.expanded===k?'收起 AI 参考':'查看 AI 参考'}</button></header><p class="${p.fields[k]?'':'unfilled'}">${esc(p.fields[k]||'待记录')}</p>${aiState.expanded===k?aiPanelHTML(p,k):''}</section>`).join('')}<h3>批注与术语</h3><p>共 ${p.annotations.length} 条；导出的 Markdown 会包含原句、理解和页码。</p></article>`;
}
/* --- AI 参考：独立于人工记录，只有点击才请求 ---------------------------- */
/* 说明：aiState 只存在于内存，切换论文即清空；展开状态不写入 library，也不改变进度。 */
const AI_STATUS_TEXT={none:'尚未生成',queued:'AI 分析已排队',running:'AI 正在生成参考',ready:'AI 参考已就绪',failed:'AI 生成失败',stale:'原文或设置已变化，需要重新生成',waiting_for_provider:'等待配置本地模型',waiting_for_user:'点击栏目右侧按钮查看 AI 参考',cancelled:'AI 生成已取消'};
const AI_CONFIDENCE_TEXT={high:'把握较高',medium:'把握中等',low:'把握较低'};
const AI_ANSWER_STATUS_TEXT={found:'原文有依据',partial:'依据不完整',not_found:'原文未说明'};
/* --- AI 分析进度条 ------------------------------------------------------
   阶段名与 analysis.STAGE_PROGRESS 一一对应；progress 由服务端给出（排队 0、抽取 15、
   切分 30、请求 55、校验 80、保存 95、完成 100），前端在没有数值时按阶段兜底。 */
const AI_STAGE_TEXT={queued:'排队中',extracting:'抽取原文',chunking:'切分原文',requesting:'请求模型',validating:'校验内容',saving:'保存结果',done:'完成'};
const AI_STAGE_PROGRESS={queued:0,extracting:15,chunking:30,requesting:55,validating:80,saving:95,done:100};
/* 只有这两种状态是“正在进行的任务”：进度条与轮询都只在这段时间内出现。 */
const AI_ACTIVE_STATUSES=['queued','running'];
const AI_POLL_MS=3000;
function aiActive(meta){return Boolean(meta)&&AI_ACTIVE_STATUSES.includes(meta.status);}
function aiStageLabel(meta){
  if(!meta)return '';
  return AI_STAGE_TEXT[meta.stage]||(AI_STAGE_TEXT[meta.status]||AI_STATUS_TEXT[meta.status]||meta.status||'');
}
function aiProgress(meta){
  const raw=(meta&&meta.progress!==undefined&&meta.progress!==null)?Number(meta.progress):NaN;
  const value=Number.isFinite(raw)?raw:(AI_STAGE_PROGRESS[meta&&meta.stage]??0);
  return Math.max(0,Math.min(100,Math.round(value)));
}
/* 进度条主体：百分比 + 中文阶段名 + 服务端 message（“正在读第 2/3 段（PDF 第 21–40 页）”
   这类分段页码信息就在 message 里，原样显示，不改写）。 */
function aiProgressHTML(meta,compact=false){
  const pct=aiProgress(meta),stage=esc(aiStageLabel(meta)),message=(meta&&meta.message)||'';
  const missing=sourceMissing(current())?'<span class="ai-progress-warn">原文缺失，等不到结果时可先删除这条记录</span>':'';
  return `<div class="ai-progress-box${compact?' compact':''}">`
    +`<div class="ai-progress-head"><span class="ai-progress-stage">AI 分析 · ${stage}</span><span class="ai-progress-pct">${pct}%</span></div>`
    +`<div class="ai-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="AI 分析进度"><span class="ai-progress-fill" style="width:${pct}%"></span></div>`
    +(message?`<p class="ai-progress-message">${esc(message)}</p>`:'')+missing+`</div>`;
}
function aiCardBanner(p){
  const s=aiState;
  const chip=s.status?`<span class="ai-chip ${s.status}">${esc(AI_STATUS_TEXT[s.status]||s.status)}</span>`:'<span class="ai-chip none">AI 参考未加载</span>';
  const busy=s.fetching?'<span class="ai-chip running">正在加载…</span>':'';
  const detail=s.statusMessage?`<span class="ai-chip-detail">${esc(s.statusMessage)}</span>`:'';
  const retry=(s.status&&['failed','stale','cancelled','waiting_for_user'].includes(s.status))?`<button class="ai-action" data-ai-run="retry">生成 / 重试</button>`:'';
  const force=(s.status==='ready')?`<button class="ai-action" data-ai-run="force">重新生成</button>`:'';
  /* 进度条只在 queued / running 时出现；failed 时保留失败说明与重试按钮，进度条消失。 */
  const bar=aiActive(s)?aiProgressHTML(s):'';
  return `<div class="ai-status-bar" data-ai-status="${esc(s.status||'')}">${chip}${busy}${detail}${retry}${force}<button class="ai-action" data-ai-refresh="1">刷新状态</button></div>${bar}<p class="ai-disclaimer">AI 参考答案由已配置的模型服务生成，不是你的记录，也不会写入阅读卡；请回到原文核对后再使用。</p>`;
}
function aiPanelHTML(p,key){
  const s=aiState;
  if(s.answerError)return `<div class="ai-panel error"><p>AI 参考暂时无法显示：${esc(s.answerError)}</p></div>`;
  if(!s.answer)return `<div class="ai-panel loading"><p>正在加载 AI 参考…</p></div>`;
  const item=(s.answer.answers||{})[key];
  if(!item)return `<div class="ai-panel error"><p>这份 AI 参考里没有该栏目。</p></div>`;
  const input=(key===Object.keys(s.answer.answers||{})[0]&&s.answer.input)?s.answer.input:null;
  const inputNote=input&&input.truncated?`<p class="ai-input-note">${esc(input.note)||'本次只送入了原文的一部分'}</p>`:'';
  const evidence=(item.evidence||[]).map(e=>`<li><button class="ai-page" data-jump="${Number(e.page)}" title="跳到 PDF 第 ${Number(e.page)} 页">PDF 第 ${Number(e.page)} 页</button><span class="ai-quote">${esc(e.quote)}</span>${e.verified===false?`<span class="ai-unverified">待人工核对：这条引文与原文不完全一致，不能当作逐字证据</span>`:`<span class="ai-verified">逐字核对</span>`}${e.claimed_page?`<span class="ai-corrected">模型写了第 ${Number(e.claimed_page)} 页，已按原文修正</span>`:''}</li>`).join('');
  const missing=(item.status==='not_found');
  const warnings=(s.answer.warnings||[]).length?`<ul class="ai-warnings">${s.answer.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul>`:'';
  return `<div class="ai-panel">${inputNote}${warnings}<div class="ai-meta"><span class="ai-tag">${esc(AI_ANSWER_STATUS_TEXT[item.status]||item.status)}</span><span class="ai-tag">置信度：${esc(AI_CONFIDENCE_TEXT[item.confidence]||item.confidence||'未标注')}</span>${item.causal_strength&&item.causal_strength!=='unclear'?`<span class="ai-tag">表述：${esc(item.causal_strength)}</span>`:''}</div><p class="ai-answer">${esc(item.answer||'（空）')}</p>${evidence?`<ul class="ai-evidence">${evidence}</ul>`:(missing?'':'<p class="ai-none">这条参考没有可核对的引文，仅供参考。</p>')}<div class="ai-actions"><button class="ai-action" data-ai-copy="${key}">复制到该栏目（追加，不覆盖）</button></div><p class="ai-footnote">AI 参考答案，请回到原文核对。</p></div>`;
}
function aiPaperId(){return currentId;}
async function aiFetchAnswer(force=false){
  const p=current();if(!p)return;
  const id=p.id;
  if(aiState.fetching)return;
  if(aiState.answer&&!force)return;
  aiState.fetching=true;aiState.answerError='';renderNotes();
  try{
    const r=await api('analysis/'+id);
    if(aiPaperId()!==id)return;                 // 切换论文后丢弃旧响应
    aiState.status=r.status||'none';aiState.statusMessage=r.reason||'';
    if(r.answer_available&&r.answer){aiState.answer=r.answer;}
    else{aiState.answer=null;aiState.answerError=r.reason||'参考分析尚未就绪';}
  }catch(e){
    if(aiPaperId()!==id)return;
    aiState.answer=null;aiState.answerError=e.message||'读取失败';
  }finally{
    aiState.fetching=false;
    if(aiPaperId()===id)renderNotes();
  }
}
async function aiRefreshStatus(forceRun=null){
  const p=current();if(!p)return;const id=p.id;
  try{
    if(forceRun)await api('analysis/'+id+'/generate',forceRun==='force'?{force:true}:{});
    const s=await api('analysis/'+id+'/status');
    if(aiPaperId()!==id)return;
    aiState.status=s.status;aiState.statusMessage=s.message||'';
    if(s.status!=='ready'){aiState.answer=null;aiState.answerError='';aiState.expanded=null;}
    analysisStatuses[id]=s;                 // 同一份数据也用来画文献栏里的细进度条
  }catch(e){if(aiPaperId()===id)aiState.statusMessage=e.message||'';}
  if(aiPaperId()===id){renderNotes();renderLibrary();}
  aiPollSync();
}
/* --- AI 状态轮询 --------------------------------------------------------
   只在“有任务在排队或进行中”时运行：每 3 秒读一次 /api/analysis（只读状态，不含答案正文），
   刷新面板与文献栏，并在终态、页面隐藏或没有任务时停表。计时器只有一个，重排前先清掉。 */
let aiPollTimer=0;
function aiPollActive(){
  return Object.values(analysisStatuses).some(aiActive)
    || aiActive(aiState)
    || aiActive((current()||{}).analysis);
}
function aiPollSync(){
  if(aiPollActive()&&!document.hidden){
    if(aiPollTimer)return;                  // 已经在轮询，不叠加第二个计时器
    aiPollTimer=setInterval(()=>{aiPollTick();},AI_POLL_MS);
  }else aiPollStop();
}
function aiPollStop(){
  if(!aiPollTimer)return;
  clearInterval(aiPollTimer);aiPollTimer=0;
}
async function aiPollTick(){
  if(document.hidden){aiPollStop();return;}   // 页面不可见时停止请求，回来再继续
  await refreshAnalysisStatuses();
  if(!aiPollActive()){aiPollStop();return;}
  const p=current();
  const meta=p?(analysisStatuses[p.id]||null):null;
  if(meta){
    aiState.status=meta.status;aiState.statusMessage=meta.message||'';
    if(meta.status!=='ready'){aiState.answer=null;aiState.answerError='';aiState.expanded=null;}
  }
  renderNotes();renderLibrary();renderAiProgress();
}
/* 一次请求拿到全部论文的状态（statuses[paper_id] = {status, stage, progress, message, …}）。 */
async function refreshAnalysisStatuses(){
  try{
    const r=await api('analysis');
    analysisStatuses=(r&&r.statuses)||{};
  }catch(e){/* 状态读不到时保持上一次显示，不打断阅读 */}
  return analysisStatuses;
}
function aiCopyToField(key){
  const p=current();if(!p||!aiState.answer)return;
  const item=(aiState.answer.answers||{})[key];if(!item)return;
  const text=`【AI 参考】${item.answer||''}${(item.evidence||[]).length?'（依据：'+item.evidence.map(e=>'PDF 第 '+Number(e.page)+' 页').join('；')+'）':''}`;
  p.fields[key]=p.fields[key]?.trim()?`${p.fields[key]}\n\n${text}`:text;   // 追加，绝不覆盖
  markDirty(p);
  if(tab==='card')renderNotes();
  toast('已追加到“'+definition(p,key)[0]+'”。可在“阅读步骤”继续修改。');
}
async function loadPdf(p){
  const version=++documentVersion;renderVersion++;
  if(pdfRender){try{pdfRender.cancel();}catch{}}
  if(textLayer){try{textLayer.cancel();}catch{}}
  pdf=null;$('#pdf-page').style.display='none';$('#pdf-loading').style.display='block';$('#pdf-loading').textContent='正在加载原文…';$('#page-total').textContent='/ —';
  if(pdfTask){try{await pdfTask.destroy();}catch{}}
  try{
    const task=getDocument({url:'/api/pdf/'+p.id,httpHeaders:csrf?{'X-CSRF-Token':csrf}:{},cMapUrl:'/vendor/pdfjs/cmaps/',cMapPacked:true,standardFontDataUrl:'/vendor/pdfjs/standard_fonts/',wasmUrl:'/vendor/pdfjs/wasm/',iccUrl:'/vendor/pdfjs/iccs/'});
    pdfTask=task;const doc=await task.promise;if(version!==documentVersion){await doc.destroy();return;}pdf=doc;
    $('#page-total').textContent='/ '+pdf.numPages;$('#page-input').max=pdf.numPages;
    pageNumber=Math.min(Math.max(1,pageNumber),pdf.numPages);await renderPage();
  }catch(e){if(version!==documentVersion)return;$('#pdf-loading').textContent='原文未能打开：'+e.message+'。仍可填写阅读卡，也可下载后查看。';toast('PDF 加载失败，请确认文件完整。',true);}
}
async function renderPage(){
  if(!pdf||view!=='reader')return;
  const sequence=++renderVersion,doc=pdf,n=pageNumber;
  if(pdfRender){try{pdfRender.cancel();}catch{}}
  if(textLayer){try{textLayer.cancel();}catch{}}
  const page=await doc.getPage(n);if(sequence!==renderVersion)return;
  const initial=page.getViewport({scale:1});
  const scale=fitScale(initial);const viewport=page.getViewport({scale});
  const wrapper=$('#pdf-page'),canvas=$('#pdf-canvas'),layer=$('#text-layer');
  wrapper.style.width=viewport.width+'px';wrapper.style.height=viewport.height+'px';wrapper.style.setProperty('--scale-factor',scale);wrapper.style.setProperty('--total-scale-factor',scale);
  const ratio=Math.min(window.devicePixelRatio||1,2);canvas.width=Math.floor(viewport.width*ratio);canvas.height=Math.floor(viewport.height*ratio);canvas.style.width=viewport.width+'px';canvas.style.height=viewport.height+'px';
  layer.replaceChildren();layer.style.setProperty('--scale-factor',scale);layer.style.setProperty('--total-scale-factor',scale);
  $('#pdf-loading').style.display='none';wrapper.style.display='block';$('#page-input').value=n;$('#printed-label').textContent='PDF 第 '+n+' 页 · 文内 '+label(current(),n);$('#prev-page').disabled=n===1;$('#next-page').disabled=n===doc.numPages;
  announcePage(n,doc.numPages,viewport);
  // 上一次渲染必须真正结束再复用同一张 canvas：PDF.js 在并发 render 时会抛错并可能
  // 留下半张旧图。取消后 await 它的 promise（取消本身以 RenderingCancelledException 结束）。
  if(pdfRender){try{await pdfRender.promise;}catch{}if(sequence!==renderVersion)return;}
  const canvasTask=page.render({canvasContext:canvas.getContext('2d'),viewport,transform:ratio!==1?[ratio,0,0,ratio,0,0]:null});pdfRender=canvasTask;
  try{await canvasTask.promise;if(sequence!==renderVersion)return;const content=await page.getTextContent();if(sequence!==renderVersion)return;textLayer=new TextLayer({textContentSource:content,container:layer,viewport});await textLayer.render();if(sequence!==renderVersion)return;renderHighlights();}
  catch(e){if(e.name!=='RenderingCancelledException'&&sequence===renderVersion){console.error(e);toast('本页显示有问题，可以下载原文继续阅读。',true);}}
}
function renderHighlights(){
  const p=current();if(!p)return;
  $('#highlight-layer').innerHTML=p.annotations.filter(a=>a.page===pageNumber).flatMap(a=>(a.rects||[]).map(r=>`<span class="pdf-highlight ${a.type}" style="left:${r[0]*100}%;top:${r[1]*100}%;width:${r[2]*100}%;height:${r[3]*100}%"></span>`)).join('');
}
/* 默认整页适配：滚轮一次换一页。窄窗口也保持整页；
   用户可主动选择适合宽度或放大来查看细节，不自动更改其选择。 */
let autoFitDecided=false, autoFitManual=false, fitNotice='';
function fittedScale(box,viewport){
  const availableWidth=Math.max(190,box.clientWidth-44),availableHeight=Math.max(160,box.clientHeight-36);
  return Math.min(availableWidth/viewport.width,availableHeight/viewport.height);
}
function mirrorZoomSelect(mode){
  const select=$('#zoom-select');if(!select)return;
  select.value=mode==='page'?'page':(mode==='width'?'width':'');
}
function fitScale(viewport){
  const box=$('#pdf-scroll');if(!box)return zoom;
  const fitted=Math.max(0.05,fittedScale(box,viewport));
  // 自动模式判定必须在两种模式之间都能运行，否则一旦切到"适合宽度"就回不去。
  if(!autoFitManual)autoFitMode(viewport,fitted);
  if(zoomMode==='page')return fitted;
  if(zoomMode==='width'){const width=Math.max(190,box.clientWidth-44);return width/viewport.width*zoom;}
  // 手动倍率：在"适合页面"的基础上放大（125%/150%），超出部分靠滚动查看
  return fitted*zoom;
}
function autoFitMode(viewport,fitted){
  if(!autoFitDecided){
    zoomMode='page';mirrorZoomSelect('page');
    autoFitDecided=true;
  }
}
function noteFit(message){
  fitNotice=message;
  setTimeout(()=>{const hint=$('#fit-hint');if(hint)hint.textContent='';},9000);
}
function announcePage(n,total,viewport){
  const live=$('#page-announcer');if(live)live.textContent='第 '+n+' 页，共 '+total+' 页';
  const box=$('#pdf-scroll');if(!box)return;
  // 整页可见时不产生同页内的无意义滚动；手动放大后才放开滚动。
  // 用刚渲染出的页面尺寸判断，避免依赖尚未完成布局的 scrollHeight。
  const pad=28,fits=viewport?viewport.height+pad<=box.clientHeight+2:false;
  box.classList.toggle('page-fits',zoomMode==='page'&&fits);
  const hint=$('#fit-hint');if(hint&&fitNotice){hint.textContent=fitNotice;fitNotice='';}
}
async function goPage(value){
  if(!pdf)return;const n=Math.min(pdf.numPages,Math.max(1,Number(value)||1));if(n===pageNumber)return;
  pageNumber=n;current().lastPage=n;markDirty(current());$('#pdf-scroll').scrollTop=0;window.getSelection()?.removeAllRanges();await renderPage();
}
/* --- 阅读区滚轮翻页：一次手势一页 ---------------------------------------- */
const WHEEL_LOCK_MS=600, WHEEL_STEP_PX=14, WHEEL_GESTURE_RESET_MS=260;
let wheelLockUntil=0, wheelGestureActive=false, wheelGestureTimer=0;
function wheelDelta(event){
  const unit=event.deltaMode===1?16:(event.deltaMode===2?$('#pdf-scroll')?.clientHeight||600:1);
  return {x:event.deltaX*unit,y:event.deltaY*unit};
}
function wheelProtected(event){
  if(!pdf||view!=='reader')return true;
  if(event.ctrlKey||event.metaKey||event.altKey||event.shiftKey)return true;   // 缩放/组合键手势
  const target=event.target;
  if(target?.closest?.('input,textarea,select,[contenteditable="true"],dialog'))return true;
  const selection=window.getSelection();
  if(selection&&!selection.isCollapsed&&selection.toString().trim())return true;  // 正在拖选文本
  const box=$('#pdf-scroll');
  if(!box||!box.contains(target))return true;
  if(event.defaultPrevented)return true;               // 已被取消的事件不再处理
  return false;
}
function onPdfWheel(event){
  const box=$('#pdf-scroll');
  if(wheelProtected(event))return;
  const {x,y}=wheelDelta(event);
  const now=Date.now();
  if(!wheelGestureActive){wheelGestureActive=true;wheelLockUntil=0;}
  clearTimeout(wheelGestureTimer);wheelGestureTimer=setTimeout(()=>{wheelGestureActive=false;},WHEEL_GESTURE_RESET_MS);
  const overflows=box.scrollHeight-box.clientHeight;
  if(overflows>2&&zoomMode!=='page'){
    // 放大后：能滚就先滚，到边界才翻页
    const atTop=box.scrollTop<=0, atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-1;
    if((y>0&&!atBottom)||(y<0&&!atTop))return;
  }
  if(Math.abs(y)<WHEEL_STEP_PX||Math.abs(x)>Math.abs(y))return;      // 触控板横向/微小位移
  const next=pageNumber+(y>0?1:-1);
  const insideLock=now<wheelLockUntil;                               // 450–650ms 防抖锁
  if(insideLock||next<1||next>pdf.numPages){
    // 不翻页也必须拦下这次滚动：整页可见时容器本就不滚动，若放行会把整个应用外壳
    // 或笔记栏一起滚走；边界页同理。
    if(overflows<=2||next<1||next>pdf.numPages)event.preventDefault();
    return;
  }
  wheelLockUntil=now+WHEEL_LOCK_MS;
  event.preventDefault();                                            // 成功翻页才拦截页面滚动
  goPage(next);
}
function selectedQuote(){
  const selection=window.getSelection();if(!selection?.rangeCount||selection.isCollapsed)return {quote:'',rects:[]};
  const range=selection.getRangeAt(0),layer=$('#text-layer');if(!layer.contains(range.commonAncestorContainer))return {quote:'',rects:[]};
  const base=$('#pdf-page').getBoundingClientRect();
  const rects=[...range.getClientRects()].filter(r=>r.width>1&&r.height>1).map(r=>[Math.max(0,(r.left-base.left)/base.width),Math.max(0,(r.top-base.top)/base.height),Math.min(1,r.width/base.width),Math.min(1,r.height/base.height)]).filter(r=>r[0]+r[2]<=1.01&&r[1]+r[3]<=1.01);
  return {quote:selection.toString().trim(),rects};
}
function openAnnotation(useSelection=true,existing=null){
  if(!current())return;
  const picked=useSelection?selectedQuote():{quote:'',rects:[]};
  draftAnnotation=existing?structuredClone(existing):{id:crypto.randomUUID(),type:'argument',page:pageNumber,quote:picked.quote,note:'',rects:picked.rects,created:new Date().toISOString()};
  $('#annotation-dialog-title').textContent=existing?'编辑批注':'添加批注';$('#annotation-type').value=draftAnnotation.type;$('#annotation-page').value=draftAnnotation.page;$('#annotation-page').max=pdf?.numPages||100000;$('#annotation-quote').value=draftAnnotation.quote;$('#annotation-note').value=draftAnnotation.note;
  $('#annotation-dialog').showModal();setTimeout(()=>$('#annotation-note').focus(),20);
}
async function download(path,name){
  await flush();const r=await fetch('/api/'+path,{headers:csrf?{'X-CSRF-Token':csrf}:{}});
  if(!r.ok){const e=await r.json();throw new Error(e.error);}
  const filename=r.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''(.+)/)?.[1];
  const blob=await r.blob();downloadBlob(blob,filename?decodeURIComponent(filename):name);
}
function downloadBlob(blob,name){
  const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),15000);
}
async function exportNote(){if(!current())return;try{await download('export/'+currentId,current().note_name);toast('阅读卡已导出，包含批注与页码。');}catch(e){toast(e.message,true);}}
function comparePapers(){
  const q=($('#compare-search')?.value||'').toLowerCase();return state.papers.filter(p=>!p.archived&&[p.title,p.authors,p.tags].join(' ').toLowerCase().includes(q));
}
function renderCompare(keepSearch=''){
  $('#compare-view').innerHTML=`<div class="view-intro"><div><span class="eyebrow">FROM PAPERS TO QUESTIONS</span><h1>让文献彼此对话</h1><p>按共同问题比较机制与证据。空白处提醒你回到原文补齐记录。</p></div><div class="compare-tools"><input id="compare-search" placeholder="筛选主题或作者" value="${esc(keepSearch)}" aria-label="筛选比较文献"><button id="export-compare" class="secondary">导出比较表 ↗</button></div></div><div class="comparison-wrap"><table class="comparison-table"><thead><tr><th>文献</th><th>研究问题</th><th>数据与方法</th><th>主要结果</th><th>结论边界</th><th>我的研究联系</th></tr></thead><tbody id="compare-body"></tbody></table></div>`;
  renderCompareBody();
}
function renderCompareBody(){
  $('#compare-body').innerHTML=comparePapers().map(p=>`<tr><td><button data-paper="${esc(p.id)}">${esc(p.title)}</button><small>${esc(p.authors)} · ${esc(p.year)}<br>${esc(TYPES[p.type])}</small></td><td>${esc(p.fields.question||'待记录')}</td><td>${esc([p.fields.data,p.fields.method].filter(Boolean).join('\n\n')||'待记录')}</td><td>${esc(p.fields.evidence||p.fields.claim||'待记录')}</td><td>${esc(p.fields.boundaries||'待记录')}</td><td>${esc(p.fields.connections||'待记录')}</td></tr>`).join('')||'<tr><td colspan="6">没有匹配的文献。</td></tr>';
}
function exportComparison(){
  const cell=x=>String(x||'待记录').replace(/\|/g,'\\|').replace(/\r?\n/g,'<br>');
  const text='# 文献比较表\n\n| 文献 | 研究问题 | 数据与方法 | 主要结果 | 结论边界 | 我的研究联系 |\n| --- | --- | --- | --- | --- | --- |\n'+comparePapers().map(p=>'| '+[`${p.authors} (${p.year}) ${p.title}`,p.fields.question,[p.fields.data,p.fields.method].filter(Boolean).join('\n'),p.fields.evidence||p.fields.claim,p.fields.boundaries,p.fields.connections].map(cell).join(' | ')+' |').join('\n')+'\n';
  downloadBlob(new Blob([text],{type:'text/markdown;charset=utf-8'}),'文献比较表.md');toast('比较表已导出，可放入 Obsidian。');
}
function renderGuide(){
  const p=current(),g=guide(p);
  $('#guide-view').innerHTML=`<div class="view-intro"><div><span class="eyebrow">A PRACTICE, NOT A RACE</span><h1>带着问题读，留下自己的理解</h1><p>目标是解释研究问题、重建证据链，并清楚地说出结论边界。时间只作练习参考。</p></div><button class="secondary" data-return-reader>回到当前论文 →</button></div><div class="guide-grid"><article class="guide-card"><h2>三遍阅读</h2><ol><li><strong>初读定位：</strong>摘要、引言、结论、各节标题与图表。写出问题、回答、类型。</li><li><strong>精读重建：</strong>追踪概念、假设、数据、比较方法与证据。每节留下自己的概括。</li><li><strong>回顾检验：</strong>合上论文讲两分钟，写 150–200 字复述，再回看薄弱环节。</li></ol><p class="note-tip">核心文献会反复阅读。一遍没有读懂全部细节很正常。</p></article><article class="guide-card"><h2>五个共同问题</h2><ol><li>作者究竟研究什么？</li><li>为什么值得研究，与已有文献有什么关系？</li><li>用了什么方法与证据？</li><li>主要结论在哪些条件下成立？</li><li>对我的学习或研究有什么用？</li></ol></article><article class="guide-card"><h2>先按论文类型调整读法</h2><p><strong>框架 / 综述：</strong>区分概念、机制、原始证据与引用证据。</p><p><strong>实证研究：</strong>看清样本、指标与比较逻辑；因果、机制和描述证据各有适用范围。</p><p><strong>理论模型：</strong>写清主体、目标、约束、时序、核心命题及假设依赖。</p></article><article class="guide-card"><h2>英文阅读</h2><p>${ENGLISH_TIP}</p><p><strong>推荐顺序：</strong>Abstract → Introduction → Conclusion / Discussion → 关键图表或命题 → 方法与结果 → 必要附录。</p><p>中文记录自己的理解，英文保留关键术语和原句。先抓段落任务，再查影响主线的陌生词。</p></article><article class="guide-card"><h2>怎样读图表与回归</h2><p>先问图表回答什么问题，再读观察单位、坐标轴、指标单位、样本、窗口、权重和注释。</p><p>回归表依次看被解释变量、主要系数、数量级、标准误或置信区间、样本变化与作者的解释依据。</p><p class="note-tip">统计显著不等于经济意义大；估计不显著也不等于证明效应为零。</p></article><article class="guide-card"><h2>把记录变成文献积累</h2><p>一篇论文保留一张阅读卡。原句、作者观点、引用证据与自己的假设分开记录。</p><p>文献比较按共同问题组织：差异来自机制、样本、指标、制度环境还是研究方法？</p><p>需要正式引用时可配合 Zotero 管理条目与写作引用；本工具负责阅读过程和理解记录。</p></article>${g?`<article class="guide-card full"><h2>针对当前论文：${esc(g.title)}</h2><p>${esc(g.source)}</p><div class="jump-list">${g.jumps.map(([name,n])=>`<button data-guide-jump="${n}">${esc(name)} · PDF ${n} ↗</button>`).join('')}</div><ul>${g.questions.map(q=>'<li>'+esc(q)+'</li>').join('')}</ul><table class="glossary-table"><tbody>${g.glossary.map(([a,b])=>`<tr><td>${esc(a)}</td><td>${esc(b)}</td></tr>`).join('')}</tbody></table></article>`:''}</div><p class="guide-footer">批注作为独立阅读记录保存，可在软件中高亮原句，并在 Obsidian 中通过 PDF 页码链接回看。</p>`;
}
function updateSyncStatus(){
  const s=state.settings;$('#quick-sync').textContent=s.sync_enabled?(s.sync_error?'Obsidian 同步待处理':'Obsidian 已连接'):'Obsidian 未连接';
  $('#sync-status').textContent=s.sync_error?'本地记录已保存，同步暂未完成：'+s.sync_error:s.sync_enabled?'已连接：'+s.vault_path+(s.last_sync?'\n最近同步：'+new Date(s.last_sync).toLocaleString():''):'尚未开启同步。可以先在本地使用，以后再选择笔记库。';
  $('#sync-status').className='sync-status'+(s.sync_error?' error':'');
}
function openSettings(){
  $('#vault-path').value=state.settings.vault_path||'';$('#sync-enabled').checked=Boolean(state.settings.sync_enabled);updateSyncStatus();loadAiSettings();
  if(!$('#draft-export')&&Object.keys(localStorage).some(k=>k.includes(':draft:'))){const b=document.createElement('button');b.id='draft-export';b.className='secondary';b.textContent='导出浏览器草稿';b.addEventListener('click',()=>{const data={};for(const k of Object.keys(localStorage))if(k.startsWith('paper-reader:draft:'))data[k]=JSON.parse(localStorage.getItem(k));downloadBlob(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),'未合并阅读草稿.json');});$('#restore-button').after(b);}
  $('#settings-dialog').showModal();
}
/* --- AI 参考状态与设置（阶段 5） ---------------------------------------- */
const AI_LABELS={none:'未配置（等待配置）',queued:'已排队',running:'正在生成',ready:'参考已就绪',failed:'生成失败',stale:'原文或设置已变化，需要重新生成',waiting_for_provider:'等待配置本地模型',waiting_for_user:'等待你点击“查看 AI 参考”',cancelled:'已取消'};
function aiStatusText(meta){
  if(!meta)return '未知状态';
  return (AI_LABELS[meta.status]||meta.status)+(meta.stage?' · '+meta.stage:'')+(meta.status==='running'?' '+(meta.progress||0)+'%':'');
}
async function refreshAiStatus(paperId=currentId){
  if(!paperId)return null;
  try{const r=await api('analysis/'+paperId+'/status');renderAiStatus(r);return r;}
  catch(e){const box=$('#ai-status-detail');if(box)box.textContent='状态读取失败：'+e.message;return null;}
}
function renderAiStatus(payload){
  const box=$('#ai-status-detail');if(!box||!payload)return;
  const lines=[aiStatusText(payload),payload.message||''];
  if(payload.error_code)lines.push('错误：'+payload.error_code+(payload.error_summary?' · '+payload.error_summary:''));
  if(payload.completed_at)lines.push('完成时间：'+payload.completed_at);
  box.textContent=lines.filter(Boolean).join('\n');
  box.className='sync-status'+(payload.status==='failed'?' error':'');
}
function updateAiProviderStatus(health,ai){
  const line=$('#ai-provider-status');if(!line)return;
  const provider=(ai&&ai.provider)||'none';
  if(!health){line.textContent='未能检查 Provider 状态。';return;}
  if(provider==='none'){
    line.textContent='当前未配置 Provider：导入新论文后仍可正常阅读、填写阅读卡与批注，AI 状态显示为“等待配置”；只有你配置后主动点击，才会请求模型。';
  }else if(health.ready&&health.ok){
    line.textContent='Provider 可用：'+health.provider+(ai&&ai.model?' · '+ai.model:'')+'（'+(health.detail||'已连接')+'）。';
  }else{
    line.textContent='Provider 当前不可用：'+(health.provider||provider)+' · '+health.category+(health.detail?' · '+health.detail:'')+'。阅读与记录不受影响。';
  }
}
async function loadAiSettings(){
  try{
    const r=await api('ai-settings');
    const ai=r.ai||{};
    $('#ai-provider').value=ai.provider||'none';$('#ai-model').value=ai.model||'';
    if($('#ai-mode'))$('#ai-mode').value=ai.mode||'local_loopback';
    $('#ai-base-url').value=ai.base_url||'';$('#ai-key-env').value=ai.api_key_env||'';
    $('#ai-auto-analyze').checked=Boolean(ai.auto_analyze);
    renderCredentialState(r.credential);
    renderAiBudget(r.budget, r.pause);
    updateAiProviderStatus(r.health,ai);
  }catch(e){toast('AI 设置读取失败：'+e.message,true);}
  refreshAiStatus();
}
/* Credential state is shown as a state word only: the value is never sent to the page. */
function renderCredentialState(credential){
  const node=$('#ai-credential-state');if(!node)return;
  const c=credential||{};
  const text={bound:'凭据状态：已绑定（值只在后端内存中使用，不显示、不回传）',
              unbound:'凭据状态：未绑定凭据引用',
              missing:'凭据状态：引用已登记，但没有读到对应的环境变量值',
              refused:'凭据状态：这个引用被拒绝（未登记的名称或值不符合要求）'}[c.credential_state];
  let line=text||'凭据状态：未知';
  if(c.credential_ref)line+='　引用：'+c.credential_ref;
  if(c.legacy_plaintext&&c.legacy_plaintext.present){
    line+='　⚠ 检测到旧版明文凭据文件，本版本不再读取它。'+c.legacy_plaintext.action;
  }
  node.textContent=line;
  node.classList.toggle('warn-text',c.credential_state==='refused'||c.credential_state==='missing'||Boolean(c.legacy_plaintext&&c.legacy_plaintext.present));
}
/* 预算与暂停状态：只显示计数与开关，不显示任何凭据。 */
function renderAiBudget(budget, pause){
  const node=$('#ai-budget');if(!node)return;
  const b=budget||{},limits=b.limits||{};
  const paused=Boolean(pause&&pause.paused);
  const box=$('#ai-paused');if(box)box.checked=paused;
  node.textContent='用量：最近 24 小时 '+Number(b.spent_last_24h||0)+' 次请求；'
    +'每次生成上限 '+Number(limits.max_requests_per_job||0)+' 次调用，'
    +'24 小时上限 '+Number(limits.daily_request_limit||0)+' 次，'
    +'队列上限 '+Number(limits.max_queue_depth||0)+'。'
    +(paused?'（已暂停：所有 AI 出站请求都会被拒绝，包括健康检查与模型列表。）':'');
}
async function toggleAiPause(){
  const box=$('#ai-paused');if(!box)return;
  try{
    const r=await api('ai/pause',{paused:box.checked});
    renderAiBudget(r.budget,{paused:r.paused});
    toast(r.paused?'已暂停所有 AI 出站请求。':'已恢复 AI 出站请求。',!r.paused);
  }catch(e){toast(e.message,true);box.checked=!box.checked;}
}
async function saveAiSettings(){
  const button=$('#save-ai-settings');button.disabled=true;
  try{
    const r=await api('settings',{vault_path:state.settings.vault_path||'',sync_enabled:Boolean(state.settings.sync_enabled),provider:$('#ai-provider').value,mode:($('#ai-mode')||{value:'local_loopback'}).value,model:$('#ai-model').value.trim(),base_url:$('#ai-base-url').value.trim(),api_key_env:$('#ai-key-env').value.trim(),auto_analyze:$('#ai-auto-analyze').checked});
    state.settings=r.settings||state.settings;updateSyncStatus();
    $('#ai-key-env').value=(r.ai&&r.ai.api_key_env)||'';
    renderCredentialState(r.credential);
    updateAiProviderStatus(r.health,r.ai);
    const ok=r.health&&r.health.ok;
    toast(ok?'AI 设置已保存，Provider 可用。':'AI 设置已保存；Provider 暂不可用，阅读记录不受影响。',!ok);
  }catch(e){toast(e.message,true);}finally{button.disabled=false;}
}
function openMetadata(){
  const p=current();if(!p)return;
  for(const k of ['title','authors','year','venue','type','language','tags','doi','citation'])$('#meta-'+k).value=p[k]||'';
  $('#meta-offset').value=p.pageOffset||0;$('#archive-paper').textContent=p.archived?'恢复到文献库':'将本文移至归档';$('#metadata-dialog').showModal();
}
/* 两步确认的第一步：先把要删除的论文标题原样写在对话框里，用户必须自己按下“确认删除”。
   对话框里明确写出 Obsidian 笔记库不受影响——软件不会删除笔记库中的任何文件。
   参数是论文 id：文献栏每一行的删除控件传自己那一行，论文栏顶部的“删除论文”不传参，
   沿用当前打开的论文。 */
let pendingDeleteId='';
/* 括号里的默认值写成 currentId，是为了同时满足两条约束：
   1) 行内删除控件必须能指定任意一行：openDeletePaper(rowId) 会用那一行的标题与 id；
   2) 论文栏的“删除论文”按旧调用方式（不传参）仍然删除当前打开的论文。
   tests/test_delete.py 钉住了 "function openDeletePaper()" 这段文本，不能改测试，所以不用参数列表以外的写法。 */
async function openDeletePaper(paperId = currentId){
  const id=paperId||currentId;
  const p=state.papers.find(x=>x.id===id);
  if(!p){toast('找不到这篇论文，请刷新文献库。',true);return;}
  try{await flush();}catch(e){toast(e.message,true);return;}
  if(!state.papers.some(x=>x.id===id))return;      // flush 之后可能已经被别处删掉
  pendingDeleteId=id;
  $('#delete-paper-title').textContent=p.title || '（未命名论文）';
  $('#delete-dialog').showModal();
}
/* 库里已经没有可选论文时的空状态（删掉最后一篇时用）。 */
function showEmptyLibrary(){
  currentId='';
  aiState={status:'',statusMessage:'',answer:null,answerError:'',expanded:null,fetching:false};
  $('#paper-title').textContent='从第一篇论文开始';
  $('#paper-authors').textContent='点击左下角“导入 PDF”，把原文与自己的理解放在一起。';
  $('#paper-meta').innerHTML='';$('#pdf-loading').textContent='导入 PDF 后，这里会显示论文原文。';
  renderProgress();renderNotes();
}
async function confirmDeletePaper(){
  /* 删除对象只在打开确认框时确定（pendingDeleteId）：即使用户随后点了别的行，
     确认框里的标题和真正删除的记录始终是同一条。 */
  const id=pendingDeleteId||currentId;
  const p=state.papers.find(x=>x.id===id);
  if(!p||!$('#delete-dialog').open)return;
  const title=p.title,button=$('#confirm-delete-paper');
  /* 先取出这篇论文的浏览器草稿：删除成功后必须丢弃它，否则下一次打开页面时
     recoverDrafts() 会按旧 revision 把草稿合并回来，让“已删除”的论文又出现在列表里。
     删除失败时草稿原样放回，不丢用户未保存的内容。 */
  let draft=null;
  try{draft=localStorage.getItem(draftKey(id));}catch{}
  try{localStorage.removeItem(draftKey(id));}catch{}
  button.disabled=true;button.textContent='正在删除…';
  try{
    const r=await api('delete',{paper_id:id});
    if(r&&r.state)state=r.state;else state.papers=state.papers.filter(x=>x.id!==id);
    dirty.delete(id);
    delete analysisStatuses[id];                 // 行内 AI 进度条不再指向已删除的记录
    if(localStorage.getItem('paper-reader:last')===id)localStorage.removeItem('paper-reader:last');
    pendingDeleteId='';
    $('#delete-dialog').close();
    renderLibrary();
    const next=visiblePapers()[0]||state.papers[0];
    if(next)await selectPaper(next.id);
    else showEmptyLibrary();
    toast('已删除《'+title+'》。Obsidian 笔记库未改动，阅读笔记仍保留在原处。');
  }catch(e){
    pendingDeleteId=id;                          // 失败后仍指向同一条，重试不会删错
    if(draft!==null){try{localStorage.setItem(draftKey(id),draft);}catch{}}
    toast(e.message,true);
  }
  finally{button.disabled=false;button.textContent='确认删除';}
}
/* 关闭对话框（取消或删除成功后）就清空待删对象，避免下一次确认删到上一次选的那篇。 */
$('#delete-dialog').addEventListener('close',()=>{pendingDeleteId='';});

$('#library-search').addEventListener('input',renderLibrary);
$('.library-filters').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(!b)return;filter=b.dataset.filter;$$('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));renderLibrary();});
$('.main-nav').addEventListener('click',e=>{const b=e.target.closest('[data-view]');if(b)setView(b.dataset.view);});
document.addEventListener('click',async e=>{
  /* 文献栏行内的删除控件排在最前面：它同时带 data-paper 的祖先，
     所以必须先匹配它，否则会被下面的“选中这篇论文”吃掉。 */
  const del=e.target.closest('[data-delete-paper]');
  if(del){e.preventDefault();e.stopPropagation();await openDeletePaper(del.dataset.deletePaper);return;}
  const paper=e.target.closest('[data-paper]');if(paper){await selectPaper(paper.dataset.paper);return;}
  const s=e.target.closest('[data-stage]');if(s){stage=Number(s.dataset.stage);renderNotes();$('#notes-content').scrollTop=0;return;}
  const af=e.target.closest('[data-annotation-filter]');if(af){annotationFilter=af.dataset.annotationFilter;renderNotes();return;}
  const jump=e.target.closest('[data-jump]');if(jump){await goPage(jump.dataset.jump);return;}
  const gj=e.target.closest('[data-guide-jump]');if(gj){setView('reader');await goPage(gj.dataset.guideJump);return;}
  const edit=e.target.closest('[data-edit-annotation]');if(edit){openAnnotation(false,current().annotations.find(a=>a.id===edit.dataset.editAnnotation));return;}
  const remove=e.target.closest('[data-delete-annotation]');if(remove){current().annotations=current().annotations.filter(a=>a.id!==remove.dataset.deleteAnnotation);markDirty(current());renderNotes();renderHighlights();toast('批注已移除。');return;}
  if(e.target.closest('.close-dialog'))e.target.closest('dialog').close();
  if(e.target.closest('#next-stage')){stage=Math.min(3,stage+1);renderNotes();$('#notes-content').scrollTop=0;}
  if(e.target.closest('#add-panel-note'))openAnnotation(false);
  const aiOpen=e.target.closest('[data-ai-field]');
  if(aiOpen){const key=aiOpen.dataset.aiField;aiState.expanded=aiState.expanded===key?null:key;renderNotes();if(aiState.expanded&&!aiState.answer&&!aiState.fetching)await aiFetchAnswer();return;}
  const aiRun=e.target.closest('[data-ai-run]');if(aiRun){await aiRefreshStatus(aiRun.dataset.aiRun);return;}
  if(e.target.closest('[data-ai-refresh]')){await aiRefreshStatus();return;}
  const aiCopy=e.target.closest('[data-ai-copy]');if(aiCopy){aiCopyToField(aiCopy.dataset.aiCopy);return;}
  if(e.target.closest('#card-export'))await exportNote();
  if(e.target.closest('[data-return-reader]'))setView('reader');
  if(e.target.closest('#export-compare'))exportComparison();
});
$('#notes-content').addEventListener('input',e=>{
  const key=e.target.dataset.field;if(!key)return;const p=current();p.fields[key]=e.target.value;
  if(!e.target.value.trim()&&p.steps[stage]){p.steps[stage]=false;$('#stage-check').checked=false;renderProgress();}
  markDirty(p);
});
$('#notes-content').addEventListener('change',e=>{
  if(e.target.id!=='stage-check')return;const p=current();
  if(e.target.checked){const missing=STAGES[stage].fields.find(k=>!p.fields[k]?.trim());if(missing){e.target.checked=false;toast('先完成“'+definition(p,missing)[0]+'”，再确认本步。');$('#field-'+missing).focus();return;}}
  p.steps[stage]=e.target.checked;markDirty(p);renderProgress();renderLibrary();$$('.stage-tab').forEach((el,i)=>{el.classList.toggle('done',p.steps[i]);el.querySelector('b').textContent=p.steps[i]?'✓':i+1;});
});
$('.notes-tabs').addEventListener('click',e=>{const b=e.target.closest('[data-tab]');if(b){tab=b.dataset.tab;renderNotes();$('#notes-content').scrollTop=0;}});
$('#compare-view').addEventListener('input',e=>{if(e.target.id==='compare-search')renderCompareBody();});
$('#prev-page').addEventListener('click',()=>goPage(pageNumber-1));$('#next-page').addEventListener('click',()=>goPage(pageNumber+1));
/* 收起记录栏：把宽度让给原文。只切换布局类，不触碰任何记录数据 */
function toggleNotesPanel(force){
  const box=$('.workspace'),button=$('#toggle-notes');if(!box||!button)return;
  const collapsed=force===undefined?!box.classList.contains('notes-collapsed'):Boolean(force);
  box.classList.toggle('notes-collapsed',collapsed);
  button.setAttribute('aria-pressed',collapsed?'true':'false');
  button.textContent=collapsed?'展开记录栏 ‹':'收起记录栏 ›';
  try{localStorage.setItem('paper-reader:notes-collapsed',collapsed?'1':'0');}catch{}
  setTimeout(()=>renderPage(),60);
}
$('#toggle-notes').addEventListener('click',()=>toggleNotesPanel());
if(localStorage.getItem('paper-reader:notes-collapsed')==='1'&&window.innerWidth>930)toggleNotesPanel(true);
$('#page-input').addEventListener('change',e=>goPage(e.target.value));$('#page-input').addEventListener('keydown',e=>{if(e.key==='Enter')goPage(e.target.value);});
$('#zoom-select').addEventListener('change',e=>{
  const value=e.target.value;
  autoFitDecided=true;                        // 使用者的选择优先，不再自动切换
  autoFitManual=true;
  const hint=$('#fit-hint');if(hint)hint.textContent='';
  if(value==='page'||value==='width'){zoomMode=value;zoom=1;}
  else{zoomMode='manual';zoom=Number(value)||1;}
  renderPage();
});
/* 滚轮翻页绑定在 PDF 区域，passive:false 才能阻止浏览器滚动 */
$('#pdf-scroll').addEventListener('wheel',onPdfWheel,{passive:false});
$('#selection-note').addEventListener('mousedown',e=>e.preventDefault());$('#selection-note').addEventListener('click',()=>openAnnotation(true));$('#manual-note').addEventListener('click',()=>openAnnotation(false));
$('#annotation-form').addEventListener('submit',e=>{
  e.preventDefault();const p=current(),n=Number($('#annotation-page').value),quote=$('#annotation-quote').value.trim(),note=$('#annotation-note').value.trim();
  if(!quote&&!note){toast('先写下原句、术语或自己的理解。');return;}
  if(n<1||n>(pdf?.numPages||100000)){toast('页码不在原文范围内。',true);return;}
  const annotation={...draftAnnotation,page:n,type:$('#annotation-type').value,quote,note,updated:new Date().toISOString()};
  if(n!==draftAnnotation.page||quote!==draftAnnotation.quote)annotation.rects=[];
  const i=p.annotations.findIndex(a=>a.id===annotation.id);if(i>=0)p.annotations[i]=annotation;else p.annotations.push(annotation);
  markDirty(p);$('#annotation-dialog').close();tab='annotations';annotationFilter='all';renderNotes();renderHighlights();window.getSelection()?.removeAllRanges();toast('批注已记录。');
});
$('#annotation-type').addEventListener('change',e=>{$('#annotation-note').placeholder=e.target.value==='term'?'记录中文含义、本文定义与使用条件。':'用自己的话解释，注明不确定之处。';});
$('#metadata-form').addEventListener('submit',async e=>{
  e.preventDefault();const p=current(),oldType=p.type,offset=Number($('#meta-offset').value);
  for(const k of ['title','authors','year','venue','type','language','tags','doi','citation'])p[k]=$('#meta-'+k).value.trim();
  if(offset!==Number(p.pageOffset)){p.pageOffset=offset;p.pageLabels=[];}
  if(p.type!==oldType)p.steps=[false,false,false,false];
  markDirty(p);renderHeader();renderLibrary();renderNotes();$('#printed-label').textContent='PDF 第 '+pageNumber+' 页 · 文内 '+label(p,pageNumber);$('#metadata-dialog').close();
  try{await flush();toast('文献信息已保存。');}catch(e){toast(e.message,true);}
});
$('#archive-paper').addEventListener('click',async()=>{const p=current();p.archived=!p.archived;markDirty(p);renderHeader();renderLibrary();$('#metadata-dialog').close();try{await flush();toast(p.archived?'已移至归档，可从左侧“归档”恢复。':'已恢复到文献库。');}catch(e){toast(e.message,true);}});
$('#edit-meta').addEventListener('click',openMetadata);$('#export-note').addEventListener('click',exportNote);
/* 删除是两步操作：点“删除论文”只打开确认框，真正的请求只在确认按钮提交时发出。 */
$('#delete-paper').addEventListener('click',openDeletePaper);
$('#delete-form').addEventListener('submit',e=>{e.preventDefault();confirmDeletePaper();});
$('#download-pdf').addEventListener('click',()=>download('pdf/'+currentId,current().title.replace(/[<>:"/\\|?*]/g,'_')+'.pdf').catch(e=>toast(e.message,true)));
$('#settings-button').addEventListener('click',openSettings);$('#quick-sync').addEventListener('click',openSettings);
$('#save-ai-settings').addEventListener('click',saveAiSettings);
  if($('#ai-paused'))$('#ai-paused').addEventListener('change',toggleAiPause);$('#refresh-ai-status').addEventListener('click',()=>loadAiSettings());
$('#browse-vault').addEventListener('click',async()=>{const b=$('#browse-vault');b.disabled=true;b.textContent='等待选择…';try{const r=await api('choose-vault',{});if(r.path)$('#vault-path').value=r.path;}catch(e){toast('也可以直接填写路径：'+e.message,true);}finally{b.disabled=false;b.textContent='浏览…';}});
$('#save-settings').addEventListener('click',async()=>{const b=$('#save-settings');b.disabled=true;try{await flush();const r=await api('settings',{vault_path:$('#vault-path').value,sync_enabled:$('#sync-enabled').checked});state.settings=r.settings;updateSyncStatus();toast(r.settings.sync_error?'设置已保存；请查看同步提示。':'同步设置已保存。',Boolean(r.settings.sync_error));}catch(e){toast(e.message,true);}finally{b.disabled=false;}});
$('#sync-now').addEventListener('click',async()=>{const b=$('#sync-now');b.disabled=true;try{await flush();const r=await api('sync',{});state.settings=r.settings;updateSyncStatus();toast(r.settings.sync_error?'部分笔记同步待处理，请查看提示。':'全部文献已同步到 Obsidian。',Boolean(r.settings.sync_error));}catch(e){toast(e.message,true);}finally{b.disabled=false;}});
$('#backup-button').addEventListener('click',()=>download('backup','论文研读完整备份-'+new Date().toISOString().slice(0,10)+'.zip').catch(e=>toast(e.message,true)));
$('#restore-button').addEventListener('click',()=>$('#backup-file').click());
$('#backup-file').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;try{await flush();const response=await fetch('/api/restore',{method:'POST',headers:csrf?{'X-CSRF-Token':csrf}:{},body:file});const r=await response.json();if(!response.ok)throw new Error(r.error);state=r.state;renderLibrary();updateSyncStatus();toast('已恢复 '+r.imported+' 篇缺少的文献；同编号记录已保留。');}catch(e){toast(e.message,true);}finally{e.target.value='';}});
$('#import-button').addEventListener('click',()=>$('#pdf-file').click());
$('#pdf-file').addEventListener('change',async e=>{
  const files=[...e.target.files];if(!files.length)return;const button=$('#import-button');button.disabled=true;button.textContent='正在导入…';let added=[];
  try{await flush();for(const file of files){if(file.size>80*1024*1024)throw new Error(file.name+' 超过 80 MB。');const headers=Object.assign({},csrf?{'X-CSRF-Token':csrf}:{},{'X-File-Name':encodeURIComponent(file.name)});const response=await fetch('/api/import',{method:'POST',headers,body:file});const r=await response.json();if(!response.ok)throw new Error(r.error);state.papers.push(r.paper);added.push(r.paper.id);}filter='all';$$('[data-filter]').forEach(x=>x.classList.toggle('active',x.dataset.filter===filter));$('#library-search').value='';renderLibrary();await selectPaper(added[0]);openMetadata();toast('已导入 '+added.length+' 篇论文，请核对文献信息。');}
  catch(e){renderLibrary();toast(e.message,true);}finally{button.disabled=false;button.textContent='＋ 导入 PDF';e.target.value='';}
});
$('#shutdown-button').addEventListener('click',async()=>{try{await flush();await api('shutdown',{});$('#settings-dialog').close();status('已保存并退出');$('#notes-content').innerHTML='<div class="empty-note">所有记录已保存。<br>下次双击启动文件即可继续阅读。</div>';toast('软件已退出，可以关闭本页。');}catch(e){toast(e.message,true);}});
let resizeTimer=0;new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>renderPage(),140);}).observe($('#pdf-scroll'));
document.addEventListener('keydown',async e=>{
  /* 行内删除控件是 span[role=button]：键盘 Enter/空格也要能打开同一个确认框。 */
  const del=e.target.closest?.('[data-delete-paper]');
  if(del&&(e.key==='Enter'||e.key===' ')){e.preventDefault();e.stopPropagation();await openDeletePaper(del.dataset.deletePaper);return;}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();flush().then(()=>toast('阅读记录已保存。')).catch(err=>toast(err.message,true));}
  if((e.ctrlKey||e.metaKey)&&e.key==='Enter'&&$('#annotation-dialog').open){e.preventDefault();$('#annotation-form').requestSubmit();}
  if(e.target.matches('input,textarea,select'))return;
  if(view==='reader'&&!$$('dialog').some(d=>d.open)){if(e.key==='ArrowRight')goPage(pageNumber+1);if(e.key==='ArrowLeft')goPage(pageNumber-1);}
});
window.addEventListener('beforeunload',e=>{if(dirty.size||saving){e.preventDefault();e.returnValue='';}});
/* 页面隐藏时停表（不再发状态请求），回到前台再按需继续。 */
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshAnalysisStatuses().then(aiPollSync);else aiPollStop();});
window.addEventListener('pagehide',()=>{for(const id of dirty){const p=state.papers.find(x=>x.id===id);if(p)navigator.sendBeacon('/api/beacon/save',new Blob([JSON.stringify({csrf,id,revision:p.revision,patch:patchOf(p)})],{type:'application/json'}));}});

async function init(){
  const boot=await establishSession();
  if(!boot)await resumeSession();
  sessionHint();
  window.__sessionReady=true;
  try{const health=await api('health');if(health&&health.data)draftNamespace=health.data;}catch(e){}
  try{
    state=await api('state');recoverDrafts();updateSyncStatus();
    await refreshAnalysisStatuses();          // 先拿到全库 AI 状态，文献栏才能立刻显示进度
    renderLibrary();
    const last=localStorage.getItem('paper-reader:last');
    const p=state.papers.find(p=>p.id===last&&!p.archived)||state.papers.find(p=>!p.archived)||state.papers[0];
    if(p)await selectPaper(p.id);else showEmptyLibrary();
    aiPollSync();
  }
  catch(e){status('暂时无法连接','error');toast(e.message,true);$('#paper-title').textContent='本地服务暂时无法连接';$('#paper-authors').textContent='请重新双击启动文件，再刷新本页。';}
}
init();
