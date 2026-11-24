'use strict';
(function(global){
  const AI_PATH='/api/ai/reviews';
  const IMPORT_PATH='/api/ai/reviews/import';
  const PRE_EVALUATION_LABELS=Object.freeze({queued:'已排队',running:'预评中',retry_scheduled:'等待重试',completed:'整套预评完成',failed:'预评失败',not_queued:'尚未开始'});
  const PRE_EVALUATION_ACTIVE=new Set(['queued','running','retry_scheduled']);
  const PRE_EVALUATION_POLL_MS=3000;
  const PRE_EVALUATION_POLL_MAX_MS=30000;
  const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const MAX_IMPORT_FILES=10;
  const MAX_IMPORT_FILE_BYTES=25*1024*1024;
  const MAX_IMPORT_TOTAL_BYTES=100*1024*1024;
  const AI_VARIANT_LABELS=Object.freeze({assisted:'设计规则与样本辅助',baseline:'基线预评'});
  const AI_VARIANT_ORDER=Object.freeze(['assisted','baseline']);
  const SAFE_UPLOAD_ERROR_PREFIXES=Object.freeze(['请选择 1–10 份 PPT/PPTX','此入口仅接收 PPT 或 PPTX','文件内容为空','单个文件不得超过 25 MB','单批文件总大小不得超过 100 MB','每套作品需包含 1–600 页','转换未生成页面']);
  let supaClient=null;
  function config(){return global.CLOUD_CONFIG||{};}
  function client(){
    const cfg=config();
    if(!supaClient&&cfg.url&&cfg.anonKey&&global.supabase?.createClient)supaClient=global.supabase.createClient(cfg.url,cfg.anonKey);
    return supaClient;
  }
  async function token(){
    try{const result=await client()?.auth.getSession();return result?.data?.session?.access_token||'';}catch(_){return '';}
  }
  async function httpRequest(path,options={}){
    if(typeof path!=='string'||!path.startsWith('/'))throw new Error('AI 请求路径无效');
    const cfg=config(),base=String(global.GUANYE_AI_BASE||cfg.aiBase||'').replace(/\/$/,'');
    const headers={'Content-Type':'application/json'};
    if(cfg.anonKey)headers.apikey=cfg.anonKey;
    if(cfg.workspaceId)headers['x-workspace-id']=cfg.workspaceId;
    const access=await token();if(access)headers.Authorization='Bearer '+access;
    const response=await global.fetch(base+path,{method:options.method||'GET',headers,body:options.body===undefined?undefined:JSON.stringify(options.body)});
    const type=response.headers?.get?.('content-type')||'';
    const payload=type.includes('application/json')?await response.json():await response.text();
    if(!response.ok){
      const detail=payload?.error||payload?.message||payload||('AI 请求失败 ('+response.status+')');
      const error=new Error(typeof detail==='string'?detail:JSON.stringify(detail));
      error.status=Number(response.status)||0;
      throw error;
    }
    return payload;
  }
  global.GuanyeAI=global.GuanyeAICore||Object.freeze({request:httpRequest});
  const style=`
  .ga-shell{display:grid;gap:18px;color:#1c2524;font-family:Inter,"Noto Sans SC",system-ui,sans-serif}
  .ga-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
  .ga-head h2{margin:0;font-size:24px;letter-spacing:-.03em}.ga-muted{color:#65706d;font-size:13px}
  .ga-actions{display:flex;gap:8px;flex-wrap:wrap}.ga-btn{border:1px solid #cbd5d1;background:#fff;border-radius:999px;padding:8px 14px;cursor:pointer;font:inherit}
  .ga-btn.primary{background:#173d35;color:#fff;border-color:#173d35}.ga-btn.danger{color:#9f2d2d}.ga-btn:disabled{opacity:.5;cursor:wait}
  .ga-band{border:1px solid #dce4e1;background:#f8faf9;border-radius:18px;padding:16px}.ga-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
  .ga-card{border:1px solid #dce4e1;border-radius:16px;padding:14px;background:#fff}.ga-card.selected{border-color:#3f7063;box-shadow:0 0 0 3px #dcece7}
  .ga-card h3{margin:0 0 8px;font-size:16px}.ga-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.ga-scores{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-top:10px}
  .ga-field{display:grid;gap:4px;font-size:12px;color:#53615d}.ga-field input,.ga-field select,.ga-field textarea{border:1px solid #cbd5d1;border-radius:9px;padding:7px 9px;font:inherit;color:inherit;background:#fff;width:100%;box-sizing:border-box}
  .ga-score-field>label{font-size:12px;color:#53615d}.ga-score-echo{min-height:1.2em;color:#65706d;font-size:11px;font-variant-numeric:tabular-nums}
  .ga-field textarea{min-height:62px;resize:vertical}.ga-pill{display:inline-flex;align-items:center;gap:6px;border:1px solid #d5dfdc;border-radius:999px;padding:5px 9px;font-size:12px;background:#fff}
  .ga-formal{margin-top:10px;border:1px solid #b9cec7;border-radius:12px;padding:12px;background:#fff}
  .ga-formal-head{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:10px}
  dialog.ga-professional-modal{width:min(1100px,calc(100vw - 32px))}dialog.ga-professional-modal [data-professional-panel]{padding:0 clamp(12px,3vw,24px) 24px}dialog.ga-professional-modal [data-professional-panel]:empty{padding:0}
  .ga-admission{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:10px;margin-top:12px;border:1px solid #d5dfdc;border-radius:12px;padding:10px 12px;background:#f8faf9}.ga-admission[data-admission-status="ready"],.ga-admission[data-admission-status="eligible"]{border-color:#8eb6a8;background:#f1f8f5}.ga-admission[data-admission-status="low"],.ga-admission[data-admission-status="expired"],.ga-admission[data-admission-status="invalid"],.ga-admission[data-admission-status="pending"]{border-color:#e2d1a7;background:#fffaf0}.ga-admission[data-admission-status="admitted"]{border-color:#b9cec7;background:#f1f7f5}.ga-admission p{margin:4px 0 0;color:#65706d;font-size:12px;line-height:1.55;overflow-wrap:anywhere}.ga-admission-action{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:8px}
  .ga-ai{margin-top:10px;border-left:3px solid #4a7f70;padding:9px 11px;background:#f1f7f5;border-radius:0 10px 10px 0;font-size:13px}
  .ga-ai-head{display:grid;gap:2px}.ga-ai-hint{margin:0;color:#65706d;font-size:12px}
  .ga-ai-group{margin-top:10px;padding-top:10px;border-top:1px solid #d7e5e0}.ga-ai-group:first-of-type{margin-top:8px}
  .ga-ai-group-label{margin:0 0 7px}
  .ga-ai-summary{display:grid;gap:7px;margin:0}.ga-ai-summary-row{display:grid;grid-template-columns:minmax(78px,28%) minmax(0,1fr);gap:10px;align-items:start}
  .ga-ai-summary-row dt{color:#53615d;font-size:12px;font-weight:600;line-height:1.5}.ga-ai-summary-row dd{min-width:0;margin:0;line-height:1.5;overflow-wrap:anywhere}
  .ga-ai-reasons{margin:0;padding-left:18px}.ga-ai-reasons li+li{margin-top:3px}
  .ga-warn{background:#fff7e8;border:1px solid #f0d7a4;color:#76551b;border-radius:12px;padding:10px 12px;font-size:13px}.ga-error{color:#a62d2d}.ga-ok{color:#24624f}.ga-sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.ga-review-status:focus-visible{outline:3px solid #f1c76a;outline-offset:2px}
  .ga-table{width:100%;border-collapse:collapse;font-size:13px}.ga-table th,.ga-table td{padding:8px;border-bottom:1px solid #e5ebe9;text-align:left;vertical-align:top}.ga-table pre{margin:0;white-space:pre-wrap}
  .ga-stats-status{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px;border:1px solid #d5dfdc;border-radius:12px;padding:9px 11px;font-size:13px;background:#fff}.ga-stats-status.ga-error{border-color:#e2b7b7;background:#fff7f7}.ga-stats-status[data-stats-loading]{border-color:#8eb6a8;background:#f1f8f5}
  .ga-stats-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}.ga-stats-pairs b{font-variant-numeric:tabular-nums}.ga-stats-note{margin:10px 0 0;color:#53615d;font-size:12px;line-height:1.55;overflow-wrap:anywhere}
  .ga-stats-scroll{max-width:100%;overflow-x:auto}.ga-stats-table{margin-top:10px;table-layout:fixed}.ga-stats-table th,.ga-stats-table td{padding:8px 6px;overflow-wrap:anywhere}.ga-stats-table th:first-child{width:30%}.ga-stats-table caption{caption-side:top;text-align:left;color:#53615d;font-size:12px;padding:0 0 6px}.ga-stats-number{white-space:nowrap;font-variant-numeric:tabular-nums}.ga-stats-improvement{margin:10px 0 0;color:#53615d;font-size:13px;line-height:1.55}.ga-stats-zero{margin-top:10px;border:1px dashed #b9cec7;border-radius:12px;padding:12px;background:#fff}.ga-stats-zero p{margin:4px 0 0;color:#65706d;font-size:13px;line-height:1.55}
  .ga-reasons{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}.ga-check{display:inline-flex;align-items:center;gap:5px;font-size:12px}
  .ga-preview{min-height:120px;max-height:240px;border-radius:12px;background:#eef3f1;display:flex;align-items:center;justify-content:center;overflow:hidden;margin:10px 0}.ga-preview img{width:100%;height:100%;max-height:240px;object-fit:contain;background:#fff}.ga-quick{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}.ga-quick .ga-pill{display:inline-flex;align-items:center;justify-content:center;min-inline-size:28px;min-block-size:28px;padding:2px 6px;font-size:11px;cursor:pointer;box-sizing:border-box}
  .ga-pre-evaluation{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}.ga-pre-evaluation-label{font-size:13px;font-weight:600}.ga-pre-evaluation-status{font-weight:600}
  .ga-upload-panel[aria-busy="true"]{border-color:#8eb6a8;background:#f1f8f5;box-shadow:0 0 0 3px #dcece7}
  .ga-upload-files{display:grid;gap:3px;margin-top:8px;font-size:12px;color:#53615d}.ga-upload-files b{color:#1c2524;font-size:13px}
  .ga-upload-status{min-height:20px;margin-top:8px;font-size:13px}.ga-upload-status.ga-error{font-weight:600}
  .ga-backfill-status{margin-top:10px;border:1px solid #d5dfdc;border-radius:12px;padding:10px 12px;font-size:13px;background:#fff}.ga-backfill-status[data-status="fetching"]{border-color:#8eb6a8;background:#f1f8f5}.ga-backfill-status[data-status="complete"]{border-color:#b9cec7;background:#f1f7f5}.ga-backfill-status[data-status="failed"]{border-color:#e2b7b7;background:#fff7f7}
  .ga-preview-shell{display:grid;gap:8px;min-width:0}.ga-preview-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}.ga-preview-controls .ga-btn,.ga-preview-error .ga-btn,.ga-lightbox button{min-height:44px;min-width:44px;touch-action:manipulation}.ga-preview-status{font-size:12px;color:#53615d;font-variant-numeric:tabular-nums;white-space:nowrap;padding:0 2px}.ga-preview-error{display:grid;justify-items:center;gap:8px;padding:10px;text-align:center}
  .ga-lightbox{position:fixed;inset:0;margin:auto;width:min(96vw,1200px);max-width:96vw;max-height:92vh;padding:0;border:0;border-radius:16px;background:#101715;color:#fff;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.45);box-sizing:border-box}.ga-lightbox::backdrop{background:rgba(6,12,10,.82)}.ga-lightbox[open]{display:grid;grid-template-rows:auto minmax(0,1fr)}.ga-lightbox-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 12px;background:#18231f;border-bottom:1px solid #31443d}.ga-lightbox-title{min-width:0;font-size:14px;overflow-wrap:anywhere}.ga-lightbox-close{border:1px solid #60736c;background:#203029;color:#fff;border-radius:999px;padding:8px 12px;font:inherit;cursor:pointer}.ga-lightbox-canvas{display:flex;align-items:center;justify-content:center;min-height:0;height:100%;padding:12px;box-sizing:border-box;overflow:auto;background:#0b100e}.ga-lightbox-canvas img{display:block;max-width:100%;max-height:calc(92vh - 72px);width:auto;height:auto;object-fit:contain;margin:auto}.ga-lightbox-close:focus-visible,.ga-preview-controls .ga-btn:focus-visible,.ga-preview-error .ga-btn:focus-visible{outline:3px solid #f1c76a;outline-offset:2px}
  @media(max-width:700px){.ga-head h2{font-size:20px}.ga-band{padding:12px}.ga-scores{grid-template-columns:1fr 1fr}.ga-actions .ga-btn{flex:1 1 auto}.ga-ai-summary-row{grid-template-columns:1fr;gap:2px}.ga-stats-table th,.ga-stats-table td{padding:7px 4px;font-size:12px}.ga-admission{grid-template-columns:1fr}.ga-admission-action{justify-content:flex-start}.ga-preview-status{order:3;flex:1 0 100%;text-align:center}.ga-lightbox{width:96vw;max-height:94vh}}
  `;
  function injectStyle(){
    if(global.document?.getElementById('guanye-ai-review-style'))return;
    const node=global.document.createElement('style');node.id='guanye-ai-review-style';node.textContent=style;(global.document.head||global.document.documentElement).appendChild(node);
  }
  function mount(root,opts={}){
    if(!root)throw new Error('缺少评审挂载节点');
    injectStyle();
    const state={opts:{kind:opts.kind==='generated'?'generated':'uploaded',deckId:opts.deckId||'',singleJudge:opts.singleJudge===true||opts.singleJudge==='true'||opts.singleJudge===1,professionalReviewOnly:opts.professionalReviewOnly===true||opts.professionalReviewOnly==='true'||opts.professionalReviewOnly===1},scope:'deck',selected:new Set(),selectionDeckKey:'',drafts:{},draftRevisions:{},data:null,busy:false,message:'',error:'',formalDrafts:{},formalDirty:{},formalRevisions:{},preEvaluationStatus:undefined,preEvaluationFeedback:'',runBusy:false,backfillByKind:{},uploadFiles:[],uploadName:'',uploadBusy:false,uploadPhase:'',uploadError:'',uploadMessage:'',previewIndex:0};
    const comparisonOnly=opts.comparisonOnly===true;
    const localProfessional=config().singleJudge===true&&config().localReview===true;
    const request=localProfessional
      ?async (path,options)=>{
        if(typeof global.GuanyeAICore?.request!=='function')throw new Error('专业评审客户端未就绪');
        return global.GuanyeAICore.request(path,options);
      }
      :httpRequest;
    const professionalComparison=()=>comparisonOnly||professionalReviewOnlyMode();
    const admissionPending=new Set(),admissionReceipts=new Set();
    const liveRegion=global.document.createElement('div'),view=global.document.createElement('div');
    liveRegion.id='ga-review-live';liveRegion.className='ga-sr-only';liveRegion.dataset.reviewLive='';
    liveRegion.setAttribute('role','status');liveRegion.setAttribute('aria-live','polite');liveRegion.setAttribute('aria-atomic','true');
    root.replaceChildren(liveRegion,view);
    let destroyed=false;
    let workspaceEpoch=0;
    let queueEpoch=0;
    let importEpoch=0;
    let preEvaluationEpoch=0;
    let preEvaluationTimer=null;
    let preEvaluationDelay=PRE_EVALUATION_POLL_MS;
    let preEvaluationHintShown=false;
    let deckPreviewEpoch=0;
    let previewDialog=null;
    let previewDialogTrigger=null;
    let previewObserver=null;
    let previewHydrationEpoch=0;
    const PREVIEW_ROOT_MARGIN='320px 0px';
    function normalizePreEvaluationStatus(value){
      const candidate=typeof value==='string'?value.trim():'';
      return Object.prototype.hasOwnProperty.call(PRE_EVALUATION_LABELS,candidate)?candidate:null;
    }
    function preEvaluationText(){
      if(state.opts.kind==='generated'&&global.Cloud?.coverTrainingSite===true){
        if(Array.isArray(state.data?.ai)&&state.data.ai.length)return 'Sol 已完成独立评审';
        if(Array.isArray(state.data?.submissions)&&state.data.submissions.length)return '人工分已保存，等待 Sol';
        return '人工分后由 Sol 评审';
      }
      return PRE_EVALUATION_LABELS[state.preEvaluationStatus]||'状态待确认';
    }
    function isPreEvaluationActive(value=state.preEvaluationStatus){
      return PRE_EVALUATION_ACTIVE.has(value);
    }
    function currentDeckKey(){
      return String(state.opts.kind||'')+':'+String(state.opts.deckId||'');
    }
    function singleJudgeMode(){
      return state.opts.singleJudge===true||state.data?.singleJudge===true;
    }
    function coverTrainingOnePage(){
      return global.Cloud?.coverTrainingSite===true&&state.opts.kind==='generated'&&images().length===1;
    }
    function professionalReviewOnlyMode(){
      return singleJudgeMode()&&(state.opts.professionalReviewOnly===true||state.data?.professionalReviewOnly===true);
    }
    function professionalActionUnavailable(action){
      return professionalReviewOnlyMode()&&['prepare','run','settings','import','formal'].includes(String(action||''));
    }
    function ensureSelectionDeck(){
      const deckKey=currentDeckKey();
      if(state.selectionDeckKey!==deckKey){
        state.selectionDeckKey=deckKey;
        state.selected.clear();
      }
      return deckKey;
    }
    function announceReviewStatus(message){
      const text=String(message||'').trim();
      if(!text||destroyed)return;
      liveRegion.textContent='';
      queueMicrotask(()=>{if(!destroyed)liveRegion.textContent='人工提交：'+text;});
    }
    function focusScopeControl(scope){
      const button=[...root.querySelectorAll('[data-scope]')].find(node=>node.dataset.scope===scope);
      try{button?.focus();}catch(_){}
    }
    function focusReviewStatus(){
      const statusNode=root.querySelector('[data-review-status]');
      try{statusNode?.focus();}catch(_){}
    }
    function focusSubmitControl(){
      const button=root.querySelector('[data-action="submit"]');
      try{button?.focus();}catch(_){}
    }
    const FOCUS_FIELDS=Object.freeze([
      {attr:'data-select-unit',dataset:'selectUnit'},
      {attr:'data-score-unit',dataset:'scoreUnit',usesDim:true},
      {attr:'data-note-unit',dataset:'noteUnit'},
      {attr:'data-suggest-unit',dataset:'suggestUnit'},
      {attr:'data-correct-unit',dataset:'correctUnit'},
      {attr:'data-other-unit',dataset:'otherUnit'},
      {attr:'data-reason-unit',dataset:'reasonUnit',usesValue:true},
      {attr:'data-quick-unit',dataset:'quickUnit',usesDim:true,usesQuickValue:true},
      {attr:'data-formal-score',dataset:'formalScore',usesDim:true},
      {attr:'data-formal-classification',dataset:'formalClassification'},
      {attr:'data-formal-reason',dataset:'formalReason',usesValue:true},
      {attr:'data-formal-other',dataset:'formalOther'},
    ]);
    function focusDescriptor(node){
      if(!node||!view.contains?.(node))return null;
      const scopeNode=node.closest?.('[data-scope]');
      if(scopeNode)return {kind:'scope',scope:String(scopeNode.dataset.scope||'')};
      const field=FOCUS_FIELDS.map(item=>({item,node:node.closest?.('['+item.attr+']')})).find(match=>match.node);
      if(field){
        const {item,node:match}=field,card=match.closest?.('[data-unit-card]');
        return {kind:'field',attr:item.attr,dataset:item.dataset,value:String(match.dataset?.[item.dataset]??''),unitId:String(card?.dataset?.unitCard??''),dim:item.usesDim?String(match.dataset?.dim??''):'',choice:item.usesValue?String(match.value??''):item.usesQuickValue?String(match.dataset?.quickValue??''):''};
      }
      const actionNode=node.closest?.('[data-action]');
      if(actionNode){
        const card=actionNode.closest?.('[data-unit-card]');
        return {kind:'action',action:String(actionNode.dataset.action||''),unitId:String(card?.dataset?.unitCard??'')};
      }
      return null;
    }
    function restoreFocusDescriptor(descriptor){
      if(!descriptor)return false;
      let target=null;
      if(descriptor.kind==='scope'){
        target=[...root.querySelectorAll('[data-scope]')].find(node=>String(node.dataset.scope||'')===descriptor.scope)||null;
      }else if(descriptor.kind==='field'){
        const field=FOCUS_FIELDS.find(item=>item.attr===descriptor.attr);
        if(field){
          target=[...root.querySelectorAll('['+descriptor.attr+']')].find(node=>{
            const card=node.closest?.('[data-unit-card]');
            return String(node.dataset?.[field.dataset]??'')===descriptor.value
              && (!descriptor.unitId||String(card?.dataset?.unitCard??'')===descriptor.unitId)
              && (!field.usesDim||String(node.dataset?.dim??'')===descriptor.dim)
              && (!field.usesValue||String(node.value??'')===descriptor.choice)
              && (!field.usesQuickValue||String(node.dataset?.quickValue??'')===descriptor.choice);
          })||null;
        }
      }else if(descriptor.kind==='action'){
        target=[...root.querySelectorAll('[data-action]')].find(node=>{
          const card=node.closest?.('[data-unit-card]');
          return String(node.dataset.action||'')===descriptor.action&&(!descriptor.unitId||String(card?.dataset?.unitCard??'')===descriptor.unitId);
        })||null;
      }
      if(!target||target.disabled)return false;
      try{target.focus();return global.document?.activeElement===target;}catch(_){return false;}
    }
    const humanSubmitPending=new Map();
    let humanSubmitSequence=0;
    function pendingHumanSubmit(workKey=currentDeckKey()){
      return humanSubmitPending.get(workKey)||null;
    }
    function humanSubmitButtonText(pending){
      return pending?.phase==='reloading'?'提交完成，刷新中…':'提交中…';
    }
    function clearPendingHumanSubmit(workKey,callId){
      const pending=humanSubmitPending.get(workKey);
      if(!pending||pending.callId!==callId)return false;
      humanSubmitPending.delete(workKey);
      return true;
    }
    function updateSubmissionDom(){
      if(typeof root.querySelector!=='function')return;
      const pending=pendingHumanSubmit(),button=root.querySelector('[data-action="submit"]');
      if(button){
        const enabled=Boolean(state.data?.deck&&!state.busy&&!pending&&submissionPlan().ready),label=humanSubmitLabel(pending);
        button.disabled=!enabled;
        button.textContent=label;
        button.dataset.submitIntent=professionalReviewOnlyMode()&&professionalBundleHasSavedFormal()?'revision':'initial';
        if(pending)button.setAttribute?.('aria-busy','true');
        else button.removeAttribute?.('aria-busy');
      }
      const prepare=root.querySelector('[data-action="prepare"]');
      if(prepare)prepare.disabled=!Boolean(state.data?.deck&&!state.busy&&selectedUnits().length);
      const feedback=root.querySelector('[data-review-status]');
      if(feedback)feedback.innerHTML=status();
      if(professionalReviewOnlyMode()&&state.opts.kind==='generated'){
        for(const host of root.querySelectorAll('[data-professional-admission]')){
          const html=professionalAdmissionHtml(host.dataset.professionalAdmission);
          if(host.dataset.admissionView!==html){host.dataset.admissionView=html;host.innerHTML=html;}
        }
      }
    }
    function newQueueTrack(){
      return {items:[],headIds:[],nextCursor:null,cursorSupported:false,loadedMore:false,loading:false,error:''};
    }
    const queueByKind={uploaded:newQueueTrack(),generated:newQueueTrack()};
    const lastDeckByKind={uploaded:'',generated:''};
    function queueTrack(kind){
      return queueByKind[kind==='generated'?'generated':'uploaded'];
    }
    function normalizeQueueCursor(value){
      return typeof value==='string'?value:null;
    }
    function normalizeQueueItems(kind,items){
      const seen=new Set(),result=[];
      for(const raw of Array.isArray(items)?items:[]){
        if(!raw||typeof raw!=='object')continue;
        const id=String(raw.id??'');
        if(!id||seen.has(id))continue;
        seen.add(id);
        const sourceKind=String(raw.sourceKind||kind)==='generated'?'generated':'uploaded';
        result.push({id,title:String(raw.title||raw.name||id),name:String(raw.name||raw.title||id),sourceKind});
      }
      return result;
    }
    function mergeQueueHead(previous,incoming){
      const result=[],seen=new Set();
      for(const item of [...(incoming||[]),...(previous||[])]){
        if(!item||seen.has(item.id))continue;
        seen.add(item.id);result.push(item);
      }
      return result;
    }
    function appendQueueItems(previous,incoming){
      const result=[...(previous||[])],positions=new Map(result.map((item,index)=>[item.id,index]));
      for(const item of incoming||[]){
        if(!item||!item.id)continue;
        const index=positions.get(item.id);
        if(index===undefined){positions.set(item.id,result.length);result.push(item);}
        else result[index]=item;
      }
      return result;
    }
    function applyWorkspaceQueues(data,{reset=false}={}){
      const queues=data?.queues&&typeof data.queues==='object'?data.queues:{};
      const cursors=data?.queueCursors;
      const hasCursorContract=!!cursors&&typeof cursors==='object'&&!Array.isArray(cursors);
      for(const kind of ['uploaded','generated']){
        const track=queueTrack(kind);
        const incoming=normalizeQueueItems(kind,queues[kind]);
        const previousHead=new Set(track.headIds);
        const incomingIds=new Set(incoming.map(item=>item.id));
        if(reset){
          track.items=[...incoming];
          track.loadedMore=false;
        }else{
          track.items=track.items.filter(item=>!previousHead.has(item.id)||incomingIds.has(item.id));
          track.items=mergeQueueHead(track.items,incoming);
        }
        track.headIds=[...incomingIds];
        track.error='';
        track.loading=false;
        if(!hasCursorContract){
          track.nextCursor=null;
          track.cursorSupported=false;
          track.loadedMore=false;
        }else{
          track.cursorSupported=true;
          if(reset||!track.loadedMore)track.nextCursor=normalizeQueueCursor(cursors[kind]);
        }
      }
    }
    function queueItemsWithCurrent(kind){
      const items=[...queueTrack(kind).items];
      const deck=kind===state.opts.kind?state.data?.deck:null;
      if(deck?.id&&!items.some(item=>item.id===String(deck.id))){
        const data=deck.data||{},id=String(deck.id);
        items.unshift({id,title:String(data.title||data.name||id),name:String(data.name||data.title||id),sourceKind:kind});
      }
      return items;
    }
    function clearPreEvaluationTimer(){
      if(preEvaluationTimer!==null&&typeof global.clearTimeout==='function'){
        try{global.clearTimeout(preEvaluationTimer);}catch(_){}
      }
      preEvaluationTimer=null;
    }
    function resetPreEvaluationPolling(){
      preEvaluationEpoch++;
      clearPreEvaluationTimer();
      preEvaluationDelay=PRE_EVALUATION_POLL_MS;
      preEvaluationHintShown=false;
    }
    function backfillProgress(kind=state.opts.kind){
      if(!state.backfillByKind[kind])state.backfillByKind[kind]={offset:0,totalDecks:null,scannedDecks:0,preparedUnits:0,fetching:false,complete:false,error:''};
      return state.backfillByKind[kind];
    }
    function backfillButtonText(progress){
      if(progress.fetching)return '补档中…';
      if(progress.complete)return '重新扫描';
      return progress.offset>0?'继续补档':'老库补档';
    }
    function backfillStatusHtml(){
      const progress=backfillProgress();
      const track=state.opts.kind==='generated'?'生成队列':'上传队列';
      let status='idle';
      let body='仅创建预评任务：按'+track+'自身进度分批扫描旧库，不生成内容，也不改写已有评分。';
      if(progress.fetching){
        status='fetching';
        body='正在扫描'+track+'本批，返回前不会重复发起同一批。';
      }else if(progress.error){
        status='failed';
        body='补档失败：'+esc(progress.error)+'；本批未推进，可重试'+(progress.offset>0?'第 '+progress.offset+' 套起的批次':'当前批')+'。';
      }else if(progress.complete){
        status='complete';
        body='补档扫描完成：本轨共检查 '+esc(progress.scannedDecks)+' 套，累计准备 '+esc(progress.preparedUnits)+' 个预评任务；未生成内容，也未改写已有评分。如需再次检查，请点“重新扫描”。';
      }else if(progress.offset>0||progress.totalDecks!==null){
        status='partial';
        const total=progress.totalDecks===null?'':(' / '+progress.totalDecks);
        body='本轨已扫描至第 '+esc(progress.offset)+' 套'+esc(total)+'，累计准备 '+esc(progress.preparedUnits)+' 个预评任务；需要时继续点击“继续补档”。';
      }
      return '<div class="ga-backfill-status" data-backfill-status data-kind="'+esc(state.opts.kind)+'" data-status="'+esc(status)+'" role="status" aria-live="polite" aria-atomic="true"><b>老库补档进度</b><div>'+body+'</div></div>';
    }
    const formatMiB=bytes=>(Number(bytes||0)/(1024*1024)).toFixed(1);
    function fileNameSummary(names){
      const values=[...new Set((names||[]).map(name=>String(name||'')).filter(Boolean))];
      return values.slice(0,3).join('、')+(values.length>3?' 等 '+values.length+' 个文件':'');
    }
    function uploadValidationMessage(files){
      if(!files.length)return '请选择 1–10 份 PPT/PPTX';
      if(files.length>MAX_IMPORT_FILES)return '每批最多 10 份 PPT/PPTX，当前选择了 '+files.length+' 份';
      const unsupported=files.filter(file=>!['ppt','pptx'].includes(String(file.name||'').split('.').pop().toLowerCase()));
      if(unsupported.length)return '仅支持 PPT/PPTX：'+fileNameSummary(unsupported.map(file=>file.name));
      const empty=files.filter(file=>!(Number(file.size)>0));
      if(empty.length)return '文件内容为空：'+fileNameSummary(empty.map(file=>file.name));
      const oversized=files.filter(file=>Number(file.size)>MAX_IMPORT_FILE_BYTES);
      if(oversized.length)return '单个文件不得超过 25 MiB：'+fileNameSummary(oversized.map(file=>file.name+'（'+formatMiB(file.size)+' MiB）'));
      const total=files.reduce((sum,file)=>sum+Math.max(0,Number(file.size)||0),0);
      if(total>MAX_IMPORT_TOTAL_BYTES)return '本批文件共 '+formatMiB(total)+' MiB，合计不得超过 100 MiB';
      return '';
    }
    function uploadSummaryHtml(){
      if(!state.uploadFiles.length)return '<span class="ga-muted">尚未选择文件 · 1–10 份 · 单份 ≤ 25 MiB · 合计 ≤ 100 MiB</span>';
      const total=state.uploadFiles.reduce((sum,file)=>sum+Math.max(0,Number(file.size)||0),0);
      return '<b>'+state.uploadFiles.length+' 份 · '+formatMiB(total)+' MiB</b><span>'+esc(fileNameSummary(state.uploadFiles.map(file=>file.name)))+'</span>';
    }
    function uploadButtonText(){
      return state.uploadBusy?(state.uploadPhase||'正在上传…'):'转换并加入 AI 评审';
    }
    function updateUploadDom(){
      const summary=typeof root.querySelector==='function'?root.querySelector('[data-import-summary]'):null;
      if(summary)summary.innerHTML=uploadSummaryHtml();
      const status=typeof root.querySelector==='function'?root.querySelector('[data-import-status]'):null;
      if(status){
        const text=state.uploadBusy?(state.uploadPhase||'正在上传…'):(state.uploadError||state.uploadMessage||'文件仅在提交时读取；失败会保留选择，可直接重试。');
        status.className='ga-upload-status '+(state.uploadBusy?'ga-muted':state.uploadError?'ga-error':'ga-muted');
        status.textContent=text;
      }
      const button=typeof root.querySelector==='function'?root.querySelector('[data-action="import"]'):null;
      if(button){
        button.disabled=state.uploadBusy;
        button.textContent=uploadButtonText();
        if(state.uploadBusy)button.setAttribute?.('aria-busy','true');else button.removeAttribute?.('aria-busy');
      }
      const input=typeof root.querySelector==='function'?root.querySelector('[data-import-files]'):null;
      if(input){
        input.disabled=state.uploadBusy;
        if(!state.uploadFiles.length&&!state.uploadBusy){try{input.value='';}catch(_){}}
      }
      const panel=typeof root.querySelector==='function'?root.querySelector('[data-import-panel]'):null;
      panel?.setAttribute?.('aria-busy',state.uploadBusy?'true':'false');
    }
    function syncUploadInput(restoreFiles=false){
      const input=typeof root.querySelector==='function'?root.querySelector('[data-import-files]'):null;
      if(restoreFiles&&input&&state.uploadFiles.length){
        try{
          if(typeof global.DataTransfer!=='function')throw new Error('DataTransfer unavailable');
          const transfer=new global.DataTransfer();
          state.uploadFiles.forEach(file=>transfer.items.add(file));
          input.files=transfer.files;
        }catch(_){}
      }
      updateUploadDom();
    }
    function friendlyUploadError(error){
      if(error?.uploadValidation)return error.message;
      const message=String(error?.message||'').trim();
      if(message.startsWith('文件 data 不是有效 base64'))return '文件读取结果无效，请重新选择后重试';
      if(SAFE_UPLOAD_ERROR_PREFIXES.some(prefix=>message.startsWith(prefix)))return message;
      const status=Number(error?.status||0);
      if(status===401||status===403)return '登录状态已失效，请刷新页面后重试';
      if(status===413)return '文件总大小超过服务端限制，请减少文件后重试';
      if(status===429)return '上传请求过于频繁，请稍后重试';
      if(status>=500)return '上传服务暂时不可用，请稍后重试';
      return '上传失败，请检查网络和文件后重试';
    }
    function uploadValidationError(message){
      const error=new Error(message);
      error.uploadValidation=true;
      return error;
    }
    function readUploadFile(file){
      return new Promise((resolve,reject)=>{
        if(typeof global.FileReader!=='function')return reject(uploadValidationError('当前浏览器无法读取文件，请更换浏览器后重试'));
        const reader=new global.FileReader();
        reader.onload=()=>{
          const value=String(reader.result||'');
          const comma=value.indexOf(',');
          const data=comma>=0?value.slice(comma+1):'';
          if(!data)return reject(uploadValidationError('无法读取文件：'+file.name));
          resolve(data);
        };
        reader.onerror=()=>reject(uploadValidationError('无法读取文件：'+file.name));
        reader.readAsDataURL(file);
      });
    }
    function updatePreEvaluationDom(){
      const node=typeof root.querySelector==='function'?root.querySelector('[data-pre-evaluation-status]'):null;
      if(!node)return;
      const nextText=preEvaluationText()+(state.preEvaluationFeedback?' · '+state.preEvaluationFeedback:'');
      const nextStatus=state.preEvaluationStatus||'unknown';
      if(node.textContent!==nextText)node.textContent=nextText;
      const currentStatus=typeof node.getAttribute==='function'?node.getAttribute('data-status'):node.dataset?.status;
      if(currentStatus!==nextStatus)node.setAttribute?.('data-status',nextStatus);
    }
    function updateRunButtonBusy(){
      const button=typeof root.querySelector==='function'?root.querySelector('[data-action="run"]'):null;
      if(!button)return;
      if(state.runBusy){
        button.disabled=true;
        button.textContent='推进中…';
        button.setAttribute?.('aria-busy','true');
      }else{
        button.disabled=false;
        button.textContent=button.dataset?.idleLabel||'推进 1 个';
        button.removeAttribute?.('aria-busy');
      }
    }
    const dimensions=()=>{
      if(state.opts.kind==='generated')return [{id:'visual',name:'视觉'},{id:'layout',name:'排版'},{id:'match',name:'匹配'}];
      const values=state.data?.workspaceSettings?.dimensions||[];
      return values.length?values:[{id:'visual',name:'视觉效果'},{id:'layout',name:'排版'}];
    };
    const maxScore=()=>Number(state.data?.workspaceSettings?.maxScore||5);
    const SCORE_STEP='0.01';
    function scoreValueText(value){
      if(value===null||value===undefined||String(value).trim()==='')return '';
      const number=Number(value);
      return Number.isFinite(number)?String(Number(number.toFixed(2))):'';
    }
    function scoreFieldId(prefix,unitId,dimId){
      const token=value=>encodeURIComponent(String(value??'')).replace(/%/g,'_');
      return ['ga',prefix,state.scope,token(unitId||'deck'),token(dimId)].join('-');
    }
    function scoreEchoText(value){
      const actual=scoreValueText(value),maximum=scoreValueText(maxScore());
      return actual?('当前 '+actual+' 分 / 满分 '+maximum+' 分'):('当前未评分，满分 '+maximum+' 分，可输入两位小数');
    }
    function scoreInputFor(unitId,dimId){
      return [...root.querySelectorAll('[data-score-unit]')].find(node=>String(node.dataset.scoreUnit??'')===String(unitId??'')&&String(node.dataset.dim??'')===String(dimId??''))||null;
    }
    function scoreEchoFor(unitId,dimId){
      return [...root.querySelectorAll('[data-score-echo]')].find(node=>String(node.dataset.scoreEchoUnit??'')===String(unitId??'')&&String(node.dataset.scoreEchoDim??'')===String(dimId??''))||null;
    }
    function updateScoreEcho(input){
      const output=scoreEchoFor(input.dataset.scoreUnit,input.dataset.dim);
      if(!output)return;
      const raw=String(input.value??'').trim();
      if(!raw){output.textContent=scoreEchoText('');input.removeAttribute('aria-invalid');return;}
      const value=Number(raw);
      if(!Number.isFinite(value)||value<0||value>maxScore()){
        output.textContent='请输入 0–'+scoreValueText(maxScore())+' 分';
        input.setAttribute('aria-invalid','true');
        return;
      }
      input.removeAttribute('aria-invalid');
      output.textContent=scoreEchoText(value);
    }
    function sortedImages(){
      const raw=Array.isArray(state.data?.deck?.data?.images)?state.data.deck.data.images:[];
      const positionOf=image=>{
        const value=image?.position;
        if(value===null||value===undefined||String(value).trim()==='')return null;
        const number=Number(value);
        return Number.isFinite(number)?number:null;
      };
      return raw.map((image,index)=>({image,index,position:positionOf(image)})).sort((left,right)=>{
        if(left.position!==null&&right.position!==null&&left.position!==right.position)return left.position-right.position;
        if(left.position!==null&&right.position===null)return -1;
        if(left.position===null&&right.position!==null)return 1;
        return left.index-right.index;
      }).map(entry=>entry.image);
    }
    const images=()=>sortedImages();
    function currentPreviewIndex(){
      const count=images().length;
      if(!count)return 0;
      const value=Number(state.previewIndex);
      const index=Number.isFinite(value)?Math.trunc(value):0;
      return Math.min(Math.max(0,index),count-1);
    }
    function currentPreviewImage(){
      const pages=images();
      return pages[currentPreviewIndex()]||null;
    }
    function previewPageNumber(image,index){
      const position=Number(image?.position);
      return Number.isFinite(position)&&position>0?position:index+1;
    }
    const unitKey=unitId=>unitId||'';
    function professionalVersionMatches(row){
      const deckVersion=String(state.data?.deckVersion??''),rubricVersion=String(state.data?.rubricVersion??'');
      if(deckVersion&&String(row?.deckVersion??'')!==deckVersion)return false;
      if(rubricVersion&&String(row?.rubricVersion??'')!==rubricVersion)return false;
      return true;
    }
    function professionalSubmissionsFor(unitId,scope){
      const rows=Array.isArray(state.data?.submissions)?state.data.submissions:[];
      return rows.filter(row=>row&&String(row.scope||'')===String(scope)&&String(row.unitId||'')===String(unitId||'')&&professionalVersionMatches(row))
        .sort((a,b)=>String(b.createdAt||'').localeCompare(String(a.createdAt||'')));
    }
    // 专业版只回填最新同版本人工 submission / formal decision，绝不读取 AI 预评。
    function latestProfessionalFormal(unitId,scope){
      const submissions=professionalSubmissionsFor(unitId,scope);
      if(!submissions.length)return null;
      const byId=new Map(submissions.map((row,index)=>[String(row.id||''),{row,index}]));
      const active=rows=>String(rows?.status||'').toLowerCase()==='confirmed'||String(rows?.status||'').toLowerCase()==='corrected';
      const decisions=(Array.isArray(state.data?.decisions)?state.data.decisions:[])
        .map((decision,index)=>({decision,index,submission:byId.get(String(decision?.submissionId||''))}))
        .filter(item=>item.submission&&active(item.decision))
        .sort((left,right)=>String(right.submission.row.createdAt||'').localeCompare(String(left.submission.row.createdAt||''))||left.index-right.index);
      for(const {decision,submission} of decisions){
        const scores=completeCurrentDimensionScores(decision.formalScores);
        if(!scores)continue;
        const base=submission.row.entry||{};
        return {scores,entry:{scores,note:base.note||'',favorite:!!base.favorite,classificationSuggestion:base.classificationSuggestion||'',classificationCorrection:decision.classification||base.classificationCorrection||'',reasons:Array.isArray(decision.reasons)?[...decision.reasons]:Array.isArray(base.reasons)?[...base.reasons]:[],reasonOther:decision.reasonOther||base.reasonOther||''},submissionId:String(submission.row.id||''),decisionId:String(decision.decisionId||'')};
      }
      for(const submission of submissions){
        const entry=submission.entry||{},scores=completeCurrentDimensionScores(entry.scores);
        if(!scores)continue;
        return {scores,entry:{scores,note:entry.note||'',favorite:!!entry.favorite,classificationSuggestion:entry.classificationSuggestion||'',classificationCorrection:entry.classificationCorrection||'',reasons:Array.isArray(entry.reasons)?[...entry.reasons]:[],reasonOther:entry.reasonOther||''},submissionId:String(submission.id||''),decisionId:''};
      }
      return null;
    }
    const entryFor=(unitId,scope=state.scope)=>{
      if(professionalReviewOnlyMode())return latestProfessionalFormal(unitId,scope)?.entry||{scores:{},note:''};
      const data=state.data||{},review=data.review||{};
      if(state.opts.kind==='generated'){
        const rows=(data.submissions||[]).filter(row=>String(row.unitId||'')===String(unitId||'')&&String(row.scope)===scope).sort((a,b)=>String(b.createdAt||'').localeCompare(String(a.createdAt||'')));
        return rows[0]?.entry||{scores:{},note:''};
      }
      if(scope==='page')return review?.pages?.[unitId]||{scores:{},note:''};
      return review||{scores:{},note:''};
    };
    function draftFor(unitId,scope=state.scope){
      const key=scope+':'+unitKey(unitId);
      if(!state.drafts[key]){
        const entry=entryFor(unitId,scope)||{};
        state.drafts[key]={scores:{...(entry.scores||{})},note:entry.note||'',favorite:!!entry.favorite,classificationSuggestion:entry.classificationSuggestion||'',classificationCorrection:entry.classificationCorrection||'',reasons:Array.isArray(entry.reasons)?[...entry.reasons]:[],reasonOther:entry.reasonOther||''};
      }
      if(state.draftRevisions[key]===undefined)state.draftRevisions[key]=0;
      return state.drafts[key];
    }
    function markDraftDirty(draftKey){
      state.draftRevisions[draftKey]=(state.draftRevisions[draftKey]||0)+1;
    }
    function discardSubmittedDrafts(submittedUnits){
      for(const unit of submittedUnits){
        if(state.drafts[unit.draftKey]!==unit.draft||(state.draftRevisions[unit.draftKey]||0)!==unit.revision)continue;
        delete state.drafts[unit.draftKey];
        delete state.draftRevisions[unit.draftKey];
      }
    }
    function discardCleanProfessionalDrafts(){
      if(!professionalReviewOnlyMode())return;
      for(const key of Object.keys(state.drafts))if((state.draftRevisions[key]||0)===0){delete state.drafts[key];delete state.draftRevisions[key];}
    }
    function selectedUnits(){
      ensureSelectionDeck();
      if(state.scope==='deck')return [{unitId:'',position:0,key:'deck:',scope:'deck'}];
      return selectedPageUnits();
    }
    function selectedPageUnits(){
      ensureSelectionDeck();
      return images().map((image,index)=>({unitId:String(image.id),position:Number(image.position||index+1),key:'page:'+image.id,scope:'page'}))
        .filter(unit=>state.selected.has(unit.unitId));
    }
    function professionalBundleHasSavedFormal(){
      if(!professionalReviewOnlyMode())return false;
      return [{unitId:'',position:0,scope:'deck'},...selectedPageUnits()]
        .some(unit=>Boolean(latestProfessionalFormal(unit.unitId,unit.scope)));
    }
    function humanSubmitLabel(pending){
      if(pending)return humanSubmitButtonText(pending);
      if(professionalReviewOnlyMode()&&professionalBundleHasSavedFormal())return '保存改分';
      if(coverTrainingOnePage())return '提交本页人工评分';
      return singleJudgeMode()?'提交整套与选页人工评分':'提交所选人工评分';
    }
    function validDraftScores(draft){
      const entries=[];
      for(const dimension of dimensions()){
        const key=String(dimension.id),value=draft?.scores?.[key];
        if(value===null||value===undefined||String(value).trim()==='')return null;
        const number=Number(value);
        if(!Number.isFinite(number)||number<0||number>maxScore())return null;
        entries.push([key,number]);
      }
      return entries.length?Object.fromEntries(entries):null;
    }
    function draftUnitFor(unit){
      const scope=unit.scope==='page'?'page':'deck',draft=draftFor(unit.unitId,scope),draftKey=scope+':'+unitKey(unit.unitId);
      return {draft,draftKey,revision:state.draftRevisions[draftKey]||0,scope};
    }
    function legacySubmissionPlan(){
      const units=selectedUnits();
      return {ready:units.length>0,units:units.map(unit=>{
        const base=draftUnitFor(unit),draft=base.draft,scores={};
        for(const [key,value] of Object.entries(draft.scores||{}))if(Number.isFinite(Number(value)))scores[key]=Number(value);
        return {...base,unitId:unit.unitId,position:unit.position,entry:{unitId:unit.unitId,position:unit.position,scores,note:draft.note||'',favorite:!!draft.favorite,classificationSuggestion:draft.classificationSuggestion||'',classificationCorrection:draft.classificationCorrection||'',reasons:Array.isArray(draft.reasons)?[...draft.reasons]:[],reasonOther:draft.reasonOther||''}};
      })};
    }
    function singleJudgeSubmissionPlan(){
      const pages=selectedPageUnits();
      if(coverTrainingOnePage()){
        if(pages.length!==1)return {ready:false,reason:'请选择唯一封面页后评分',units:[]};
        const unit=pages[0],base=draftUnitFor(unit),draft=base.draft,scores=validDraftScores(draft);
        if(!scores)return {ready:false,reason:'请填写本页全部评分项',units:[]};
        const entry={scope:'page',unitId:unit.unitId,position:unit.position,scores,note:draft.note||'',favorite:!!draft.favorite,classificationSuggestion:draft.classificationSuggestion||'',classificationCorrection:draft.classificationCorrection||'',reasons:Array.isArray(draft.reasons)?[...draft.reasons]:[],reasonOther:draft.reasonOther||''};
        return {ready:true,units:[
          {...base,unitId:'',position:0,entry:{...entry,scope:'deck',unitId:'',position:0}},
          {...base,unitId:unit.unitId,position:unit.position,entry},
        ]};
      }
      const units=[];
      for(const unit of [{unitId:'',position:0,key:'deck:',scope:'deck'},...pages]){
        const base=draftUnitFor(unit),draft=base.draft,scores=validDraftScores(draft);
        if(!scores)return {ready:false,reason:unit.scope==='deck'?'请填写整套所有评分项':'请填写第 '+unit.position+' 页所有评分项',units:[]};
        units.push({...base,unitId:unit.unitId,position:unit.position,entry:{scope:unit.scope,unitId:unit.unitId,position:unit.position,scores,note:draft.note||'',favorite:!!draft.favorite,classificationSuggestion:draft.classificationSuggestion||'',classificationCorrection:draft.classificationCorrection||'',reasons:Array.isArray(draft.reasons)?[...draft.reasons]:[],reasonOther:draft.reasonOther||''}});
      }
      return {ready:true,units};
    }
    function submissionPlan(){
      return singleJudgeMode()?singleJudgeSubmissionPlan():legacySubmissionPlan();
    }
    function switchReviewScope(nextScope){
      const scope=nextScope==='page'?'page':'deck';
      if(scope===state.scope)return false;
      deckPreviewEpoch++;
      closePreviewDialog(false);
      ensureSelectionDeck();
      state.scope=scope;
      preEvaluationDelay=PRE_EVALUATION_POLL_MS;
      preEvaluationHintShown=false;
      render();
      return true;
    }
    function focusPageRatingControl(deckKey,pageId){
      if(destroyed||state.scope!=='page'||currentDeckKey()!==deckKey)return false;
      const card=[...root.querySelectorAll('[data-unit-card]')].find(node=>String(node.dataset.unitCard??'')===String(pageId)&&String(node.dataset.unitDeck??'')===String(deckKey))||null;
      if(!card)return false;
      const score=[...card.querySelectorAll('[data-score-unit]')].find(node=>!node.disabled)||null;
      const checkbox=card.querySelector('[data-select-unit]');
      const target=score||checkbox||card;
      if(target===card)card.setAttribute('tabindex','-1');
      try{target.scrollIntoView?.({block:'center',inline:'nearest'});}catch(_){try{target.scrollIntoView?.();}catch(_){}}
      try{target.focus({preventScroll:true});}catch(_){try{target.focus();}catch(_){}}
      return global.document?.activeElement===target;
    }
    function schedulePageRatingFocus(deckKey,pageId){
      const focus=()=>{focusPageRatingControl(deckKey,pageId);};
      if(typeof global.requestAnimationFrame==='function')global.requestAnimationFrame(focus);
      else if(typeof global.queueMicrotask==='function')global.queueMicrotask(focus);
      else global.setTimeout(focus,0);
    }
    function openDeck(kind,deckId=''){
      const nextKind=kind==='generated'?'generated':'uploaded';
      const previousDeckKey=currentDeckKey();
      let nextDeckId=String(deckId||'');
      if(!nextDeckId){
        const remembered=lastDeckByKind[nextKind];
        if(remembered&&queueTrack(nextKind).items.some(item=>item.id===remembered))nextDeckId=remembered;
      }
      const nextDeckKey=nextKind+':'+nextDeckId;
      if(nextDeckId)lastDeckByKind[nextKind]=nextDeckId;
      importEpoch++;
      deckPreviewEpoch++;
      closePreviewDialog(false);
      if(nextDeckKey!==previousDeckKey)state.previewIndex=0;
      if(nextKind!==state.opts.kind&&!professionalComparison())statsEpoch++;
      state.opts={...state.opts,kind:nextKind,deckId:nextDeckId};
      state.data=null;
      state.scope='deck';
      state.selectionDeckKey=currentDeckKey();
      state.selected.clear();
      state.drafts={};
      state.draftRevisions={};
      state.formalDrafts={};
      state.formalDirty={};
      state.formalRevisions={};
      state.message='';
      return load();
    }
    async function load({resetQueues=false,focusRestore=null,keepMessage=false,beforeRender=null}={}){
      const runEpoch=++workspaceEpoch;
      queueEpoch++;
      deckPreviewEpoch++;
      closePreviewDialog(false);
      for(const kind of ['uploaded','generated']){
        const track=queueTrack(kind);
        track.loading=false;track.error='';
      }
      state.runBusy=false;
      resetPreEvaluationPolling();
      state.preEvaluationStatus=undefined;state.preEvaluationFeedback='';
      state.busy=true;state.error='';render();
      let pendingFocus=focusRestore;
      const trackingFocus=Boolean(pendingFocus);
      const onFocusIn=event=>{pendingFocus=focusDescriptor(event.target);};
      if(trackingFocus)global.document?.addEventListener?.('focusin',onFocusIn,true);
      try{
        const query=new URLSearchParams({action:'workspace',kind:state.opts.kind});if(state.opts.deckId)query.set('deckId',state.opts.deckId);
        const data=await request(AI_PATH+'?'+query.toString());
        if(destroyed||runEpoch!==workspaceEpoch)return state.data;
        state.data=data;
        if(coverTrainingOnePage()){
          const only=images()[0];
          state.scope='page';
          state.selectionDeckKey=currentDeckKey();
          state.selected.clear();
          if(only?.id)state.selected.add(String(only.id));
        }
        discardCleanProfessionalDrafts();
        discardCleanFormalDrafts();
        applyWorkspaceQueues(state.data,{reset:resetQueues});
        if(state.data?.deck?.id){
          state.opts.deckId=state.data.deck.id;
          lastDeckByKind[state.opts.kind]=String(state.data.deck.id);
        }
        state.preEvaluationStatus=normalizePreEvaluationStatus(state.data?.preEvaluationStatus);
        if(state.scope==='page'){
          const current=new Set(images().map(image=>String(image.id)));
          for(const id of [...state.selected])if(!current.has(id))state.selected.delete(id);
        }
        if(!keepMessage)state.message='';
      }catch(err){
        if(!destroyed&&runEpoch===workspaceEpoch)state.error=err.message||String(err);
      }finally{
        if(trackingFocus)global.document?.removeEventListener?.('focusin',onFocusIn,true);
        if(!destroyed&&runEpoch===workspaceEpoch){
          state.busy=false;
          if(typeof beforeRender==='function')beforeRender();
          render();
          if(!destroyed)restoreFocusDescriptor(pendingFocus);
          if(isPreEvaluationActive())schedulePreEvaluationPoll();
        }
      }
      return state.data;
    }
    function statusRequestIsCurrent(workspaceToken,statusToken,deckKey){
      return !destroyed&&workspaceEpoch===workspaceToken&&preEvaluationEpoch===statusToken&&currentDeckKey()===deckKey;
    }
    function schedulePreEvaluationPoll(delay=preEvaluationDelay){
      if(destroyed||!isPreEvaluationActive()||!state.opts.deckId||typeof global.setTimeout!=='function')return;
      clearPreEvaluationTimer();
      const workspaceToken=workspaceEpoch;
      const statusToken=++preEvaluationEpoch;
      const deckKey=currentDeckKey();
      preEvaluationTimer=global.setTimeout(()=>{
        preEvaluationTimer=null;
        void pollPreEvaluationStatus(workspaceToken,statusToken,deckKey);
      },Math.max(0,delay));
    }
    async function syncPreEvaluationStatus({workspaceToken,statusToken,deckKey,resetBackoff=false}={}){
      if(!statusRequestIsCurrent(workspaceToken,statusToken,deckKey))return false;
      if(resetBackoff){preEvaluationDelay=PRE_EVALUATION_POLL_MS;preEvaluationHintShown=false;}
      try{
        const query=new URLSearchParams({action:'pre-evaluation-status',kind:state.opts.kind,deckId:state.opts.deckId});
        const payload=await request(AI_PATH+'?'+query.toString());
        if(!statusRequestIsCurrent(workspaceToken,statusToken,deckKey))return false;
        const nextStatus=normalizePreEvaluationStatus(payload?.preEvaluationStatus);
        if(!nextStatus)throw new Error('预评状态数据无效');
        const changed=nextStatus!==state.preEvaluationStatus;
        state.preEvaluationStatus=nextStatus;
        if(state.data&&typeof state.data==='object')state.data.preEvaluationStatus=nextStatus;
        state.preEvaluationFeedback='';
        preEvaluationHintShown=false;
        preEvaluationDelay=changed?PRE_EVALUATION_POLL_MS:Math.min(Math.max(preEvaluationDelay,PRE_EVALUATION_POLL_MS)*2,PRE_EVALUATION_POLL_MAX_MS);
        updatePreEvaluationDom();
        if(isPreEvaluationActive())schedulePreEvaluationPoll();
        return true;
      }catch(_){
        if(!statusRequestIsCurrent(workspaceToken,statusToken,deckKey))return false;
        if(!preEvaluationHintShown){
          state.preEvaluationFeedback='状态暂时无法更新，稍后重试';
          preEvaluationHintShown=true;
          updatePreEvaluationDom();
        }
        preEvaluationDelay=Math.min(Math.max(preEvaluationDelay,PRE_EVALUATION_POLL_MS)*2,PRE_EVALUATION_POLL_MAX_MS);
        if(isPreEvaluationActive())schedulePreEvaluationPoll();
        return false;
      }
    }
    async function pollPreEvaluationStatus(workspaceToken,statusToken,deckKey){
      await syncPreEvaluationStatus({workspaceToken,statusToken,deckKey});
    }
    async function refreshPreEvaluationStatus({resetBackoff=false,workspaceToken=workspaceEpoch}={}){
      if(destroyed||workspaceToken!==workspaceEpoch||!state.opts.deckId)return false;
      clearPreEvaluationTimer();
      const statusToken=++preEvaluationEpoch;
      return syncPreEvaluationStatus({workspaceToken,statusToken,deckKey:currentDeckKey(),resetBackoff});
    }
    function aiFor(unitId){
      const rows=state.data?.ai||[];
      return rows.filter(row=>String(row.unitId||'')===String(unitId||'')&&String(row.scope)===state.scope);
    }
    function submissionFor(unitId){
      const rows=state.data?.submissions||[];
      return rows.filter(row=>String(row.unitId||'')===String(unitId||'')&&String(row.scope)===state.scope);
    }
    function readableText(value){
      if(value===null||value===undefined)return '';
      if(typeof value==='string')return value.trim();
      if(typeof value==='number')return Number.isFinite(value)?String(value):'';
      if(typeof value==='boolean')return '';
      if(Array.isArray(value))return [...new Set(value.map(readableText).filter(Boolean))].join('；');
      if(typeof value==='object'){
        for(const key of ['label','primary','name','text','reason','summary','title','value','description','code']){
          const text=readableText(value[key]);
          if(text)return text;
        }
        return [...new Set(Object.values(value).map(readableText).filter(Boolean))].join('；');
      }
      return '';
    }
    function reasonsFor(result){
      const raw=result?.reasons;
      const values=Array.isArray(raw)?raw:(raw==null?[]:[raw]);
      return [...new Set(values.map(readableText).filter(Boolean))];
    }
    function scoreName(dimensionId){
      const id=String(dimensionId??'');
      const dim=dimensions().find(item=>String(item.id)===id);
      return String(dim?.name||dim?.label||id||'评分项');
    }
    function aiVariantLabel(variant){
      const key=String(variant||'');
      return AI_VARIANT_LABELS[key]||key||'预评';
    }
    function aiRecordHtml(row){
      const result=row?.result&&typeof row.result==='object'&&!Array.isArray(row.result)?row.result:{};
      const scores=result.scores&&typeof result.scores==='object'&&!Array.isArray(result.scores)?result.scores:{};
      const rows=[];
      for(const [dimension,value] of Object.entries(scores)){
        const text=readableText(value);
        if(text)rows.push([scoreName(dimension),esc(text)]);
      }
      const classification=readableText(result.classification);
      if(classification)rows.push(['分类',esc(classification)]);
      const reasons=reasonsFor(result);
      if(reasons.length)rows.push(['理由','<ul class="ga-ai-reasons">'+reasons.map(reason=>'<li>'+esc(reason)+'</li>').join('')+'</ul>']);
      const summary=readableText(result.summary);
      if(summary)rows.push(['摘要',esc(summary)]);
      if(!rows.length)return '';
      return '<dl class="ga-ai-summary">'+rows.map(([label,value])=>'<div class="ga-ai-summary-row"><dt>'+esc(label)+'</dt><dd>'+value+'</dd></div>').join('')+'</dl>';
    }
    function aiPreviewHtml(unitId){
      if(professionalReviewOnlyMode()&&professionalManualOnly())return '';
      const records=aiFor(unitId);
      if(!records.length)return '';
      const variants=[...AI_VARIANT_ORDER];
      for(const row of records){const variant=String(row.variant||'');if(!variants.includes(variant))variants.push(variant);}
      const groups=variants.map(variant=>{
        const bodies=records.filter(row=>String(row.variant||'')===variant).map(aiRecordHtml).filter(Boolean);
        if(!bodies.length)return '';
        return '<div class="ga-ai-group" data-ai-variant="'+esc(variant)+'"><div class="ga-ai-group-label"><b>'+esc(aiVariantLabel(variant))+'</b></div>'+bodies.join('')+'</div>';
      }).join('');
      if(!groups)return '';
      if(professionalReviewOnlyMode())return '<section class="ga-ai" data-ai-comparison="first-pair" aria-label="首次有效配对已解锁"><div class="ga-ai-head"><b>首次有效配对已解锁</b><p class="ga-ai-hint">仅展示 workspace 已按同版本、同评分标准、同粒度过滤的数据。</p></div>'+groups+'</section>';
      return '<section class="ga-ai" aria-label="已解锁同粒度预评"><div class="ga-ai-head"><b>已解锁同粒度预评</b><p class="ga-ai-hint">正式评分以专职评委提交为准。</p></div>'+groups+'</section>';
    }
    function professionalManualOnly(){
      const data=state.data||{},deck=data.deck||{},deckData=deck.data||{};
      const value=deckData.aiAllowed??deckData.ai_allowed??deck.aiAllowed??deck.ai_allowed??data.aiAllowed;
      return value===false||value==='false';
    }
    function professionalReviewNoticeHtml(){
      if(!professionalReviewOnlyMode())return '';
      const manual=professionalManualOnly();
      return '<div class="ga-band" data-professional-review-only><div class="ga-row"><b>专业人工评分</b><span class="ga-pill">首屏数据</span></div><p class="ga-muted">'+(manual?'仅人工素材只保存人工分，不产生模型对比。':'提交整套与选中页人工分；首次有效配对只使用 workspace 已过滤的同版本数据。')+'</p>'+(manual?'<div class="ga-warn" data-manual-only-note style="margin-top:10px"><b>仅人工素材</b><br>该素材不发送给模型、不进入 AI 参考样本，因此不会产生模型对比。</div>':'')+'</div>';
    }
    function status(){
      if(state.busy)return '<span class="ga-muted">处理中…</span>';
      const pending=pendingHumanSubmit();
      if(pending)return '<span class="ga-muted">'+esc(humanSubmitButtonText(pending))+'</span>';
      if(state.error)return (state.message&&professionalReviewOnlyMode()?'<span class="ga-ok">'+esc(state.message)+'</span> ':'')+'<span class="ga-error">'+esc(state.error)+'</span>';
      if(state.message)return '<span class="ga-ok">'+esc(state.message)+'</span>';
      return '<span class="ga-muted">'+(professionalReviewOnlyMode()?'正式人分保存后会刷新首次有效对比':'AI 预评在人工提交前严格不可见')+'</span>';
    }
    function scoreFieldHtml(unitId,unitLabel,draft,dim){
      const dimId=String(dim.id??''),dimensionName=String(dim.name||dim.label||dimId||'评分项');
      const inputId=scoreFieldId('score',unitId,dimId),echoId=scoreFieldId('echo',unitId,dimId),maximum=scoreValueText(maxScore());
      const value=scoreValueText(draft.scores?.[dim.id]??'');
      const quick=[0,.5,.75,.85,1].map(ratio=>{
        const actual=scoreValueText(maxScore()*ratio);
        return '<button type="button" class="ga-pill" data-quick-unit="'+esc(unitId)+'" data-quick-dim="'+esc(dimId)+'" data-quick-value="'+esc(actual)+'" aria-controls="'+inputId+'" aria-label="将'+esc(dimensionName)+'设为 '+esc(actual)+' 分（'+(ratio*100)+'%）">'+(ratio*100)+'%</button>';
      }).join('');
      return '<div class="ga-field ga-score-field">'
        +'<label for="'+inputId+'">'+esc(dimensionName)+'</label>'
        +'<input id="'+inputId+'" type="number" min="0" max="'+esc(maximum)+'" step="'+SCORE_STEP+'" inputmode="decimal" autocomplete="off" data-score-unit="'+esc(unitId)+'" data-dim="'+esc(dimId)+'" value="'+esc(value)+'" aria-describedby="'+echoId+'">'
        +'<div class="ga-quick" role="group" aria-label="'+esc(unitLabel+' · '+dimensionName+'速选，满分 '+maximum+' 分')+'">'+quick+'</div>'
        +'<output id="'+echoId+'" class="ga-score-echo" for="'+inputId+'" data-score-echo data-score-echo-unit="'+esc(unitId)+'" data-score-echo-dim="'+esc(dimId)+'" aria-live="polite" aria-atomic="true">'+esc(scoreEchoText(value))+'</output>'
        +'</div>';
    }
    function deckPreviewHtml(){
      const pages=images(),index=currentPreviewIndex(),image=pages[index]||null,total=pages.length,pageId=String(image?.id??'').trim();
      const status=total?('第'+(index+1)+'/'+total+'页'):'暂无页面';
      return '<div class="ga-preview-shell" role="group" data-preview-shell aria-roledescription="轮播" aria-label="整套作品页面预览">'
        +'<div class="ga-preview" data-preview-stage data-preview-path="'+esc(image?.path||'')+'" aria-busy="'+(image?'true':'false')+'">'+(image?'<span class="ga-muted">预览加载中…</span>':'<span class="ga-muted">暂无页面预览</span>')+'</div>'
        +'<div class="ga-preview-controls" role="group" aria-label="页面预览控制">'
        +'<button type="button" class="ga-btn ga-preview-nav" data-action="preview-prev" aria-label="上一页" '+(!image||index<=0?'disabled':'')+'>上一页</button>'
        +'<span class="ga-preview-status" data-preview-status id="ga-preview-status" role="status" aria-live="polite" aria-atomic="true">'+esc(status)+'</span>'
        +'<button type="button" class="ga-btn ga-preview-nav" data-action="preview-next" aria-label="下一页" '+(!image||index>=total-1?'disabled':'')+'>下一页</button>'
        +'<button type="button" class="ga-btn ga-preview-rate-page" data-action="preview-rate-page" aria-controls="ga-review-units" aria-describedby="ga-preview-status" aria-label="给这一页打分" '+(pageId?'':'disabled')+'>给这一页打分</button>'
        +'<button type="button" class="ga-btn ga-preview-zoom" data-action="preview-zoom" aria-label="放大预览" disabled>放大预览</button>'
        +'</div></div>';
    }
    function setDeckPreviewZoomEnabled(enabled){
      const button=root.querySelector('[data-action="preview-zoom"]');
      if(button)button.disabled=!enabled;
    }
    function showDeckPreviewFailure(message){
      const stage=root.querySelector('[data-preview-stage]');
      if(!stage)return;
      stage.dataset.loaded='0';
      stage.setAttribute('aria-busy','false');
      stage.innerHTML='<div class="ga-preview-error"><span class="ga-muted">'+esc(message||'预览暂不可用')+'</span><button type="button" class="ga-btn" data-action="preview-retry">重试预览</button></div>';
      setDeckPreviewZoomEnabled(false);
    }
    function showDeckPreviewImage(url,image,epoch,deckKey){
      const stage=root.querySelector('[data-preview-stage]');
      if(!stage)return;
      stage.innerHTML='';
      const img=global.document.createElement('img');
      const pages=images(),index=currentPreviewIndex();
      const current=()=>!destroyed
        &&epoch===deckPreviewEpoch
        &&state.scope==='deck'
        &&currentDeckKey()===deckKey
        &&currentPreviewImage()?.path===String(image.path||'')
        &&img.parentNode===stage
        &&stage.contains(img);
      img.alt='第 '+previewPageNumber(image,index)+' 页 · '+String(image.name||image.id||'页面预览');
      img.decoding='async';
      img.loading='eager';
      img.onload=()=>{
        if(!current())return;
        stage.dataset.loaded='1';
        stage.setAttribute('aria-busy','false');
        setDeckPreviewZoomEnabled(true);
      };
      img.onerror=()=>{
        if(!current())return;
        showDeckPreviewFailure('图片加载失败，请重试');
      };
      stage.appendChild(img);
      img.src=String(url);
    }
    async function loadDeckPreview(){
      if(state.scope!=='deck')return;
      const image=currentPreviewImage(),epoch=++deckPreviewEpoch,deckKey=currentDeckKey(),requestPath=String(image?.path||'');
      const current=()=>!destroyed&&epoch===deckPreviewEpoch&&state.scope==='deck'&&currentDeckKey()===deckKey&&currentPreviewImage()?.path===requestPath;
      if(!image){
        showDeckPreviewFailure('暂无页面预览');
        return;
      }
      const stage=root.querySelector('[data-preview-stage]');
      if(stage){
        stage.dataset.previewPath=requestPath;
        stage.dataset.loaded='0';
        stage.setAttribute('aria-busy','true');
        stage.innerHTML='<span class="ga-muted">预览加载中…</span>';
      }
      setDeckPreviewZoomEnabled(false);
      if(!requestPath||!global.Cloud?.imageUrl){
        if(current())showDeckPreviewFailure('预览暂不可用，请重试');
        return;
      }
      try{
        const url=await global.Cloud.imageUrl(requestPath);
        if(!current())return;
        if(!url)throw new Error('预览签名无效');
        showDeckPreviewImage(url,image,epoch,deckKey);
      }catch(_){
        if(current())showDeckPreviewFailure('预览暂不可用，请重试');
      }
    }
    function refreshDeckPreview(){
      const shell=root.querySelector('[data-preview-shell]');
      if(!shell)return;
      const pages=images(),index=currentPreviewIndex(),image=pages[index]||null,total=pages.length;
      const stage=shell.querySelector('[data-preview-stage]');
      const status=shell.querySelector('[data-preview-status]');
      const previous=shell.querySelector('[data-action="preview-prev"]');
      const next=shell.querySelector('[data-action="preview-next"]');
      const rate=shell.querySelector('[data-action="preview-rate-page"]');
      if(stage){
        stage.dataset.previewPath=String(image?.path||'');
        stage.dataset.loaded='0';
      }
      if(status)status.textContent=total?('第'+(index+1)+'/'+total+'页'):'暂无页面';
      if(previous)previous.disabled=!image||index<=0;
      if(next)next.disabled=!image||index>=total-1;
      if(rate){
        const pageId=String(image?.id??'').trim();
        rate.disabled=!pageId;
      }
      setDeckPreviewZoomEnabled(false);
      void loadDeckPreview();
    }
    function changeDeckPreview(delta){
      const total=images().length;
      if(!total)return;
      const next=Math.min(Math.max(0,currentPreviewIndex()+delta),total-1);
      if(next===currentPreviewIndex())return;
      state.previewIndex=next;
      refreshDeckPreview();
      const activeButton=root.querySelector(delta<0?'[data-action="preview-prev"]':'[data-action="preview-next"]');
      const fallbackButton=root.querySelector(delta<0?'[data-action="preview-next"]':'[data-action="preview-prev"]');
      if(activeButton?.disabled&&fallbackButton&&!fallbackButton.disabled)fallbackButton.focus();
    }
    function ensurePreviewDialog(){
      if(previewDialog?.isConnected)return previewDialog;
      if(!global.document?.body)return null;
      const dialog=global.document.createElement('dialog');
      dialog.id='guanye-ai-preview-dialog';
      dialog.className='ga-lightbox';
      dialog.setAttribute('role','dialog');
      dialog.setAttribute('aria-modal','true');
      dialog.setAttribute('aria-labelledby','ga-preview-dialog-title');
      dialog.innerHTML='<div class="ga-lightbox-toolbar"><b class="ga-lightbox-title" id="ga-preview-dialog-title">页面大图</b><button type="button" class="ga-lightbox-close" data-action="preview-close" aria-label="关闭大图">关闭大图</button></div><div class="ga-lightbox-canvas"><img alt="放大页面预览"></div>';
      const close=()=>closePreviewDialog(true);
      dialog.querySelector('[data-action="preview-close"]')?.addEventListener('click',close);
      dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
      dialog.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();close();}});
      dialog.addEventListener('click',event=>{if(event.target===dialog)close();});
      global.document.body.appendChild(dialog);
      previewDialog=dialog;
      return dialog;
    }
    function openPreviewDialog(trigger){
      const source=root.querySelector('.ga-preview img');
      if(!source)return;
      const dialog=ensurePreviewDialog();
      if(!dialog)return;
      const image=currentPreviewImage(),pages=images(),index=currentPreviewIndex();
      previewDialogTrigger=trigger||null;
      const title=dialog.querySelector('.ga-lightbox-title');
      const large=dialog.querySelector('.ga-lightbox-canvas img');
      if(title)title.textContent=image?('第 '+previewPageNumber(image,index)+' / '+pages.length+' 页 · '+String(image.name||image.id||'页面预览')):'页面大图';
      if(large){large.src=source.currentSrc||source.src;large.alt=source.alt||'放大页面预览';}
      if(!dialog.open){if(typeof dialog.showModal==='function')dialog.showModal();else dialog.setAttribute('open','');}
      dialog.querySelector('[data-action="preview-close"]')?.focus();
    }
    function closePreviewDialog(restoreFocus=true){
      const dialog=previewDialog,trigger=previewDialogTrigger;
      previewDialogTrigger=null;
      if(dialog?.open){try{dialog.close();}catch(_){dialog.removeAttribute?.('open');}}
      else if(dialog?.hasAttribute?.('open'))dialog.removeAttribute('open');
      if(restoreFocus&&trigger?.isConnected){try{trigger.focus();}catch(_){}}
    }
    function unitCard(unitId,position,label){
      const draft=draftFor(unitId),key=state.scope+':'+unitKey(unitId),checked=state.scope==='deck'||state.selected.has(String(unitId));
      const subs=submissionFor(unitId);
      const admission=professionalReviewOnlyMode()&&state.opts.kind==='generated'?professionalAdmissionHtml(unitId):null;
      return '<article class="ga-card '+(checked?'selected':'')+'" data-unit-card="'+esc(unitId)+'" data-unit-deck="'+esc(currentDeckKey())+'">'
        +'<div class="ga-row"><label class="ga-check"><input type="checkbox" data-select-unit="'+esc(unitId)+'" '+(checked?'checked':'')+' '+(state.scope==='deck'?'disabled':'')+'> '+esc(label)+'</label><span class="ga-pill">'+(state.scope==='page'?'页 '+(position||1):'整套')+'</span></div>'
        +(state.scope==='deck'?deckPreviewHtml():'<div class="ga-preview" data-preview-path="'+esc(images().find(image=>String(image.id)===String(unitId))?.path||'')+'"><span>预览加载中…</span></div>')
        +'<div class="ga-scores">'+dimensions().map(dim=>scoreFieldHtml(unitId,label,draft,dim)).join('')+'</div>'
        +'<label class="ga-field" style="margin-top:10px">人工备注<textarea data-note-unit="'+esc(unitId)+'">'+esc(draft.note||'')+'</textarea></label>'
        +'<div class="ga-grid" style="margin-top:10px"><label class="ga-field">分类建议<input data-suggest-unit="'+esc(unitId)+'" value="'+esc(draft.classificationSuggestion||'')+'"></label><label class="ga-field">人工纠偏<input data-correct-unit="'+esc(unitId)+'" value="'+esc(draft.classificationCorrection||'')+'"></label></div>'
        +reasonsHtml(unitId,draft)
        +'<label class="ga-field" style="margin-top:8px">其他原因文字<input data-other-unit="'+esc(unitId)+'" maxlength="500" value="'+esc(draft.reasonOther||'')+'"></label>'
        +(subs.length&&state.data?.isJudge&&!professionalReviewOnlyMode()?formalHtml(unitId,subs):'')
        +(admission===null?'':'<div data-professional-admission="'+esc(unitId)+'" data-admission-view="'+esc(admission)+'">'+admission+'</div>')
        +aiPreviewHtml(unitId)
        +'</article>';
    }
    function reasonsHtml(unitId,draft){
      const catalog=state.data?.settings?.reasonCatalog||[];
      return '<div class="ga-reasons">'+catalog.map(item=>{const code=String(item.code||item);const label=String(item.label||code);return '<label class="ga-check"><input type="checkbox" data-reason-unit="'+esc(unitId)+'" value="'+esc(code)+'" '+((draft.reasons||[]).includes(code)?'checked':'')+'> '+esc(label)+'</label>';}).join('')+'</div>';
    }
    function formalDraftKey(submissionId){
      return state.scope+':'+String(submissionId??'');
    }
    function submissionById(submissionId){
      return (state.data?.submissions||[]).find(row=>String(row.id||'')===String(submissionId??'')&&String(row.scope||'')===state.scope)||null;
    }
    function currentDimensionScores(source){
      const values=source&&typeof source==='object'&&!Array.isArray(source)?source:{},scores={};
      for(const dim of dimensions()){
        const value=statsNumber(values[dim.id]);
        if(value!==null&&value>=0&&value<=maxScore())scores[String(dim.id)]=value;
      }
      return scores;
    }
    function completeCurrentDimensionScores(source){
      const current=dimensions(),scores=currentDimensionScores(source);
      return Object.keys(scores).length===current.filter(dim=>dim.id!==undefined&&dim.id!==null&&String(dim.id)!=='').length?scores:null;
    }
    // 正式分只回填同一 submission 的已保存正式决议或人工分，不读取 AI 预评。
    function formalPrefillScores(submissionId){
      const submission=submissionById(submissionId);
      if(!submission)return {};
      for(const decision of decisionsFor(submissionId)){
        const status=String(decision?.status||'').toLowerCase();
        if(status!=='confirmed'&&status!=='corrected')continue;
        const scores=completeCurrentDimensionScores(decision.formalScores);
        if(scores)return scores;
      }
      return currentDimensionScores(submission.entry?.scores);
    }
    function formalDraftFor(submissionId){
      const key=formalDraftKey(submissionId);
      if(!state.formalDrafts[key]){
        state.formalDrafts[key]={scores:formalPrefillScores(submissionId),classification:'',reasons:[],reasonOther:'',summary:''};
        state.formalDirty[key]=false;
        state.formalRevisions[key]=0;
      }
      return state.formalDrafts[key];
    }
    function markFormalDraftDirty(submissionId){
      const key=formalDraftKey(submissionId);
      state.formalDirty[key]=true;
      state.formalRevisions[key]=(state.formalRevisions[key]||0)+1;
    }
    function discardCleanFormalDrafts(){
      for(const key of Object.keys(state.formalDrafts)){
        if(state.formalDirty[key])continue;
        delete state.formalDrafts[key];
        delete state.formalDirty[key];
        delete state.formalRevisions[key];
      }
    }
    function forgetFormalDraft(submissionId){
      const key=formalDraftKey(submissionId);
      delete state.formalDrafts[key];
      delete state.formalDirty[key];
      delete state.formalRevisions[key];
    }
    function updateFormalDraftField(target){
      const submissionId=target.dataset.formalScore??target.dataset.formalReason??target.dataset.formalClassification??target.dataset.formalOther;
      if(submissionId===undefined)return false;
      const draft=formalDraftFor(submissionId);
      if(target.matches('[data-formal-score]')){
        const value=String(target.value??'').trim();
        if(!value||!Number.isFinite(Number(value)))delete draft.scores[target.dataset.dim];
        else draft.scores[target.dataset.dim]=Number(value);
      }else if(target.matches('[data-formal-reason]')){
        draft.reasons=draft.reasons||[];
        if(target.checked&&!draft.reasons.includes(target.value))draft.reasons.push(target.value);
        if(!target.checked)draft.reasons=draft.reasons.filter(value=>value!==target.value);
      }else if(target.matches('[data-formal-classification]')){
        draft.classification=target.value;
      }else if(target.matches('[data-formal-other]')){
        draft.reasonOther=target.value;
      }
      markFormalDraftDirty(submissionId);
      return true;
    }
    function decisionsFor(submissionId){
      const rows=Array.isArray(state.data?.decisions)?state.data.decisions:[];
      return rows.filter(row=>row&&String(row.submissionId||'')===String(submissionId));
    }
    function admissionStateFor(submissionId){
      const decisions=decisionsFor(submissionId);
      const admitted=decisions.find(row=>row.libraryVisible);
      if(admitted)return {state:'admitted',badge:'已收入素材库 · AI 自我学习生成',detail:'该正式评分已进入素材库。',canAdmit:false,decisionId:''};
      const decision=decisions[0];
      const ratioFmt=value=>{const number=statsNumber(value);return number===null?'—':number.toFixed(4);};
      if(!decision)return {state:'missing',badge:'未确认',detail:'尚未确认正式分；确认并达到人工高分线后才可收入素材库。',canAdmit:false,decisionId:''};
      const status=String(decision.status||'').toLowerCase();
      const statusLabel=status==='corrected'?'已纠偏':status==='confirmed'?'已确认':'未确认';
      const decisionId=String(decision.decisionId||'');
      if(!decisionId)return {state:'invalid',badge:'数据不完整',detail:'最新正式评分缺少 decisionId，暂不可入库。',canAdmit:false,decisionId:''};
      if(status!=='confirmed'&&status!=='corrected')return {state:'pending',badge:'未确认',detail:'最新正式评审批次尚未确认，暂不可入库。',canAdmit:false,decisionId:''};
      const threshold=statsNumber(state.data?.settings?.humanThreshold);
      let ratio=statsNumber(decision.scoreRatio);
      if(ratio===null){
        const total=statsNumber(decision.formalTotal),maximum=statsNumber(maxScore());
        if(total!==null&&maximum!==null&&maximum>0)ratio=total/maximum;
      }
      if(threshold===null)return {state:'invalid',badge:'数据不完整',detail:'人工高分线缺失，暂不可入库。',canAdmit:false,decisionId:''};
      if(ratio===null)return {state:'invalid',badge:'数据不完整',detail:'正式分比例缺失，暂不可入库。',canAdmit:false,decisionId:''};
      if(ratio<threshold)return {state:'low',badge:'未达线',detail:statusLabel+'；正式分比例 '+ratioFmt(ratio)+'，低于人工高分线 '+ratioFmt(threshold)+'。',canAdmit:false,decisionId:''};
      if(!decision.expiresAt)return {state:'invalid',badge:'数据不完整',detail:'正式分有效期缺失，暂不可入库。',canAdmit:false,decisionId:''};
      const expiresAt=Date.parse(String(decision.expiresAt));
      if(!Number.isFinite(expiresAt))return {state:'invalid',badge:'数据不完整',detail:'正式分有效期无效，暂不可入库。',canAdmit:false,decisionId:''};
      if(expiresAt<=Date.now())return {state:'expired',badge:'已过期',detail:'正式分已过期，请重新确认后再入库。',canAdmit:false,decisionId:''};
      return {state:'ready',badge:statusLabel,detail:'正式分比例 '+ratioFmt(ratio)+'，达到人工高分线 '+ratioFmt(threshold)+'。',canAdmit:true,decisionId};
    }
    function admissionHtml(sub){
      if(state.opts.kind!=='generated')return '';
      const view=admissionStateFor(sub.id);
      const action=view.state==='admitted'?''
        :view.canAdmit
          ?'<button class="ga-btn primary" data-action="admit" data-submission="'+esc(sub.id)+'" data-decision-id="'+esc(view.decisionId)+'">收入素材库</button>'
          :'<button class="ga-btn" disabled aria-disabled="true">收入素材库</button>';
      return '<div class="ga-admission" data-admission-status="'+esc(view.state)+'" data-admission-submission="'+esc(sub.id)+'"><div><div class="ga-row"><b>素材库入库</b><span class="ga-pill">'+esc(view.badge)+'</span></div><p>'+esc(view.detail)+'</p></div>'+(action?'<div class="ga-admission-action">'+action+'</div>':'')+'</div>';
    }
    function formalHtml(unitId,subs){
      return subs.map(sub=>{
        const key=String(sub.id),draft=formalDraftFor(key),reasonCodes=new Set((draft.reasons||[]).map(String));
        return '<div class="ga-formal" data-formal-submission="'+esc(key)+'"><div class="ga-formal-head"><b>专职评委正式评分</b><span class="ga-pill">正式确认</span></div><div class="ga-scores">'+dimensions().map(dim=>'<label class="ga-field">'+esc(dim.name||dim.id)+'<input type="number" min="0" max="'+maxScore()+'" step="'+SCORE_STEP+'" inputmode="decimal" data-formal-score="'+esc(key)+'" data-dim="'+esc(dim.id)+'" value="'+esc(scoreValueText(draft.scores?.[dim.id]??''))+'"></label>').join('')+'</div>'
          +'<div class="ga-grid" style="margin-top:8px"><label class="ga-field">正式分类（评委填写）<input data-formal-classification="'+esc(key)+'" value="'+esc(draft.classification||'')+'"></label><label class="ga-field">其他理由<input data-formal-other="'+esc(key)+'" value="'+esc(draft.reasonOther||'')+'"></label></div>'
          +'<div class="ga-reasons">'+(state.data?.settings?.reasonCatalog||[]).map(item=>{const code=String(item.code||item);return '<label class="ga-check"><input type="checkbox" data-formal-reason="'+esc(key)+'" value="'+esc(code)+'" '+(reasonCodes.has(code)?'checked':'')+'> '+esc(item.label||code)+'</label>';}).join('')+'</div>'
          +'<div class="ga-row" style="margin-top:8px"><button class="ga-btn primary" data-action="formal" data-submission="'+esc(key)+'">确认正式分</button></div>'
          +admissionHtml(sub)
          +'</div>';
      }).join('');
    }
    function queueHtml(){
      const data=state.data||{},counts=data.queue||{};
      return '<div class="ga-band"><b>双队列</b><div class="ga-grid" style="margin-top:10px">'+['uploaded','generated'].map(kind=>{
        const track=queueTrack(kind),items=queueItemsWithCurrent(kind);
        const options=items.map(item=>'<option value="'+esc(item.id)+'" '+(state.opts.kind===kind&&String(state.opts.deckId)===String(item.id)?'selected':'')+'>'+esc(item.title||item.name||item.id)+'</option>').join('');
        const more=track.nextCursor!==null?'<div class="ga-row" style="margin-top:8px"><button class="ga-btn" data-action="queue-more" data-queue-more="'+kind+'" '+(track.loading?'disabled aria-busy="true"':'')+'>'+(track.loading?'加载中…':'加载更多')+'</button></div>':'';
        const error=track.error?'<div class="ga-error" data-queue-error="'+kind+'" style="margin-top:8px">'+esc(track.error)+'</div>':'';
        const hasCounts=!!data.queue&&typeof data.queue==='object';
        const countText=key=>{const value=counts[kind]?.[key];return hasCounts&&value!==null&&value!==undefined?esc(value):'—';};
        return '<div class="ga-card" data-queue-track="'+kind+'"><b>'+esc(kind==='generated'?'生成队列':'上传队列')+'</b><div class="ga-muted">排队 '+countText('queued')+' · 执行 '+countText('running')+' · 完成 '+countText('completed')+' · 失败 '+countText('failed')+'</div><label class="ga-field" style="margin-top:8px">选择'+esc(kind==='generated'?'生成作品':'上传作品')+'<select data-queue-kind="'+kind+'">'+(options||'<option value="">暂无作品</option>')+'</select></label>'+more+error+'</div>';
      }).join('')+'</div></div>';
    }
    function stopPreviewHydration(){
      previewHydrationEpoch++;
      previewObserver?.disconnect?.();
      previewObserver=null;
    }
    function holderOwnsFocus(holder){
      const active=global.document?.activeElement;
      return Boolean(active&&(active===holder||holder.contains(active)));
    }
    function previewFailure(holder,{message='预览暂不可用'}={}){
      if(!holder?.isConnected||destroyed)return;
      const ownsFocus=holderOwnsFocus(holder);
      holder.dataset.loaded='0';
      holder.dataset.previewState='error';
      holder.setAttribute('aria-busy','false');
      holder.innerHTML=`<div class="ga-preview-error"><span class="ga-muted" role="status">${esc(message)}</span><button type="button" class="ga-btn" data-action="preview-page-retry">重试预览</button></div>`;
      if(ownsFocus)holder.querySelector('[data-action="preview-page-retry"]')?.focus?.();
    }
    function pagePreviewCurrent(holder,token,requestId){
      return !destroyed
        &&token===previewHydrationEpoch
        &&state.scope==='page'
        &&holder?.isConnected
        &&holder.dataset.previewRequest===requestId;
    }
    function showPagePreviewImage(holder,url,{token,requestId}){
      if(!pagePreviewCurrent(holder,token,requestId))return;
      holder.dataset.loaded='0';
      holder.dataset.previewState='loading';
      holder.setAttribute('aria-busy','true');
      holder.innerHTML='';
      const img=global.document.createElement('img');
      img.alt='页面预览';
      img.decoding='async';
      img.loading='eager';
      img.addEventListener('load',()=>{
        if(!pagePreviewCurrent(holder,token,requestId))return;
        holder.dataset.loaded='1';
        holder.dataset.previewState='loaded';
        holder.setAttribute('aria-busy','false');
      },{once:true});
      img.addEventListener('error',()=>{
        if(!pagePreviewCurrent(holder,token,requestId))return;
        previewFailure(holder,{message:'图片加载失败，请重试'});
      },{once:true});
      holder.appendChild(img);
      img.src=String(url);
    }
    function hydratePreviewBatch(holders,token=previewHydrationEpoch,{retry=false}={}){
      const requests=[];
      for(const holder of holders){
        if(!holder?.isConnected||holder.dataset.loaded==='1'||holder.dataset.previewState==='loading')continue;
        if(holder.dataset.previewState==='error'&&!retry)continue;
        const path=holder.dataset.previewPath;
        const requestId=String(Number(holder.dataset.previewRequest||0)+1);
        holder.dataset.previewRequest=requestId;
        holder.dataset.loaded='0';
        holder.dataset.previewState='loading';
        holder.setAttribute('aria-busy','true');
        holder.innerHTML='<span class="ga-muted">预览加载中…</span>';
        if(!path||typeof global.Cloud?.imageUrl!=='function'){
          previewFailure(holder);
          continue;
        }
        try{
          const request=global.Cloud.imageUrl(path);
          requests.push({holder,request,requestId});
        }catch(_){
          previewFailure(holder);
        }
      }
      for(const {holder,request,requestId} of requests){
        Promise.resolve(request).then(url=>{
          if(!pagePreviewCurrent(holder,token,requestId))return;
          if(!url){previewFailure(holder);return;}
          showPagePreviewImage(holder,url,{token,requestId});
        },()=>{
          if(!pagePreviewCurrent(holder,token,requestId))return;
          previewFailure(holder);
        });
      }
    }
    function retryPagePreview(holder,trigger){
      if(!holder?.isConnected||destroyed||state.scope!=='page'||holder.dataset.previewState!=='error')return;
      if(trigger&&holder.contains(trigger)&&trigger.matches('[data-action="preview-page-retry"]')){
        holder.tabIndex=-1;
        try{holder.focus();}catch(_){ }
      }
      hydratePreviewBatch([holder],previewHydrationEpoch,{retry:true});
    }
    function observePreviews(){
      stopPreviewHydration();
      if(destroyed||state.busy)return;
      if(state.scope==='deck'){void loadDeckPreview();return;}
      if(state.scope!=='page')return;
      const holders=[...root.querySelectorAll('[data-preview-path]')];
      if(!holders.length)return;
      if(typeof global.IntersectionObserver!=='function'){
        hydratePreviewBatch(holders);
        return;
      }
      const token=previewHydrationEpoch;
      const observer=new global.IntersectionObserver(entries=>{
        if(destroyed||token!==previewHydrationEpoch||state.scope!=='page'||previewObserver!==observer)return;
        const visible=[];
        for(const entry of entries){
          if(!entry.isIntersecting)continue;
          observer.unobserve(entry.target);
          if(entry.target?.isConnected)visible.push(entry.target);
        }
        hydratePreviewBatch(visible,token);
      },{root:null,rootMargin:PREVIEW_ROOT_MARGIN,threshold:0});
      previewObserver=observer;
      for(const holder of holders)observer.observe(holder);
    }
    function render(){
      if(comparisonOnly){
        const generated=state.opts.kind==='generated';
        view.innerHTML='<section class="ga-shell" data-professional-comparison-only><div class="ga-row" role="group" aria-label="比较轨道">'
          +'<button class="ga-btn" type="button" data-action="comparison-uploaded" aria-pressed="'+!generated+'">上传素材</button>'
          +'<button class="ga-btn" type="button" data-action="comparison-generated" aria-pressed="'+generated+'">生成作品</button>'
          +'</div><div class="ga-row" style="justify-content:flex-end"><button class="ga-btn" type="button" data-action="export-comparison">导出 AI 对比汇总</button></div>'+statsPanelHtml()+'</section>';
        return;
      }
      stopPreviewHydration();
      if(previewDialog?.open)closePreviewDialog(false);
      const data=state.data||{},deck=data.deck,professional=professionalReviewOnlyMode();
      const units=state.scope==='deck'?[['',0,'整套作品评分']]:images().map((image,index)=>[String(image.id),Number(image.position||index+1),'第 '+Number(image.position||index+1)+' 页 · '+(image.name||image.id)]);
      const pendingSubmit=pendingHumanSubmit();
      const workspaceReady=Boolean(deck&&!state.busy&&selectedUnits().length);
      const canSubmit=Boolean(deck&&!state.busy&&!pendingSubmit&&submissionPlan().ready);
      const submitAttrs=pendingSubmit?'disabled aria-busy="true"':(canSubmit?'':'disabled');
      const submitLabel=humanSubmitLabel(pendingSubmit);
      const submitIntent=professional&&professionalBundleHasSavedFormal()?'revision':'initial';
      const backfill=backfillProgress();
      const kindLabel=state.opts.kind==='generated'?'生成作品':'上传素材';
      const judgeActions=professional?(data.isJudge&&!coverTrainingOnePage()?'<button class="ga-btn" data-action="backfill" '+(backfill.fetching?'disabled aria-busy="true"':'')+'>'+esc(backfillButtonText(backfill))+'</button>':''):(data.isJudge?'<button class="ga-btn" data-action="prepare" '+(workspaceReady?'':'disabled')+'>预评当前粒度</button><button class="ga-btn" data-action="run" data-idle-label="推进 1 个" '+(state.runBusy?'disabled aria-busy="true"':'')+'>'+(state.runBusy?'推进中…':'推进 1 个')+'</button><button class="ga-btn" data-action="backfill" '+(backfill.fetching?'disabled aria-busy="true"':'')+'>'+esc(backfillButtonText(backfill))+'</button>':'');
      const importPanel=professional?'':'<div class="ga-band ga-upload-panel" data-import-panel aria-busy="'+(state.uploadBusy?'true':'false')+'"><div class="ga-head"><div><b>PPT/PPTX 转可预评作品</b><p class="ga-muted">上传 PPT/PPTX，转换完成后加入 AI 评审并进入可预评状态；普通 PDF/图片导入保持不变。</p></div><span class="ga-pill">1–10 份 · 单份 ≤ 25 MiB · 合计 ≤ 100 MiB</span></div><div class="ga-grid" style="margin-top:10px"><label class="ga-field">作品名称（可选）<input data-import-name maxlength="100" value="'+esc(state.uploadName)+'"></label><label class="ga-field">PPT/PPTX 文件<input type="file" accept=".ppt,.pptx" multiple data-import-files '+(state.uploadBusy?'disabled':'')+'></label></div><div class="ga-upload-files" data-import-summary>'+uploadSummaryHtml()+'</div><div class="ga-row" style="margin-top:10px"><button class="ga-btn primary" data-action="import" '+(state.uploadBusy?'disabled aria-busy="true"':'')+'>'+esc(uploadButtonText())+'</button><span class="ga-muted">失败时保留选择，可直接重试</span></div><div class="ga-upload-status" data-import-status role="status" aria-live="polite" aria-atomic="true"></div></div>';
      const settings=professional?'':(data.isJudge?settingsHtml():'');
      const stats=statsPanelHtml();
      const unitCards=professional&&!deck?'<div class="ga-muted" data-professional-loading>正在读取专业评分数据…</div>':units.map(([id,pos,label])=>unitCard(id,pos,label)).join('');
      view.innerHTML='<section class="ga-shell" '+(professional?'data-professional-review-only="true"':'')+'>'
        +'<header class="ga-head"><div><h2>'+esc(professional?'专业人工评分 · '+kindLabel:'AI 盲评 · '+kindLabel)+'</h2><p class="ga-muted">'+esc(professional?(professionalManualOnly()?'仅人工素材只保存人工分，不发送给模型，也不进入 AI 参考样本。':coverTrainingOnePage()?'只需给唯一封面打一次分；提交后由 Sol 独立评审，不会读取你的分数。':'提交整套与选中页人工分；首次有效对比只展示 workspace 已过滤的数据。'):'先预评并持久化；你提交人工分后，仅同版本、同评分标准、同粒度的预测会解锁。')+'</p></div><div class="ga-actions"><button class="ga-btn" type="button" data-action="refresh">刷新</button>'+(coverTrainingOnePage()?'':'<button class="ga-btn" type="button" data-action="switch-kind">切换到'+(state.opts.kind==='generated'?'上传素材':'生成作品')+'</button>')+'<button class="ga-btn primary" type="button" data-action="submit" data-submit-intent="'+submitIntent+'" aria-controls="ga-review-live" '+submitAttrs+'>'+esc(submitLabel)+'</button></div></header>'
        +'<div class="ga-band"><div class="ga-row"><b>'+esc(deck?.data?.title||deck?.data?.name||'暂无作品')+'</b><span class="ga-pill">版本 '+esc((data.deckVersion||'').slice(0,10))+'</span><span class="ga-pill">标准 '+esc((data.rubricVersion||'').slice(0,10))+'</span><span class="ga-pill">'+esc(data.role||'')+'</span></div><div class="ga-pre-evaluation"><span class="ga-pre-evaluation-label">'+(coverTrainingOnePage()?'本页评审':'整套预评')+'</span><span class="ga-pill ga-pre-evaluation-status" data-pre-evaluation-status data-status="'+esc(state.preEvaluationStatus||'unknown')+'" role="status" aria-live="polite" aria-atomic="true">'+esc(preEvaluationText()+(state.preEvaluationFeedback?' · '+state.preEvaluationFeedback:''))+'</span></div><div class="ga-actions" style="margin-top:10px">'+(coverTrainingOnePage()?'':'<div class="ga-row" role="group" aria-label="评分范围"><button class="ga-btn" type="button" data-scope="deck" aria-pressed="'+(state.scope==='deck'?'true':'false')+'" aria-controls="ga-review-units">整套</button><button class="ga-btn" type="button" data-scope="page" aria-pressed="'+(state.scope==='page'?'true':'false')+'" aria-controls="ga-review-units">选页</button></div>')+judgeActions+'</div><div class="ga-review-status" style="margin-top:10px" data-review-status tabindex="-1">'+status()+'</div>'+(data.isJudge&&!coverTrainingOnePage()?backfillStatusHtml():'')+'</div>'
        +(coverTrainingOnePage()?'':professionalReviewNoticeHtml())
        +(data.contractWarnings?.length?'<div class="ga-warn">'+data.contractWarnings.map(esc).join('<br>')+'</div>':'')
        +(coverTrainingOnePage()?'':queueHtml())
        +importPanel
        +(state.scope==='page'?'<p class="ga-muted">'+(coverTrainingOnePage()?'唯一封面已自动选中。填写三项评分后提交。':'勾选需要评分的页面，再提交所选评分。')+'</p>':'')
        +'<div class="ga-grid" id="ga-review-units">'+unitCards+'</div>'
        +settings
        +stats
        +'</section>';
      syncUploadInput(true);
      observePreviews();
    }
    function settingsHtml(){
      const settings=state.data?.settings||{};
      return '<div class="ga-band"><b>生成自评与人工门槛</b><div class="ga-grid" style="margin-top:10px"><label class="ga-field">视觉权重<input type="number" data-setting="visionWeight" value="'+esc(settings.visionWeight??40)+'"></label><label class="ga-field">排版权重<input type="number" data-setting="layoutWeight" value="'+esc(settings.layoutWeight??30)+'"></label><label class="ga-field">匹配权重<input type="number" data-setting="matchWeight" value="'+esc(settings.matchWeight??30)+'"></label><label class="ga-field">人工门槛 0–1<input type="number" min="0" max="1" step="0.01" data-setting="humanThreshold" value="'+esc(settings.humanThreshold??0.85)+'"></label></div><button class="ga-btn" data-action="settings" style="margin-top:10px">保存 AI 配置</button></div>';
    }
    function statsPanelHtml(){
      return '<div class="ga-band"><div class="ga-head"><div><b>固定留出组 MAE</b><div class="ga-muted">'+(professionalComparison()?'按当前轨道汇总；只统计首次有效盲评与符合条件的历史样本，正式改分不重复计入。':'固定留出组只做稳定对比，不把完整历史逐次送模型；训练包有界且只含校准组。MAE 使用人工基准分：有现代配对盲评提交时取盲评提交分，否则沿用旧库原有人工评分；正式纠偏分不参与 MAE，只用于素材库展示与正式评级。')+'</div></div><button class="ga-btn" data-action="stats" '+(statsUi(state.opts.kind).loading?'disabled aria-busy="true"':'')+'>'+(statsUi(state.opts.kind).loading?'统计加载中…':'刷新统计')+'</button></div><div id="ga-stats" aria-busy="'+(statsUi(state.opts.kind).loading?'true':'false')+'" data-stats-kind="'+esc(state.opts.kind)+'">'+statsHtml()+'</div></div>';
    }
    function exportComparisonSummary(){
      const payload={
        format:'guanye-professional-comparison',version:1,
        exportedAt:new Date().toISOString(),
        byKind:{uploaded:statsByKind.get('uploaded')||null,generated:statsByKind.get('generated')||null},
      };
      if(!payload.byKind.uploaded||!payload.byKind.generated)throw new Error('两类对比统计尚未加载完成');
      const doc=global.document;
      if(!doc||typeof Blob!=='function'||typeof URL?.createObjectURL!=='function')throw new Error('当前浏览器不支持导出');
      const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json;charset=utf-8'});
      const href=URL.createObjectURL(blob),link=doc.createElement('a');
      link.href=href;link.download='观页-AI对比汇总-'+new Date().toISOString().slice(0,10)+'.json';link.rel='noopener';
      doc.body.appendChild(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(href),1000);
    }
    const statsByKind=new Map();
    const statsUiByKind={uploaded:{loading:false,error:'',requestEpoch:0},generated:{loading:false,error:'',requestEpoch:0}};
    let statsEpoch=0;
    function statsUi(kind){
      return statsUiByKind[!professionalComparison()&&kind==='generated'?'generated':'uploaded'];
    }
    function statsNumber(value){
      if(value===null||value===undefined||value==='')return null;
      const number=Number(value);
      return Number.isFinite(number)?number:null;
    }
    const statsFmt=value=>{const number=statsNumber(value);return number===null?'—':number.toFixed(4);};
    function scopedStats(kind){
      const payload=statsByKind.get(kind);
      const scoped=payload?.byKind?.[kind];
      return scoped&&typeof scoped==='object'&&!Array.isArray(scoped)?scoped:null;
    }
    function statsDimensions(kind){
      const available=comparisonOnly
        ?Array.from(new Set(['baseline','assisted'].flatMap(variant=>Object.keys(scopedStats(kind)?.[variant]?.dimensions||{}))))
          .map(id=>({id,name:({visual:'视觉',vision:'视觉',layout:'排版',match:'匹配'})[id]||id}))
        :dimensions();
      const labels=new Map(available.map(dim=>[String(dim.id),String(dim.name||dim.label||dim.id)]));
      const visualDimension=available.find(dim=>String(dim.id)==='visual')||available.find(dim=>String(dim.id)==='vision');
      if(kind==='generated')return [
        {key:'visual',aliases:['visual','vision'],label:labels.get('visual')||labels.get('vision')||'视觉'},
        {key:'layout',aliases:['layout'],label:labels.get('layout')||'排版'},
        {key:'match',aliases:['match'],label:labels.get('match')||'匹配'},
      ];
      const definitions=[],seen=new Set();
      for(const dim of available){
        const id=String(dim.id??'');
        if(!id)continue;
        const key=id==='vision'?'visual':id;
        if(seen.has(key))continue;
        seen.add(key);
        const label=key==='visual'
          ?String(visualDimension?.name||visualDimension?.label||labels.get('visual')||labels.get('vision')||'视觉')
          :String(dim.name||dim.label||id);
        definitions.push({key,aliases:key==='visual'?['visual','vision']:[id],label});
      }
      return definitions;
    }
    function statsDimensionValue(metrics,definition){
      const values=metrics?.dimensions;
      if(!values||typeof values!=='object'||Array.isArray(values))return null;
      for(const key of [definition.key,...definition.aliases]){
        if(!Object.prototype.hasOwnProperty.call(values,key))continue;
        const number=statsNumber(values[key]);
        if(number!==null)return number;
      }
      return null;
    }
    function statsPairsHtml(stats){
      const pairs=statsNumber(stats?.holdoutUnits);
      return '<div class="ga-stats-meta"><span class="ga-pill ga-stats-pairs">配对评分单元 <b data-stats-pairs>'+esc(pairs===null?'—':pairs)+'</b> 个</span><span class="ga-muted">按评分单元计数，不是作品套数</span></div>';
    }
    function statsSourceDedup(kind,stats){
      if(kind!=='uploaded')return null;
      const source=stats?.sourceDedup;
      if(!source||typeof source!=='object'||Array.isArray(source))return null;
      const keys=['knownGroups','unknownDecks','excludedDuplicateDecks'];
      if(!keys.every(key=>Object.prototype.hasOwnProperty.call(source,key)))return null;
      const values=keys.map(key=>source[key]);
      if(!values.every(value=>Number.isSafeInteger(value)&&value>=0))return null;
      return {knownGroups:values[0],unknownDecks:values[1],excludedDuplicateDecks:values[2]};
    }
    function statsSourceDedupHtml(kind,stats){
      const dedup=statsSourceDedup(kind,stats);
      if(!dedup)return '';
      const counts='已确认来源 '+dedup.knownGroups+' 组 · 来源未知 '+dedup.unknownDecks+' 套 · 排除重复 '+dedup.excludedDuplicateDecks+' 套';
      const note='同一已确认原文件只保留一套；来源未知的历史作品按原记录计入。';
      const spoken='已确认来源 '+dedup.knownGroups+' 组，来源未知 '+dedup.unknownDecks+' 套，排除重复 '+dedup.excludedDuplicateDecks+' 套。'+note;
      return '<p class="ga-muted" data-stats-source-dedup><span class="ga-sr-only">'+esc(spoken)+'</span><span aria-hidden="true"><span data-stats-source-counts>'+esc(counts)+'</span><br><span data-stats-source-note>'+esc(note)+'</span></span></p>';
    }
    function statsStatusHtml(ui,hasCache){
      const parts=[];
      if(ui.loading)parts.push('<div class="ga-stats-status" data-stats-loading role="status" aria-live="polite">'+(hasCache?'正在刷新统计…':'正在加载统计…')+'</div>');
      if(ui.error)parts.push('<div class="ga-stats-status ga-error" data-stats-error role="status" aria-live="polite"><span>统计刷新失败：'+esc(ui.error)+(hasCache?'；以下为上次成功结果':'')+'</span><button class="ga-btn" type="button" data-action="stats" data-stats-retry>重试</button></div>');
      return parts.join('');
    }
    function statsImprovementHtml(stats){
      const baseline=statsNumber(stats?.baseline?.mae),assisted=statsNumber(stats?.assisted?.mae);
      const improvement=statsNumber(stats?.improvement)??(baseline!==null&&assisted!==null?baseline-assisted:null);
      const formula='改善值（'+aiVariantLabel('baseline')+' − '+aiVariantLabel('assisted')+'）';
      if(improvement===null)return '<p class="ga-stats-improvement" data-stats-improvement><span>'+esc(formula)+'：—</span></p>';
      let reading;
      if(improvement>0)reading=aiVariantLabel('assisted')+'比'+aiVariantLabel('baseline')+'改善 '+statsFmt(improvement);
      else if(improvement<0)reading=aiVariantLabel('assisted')+'比'+aiVariantLabel('baseline')+'变差 '+statsFmt(Math.abs(improvement));
      else reading=aiVariantLabel('assisted')+'与'+aiVariantLabel('baseline')+'误差相同（0.0000）';
      return '<p class="ga-stats-improvement" data-stats-improvement><span>'+esc(formula)+'：'+statsFmt(improvement)+'</span> · '+esc(reading)+'</p>';
    }
    function statsHtml(){
      const kind=state.opts.kind,ui=statsUi(kind),stats=scopedStats(kind);
      if(!stats){
        return statsStatusHtml(ui,false)+(ui.loading||ui.error?'':'<div class="ga-muted" style="margin-top:10px">尚未加载统计。点击“刷新统计”加载当前轨道。</div>');
      }
      const pairs=statsNumber(stats.holdoutUnits);
      const status=statsStatusHtml(ui,true);
      if(pairs===0){
        return status+statsPairsHtml(stats)+statsSourceDedupHtml(kind,stats)+'<div class="ga-stats-zero" data-stats-zero><b>尚无配对样本</b><p>当前轨道还没有同时具备基线预评和辅助预评的同一人工评分单元；完成配对后再计算误差与改善。</p></div>';
      }
      const row=(key,label,baseline,assisted)=>'<tr data-stat-dimension="'+esc(key)+'"><th scope="row">'+esc(label)+'</th><td class="ga-stats-number">'+statsFmt(baseline)+'</td><td class="ga-stats-number">'+statsFmt(assisted)+'</td></tr>';
      const rows=[row('overall','综合',stats.baseline?.mae,stats.assisted?.mae)]
        .concat(statsDimensions(kind).map(definition=>row(definition.key,definition.label,statsDimensionValue(stats.baseline,definition),statsDimensionValue(stats.assisted,definition))));
      return status+statsPairsHtml(stats)+statsSourceDedupHtml(kind,stats)
        +'<p class="ga-stats-note" id="ga-stats-metric-note">误差为按满分归一化的 MAE：0 表示与人工基准分一致；数值越小越接近人工分，即这里的人工基准分。各维度按该维度全部配对误差平均，综合（总体）为各配对评分单元 MAE 的平均。</p>'
        +'<div class="ga-stats-scroll"><table class="ga-table ga-stats-table" aria-describedby="ga-stats-metric-note"><caption>当前轨道的留出配对评分单元汇总</caption><thead><tr><th scope="col">评分项</th><th scope="col">'+esc(aiVariantLabel('baseline'))+'</th><th scope="col">'+esc(aiVariantLabel('assisted'))+'</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>'
        +statsImprovementHtml(stats);
    }
    function updateStatsDom({focusRefresh=false}={}){
      const ui=statsUi(state.opts.kind);
      const container=typeof root.querySelector==='function'?root.querySelector('#ga-stats'):null;
      if(container){
        container.innerHTML=statsHtml();
        container.setAttribute?.('aria-busy',ui.loading?'true':'false');
      }
      const button=typeof root.querySelector==='function'?root.querySelector('[data-action="stats"]'):null;
      if(button){
        button.disabled=ui.loading;
        button.textContent=ui.loading?'统计加载中…':'刷新统计';
        if(ui.loading)button.setAttribute?.('aria-busy','true');else button.removeAttribute?.('aria-busy');
        if(focusRefresh)button.focus?.({preventScroll:true});
      }
    }
    async function loadStats({focusRefresh=false}={}){
      const kind=state.opts.kind,ui=statsUi(kind);
      if(professionalComparison()&&ui.loading)return;
      const epoch=++statsEpoch;
      ui.requestEpoch=epoch;ui.loading=true;ui.error='';
      updateStatsDom();
      try{
        const aggregate=professionalComparison();
        const payload=await request(AI_PATH+'?action=comparison'+(aggregate?'':'&kind='+encodeURIComponent(kind)));
        if(destroyed||epoch!==statsEpoch||!aggregate&&kind!==state.opts.kind)return;
        const scoped=payload?.byKind?.[kind];
        if(!scoped||typeof scoped!=='object'||Array.isArray(scoped))throw new Error('统计返回数据缺少当前轨道');
        for(const track of aggregate?['uploaded','generated']:[kind])statsByKind.set(track,payload);
      }catch(err){
        if(destroyed||epoch!==statsEpoch||kind!==state.opts.kind)return;
        ui.error=err?.message||String(err)||'统计刷新失败，请重试';
      }finally{
        if(ui.requestEpoch===epoch){
          ui.loading=false;
          if(!destroyed&&(professionalComparison()||kind===state.opts.kind))updateStatsDom({focusRefresh:focusRefresh&&kind===state.opts.kind});
        }
      }
    }
    async function persistDraftInput(target){
      const unit=target.dataset.scoreUnit??target.dataset.noteUnit??target.dataset.suggestUnit??target.dataset.correctUnit??target.dataset.otherUnit??target.dataset.reasonUnit;
      if(unit===undefined)return;
      const draftKey=state.scope+':'+unitKey(unit),draft=draftFor(unit);
      if(target.dataset.dim){
        draft.scores[target.dataset.dim]=target.value===''?undefined:Number(target.value);
        if(target.matches('[data-score-unit]'))updateScoreEcho(target);
      }
      else if(target.hasAttribute('data-note-unit'))draft.note=target.value;
      else if(target.hasAttribute('data-suggest-unit'))draft.classificationSuggestion=target.value;
      else if(target.hasAttribute('data-correct-unit'))draft.classificationCorrection=target.value;
      else if(target.hasAttribute('data-other-unit'))draft.reasonOther=target.value;
      else if(target.hasAttribute('data-reason-unit')){draft.reasons=draft.reasons||[];if(target.checked&&!draft.reasons.includes(target.value))draft.reasons.push(target.value);if(!target.checked)draft.reasons=draft.reasons.filter(value=>value!==target.value);}
      markDraftDirty(draftKey);
      if(singleJudgeMode())updateSubmissionDom();
    }
    async function submitHuman(){
      const workKey=currentDeckKey();
      if(pendingHumanSubmit(workKey))return false;
      const bundle=singleJudgeMode(),plan=submissionPlan(),submittedScope=state.scope,revising=professionalReviewOnlyMode()&&professionalBundleHasSavedFormal();
      if(!plan.ready)throw new Error(plan.reason||'请选择至少一个评分单元');
      const submittedUnits=plan.units;
      const payload=bundle
        ?{deckId:state.opts.deckId,deckVersion:state.data?.deckVersion,rubricVersion:state.data?.rubricVersion,units:submittedUnits.map(unit=>unit.entry)}
        :{deckId:state.opts.deckId,scope:submittedScope,deckVersion:state.data?.deckVersion,rubricVersion:state.data?.rubricVersion,units:submittedUnits.map(unit=>unit.entry)};
      const submitAction=bundle?'submit-bundle':'submit';
      const callId=++humanSubmitSequence;
      const submitControl=root.querySelector('[data-action="submit"]');
      const restoreSubmitFocus=Boolean(submitControl&&global.document?.activeElement===submitControl);
      let userMovedFocus=false,successMessage='';
      const onFocusIn=event=>{if(!submitControl?.contains?.(event.target))userMovedFocus=true;};
      if(restoreSubmitFocus)global.document?.addEventListener?.('focusin',onFocusIn,true);
      humanSubmitPending.set(workKey,{callId,phase:'posting'});
      updateSubmissionDom();
      announceReviewStatus(revising?'正在保存正式改分…':'正在提交人工评分…');
      try{
        const result=await request(AI_PATH+'?action='+submitAction,{method:'POST',body:payload});
        if(destroyed)return false;
        const pending=pendingHumanSubmit(workKey);
        if(!pending||pending.callId!==callId||currentDeckKey()!==workKey)return false;
        pending.phase='reloading';
        updateSubmissionDom();
        announceReviewStatus(revising?'改分已保存，正在刷新…':'评分已保存，正在刷新…');
        state.message=result.message||(revising?'正式人分改分已保存':'已提交人工评分');
        successMessage=state.message;
        const movedFocus=userMovedFocus?focusDescriptor(global.document?.activeElement):null;
        await load({
          focusRestore:movedFocus,
          keepMessage:true,
          beforeRender:()=>{if(!state.error&&currentDeckKey()===workKey)discardSubmittedDrafts(submittedUnits);},
        });
        if(destroyed||currentDeckKey()!==workKey)return false;
        try{opts.onHumanSaved?.();}catch{}
        return true;
      }catch(err){
        const message=err?.message||String(err)||'提交失败，请重试';
        if(!destroyed&&currentDeckKey()===workKey){
          state.error=message;
          announceReviewStatus(message);
        }
        return false;
      }finally{
        const cleared=clearPendingHumanSubmit(workKey,callId);
        if(cleared&&!destroyed&&currentDeckKey()===workKey)updateSubmissionDom();
        if(!destroyed&&currentDeckKey()===workKey){
          if(successMessage)announceReviewStatus(successMessage);
          if(restoreSubmitFocus&&!userMovedFocus){
            if(successMessage)focusReviewStatus();
            else if(cleared)focusSubmitControl();
          }
        }
        if(restoreSubmitFocus)global.document?.removeEventListener?.('focusin',onFocusIn,true);
      }
    }
    async function prepare(){
      if(professionalReviewOnlyMode())return false;
      const body={deckId:state.opts.deckId,scope:state.scope};if(state.scope==='page')body.unitIds=[...state.selected];
      const result=await request(AI_PATH+'?action=prepare',{method:'POST',body});state.message='已准备 '+((result.jobs||[]).length)+' 个预评任务';await load();
    }
    async function runQueue(){
      if(professionalReviewOnlyMode())return false;
      if(state.runBusy)return;
      if(!state.opts.deckId){
        state.preEvaluationFeedback='请先选择一套作品再推进';
        updatePreEvaluationDom();
        return;
      }
      const workspaceToken=workspaceEpoch;
      const deckKey=currentDeckKey();
      const runToken=++preEvaluationEpoch;
      clearPreEvaluationTimer();
      preEvaluationDelay=PRE_EVALUATION_POLL_MS;
      preEvaluationHintShown=false;
      state.runBusy=true;
      state.preEvaluationFeedback='正在推进 1 个预评任务…';
      updatePreEvaluationDom();
      updateRunButtonBusy();
      try{
        const result=await request(AI_PATH+'?action=run',{method:'POST',body:{kind:state.opts.kind,drain:false,limit:1}});
        if(!statusRequestIsCurrent(workspaceToken,runToken,deckKey))return;
        const completed=Number(result?.completed||0),failed=Number(result?.failed||0);
        state.preEvaluationFeedback=(completed||failed)?'已推进：完成 '+completed+'，失败 '+failed:'已提交一次推进';
        const refreshed=await refreshPreEvaluationStatus({resetBackoff:true,workspaceToken});
        if(!destroyed&&workspaceEpoch===workspaceToken&&currentDeckKey()===deckKey){
          state.preEvaluationFeedback=refreshed?'推进请求已完成，状态已刷新':'推进请求已提交，状态稍后自动刷新';
          updatePreEvaluationDom();
        }
      }catch(_){
        if(!destroyed&&workspaceEpoch===workspaceToken&&currentDeckKey()===deckKey){
          state.preEvaluationFeedback='推进暂时不可用，请稍后重试';
          updatePreEvaluationDom();
          preEvaluationDelay=Math.min(PRE_EVALUATION_POLL_MS*2,PRE_EVALUATION_POLL_MAX_MS);
          if(isPreEvaluationActive())schedulePreEvaluationPoll();
        }
      }finally{
        if(!destroyed&&workspaceEpoch===workspaceToken&&currentDeckKey()===deckKey){
          state.runBusy=false;
          updateRunButtonBusy();
        }
      }
    }
    async function requestQueuePage(kind,cursor){
      const query=new URLSearchParams({action:'queue-decks',kind,cursor});
      const payload=await request(AI_PATH+'?'+query.toString());
      if(!payload||typeof payload!=='object'||!Array.isArray(payload.items))throw new Error('队列分页返回数据无效');
      if(payload.kind&&String(payload.kind)!==kind)throw new Error('队列分页返回类型不匹配');
      return {items:normalizeQueueItems(kind,payload.items),nextCursor:normalizeQueueCursor(payload.nextCursor)};
    }
    async function loadMoreQueue(kind){
      const track=queueTrack(kind);
      if(track.loading||track.nextCursor===null)return;
      const cursor=track.nextCursor,token=queueEpoch;
      track.loading=true;track.error='';render();
      try{
        const page=await requestQueuePage(kind,cursor);
        if(destroyed||token!==queueEpoch)return;
        track.items=appendQueueItems(track.items,page.items);
        track.nextCursor=page.nextCursor;
        track.loadedMore=true;
        track.error='';
      }catch(err){
        if(!destroyed&&token===queueEpoch){
          track.nextCursor=cursor;
          track.error=err?.message||String(err)||'加载更多失败，请重试';
        }
      }finally{
        if(!destroyed&&token===queueEpoch){
          track.loading=false;
          render();
        }
      }
    }
    async function backfill(){
      const kind=state.opts.kind;
      const progress=backfillProgress(kind);
      if(progress.fetching)return;
      if(progress.complete){
        progress.offset=0;progress.totalDecks=null;progress.scannedDecks=0;progress.preparedUnits=0;progress.complete=false;progress.error='';
      }
      const offset=Math.max(0,Number(progress.offset)||0);
      progress.fetching=true;progress.error='';
      render();
      try{
        const result=await request(AI_PATH+'?action=backfill',{method:'POST',body:{kind,offset,limit:8}});
        const decks=Number(result?.decks),preparedUnits=Number(result?.preparedUnits),totalDecks=Number(result?.totalDecks);
        const rawNextOffset=result?.nextOffset;
        const nextOffset=rawNextOffset===null?null:Number(rawNextOffset);
        if(!Number.isFinite(decks)||decks<0||!Number.isFinite(preparedUnits)||preparedUnits<0||!Number.isFinite(totalDecks)||totalDecks<0||(nextOffset!==null&&(!Number.isFinite(nextOffset)||nextOffset<0)))throw new Error('补档返回数据无效');
        progress.scannedDecks+=decks;
        progress.preparedUnits+=preparedUnits;
        progress.totalDecks=totalDecks;
        progress.complete=nextOffset===null;
        progress.offset=nextOffset===null?Math.max(offset+decks,progress.scannedDecks):nextOffset;
        progress.error='';
      }catch(err){
        progress.error=err?.message||String(err)||'请求失败';
      }finally{
        progress.fetching=false;
        if(!destroyed&&kind===state.opts.kind)render();
      }
    }
    async function formal(submission){
      if(professionalReviewOnlyMode())return false;
      const requestDeckKey=currentDeckKey(),requestScope=state.scope,draftKey=formalDraftKey(submission),draftRevision=state.formalRevisions[draftKey]||0;
      const draft=formalDraftFor(submission),scores={},missing=[];
      for(const dim of dimensions()){
        const value=statsNumber(draft.scores?.[dim.id]);
        if(value===null)missing.push(String(dim.name||dim.id));
        else if(value<0||value>maxScore())throw new Error('正式评分超出范围：'+String(dim.name||dim.id));
        else scores[dim.id]=value;
      }
      if(missing.length)throw new Error('请完整填写正式评分：'+missing.join('、'));
      const reasons=[...new Set((draft.reasons||[]).map(String))];
      const reasonOther=String(draft.reasonOther||'').trim();
      if(reasons.includes('other')&&!reasonOther)throw new Error('选择“其他”时请填写原因');
      const body={submissionId:submission,formalScores:scores,classification:draft.classification||null,reasons,reasonOther,summary:draft.summary||'',status:'confirmed'};
      const result=await request(AI_PATH+'?action=formal',{method:'POST',body});
      if(destroyed||currentDeckKey()!==requestDeckKey)return;
      const revisionStillCurrent=(state.formalRevisions[draftKey]||0)===draftRevision;
      if(revisionStillCurrent&&state.formalDrafts[draftKey])state.formalDirty[draftKey]=false;
      if(state.scope!==requestScope)return;
      if(revisionStillCurrent)forgetFormalDraft(submission);
      await load();
      if(destroyed||currentDeckKey()!==requestDeckKey||state.scope!==requestScope)return;
      state.message='正式评分已确认，总分 '+result.formalTotal+(state.opts.kind==='generated'?'；尚未收入素材库':'');
      render();
    }
    const admissionUnitKey=unitId=>currentDeckKey()+':'+state.scope+':'+unitKey(unitId);
    function professionalAdmission(unitId){
      const data=state.data;
      if(!data?.deck||!data.isJudge||state.opts.kind!=='generated'||!data.deckVersion||!data.rubricVersion)return null;
      const submissions=professionalSubmissionsFor(unitId,state.scope),decisions=submissions.flatMap(sub=>decisionsFor(sub.id).map(decision=>({sub,decision})));
      if(admissionReceipts.has(admissionUnitKey(unitId))||decisions.some(({decision})=>decision.libraryVisible===true)){
        return {status:'admitted',message:'已收入素材库 · AI 自我学习生成'};
      }
      // M27 returns decisions newest-first within each submission, with version binding via submissionId.
      const current=decisions[0];
      if(!current)return null;
      const {sub,decision}=current;
      if(state.scope==='page'&&!state.selected.has(String(unitId)))return {status:'unselected',message:'选中此页后可确认该页入库。'};
      if((state.draftRevisions[state.scope+':'+unitKey(unitId)]||0)>0)return {status:'draft',message:'请先保存改分，再确认入库。'};
      if(admissionPending.has(currentDeckKey())||state.busy||pendingHumanSubmit())return {status:'pending',message:'正在更新正式评分或入库状态…'};
      if(state.error)return {status:'unavailable',message:'请先刷新正式评分，再确认入库。'};
      if(!sub.createdAt||!sub.id||!decision.decisionId||!['confirmed','corrected'].includes(decision.status)
        ||decisions.some(row=>row.sub.id!==sub.id&&row.sub.createdAt===sub.createdAt))return {status:'unavailable',message:'无法确认唯一的最新正式评分，请刷新后重试。'};
      const human=completeCurrentDimensionScores(sub.entry?.scores),formal=completeCurrentDimensionScores(decision.formalScores);
      const threshold=statsNumber(data.settings?.humanThreshold),ratio=statsNumber(decision.scoreRatio),expiry=Date.parse(decision.expiresAt||'');
      if(!human||!formal||!Object.keys(formal).every(key=>formal[key]===human[key])||threshold===null||ratio===null||!Number.isFinite(expiry)){
        return {status:'unavailable',message:'正式人分、门槛或有效期无法核对，请刷新后重试。'};
      }
      if(expiry<=Date.now())return {status:'expired',message:'最新正式评分已过期，不能入库。'};
      if(ratio<threshold)return {status:'low',message:'最新正式人工评分未达到入库门槛。'};
      return {status:'eligible',message:'最新正式人工评分已达线，可手动确认入库。',submissionId:sub.id,decisionId:decision.decisionId};
    }
    function professionalAdmissionHtml(unitId){
      const admission=professionalAdmission(unitId);
      if(!admission)return '';
      return '<div class="ga-admission" data-admission-status="'+esc(admission.status)+'"><div><b>素材库入库</b><p>'+esc(admission.message)+'</p></div>'
        +(admission.status==='eligible'?'<button type="button" class="ga-btn primary" data-action="admit" data-admit-unit="'+esc(unitId)+'" data-submission="'+esc(admission.submissionId)+'" data-decision-id="'+esc(admission.decisionId)+'">收入素材库</button>':'')+'</div>';
    }
    async function admitProfessional(button){
      const unitId=button.dataset.admitUnit,admission=professionalAdmission(unitId);
      if(admission?.status!=='eligible'||admission.submissionId!==button.dataset.submission||admission.decisionId!==button.dataset.decisionId)return;
      const workKey=currentDeckKey(),unit=admissionUnitKey(unitId);
      const stillCurrent=()=>!destroyed&&workKey===currentDeckKey();
      admissionPending.add(workKey);updateSubmissionDom();
      try{
        const result=await request(AI_PATH+'?action=admit',{method:'POST',body:{submissionId:admission.submissionId,decisionId:admission.decisionId}});
        admissionReceipts.add(unit);
        let refreshFailed=false;
        try{await opts.onAdmissionSaved?.();}catch{refreshFailed=true;}
        if(!stillCurrent())return;
        state.message=(result.idempotent?'此评分单元已收入素材库':'已收入素材库')+' · AI 自我学习生成'+(refreshFailed?'；原库刷新失败，请重新加载页面。':'');
        await load({keepMessage:true});
        if(stillCurrent()){
          announceReviewStatus(state.message+(state.error?'；评审数据刷新失败。':''));
          root.querySelector('[data-review-status]')?.focus({preventScroll:true});
        }
      }finally{
        admissionPending.delete(workKey);
        if(stillCurrent())updateSubmissionDom();
      }
    }
    async function admit(submission,decisionId){
      if(professionalReviewOnlyMode())return false;
      if(!decisionId)throw new Error('缺少正式评审批次，请刷新后重试');
      const body={submissionId:submission,decisionId};
      const result=await request(AI_PATH+'?action=admit',{method:'POST',body});
      await load();
      state.message=result.idempotent?'该正式评分已收入素材库':'已收入素材库 · AI 自我学习生成';
      render();
    }
    async function saveSettings(){
      if(professionalReviewOnlyMode())return false;
      const body={};root.querySelectorAll('[data-setting]').forEach(input=>{body[input.dataset.setting]=Number(input.value);});
      await request(AI_PATH+'?action=settings',{method:'POST',body});state.message='AI 配置已保存';await load();
    }
    async function importPpt(){
      if(professionalReviewOnlyMode())return false;
      if(state.uploadBusy)return false;
      const files=[...state.uploadFiles];
      const validation=uploadValidationMessage(files);
      if(validation){state.uploadError=validation;state.uploadMessage='';state.uploadPhase='';updateUploadDom();return false;}
      const token=++importEpoch,startEpoch=workspaceEpoch,startDeckKey=currentDeckKey();
      state.uploadBusy=true;state.uploadError='';state.uploadMessage='';state.uploadPhase='正在校验并准备文件…';updateUploadDom();
      try{
        const payload=[];
        for(let index=0;index<files.length;index++){
          const file=files[index];
          state.uploadPhase='正在读取 '+(index+1)+' / '+files.length+' · '+file.name;updateUploadDom();
          payload.push({name:file.name,data:await readUploadFile(file)});
        }
        const total=files.reduce((sum,file)=>sum+Math.max(0,Number(file.size)||0),0);
        state.uploadPhase='正在上传 '+files.length+' 份文件 · '+formatMiB(total)+' MiB…';updateUploadDom();
        const result=await request(IMPORT_PATH,{method:'POST',body:{name:String(state.uploadName||'').trim(),files:payload}});
        if(!result?.deckId)throw uploadValidationError('上传完成但未返回作品信息，请刷新上传队列后重试');
        if(destroyed)return false;
        const current=token===importEpoch&&workspaceEpoch===startEpoch&&currentDeckKey()===startDeckKey;
        state.uploadFiles=[];
        state.uploadError='';
        if(current){
          state.uploadMessage='上传完成，正在打开新作品…';updateUploadDom();
          await openDeck('uploaded',result.deckId);
        }else{
          state.uploadMessage='上传完成，但当前队列已切换，未自动打开新作品；刷新上传队列后可查看。';
        }
        return {result,stale:!current};
      }catch(error){
        if(destroyed)return false;
        state.uploadError=friendlyUploadError(error);state.uploadPhase='';state.uploadMessage='';
        return false;
      }finally{
        if(!destroyed){state.uploadBusy=false;state.uploadPhase='';updateUploadDom();}
      }
    }
    async function handleClick(event){
      if(comparisonOnly){
        const action=event.target.closest('[data-action]')?.dataset.action;
        if(action==='stats')await loadStats({focusRefresh:true});
        if(action==='export-comparison'){
          try{exportComparisonSummary();}catch(error){console.warn('无法导出 AI 对比汇总',error);}
          return;
        }
        if(['comparison-uploaded','comparison-generated'].includes(action)){
          const target=action==='comparison-generated'?'generated':'uploaded';
          if(target!==state.opts.kind){
            state.opts.kind=target;render();
            root.querySelector('[data-action="'+action+'"]')?.focus();
            if(!statsByKind.has(target)&&!statsUi(target).loading)await loadStats();
          }
        }
        return;
      }
      const quick=event.target.closest('[data-quick-unit]');
      if(quick){
        const draft=draftFor(quick.dataset.quickUnit),draftKey=state.scope+':'+unitKey(quick.dataset.quickUnit),value=Number(quick.dataset.quickValue),input=scoreInputFor(quick.dataset.quickUnit,quick.dataset.quickDim);
        draft.scores[quick.dataset.quickDim]=value;
        markDraftDirty(draftKey);
        if(input){input.value=scoreValueText(value);updateScoreEcho(input);}
        if(singleJudgeMode())updateSubmissionDom();
        return;
      }
      const action=event.target.closest('[data-action]')?.dataset.action;const scope=event.target.closest('[data-scope]')?.dataset.scope;const statsRetry=action==='stats'&&Boolean(event.target.closest('[data-stats-retry]'));const decisionId=event.target.closest('[data-decision-id]')?.dataset.decisionId||'';
      if(action==='preview-prev'||action==='preview-next'){
        changeDeckPreview(action==='preview-prev'?-1:1);
        return;
      }
      if(action==='preview-page-retry'){
        retryPagePreview(
          event.target.closest('[data-preview-path]'),
          event.target.closest('[data-action="preview-page-retry"]'),
        );
        return;
      }
      if(action==='preview-retry'){
        void loadDeckPreview();
        return;
      }
      if(action==='preview-zoom'){
        openPreviewDialog(event.target.closest('[data-action="preview-zoom"]'));
        return;
      }
      if(action==='preview-rate-page'){
        const deckKey=ensureSelectionDeck(),image=currentPreviewImage(),pageId=String(image?.id??'').trim();
        if(!pageId)return;
        state.selected.add(pageId);
        switchReviewScope('page');
        schedulePageRatingFocus(deckKey,pageId);
        return;
      }
      if(scope){
        const nextScope=scope==='page'?'page':'deck';
        switchReviewScope(nextScope);
        focusScopeControl(nextScope);
        return;
      }
      if(!action)return;
      if(professionalActionUnavailable(action)){state.message='当前专业版阶段尚未接入此操作';render();return;}
      preEvaluationDelay=PRE_EVALUATION_POLL_MS;preEvaluationHintShown=false;
      const actionEpoch=workspaceEpoch;
      state.error='';try{
        if(action==='refresh')await load({resetQueues:true});
        else if(action==='switch-kind')await openDeck(state.opts.kind==='generated'?'uploaded':'generated');
        else if(action==='queue-more')await loadMoreQueue(event.target.closest('[data-queue-more]')?.dataset.queueMore);
        else if(action==='submit')await submitHuman();else if(action==='prepare')await prepare();else if(action==='run')await runQueue();else if(action==='backfill')await backfill();else if(action==='stats')await loadStats({focusRefresh:statsRetry});else if(action==='settings')await saveSettings();else if(action==='import')await importPpt();
        else if(action==='formal')await formal(event.target.closest('[data-submission]').dataset.submission);else if(action==='admit'){
          if(professionalReviewOnlyMode())await admitProfessional(event.target.closest('[data-action="admit"]'));
          else await admit(event.target.closest('[data-submission]').dataset.submission,decisionId);
        }
      }catch(err){
        if(destroyed||actionEpoch!==workspaceEpoch)return;
        if(action==='run'){
          state.runBusy=false;state.preEvaluationFeedback='推进暂时不可用，请稍后重试';updatePreEvaluationDom();updateRunButtonBusy();
        }else{
          state.error=err.message||String(err);render();
          if(action==='submit'){announceReviewStatus(state.error);focusSubmitControl();}
          if(action==='admit'&&professionalReviewOnlyMode()){announceReviewStatus(state.error);root.querySelector('[data-review-status]')?.focus({preventScroll:true});}
        }
      }
    }
    async function handleChange(event){
      const target=event.target;
      if(target.matches('[data-import-files]')){
        if(state.uploadBusy)return;
        state.uploadFiles=[...target.files];state.uploadError='';state.uploadMessage='';state.uploadPhase='';updateUploadDom();
        return;
      }
      if(target.matches('[data-queue-kind]')){
        await openDeck(target.dataset.queueKind,target.value||'');
        return;
      }
      if(target.matches('[data-select-unit]')){
        ensureSelectionDeck();
        const id=String(target.dataset.selectUnit);if(target.checked)state.selected.add(id);else state.selected.delete(id);
        const card=target.closest('[data-unit-card]');if(card)card.classList.toggle('selected',target.checked);
        updateSubmissionDom();
        return;
      }
      if(updateFormalDraftField(target))return;
      await persistDraftInput(target);
    }
    async function handleInput(event){
      const target=event.target;
      if(target.matches('[data-import-name]')){state.uploadName=target.value;return;}
      if(updateFormalDraftField(target))return;
      if(target.matches('input[type="number"],textarea,input[data-suggest-unit],input[data-correct-unit],input[data-other-unit]'))await persistDraftInput(target);
    }
    root.addEventListener('click',handleClick);
    root.addEventListener('change',handleChange);
    root.addEventListener('input',handleInput);
    if(comparisonOnly){render();loadStats();}else load();
    function destroy(){
      if(destroyed)return;
      destroyed=true;
      workspaceEpoch++;
      queueEpoch++;
      preEvaluationEpoch++;
      deckPreviewEpoch++;
      humanSubmitPending.clear();
      stopPreviewHydration();
      clearPreEvaluationTimer();
      closePreviewDialog(false);
      previewDialog?.remove?.();
      previewDialog=null;
      root.removeEventListener?.('click',handleClick);
      root.removeEventListener?.('change',handleChange);
      root.removeEventListener?.('input',handleInput);
      root.innerHTML='';
    }
    return {refresh:comparisonOnly?loadStats:()=>load({resetQueues:true}),stats:loadStats,get state(){return state;},destroy};
  }
  global.GuanyeReviews=Object.freeze({mount});
})(typeof window!=='undefined'?window:globalThis);
