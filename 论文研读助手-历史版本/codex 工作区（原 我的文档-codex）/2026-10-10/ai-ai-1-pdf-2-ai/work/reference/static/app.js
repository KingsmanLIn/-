import {getDocument, GlobalWorkerOptions, TextLayer} from '/vendor/pdfjs/pdf.min.mjs';
import {TYPES, ANNOTATIONS, FIELDS, STAGES, TYPE_HINTS, THEORY_FIELDS, PAPERS, ENGLISH_TIP} from './guides.js';

const BOOT = JSON.parse(document.getElementById('boot').textContent);
GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.min.mjs';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = {papers:[],settings:{}}, currentId = '', view = 'reader', tab = 'steps', stage = 0, filter = 'all', annotationFilter = 'all';
let pdf = null, pdfTask = null, pdfRender = null, textLayer = null, pageNumber = 1, zoom = 1, renderVersion = 0, documentVersion = 0;
let draftAnnotation = null, dirty = new Set(), saveTimer = 0, saving = null, toastTimer = 0;
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
  const options={headers:{'X-Reader-Token':BOOT.token}};
  if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await fetch('/api/'+path,options);
  const result=await response.json();
  if(!response.ok)throw new Error(result.error||'操作没有完成');
  return result;
}
function patchOf(p){
  const out={};for(const key of ['title','authors','year','venue','doi','tags','citation','type','language','fields','steps','annotations','pageOffset','pageLabels','lastPage','archived'])out[key]=p[key];
  return structuredClone(out);
}
function markDirty(p){
  dirty.add(p.id);localStorage.setItem('paper-reader:draft:'+p.id,JSON.stringify({revision:p.revision,patch:patchOf(p),time:new Date().toISOString()}));
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
        if(!dirty.has(id))localStorage.removeItem('paper-reader:draft:'+id);
        else localStorage.setItem('paper-reader:draft:'+id,JSON.stringify({revision:p.revision,patch:patchOf(p),time:new Date().toISOString()}));
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
      const raw=localStorage.getItem('paper-reader:draft:'+p.id);if(!raw)continue;
      const draft=JSON.parse(raw);
      if(draft.revision===p.revision){Object.assign(p,draft.patch);dirty.add(p.id);recovered++;}
      else if(draft.time>p.updated){toast('发现另一个窗口的更新与未合并草稿。可在设置中导出浏览器草稿后核对。',true);}
      else localStorage.removeItem('paper-reader:draft:'+p.id);
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
function renderLibrary(){
  $('#paper-count').textContent=state.papers.filter(p=>!p.archived).length;
  const papers=visiblePapers();
  $('#paper-list').innerHTML=papers.map(p=>`<button class="paper-item ${p.id===currentId?'active':''}" data-paper="${esc(p.id)}"><span class="paper-item-title">${esc(p.title)}</span><small>${esc(p.authors.split(/[;；]/)[0]||'作者待核对')} · ${esc(p.year||'年份待核对')} · ${p.language==='en'?'EN':'中文'}</small><div class="mini-progress"><span style="width:${p.steps.filter(Boolean).length*25}%"></span></div></button>`).join('')||'<p class="empty-library">这里还没有匹配的论文。<br>可以导入 PDF，或调整筛选条件。</p>';
}
function renderHeader(){
  const p=current();if(!p)return;
  $('#paper-title').textContent=p.title;
  $('#paper-authors').textContent=[p.authors||'作者待核对',p.venue||'来源待核对',p.year||''].filter(Boolean).join('  /  ');
  $('#paper-meta').innerHTML=`<span class="chip ${p.language==='en'?'en':''}">${p.language==='en'?'ENGLISH PAPER':'中文论文'}</span><span class="chip">${esc(TYPES[p.type])}</span>${p.tags?`<span class="chip neutral">${esc(p.tags)}</span>`:''}${p.archived?'<span class="chip neutral">已归档</span>':''}`;
  renderProgress();
}
function renderProgress(){
  const p=current();if(!p)return;
  const count=p.steps.filter(Boolean).length;
  $('#progress-label').textContent=count+' / 4 已确认';$('#progress-bar').style.width=count*25+'%';
  $('#ribbon-hint').textContent=count===4?'已经完成一轮阅读，下一次从疑问出发。':'先抓住问题，再拆解证据。';
}
async function selectPaper(id){
  if(id===currentId&&pdf){setView('reader');return;}
  try{await flush();}catch(e){toast(e.message,true);return;}
  currentId=id;const p=current();if(!p)return;
  localStorage.setItem('paper-reader:last',id);pageNumber=p.lastPage||1;stage=Math.max(0,p.steps.findIndex(x=>!x));tab='steps';annotationFilter='all';
  setView('reader');renderHeader();renderLibrary();renderNotes();await loadPdf(p);
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
  const p=current();if(!p){$('#notes-content').innerHTML='导入一篇 PDF，开始你的第一次阅读。';return;}
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
  $('#notes-content').innerHTML=`<div class="notes-section-header"><h2>本篇阅读卡</h2><button class="text-button" id="card-export">导出 Markdown ↗</button></div><div class="card-note">阅读卡自动汇总你的记录。每一栏都可以回到“阅读步骤”继续完善。</div><article class="card-preview"><h2>${esc(p.title)}</h2><p class="card-meta">${esc(p.authors)} · ${esc(p.year)}<br>${esc(TYPES[p.type])} · ${p.steps.filter(Boolean).length}/4 步已确认</p>${Object.entries(FIELDS).map(([k,v])=>`<h3>${esc(definition(p,k)[0])}</h3><p class="${p.fields[k]?'':'unfilled'}">${esc(p.fields[k]||'待记录')}</p>`).join('')}<h3>批注与术语</h3><p>共 ${p.annotations.length} 条；导出的 Markdown 会包含原句、理解和页码。</p></article>`;
}
async function loadPdf(p){
  const version=++documentVersion;renderVersion++;
  if(pdfRender){try{pdfRender.cancel();}catch{}}
  if(textLayer){try{textLayer.cancel();}catch{}}
  pdf=null;$('#pdf-page').style.display='none';$('#pdf-loading').style.display='block';$('#pdf-loading').textContent='正在加载原文…';$('#page-total').textContent='/ —';
  if(pdfTask){try{await pdfTask.destroy();}catch{}}
  try{
    const task=getDocument({url:'/api/pdf/'+p.id,httpHeaders:{'X-Reader-Token':BOOT.token},cMapUrl:'/vendor/pdfjs/cmaps/',cMapPacked:true,standardFontDataUrl:'/vendor/pdfjs/standard_fonts/',wasmUrl:'/vendor/pdfjs/wasm/',iccUrl:'/vendor/pdfjs/iccs/'});
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
  const initial=page.getViewport({scale:1});const width=Math.max(190,$('#pdf-scroll').clientWidth-44);const scale=width/initial.width*zoom;const viewport=page.getViewport({scale});
  const wrapper=$('#pdf-page'),canvas=$('#pdf-canvas'),layer=$('#text-layer');
  wrapper.style.width=viewport.width+'px';wrapper.style.height=viewport.height+'px';wrapper.style.setProperty('--scale-factor',scale);wrapper.style.setProperty('--total-scale-factor',scale);
  const ratio=Math.min(window.devicePixelRatio||1,2);canvas.width=Math.floor(viewport.width*ratio);canvas.height=Math.floor(viewport.height*ratio);canvas.style.width=viewport.width+'px';canvas.style.height=viewport.height+'px';
  layer.replaceChildren();layer.style.setProperty('--scale-factor',scale);layer.style.setProperty('--total-scale-factor',scale);
  $('#pdf-loading').style.display='none';wrapper.style.display='block';$('#page-input').value=n;$('#printed-label').textContent='PDF 第 '+n+' 页 · 文内 '+label(current(),n);$('#prev-page').disabled=n===1;$('#next-page').disabled=n===doc.numPages;
  const canvasTask=page.render({canvasContext:canvas.getContext('2d'),viewport,transform:ratio!==1?[ratio,0,0,ratio,0,0]:null});pdfRender=canvasTask;
  try{await canvasTask.promise;if(sequence!==renderVersion)return;const content=await page.getTextContent();if(sequence!==renderVersion)return;textLayer=new TextLayer({textContentSource:content,container:layer,viewport});await textLayer.render();renderHighlights();}
  catch(e){if(e.name!=='RenderingCancelledException'&&sequence===renderVersion){console.error(e);toast('本页显示有问题，可以下载原文继续阅读。',true);}}
}
function renderHighlights(){
  const p=current();if(!p)return;
  $('#highlight-layer').innerHTML=p.annotations.filter(a=>a.page===pageNumber).flatMap(a=>(a.rects||[]).map(r=>`<span class="pdf-highlight ${a.type}" style="left:${r[0]*100}%;top:${r[1]*100}%;width:${r[2]*100}%;height:${r[3]*100}%"></span>`)).join('');
}
async function goPage(value){
  if(!pdf)return;const n=Math.min(pdf.numPages,Math.max(1,Number(value)||1));if(n===pageNumber)return;
  pageNumber=n;current().lastPage=n;markDirty(current());$('#pdf-scroll').scrollTop=0;window.getSelection()?.removeAllRanges();await renderPage();
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
  await flush();const r=await fetch('/api/'+path,{headers:{'X-Reader-Token':BOOT.token}});
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
  $('#vault-path').value=state.settings.vault_path||'';$('#sync-enabled').checked=Boolean(state.settings.sync_enabled);updateSyncStatus();
  if(!$('#draft-export')&&Object.keys(localStorage).some(k=>k.startsWith('paper-reader:draft:'))){const b=document.createElement('button');b.id='draft-export';b.className='secondary';b.textContent='导出浏览器草稿';b.addEventListener('click',()=>{const data={};for(const k of Object.keys(localStorage))if(k.startsWith('paper-reader:draft:'))data[k]=JSON.parse(localStorage.getItem(k));downloadBlob(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),'未合并阅读草稿.json');});$('#restore-button').after(b);}
  $('#settings-dialog').showModal();
}
function openMetadata(){
  const p=current();if(!p)return;
  for(const k of ['title','authors','year','venue','type','language','tags','doi','citation'])$('#meta-'+k).value=p[k]||'';
  $('#meta-offset').value=p.pageOffset||0;$('#archive-paper').textContent=p.archived?'恢复到文献库':'将本文移至归档';$('#metadata-dialog').showModal();
}

$('#library-search').addEventListener('input',renderLibrary);
$('.library-filters').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(!b)return;filter=b.dataset.filter;$$('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));renderLibrary();});
$('.main-nav').addEventListener('click',e=>{const b=e.target.closest('[data-view]');if(b)setView(b.dataset.view);});
document.addEventListener('click',async e=>{
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
$('#page-input').addEventListener('change',e=>goPage(e.target.value));$('#page-input').addEventListener('keydown',e=>{if(e.key==='Enter')goPage(e.target.value);});
$('#zoom-select').addEventListener('change',e=>{zoom=Number(e.target.value);renderPage();});
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
$('#download-pdf').addEventListener('click',()=>download('pdf/'+currentId,current().title.replace(/[<>:"/\\|?*]/g,'_')+'.pdf').catch(e=>toast(e.message,true)));
$('#settings-button').addEventListener('click',openSettings);$('#quick-sync').addEventListener('click',openSettings);
$('#browse-vault').addEventListener('click',async()=>{const b=$('#browse-vault');b.disabled=true;b.textContent='等待选择…';try{const r=await api('choose-vault',{});if(r.path)$('#vault-path').value=r.path;}catch(e){toast('也可以直接填写路径：'+e.message,true);}finally{b.disabled=false;b.textContent='浏览…';}});
$('#save-settings').addEventListener('click',async()=>{const b=$('#save-settings');b.disabled=true;try{await flush();const r=await api('settings',{vault_path:$('#vault-path').value,sync_enabled:$('#sync-enabled').checked});state.settings=r.settings;updateSyncStatus();toast(r.settings.sync_error?'设置已保存；请查看同步提示。':'同步设置已保存。',Boolean(r.settings.sync_error));}catch(e){toast(e.message,true);}finally{b.disabled=false;}});
$('#sync-now').addEventListener('click',async()=>{const b=$('#sync-now');b.disabled=true;try{await flush();const r=await api('sync',{});state.settings=r.settings;updateSyncStatus();toast(r.settings.sync_error?'部分笔记同步待处理，请查看提示。':'全部文献已同步到 Obsidian。',Boolean(r.settings.sync_error));}catch(e){toast(e.message,true);}finally{b.disabled=false;}});
$('#backup-button').addEventListener('click',()=>download('backup','论文研读完整备份-'+new Date().toISOString().slice(0,10)+'.zip').catch(e=>toast(e.message,true)));
$('#restore-button').addEventListener('click',()=>$('#backup-file').click());
$('#backup-file').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;try{await flush();const response=await fetch('/api/restore',{method:'POST',headers:{'X-Reader-Token':BOOT.token},body:file});const r=await response.json();if(!response.ok)throw new Error(r.error);state=r.state;renderLibrary();updateSyncStatus();toast('已恢复 '+r.imported+' 篇缺少的文献；同编号记录已保留。');}catch(e){toast(e.message,true);}finally{e.target.value='';}});
$('#import-button').addEventListener('click',()=>$('#pdf-file').click());
$('#pdf-file').addEventListener('change',async e=>{
  const files=[...e.target.files];if(!files.length)return;const button=$('#import-button');button.disabled=true;button.textContent='正在导入…';let added=[];
  try{await flush();for(const file of files){if(file.size>80*1024*1024)throw new Error(file.name+' 超过 80 MB。');const response=await fetch('/api/import',{method:'POST',headers:{'X-Reader-Token':BOOT.token,'X-File-Name':encodeURIComponent(file.name)},body:file});const r=await response.json();if(!response.ok)throw new Error(r.error);state.papers.push(r.paper);added.push(r.paper.id);}filter='all';$$('[data-filter]').forEach(x=>x.classList.toggle('active',x.dataset.filter===filter));$('#library-search').value='';renderLibrary();await selectPaper(added[0]);openMetadata();toast('已导入 '+added.length+' 篇论文，请核对文献信息。');}
  catch(e){renderLibrary();toast(e.message,true);}finally{button.disabled=false;button.textContent='＋ 导入 PDF';e.target.value='';}
});
$('#shutdown-button').addEventListener('click',async()=>{try{await flush();await api('shutdown',{});$('#settings-dialog').close();status('已保存并退出');$('#notes-content').innerHTML='<div class="empty-note">所有记录已保存。<br>下次双击启动文件即可继续阅读。</div>';toast('软件已退出，可以关闭本页。');}catch(e){toast(e.message,true);}});
let resizeTimer=0;new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>renderPage(),140);}).observe($('#pdf-scroll'));
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();flush().then(()=>toast('阅读记录已保存。')).catch(err=>toast(err.message,true));}
  if((e.ctrlKey||e.metaKey)&&e.key==='Enter'&&$('#annotation-dialog').open){e.preventDefault();$('#annotation-form').requestSubmit();}
  if(e.target.matches('input,textarea,select'))return;
  if(view==='reader'&&!$$('dialog').some(d=>d.open)){if(e.key==='ArrowRight')goPage(pageNumber+1);if(e.key==='ArrowLeft')goPage(pageNumber-1);}
});
window.addEventListener('beforeunload',e=>{if(dirty.size||saving){e.preventDefault();e.returnValue='';}});
window.addEventListener('pagehide',()=>{for(const id of dirty){const p=state.papers.find(x=>x.id===id);if(p)navigator.sendBeacon('/api/save',new Blob([JSON.stringify({token:BOOT.token,id,revision:p.revision,patch:patchOf(p)})],{type:'application/json'}));}});

async function init(){
  try{state=await api('state');recoverDrafts();updateSyncStatus();renderLibrary();const last=localStorage.getItem('paper-reader:last');const p=state.papers.find(p=>p.id===last&&!p.archived)||state.papers.find(p=>!p.archived)||state.papers[0];if(p)await selectPaper(p.id);else{$('#paper-title').textContent='从第一篇论文开始';$('#paper-authors').textContent='点击左下角“导入 PDF”，把原文与自己的理解放在一起。';$('#pdf-loading').textContent='导入 PDF 后，这里会显示论文原文。';renderNotes();}}
  catch(e){status('暂时无法连接','error');toast(e.message,true);$('#paper-title').textContent='本地服务暂时无法连接';$('#paper-authors').textContent='请重新双击启动文件，再刷新本页。';}
}
init();
