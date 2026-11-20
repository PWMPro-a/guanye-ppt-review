'use strict';
window.Cloud = (() => {
  const config=window.CLOUD_CONFIG;
  if(!config)return null;
  const client=config.url&&config.anonKey?window.supabase?.createClient(config.url,config.anonKey):null;
  let member=null, members=[], teamReviews=[];
  const checked=async promise=>{const r=await promise;if(r.error)throw new Error(r.error.message);return r.data;};
  const rpc=(name,args={})=>checked(client.rpc(name,{w:config.workspaceId,...args}));
  async function allRows(table,columns,order){
    const rows=[];
    for(let offset=0;;offset+=500){let query=client.from(table).select(columns).eq('workspace_id',config.workspaceId);for(const column of order.split(','))query=query.order(column);const batch=await checked(query.range(offset,offset+499));rows.push(...batch);if(batch.length<500)return rows;}
  }
  const safe=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function gate(message='',mode='login'){
    document.querySelector('#app').innerHTML='<main class="cloud-gate"><div class="cloud-login"><div class="eyebrow">SLIDE CURATOR · TEAM EDITION</div><h1>观页<span>.</span></h1><p>独立记录每个人的判断，一起发现值得留下的设计。</p><form id="cloud-auth"><label>邮箱<input id="cloud-email" type="email" autocomplete="email" required></label><label>密码<input id="cloud-password" type="password" autocomplete="current-password" minlength="8" required></label><label>你的姓名<input id="cloud-name" maxlength="40" autocomplete="name" placeholder="加入团队时填写"></label><label>团队邀请码<input id="cloud-invitation" autocomplete="off" placeholder="首次加入团队时填写"></label><div class="cloud-auth-actions"><button class="button primary" type="submit">登录并进入</button><button class="button" type="button" id="cloud-register">注册账号</button></div><div id="cloud-auth-message" role="status">'+safe(message)+'</div></form></div></main>';
    document.querySelector('#cloud-auth').onsubmit=e=>{e.preventDefault();authenticate(false);};
    document.querySelector('#cloud-register').onclick=()=>authenticate(true);
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
  function imageUrl(path){
    const cached=urlCache.get(path);if(cached&&cached.expires>Date.now())return cached.promise;
    if(!waiting.size)Promise.resolve().then(signWaiting);
    const promise=new Promise((resolve,reject)=>waiting.set(path,{resolve,reject}));
    urlCache.set(path,{promise,expires:Date.now()+55*60*1000});return promise;
  }
  async function refreshTeam(){
    [members,teamReviews]=await Promise.all([allRows('members','user_id,name,role','user_id'),allRows('reviews','user_id,deck_id,data','deck_id,user_id')]);
    return {members, reviews:teamReviews};
  }
  let summary={},summarySeq=0;
  async function refreshSummary(){const seq=++summarySeq;const rows=await rpc('review_score_summary');if(seq===summarySeq)summary=rows;return summary;}
  async function library(){
    const [rows,state]=await Promise.all([allRows('decks','data','id'),rpc('get_review_state'),refreshSummary()]);
    return {decks:rows.map(row=>row.data),state,source:'团队作品库',sourceAvailable:true};
  }
  async function request(url,options={}){
    if(url==='/api/library')return library();
    if(url==='/api/state')return rpc('save_review_state',{incoming:JSON.parse(options.body)});
    if(url==='/api/delete-deck')return rpc('delete_review_deck',JSON.parse(options.body));
    if(url==='/api/rescan')return {decks:(await library()).decks,sourceAvailable:true};
    if(url==='/api/import-documents')return importDocuments(JSON.parse(options.body));
    if(url==='/api/import')return importImages(JSON.parse(options.body));
    throw new Error('云端暂不支持此操作');
  }
  async function importDocuments(value){
    if(value.files.some(f=>!f.name.toLowerCase().endsWith('.pdf')))throw new Error('在线版请将 PPT 另存为 PDF 后导入；本机版支持直接导入 PPT / PPTX。');
    const pdfjs=await import('./vendor/pdf.mjs');
    pdfjs.GlobalWorkerOptions.workerSrc=new URL('./vendor/pdf.worker.mjs',location.href).href;
    const images=[];
    for(const file of value.files){
      const pdf=await pdfjs.getDocument({data:Uint8Array.from(atob(file.data),c=>c.charCodeAt(0)),isEvalSupported:false}).promise;
      try{
        if(pdf.numPages>600)throw new Error('每份文档最多 600 页');
        const group=value.files.length===1&&value.name?value.name:file.name.replace(/\.[^.]+$/,'');
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
  return {ready,request,imageUrl,refreshSummary,get summary(){return summary;},refreshTeam,changePassword,get member(){return member;},get team(){return {members,reviews:teamReviews};},async logout(){await client.auth.signOut();location.reload();}};
})();
