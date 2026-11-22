// Exercise frontend cloud transport and aggregation without opening a browser.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');

function singleJudgeFixture({access='judge',initialSession=true,localImport=true,localReview=false,localCreation=false,professionalResponses=null,localResponses=null,locationHref='https://site.example/review/'}={}) {
  const workspace='11111111-1111-4111-8111-111111111111';
  const calls=[],tableReads=[],fetchCalls=[],reloads=[];
  let session=initialSession?{user:{id:'judge-user',email:'judge@example.com'},access_token:'session-token'}:null;
  const app={innerHTML:''},message={textContent:''},buttons=[{disabled:false}];
  const form={reportValidity:()=>true,querySelectorAll:()=>buttons,onsubmit:null};
  const nodes={
    '#app':app,
    '#cloud-auth':form,
    '#cloud-auth-message':message,
    '#cloud-email':{value:'judge@example.com'},
    '#cloud-password':{value:'correct-password'},
  };
  const deck={id:'single-deck',title:'单评委作品',images:[{id:'p1',path:workspace+'/single-deck/p1.png'}]};
  const client={
    auth:{
      getSession:async()=>({data:{session}}),
      signInWithPassword:async input=>{calls.push({name:'signInWithPassword',args:input});session={user:{id:'judge-user',email:input.email},access_token:'session-token'};return {data:{session}};},
      signOut:async()=>{session=null;return {data:null};},
      refreshSession:async()=>{calls.push({name:'refreshSession',args:{}});session={...session,access_token:'refreshed-token'};return {data:{session}};},
      updateUser:async()=>({data:{user:{id:'judge-user'}}}),
    },
    rpc:async(name,args)=>{
      calls.push({name,args});
      if(name==='single_judge_access'){
        if(access==='error')return {error:{message:'权限校验失败',code:'DENIED'}};
        if(access==='null')return {data:null};
        if(access==='invalid')return {data:{mode:'team',role:'judge'}};
        return {data:{mode:'single-judge',role:access}};
      }
      if(name==='single_judge_review_workspace')return {data:{kind:args.p_kind,singleJudge:true,isJudge:true,preEvaluationStatus:'queued',ai:[]}};
      if(name==='single_judge_comparison')return {data:{byKind:{uploaded:{holdoutUnits:1},generated:{holdoutUnits:2}}}};
      if(name==='single_judge_backfill')return {data:{ok:true,decks:1,preparedUnits:2,totalDecks:1,nextOffset:null}};
      if(name==='single_judge_review_queue')return {data:{items:[{id:'next-deck',title:'下一页作品',sourceKind:args.p_kind}],nextCursor:null}};
      if(name==='single_judge_original')return {data:{available:true,path:workspace+'/single-deck/original/'+('a'.repeat(64))+'.pptx',name:'sample.pptx'}};
      if(name==='single_judge_admit')return {data:{ok:true,admitted:true,idempotent:true,entryId:workspace}};
      if(name==='single_judge_submit_bundle')return {data:{ok:true,message:'人工评分已保存'}};
      if(name==='get_review_library')return {data:{decks:[{data:deck}],entries:[]}};
      if(name==='get_review_state')return {data:{revision:0,configRevision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉'}],reviews:{}}};
      if(name==='review_score_summary')return {data:{[deck.id]:{mean:4.5,count:1}}};
      return {error:{message:'unexpected rpc '+name,code:'UNEXPECTED'}};
    },
    storage:{from:()=>({upload:async()=>({data:null}),createSignedUrls:async paths=>({data:paths.map(path=>({path,signedUrl:'https://signed.example/'+path}))})})},
    from(table){tableReads.push(table);throw new Error('single judge must not read table '+table);},
  };
  const cloudConfig={url:'https://example.supabase.co',anonKey:'test',workspaceId:workspace,singleJudge:true,localImport};
  if(localReview)cloudConfig.localReview=true;
  if(localCreation)cloudConfig.localCreation=true;
  if(!localReview&&!localCreation)cloudConfig.aiGatewayUrl='https://gateway.example/ai';
  const pageOrigin=new URL(locationHref).origin;
  const context={
    window:{
      CLOUD_CONFIG:cloudConfig,
      supabase:{createClient:()=>client},
    },
    structuredClone,Map,Set,Uint8Array,URL,URLSearchParams,atob,
    crypto:require('node:crypto').webcrypto,
    location:{href:locationHref,origin:pageOrigin,reload:()=>reloads.push(Date.now())},
    document:{querySelector:selector=>nodes[selector]||null},
    fetch:async(url,options)=>{
      const target=String(url);fetchCalls.push({url:target,options});
      if(target==='/api/professional/import'){
        const index=fetchCalls.filter(row=>row.url==='/api/professional/import').length-1;
        const status=Array.isArray(professionalResponses)?professionalResponses[Math.min(index,professionalResponses.length-1)]:200;
        if(status===401)return {status:401,ok:false,text:async()=>JSON.stringify({error:'token expired'})};
        return {status:200,ok:true,text:async()=>JSON.stringify({ok:true,deckId:'professional-deck',deckVersion:'v1',idempotent:false,pageCount:3})};
      }
      if(target.startsWith('http://127.0.0.1:')&&new URL(target).pathname.startsWith('/api/ai/')){
        const index=fetchCalls.filter(row=>row.url.startsWith('http://127.0.0.1:')&&new URL(row.url).pathname.startsWith('/api/ai/')).length-1;
        const status=Array.isArray(localResponses)?localResponses[Math.min(index,localResponses.length-1)]:200;
        const parsed=new URL(target),payload=parsed.pathname==='/api/ai/generation/artifact'
          ?{dataBase64:'iVBORw0KGgo=',contentType:'image/png'}
          :parsed.pathname==='/api/ai/generation'&&parsed.searchParams.get('action')==='recent'?{jobs:[]}:{ok:true,path:parsed.pathname,query:parsed.search};
        return {status,ok:status>=200&&status<300,headers:{get:name=>name.toLowerCase()==='content-type'?'application/json; charset=utf-8':null},text:async()=>JSON.stringify(status===401?{error:'expired'}:payload)};
      }
      return {status:200,ok:true,text:async()=>JSON.stringify({ok:true})};
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('public/cloud.js','utf8'),context);
  return {cloud:context.window.Cloud,context,app,message,form,calls,tableReads,fetchCalls,reloads,workspace};
}
(async()=>{
 const workspace='11111111-1111-4111-8111-111111111111';
 const own={user_id:'alice',name:'Alice',role:'reviewer'};
 const members=[own,{user_id:'bob',name:'Bob',role:'reviewer'}];
 const records=Array.from({length:1101},(_,i)=>({user_id:'bob',deck_id:'other-'+i,data:{scores:{visual:3,layout:4}}}));
 records.push({user_id:'alice',deck_id:'deck',data:{scores:{visual:4,layout:5},note:'a'}},{user_id:'bob',deck_id:'deck',data:{scores:{visual:2},note:'partial'}});
 const settings={revision:0,configRevision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉效果'},{id:'layout',name:'排版'}],reviews:{deck:records.at(-2).data}};
 const dataset={members,reviews:records,decks:[{id:'deck',data:{id:'deck',title:'test',images:[{id:'img',path:workspace+'/deck/image.jpg'}]}}]};
 const signs=[]; const uploads=[];
 const client={auth:{getSession:async()=>({data:{session:{user:{id:'alice'}}}})},rpc:async(name,args)=>{if(name==='review_score_summary')return {data:{deck:{mean:4.5,count:1}}};if(name==='add_review_deck'){dataset.decks.push({id:args.deck.id,data:args.deck});return {data:null};}return {data:name==='get_review_state'?structuredClone(settings):{revision:args.incoming.revision+1,configRevision:0}};},storage:{from:()=>({upload:async(path,bytes)=>{uploads.push({path,size:bytes.length});return {data:{path}};},createSignedUrls:async paths=>{signs.push([...paths]);return {data:paths.map(path=>({path,signedUrl:'https://signed.example/'+path}))};}})},from(table){let filters=[],begin=0,end=Infinity;const q={select(){return q;},eq(k,v){filters.push([k,v]);return q;},order(){return q;},range(a,b){begin=a;end=b;return q;},maybeSingle:async()=>({data:own}),then(resolve,reject){const rows=dataset[table].filter(r=>filters.every(([k,v])=>k==='workspace_id'||r[k]===v)).slice(begin,end+1);return Promise.resolve({data:structuredClone(rows)}).then(resolve,reject);}};return q;}};
 const context={window:{CLOUD_CONFIG:{url:'https://example.supabase.co',anonKey:'test',workspaceId:workspace},supabase:{createClient:()=>client}},structuredClone,Map,Set,Uint8Array,URL,crypto:require('node:crypto').webcrypto,atob,location:{href:'https://site.example/review/'}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('public/cloud.js','utf8'),context);
 const cloud=context.window.Cloud;assert.equal(await cloud.ready(),true);assert.equal(cloud.mode,'team');assert.equal(cloud.role,'reviewer');assert.equal(cloud.capabilities,null);
 const library=await cloud.request('/api/library');assert.equal(library.decks.length,1);assert.equal(cloud.team.reviews.length,0);assert.equal(cloud.summary.deck.mean,4.5);assert.equal(signs.length,0);
 const path=library.decks[0].images[0].path;await Promise.all([cloud.imageUrl(path),cloud.imageUrl(path),cloud.imageUrl(path+'2')]);assert.deepEqual(signs,[[path,path+'2']]);assert.equal(await cloud.imageUrl(path),'https://signed.example/'+path);assert.equal(signs.length,1);await cloud.refreshTeam();assert.equal(cloud.team.reviews.length,1103);
 assert.equal((await cloud.request('/api/state',{body:JSON.stringify(settings)})).revision,1);
 const imported=await cloud.request('/api/import',{body:JSON.stringify({name:'reviewer import',images:[{name:'test.png',data:'aGVsbG8='}]})});
 assert.equal(imported.added,1);assert.equal(imported.created.length,1);assert.equal(imported.decks.length,2);assert.equal(uploads.length,1);assert.ok(uploads[0].path.startsWith(workspace+'/'));
 Object.assign(context,{document:{addEventListener(){}},Cloud:cloud,decks:library.decks,state:settings,hasScore:n=>typeof n==='number'&&Number.isFinite(n)});
 vm.runInContext(fs.readFileSync('public/scoring.js','utf8'),context);context.Scoring=context.window.Scoring;
 vm.runInContext(fs.readFileSync('public/team.js','utf8'),context);
 let rows=context.teamRows();assert.equal(rows[0].mean,4.5);assert.equal(rows[0].records.length,1);
 records.at(-1).data.scores.layout=3;await cloud.refreshTeam();rows=context.teamRows();assert.equal(rows[0].mean,3.5);assert.equal(rows[0].records.length,2);
 records.at(-1).data={mode:'page',scores:{},pages:{img:{scores:{visual:2,layout:3}}}};await cloud.refreshTeam();rows=context.teamRows();assert.equal(rows[0].mean,3.5);assert.equal(rows[0].records.length,2);
 delete records.at(-1).data.pages.img.scores.layout;await cloud.refreshTeam();assert.equal(context.teamRows()[0].records.length,1);
 const pptBase64=Buffer.from('D0CF11E0A1B11AE1','hex').toString('base64');
 const uploadsBefore=uploads.length;
 await assert.rejects(cloud.request('/api/import-documents',{body:JSON.stringify({name:'single',files:[{name:'single.pptx',data:pptBase64}]})}),/另存为 PDF/);
 assert.equal(uploads.length,uploadsBefore);

 const gatewayRequests=[],gatewayUploads=[];
 const importedDeck={id:'ai-import-1',title:'单份导入',images:[{id:'p1',path:workspace+'/ai-import-1/p1.png'}]};
 const gatewayClient={auth:{getSession:async()=>({data:{session:{user:{id:'alice'},access_token:'token'}}})},rpc:async name=>{if(name==='get_review_library')return {data:{decks:[{data:importedDeck}],entries:[]}};if(name==='get_review_state')return {data:{revision:0,configRevision:0,maxScore:5,dimensions:[],reviews:{}}};if(name==='review_score_summary')return {data:{}};throw new Error('unexpected rpc '+name);},storage:{from:()=>({upload:async(path,bytes)=>{gatewayUploads.push({path,size:bytes.length});return {data:{path}};},createSignedUrls:async()=>({data:[]})})},from(){throw new Error('unexpected table read');}};
 const gatewayContext={window:{CLOUD_CONFIG:{url:'https://example.supabase.co',anonKey:'test',workspaceId:workspace,aiGatewayUrl:'https://gateway.example/ai'},supabase:{createClient:()=>gatewayClient}},structuredClone,Map,Set,Uint8Array,URL,crypto:require('node:crypto').webcrypto,atob,location:{href:'https://site.example/review/'},document:{querySelector(){return null;}},fetch:async(url,options)=>{gatewayRequests.push({url:String(url),options});return {status:200,ok:true,text:async()=>JSON.stringify({ok:true,deckId:'ai-import-1',pageCount:'3'})};}};
 vm.createContext(gatewayContext);vm.runInContext(fs.readFileSync('public/cloud.js','utf8'),gatewayContext);
 const gatewayCloud=gatewayContext.window.Cloud;
 const gatewayImport=await gatewayCloud.request('/api/import-documents',{body:JSON.stringify({target:null,name:'单份导入',files:[{name:'single.pptx',data:pptBase64}]})});
 assert.deepEqual([...gatewayImport.created],['ai-import-1']);assert.equal(gatewayImport.added,3);assert.equal(gatewayImport.decks.length,1);assert.equal(gatewayRequests.length,1);assert.equal(gatewayRequests[0].url,'https://gateway.example/ai/api/ai/reviews/import');assert.deepEqual(JSON.parse(gatewayRequests[0].options.body),{name:'单份导入',files:[{name:'single.pptx',data:pptBase64}]});assert.equal(gatewayUploads.length,0);
 await assert.rejects(gatewayCloud.request('/api/import-documents',{body:JSON.stringify({name:'two',files:[{name:'a.pptx',data:pptBase64},{name:'b.pptx',data:pptBase64}]})}),/多份或混合/);
 await assert.rejects(gatewayCloud.request('/api/import-documents',{body:JSON.stringify({name:'mixed',files:[{name:'a.pptx',data:pptBase64},{name:'b.pdf',data:Buffer.from('%PDF-1.4').toString('base64')}]})}),/多份或混合/);
 assert.equal(gatewayRequests.length,1);
 await assert.rejects(gatewayCloud.request('/api/import-documents',{body:JSON.stringify({target:'existing',name:'append',files:[{name:'single.pptx',data:pptBase64}]})}),/追加/);
 assert.equal(gatewayRequests.length,1);assert.equal(gatewayUploads.length,0);
 console.log('PASS: cloud transport, >1000 team records, signed private images, isolated save, ordinary member image import, complete-only team averages, single PPT via existing AI import, multi/append rejected without fallback');
 const loginFixture=singleJudgeFixture({access:'judge',initialSession:false});
 assert.equal(await loginFixture.cloud.ready(),false);
 assert.match(loginFixture.app.innerHTML,/单评委专业版/);
 assert.match(loginFixture.app.innerHTML,/id="cloud-email"/);
 assert.match(loginFixture.app.innerHTML,/id="cloud-password"/);
 assert.doesNotMatch(loginFixture.app.innerHTML,/cloud-name|cloud-invitation|cloud-register|注册账号|邀请码/);
 assert.equal(loginFixture.tableReads.length,0);
 assert.equal(await loginFixture.form.onsubmit({preventDefault(){}}),true);
 assert.equal(loginFixture.reloads.length,1);
 assert.equal(loginFixture.cloud.mode,'single-judge');
 assert.equal(loginFixture.cloud.role,'judge');
 assert.equal(JSON.stringify(loginFixture.cloud.access),JSON.stringify({mode:'single-judge',role:'judge'}));
 assert.equal(loginFixture.cloud.capabilities.canUseOfficialReview,true);
 assert.equal(loginFixture.cloud.capabilities.canLegacyWrite,false);
 const loginAccessCall=loginFixture.calls.find(row=>row.name==='single_judge_access');assert.equal(loginAccessCall.args.w,loginFixture.workspace);
 assert.equal(loginFixture.calls.some(row=>row.name==='join_workspace'),false);
 assert.equal(loginFixture.tableReads.length,0);

 for(const access of ['null','error','invalid']){
   const denied=singleJudgeFixture({access,initialSession:true});
   assert.equal(await denied.cloud.ready(),false);
   assert.equal(denied.cloud.mode,null);
   assert.equal(denied.cloud.role,null);
   assert.equal(denied.cloud.capabilities,null);
   assert.equal(denied.tableReads.length,0);
   assert.equal(denied.calls.some(row=>row.name==='review_score_summary'),false);
   await assert.rejects(denied.cloud.request('/api/library'),/权限尚未通过校验/);
   await assert.rejects(denied.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify({title:'x',aiAllowed:false,file:{name:'x.pdf',data:'aGVsbG8='}})}),/权限尚未通过校验/);
   assert.match(denied.app.innerHTML,access==='error'?/暂时无法校验单评委权限/:/未获单评委专业版权限/);
 }

 const adminFixture=singleJudgeFixture({access:'admin',initialSession:true});
 assert.equal(await adminFixture.cloud.ready(),true);
 assert.equal(adminFixture.cloud.role,'admin');
 assert.equal(adminFixture.cloud.capabilities.canBrowseLibrary,true);
 assert.equal(adminFixture.cloud.capabilities.canUseOfficialReview,true);
 assert.equal(adminFixture.cloud.capabilities.canLegacyWrite,false);
 assert.equal(adminFixture.cloud.capabilities.canViewTeamScores,false);
 assert.equal(adminFixture.cloud.capabilities.canInvite,false);
 assert.equal(adminFixture.cloud.aiAvailable,true);
 assert.equal(JSON.stringify(adminFixture.cloud.team),JSON.stringify({members:[],reviews:[]}));
 const adminLibrary=await adminFixture.cloud.request('/api/library');
 assert.equal(adminLibrary.decks.length,1);
 assert.equal(adminLibrary.source,'单评委作品库');
 assert.equal(adminFixture.calls.some(row=>row.name==='review_score_summary'),false);
 for(const [url,options] of [['/api/state',{method:'PUT',body:'{}'}],['/api/delete-deck',{method:'POST',body:'{}'}],['/api/import',{method:'POST',body:'{}'}],['/api/import-documents',{method:'POST',body:'{}'}],['/api/rescan',{method:'POST'}]]){
   await assert.rejects(adminFixture.cloud.request(url,options),/已停用旧评分、导入和删除写入接口/);
 }
 assert.equal(adminFixture.calls.some(row=>['save_review_state','delete_review_deck','add_review_deck'].includes(row.name)),false);
 await assert.rejects(adminFixture.cloud.refreshTeam(),/不提供团队评分汇总/);
 assert.equal(JSON.stringify(await adminFixture.context.window.GuanyeAI.request('/api/ai/reviews?action=workspace')),JSON.stringify({ok:true}));
 assert.equal(adminFixture.tableReads.length,0);
 assert.equal(adminFixture.fetchCalls.length,1);

 const judgeFixture=singleJudgeFixture({access:'judge',initialSession:true});
 assert.equal(await judgeFixture.cloud.ready(),true);
 assert.equal(judgeFixture.cloud.role,'judge');
 assert.equal(judgeFixture.cloud.capabilities.canUseOfficialReview,true);
 assert.equal(judgeFixture.cloud.capabilities.canLegacyWrite,false);
 assert.equal(judgeFixture.cloud.aiAvailable,true);
 await assert.rejects(judgeFixture.cloud.request('/api/state',{method:'PUT',body:'{}'}),/已停用旧评分、导入和删除写入接口/);
 assert.equal(judgeFixture.calls.some(row=>row.name==='save_review_state'),false);
 assert.equal(JSON.stringify(await judgeFixture.context.window.GuanyeAI.request('/api/ai/reviews?action=workspace')),JSON.stringify({ok:true}));
 assert.equal(judgeFixture.fetchCalls.length,1);
 assert.match(judgeFixture.fetchCalls[0].url,/\/api\/ai\/reviews\?action=workspace$/);
 assert.equal(judgeFixture.fetchCalls[0].options.headers.Authorization,'Bearer session-token');
 assert.equal(judgeFixture.tableReads.length,0);
 const original=await judgeFixture.cloud.original('single-deck');
 assert.equal(original.name,'sample.pptx');
 assert.equal(await judgeFixture.cloud.fileUrl(original.path),'https://signed.example/'+original.path);
 assert.deepEqual(JSON.parse(JSON.stringify(judgeFixture.calls.slice(-1))),[{name:'single_judge_original',args:{w:workspace,target_id:'single-deck'}}]);

 const professionalBody=(aiAllowed,fileOverride={})=>({title:'人工素材',aiAllowed,file:{name:'manual.pdf',data:'aGVsbG8=',...fileOverride}});
 await assert.rejects(judgeFixture.cloud.request('/api/professional/import?x=1',{method:'POST',body:JSON.stringify(professionalBody(false))}),/云端暂不支持此操作/);
 await assert.rejects(judgeFixture.cloud.request('/api/import-documents',{method:'POST',body:'{}'}),/已停用旧评分、导入和删除写入接口/);
 await assert.rejects(judgeFixture.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify({...professionalBody(false),extra:true})}),/严格包含/);
 await assert.rejects(judgeFixture.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify({...professionalBody(false),aiAllowed:'false'})}),/必须是布尔值/);
 await assert.rejects(judgeFixture.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify(professionalBody(false,{name:'manual.txt'}))}),/仅支持/);
 const judgeReceipt=await judgeFixture.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify(professionalBody(false))});
 assert.equal(JSON.stringify(judgeReceipt),JSON.stringify({ok:true,deckId:'professional-deck',deckVersion:'v1',idempotent:false,pageCount:3}));
 const professionalCall=judgeFixture.fetchCalls.find(row=>row.url==='/api/professional/import');
 assert.ok(professionalCall);
 assert.equal(professionalCall.options.method,'POST');
 assert.equal(professionalCall.options.headers.Authorization,'Bearer session-token');
 const sentProfessional=JSON.parse(professionalCall.options.body);
 assert.deepEqual(Object.keys(sentProfessional).sort(),['aiAllowed','file','title']);
 assert.equal(sentProfessional.aiAllowed,false);
 assert.deepEqual(Object.keys(sentProfessional.file).sort(),['data','name']);
 assert.equal(JSON.stringify(judgeReceipt).includes('session-token'),false);
 assert.equal(adminFixture.cloud.capabilities.canSubmitMaterials,true);
 const adminReceipt=await adminFixture.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify({...professionalBody(true),title:'管理员投递'})});
 assert.equal(adminReceipt.ok,true);
 const adminProfessionalCall=adminFixture.fetchCalls.find(row=>row.url==='/api/professional/import');
 assert.equal(JSON.parse(adminProfessionalCall.options.body).aiAllowed,true);

 const refreshFixture=singleJudgeFixture({access:'judge',initialSession:true,professionalResponses:[401,200]});
 assert.equal(await refreshFixture.cloud.ready(),true);
 const refreshedReceipt=await refreshFixture.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify(professionalBody(false))});
 assert.equal(refreshedReceipt.ok,true);
 const refreshProfessionalCalls=refreshFixture.fetchCalls.filter(row=>row.url==='/api/professional/import');
 assert.equal(refreshProfessionalCalls.length,2);
 assert.equal(refreshProfessionalCalls[0].options.headers.Authorization,'Bearer session-token');
 assert.equal(refreshProfessionalCalls[1].options.headers.Authorization,'Bearer refreshed-token');
 assert.equal(refreshFixture.calls.some(row=>row.name==='refreshSession'),true);

 const submitterFixture=singleJudgeFixture({access:'submitter',initialSession:true});
 assert.equal(await submitterFixture.cloud.ready(),true);
 assert.equal(submitterFixture.cloud.role,'submitter');
 assert.equal(submitterFixture.cloud.capabilities.canBrowseLibrary,false);
 assert.equal(submitterFixture.cloud.capabilities.canReadStatus,false);
 assert.equal(submitterFixture.cloud.capabilities.canScore,false);
 assert.equal(submitterFixture.cloud.capabilities.canSubmitMaterials,true);
 assert.equal(submitterFixture.cloud.capabilities.canUseOfficialReview,false);
 assert.equal(submitterFixture.cloud.capabilities.canUseAiWorkspace,false);
 assert.equal(submitterFixture.cloud.professionalImportAvailable,true);
 await assert.rejects(submitterFixture.cloud.request('/api/library'),/无权读取作品库/);
 await assert.rejects(submitterFixture.cloud.refreshSummary(),/无权读取作品库状态/);
 await assert.rejects(submitterFixture.cloud.imageUrl('private/image.png'),/无权读取作品库图片/);
 await assert.rejects(submitterFixture.cloud.original('single-deck'),/无权读取原始素材/);
 await assert.rejects(submitterFixture.context.window.GuanyeAI.request('/api/ai/reviews?action=workspace'),/无权使用正式评分工作台/);
 const submitterReceipt=await submitterFixture.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify({...professionalBody(false),title:'投递者素材'})});
 assert.equal(submitterReceipt.ok,true);
 assert.equal(submitterFixture.tableReads.length,0);

 const localReviewFixture=singleJudgeFixture({localReview:true});
 assert.equal(await localReviewFixture.cloud.ready(),true);
 assert.equal(localReviewFixture.cloud.aiGatewayUrl,'');
 assert.equal(localReviewFixture.cloud.aiAvailable,false,'avoid the old dual-mounted AI shell');
 assert.equal(localReviewFixture.cloud.professionalReviewAvailable,true);
 assert.equal(localReviewFixture.cloud.professionalComparisonAvailable,true);
 const localReviewRequest=localReviewFixture.context.window.GuanyeAI.request;
 const reviewed=(await localReviewRequest('/api/ai/reviews?action=workspace&kind=uploaded&deckId=single-deck'));
 assert.equal(reviewed.kind,'uploaded');assert.deepEqual(reviewed.ai,[]);
 assert.equal((await localReviewRequest('/api/ai/reviews?action=pre-evaluation-status&kind=generated&deckId=generated-deck')).preEvaluationStatus,'queued');
 const backfill=await localReviewRequest('/api/ai/reviews?action=backfill',{method:'POST',body:{kind:'uploaded',offset:0,limit:8}});
 assert.equal(backfill.preparedUnits,2);
 assert.deepEqual(JSON.parse(JSON.stringify(localReviewFixture.calls.slice(-1))),[{name:'single_judge_backfill',args:{w:workspace,p_kind:'uploaded',p_offset:0,p_limit:8}}]);
 const queuePage=await localReviewRequest('/api/ai/reviews?action=queue-decks&kind=uploaded&cursor=next-page-must-not-load');
 assert.equal(queuePage.kind,'uploaded');assert.equal(queuePage.items[0].id,'next-deck');
 assert.deepEqual(JSON.parse(JSON.stringify(localReviewFixture.calls.slice(-1))),[{name:'single_judge_review_queue',args:{w:workspace,p_kind:'uploaded',p_after:'next-page-must-not-load',p_limit:8}}]);
 const scoreBody={deckId:'single-deck',deckVersion:'v1',rubricVersion:'r1',units:[{scope:'deck',scores:{visual:4}}]};
 assert.equal((await localReviewRequest('/api/ai/reviews?action=submit-bundle',{method:'POST',body:scoreBody})).ok,true);
 assert.deepEqual(JSON.parse(JSON.stringify(localReviewFixture.calls.slice(-3))),[
   {name:'single_judge_backfill',args:{w:workspace,p_kind:'uploaded',p_offset:0,p_limit:8}},
   {name:'single_judge_review_queue',args:{w:workspace,p_kind:'uploaded',p_after:'next-page-must-not-load',p_limit:8}},
   {name:'single_judge_submit_bundle',args:{w:workspace,payload:scoreBody}},
 ]);
 for(const path of ['/api/ai/reviews?action=prepare','/api/ai/reviews?action=workspace&kind=uploaded&w=other','https://evil.example/api/ai/reviews?action=workspace&kind=uploaded','/api/ai/reviews?action=workspace&kind=uploaded&kind=generated']){
   await assert.rejects(localReviewRequest(path),/未开放|重复/);
 }
 await assert.rejects(localReviewRequest('/api/ai/planning/history'),/未开放/);
 await assert.rejects(localReviewRequest('/api/ai/generation?action=recent'),/未开放/);
 await assert.rejects(localReviewRequest('/api/ai/reviews?action=submit-bundle',{method:'POST',body:{...scoreBody,w:'other'}}),/格式无效/);
 const target={submissionId:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',decisionId:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'};
 const admitPath='/api/ai/reviews?action=admit', comparisonPath='/api/ai/reviews?action=comparison';
 assert.equal((await localReviewRequest(admitPath,{method:'POST',body:target})).idempotent,true);
 assert.equal((await localReviewRequest(comparisonPath)).byKind.generated.holdoutUnits,2);
 assert.deepEqual(JSON.parse(JSON.stringify(localReviewFixture.calls.slice(-2))),[
   {name:'single_judge_admit',args:{w:workspace,p_submission:target.submissionId,p_decision:target.decisionId}},
   {name:'single_judge_comparison',args:{w:workspace}},
 ]);
 const numCalls=localReviewFixture.calls.length;
 for(const args of [
   [admitPath,{method:'GET'}], [admitPath+'&w=other',{method:'POST',body:target}],
   [admitPath,{method:'POST',body:{...target,actor:'other'}}], [admitPath,{method:'POST',body:{...target,decisionId:'not-uuid'}}],
   [comparisonPath+'&kind=generated'],[comparisonPath+'&action=comparison'],[comparisonPath,{body:{w:'other'}}],
   [comparisonPath,{method:'POST'}],['/api/ai/reviews?action=formal',{method:'POST',body:target}],
   ['/api/ai/reviews?action=backfill',{method:'POST',body:{kind:'uploaded',offset:-1,limit:8}}],
   ['/api/ai/reviews?action=backfill',{method:'POST',body:{kind:'other',offset:0,limit:8}}],
 ])await assert.rejects(localReviewRequest(...args),/未开放|参数无效|重复/);
 assert.equal(localReviewFixture.calls.length,numCalls);
 const adminReview=singleJudgeFixture({access:'admin',localReview:true});
 assert.equal(await adminReview.cloud.ready(),true);
 assert.equal(adminReview.cloud.professionalReviewAvailable,true);
 assert.equal(adminReview.cloud.professionalComparisonAvailable,true);
 assert.equal((await adminReview.context.window.GuanyeAI.request('/api/ai/reviews?action=workspace&kind=uploaded')).kind,'uploaded');
 assert.equal((await adminReview.context.window.GuanyeAI.request(admitPath,{method:'POST',body:target})).idempotent,true);
 assert.equal((await adminReview.context.window.GuanyeAI.request('/api/ai/reviews?action=submit-bundle',{method:'POST',body:scoreBody})).ok,true);
 assert.equal((await adminReview.context.window.GuanyeAI.request(comparisonPath)).byKind.uploaded.holdoutUnits,1);
 const submitterReview=singleJudgeFixture({access:'submitter',localReview:true});
 assert.equal(await submitterReview.cloud.ready(),true);
 assert.equal(submitterReview.cloud.aiAvailable,false);
 await assert.rejects(submitterReview.context.window.GuanyeAI.request('/api/ai/reviews?action=workspace&kind=uploaded'),/无权/);
 assert.equal(submitterReview.cloud.professionalReviewAvailable,false);
 assert.equal(submitterReview.cloud.professionalComparisonAvailable,false);
 await assert.rejects(submitterReview.context.window.GuanyeAI.request(admitPath,{method:'POST',body:target}),/无权/);
 await assert.rejects(submitterReview.context.window.GuanyeAI.request('/api/ai/reviews?action=submit-bundle',{method:'POST',body:scoreBody}),/无权/);
 assert.equal(localReviewFixture.fetchCalls.length,0);
 const creationOrigin='http://127.0.0.1:8765/';
 const planId='20000000-0000-4000-8000-000000000001';
 const jobId='20000000-0000-4000-8000-000000000002';
 const pageId='20000000-0000-4000-8000-000000000003';
 const cursor='20000000-0000-4000-8000-000000000004';
 const draft={title:'S13 样板',mode:'content_draft',pages:[{index:1,title:'封面'}]};
 const analyzeBody={mode:'content_draft',pageCount:2,outline:'两页样板',materials:'',papers:'',styleBrief:'',content_files:[],style_files:[],reference_images:[]};
 const localCreationFixture=singleJudgeFixture({localReview:true,localCreation:true,locationHref:creationOrigin});
 assert.equal(await localCreationFixture.cloud.ready(),true);
 assert.equal(localCreationFixture.cloud.localCreation,true);
 assert.equal(localCreationFixture.cloud.localCreationAvailable,true);
 assert.equal(localCreationFixture.cloud.aiAvailable,true);
 assert.equal(localCreationFixture.cloud.professionalReviewAvailable,true);
 const localRequest=localCreationFixture.context.window.GuanyeAI.request;
 await localRequest(`/api/ai/planning/history?cursor=${cursor}`);
 await localRequest('/api/ai/planning/analyze',{method:'POST',body:analyzeBody});
 await localRequest('/api/ai/planning/save',{method:'POST',body:{plan_id:planId,expected_revision:1,draft}});
 await localRequest(`/api/ai/planning?plan_id=${planId}`);
 await localRequest('/api/ai/planning/confirm',{method:'POST',body:{plan_id:planId,expected_revision:2,draft}});
 await localRequest('/api/ai/generation',{method:'POST',body:{plan_id:planId}});
 await localRequest('/api/ai/generation?action=recent');
 await localRequest(`/api/ai/generation?job_id=${jobId}`);
 await localRequest(`/api/ai/generation?plan_id=${planId}`);
 await localRequest('/api/ai/generation/retry',{method:'POST',body:{job_id:jobId,page_ids:[pageId]}});
 const topArtifact=await localRequest(`/api/ai/generation/artifact?job_id=${jobId}&page_id=${pageId}&download=1`);
 assert.equal(topArtifact.contentType,'image/png');
 const localCalls=localCreationFixture.fetchCalls.filter(row=>row.url.startsWith('http://127.0.0.1:8765/api/ai/'));
 assert.equal(localCalls.length,11);
 assert.ok(localCalls.every(row=>row.options.credentials==='omit'&&row.options.redirect==='error'&&row.options.cache==='no-store'));
 assert.ok(localCalls.every(row=>row.options.headers.Authorization==='Bearer session-token'));
 assert.ok(localCalls.every(row=>String(row.options.headers.Authorization).includes('service_role')===false));
 assert.ok(localCalls.some(row=>row.url==='/api/ai/planning/history'.replace('/api','http://127.0.0.1:8765/api')+`?cursor=${cursor}`));
 const reviewedInCreation=await localRequest('/api/ai/reviews?action=workspace&kind=uploaded');
 assert.equal(reviewedInCreation.kind,'uploaded');
 assert.equal(localCreationFixture.fetchCalls.filter(row=>row.url.startsWith('http://127.0.0.1:8765/api/ai/')).length,11,'review route must stay on the existing RPC path');
 for(const [path,options,pattern] of [
   ['/api/ai/generation/settings',{method:'POST',body:{max_workers:2}},/未开放/],
   ['/api/ai/generation/worker/claim',{method:'POST',body:{}},/未开放/],
   ['/api/ai/planning/analyze?w=other',{method:'POST',body:analyzeBody},/参数无效/],
   ['/api/ai/planning/analyze',{method:'GET'},/方法无效/],
   ['/api/ai/planning/analyze',{method:'POST',body:{...analyzeBody,extra:true}},/格式无效/],
   ['/api/ai/planning/analyze',{method:'POST',body:{...analyzeBody,content_files:[{name:'x.txt',mime_type:'text/plain',data_base64:'',extra:1}]}},/参数无效/],
   ['/api/ai/generation?action=recent&job_id='+jobId,/生成任务请求参数无效/],
   ['/api/ai/generation/retry',{method:'POST',body:{job_id:jobId,page_ids:[pageId,pageId]}},/单页重试/],
   [`/api/ai/generation/artifact?job_id=${jobId}&page_id=${pageId}&download=2`,null,/下载参数无效/],
   ['https://evil.example/api/ai/planning/history',null,/路径无效/],
   ['//evil.example/api/ai/planning/history',null,/路径无效/],
   ['/api/ai/planning%2fhistory',null,/路径无效/],
 ]){
   await assert.rejects(localRequest(path,options||{}),pattern);
 }
 const beforeInvalid=localCreationFixture.fetchCalls.length;
 assert.equal(beforeInvalid,11,'rejected routes must not reach fetch');
 const creationRefresh=singleJudgeFixture({localReview:true,localCreation:true,locationHref:creationOrigin,localResponses:[401,200]});
 assert.equal(await creationRefresh.cloud.ready(),true);
 await creationRefresh.context.window.GuanyeAI.request('/api/ai/planning/history');
 const refreshCalls=creationRefresh.fetchCalls.filter(row=>row.url.startsWith('http://127.0.0.1:8765/api/ai/'));
 assert.equal(refreshCalls.length,2);
 assert.equal(refreshCalls[0].options.headers.Authorization,'Bearer session-token');
 assert.equal(refreshCalls[1].options.headers.Authorization,'Bearer refreshed-token');
 assert.equal(creationRefresh.calls.some(row=>row.name==='refreshSession'),true);
 const adminCreation=singleJudgeFixture({access:'admin',localReview:true,localCreation:true,locationHref:creationOrigin});
 assert.equal(await adminCreation.cloud.ready(),true);
 assert.equal(adminCreation.cloud.localCreationAvailable,true);
 assert.equal(adminCreation.cloud.aiAvailable,true);
 assert.equal((await adminCreation.context.window.GuanyeAI.request('/api/ai/planning/history?cursor='+cursor)).ok,true);
 assert.equal(adminCreation.fetchCalls.filter(row=>row.url.startsWith('http://127.0.0.1:8765/api/ai/')).length,1);
 const restricted=singleJudgeFixture({access:'submitter',localReview:true,localCreation:true,locationHref:creationOrigin});
 assert.equal(await restricted.cloud.ready(),true);
 assert.equal(restricted.cloud.localCreationAvailable,false);
 assert.equal(restricted.cloud.aiAvailable,false);
 await assert.rejects(restricted.context.window.GuanyeAI.request('/api/ai/planning/history'),/无权使用策划与生图工作台/);
 assert.equal(restricted.fetchCalls.filter(row=>row.url.startsWith('http://127.0.0.1:8765/api/ai/')).length,0);
 assert.equal((await localReviewFixture.cloud.request('/api/library')).decks.length,1);
 console.log('PASS: professional scoring uses fixed authenticated workspace and bundle RPCs without an AI gateway');
 const localImportOff=singleJudgeFixture({access:'submitter',initialSession:true,localImport:false});
 assert.equal(await localImportOff.cloud.ready(),true);
 assert.equal(localImportOff.cloud.capabilities.canSubmitMaterials,true);
 assert.equal(localImportOff.cloud.localImport,false);
 assert.equal(localImportOff.cloud.professionalImportAvailable,false);
 await assert.rejects(localImportOff.cloud.request('/api/professional/import',{method:'POST',body:JSON.stringify(professionalBody(false))}),/专业投递能力未启用/);
 assert.equal(localImportOff.fetchCalls.length,0);


 function intakeFixture({role='submitter',localImport=true,uploadError=null,insertError=null,cleanupError=null}={}){
  const workspace='11111111-1111-4111-8111-111111111111';
  let session=role?{user:{id:'alice',email:'alice@example.com'},access_token:'alice-token'}:null;
  const sequence=[],uploads=[],removes=[],inserts=[],queries=[],tableFilters=[];
  const rows=[
   {id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',workspace_id:workspace,submitted_by:'alice',title:'我的旧投递',original_name:'old.pdf',object_path:workspace+'/intake/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/old.pdf',content_type:'application/pdf',size_bytes:12,sha256:'a'.repeat(64),ai_allowed:false,status:'completed',created_at:'2026-11-22T01:00:00Z'},
   {id:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',workspace_id:workspace,submitted_by:'bob',title:'他人素材',original_name:'other.pdf',object_path:workspace+'/intake/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/other.pdf',content_type:'application/pdf',size_bytes:9,sha256:'b'.repeat(64),ai_allowed:true,status:'queued',created_at:'2026-11-22T02:00:00Z'}
  ];
  const client={
   auth:{getSession:async()=>({data:{session}}),signOut:async()=>({data:null}),refreshSession:async()=>({data:{session}}),updateUser:async()=>({data:{user:{id:'alice'}}})},
   rpc:async(name,args)=>{
    if(name==='single_judge_access')return {data:{mode:'single-judge',role}};
    return {error:{message:'unexpected rpc '+name,code:'UNEXPECTED'}};
   },
   storage:{from(bucket){
    if(bucket!=='ppt-library')throw new Error('unexpected bucket '+bucket);
    return {
     upload:async(path,bytes,options)=>{sequence.push('upload');uploads.push({path,bytes,options});return uploadError?{data:null,error:uploadError}:{data:{path},error:null};},
     remove:async paths=>{sequence.push('remove');removes.push([...paths]);return cleanupError?{data:null,error:cleanupError}:{data:[],error:null};},
     createSignedUrls:async paths=>({data:paths.map(path=>({path,signedUrl:'https://signed.example/'+path})),error:null})
    };
   }},
   from(table){
    if(table!=='single_judge_intake')throw new Error('unexpected table '+table);
    const filters=[];let selected='',order=null,limitValue=null;
    const query={
     select(columns){selected=columns;return query;},
     insert(row){sequence.push('insert');inserts.push(structuredClone(row));queries.push({table,operation:'insert',filters:[],selected:'',order:null,limit:null});return Promise.resolve(insertError?{data:null,error:insertError}:{data:null,error:null});},
     eq(key,value){filters.push([key,value]);tableFilters.push([key,value]);return query;},
     order(column,options){order={column,options};return query;},
     limit(value){limitValue=value;return query;},
     then(resolve,reject){queries.push({table,operation:'select',filters:[...filters],selected,order,limit:limitValue});const data=rows.filter(row=>filters.every(([key,value])=>row[key]===value)).slice(0,limitValue||rows.length);return Promise.resolve({data:structuredClone(data),error:null}).then(resolve,reject);}
    };
    return query;
   }
  };
  const context={
   window:{CLOUD_CONFIG:{url:'https://example.supabase.co',anonKey:'test',workspaceId:workspace,singleJudge:true,localImport},supabase:{createClient:()=>client}},
   structuredClone,Map,Set,Uint8Array,ArrayBuffer,URL,URLSearchParams,atob,crypto:require('node:crypto').webcrypto,
   location:{href:'https://site.example/review/',origin:'https://site.example',reload(){}},
   document:{querySelector(){return null;}}
  };
  vm.createContext(context);vm.runInContext(fs.readFileSync('public/cloud.js','utf8'),context);
  function file(name,bytes,type=undefined,declaredSize=undefined){
   const raw=Buffer.from(bytes);
   return {name,type:type===undefined?'':type,size:declaredSize===undefined?raw.length:declaredSize,arrayBuffer:async()=>{sequence.push('arrayBuffer');return raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength);}};
  }
  return {cloud:context.window.Cloud,workspace,session,sequence,uploads,removes,inserts,queries,tableFilters,file};
 }

 const intake=intakeFixture();
 assert.equal(await intake.cloud.ready(),true);
 assert.equal(intake.cloud.role,'submitter');
 assert.equal(intake.cloud.webIntakeAvailable,true);
 assert.equal(intake.cloud.professionalImportAvailable,true,'local professional import must remain available');
 const progress=[];
 const legalFile=intake.file('deck.pdf',Buffer.from('hello'),'application/pdf');
 const receipt=await intake.cloud.submitWebIntake({title:'',aiAllowed:false,file:legalFile},{onProgress:step=>progress.push(step.stage)});
 assert.equal(receipt.title,'deck.pdf');
 assert.equal(receipt.originalName,'deck.pdf');
 assert.equal(receipt.sha256,'2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824');
 assert.deepEqual(intake.sequence,['arrayBuffer','upload','insert']);
 assert.deepEqual(progress,['hashing','uploading','inserting','complete']);
 assert.equal(intake.uploads.length,1);
 assert.match(intake.uploads[0].path,new RegExp('^'+intake.workspace+'/intake/[0-9a-f-]{36}/deck\\.pdf$')); assert.deepEqual(JSON.parse(JSON.stringify(intake.uploads[0].options)),{contentType:'application/pdf',upsert:false});
 assert.equal(intake.uploads[0].bytes.byteLength,5);
 assert.equal(intake.inserts.length,1);
 assert.deepEqual(Object.keys(intake.inserts[0]).sort(),['ai_allowed','content_type','id','object_path','original_name','sha256','size_bytes','status','submitted_by','title','workspace_id']);
 assert.equal(intake.inserts[0].workspace_id,intake.workspace);
 assert.equal(intake.inserts[0].submitted_by,'alice');
 assert.equal(intake.inserts[0].object_path,intake.uploads[0].path);
 assert.equal(intake.inserts[0].content_type,'application/pdf');
 assert.equal(intake.inserts[0].size_bytes,5);
 assert.equal(intake.inserts[0].sha256,receipt.sha256);
 assert.equal(intake.inserts[0].ai_allowed,false);
 assert.equal(intake.inserts[0].status,'queued');
 const ownRows=await intake.cloud.listMyIntakes();
 assert.deepEqual(ownRows.map(row=>row.id),['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa']);
 assert.deepEqual(intake.queries.find(row=>row.operation==='select').filters,[['workspace_id',intake.workspace],['submitted_by','alice']]);
 assert.equal(intake.queries.find(row=>row.operation==='select').selected,'id,title,original_name,object_path,content_type,size_bytes,sha256,ai_allowed,status,created_at');
 assert.equal(intake.queries.find(row=>row.operation==='select').limit,20);
 const intakeBeforeInvalid=intake.sequence.length;
 for(const bad of ['../evil.pdf','dir\\evil.pdf','bad\u0001.pdf','','.','..','bad%2fname.pdf','bad%5cname.pdf']){
  await assert.rejects(intake.cloud.submitWebIntake({title:'',aiAllowed:false,file:intake.file(bad,Buffer.from('x'),'application/pdf')}),/文件名无效|仅支持 PPT/);
 }
 assert.equal(intake.sequence.length,intakeBeforeInvalid,'invalid names must fail before hashing or upload');
 await assert.rejects(intake.cloud.submitWebIntake({title:'',aiAllowed:false,file:intake.file('fake.pdf',Buffer.from('x'),'image/png')}),/文件类型与扩展名不匹配/);
 await assert.rejects(intake.cloud.submitWebIntake({title:'',aiAllowed:false,file:intake.file('zero.pdf',Buffer.alloc(0),'application/pdf')}),/25 MiB/);
 await assert.rejects(intake.cloud.submitWebIntake({title:'',aiAllowed:false,file:intake.file('large.pdf',Buffer.from('x'),'application/pdf',25*1024*1024+1)}),/25 MiB/);
 const localized=intakeFixture();assert.equal(await localized.cloud.ready(),true);
 const localizedReceipt=await localized.cloud.submitWebIntake({title:'',aiAllowed:false,file:localized.file('Q3 秋季 报告.pdf',Buffer.from('cn'),'application/pdf')});
 assert.equal(localizedReceipt.title,'Q3 秋季 报告.pdf');
 assert.equal(localizedReceipt.originalName,'Q3.pdf');
 assert.match(localized.uploads[0].path,/\/Q3\.pdf$/);
 assert.equal(localized.inserts[0].original_name,'Q3.pdf');
 for(const [name,type,expected] of [
  ['slides.ppt','application/vnd.ms-powerpoint','application/vnd.ms-powerpoint'],
  ['slides.pptx','application/vnd.openxmlformats-officedocument.presentationml.presentation','application/vnd.openxmlformats-officedocument.presentationml.presentation'],
  ['slides.pdf','application/pdf','application/pdf'],
 ]){
  const item=intakeFixture(),f=item.file(name,Buffer.from(name),type);assert.equal(await item.cloud.ready(),true);await item.cloud.submitWebIntake({title:'',aiAllowed:true,file:f});assert.equal(item.uploads[0].options.contentType,expected);assert.equal(item.inserts[0].ai_allowed,true);
 }
 const uploadFailure=intakeFixture({uploadError:{message:'storage unavailable',code:'STORAGE'}});assert.equal(await uploadFailure.cloud.ready(),true);
 await assert.rejects(uploadFailure.cloud.submitWebIntake({title:'',aiAllowed:false,file:uploadFailure.file('fail.pdf',Buffer.from('fail'),'application/pdf')}),/storage unavailable/);
 assert.equal(uploadFailure.inserts.length,0);assert.equal(uploadFailure.removes.length,0);assert.equal(uploadFailure.cloud.webIntakeAvailable,true);
 const insertFailure=intakeFixture({insertError:{message:'intake denied',code:'RLS'}});assert.equal(await insertFailure.cloud.ready(),true);
 await assert.rejects(insertFailure.cloud.submitWebIntake({title:'',aiAllowed:false,file:insertFailure.file('denied.pdf',Buffer.from('denied'),'application/pdf')}),error=>error.message==='intake denied'&&!error.cleanupWarning);
 assert.equal(insertFailure.uploads.length,1);assert.equal(insertFailure.removes.length,1);assert.deepEqual(insertFailure.removes[0],[insertFailure.uploads[0].path]);
 const cleanupFailure=intakeFixture({insertError:{message:'intake denied',code:'RLS'},cleanupError:{message:'delete policy denied',code:'STORAGE'}});assert.equal(await cleanupFailure.cloud.ready(),true);
 await assert.rejects(cleanupFailure.cloud.submitWebIntake({title:'',aiAllowed:false,file:cleanupFailure.file('cleanup.pdf',Buffer.from('cleanup'),'application/pdf')}),error=>{assert.equal(error.message,'intake denied');assert.match(error.cleanupWarning,/清理未完成/);return true;});
 assert.equal(cleanupFailure.inserts.length,1);assert.equal(cleanupFailure.removes.length,1);
 await assert.rejects(intake.cloud.request('/api/single-judge/intake/claim',{method:'POST',body:'{}'}),/云端暂不支持此操作/);
 await assert.rejects(intake.cloud.request('/api/single-judge/intake/complete',{method:'POST',body:'{}'}),/云端暂不支持此操作/);
 const judgeIntake=intakeFixture({role:'judge',localImport:true});assert.equal(await judgeIntake.cloud.ready(),true);assert.equal(judgeIntake.cloud.webIntakeAvailable,true);assert.equal(judgeIntake.cloud.professionalImportAvailable,true);
 const noSession=intakeFixture({role:null,localImport:true});assert.equal(await noSession.cloud.ready(),false);assert.equal(noSession.cloud.webIntakeAvailable,false);
 assert.equal(fs.readFileSync('public/cloud.js','utf8').includes('service_role'),false);
 console.log('PASS: M35 web intake hashes locally, uploads before insert, maps MIME, rejects unsafe input, keeps local import, reads only own receipts, and preserves primary errors when cleanup fails');
 console.log('PASS: legacy cloud transport unchanged; single-judge roles, strict manual-only import, single-owner localCreation planning/generation with same-origin path allowlist, bearer refresh, no token output');
})().catch(e=>{console.error(e);process.exit(1);});
