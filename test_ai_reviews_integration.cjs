// Integration checks for AI pre-evaluation, library visibility, legacy guards, and browser transport.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict'),vm=require('node:vm');
const W='11111111-1111-4111-8111-111111111111';
const JUDGE='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const MEMBER='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
async function setup(){
 const db=new PGlite({extensions:{pgcrypto}});
 await db.exec("create schema auth; create schema storage; create schema extensions; create role anon; create role authenticated; create role service_role bypassrls; create table auth.users(id uuid primary key); create function auth.uid() returns uuid language sql stable as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$; create function auth.jwt() returns jsonb language sql stable as $$select jsonb_build_object('email',current_setting('request.jwt.claim.email',true))$$; create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint); create table storage.objects(id uuid,name text,bucket_id text); create function storage.foldername(text) returns text[] language sql as $$select string_to_array($1,'/')$$;");
 await db.exec(fs.readFileSync('cloud/schema.sql','utf8'));
 await db.exec(fs.readFileSync('cloud/migrations/20261121_02_ai_reviews.sql','utf8'));
 await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,'w','a@b.c','x')",[W]);
 await db.query('insert into auth.users values($1),($2)',[JUDGE,MEMBER]);
 await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'评委','admin'),($1,$3,'成员','reviewer')",[W,JUDGE,MEMBER]);
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W,JUDGE]);
 return db;
}
async function login(db,id){await db.exec('reset role');await db.query("select set_config('request.jwt.claim.sub',$1,false)",[id]);await db.exec('set role authenticated');}
async function finishOne(db,scope,unitIds){
 const prep=(await db.query('select ai_review_prepare($1,$2,$3) j',[W,JUDGE,{deckId:'gen-page',scope,unitIds}])).rows[0].j;
 const jobs=(await db.query('select ai_review_claim_jobs($1,$2,$3,$4) j',[W,JUDGE,'generated',8])).rows[0].j;
 for(const job of jobs){
  await db.query('select ai_review_finish_job($1,$2,$3,$4,$5,$6)',[W,JUDGE,job.id,'completed',{scores:{visual:4,layout:3,match:4},classification:{label:'模型'},reasons:['模型理由'],modelHint:'test'},null]);
 }
 return {prep,jobs};
}
(async()=>{
 const db=await setup();
 const uploaded={id:'up-1',name:'上传',title:'上传',images:[{id:'u1',name:'u1.png',path:W+'/up-1/u1.png'}]};
 const uploadedExisting={id:'up-existing',name:'已有分类',title:'已有分类',classification:'原分类',style:'原风格',images:[{id:'e1',name:'e1.png',path:W+'/up-existing/e1.png'}]};
 const generated={id:'gen-page',name:'生成',title:'生成',generation:{outline:'o',style:'s',pages:[],references:[]},images:[{id:'g1',name:'g1.png',path:W+'/gen-page/g1.png'},{id:'g2',name:'g2.png',path:W+'/gen-page/g2.png'}]};
 const generatedWhole={id:'gen-whole',name:'整生成',title:'整生成',generation:{outline:'o',style:'s',pages:[],references:[]},images:[{id:'w1',name:'w1.png',path:W+'/gen-whole/w1.png'}]};
 const generatedLate={id:'gen-late',name:'迟到',title:'迟到',generation:{outline:'o',style:'s',pages:[],references:[]},images:[{id:'l1',name:'l1.png',path:W+'/gen-late/l1.png'}]};
 await db.query("insert into decks(id,workspace_id,source_kind,data) values($1,$2,'uploaded',$3),($4,$2,'uploaded',$5),($6,$2,'generated',$7),($8,$2,'generated',$9),($10,$2,'generated',$11)",[uploaded.id,W,uploaded,uploadedExisting.id,uploadedExisting,generated.id,generated,generatedWhole.id,generatedWhole,generatedLate.id,generatedLate]);
 const initialDeckHash=(await db.query('select ai_review_deck_version($1,$2) h',[W,generated.id])).rows[0].h;
 // Preevaluate one generated page before any human submission.
 await db.query('select ai_review_prepare($1,$2,$3)',[W,JUDGE,{deckId:generated.id,scope:'page',unitIds:['g1']}]);
 let jobs=(await db.query('select ai_review_claim_jobs($1,$2,$3,$4) j',[W,JUDGE,'generated',4])).rows[0].j;
 assert.equal(jobs.length,1);
 await db.query('select ai_review_finish_job($1,$2,$3,$4,$5,$6)',[W,JUDGE,jobs[0].id,'completed',{scores:{visual:4,layout:3,match:4},classification:{primary:'AI分类',style:'AI风格'},reasons:['模型理由'],modelHint:'test'},null]);
 assert.equal((await db.query('select count(*)::int n from ai_review_records')).rows[0].n,1);
 assert.equal((await db.query('select count(*)::int n from ai_review_submissions')).rows[0].n,0);
 assert.equal((await db.query('select count(*)::int n from ai_review_matches')).rows[0].n,0);
 const notesOnly=(await db.query('select ai_review_submit($1,$2,$3) j',[W,MEMBER,{deckId:generated.id,scope:'page',units:[{unitId:'g1',position:1,scores:{},note:'只有笔记'}]}])).rows[0].j;
 assert.equal(notesOnly.matchedAssisted,0);assert.equal(notesOnly.predictionAvailable,false);
 assert.equal((await db.query('select count(*)::int n from ai_review_matches')).rows[0].n,0,'notes-only submission must not unlock pre-evaluation');
 await login(db,MEMBER);
 let library=(await db.query('select get_review_library($1) j',[W])).rows[0].j;
 assert.deepEqual(library.decks.map(d=>d.id),['up-1','up-existing'],'unreviewed generated deck must not enter ordinary library');
 const staleReview={scores:{},mode:'page',pages:{g1:{scores:{visual:2,layout:2,match:2}}}};
 await db.exec('reset role');await db.exec('set role service_role');
 await assert.rejects(db.query('insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4)',[W,MEMBER,generated.id,staleReview]),/尚未正式入库/);
 await db.exec('reset role');
 // Generated AI-track submit stores only ai_review_submissions, not legacy reviews.
 const submit=(await db.query('select ai_review_submit($1,$2,$3) j',[W,MEMBER,{deckId:generated.id,scope:'page',units:[{unitId:'g1',position:1,scores:{visual:2,layout:2,match:2}}]}])).rows[0].j;
 assert.equal(submit.matchedAssisted,1);
 assert.equal((await db.query('select count(*)::int n from ai_review_jobs')).rows[0].n,1,'human submit must reuse pre-eval job');
 assert.equal((await db.query('select count(*)::int n from reviews where deck_id=$1',[generated.id])).rows[0].n,0,'generated formal track must not write legacy reviews');
 const sid=submit.submissionIds[0];
 await assert.rejects(db.query('select ai_review_confirm($1,$2,$3)',[W,JUDGE,{submissionId:sid}]),/正式分/);
 const lowDecision=(await db.query('select ai_review_confirm($1,$2,$3) j',[W,JUDGE,{submissionId:sid,formalScores:{visual:4,layout:3,match:4},classification:null,reasons:null,idempotencyKey:'page-low'}])).rows[0].j;
 assert.equal(lowDecision.scoreRatio,.74);
 await login(db,MEMBER);library=(await db.query('select get_review_library($1) j',[W])).rows[0].j;assert.ok(!library.decks.find(d=>d.id===generated.id),'formal score alone must not admit');
 await db.exec('reset role');
 await assert.rejects(db.query('select ai_review_admit($1,$2,$3)',[W,JUDGE,{submissionId:sid,decisionId:lowDecision.decisionId}]),/未达到/);
 const passDecision=(await db.query('select ai_review_confirm($1,$2,$3) j',[W,JUDGE,{submissionId:sid,formalScores:{visual:4.25,layout:4.25,match:4.25},idempotencyKey:'page-pass'}])).rows[0].j;
 assert.equal(passDecision.scoreRatio,.85);
 await login(db,MEMBER);library=(await db.query('select get_review_library($1) j',[W])).rows[0].j;assert.ok(!library.decks.find(d=>d.id===generated.id),'must wait for explicit admit');
 await db.exec('reset role');
 const admitted=(await db.query('select ai_review_admit($1,$2,$3) j',[W,JUDGE,{submissionId:sid,decisionId:passDecision.decisionId}])).rows[0].j;
 assert.equal(admitted.admitted,true);assert.equal(admitted.libraryLabel,'AI 自我学习生成');
 const admittedAgain=(await db.query('select ai_review_admit($1,$2,$3) j',[W,JUDGE,{submissionId:sid,decisionId:passDecision.decisionId}])).rows[0].j;
 assert.equal(admittedAgain.idempotent,true);
 assert.equal((await db.query('select ai_review_deck_version($1,$2) h',[W,generated.id])).rows[0].h,initialDeckHash,'admit must not mutate deck data');
 await login(db,MEMBER);
 library=(await db.query('select get_review_library($1) j',[W])).rows[0].j;
 const partial=library.decks.find(d=>d.id===generated.id);
 assert.ok(partial,'approved generated page must enter ordinary library');
 assert.equal(partial.data.libraryScope,'pages');assert.deepEqual(partial.data.images.map(i=>i.id),['g1']);assert.equal(partial.data.aiLabel,'AI 自我学习生成');
 await db.exec('reset role');await db.exec('set role service_role');
 await db.query('insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4) on conflict(workspace_id,user_id,deck_id) do update set data=excluded.data',[W,MEMBER,generated.id,{scores:{},mode:'page',pages:{g1:{scores:{visual:2,layout:2,match:2}}}}]);
 await assert.rejects(db.query('insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4)',[W,MEMBER,generated.id,{scores:{},mode:'page',pages:{g2:{scores:{visual:2,layout:2,match:2}}}}]),/尚未正式入库/);
 await db.exec('reset role');
 // Whole generated deck preevaluation and independent admission.
 await db.query('select ai_review_prepare($1,$2,$3)',[W,JUDGE,{deckId:generatedWhole.id,scope:'deck'}]);
 const wholeJobs=(await db.query('select ai_review_claim_jobs($1,$2,$3,$4) j',[W,JUDGE,'generated',4])).rows[0].j;assert.equal(wholeJobs.length,1);
 await db.query('select ai_review_finish_job($1,$2,$3,$4,$5,$6)',[W,JUDGE,wholeJobs[0].id,'completed',{scores:{visual:4,layout:4,match:4},modelHint:'test'},null]);
 const wholeSubmit=(await db.query('select ai_review_submit($1,$2,$3) j',[W,MEMBER,{deckId:generatedWhole.id,scope:'deck',units:[{scores:{visual:3,layout:3,match:3}}]}])).rows[0].j;assert.equal(wholeSubmit.matchedAssisted,1);
 const wholeLow=(await db.query('select ai_review_confirm($1,$2,$3) j',[W,JUDGE,{submissionId:wholeSubmit.submissionIds[0],formalScores:{visual:4,layout:4,match:4},idempotencyKey:'whole-low'}])).rows[0].j;assert.equal(wholeLow.scoreRatio,.8);
 await assert.rejects(db.query('select ai_review_admit($1,$2,$3)',[W,JUDGE,{submissionId:wholeSubmit.submissionIds[0],decisionId:wholeLow.decisionId}]),/未达到/);
 const wholePass=(await db.query('select ai_review_confirm($1,$2,$3) j',[W,JUDGE,{submissionId:wholeSubmit.submissionIds[0],formalScores:{visual:4.25,layout:4.25,match:4.25},idempotencyKey:'whole-pass'}])).rows[0].j;
 await db.query('select ai_review_admit($1,$2,$3)',[W,JUDGE,{submissionId:wholeSubmit.submissionIds[0],decisionId:wholePass.decisionId}]);
 // Late pre-evaluation links only complete submissions, never notes-only rows.
 await db.query('select ai_review_submit($1,$2,$3)',[W,MEMBER,{deckId:generatedLate.id,scope:'page',units:[{unitId:'l1',position:1,scores:{},note:'late notes only'}]}]);
 const lateComplete=(await db.query('select ai_review_submit($1,$2,$3) j',[W,MEMBER,{deckId:generatedLate.id,scope:'page',units:[{unitId:'l1',position:1,scores:{visual:3,layout:3,match:3}}]}])).rows[0].j;
 assert.equal(lateComplete.matchedAssisted,0);
 await db.query('select ai_review_prepare($1,$2,$3)',[W,JUDGE,{deckId:generatedLate.id,scope:'page',unitIds:['l1']}]);
 const lateJobs=(await db.query('select ai_review_claim_jobs($1,$2,$3,$4) j',[W,JUDGE,'generated',4])).rows[0].j;assert.equal(lateJobs.length,1);
 const lateFinish=(await db.query('select ai_review_finish_job($1,$2,$3,$4,$5,$6) j',[W,JUDGE,lateJobs[0].id,'completed',{scores:{visual:4,layout:3,match:4},modelHint:'test'},null])).rows[0].j;
 assert.equal(lateFinish.matchedSubmissions,1,'late pre-eval must link only the complete submission');
 assert.equal((await db.query('select count(*)::int n from ai_review_matches where record_id=$1',[lateFinish.recordId])).rows[0].n,1);
 // Uploaded classification priority: AI only when no original class and no judge/human correction.
 const uploadVersion=await db.query('select ai_review_deck_version($1,$2) h',[W,uploaded.id]);
 await db.query('select ai_review_prepare($1,$2,$3)',[W,JUDGE,{deckId:uploaded.id,scope:'deck'}]);
 const uploadJobs=(await db.query('select ai_review_claim_jobs($1,$2,$3,$4) j',[W,JUDGE,'uploaded',4])).rows[0].j;assert.equal(uploadJobs.length,1);
 await db.query('select ai_review_finish_job($1,$2,$3,$4,$5,$6)',[W,JUDGE,uploadJobs[0].id,'completed',{scores:{visual:4,layout:4},classification:{primary:'AI建议分类',style:'AI建议风格'},reasons:['AI'],modelHint:'test'},null]);
 await db.query('select ai_review_submit($1,$2,$3)',[W,MEMBER,{deckId:uploaded.id,scope:'deck',units:[{scores:{visual:4,layout:4}}]}]);
 await login(db,MEMBER);library=(await db.query('select get_review_library($1) j',[W])).rows[0].j;
 let up=library.decks.find(d=>d.id===uploaded.id);assert.equal(up.data.effectiveClassification,'AI建议分类');assert.equal(up.data.classificationSource,'ai');assert.equal(up.data.effectiveStyle,'AI建议风格');assert.equal(up.data.styleSource,'ai');
 await db.exec('reset role');
 await db.query('select ai_review_submit($1,$2,$3)',[W,MEMBER,{deckId:uploaded.id,scope:'deck',units:[{scores:{visual:4,layout:4},classificationCorrection:{primary:'人工纠偏分类',style:'人工纠偏风格'}}]}]);
 await login(db,MEMBER);library=(await db.query('select get_review_library($1) j',[W])).rows[0].j;
 up=library.decks.find(d=>d.id===uploaded.id);assert.equal(up.data.effectiveClassification,'人工纠偏分类');assert.equal(up.data.classificationSource,'human');assert.equal(up.data.effectiveStyle,'人工纠偏风格');
 // Existing uploaded classification wins over a whole-deck AI suggestion.
 await db.exec('reset role');
 await db.query('select ai_review_prepare($1,$2,$3)',[W,JUDGE,{deckId:uploadedExisting.id,scope:'deck'}]);
 const existingJobs=(await db.query('select ai_review_claim_jobs($1,$2,$3,$4) j',[W,JUDGE,'uploaded',4])).rows[0].j;assert.equal(existingJobs.length,1);
 await db.query('select ai_review_finish_job($1,$2,$3,$4,$5,$6)',[W,JUDGE,existingJobs[0].id,'completed',{scores:{visual:3,layout:3},classification:{primary:'不应覆盖',style:'不应覆盖'},modelHint:'test'},null]);
 await db.query('select ai_review_submit($1,$2,$3)',[W,MEMBER,{deckId:uploadedExisting.id,scope:'deck',units:[{scores:{visual:3,layout:3}}]}]);
 await login(db,MEMBER);library=(await db.query('select get_review_library($1) j',[W])).rows[0].j;const ex=library.decks.find(d=>d.id===uploadedExisting.id);assert.equal(ex.data.effectiveClassification,'原分类');assert.equal(ex.data.classificationSource,'existing');assert.equal(ex.data.effectiveStyle,'原风格');
 // Old ordinary scoring and global max-score update must keep working with generated AI-track state.
 let state=(await db.query('select get_review_state($1) j',[W])).rows[0].j;state.reviews['up-1']={scores:{visual:4,layout:4},note:'legacy'};
 await db.query('select save_review_state($1,$2)',[W,state]);
 state=(await db.query('select get_review_state($1) j',[W])).rows[0].j;state.maxScore=10;state.reviews['up-1']={scores:{visual:8,layout:8},note:'legacy'};
 await db.query('select save_review_state($1,$2)',[W,state]);
 await db.exec('reset role');
  assert.equal(Number((await db.query('select (settings->>\'maxScore\')::numeric m from workspaces where id=$1',[W])).rows[0].m),10);
 assert.ok((await db.query('select count(*)::int n from ai_review_submissions')).rows[0].n>=5);
 assert.ok((await db.query('select count(*)::int n from ai_review_decisions')).rows[0].n>=4);
 await db.exec('reset role');
 await db.close();
 // Browser transport contract: body is a JSON object and auth is bearer-only.
 const calls=[];const client={auth:{getSession:async()=>({data:{session:{access_token:'user-token'}}})}};
 const makeFakeElement=(tagName='div')=>({tagName,id:'',className:'',textContent:'',innerHTML:'',dataset:{},attributes:{},children:[],setAttribute(name,value){this.attributes[name]=String(value);},removeAttribute(name){delete this.attributes[name];},getAttribute(name){return this.attributes[name]??null;},appendChild(child){this.children.push(child);return child;},replaceChildren(...children){this.children=[...children];},addEventListener(){},removeEventListener(){},querySelector(){return null;},querySelectorAll(){return [];},focus(){}});
 const createdElements=[];const head=makeFakeElement('head');const fakeDocument={head,documentElement:head,body:makeFakeElement('body'),getElementById:()=>null,createElement:tagName=>{const node=makeFakeElement(tagName);createdElements.push(node);return node;}};
 const window={CLOUD_CONFIG:{url:'https://example.supabase.co',anonKey:'anon',workspaceId:W},supabase:{createClient:()=>client},document:fakeDocument,fetch:async(url,options)=>{calls.push({url,options});return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({ok:true,decks:[]})};},URLSearchParams,Map,Set,Number,String,Object,Array,JSON,console};
 const context=vm.createContext({window,fetch:window.fetch,URLSearchParams,Map,Set,Number,String,Object,Array,JSON,console});vm.runInContext(fs.readFileSync('public/ai-reviews.js','utf8'),context);
 const result=await context.window.GuanyeAI.request('/api/ai/reviews?action=library',{method:'POST',body:{workspace:'ignored'}});
 assert.deepEqual(result,{ok:true,decks:[]});assert.equal(calls.length,1);assert.equal(calls[0].options.method,'POST');assert.deepEqual(JSON.parse(calls[0].options.body),{workspace:'ignored'});assert.equal(calls[0].options.headers.Authorization,'Bearer user-token');assert.equal(typeof context.window.GuanyeReviews.mount,'function');
 const root=makeFakeElement('main');root.addEventListener=()=>{};const instance=context.window.GuanyeReviews.mount(root,{kind:'uploaded'});await new Promise(resolve=>setTimeout(resolve,0));assert.equal(root.children.length,2);assert.equal(root.children[0].id,'ga-review-live');assert.equal(root.children[0].attributes.role,'status');assert.equal(root.children[0].attributes['aria-live'],'polite');assert.equal(root.children[0].attributes['aria-atomic'],'true');const renderedView=root.children.find(node=>node.tagName==='div'&&node.id==='');assert.ok(renderedView);assert.ok(renderedView.innerHTML.includes('AI 盲评'));assert.equal(typeof instance.refresh,'function');
 console.log('PASS: pre-eval before human, blind library, no duplicate jobs, same-scope unlock, formal-only classification/reasons, legacy guard, browser transport');
})().catch(e=>{console.error(e.stack||e.message);process.exit(1);});
