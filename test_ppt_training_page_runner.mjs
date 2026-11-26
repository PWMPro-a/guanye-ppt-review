import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {mkdtemp,readFile,readdir,rmdir,unlink,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {runPage} from './cloud/run_ppt_training_page.mjs';

const sharp=createRequire(import.meta.url)('sharp');
const sha=b=>createHash('sha256').update(b).digest('hex');
const config={workspaceId:'dc13c526-4c6a-52c9-8cf7-3dbe0c4c30b7',url:'https://etorcisrfcbtagnxrpau.supabase.co',coverTrainingSite:true,anonKey:'fake-pilot-anon'};
const secrets={imageKey:'fake-image-key',reviewerPassword:'fake-reviewer-password'};
const png=(height=18,r=9)=>sharp({create:{width:32,height,channels:3,background:{r,g:42,b:115}}}).png().toBuffer();
const spec={scope:'page',arm:'baseline',position:1,pageCount:1,prompt:'One content slide about library services.'};
const fileResponse=bytes=>Response.json({data:[{b64_json:bytes.toString('base64')}]});
async function setup(t) {
 const dir=await mkdtemp(join(tmpdir(),'guanye-pilot-page-'));
 t.after(async()=>{for(const file of await readdir(dir)) await unlink(join(dir,file)); await rmdir(dir);});
 return dir;
}
function mockReference(bytes, callback, {revoke=false,replacePage=false}={}) {
 const deckId='library-source', pageId='verified-page';
 const ref={deckId,pageId,position:2,sha256:sha(bytes)};
 const path=`${config.workspaceId}/${deckId}/preview/2.png`;
 let checks=0, reads=0;
 return {ref,fetcher:async (url,opt)=>{
  const u=new URL(url);
  assert.equal(opt.redirect,'error');
  if(u.origin==='https://img.937ai.chat') return callback(u,opt);
  assert.equal(u.origin,config.url);
  assert.equal(opt.headers.apikey,config.anonKey);
  if(u.pathname.startsWith('/auth/')) {
   assert.equal(JSON.parse(opt.body).password,secrets.reviewerPassword);
   return Response.json({access_token:'test-only-session'});
  }
  assert.equal(opt.headers.Authorization,'Bearer test-only-session');
  if(u.pathname.endsWith('/single_judge_ppt_training_reference')) {
   checks++;
   return Response.json(revoke && checks>1 ? null : {deckId,split:'calibration',bucket:1,pageCount:2,scoreSource:'team',authorizedPagePaths:[null,path]});
  }
  if(u.pathname.endsWith('/single_judge_ppt_training_samples')) {
   reads++;
   return Response.json([{deckId,images:[{}, {id:replacePage && reads>1?'different-page':pageId,path}]}]);
  }
  assert.equal(u.pathname,`/storage/v1/object/authenticated/ppt-library/${path}`);
  return new Response(bytes);
 }};
}

test('baseline sends one text-only request and retains a verified full PNG',async t=>{
 const dir=await setup(t), bytes=await png(); let calls=0;
 const fetcher=async(url,opt)=>{
  calls++;
  assert.equal(url,'https://img.937ai.chat/v1/images/generations');
  assert.equal(opt.headers.Authorization,`Bearer ${secrets.imageKey}`);
  const body=JSON.parse(opt.body);
  assert.deepEqual(Object.keys(body),['model','model_config_key','prompt','n','size','response_format']);
  assert.equal(body.response_format,'b64_json');
  assert.equal(body.model,'gpt-image-2.6-sunburst');
  assert.equal(body.model_config_key,'gpt-image-2.6-sunburst');
  assert.equal(body.n,1);
  assert.equal(body.size,'1536x864');
  return fileResponse(bytes);
 };
 const r=await runPage(spec,config,secrets,dir,fetcher);
 assert.equal(r.status,'verified-16-by-9');
 assert.equal(r.output.sha256,sha(bytes));
 assert.equal(sha(await readFile(join(dir,r.output.file))),r.output.sha256);
 assert.equal(r.reference,undefined);
 for(const file of (await readdir(dir)).filter(f=>f.endsWith('.json'))) {
  const text=await readFile(join(dir,file),'utf8');
  for(const secret of Object.values(secrets)) assert.ok(!text.includes(secret));
 }
 await assert.rejects(runPage(spec,config,secrets,dir,fetcher),{code:'EEXIST'});
 assert.equal(calls,1);
});

test('reference arm binds the raw preview and feedback retains the same reference and original prompt',async t=>{
 const dir=await setup(t), refBytes=await png(18,10), first=await png(18,20), revised=await png(18,21);
 const prompts=[]; const inputs=[];
 const {ref,fetcher}=mockReference(refBytes,async(u,opt)=>{
  assert.equal(u.pathname,'/v1/images/edits');
  assert.ok(opt.body instanceof FormData);
  inputs.push(sha(Buffer.from(await opt.body.get('image').arrayBuffer())));
  assert.ok(!opt.headers.Authorization.includes('test-only-session'));
  prompts.push(opt.body.get('prompt'));
  return fileResponse(prompts.length===1?first:revised);
 });
 const r=await runPage({...spec,arm:'reference',reference:ref},config,secrets,dir,fetcher);
 assert.equal(r.status,'verified-16-by-9');
 const change={...spec,arm:'confirmed-feedback',reference:ref,feedback:{confirmationId:'review-1',parentSha256:sha(first),keep:'Keep title',change:'Improve alignment',doNotChange:'Do not add figures'}};
 await assert.rejects(runPage({...change,prompt:'Different title'},config,secrets,dir,fetcher),/matching verified reference-arm/);
 await assert.rejects(runPage({...change,model:'gpt-image-2.5-sunburst'},config,secrets,dir,fetcher),/matching verified reference-arm/);
 await assert.rejects(runPage({...change,responseFormat:'url'},config,secrets,dir,fetcher),/matching verified reference-arm/);
 const v=await runPage(change,config,secrets,dir,fetcher);
 assert.equal(v.status,'verified-16-by-9');
 assert.equal(v.feedback.parentSha256,r.output.sha256);
 assert.equal(v.feedback.authority,'caller-declared');
 assert.equal(prompts.length,2);
 assert.deepEqual(inputs,[sha(refBytes),sha(first)]);
 assert.equal(v.feedback.parentInputSha256,sha(first));
 assert.ok(prompts.every(text=>text.startsWith(spec.prompt)));
 assert.match(prompts[1],/Edit the attached previous PowerPoint slide/);
 assert.match(prompts[1],/Improve alignment/);
});

test('feedback rejects changed parent bytes or mismatched quality before any outbound request',async t=>{
 const dir=await setup(t), referenceBytes=await png(18,31), first=await png(18,32), replacement=await png(18,33);
 let edits=0;
 const {ref,fetcher}=mockReference(referenceBytes,async()=>{edits++; return fileResponse(first);});
 const seed={...spec,arm:'reference',quality:'medium',reference:ref};
 const result=await runPage(seed,config,secrets,dir,fetcher);
 assert.equal(result.status,'verified-16-by-9');
 const revision={...seed,arm:'confirmed-feedback',feedback:{confirmationId:'review-2',parentSha256:sha(first),keep:'Keep headline',change:'Align captions',doNotChange:'Keep body text'}};
 const noFetch=async()=>{assert.fail('Preflight must reject before network access');};
 await assert.rejects(runPage({...revision,quality:'high'},config,secrets,dir,noFetch),/matching verified reference-arm/);
 await writeFile(join(dir,result.output.file),replacement);
 await assert.rejects(runPage(revision,config,secrets,dir,noFetch),/Feedback parent bytes changed/);
 assert.equal(edits,1);
 assert.ok(!(await readdir(dir)).some(file=>file.startsWith('page-confirmed-feedback-')));
});

test('a revoked reference never dispatches the image request',async t=>{
 const dir=await setup(t), bytes=await png(); let calls=0;
 const {ref,fetcher}=mockReference(bytes,async()=>{calls++; return fileResponse(bytes);},{revoke:true});
 const r=await runPage({...spec,arm:'reference',reference:ref},config,secrets,dir,fetcher);
 assert.equal(r.status,'preflight-rejected');
 assert.equal(calls,0);
 assert.ok(!(await readdir(dir)).some(file=>file.endsWith('.dispatch.json')));
});

test('uncertain dispatch is not retried and non-wide output stays diagnostic',async t=>{
 const dir=await setup(t), other=await setup(t); let calls=0;
 const fetcher=async()=>{calls++; throw new TypeError('Network error');};
 assert.equal((await runPage(spec,config,secrets,dir,fetcher)).status,'dispatch-not-confirmed');
 await assert.rejects(runPage(spec,config,secrets,dir,fetcher),{code:'EEXIST'});
 assert.equal(calls,1);
 const bytes=await png(24);
 const r=await runPage(spec,config,secrets,other,async()=>fileResponse(bytes));
 assert.equal(r.status,'diagnostic-wrong-aspect');
 assert.equal(r.output.sha256,sha(bytes));
});


test('a changed reference digest and a non-pilot config fail before image dispatch',async t=>{
 const dir=await setup(t), bytes=await png(); let calls=0;
 const {ref,fetcher}=mockReference(bytes,async()=>{calls++;return fileResponse(bytes);});
 await assert.rejects(runPage(spec,{...config,workspaceId:'other'},secrets,dir,fetcher),/Isolated pilot config/);
 const r=await runPage({...spec,arm:'reference',reference:{...ref,sha256:'0'.repeat(64)}},config,secrets,dir,fetcher);
 assert.equal(r.status,'preflight-rejected');
 assert.equal(calls,0);
});

test('HTTP 200 alone cannot certify an image, and URL-only payloads are never fetched',async t=>{
 const dir=await setup(t); let calls=0;
 const r=await runPage(spec,config,secrets,dir,async()=>{
  calls++;
  return Response.json({data:[{url:'https://example.invalid/not-a-local-artifact'}]});
 });
 assert.equal(r.httpStatus,200);
 assert.equal(r.status,'invalid-or-unsaved-result');
 assert.equal(r.output,undefined);
 assert.equal(calls,1);
});

test('a reference without a page ID resolves it from the authorized pilot page',async t=>{
 const dir=await setup(t), bytes=await png(); let calls=0;
 const {ref,fetcher}=mockReference(bytes,async()=>{calls++; return fileResponse(await png(18,20));});
 const {pageId,...selection}=ref;
 const r=await runPage({...spec,arm:'reference',reference:selection},config,secrets,dir,fetcher);
 assert.equal(r.status,'verified-16-by-9');
 assert.equal(r.reference.pageId,pageId);
 assert.equal(calls,1);
});

test('when the pilot page ID changes under the same path the request is not dispatched',async t=>{
 const dir=await setup(t), bytes=await png(); let calls=0;
 const {ref,fetcher}=mockReference(bytes,async()=>{calls++; return fileResponse(bytes);},{replacePage:true});
 const {pageId,...selection}=ref;
 const r=await runPage({...spec,arm:'reference',reference:selection},config,secrets,dir,fetcher);
 assert.equal(r.status,'preflight-rejected');
 assert.equal(calls,0);
 assert.ok(!(await readdir(dir)).some(file=>file.endsWith('.dispatch.json')));
});

test('only the two requested models are allowed and the alternate is explicit',async t=>{
 const dir=await setup(t), bytes=await png(); const calls=[];
 const fetcher=async(url,opt)=>{calls.push(JSON.parse(opt.body).model);return fileResponse(bytes);};
 await assert.rejects(runPage({...spec,model:'arbitrary-model'},config,secrets,dir,fetcher),/Unknown pilot model/);
 assert.deepEqual(await readdir(dir),[]);
 const result=await runPage({...spec,model:'gpt-image-2.5-sunburst'},config,secrets,dir,fetcher);
 assert.deepEqual(calls,['gpt-image-2.5-sunburst']);
 assert.equal(result.model,calls[0]);
});

test('HTTP errors record only a safe category and never trigger fallback',async t=>{
 const dir=await setup(t); const calls=[];
 const r=await runPage(spec,config,secrets,dir,async(url,opt)=>{
  calls.push(JSON.parse(opt.body).model);
  return Response.json({error:{param:'size',message:`echoed ${secrets.imageKey}`}},{status:400});
 });
 assert.deepEqual(calls,['gpt-image-2.6-sunburst']);
 assert.equal(r.status,'http-not-success');
 assert.equal(r.httpStatus,400);
 assert.equal(r.errorCategory,'image-size');
 assert.doesNotMatch(await readFile(join(dir,'page-baseline-01.result.json'),'utf8'),/echoed|fake-image-key/);
});

test('blocked policy placeholder is not accepted as a generated artifact',async t=>{
 const dir=await setup(t), bytes=await png();
 const r=await runPage(spec,config,secrets,dir,async()=>Response.json({data:[{b64_json:bytes.toString('base64'),generation_status:'blocked',content_policy:{blocked:true}}]}));
 assert.equal(r.status,'blocked-result');
 assert.equal(r.errorCategory,'content-policy-blocked');
 assert.equal(r.output,undefined);
 assert.ok(!(await readdir(dir)).some(file=>file.endsWith('.png')));
});
test('explicit URL mode downloads the channel PNG without credentials or redirects',async t=>{
 const dir=await setup(t), bytes=await png();
 const imageUrl='https://img.937ai.chat/api/images/file/pilot.png?signature=private-test-value';
 const requests=[];
 const r=await runPage({...spec,responseFormat:'url'},config,secrets,dir,async(url,opt)=>{
  requests.push({url,opt});
  if(requests.length===1) return Response.json({data:[{url:imageUrl}]});
  return new Response(bytes);
 });
 assert.equal(JSON.parse(requests[0].opt.body).response_format,'url');
 assert.equal(r.status,'verified-16-by-9');
 assert.equal(r.responseFormat,'url');
 assert.equal(r.output.sha256,sha(bytes));
 assert.equal(requests.length,2);
 assert.equal(requests[1].url,imageUrl);
 assert.equal(requests[1].opt.redirect,'error');
 assert.equal(new Headers(requests[1].opt.headers).has('authorization'),false);
 assert.doesNotMatch(await readFile(join(dir,'page-baseline-01.result.json'),'utf8'),/private-test-value/);
});

test('URL mode does not fetch external, credential-bearing, or non-image-route URLs',async t=>{
 for(const url of ['https://example.invalid/pilot.png','https://user:pass@img.937ai.chat/api/images/file/pilot.png','https://img.937ai.chat/api/me/images','https://img.937ai.chat/api/images/file/%252e%252e%252fme']) {
  const dir=await setup(t); let calls=0;
  const r=await runPage({...spec,responseFormat:'url'},config,secrets,dir,async()=>{
   calls++; return Response.json({data:[{url}]});
  });
  assert.equal(calls,1);
  assert.equal(r.status,'invalid-or-unsaved-result');
 }
});

test('Sunburst requests send the catalog model ID and its matching configuration key',async t=>{
 const dir=await setup(t), bytes=await png(), seen=[];
 const result=await runPage(spec,config,secrets,dir,async(url,opt)=>{const body=JSON.parse(opt.body);seen.push(body);return fileResponse(bytes);});
 assert.equal(result.status,'verified-16-by-9');
 assert.deepEqual(seen.map(x=>[x.model,x.model_config_key]),[['gpt-image-2.6-sunburst','gpt-image-2.6-sunburst']]);
});
test('key-default omits the response format and verifies either delivery without resubmitting',async t=>{
 for(const format of ['b64_json','url']) {
  const dir=await setup(t), bytes=await png(), calls=[];
  const url='https://img.937ai.chat/api/images/file/default.png?sig=private-default';
  const receipt=await runPage({...spec,responseFormat:'key-default',quality:'medium'},config,secrets,dir,async (target,opt)=>{
   calls.push([target,opt.method]);
   if(calls.length===1) {
    const fields=JSON.parse(opt.body);
    assert.equal(Object.hasOwn(fields,'response_format'),false);
    assert.equal(fields.quality,'medium');
    return format==='url'?Response.json({data:[{url}]}):fileResponse(bytes);
   }
   assert.equal(new Headers(opt.headers).has('authorization'),false);
   return new Response(bytes);
  });
  assert.equal(receipt.status,'verified-16-by-9');
  assert.equal(receipt.responseFormat,'key-default');
  assert.equal(receipt.deliveredFormat,format);
  assert.equal(receipt.quality,'medium');
  assert.equal(receipt.generationTimeoutMs,600_000);
  assert.equal(receipt.output.sha256,sha(bytes));
  assert.deepEqual(calls,[['https://img.937ai.chat/v1/images/generations','POST'],...(format==='url'?[[url,'GET']]:[])]);
  assert.doesNotMatch(await readFile(join(dir,'page-baseline-01.result.json'),'utf8'),/private-default/);
 }
});

test('key-default preserves URL validation and does not request an external image',async t=>{
 const dir=await setup(t); let calls=0;
 const receipt=await runPage({...spec,responseFormat:'key-default'},config,secrets,dir,async()=>{
  calls++; return Response.json({data:[{url:'https://example.invalid/result.png'}]});
 });
 assert.equal(calls,1);
 assert.equal(receipt.status,'invalid-or-unsaved-result');
});
