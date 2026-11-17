'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  library:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  check:'<path d="m5 12 4 4L19 6"/>', clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  heart:'<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>',
  settings:'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
  plus:'<path d="M12 5v14M5 12h14"/>', search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  arrow:'<path d="M5 12h14m-6-6 6 6-6 6"/>', left:'<path d="m15 5-7 7 7 7"/>', right:'<path d="m9 5 7 7-7 7"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>', upload:'<path d="M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>', expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  refresh:'<path d="M20 7V3l-3 3a9 9 0 1 0 3 10M20 3h-5"/>', list:'<path d="M9 6h12M9 12h12M9 18h12M3 6h1M3 12h1M3 18h1"/>',
  star:'<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-2.9-5.6 2.9 1.1-6.2L3 9.6l6.2-.9Z"/>',
  folder:'<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  mark:'<rect x="3" y="4" width="18" height="14" rx="2"/><path d="m8 11 3 3 5-6M8 22h8"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>'
};
const icon = (name, cls = '') => '<svg class="icon '+cls+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(paths[name] || paths.image)+'</svg>';
let decks = [], state, source = '', sourceAvailable = true;
let view = 'all', query = '', sort = 'default', styleFilter = '', layout = 'grid';
let activeId = null, slide = 0, overview = false, lightZoom = false;
let saveTimer, saveFlight, saving = false, pending = false, saveError = false, toastTimer;
let selectedFiles = [], importMode = 'new', importBusy = false, restoreFile;
const modal = $('#modal'), lightbox = $('#lightbox');
const review = id => state.reviews[id] || { scores: {}, note: '', favorite: false };
const hasScore = n => typeof n === 'number' && Number.isFinite(n);
const reviewStats = id => Scoring.stats(review(id),decks.find(d=>d.id===id),state.dimensions);
const scoreValues = id => reviewStats(id).values;
const completed = id => reviewStats(id).complete;
const average = id => reviewStats(id).mean;
const fmt = n => n == null ? '—' : Number(n.toFixed(2)).toString();
const activeDeck = () => decks.find(d => d.id === activeId);
const statusText = id => completed(id) ? '已评分' : scoreValues(id).length ? '评分中' : '待评分';

async function api(url, options = {}) {
  if(window.Cloud)return Cloud.request(url,options);
  const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作失败，请重试');
  return result;
}
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.className = error ? 'visible error' : 'visible';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.className = '', 4500);
}
function mutableReview(id) {
  if (!state.reviews[id]) state.reviews[id] = {scores:{},note:'',favorite:false};
  return state.reviews[id];
}
function currentEntry(create=false) {
  const r=create?mutableReview(activeId):review(activeId);
  if(Scoring.mode(r)!=='page')return r;
  const image=activeDeck().images[slide];
  if(!create)return Scoring.page(r,image);
  r.pages ||= {};
  return r.pages[image.id] ||= {scores:{},note:''};
}
function setScoringMode(mode) {
  const r=mutableReview(activeId);r.mode=mode==='page'?'page':'deck';
  r.updatedAt=new Date().toISOString();overview=false;queueSave();renderDetail();
  $('.sidebar').outerHTML=sidebar();if(window.Cloud)decorateCloud();
}
function setSaveStatus(text, error = false) {
  if(window.Cloud)text=text.replace('到本地','到云端');
  $$('.save-status').forEach(el => { el.innerHTML = '<span class="status-dot '+(error?'error':'')+'"></span>'+esc(text); });
}
function queueSave() {
  pending = true; setSaveStatus('正在保存…');
  clearTimeout(saveTimer); saveTimer = setTimeout(flushSave, 450);
}
async function flushSave() {
  clearTimeout(saveTimer);
  if (saving) { await saveFlight; if(saveError)return false; return pending?flushSave():true; }
  if (!pending) return !saveError;
  saving = true; pending = false;
  const snapshot = structuredClone(state);
  saveFlight = (async () => { try {
    const result = await api('/api/state', {method:'PUT',body:JSON.stringify(snapshot)});
    state.revision = result.revision; saveError = false;
    if(result.configRevision!==undefined)state.configRevision=result.configRevision;
    setSaveStatus(pending ? '正在保存…' : '已保存到本地');
  } catch (err) {
    pending = true; saveError = true; setSaveStatus('未保存 · 点击重试', true); toast(err.message, true);
  } finally { saving = false; } })();
  await saveFlight;
  if (pending && !saveError) return flushSave();
  return !saveError;
}
window.addEventListener('beforeunload', e => { if (pending || saving) { e.preventDefault(); e.returnValue = ''; } });
document.addEventListener('visibilitychange', () => { if (document.hidden && pending && !saving) flushSave(); });

function sidebar() {
  const done = decks.filter(d => completed(d.id)).length;
  const favorites = decks.filter(d=>review(d.id).favorite).length;
  const nav = [ ['all','library','全部作品',decks.length], ['pending','clock','待评分',decks.length-done], ['rated','check','已评分',done], ['favorites','heart','我的收藏',favorites] ];
  return '<aside class="sidebar"><a class="brand" href="#" data-action="home"><span class="brand-mark">'+icon('mark')+'</span><span>观页<span class="brand-en">SLIDE CURATOR</span></span></a><div class="workspace-label">我的评审工作台</div><nav aria-label="作品导航">'+nav.map(([id,ic,label,count])=>'<button class="nav-item '+(!activeId&&view===id?'active':'')+'" data-view="'+id+'">'+icon(ic)+'<span>'+label+'</span><span class="nav-count">'+count+'</span></button>').join('')+'</nav><div class="sidebar-divider"></div><button class="nav-item" data-action="settings">'+icon('settings')+'<span>评分设置</span></button><button class="nav-item" data-action="export">'+icon('download')+'<span>导出与备份</span></button><div class="sidebar-bottom"><div class="progress-label"><span>本轮评审进度</span><b>'+done+' / '+decks.length+'</b></div><div class="progress-track"><span style="width:'+(decks.length?done/decks.length*100:0)+'%"></span></div><p>慢慢看，认真选。</p><button class="save-status" data-action="retry-save"><span class="status-dot"></span>'+(pending?'正在保存…':saveError?'未保存 · 点击重试':'已保存到本地')+'</button></div></aside>';
}
function topbar() {
  return '<header class="topbar"><div class="breadcrumb">个人空间 <span>/</span> <b>'+(activeId?'作品评审':'作品库')+'</b></div><div class="topbar-actions"><span class="local-label"><span class="status-dot"></span>本地作品库</span><div class="mobile-tools"><button class="icon-button" data-action="settings" aria-label="评分设置">'+icon('settings')+'</button><button class="icon-button" data-action="export" aria-label="导出与备份">'+icon('download')+'</button></div><button class="button quiet small" data-action="rescan" title="重新扫描原文件夹">'+icon('refresh')+'<span>刷新图片库</span></button><span class="avatar">观</span></div></header>';
}
function filtered() {
  let list = decks.filter(d => {
    if (view === 'pending' && completed(d.id)) return false;
    if (view === 'rated' && !completed(d.id)) return false;
    if (view === 'favorites' && !review(d.id).favorite) return false;
    if (styleFilter && !d.style.includes(styleFilter)) return false;
    return !query || (d.name+' '+review(d.id).note).toLocaleLowerCase().includes(query.toLocaleLowerCase());
  });
  if (sort === 'high' || sort === 'low') list.sort((a,b) => { const x=average(a.id), y=average(b.id); if(x==null)return y==null?0:1;if(y==null)return -1; return sort==='high'?y-x:x-y; });
  if (sort === 'name') list.sort((a,b)=>a.title.localeCompare(b.title,'zh-CN',{numeric:true}));
  if (sort === 'recent') list.sort((a,b)=>(review(b.id).updatedAt||'').localeCompare(review(a.id).updatedAt||''));
  return list;
}
function card(d) {
  const avg = average(d.id), done = completed(d.id), r = review(d.id);
  return '<article class="deck-card"><div class="card-visual"><button class="cover-button" data-open="'+d.id+'" aria-label="查看 '+esc(d.title)+'"><img src="'+d.images[0].thumb+'" alt="'+esc(d.title)+'预览" loading="lazy"><span class="cover-open">查看整套 '+icon('arrow')+'</span></button><span class="image-count">'+icon('image')+d.images.length+' 张</span><button class="favorite-button '+(r.favorite?'selected':'')+'" data-favorite="'+d.id+'" aria-label="'+(r.favorite?'取消收藏':'收藏')+esc(d.title)+'" aria-pressed="'+!!r.favorite+'">'+icon('heart')+'</button></div><div class="card-content"><div class="card-heading"><button data-open="'+d.id+'" class="card-title">'+esc(d.title)+'</button><span class="score-badge '+(done?'rated':'')+'">'+(avg==null?'—':fmt(avg))+'<small>/ '+state.maxScore+'</small></span></div><div class="card-meta"><span>'+esc(d.style)+'</span><span class="review-state '+(done?'done':'')+'">'+(done?icon('check'):'<i></i>')+statusText(d.id)+'</span></div><div class="card-filmstrip">'+d.images.slice(0,3).map((im,i)=>'<button data-open="'+d.id+'" data-slide="'+i+'" aria-label="查看第 '+(i+1)+' 张"><img src="'+im.thumb+'" alt="" loading="lazy"></button>').join('')+'<button data-open="'+d.id+'" class="filmstrip-more" aria-label="查看全部 '+d.images.length+' 张">'+(d.images.length>3?'+'+(d.images.length-3):icon('arrow'))+'</button></div></div></article>';
}
function gallery() {
  const done = decks.filter(d=>completed(d.id)).length;
  const list = filtered();
  const title = {all:'作品库',pending:'待评分',rated:'已评分',favorites:'我的收藏'}[view];
  return '<section class="library-head"><div><div class="eyebrow">THE PRESENTATION COLLECTION</div><h1>'+title+'<span class="title-dot">.</span></h1><p>从视觉到排版，记录你对每一套设计的判断。</p></div><button class="button primary" data-action="import">'+icon('plus')+'导入作品</button></section><section class="collection-summary"><div class="summary-item"><b>'+decks.length+'</b><span>套作品</span></div><div class="summary-item"><b>'+decks.reduce((n,d)=>n+d.images.length,0)+'</b><span>张灵感切片</span></div><div class="summary-item"><b>'+done+'</b><span>套已完成评分</span></div><div class="summary-rule"></div><div class="scoring-summary"><span>当前评分标准</span><b>'+state.dimensions.map(d=>esc(d.name)).join('<i> / </i>')+'<small>'+state.maxScore+' 分制</small></b></div><button class="icon-button" data-action="settings" aria-label="修改评分标准">'+icon('settings')+'</button></section>'+(!sourceAvailable?'<div class="source-warning">原图片文件夹暂不可用。请检查文件夹位置，或点击“导入作品”添加图片。</div>':'')+'<div class="filterbar"><label class="search-field">'+icon('search')+'<input id="search" type="search" placeholder="搜索作品、风格或备注…" aria-label="搜索作品" value="'+esc(query)+'"><kbd>/</kbd></label><div class="filter-controls"><select id="style-filter" aria-label="风格筛选"><option value="">全部风格</option>'+['蓝','青','绿','红','棕','米','黑','灰','紫'].map(x=>'<option value="'+x+'" '+(styleFilter===x?'selected':'')+'>'+x+'色系</option>').join('')+'</select><select id="sort" aria-label="作品排序">'+[['default','默认排序'],['high','评分从高到低'],['low','评分从低到高'],['recent','最近评分'],['name','名称排序']].map(([v,n])=>'<option value="'+v+'" '+(sort===v?'selected':'')+'>'+n+'</option>').join('')+'</select><div class="layout-toggle"><button class="'+(layout==='grid'?'selected':'')+'" data-layout="grid" aria-label="网格视图">'+icon('library')+'</button><button class="'+(layout==='list'?'selected':'')+'" data-layout="list" aria-label="紧凑视图">'+icon('list')+'</button></div></div></div><div class="results-line"><span id="result-count">共 '+list.length+' 套作品</span><span>支持整套 / 逐页评分 · 自动保存</span></div><section id="deck-grid" class="deck-grid '+(layout==='list'?'compact':'')+'" aria-label="PPT作品列表">'+(list.length?list.map(card).join(''):emptyState())+'</section><footer class="library-footer"><span>观页 · 让好设计有迹可循</span><span>图片保留原始比例</span></footer>';
}
function emptyState() { return '<div class="empty-state">'+icon('search')+'<h3>这里暂时没有作品</h3><p>试试其他关键词、切换筛选，或导入一套新作品。</p><button class="button" data-action="reset-filter">查看全部作品</button></div>'; }
function updateGrid() {
  const list = filtered(); $('#deck-grid').innerHTML = list.length?list.map(card).join(''):emptyState();
  $('#deck-grid').className = 'deck-grid '+(layout==='list'?'compact':''); $('#result-count').textContent = '共 '+list.length+' 套作品';
}
function scorePanel(d) {
  const r=currentEntry(), perPage=Scoring.mode(review(d.id))==='page', stats=reviewStats(d.id);
  const values=state.dimensions.map(dim=>r.scores[dim.id]).filter(hasScore), avg=perPage?(values.length?values.reduce((a,b)=>a+b,0)/values.length:null):stats.mean, done=perPage?values.length===state.dimensions.length:stats.complete;
  return '<aside class="scoring-panel"><div class="panel-heading"><div><span class="eyebrow">YOUR PERSPECTIVE</span><h2>'+(perPage?'第 '+(slide+1)+' 页评分':'整套评分')+'</h2></div><button class="icon-button" data-action="settings" aria-label="评分设置">'+icon('settings')+'</button></div><div class="scoring-mode segmented" aria-label="评分模式"><button data-scoring-mode="deck" class="'+(!perPage?'selected':'')+'">整套评分</button><button data-scoring-mode="page" class="'+(perPage?'selected':'')+'">逐页评分</button></div><p class="mode-description">'+(perPage?'每页独立评分 · 已完成 '+stats.pagesDone+' / '+d.images.length+' 页':'针对整套作品评价 · 切换模式会保留已有评分')+'</p><div class="overall-score"><div><span class="overall-value">'+fmt(avg)+'</span><span class="overall-max"> / '+state.maxScore+'</span></div><div class="overall-label"><span>'+(done?'综合均分':avg==null?'等待你的评价':'当前均分')+'</span><small>'+values.length+' / '+state.dimensions.length+' 项已评</small></div></div><div class="dimension-list">'+state.dimensions.map(dim=>{
    const value=r.scores[dim.id];
    return '<div class="dimension" data-dimension="'+esc(dim.id)+'"><div class="dimension-heading"><label for="score-'+esc(dim.id)+'">'+esc(dim.name)+'</label><div class="numeric-score"><input id="score-'+esc(dim.id)+'" data-score-input="'+esc(dim.id)+'" type="number" min="0" max="'+state.maxScore+'" step="0.1" placeholder="—" value="'+(hasScore(value)?value:'')+'" aria-label="'+esc(dim.name)+'分数"><span>/ '+state.maxScore+'</span></div></div><p>'+esc(dim.description||'为这一维度记录你的判断')+'</p><div class="score-options">'+[1,2,3,4,5].map(n=>{const num=Math.round(state.maxScore*n/5*100)/100;return '<button data-score-dim="'+esc(dim.id)+'" data-score-value="'+num+'" class="'+(hasScore(value)&&Math.abs(value-num)<.001?'chosen':'')+'" aria-label="'+esc(dim.name)+' '+num+' 分">'+fmt(num)+'</button>';}).join('')+'</div><div class="score-range-label"><span>仍有提升空间</span><span>表现出色</span></div></div>';
  }).join('')+'</div><div class="note-field"><label for="review-note">评审笔记 <span>可选</span></label><textarea id="review-note" maxlength="10000" placeholder="哪些地方值得借鉴？哪些细节可以更好？">'+esc(r.note||'')+'</textarea></div><button class="save-status panel-save" data-action="retry-save"><span class="status-dot"></span>'+(pending?'正在保存…':saveError?'未保存 · 点击重试':'已保存到本地')+'</button><button class="button primary next-review" data-action="'+(perPage?'next-page':'next-pending')+'">'+(perPage?(slide<d.images.length-1?'保存并评下一页':'完成并评下一套'):(done?'继续评审下一套':'保存并评下一套'))+icon('arrow')+'</button><p class="score-footnote">'+(perPage?'整套逐页均分 '+fmt(stats.mean)+' · 所有页面完成后计入团队均分':'综合分为当前评分项的等权平均分')+'</p></aside>';
}
function stage(d) {
  if (overview) return '<div class="contact-sheet">'+d.images.map((im,i)=>'<button data-preview-slide="'+i+'"><img src="'+im.thumb+'" loading="lazy" alt="'+esc(d.title)+' 第 '+(i+1)+' 张"><span>'+String(i+1).padStart(2,'0')+'</span></button>').join('')+'</div>';
  return '<div class="preview-stage"><button class="stage-arrow previous" data-action="prev-slide" '+(slide===0?'disabled':'')+' aria-label="上一张">'+icon('left')+'</button><button class="main-image-button" data-action="lightbox" aria-label="放大查看原图"><img id="main-image" src="'+d.images[slide].url+'" alt="'+esc(d.title)+' 第 '+(slide+1)+' 张"></button><button class="stage-arrow next" data-action="next-slide" '+(slide===d.images.length-1?'disabled':'')+' aria-label="下一张">'+icon('right')+'</button><span class="stage-hint">'+icon('expand')+'点击图片，查看细节</span></div><div class="slide-bottom"><span class="slide-position">'+String(slide+1).padStart(2,'0')+' <i>/ '+String(d.images.length).padStart(2,'0')+'</i></span><span class="slide-filename" title="'+esc(d.images[slide].name)+'">'+esc(d.images[slide].name)+'</span><span class="key-help"><kbd>←</kbd><kbd>→</kbd> 切换图片</span></div><div class="thumbnail-strip">'+d.images.map((im,i)=>'<button class="thumbnail '+(i===slide?'current':'')+' '+(Scoring.mode(review(d.id))==='page'&&state.dimensions.every(dim=>hasScore(Scoring.page(review(d.id),im).scores[dim.id]))?'page-rated':'')+'" data-slide-index="'+i+'" aria-label="第 '+(i+1)+' 张" aria-current="'+(i===slide)+'"><img src="'+im.thumb+'" alt="" loading="lazy"><span>'+String(i+1).padStart(2,'0')+'</span></button>').join('')+'</div>';
}
function detail() {
  const d=activeDeck(); if(!d) return '<div class="empty-state">作品不存在，请返回作品库。</div>';
  slide=Math.max(0,Math.min(slide,d.images.length-1));
  return '<div class="detail-breadcrumb"><button class="text-button" data-action="back">'+icon('left')+'返回作品库</button><span>整套 / 逐页自由切换</span></div><div class="detail-heading"><div><span class="style-label">'+esc(d.style)+' <i>·</i> '+d.images.length+' 张图片</span><h1>'+esc(d.title)+'</h1></div><div class="detail-actions"><button class="button '+(review(d.id).favorite?'is-favorite':'')+'" data-favorite="'+d.id+'">'+icon('heart')+(review(d.id).favorite?'已收藏':'收藏作品')+'</button><button class="button" data-action="append">'+icon('plus')+'添加图片</button></div></div><div class="review-layout"><section class="viewer"><div class="viewer-toolbar"><div class="segmented"><button data-overview="false" class="'+(!overview?'selected':'')+'">'+icon('image')+'单图浏览</button><button data-overview="true" class="'+(overview?'selected':'')+'">'+icon('library')+'整套总览</button></div><button class="icon-button" data-action="lightbox" aria-label="放大当前图片">'+icon('expand')+'</button></div><div id="stage-container">'+stage(d)+'</div></section><div id="panel-container">'+scorePanel(d)+'</div></div>';
}
function render() {
  $('#app').innerHTML = sidebar()+'<div class="main-shell">'+topbar()+'<main class="main-content '+(activeId?'detail-content':'')+'">'+(activeId?detail():gallery())+'</main></div>';
  if(window.Cloud)decorateCloud();
}
function renderDetail() { const d=activeDeck(); if(d) $('.main-content').innerHTML=detail(); }
let returnScroll=0;
function openDeck(id, index=0) {
  if(!activeId) returnScroll=window.scrollY;
  activeId=id;slide=index;overview=false;
  history.pushState(null,'','#deck='+encodeURIComponent(id)); render();window.scrollTo(0,0);
}
function backToLibrary() { activeId=null;history.pushState(null,'',location.pathname);render();window.scrollTo(0,returnScroll); }
function changeSlide(index) {
  const d=activeDeck(); if(!d)return;
  slide=Math.max(0,Math.min(index,d.images.length-1));
  if(lightbox.open) return renderLightbox();
  overview=false; $('#stage-container').innerHTML=stage(d);
  if(Scoring.mode(review(d.id))==='page'){$('#panel-container').innerHTML=scorePanel(d);if(window.Cloud)decorateCloud();}
  $$('.segmented button').forEach(b=>b.classList.toggle('selected',b.dataset.overview==='false'));
  $('.thumbnail.current')?.scrollIntoView({block:'nearest',inline:'nearest'});
}
function updateScore(dim,value) {
  const r=currentEntry(true);
  if(value===null) delete r.scores[dim]; else r.scores[dim]=value;
  mutableReview(activeId).updatedAt=new Date().toISOString(); queueSave();
  $('#panel-container').innerHTML=scorePanel(activeDeck());
  if(Scoring.mode(review(activeId))==='page')$$('.thumbnail').forEach((el,i)=>el.classList.toggle('page-rated',state.dimensions.every(d=>hasScore(Scoring.page(review(activeId),activeDeck().images[i]).scores[d.id]))));
  const sidebarEl=$('.sidebar');sidebarEl.outerHTML=sidebar();
  if(window.Cloud)decorateCloud();
}

function modalHeader(title,subtitle='') {
  return '<div class="modal-header"><div><span class="eyebrow">SLIDE CURATOR</span><h2 id="modal-title">'+title+'</h2>'+(subtitle?'<p>'+subtitle+'</p>':'')+'</div><button class="icon-button" data-action="close-modal" aria-label="关闭">'+icon('close')+'</button></div>';
}
function showModal(html,cls='') { modal.className=cls;modal.innerHTML=html;if(!modal.open)modal.showModal(); }
function showSettings() {
  const subtitle=window.Cloud?'团队成员均可修改，保存后对所有人生效。':'按你的评审习惯，建立一套自己的标准。';
  showModal(modalHeader('评分设置',subtitle)+'<form id="settings-form"><div class="settings-scale"><div><label for="max-score">评分满分</label><p>适用于所有评分项，可输入 1–100。</p></div><div class="max-score-input"><input id="max-score" type="number" min="1" max="100" step="0.1" required value="'+state.maxScore+'"><span>分</span></div></div><div class="scale-notice">'+icon('info')+(window.Cloud?'调整满分时，全团队已有分数会按比例换算。':'调整满分时，已有分数会按比例换算。')+'</div><div class="settings-section-title"><h3>评分项</h3><span>至少 1 项，最多 12 项</span></div><div id="settings-dimensions">'+state.dimensions.map(settingsRow).join('')+'</div><button type="button" class="add-dimension" data-action="add-dimension">'+icon('plus')+'添加评分项</button><p class="settings-note">移除评分项后，已有分数会保留在记录中，但不再参与均分。</p><div class="form-error" id="settings-error" role="alert"></div><div class="modal-footer"><button type="button" class="button" data-action="close-modal">取消</button><button class="button primary" type="submit">保存设置</button></div></form>','settings-modal');
}
function settingsRow(d) {
  return '<div class="settings-row" data-dim-id="'+esc(d.id)+'"><div class="settings-row-fields"><input class="dimension-name" aria-label="评分项名称" maxlength="30" required placeholder="评分项名称" value="'+esc(d.name)+'"><input class="dimension-desc" aria-label="评分项说明" maxlength="200" placeholder="评分时关注哪些细节？（可选）" value="'+esc(d.description||'')+'"></div><button type="button" class="icon-button remove-dim" aria-label="移除此评分项">'+icon('trash')+'</button></div>';
}
async function applySettings(e) {
  e.preventDefault(); const err=$('#settings-error'); err.textContent='';
  const maximum=Number($('#max-score').value);
  const dimensions=$$('.settings-row').map(row=>({id:row.dataset.dimId,name:$('.dimension-name',row).value.trim(),description:$('.dimension-desc',row).value.trim()}));
  if(!dimensions.length||dimensions.length>12){err.textContent='请保留 1 至 12 个评分项。';return;}
  if(dimensions.some(d=>!d.name)){err.textContent='请填写每个评分项的名称。';return;}
  if(new Set(dimensions.map(d=>d.name)).size!==dimensions.length){err.textContent='评分项名称不能重复。';return;}
  if(!Number.isFinite(maximum)||maximum<1||maximum>100){err.textContent='满分应在 1 至 100 之间。';return;}
  if(maximum!==state.maxScore) Object.values(state.reviews).forEach(r=>Scoring.rescale(r,state.maxScore,maximum));
  state.maxScore=maximum;state.dimensions=dimensions;queueSave();modal.close();render();
  if(await flushSave())toast('评分标准已更新');
}
function showImport(target=null) {
  importMode=target?'append':'new'; selectedFiles=[];importBusy=false;
  showModal(modalHeader(target?'添加图片':'导入作品',target?'把更多图片加入「'+esc(activeDeck().title)+'」。':'选择一套图片，或按文件夹批量导入多套作品。')+'<form id="import-form" data-target="'+esc(target||'')+'">'+(!target?'<label class="field-label" for="import-name">作品名称</label><input id="import-name" class="text-input" maxlength="100" placeholder="例如：青绿农业_智耕科技">':'')+'<div class="dropzone" id="dropzone">'+icon('upload')+'<h3>把 PPT 图片拖到这里</h3><p>JPG、PNG、WEBP、GIF、BMP · 单张不超过 25 MB</p><div class="dropzone-buttons"><button type="button" class="button" data-action="choose-images">选择图片</button>'+(!target?'<button type="button" class="button" data-action="choose-folder">选择文件夹</button>':'')+'</div><input type="file" id="image-files" accept="image/jpeg,image/png,image/webp,image/gif,image/bmp" multiple hidden><input type="file" id="folder-files" webkitdirectory multiple hidden></div><div id="selected-files" class="selected-files">尚未选择图片</div><div class="form-error" id="import-error" role="alert"></div><div class="import-note">'+icon('folder')+'选择上级文件夹时，会将其中的子文件夹分别识别为一套作品。</div><div class="modal-footer"><button type="button" class="button" data-action="close-modal">取消</button><button class="button primary" type="submit" id="import-submit">'+icon('plus')+(target?'添加图片':'导入作品')+'</button></div></form>','import-modal');
  const zone=$('#dropzone');
  $('h3',zone).textContent='拖入 PPT、PDF 或图片';
  $('p',zone).textContent='PPT / PPTX / PDF 自动转为逐页预览 · 每批最多 100 MB';
  $('.dropzone-buttons',zone).insertAdjacentHTML('afterbegin','<button type="button" class="button primary" data-action="choose-documents">选择 PPT / PDF</button>');
  zone.insertAdjacentHTML('beforeend','<input type="file" id="document-files" accept=".ppt,.pptx,.pdf,application/pdf,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.presentationml.presentation" multiple hidden>');
  $('.modal-header p').textContent=target?'把文档页面或图片追加到「'+activeDeck().title+'」。':'每份 PPT / PDF 作为一套作品，图片也可以按文件夹导入。';
  $('.import-note').innerHTML=icon('folder')+'PPT / PDF 按原始页序导入，每份最多 600 页；文件夹中的图片仍按文件夹分套。';
  if(!target)$('#import-name').placeholder='可选：单份文件导入时自定义名称';
  $('#import-submit').textContent=target?'追加到作品':'导入作品';
  if(window.Cloud){
    $('h3',zone).textContent='拖入 PDF 或图片';
    $('p',zone).textContent='PDF 自动转为逐页预览 · PPT 请先另存为 PDF';
    $('[data-action="choose-documents"]',zone).textContent='选择 PDF';
    $('#document-files').accept='.pdf,application/pdf';
    $('.import-note').textContent='在线版支持 PDF 和图片；直接导入 PPT / PPTX 请使用本机版，再导入生成的图片。';
  }
}
const imageFile=f=>/\.(jpe?g|png|webp|gif|bmp)$/i.test(f.name);
const documentFile=f=>/\.(pptx?|pdf)$/i.test(f.name);
function chooseFiles(files) {
  if(importBusy)return;
  selectedFiles=[...files].filter(f=>imageFile(f)||documentFile(f)).sort((a,b)=>(a.webkitRelativePath||a.name).localeCompare(b.webkitRelativePath||b.name,'zh-CN',{numeric:true}));
  const size=selectedFiles.reduce((n,f)=>n+f.size,0);
  const documents=selectedFiles.filter(documentFile),images=selectedFiles.filter(imageFile);
  const groupNames=new Set(images.map(f=>fileGroup(f)));
  const labels=[documents.length?documents.length+' 份文档':'',images.length?images.length+' 张图片':''].filter(Boolean);
  $('#selected-files').innerHTML=selectedFiles.length?'<b>'+labels.join('、')+'</b><span>'+fmt(size/1024/1024)+' MB'+(importMode==='new'&&groupNames.size>1?' · '+groupNames.size+' 套作品':'')+'</span>':'没有找到支持的 PPT、PDF 或图片';
  const input=$('#import-name');
  if(input&&!input.value&&selectedFiles.length) input.value=documents.length?(documents.length===1?documents[0].name.replace(/\.[^.]+$/,''):''):(selectedFiles[0].webkitRelativePath.split('/')[0]||selectedFiles[0].name.replace(/\.[^.]+$/,''));
  if(input) {input.disabled=documents.length>1;input.title=documents.length>1?'多份文档分别使用文件名作为作品名称':'';}
  $('#import-error').textContent='';
}
function fileGroup(f) {
  const parts=(f.webkitRelativePath||'').split('/');
  if(parts.length>=3)return parts[1];
  if(parts.length===2)return parts[0];
  return '';
}
function readBase64(file) { return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('无法读取 '+file.name));reader.readAsDataURL(file);}); }
async function submitImport(e) {
  e.preventDefault();if(importBusy)return;
  const error=$('#import-error'), button=$('#import-submit'), target=$('#import-form').dataset.target;
  const documents=selectedFiles.filter(documentFile),pictures=selectedFiles.filter(imageFile);
  if(!selectedFiles.length){error.textContent='请先选择 PPT、PDF 或图片。';return;}
  if(documents.length&&pictures.length){error.textContent='请分开导入文档和图片，避免作品分组混淆。';return;}
  if(pictures.some(f=>f.size>25*1024*1024)){error.textContent='单张图片需小于 25 MB。';return;}
  if(pictures.length>600||documents.length>10||selectedFiles.reduce((n,f)=>n+f.size,0)>100*1024*1024){error.textContent='每批最多 10 份文档或 600 张图片，总大小不超过 100 MB。';return;}
  importBusy=true; button.disabled=true;button.textContent='正在读取文件…';
  $$('.dropzone button, .modal-header button, .modal-footer button').forEach(b=>b.disabled=true);
  try {
    const multipleGroups=new Set(selectedFiles.map(fileGroup)).size>1;
    const name=$('#import-name')?.value.trim()||'未命名作品';
    const images=[];
    for(let i=0;i<selectedFiles.length;i++) {const f=selectedFiles[i];images.push({name:f.name,data:await readBase64(f),group:multipleGroups?fileGroup(f):name});button.textContent='准备图片 '+(i+1)+' / '+selectedFiles.length;}
    button.textContent='正在导入…';
    let result;
    if(documents.length){
      button.textContent='正在上传文档…';
      const job=await api('/api/import-documents',{method:'POST',body:JSON.stringify({target:target||null,name:documents.length===1?name:'',files:images})});
      result=job.jobId?await waitDocumentImport(job.jobId,button):job;
    } else result=await api('/api/import',{method:'POST',body:JSON.stringify({target:target||null,name,images})});
    decks=result.decks;modal.close();render();toast('已导入 '+result.added+' 张图片'+(result.skipped?'，跳过 '+result.skipped+' 张重复图片':''));
    if(!target&&result.created.length===1)openDeck(result.created[0]);
  }catch(err){error.textContent=err.message;}finally{importBusy=false;button.disabled=false;button.textContent=target?'追加到作品':'导入作品';$$('.dropzone button, .modal-header button, .modal-footer button').forEach(b=>b.disabled=false);}
}
async function waitDocumentImport(jobId,button) {
  const message=$('#selected-files');
  for(;;){
    await new Promise(resolve=>setTimeout(resolve,750));
    const job=await api('/api/import-jobs/'+encodeURIComponent(jobId));
    if(job.status==='failed')throw new Error(job.error||'文档转换失败，请重试');
    if(job.status==='completed')return job;
    button.textContent='正在转换…';
    message.innerHTML='<b>'+esc(job.message)+'</b><span>'+(job.fileCount?job.fileIndex+' / '+job.fileCount+' 份文件':'')+'</span>';
  }
}
function download(name,content,type) {
  const url=URL.createObjectURL(new Blob([content],{type}));const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),2000);
}
function dateStamp(){return new Date().toLocaleDateString('sv-SE');}
function exportBackup() {
  const value={format:'ppt-review-backup',version:1,exportedAt:new Date().toISOString(),state:structuredClone(state),decks:decks.map(({images,...d})=>d)};
  download('观页-评分备份-'+dateStamp()+'.json',JSON.stringify(value,null,2),'application/json');toast('评分备份已导出');
}
function exportCsv() {
  const safe=v=>{let s=String(v??'');if(/^[=+\-@\t\r]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};
  const rows=[['作品名称','风格','图片数量','评分模式','页码','评分状态',...state.dimensions.map(d=>d.name),'综合均分','满分','收藏','评审笔记','更新时间']];
  for(const d of decks){
    const r=review(d.id),stats=reviewStats(d.id),perPage=Scoring.mode(r)==='page';
    rows.push([d.title,d.style,d.images.length,perPage?'逐页评分':'整套评分','汇总',statusText(d.id),...state.dimensions.map(dim=>stats.scores[dim.id]),stats.mean==null?'':fmt(stats.mean),state.maxScore,r.favorite?'是':'否',perPage?'':r.note||'',r.updatedAt||'']);
    if(perPage)d.images.forEach((im,i)=>{const entry=Scoring.page(r,im),values=state.dimensions.map(dim=>entry.scores[dim.id]),complete=values.every(hasScore),filled=values.filter(hasScore);
      rows.push([d.title,d.style,d.images.length,'逐页评分',i+1,complete?'已评分':filled.length?'评分中':'待评分',...values,filled.length?fmt(filled.reduce((a,b)=>a+b,0)/filled.length):'',state.maxScore,r.favorite?'是':'否',entry.note||'',r.updatedAt||'']);
    });
  }
  download('观页-评分表-'+dateStamp()+'.csv','\uFEFF'+rows.map(row=>row.map(safe).join(',')).join('\r\n'),'text/csv;charset=utf-8');toast('评分表已导出，可用 Excel 打开');
}
function showExport() {
  restoreFile=null;
  showModal(modalHeader('导出与备份','把你的判断留存下来，随时查看或继续评审。')+'<div class="export-options"><button class="export-option" data-action="export-csv"><span class="export-icon">'+icon('list')+'</span><div><h3>导出评分表</h3><p>CSV 格式，包含所有评分项、均分和笔记，可用 Excel 打开。</p></div>'+icon('arrow')+'</button><button class="export-option" data-action="export-json"><span class="export-icon">'+icon('download')+'</span><div><h3>备份评分记录</h3><p>JSON 格式，保存评分设置、笔记与收藏，不包含原始图片。</p></div>'+icon('arrow')+'</button></div><div class="restore-section"><h3>恢复评分备份</h3><p>'+(window.Cloud?'恢复会替换你的个人评分记录，并将备份中的评分标准应用到全团队。恢复前会自动下载当前备份。':'恢复会替换当前评分设置与记录。恢复前会自动下载一份当前备份。')+'</p><button class="button" data-action="choose-backup">'+icon('upload')+'选择备份文件</button><input type="file" id="backup-file" accept=".json,application/json" hidden><div id="restore-preview"></div><div class="form-error" id="restore-error" role="alert"></div></div><div class="modal-footer"><button class="button" data-action="close-modal">完成</button></div>','export-modal');
}
async function prepareRestore(file) {
  try {
    if(file.size>10*1024*1024)throw new Error('备份文件过大，请选择观页导出的 JSON 文件');
    const data=JSON.parse(await file.text());
    if(data.format!=='ppt-review-backup'||data.version!==1||!data.state||!Array.isArray(data.state.dimensions)||!data.state.reviews)throw new Error('这不是有效的观页评分备份');
    restoreFile=data;
    $('#restore-preview').innerHTML='<div class="restore-preview"><b>'+esc(file.name)+'</b><span>'+Object.keys(data.state.reviews).length+' 条作品记录 · '+data.state.dimensions.length+' 个评分项 · '+esc(data.state.maxScore)+' 分制</span><button class="button primary" data-action="confirm-restore">备份当前记录并恢复</button></div>';
    $('#restore-error').textContent='';
  }catch(err){restoreFile=null;$('#restore-preview').innerHTML='';$('#restore-error').textContent=err.message;}
}
async function restoreBackup() {
  if(!restoreFile)return;
  if(pending||saving) {await flushSave();if(saving||saveError){toast('请等待当前记录保存完成后重试',true);return;}}
  const restored=structuredClone(restoreFile.state);restored.revision=state.revision;
  if(window.Cloud)restored.configRevision=state.configRevision;
  try {
    exportBackup();const result=await api('/api/state',{method:'PUT',body:JSON.stringify(restored)});
    restored.revision=result.revision;if(result.configRevision!==undefined)restored.configRevision=result.configRevision;state=restored;modal.close();render();toast('评分记录已恢复');
  }catch(err){$('#restore-error').textContent=err.message;}
}
function renderLightbox() {
  const d=activeDeck();if(!d)return;
  lightbox.className=lightZoom?'zoomed':'';
  lightbox.innerHTML='<div class="lightbox-toolbar"><div><b>'+esc(d.title)+'</b><span>'+String(slide+1).padStart(2,'0')+' / '+d.images.length+'</span></div><div><button class="button light" data-action="zoom">'+(lightZoom?'适应窗口':'放大查看')+'</button><button class="icon-button" data-action="close-lightbox" aria-label="关闭大图">'+icon('close')+'</button></div></div><div class="lightbox-canvas"><img src="'+d.images[slide].url+'" alt="'+esc(d.title)+' 第 '+(slide+1)+' 张" data-action="zoom"></div><button class="lightbox-arrow lb-prev" data-action="prev-slide" aria-label="上一张" '+(slide===0?'disabled':'')+'>'+icon('left')+'</button><button class="lightbox-arrow lb-next" data-action="next-slide" aria-label="下一张" '+(slide===d.images.length-1?'disabled':'')+'>'+icon('right')+'</button>';
}
function openLightbox() { lightZoom=false;renderLightbox();lightbox.showModal(); }
async function rescan() {
  try{const result=await api('/api/rescan',{method:'POST'});decks=result.decks;sourceAvailable=result.sourceAvailable;if(activeId&&!activeDeck())activeId=null;render();toast('图片库已更新：'+decks.length+' 套作品');}catch(err){toast(err.message,true);}
}
async function nextPending() {
  if(!completed(activeId)){toast('请先完成所有评分项；也可以返回作品库稍后再评',true);return;}
  if(!await flushSave())return;
  const current=decks.findIndex(d=>d.id===activeId);
  const next=[...decks.slice(current+1),...decks.slice(0,current)].find(d=>!completed(d.id));
  if(next)openDeck(next.id);else toast('全部作品已完成评分，做得不错。');
}

document.addEventListener('click', e => {
  const button=e.target.closest('button, a[data-action]');if(!button||button.disabled)return;
  if(button.dataset.open){openDeck(button.dataset.open,Number(button.dataset.slide||0));return;}
  if(button.dataset.view){view=button.dataset.view;activeId=null;query='';styleFilter='';history.pushState(null,'',location.pathname);render();window.scrollTo(0,0);return;}
  if(button.dataset.favorite){
    const r=mutableReview(button.dataset.favorite);r.favorite=!r.favorite;queueSave();
    if(activeId)renderDetail();else updateGrid();$('.sidebar').outerHTML=sidebar();if(window.Cloud)decorateCloud();return;
  }
  if(button.dataset.layout){layout=button.dataset.layout;updateGrid();$$('[data-layout]').forEach(b=>b.classList.toggle('selected',b.dataset.layout===layout));return;}
  if(button.dataset.scoringMode){setScoringMode(button.dataset.scoringMode);return;}
  if(button.dataset.slideIndex!==undefined){changeSlide(Number(button.dataset.slideIndex));return;}
  if(button.dataset.previewSlide!==undefined){changeSlide(Number(button.dataset.previewSlide));return;}
  if(button.dataset.overview!==undefined){overview=button.dataset.overview==='true';renderDetail();return;}
  if(button.dataset.scoreDim){updateScore(button.dataset.scoreDim,Number(button.dataset.scoreValue));return;}
  if(button.classList.contains('remove-dim')){if($$('.settings-row').length<=1){toast('请至少保留一个评分项',true);return;}button.closest('.settings-row').remove();return;}
  const action=button.dataset.action;
  switch(action){
    case 'home':e.preventDefault();activeId=null;view='all';history.pushState(null,'',location.pathname);render();window.scrollTo(0,0);break;
    case 'back':backToLibrary();break;
    case 'settings':showSettings();break;
    case 'export':showExport();break;
    case 'import':showImport();break;
    case 'append':showImport(activeId);break;
    case 'close-modal':if(!importBusy)modal.close();break;
    case 'rescan':rescan();break;
    case 'retry-save':flushSave().then(ok=>{if(ok)toast('评分已保存到本地');});break;
    case 'next-page':{if(!state.dimensions.every(dim=>hasScore(currentEntry().scores[dim.id]))){toast('请先完成本页所有评分项',true);break;}if(slide<activeDeck().images.length-1)changeSlide(slide+1);else nextPending();break;}
    case 'prev-slide':changeSlide(slide-1);break;
    case 'next-slide':changeSlide(slide+1);break;
    case 'lightbox':openLightbox();break;
    case 'close-lightbox':lightbox.close();break;
    case 'zoom':lightZoom=!lightZoom;renderLightbox();break;
    case 'next-pending':nextPending();break;
    case 'add-dimension':if($$('.settings-row').length>=12){toast('最多添加 12 个评分项',true);break;}$('#settings-dimensions').insertAdjacentHTML('beforeend',settingsRow({id:'dim-'+crypto.randomUUID(),name:'',description:''}));$$('.dimension-name').at(-1).focus();break;
    case 'choose-images':$('#image-files').click();break;
    case 'choose-documents':$('#document-files').click();break;
    case 'choose-folder':$('#folder-files').click();break;
    case 'export-csv':exportCsv();break;
    case 'export-json':exportBackup();break;
    case 'choose-backup':$('#backup-file').click();break;
    case 'confirm-restore':restoreBackup();break;
    case 'reset-filter':view='all';query='';styleFilter='';render();break;
  }
});
document.addEventListener('input', e => {
  if(e.target.id==='search'){query=e.target.value;updateGrid();}
  if(e.target.id==='review-note'){const r=currentEntry(true);r.note=e.target.value;mutableReview(activeId).updatedAt=new Date().toISOString();queueSave();}
});
document.addEventListener('change', e => {
  if(e.target.id==='sort'){sort=e.target.value;updateGrid();}
  if(e.target.id==='style-filter'){styleFilter=e.target.value;updateGrid();}
  if(e.target.dataset.scoreInput){
    const value=e.target.value===''?null:Number(e.target.value);
    if(value!==null&&(!Number.isFinite(value)||value<0||value>state.maxScore)){toast('请输入 0 至 '+state.maxScore+' 之间的分数',true);e.target.value=currentEntry().scores[e.target.dataset.scoreInput]??'';return;}
    updateScore(e.target.dataset.scoreInput,value);
  }
  if(e.target.id==='image-files'||e.target.id==='folder-files'||e.target.id==='document-files')chooseFiles(e.target.files);
  if(e.target.id==='backup-file'&&e.target.files[0])prepareRestore(e.target.files[0]);
});
document.addEventListener('submit', e=>{if(e.target.id==='settings-form')applySettings(e);if(e.target.id==='import-form')submitImport(e);});
document.addEventListener('keydown', e=>{
  if(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)||modal.open)return;
  if(e.key==='/'&&!activeId){e.preventDefault();$('#search').focus();}
  if(activeId&&e.key==='ArrowLeft'){e.preventDefault();changeSlide(slide-1);}
  if(activeId&&e.key==='ArrowRight'){e.preventDefault();changeSlide(slide+1);}
  if(activeId&&e.key==='Escape'&&!lightbox.open)backToLibrary();
});
modal.addEventListener('cancel', e=>{if(importBusy)e.preventDefault();});
modal.addEventListener('click',e=>{if(e.target===modal&&!importBusy){const r=modal.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)modal.close();}});
document.addEventListener('dragover',e=>{if(e.target.closest('#dropzone')){e.preventDefault();$('#dropzone').classList.add('dragging');}});
document.addEventListener('dragleave',e=>{if(e.target.id==='dropzone')e.target.classList.remove('dragging');});
document.addEventListener('drop',e=>{if(e.target.closest('#dropzone')){e.preventDefault();$('#dropzone').classList.remove('dragging');chooseFiles(e.dataTransfer.files);}});
document.addEventListener('error', e=>{if(e.target.tagName==='IMG'&&!e.target.dataset.failed){e.target.dataset.failed='true';e.target.alt='图片暂不可用，请刷新图片库';e.target.classList.add('image-failed');}},true);
window.addEventListener('popstate',()=>{activeId=new URLSearchParams(location.hash.slice(1)).get('deck');slide=0;overview=false;render();});
lightbox.addEventListener('close',()=>{if(activeDeck())changeSlide(slide);});

async function init() {
  try {
    if(window.Cloud&&!await Cloud.ready())return;
    const library=await api('/api/library');decks=library.decks;state=library.state;source=library.source;sourceAvailable=library.sourceAvailable;
    activeId=new URLSearchParams(location.hash.slice(1)).get('deck');if(activeId&&!activeDeck())activeId=null;
    render();
  }catch(err){$('#app').innerHTML='<div class="boot boot-error">'+icon('folder')+'<h2>作品库暂时没有连接</h2><p>'+esc(err.message)+'</p><p>'+(window.Cloud?'请检查网络连接，或联系管理员确认团队配置。':'请启动项目中的“启动评分系统.cmd”，然后刷新此页面。')+'</p><button class="button" onclick="location.reload()">重新连接</button></div>';}
}
init();
