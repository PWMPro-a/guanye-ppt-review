'use strict';
window.Cloud = (() => {
  const config=window.CLOUD_CONFIG;
  if(!config)return null;
  const aiGatewayUrl=String(config.aiGatewayUrl||'').trim().replace(/\/+$/,'');
  const client=config.url&&config.anonKey?window.supabase?.createClient(config.url,config.anonKey):null;
  const singleJudgeEnabled=config.singleJudge===true;
  const localImport=config.localImport===true;
  const localReview=singleJudgeEnabled&&config.localReview===true;
  const localCreation=singleJudgeEnabled&&config.localCreation===true;
  const LOCAL_CREATION_MAX_JSON_CHARS=36*1024*1024;
  const LOCAL_UUID=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
  const WEB_INTAKE_TABLE='single_judge_intake';
  const WEB_INTAKE_BUCKET='ppt-library';
  const WEB_INTAKE_MAX_BYTES=25*1024*1024;
  const WEB_INTAKE_RECEIPT_COLUMNS='id,title,original_name,object_path,content_type,size_bytes,sha256,ai_allowed,status,created_at';
  const WEB_INTAKE_TYPES=Object.freeze({
    ppt:Object.freeze({contentType:'application/vnd.ms-powerpoint'}),
    pptx:Object.freeze({contentType:'application/vnd.openxmlformats-officedocument.presentationml.presentation'}),
    pdf:Object.freeze({contentType:'application/pdf'})
  });
  const SINGLE_JUDGE_CAPABILITIES=Object.freeze({
    judge:Object.freeze({role:'judge',canReadComparison:true,canBrowse:true,canBrowseLibrary:true,canReadStatus:true,canScore:true,canSubmitMaterials:true,canUseOfficialReview:true,canUseAiWorkspace:true,canWriteLegacy:false,canLegacyWrite:false,canImport:false,canImportLibrary:false,canDelete:true,canDeleteLibrary:true,canViewTeam:false,canViewTeamScores:false,canInvite:false,canManageAdminData:true}),
    admin:Object.freeze({role:'admin',canReadComparison:true,canBrowse:true,canBrowseLibrary:true,canReadStatus:true,canScore:true,canSubmitMaterials:true,canUseOfficialReview:true,canUseAiWorkspace:true,canWriteLegacy:false,canLegacyWrite:false,canImport:false,canImportLibrary:false,canDelete:true,canDeleteLibrary:true,canViewTeam:false,canViewTeamScores:false,canInvite:false,canManageAdminData:true}),
    submitter:Object.freeze({role:'submitter',canReadComparison:false,canBrowse:false,canBrowseLibrary:false,canReadStatus:false,canScore:false,canSubmitMaterials:true,canUseOfficialReview:false,canUseAiWorkspace:false,canWriteLegacy:false,canLegacyWrite:false,canImport:false,canImportLibrary:false,canDelete:false,canDeleteLibrary:false,canViewTeam:false,canViewTeamScores:false,canInvite:false,canManageAdminData:false})
  });
  let member=null, members=[], teamReviews=[], singleJudgeAccess=null;
  const singleJudgeCapabilities=()=>singleJudgeEnabled&&singleJudgeAccess?SINGLE_JUDGE_CAPABILITIES[singleJudgeAccess.role]||null:null;
  function assertSingleJudgeAccess(){
    if(singleJudgeEnabled&&!singleJudgeAccess)throw new Error('单评委权限尚未通过校验');
  }
  const checked=async promise=>{const r=await promise;if(r.error){const error=new Error(r.error.message);error.code=r.error.code;error.status=r.error.status;error.details=r.error.details;error.hint=r.error.hint;throw error;}return r.data;};
  const rpc=(name,args={})=>checked(client.rpc(name,{w:config.workspaceId,...args}));
  async function allRows(table,columns,order,filters={}){
    const rows=[];
    for(let offset=0;;offset+=500){let query=client.from(table).select(columns).eq('workspace_id',config.workspaceId);for(const [key,value] of Object.entries(filters))query=query.eq(key,value);for(const column of order.split(','))query=query.order(column);const batch=await checked(query.range(offset,offset+499));rows.push(...batch);if(batch.length<500)return rows;}
  }
  const safe=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function gate(message='',mode='login'){
    if(singleJudgeEnabled)return gateSingleJudge(message);
    document.querySelector('#app').innerHTML='<main class="cloud-gate"><div class="cloud-login"><div class="eyebrow">SLIDE CURATOR · TEAM EDITION</div><h1>观页<span>.</span></h1><p>独立记录每个人的判断，一起发现值得留下的设计。</p><form id="cloud-auth"><label>邮箱<input id="cloud-email" type="email" autocomplete="email" required></label><label>密码<input id="cloud-password" type="password" autocomplete="current-password" minlength="8" required></label><label>你的姓名<input id="cloud-name" maxlength="40" autocomplete="name" placeholder="加入团队时填写"></label><label>团队邀请码<input id="cloud-invitation" autocomplete="off" placeholder="首次加入团队时填写"></label><div class="cloud-auth-actions"><button class="button primary" type="submit">登录并进入</button><button class="button" type="button" id="cloud-register">注册账号</button></div><div id="cloud-auth-message" role="status">'+safe(message)+'</div></form></div></main>';
    document.querySelector('#cloud-auth').onsubmit=e=>{e.preventDefault();authenticate(false);};
    document.querySelector('#cloud-register').onclick=()=>authenticate(true);
  }
  function gateSingleJudge(message=''){
    const app=document.querySelector('#app');if(!app)return;
    app.innerHTML='<main class="cloud-gate"><div class="cloud-login"><div class="eyebrow">GUANYE · SINGLE JUDGE EDITION</div><h1>观页<span>.</span></h1><p>单评委专业版。请使用邮箱和密码登录；访问权限由当前工作区校验。</p><form id="cloud-auth"><label>邮箱<input id="cloud-email" type="email" autocomplete="email" required></label><label>密码<input id="cloud-password" type="password" autocomplete="current-password" required></label><div class="cloud-auth-actions"><button class="button primary" type="submit">登录单评委专业版</button></div><div id="cloud-auth-message" role="status">'+safe(message)+'</div></form></div></main>';
    const form=document.querySelector('#cloud-auth');
    if(form)form.onsubmit=e=>{e.preventDefault();return authenticateSingleJudge();};
  }
  function singleJudgeMember(session,access){
    const email=String(session?.user?.email||session?.user?.id||'单评委用户');
    return {user_id:String(session?.user?.id||''),name:email,email,role:access.role,mode:'single-judge'};
  }
  async function loadSingleJudgeAccess(){
    const value=await rpc('single_judge_access');
    if(!value||value.mode!=='single-judge'||!['judge','admin','submitter'].includes(value.role))return null;
    return Object.freeze({mode:'single-judge',role:value.role});
  }
  async function authenticateSingleJudge(){
    const form=document.querySelector('#cloud-auth');if(!form?.reportValidity?.())return false;
    const msg=document.querySelector('#cloud-auth-message');
    const email=document.querySelector('#cloud-email').value.trim(),password=document.querySelector('#cloud-password').value;
    const buttons=form.querySelectorAll?.('button')||[];
    for(const button of buttons)button.disabled=true;
    try{
      await checked(client.auth.signInWithPassword({email,password}));
      const session=await checked(client.auth.getSession());
      if(!session?.session)throw new Error('登录会话无效');
      const access=await loadSingleJudgeAccess();
      if(!access)throw new Error('当前账号未获单评委专业版权限');
      singleJudgeAccess=access;member=singleJudgeMember(session.session,access);
      location.reload();
      return true;
    }catch(err){
      singleJudgeAccess=null;member=null;
      if(msg)msg.textContent=err?.message||'单评委登录失败';
      return false;
    }finally{
      for(const button of buttons)button.disabled=false;
    }
  }
  async function authenticate(register){
    const form=document.querySelector('#cloud-auth');if(!form.reportValidity())return;
    const msg=document.querySelector('#cloud-auth-message');
    const email=document.querySelector('#cloud-email').value.trim(),password=document.querySelector('#cloud-password').value;
    const name=document.querySelector('#cloud-name').value.trim(),invitation=document.querySelector('#cloud-invitation').value.trim();
    form.querySelectorAll('button').forEach(b=>b.disabled=true);
    try{
      if(register){const data=await checked(client.auth.signUp({email,password,options:{emailRedirectTo:location.origin+location.pathname}}));if(!data.session){msg.textContent='注册已提交，请前往邮箱确认后再登录。';return;}}
      else await checked(client.auth.signInWithPassword({email,password}));
      const session=await checked(client.auth.getSession());
      member=await checked(client.from('members').select('*').eq('workspace_id',config.workspaceId).eq('user_id',session.session.user.id).maybeSingle());
      if(!member){if(!name||!invitation)throw new Error('首次加入团队请填写姓名和团队邀请码');await rpc('join_workspace',{display_name:name,invitation});}
      location.reload();
    }catch(err){msg.textContent=err.message;}finally{form.querySelectorAll('button').forEach(b=>b.disabled=false);}
  }
  async function ready(){
    if(!client){gate('云端客户端未加载，请刷新页面');return false;}
    if(!config.url||!config.anonKey||!config.workspaceId){gate('网站正在配置云端连接，请管理员完成部署后再使用');return false;}
    if(singleJudgeEnabled){
      singleJudgeAccess=null;member=null;
      try{
        const session=await checked(client.auth.getSession());
        if(!session?.session){gateSingleJudge();return false;}
        const access=await loadSingleJudgeAccess();
        if(!access){gateSingleJudge('当前账号未获单评委专业版权限');return false;}
        singleJudgeAccess=access;member=singleJudgeMember(session.session,access);
        return true;
      }catch(_){
        singleJudgeAccess=null;member=null;
        gateSingleJudge('暂时无法校验单评委权限，请稍后重试');
        return false;
      }
    }
    const session=await checked(client.auth.getSession());
    if(!session.session){gate();return false;}
    member=await checked(client.from('members').select('*').eq('workspace_id',config.workspaceId).eq('user_id',session.session.user.id).maybeSingle());
    if(!member){gate('已登录，请填写姓名和团队邀请码后再次点击“登录并进入”。');return false;}
    return true;
  }
  const urlCache=new Map(), waiting=new Map();
  async function signWaiting(){
    const entries=[...waiting];waiting.clear();
    for(let i=0;i<entries.length;i+=100){
      const group=entries.slice(i,i+100);
      try{
        const rows=await checked(client.storage.from('ppt-library').createSignedUrls(group.map(([path])=>path),3600));
        const urls=new Map(rows.filter(r=>!r.error&&r.signedUrl).map(r=>[r.path,r.signedUrl]));
        for(const [path,job] of group){if(urls.has(path))job.resolve(urls.get(path));else{urlCache.delete(path);job.reject(new Error('图片暂不可用'));}}
      }catch(err){for(const [path,job] of group){urlCache.delete(path);job.reject(err);}}
    }
  }
  function fileUrl(path){
    if(singleJudgeEnabled&&!singleJudgeCapabilities()?.canBrowseLibrary)return Promise.reject(new Error('当前角色无权读取作品库文件'));
    if(typeof path!=='string'||!path||path.length>1024||path.startsWith('/')||path.includes('\\')||path.includes('\x00')||path.split('/').some(part=>!part||part==='.'||part==='..'))return Promise.reject(new Error('私有文件路径无效'));
    const cached=urlCache.get(path);if(cached&&cached.expires>Date.now())return cached.promise;
    if(!waiting.size)Promise.resolve().then(signWaiting);
    const promise=new Promise((resolve,reject)=>waiting.set(path,{resolve,reject}));
    urlCache.set(path,{promise,expires:Date.now()+55*60*1000});return promise;
  }
  function imageUrl(path){
    if(singleJudgeEnabled&&!singleJudgeCapabilities()?.canBrowseLibrary)return Promise.reject(new Error('当前角色无权读取作品库图片'));
    return fileUrl(path);
  }
  async function original(deckId){
    assertSingleJudgeAccess();
    if(!singleJudgeCapabilities()?.canBrowseLibrary)throw new Error('当前角色无权读取原始素材');
    if(typeof deckId!=='string'||!/^[A-Za-z0-9_-]{1,100}$/.test(deckId))throw new Error('作品编号无效');
    const value=await rpc('single_judge_original',{target_id:deckId});
    if(!value||typeof value!=='object'||value.available!==true||typeof value.path!=='string'||typeof value.name!=='string'||value.path.split('/').some(part=>!part||part==='.'||part==='..'))throw new Error('原始素材不可用');
    return value;
  }
  async function refreshTeam(){
    if(singleJudgeEnabled)throw new Error('单评委模式不提供团队评分汇总');
    [members,teamReviews]=await Promise.all([allRows('members','user_id,name,role','user_id'),allRows('reviews','user_id,deck_id,data','deck_id,user_id')]);
    return {members, reviews:teamReviews};
  }
  let summary={},summarySeq=0,visibleDeckIds=new Set(),sourceKindAvailable=false;
  async function refreshSummary(){
    if(singleJudgeEnabled){assertSingleJudgeAccess();if(!singleJudgeCapabilities()?.canBrowseLibrary)throw new Error('当前角色无权读取作品库状态');summary={};return summary;}
    const seq=++summarySeq;const rows=await rpc('review_score_summary');let next=rows;if(sourceKindAvailable&&rows&&typeof rows==='object'&&!Array.isArray(rows))next=Object.fromEntries(Object.entries(rows).filter(([id])=>visibleDeckIds.has(id)));if(seq===summarySeq)summary=next;return summary;
  }
function isMissingRpc(error,name){const code=String(error?.code||''),text=[error?.message,error?.details,error?.hint,String(error||'')].filter(Boolean).join(' ');if(!text.includes(name))return false;if(code==='PGRST202'||code==='42883')return true;return /(could not find the function|does not exist|not found|schema cache)/i.test(text);}
async function deckRows(){if(!aiGatewayUrl){sourceKindAvailable=false;return allRows('decks','data','id');}try{const rows=await allRows('decks','id,data,source_kind','id',{source_kind:'uploaded'});sourceKindAvailable=true;return rows;}catch(error){if(!/source_kind|column.*does not exist|schema cache|could not find/i.test(String(error?.message||error)))throw error;sourceKindAvailable=false;return allRows('decks','data','id');}}
  async function library(){
    if(singleJudgeEnabled){assertSingleJudgeAccess();if(!singleJudgeCapabilities()?.canBrowseLibrary)throw new Error('当前角色无权读取作品库');}
    if(aiGatewayUrl||localReview){
      try{
        const value=await rpc('get_review_library');
        if(value&&Array.isArray(value.decks)){
          const decks=value.decks.map(row=>row?.data).filter(deck=>deck&&deck.id);
          visibleDeckIds=new Set(decks.map(deck=>deck.id));sourceKindAvailable=true;
          const [state]=await Promise.all([rpc('get_review_state'),refreshSummary()]);
          return {decks,entries:Array.isArray(value.entries)?value.entries:[],state,source:singleJudgeEnabled?'单评委作品库':'团队作品库',sourceAvailable:true};
        }
        throw new Error('get_review_library 返回格式无效');
      }catch(error){if(localReview||!isMissingRpc(error,'get_review_library'))throw error;}
    }
    const rows=await deckRows();visibleDeckIds=new Set(rows.map(row=>row.id||row.data?.id));const [state]=await Promise.all([rpc('get_review_state'),refreshSummary()]);
    return {decks:rows.map(row=>row.data),state,source:singleJudgeEnabled?'单评委作品库':'团队作品库',sourceAvailable:true};
  }
  const PROFESSIONAL_IMPORT_PATH='/api/professional/import';
  const PROFESSIONAL_IMPORT_MAX_BYTES=25*1024*1024;
  function exactObjectKeys(value,keys){
    if(!value||typeof value!=='object'||Array.isArray(value)||Object.getPrototypeOf(value)!==null&&Object.getPrototypeOf(Object.getPrototypeOf(value))!==null)return false;
    const actual=Object.keys(value).sort(),expected=[...keys].sort();
    return actual.length===expected.length&&actual.every((key,index)=>key===expected[index]);
  }
  function professionalFileBytes(data){
    if(typeof data!=='string'||!data||data.length%4!==0||!/^[A-Za-z0-9+/]*={0,2}$/.test(data))throw new Error('投递文件内容不是有效的 base64');
    const padding=data.endsWith('==')?2:data.endsWith('=')?1:0;
    return data.length/4*3-padding;
  }
  function validateProfessionalImport(value){
    if(!exactObjectKeys(value,['title','aiAllowed','file']))throw new Error('投递请求必须严格包含 title、aiAllowed、file');
    const title=typeof value.title==='string'?value.title.trim():'';
    if(!title)throw new Error('请填写素材标题');
    if(typeof value.aiAllowed!=='boolean')throw new Error('aiAllowed 必须是布尔值');
    if(!exactObjectKeys(value.file,['name','data']))throw new Error('投递请求 file 必须严格包含 name、data');
    const name=typeof value.file.name==='string'?value.file.name.trim():'';
    if(!name||/[\\/]/.test(name))throw new Error('投递文件名无效');
    if(!/\.(?:pptx?|pdf)$/i.test(name))throw new Error('仅支持 PPT、PPTX 或 PDF');
    const bytes=professionalFileBytes(value.file.data);
    if(bytes>PROFESSIONAL_IMPORT_MAX_BYTES)throw new Error('单个投递文件不得超过 25 MiB');
    return {title,aiAllowed:value.aiAllowed,file:{name,data:value.file.data}};
  }
  async function professionalImport(options={}){
    if(String(options.method||'').toUpperCase()!=='POST')throw new Error('专业投递仅支持 POST');
    let value=options.body;
    if(typeof value==='string'){try{value=JSON.parse(value);}catch{throw new Error('投递请求 body 必须是 JSON');}}
    const body=validateProfessionalImport(value);
    const send=async refresh=>{
      const headers={Accept:'application/json','Content-Type':'application/json',Authorization:'Bearer '+await aiToken(refresh)};
      return fetch(PROFESSIONAL_IMPORT_PATH,{method:'POST',headers,body:JSON.stringify(body),credentials:'same-origin',cache:'no-store'});
    };
    let response=await send(false);
    if(response.status===401)response=await send(true);
    const text=await response.text();
    let payload=null;
    if(text){try{payload=JSON.parse(text);}catch{throw new Error('专业投递返回了无效 JSON');}}
    if(!response.ok){const error=new Error(payload?.error||payload?.message||'专业投递失败');error.status=response.status;error.code=payload?.code;throw error;}
    const pageCount=Number(payload?.pageCount);
    if(payload?.ok!==true||typeof payload.deckId!=='string'||!payload.deckId||typeof payload.deckVersion!=='string'||!payload.deckVersion||typeof payload.idempotent!=='boolean'||!Number.isInteger(pageCount)||pageCount<0)throw new Error('专业投递回执格式无效');
    return {ok:true,deckId:payload.deckId,deckVersion:payload.deckVersion,idempotent:payload.idempotent,pageCount};
  }
  function intakeProgress(options,stage,percent,message){
    try{if(typeof options?.onProgress==='function')options.onProgress(Object.freeze({stage,percent,message}));}catch(_){}
  }
  function safeIntakeStorageName(name,extension){
    const dot=name.lastIndexOf('.');
    let stem=(dot>0?name.slice(0,dot):name).normalize('NFKD').replace(/[\u0300-\u036f]/g,'');
    stem=stem.replace(/[^A-Za-z0-9._ -]+/g,'-').replace(/[\s.]+/g,' ').replace(/-+/g,'-').trim();
    stem=stem.replace(/^[^A-Za-z0-9]+/,'').replace(/[^A-Za-z0-9]+$/,'');
    if(!stem)stem='intake';
    return stem.slice(0,220)+'.'+extension;
  }
  function webIntakeFile(value){
    const raw=String(value?.name??'').trim();
    if(!raw||Array.from(raw).length>255||/[\\/]/.test(raw)||/[\u0000-\u001f\u007f-\u009f]/.test(raw)||raw==='.'||raw==='..'||/%2e|%2f|%5c/i.test(raw))throw new Error('投递文件名无效');
    const dot=raw.lastIndexOf('.'),extension=dot>0?raw.slice(dot+1).toLowerCase():'';
    const type=WEB_INTAKE_TYPES[extension];
    if(!type)throw new Error('仅支持 PPT、PPTX 或 PDF');
    const declared=String(value?.type??'').trim().toLowerCase().split(';',1)[0];
    if(declared&&declared!=='application/octet-stream'&&declared!==type.contentType)throw new Error('文件类型与扩展名不匹配');
    if(!Number.isSafeInteger(value?.size)||value.size<1||value.size>WEB_INTAKE_MAX_BYTES)throw new Error('单个投递文件不得超过 25 MiB');
    if(typeof value?.arrayBuffer!=='function')throw new Error('浏览器无法读取所选文件');
    return {name:safeIntakeStorageName(raw,extension),sourceName:raw,extension,contentType:type.contentType,size:value.size,file:value};
  }
  function validateWebIntake(value){
    if(!exactObjectKeys(value,['title','aiAllowed','file']))throw new Error('投递请求必须严格包含 title、aiAllowed、file');
    if(typeof value.aiAllowed!=='boolean')throw new Error('aiAllowed 必须是布尔值');
    const file=webIntakeFile(value.file);
    let title=typeof value.title==='string'?value.title.trim():'';
    if(!title)title=Array.from(file.sourceName).slice(0,100).join('');
    if(Array.from(title).length>100||/[\u0000-\u001f\u007f-\u009f]/.test(title))throw new Error('素材标题无效（最多 100 字）');
    return {...file,title,aiAllowed:value.aiAllowed};
  }
  async function currentSessionUser(){
    if(!client)throw new Error('云端客户端未加载');
    const data=await checked(client.auth.getSession()),session=data?.session,userId=String(session?.user?.id||'');
    if(!userId)throw new Error('请先登录后再继续');
    return {session,userId};
  }
  function intakeReceipt(row){
    if(!row||typeof row!=='object'||typeof row.id!=='string'||!row.id||typeof row.title!=='string'||typeof row.original_name!=='string'||typeof row.object_path!=='string'||typeof row.content_type!=='string'||!Number.isSafeInteger(row.size_bytes)||typeof row.sha256!=='string'||!/^[0-9a-f]{64}$/.test(row.sha256)||typeof row.ai_allowed!=='boolean'||typeof row.status!=='string'||!row.status)
      throw new Error('投递回执格式无效');
    return {id:row.id,title:row.title,originalName:row.original_name,objectPath:row.object_path,contentType:row.content_type,sizeBytes:row.size_bytes,sha256:row.sha256,aiAllowed:row.ai_allowed,status:row.status,createdAt:typeof row.created_at==='string'?row.created_at:null};
  }
  async function listMyIntakes(){
    assertSingleJudgeAccess();
    if(!singleJudgeCapabilities()?.canSubmitMaterials)throw new Error('当前角色无权投递素材');
    const {userId}=await currentSessionUser();
    const rows=await checked(client.from(WEB_INTAKE_TABLE)
      .select(WEB_INTAKE_RECEIPT_COLUMNS)
      .eq('workspace_id',config.workspaceId)
      .eq('submitted_by',userId)
      .order('created_at',{ascending:false})
      .limit(20));
    if(!Array.isArray(rows))throw new Error('投递列表格式无效');
    return rows.map(intakeReceipt);
  }
  async function submitWebIntake(value,options={}){
    assertSingleJudgeAccess();
    if(!singleJudgeCapabilities()?.canSubmitMaterials)throw new Error('当前角色无权投递素材');
    const input=validateWebIntake(value);
    const {userId}=await currentSessionUser();
    const intakeId=crypto.randomUUID();
    if(!LOCAL_UUID.test(intakeId))throw new Error('无法生成安全投递编号');
    const objectPath=config.workspaceId+'/intake/'+intakeId+'/'+input.name;
    const expectedPath=config.workspaceId+'/intake/'+intakeId+'/'+input.name;
    if(objectPath!==expectedPath||objectPath.split('/').some(part=>!part||part==='.'||part==='..'))throw new Error('投递对象路径无效');
    intakeProgress(options,'hashing',18,'正在浏览器内计算 SHA-256…');
    const bytes=await input.file.arrayBuffer();
    if(!bytes||typeof bytes.byteLength!=='number'||bytes.byteLength!==input.size)throw new Error('读取到的文件大小不一致，请重新选择文件');
    if(!crypto?.subtle?.digest)throw new Error('当前浏览器不支持 SHA-256，请升级浏览器后重试');
    const digest=await crypto.subtle.digest('SHA-256',bytes);
    const sha256=[...new Uint8Array(digest)].map(byte=>byte.toString(16).padStart(2,'0')).join('');
    if(!/^[0-9a-f]{64}$/.test(sha256))throw new Error('SHA-256 计算失败，请重试');
    intakeProgress(options,'uploading',46,'正在直传私有素材库…');
    await checked(client.storage.from(WEB_INTAKE_BUCKET).upload(objectPath,bytes,{contentType:input.contentType,upsert:false}));
    intakeProgress(options,'inserting',82,'文件已上传，正在登记投递回执…');
    const record={id:intakeId,workspace_id:config.workspaceId,submitted_by:userId,title:input.title,original_name:input.name,object_path:objectPath,content_type:input.contentType,size_bytes:input.size,sha256,ai_allowed:input.aiAllowed,status:'queued'};
    try{
      await checked(client.from(WEB_INTAKE_TABLE).insert(record));
    }catch(error){
      try{
        await checked(client.storage.from(WEB_INTAKE_BUCKET).remove([objectPath]));
      }catch(cleanupError){
        error.cleanupWarning='投递登记失败，且刚上传的文件清理未完成；请联系管理员核对，勿重复上传同一文件。';
        try{error.cleanupCause=String(cleanupError?.message||cleanupError);}catch(_){}
      }
      throw error;
    }
    intakeProgress(options,'complete',100,'投递已登记，回执已生成。');
    return intakeReceipt({...record,created_at:new Date().toISOString()});
  }
  async function request(url,options={}){
    if(singleJudgeEnabled){
      assertSingleJudgeAccess();
      const capabilities=singleJudgeCapabilities();
      if(url===PROFESSIONAL_IMPORT_PATH){
        if(!capabilities?.canSubmitMaterials)throw new Error('当前角色无权投递素材');
        if(!localImport)throw new Error('专业投递能力未启用');
        return professionalImport(options);
      }
      if(url==='/api/library'){
        if(!capabilities?.canBrowseLibrary)throw new Error('当前角色无权读取作品库');
        return library();
      }
      if(['/api/state','/api/delete-deck','/api/rescan','/api/import-documents','/api/import'].includes(url))throw new Error('单评委模式已停用旧评分、导入和删除写入接口');
      throw new Error('云端暂不支持此操作');
    }
    if(url==='/api/library')return library();
    if(url==='/api/state')return rpc('save_review_state',{incoming:JSON.parse(options.body)});
    if(url==='/api/delete-deck')return rpc('delete_review_deck',JSON.parse(options.body));
    if(url==='/api/rescan')return {decks:(await library()).decks,sourceAvailable:true};
    if(url==='/api/import-documents')return importDocuments(JSON.parse(options.body));
    if(url==='/api/import')return importImages(JSON.parse(options.body));
    throw new Error('云端暂不支持此操作');
  }
  async function importDocuments(value){
    const files=Array.isArray(value?.files)?value.files:[];
    if(!files.length)throw new Error('请至少选择一份文档');
    const presentations=files.filter(f=>/\.pptx?$/i.test(String(f?.name||'')));
    if(presentations.length){
      if(!aiGatewayUrl)throw new Error('当前未配置 AI 网关。请将 PPT / PPTX 另存为 PDF 后导入。');
      if(value.target)throw new Error('在线追加 PPT / PPTX 请先另存为 PDF，再作为 PDF 追加。');
      if(files.length!==1)throw new Error('多份或混合文档暂不能直接导入素材库；AI 工作台会把同一批合并为一套。想各自成套请逐份导入，或将 PPT / PPTX 另存为 PDF 后导入。');
      const file=files[0];
      const result=await aiRequest('/api/ai/reviews/import',{method:'POST',body:{name:String(value.name||'').trim(),files:[{name:file.name,data:file.data}]}});
      if(!result?.deckId)throw new Error('PPT 导入未返回作品信息');
      return {decks:(await library()).decks,created:[result.deckId],added:Number(result.pageCount)||0,skipped:0};
    }
    if(files.some(f=>!String(f?.name||'').toLowerCase().endsWith('.pdf')))throw new Error('在线版请将 PPT 另存为 PDF 后导入；本机版支持直接导入 PPT / PPTX。');
    const pdfjs=await import('./vendor/pdf.mjs');
    pdfjs.GlobalWorkerOptions.workerSrc=new URL('./vendor/pdf.worker.mjs',location.href).href;
    const images=[];
    for(const file of files){
      const pdf=await pdfjs.getDocument({data:Uint8Array.from(atob(file.data),c=>c.charCodeAt(0)),isEvalSupported:false}).promise;
      try{
        if(pdf.numPages>600)throw new Error('每份文档最多 600 页');
        const group=files.length===1&&value.name?value.name:file.name.replace(/\.[^.]+$/,'');
        for(let p=1;p<=pdf.numPages;p++){
          const status=document.querySelector('#selected-files');if(status)status.textContent=file.name+' · 正在转换 '+p+' / '+pdf.numPages+' 页';
          const page=await pdf.getPage(p),base=page.getViewport({scale:1});
          const viewport=page.getViewport({scale:Math.min(2.5,2000/Math.max(base.width,base.height))});
          const canvas=document.createElement('canvas');canvas.width=Math.ceil(viewport.width);canvas.height=Math.ceil(viewport.height);
          await page.render({canvasContext:canvas.getContext('2d'),viewport,background:'#ffffff'}).promise;
          images.push({name:String(p).padStart(4,'0')+'.jpg',group,data:canvas.toDataURL('image/jpeg',.92).split(',')[1]});
          canvas.width=canvas.height=0;page.cleanup();
        }
      }finally{await pdf.destroy();}
    }
    return importImages({target:value.target,images});
  }
  async function importImages(value){
    const groups=new Map();
    for(const image of value.images){const group=value.target||image.group||value.name||'新作品';if(!groups.has(group))groups.set(group,[]);groups.get(group).push(image);}
    const created=[];let added=0;
    for(const [group,images] of groups){
      const id=value.target||'import-'+crypto.randomUUID();
      let deck;
      if(value.target){const row=await checked(client.from('decks').select('data').eq('id',id).eq('workspace_id',config.workspaceId).single());deck=row.data;}
      else{const split=group.indexOf('_');deck={id,name:group,title:split>=0?group.slice(split+1):group,style:split>=0?group.slice(0,split):'新导入',origin:'import',images:[]};created.push(id);}
      if(deck.images.length+images.length>600)throw new Error('每套作品最多 600 张图片');
      for(const image of images){
        const raw=Uint8Array.from(atob(image.data),c=>c.charCodeAt(0));
        const suffix=image.name.split('.').pop().toLowerCase();
        if(!['jpg','jpeg','png','webp','gif','bmp'].includes(suffix))throw new Error('不支持的图片格式');
        const path=config.workspaceId+'/'+id+'/'+crypto.randomUUID()+'.'+suffix;
        await checked(client.storage.from('ppt-library').upload(path,raw,{contentType:({jpg:'image/jpeg',jpeg:'image/jpeg',png:'image/png',webp:'image/webp',gif:'image/gif',bmp:'image/bmp'})[suffix]}));
        deck.images.push({id:crypto.randomUUID(),name:image.name,path,thumbPath:path});added++;
      }
      await rpc('add_review_deck',{deck});
    }
    return {decks:(await library()).decks,created,added,skipped:0};
  }
  async function changePassword(password){if(password.length<8)throw new Error('密码至少需要 8 个字符');await checked(client.auth.updateUser({password}));}
  function aiEndpoint(path){
    const raw=String(path??'').trim();
    if(!raw||raw.startsWith('//')||raw.includes('\\')||raw.includes('#')||/^[a-z][a-z0-9+.-]*:/i.test(raw)||/%2e|%2f|%5c/i.test(raw))throw new Error('AI 接口路径无效');
    if(raw.split('/').some(segment=>segment==='.'||segment==='..'))throw new Error('AI 接口路径无效');
    const base=new URL(aiGatewayUrl.replace(/\/?$/,'/'),location.href);
    const endpoint=new URL(raw.replace(/^\/+/,''),base);
    if(endpoint.origin!==base.origin||endpoint.username||endpoint.password||!endpoint.pathname.startsWith(base.pathname))throw new Error('AI 接口路径无效');
    return endpoint;
  }
  async function aiToken(refresh=false){
    const data=refresh?await checked(client.auth.refreshSession()):await checked(client.auth.getSession());
    const session=data?.session;
    if(!session?.access_token)throw new Error('请先登录后再继续');
    return session.access_token;
  }
  function localCreationPath(path){
    const raw=String(path??'').trim();
    return raw.includes('/api/ai/planning')||raw.includes('/api/ai/generation');
  }
  function localPlainObject(value){
    if(!value||typeof value!=='object'||Array.isArray(value))return false;
    const prototype=Object.getPrototypeOf(value);
    return prototype===null||Object.getPrototypeOf(prototype)===null;
  }
  function localFiles(value){
    return Array.isArray(value)&&value.every(file=>exactObjectKeys(file,['name','mime_type','data_base64'])
      &&typeof file.name==='string'&&typeof file.mime_type==='string'&&typeof file.data_base64==='string');
  }
  function localCreationTarget(path,options={}){
    assertSingleJudgeAccess();
    if(!singleJudgeCapabilities()?.canUseAiWorkspace)throw new Error('当前角色无权使用策划与生图工作台');
    if(!localCreation)throw new Error('本地策划与生图能力未启用');
    const raw=String(path??'').trim();
    if(!raw||!raw.startsWith('/api/ai/')||raw.startsWith('//')||raw.includes('\\')||raw.includes('#')
      ||/^[a-z][a-z0-9+.-]*:/i.test(raw)||/%2e|%2f|%5c/i.test(raw))throw new Error('本地策划与生图请求路径无效');
    let url;
    try{url=new URL(raw,location.href);}catch{throw new Error('本地策划与生图请求路径无效');}
    if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||url.origin!==location.origin
      ||url.username||url.password||!url.pathname.startsWith('/api/ai/'))throw new Error('本地策划与生图只允许访问同源 127.0.0.1 服务');
    const method=String(options.method||'GET').toUpperCase();
    const query=allowed=>{
      const keys=[...url.searchParams.keys()];
      if(new Set(keys).size!==keys.length||keys.some(key=>!allowed.includes(key)))throw new Error('本地策划与生图请求参数无效');
      return url.searchParams;
    };
    const noQuery=()=>query([]);
    const noBody=()=>{if(options.body!=null)throw new Error('本地策划与生图请求不接受 body');};
    const bodyFor=required=>{
      if(method!=='POST'||!exactObjectKeys(options.body,required))throw new Error('本地策划与生图请求格式无效');
      return options.body;
    };
    const expectMethod=value=>{if(method!==value)throw new Error('本地策划与生图请求方法无效');};
    const requireUuid=(value,label)=>{if(typeof value!=='string'||!LOCAL_UUID.test(value))throw new Error(label+'无效');return value;};
    let body;
    if(url.pathname==='/api/ai/planning/analyze'){
      expectMethod('POST');noQuery();
      body=bodyFor(['mode','pageCount','outline','materials','papers','styleBrief','content_files','style_files','reference_images']);
      if(!['content_draft','template_sample'].includes(body.mode)||!Number.isInteger(body.pageCount)||body.pageCount<1||body.pageCount>30
        ||!['outline','materials','papers','styleBrief'].every(key=>typeof body[key]==='string')
        ||!['content_files','style_files','reference_images'].every(key=>localFiles(body[key])))throw new Error('本地策划请求参数无效');
    }else if(url.pathname==='/api/ai/planning/history'){
      expectMethod('GET');noBody();
      const params=query(['cursor']);const cursor=params.get('cursor');
      if(cursor!==null)requireUuid(cursor,'策划分页游标');
    }else if(url.pathname==='/api/ai/planning/save'||url.pathname==='/api/ai/planning/confirm'){
      expectMethod('POST');noQuery();
      body=bodyFor(['plan_id','expected_revision','draft']);
      requireUuid(body.plan_id,'策划 ID');
      if(!Number.isInteger(body.expected_revision)||body.expected_revision<0||!localPlainObject(body.draft))throw new Error('本地策划请求参数无效');
    }else if(url.pathname==='/api/ai/planning'){
      expectMethod('GET');noBody();
      const params=query(['plan_id']);
      if([...params.keys()].length!==1)throw new Error('本地策划请求参数无效');
      requireUuid(params.get('plan_id'),'策划 ID');
    }else if(url.pathname==='/api/ai/generation'){
      if(method==='POST'){
        noQuery();body=bodyFor(['plan_id']);requireUuid(body.plan_id,'策划 ID');
      }else if(method==='GET'){
        noBody();const params=query(['action','job_id','plan_id']),keys=[...params.keys()];
        if(keys.length===1&&params.get('action')==='recent')return {url,method,signal:options.signal};
        if(keys.length!==1||keys[0]!=='job_id'&&keys[0]!=='plan_id')throw new Error('本地生成任务请求参数无效');
        requireUuid(params.get(keys[0]),keys[0]==='job_id'?'生成任务 ID':'策划 ID');
      }else throw new Error('本地策划与生图请求方法无效');
    }else if(url.pathname==='/api/ai/generation/retry'){
      expectMethod('POST');noQuery();
      body=bodyFor(['job_id','page_ids']);
      requireUuid(body.job_id,'生成任务 ID');
      if(!Array.isArray(body.page_ids)||body.page_ids.length!==1)throw new Error('本地生图只允许单页重试');
      requireUuid(body.page_ids[0],'生成页面 ID');
    }else if(url.pathname==='/api/ai/generation/artifact'){
      expectMethod('GET');noBody();
      const params=query(['job_id','page_id','download']);
      if([...params.keys()].sort().join(',')!==['job_id','page_id','download'].filter(key=>params.has(key)).sort().join(','))throw new Error('本地生成素材请求参数无效');
      requireUuid(params.get('job_id'),'生成任务 ID');requireUuid(params.get('page_id'),'生成页面 ID');
      if(params.has('download')&&params.get('download')!=='1')throw new Error('本地生成素材下载参数无效');
    }else throw new Error('当前专业版未开放此本地策划与生图操作');
    return {url,method,body,signal:options.signal};
  }
  async function localCreationRequest(path,options={}){
    const target=localCreationTarget(path,options);
    const send=async refresh=>{
      const headers={Accept:'application/json'};
      if(target.body!==undefined)headers['Content-Type']='application/json';
      headers.Authorization='Bearer '+await aiToken(refresh);
      return fetch(target.url.href,{method:target.method,headers,body:target.body===undefined?undefined:JSON.stringify(target.body),
        credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:target.signal});
    };
    let response=await send(false);
    if(response.status===401)response=await send(true);
    const declared=Number(response.headers?.get?.('content-length'));
    if(Number.isFinite(declared)&&declared>LOCAL_CREATION_MAX_JSON_CHARS)throw new Error('本地策划与生图响应过大');
    const type=String(response.headers?.get?.('content-type')||'').toLowerCase();
    if(type&&!type.includes('application/json'))throw new Error('本地策划与生图服务未返回 JSON');
    const text=await response.text();
    if(text.length>LOCAL_CREATION_MAX_JSON_CHARS)throw new Error('本地策划与生图响应过大');
    let payload=null;
    if(text){try{payload=JSON.parse(text);}catch{throw new Error('本地策划与生图服务返回了无效 JSON');}}
    if(!response.ok){
      const detail=payload?.error?.message||payload?.error||payload?.message||'本地策划与生图请求失败';
      const error=new Error(typeof detail==='string'?detail:JSON.stringify(detail));error.status=response.status;error.code=payload?.code;throw error;
    }
    return payload;
  }
  async function professionalReviewRequest(path,options={}){
    assertSingleJudgeAccess();
    const prefix='/api/ai/reviews?';
    if(typeof path!=='string'||!path.startsWith(prefix)||path.includes('#'))throw new Error('当前专业版未开放此操作');
    const params=new URLSearchParams(path.slice(prefix.length));
    const method=String(options.method||'GET').toUpperCase(),action=params.get('action');
    const keys=[...params.keys()];
    if(new Set(keys).size!==keys.length)throw new Error('评审参数重复');
    const capabilities=singleJudgeCapabilities();
    if(method==='GET'&&action==='comparison'&&keys.length===1&&options.body==null){
      if(!capabilities?.canReadComparison)throw new Error('当前角色无权读取评审比较');
      const result=await rpc('single_judge_comparison');
      if(!['uploaded','generated'].every(kind=>result?.byKind?.[kind]&&typeof result.byKind[kind]==='object'&&!Array.isArray(result.byKind[kind])))throw new Error('评审比较未返回两个轨道');
      return result;
    }
    if(!capabilities?.canUseOfficialReview)throw new Error('当前角色无权使用正式评分工作台');
    if(method==='GET'&&action==='queue-decks'&&keys.length===3&&options.body==null){
      const kind=params.get('kind'),cursor=params.get('cursor');
      if(!['uploaded','generated'].includes(kind)||typeof cursor!=='string'||!/^[A-Za-z0-9_-]{1,100}$/.test(cursor))
        throw new Error('评审队列分页参数无效');
      const result=await rpc('single_judge_review_queue',{p_kind:kind,p_after:cursor,p_limit:8});
      if(!result||typeof result!=='object'||!Array.isArray(result.items)||typeof result.nextCursor!=='string'&&result.nextCursor!==null)
        throw new Error('评审队列分页未返回有效数据');
      return {...result,kind};
    }
    if(method==='POST'&&action==='admit'&&keys.length===1){
      const body=options.body,uuid=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
      if(!exactObjectKeys(body,['submissionId','decisionId'])||!['submissionId','decisionId'].every(key=>typeof body[key]==='string'&&uuid.test(body[key])))throw new Error('入库确认参数无效');
      const result=await rpc('single_judge_admit',{p_submission:body.submissionId,p_decision:body.decisionId});
      if(result?.ok!==true||result.admitted!==true||!uuid.test(result.entryId||''))throw new Error('入库操作未返回确认回执');
      return result;
    }
    if(method==='POST'&&action==='submit-bundle'&&keys.length===1){
      const body=options.body;
      if(!exactObjectKeys(body,['deckId','deckVersion','rubricVersion','units'])||!Array.isArray(body.units))throw new Error('正式评分提交格式无效');
      const result=await rpc('single_judge_submit_bundle',{payload:body});
      if(result?.ok!==true)throw new Error('正式评分未返回保存回执');
      return result;
    }
    if(method==='POST'&&action==='backfill'&&keys.length===1&&options.body){
      const body=options.body;
      if(!exactObjectKeys(body,['kind','offset','limit'])||!['uploaded','generated'].includes(body.kind)||
         !Number.isInteger(body.offset)||body.offset<0||!Number.isInteger(body.limit)||body.limit<1||body.limit>20)
        throw new Error('老库补档参数无效');
      const result=await rpc('single_judge_backfill',{p_kind:body.kind,p_offset:body.offset,p_limit:body.limit});
      if(!result||result.ok!==true||!Number.isInteger(result.decks)||!Number.isInteger(result.preparedUnits)||!Number.isInteger(result.totalDecks))
        throw new Error('老库补档未返回有效进度');
      return result;
    }
    if(method==='GET'&&['workspace','pre-evaluation-status'].includes(action)&&keys.every(key=>['action','kind','deckId'].includes(key))&&options.body==null){
      const kind=params.get('kind'),deckId=params.get('deckId');
      if(!['uploaded','generated'].includes(kind)||deckId!==null&&!/^[A-Za-z0-9_-]{1,100}$/.test(deckId)||action==='pre-evaluation-status'&&!deckId)throw new Error('作品或评审队列无效');
      const result=await rpc('single_judge_review_workspace',{p_kind:kind,p_deck_id:deckId});
      if(!result||result.singleJudge!==true||result.isJudge!==true||result.kind!==kind)throw new Error('专业评审没有返回当前队列');
      return action==='pre-evaluation-status'?{preEvaluationStatus:result.preEvaluationStatus}:result;
    }
    throw new Error('当前专业版未开放此操作');
  }
  async function aiRequest(path,options={}){
    if(localCreation&&localCreationPath(path))return localCreationRequest(path,options);
    if(localReview)return professionalReviewRequest(path,options);
    if(!aiGatewayUrl)throw new Error('尚未配置 AI 网关');
    if(!client)throw new Error('云端客户端未加载');
    if(singleJudgeEnabled){
      assertSingleJudgeAccess();
      if(!singleJudgeCapabilities()?.canUseOfficialReview)throw new Error('当前角色无权使用正式评分工作台');
    }
    const method=String(options.method||'GET').toUpperCase();
    if(!['GET','POST','PUT','PATCH','DELETE'].includes(method))throw new Error('AI 请求方法无效');
    const body=options.body;
    if(body!==undefined&&body!==null){const prototype=typeof body==='object'?Object.getPrototypeOf(body):undefined;if(typeof body!=='object'||Array.isArray(body)||(prototype!==Object.prototype&&prototype!==null))throw new Error('AI 请求 body 必须是 JSON 对象');}
    const endpoint=aiEndpoint(path);
    const send=async refresh=>{
      const headers={Accept:'application/json'};
      if(body!==undefined&&body!==null)headers['Content-Type']='application/json';
      headers.Authorization='Bearer '+await aiToken(refresh);
      return fetch(endpoint,{method,headers,body:body===undefined||body===null?undefined:JSON.stringify(body),credentials:'omit',cache:'no-store',signal:options.signal});
    };
    let response=await send(false);
    if(response.status===401)response=await send(true);
    const text=await response.text();
    let payload=null;
    if(text){try{payload=JSON.parse(text);}catch{throw new Error('AI 网关返回了无效 JSON');}}
    if(!response.ok){const error=new Error(payload?.error||'AI 请求失败');error.status=response.status;error.code=payload?.code;throw error;}
    return payload;
  }
  if(aiGatewayUrl||localReview||localCreation){const aiCore=Object.freeze({request:aiRequest,get available(){return true;},get baseUrl(){return aiGatewayUrl||(localCreation?location.origin:'');}});window.GuanyeAICore=aiCore;window.GuanyeAI=aiCore;window.GUANYE_AI_BASE=aiGatewayUrl;}
  return {ready,request,imageUrl,fileUrl,original,refreshSummary,refreshTeam,changePassword,submitWebIntake,listMyIntakes,get summary(){return summary;},get member(){return member;},get team(){return singleJudgeEnabled?{members:[],reviews:[]}:{members,reviews:teamReviews};},get mode(){return singleJudgeEnabled?(singleJudgeAccess?.mode||null):'team';},get role(){return singleJudgeEnabled?(singleJudgeAccess?.role||null):(member?.role||null);},get access(){return singleJudgeAccess?{mode:singleJudgeAccess.mode,role:singleJudgeAccess.role}:null;},get capabilities(){return singleJudgeCapabilities();},get localImport(){return localImport;},get localCreation(){return localCreation;},get webIntakeAvailable(){return Boolean(singleJudgeEnabled&&singleJudgeAccess&&singleJudgeCapabilities()?.canSubmitMaterials&&client&&config.workspaceId);},get professionalImportAvailable(){return Boolean(singleJudgeEnabled&&singleJudgeCapabilities()?.canSubmitMaterials&&localImport);},get professionalReviewAvailable(){return Boolean(localReview&&singleJudgeCapabilities()?.canUseOfficialReview);},get professionalComparisonAvailable(){return Boolean(localReview&&singleJudgeCapabilities()?.canReadComparison);},get localCreationAvailable(){return Boolean(localCreation&&singleJudgeCapabilities()?.canUseAiWorkspace);},get aiAvailable(){return singleJudgeEnabled?Boolean(['judge','admin'].includes(singleJudgeAccess?.role)&&singleJudgeCapabilities()?.canUseAiWorkspace&&(aiGatewayUrl||localCreation)&&window.GuanyeAI):Boolean(aiGatewayUrl&&window.GuanyeAI);},get aiGatewayUrl(){return aiGatewayUrl;},async logout(){singleJudgeAccess=null;member=null;await client.auth.signOut();location.reload();}};
})();
