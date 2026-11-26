import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {mkdtemp, readFile, readdir, rmdir, unlink, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pack} from './cloud/package_ppt_training_deck.mjs';
import {publishBlindPacket} from './cloud/publish_ppt_training_blind_packet.mjs';

const sharp=createRequire(import.meta.url)('sharp');
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');

test('verifies exact 22-slide export and rejects incomplete or substituted input',async t=>{
 const root=await mkdtemp(join(tmpdir(),'guanye-package-test-'));
 t.after(async()=>{
  for (const entry of await readdir(root)) await unlink(join(root,entry));
  await rmdir(root);
 });
 const manifestPath=join(root,'manifest.json');
 const makePng=(position,height=18)=>sharp({create:{width:32,height,channels:3,background:{r:position,g:42,b:115}}}).png().toBuffer();
 const pages=await Promise.all(Array.from({length:22},async (_,i)=>{
  const position=i+1,file=`slide-${position}.png`,bytes=await makePng(position);
  await writeFile(join(root,file),bytes,{flag:'wx'});
  return {position,file,bytes:bytes.length,sha256:sha(bytes)};
 }));
 const manifest={schema:'ppt-training-generated-pages-v1',pageCount:22,pages};
 const save=()=>writeFile(manifestPath,JSON.stringify(manifest));
 await t.test('matching ordered 16:9 pages are preserved',async()=>{
  await save();
  const {bytes,report}=await pack(manifestPath);
  assert.equal(report.sourceMode,'declared-generated-pages');
  assert.equal(report.pageCount,22);
  assert.equal(report.aspectRatio,'16:9');
  assert.equal(sha(bytes),report.artifactSha256);
  assert.deepEqual(report.pageEvidence.map(p=>p.sha256),pages.map(p=>p.sha256));
 });
 await t.test('missing planned page is rejected',async()=>{
  manifest.pages=pages.slice(0,-1); await save();
  await assert.rejects(pack(manifestPath),/all 20-30 planned pages/);
 });
 await t.test('out of order and duplicate pages are rejected',async()=>{
  manifest.pages=[pages[1],pages[0],...pages.slice(2)]; await save();
  await assert.rejects(pack(manifestPath),/out-of-order page 1/);
  const bytes=await readFile(join(root,pages[0].file));
  await writeFile(join(root,'duplicate.png'),bytes,{flag:'wx'});
  manifest.pages=[pages[0],{...pages[0],position:2,file:'duplicate.png'},...pages.slice(2)]; await save();
  await assert.rejects(pack(manifestPath),/Duplicate page bytes at 2/);
 });
 await t.test('incorrect hashes and wrong aspect ratio are rejected',async()=>{
  manifest.pages=[{...pages[0],sha256:'0'.repeat(64)},...pages.slice(1)]; await save();
  await assert.rejects(pack(manifestPath),/identity mismatch at page 1/);
  const bytes=await makePng(1,24);
  await writeFile(join(root,'wrong-size.png'),bytes,{flag:'wx'});
  manifest.pages=[{position:1,file:'wrong-size.png',bytes:bytes.length,sha256:sha(bytes)},...pages.slice(1)]; await save();
  await assert.rejects(pack(manifestPath),/not a single 16:9 PNG/);
 });
 await t.test('local-material fixture requires explicit fixture mode',async()=>{
  manifest.schema='ppt-training-local-deck-render-v1'; manifest.pages=pages; await save();
  await assert.rejects(pack(manifestPath),/Unexpected input schema/);
  assert.equal((await pack(manifestPath,{fixture:true})).report.sourceMode,'existing-material-fixture');
 });
});

test('receipt mode packages only a complete plan-bound arm with verified revision lineage',async t=>{
 const root=await mkdtemp(join(tmpdir(),'guanye-receipt-test-'));
 t.after(async()=>{
  if((await readdir(root)).includes('packet')) {
   for(const name of ['reviewer','private']) {
    const dir=join(root,'packet',name);
    for(const file of await readdir(dir)) await unlink(join(dir,file));
    await rmdir(dir);
   }
   await rmdir(join(root,'packet'));
  }
  for(const entry of await readdir(root)) await unlink(join(root,entry));
  await rmdir(root);
 });
 const path=join(root,'plan.json');
 const packet=join(root,'packet');
 const brief='An outline shared with every reviewer.';

 const settings={model:'gpt-image-2.6-sunburst',size:'1536x864',quality:'medium',responseFormat:'key-default'};
 const pageCount=22, pages=[];
 let request=0;
 async function writeReceipt(position,arm,expected,parent){
  const stem=`deck-${arm}-${String(position).padStart(2,'0')}`;
  const file=`${stem}.png`;
  const bytes=await sharp({create:{width:32,height:18,channels:3,background:{r:position,g:parent?80:arm==="baseline"?20:40,b:115}}}).png().toBuffer();
  const output={position,file,bytes:bytes.length,sha256:sha(bytes),width:32,height:18};
  const fields={schema:'ppt-training-direct-page-v1',environment:'local-direct-pilot-not-gateway',workspaceId:'dc13c526-4c6a-52c9-8cf7-3dbe0c4c30b7',scope:'deck',arm,position,pageCount,...settings,generationTimeoutMs:600000,requestId:`11111111-1111-4111-8111-${String(++request).padStart(12,'0')}`,basePromptSha256:expected.basePromptSha256,promptSha256:sha(stem),...(arm==='baseline'?{}:{reference:{...expected.reference,pageId:'test-page'}}),feedback:parent?{confirmationId:`review-${position}`,parentSha256:parent.sha256,parentInputSha256:parent.sha256,instructionsSha256:sha(`changes-${position}`),authority:'caller-declared'}:null};
  const dispatch=join(root,`${stem}.dispatch.json`), result=join(root,`${stem}.result.json`);
  await writeFile(dispatch,JSON.stringify({...fields,status:'dispatch-recorded'}));
  await writeFile(result,JSON.stringify({...fields,status:'verified-16-by-9',httpStatus:200,deliveredFormat:'b64_json',output}));
  await writeFile(join(root,file),bytes);
  return {dispatch,result,output};
 }
 let sample;
 for(let position=1;position<=pageCount;position++){
  const item={position,basePromptSha256:sha(`planned page ${position}`),reference:{deckId:'test-deck',position:2,sha256:sha('library original')}};
  pages.push(item);
  const reference=await writeReceipt(position,'reference',item);
  const revised=await writeReceipt(position,'confirmed-feedback',item,reference.output);
  if(position===1) sample={reference,revised};
 }
 const plan={schema:'ppt-training-direct-deck-plan-v1',outlineSha256:sha(brief),pageCount,...settings,pages};
 await writeFile(path,JSON.stringify(plan));
 const options={receiptArm:'confirmed-feedback'};
 const {report}=await pack(path,options);
 assert.equal(report.sourceMode,'locally-verified-generation-receipts');
 assert.equal(report.pageCount,22);
 assert.equal(report.receiptEvidence.receipts[0].parentResultSha256,sha(await readFile(sample.reference.result)));
 await assert.rejects(publishBlindPacket(path,packet,{brief}),{code:'ENOENT'});
 assert(!(await readdir(root)).includes('packet'));
 for(const item of pages) await writeReceipt(item.position,'baseline',item);
 const {review,mapping}=await publishBlindPacket(path,packet,{brief});
 assert.equal(review.pageCount,22);
 assert.equal(review.scope,'deck');
 assert.equal(review.candidates.length,3);
 assert.doesNotMatch(await readFile(join(packet,'reviewer','review.json'),'utf8'),/baseline|confirmed-feedback|test-deck|sunburst/);
 for(const c of mapping.candidates) {
  assert.equal(c.receipts.length,22);
  assert.equal(c.reviewerSha256,sha(await readFile(join(packet,'reviewer',c.file))));
 }
 assert.equal(mapping.candidates.find(c=>c.arm==='confirmed-feedback').feedbackConfirmationIds.length,22);
 const original=await readFile(sample.revised.result,'utf8');
 const modified=JSON.parse(original);
 modified.quality='high';
 await writeFile(sample.revised.result,JSON.stringify(modified));
 await assert.rejects(pack(path,options),/Dispatch and result disagree/);
 await writeFile(sample.revised.result,original);
 const originalDispatch=await readFile(sample.revised.dispatch,'utf8');
 const bad=JSON.parse(original);
 const badDispatch=JSON.parse(originalDispatch);
 bad.feedback.parentInputSha256=badDispatch.feedback.parentInputSha256=sha('wrong edit input');
 await writeFile(sample.revised.result,JSON.stringify(bad));
 await writeFile(sample.revised.dispatch,JSON.stringify(badDispatch));
 await assert.rejects(pack(path,options),/Unbound or unchanged feedback/);
 await writeFile(sample.revised.result,original);
 await writeFile(sample.revised.dispatch,originalDispatch);
 await writeFile(path,JSON.stringify({...plan,pages:[{...pages[0],basePromptSha256:sha('wrong page brief')},...pages.slice(1)]}));
 await assert.rejects(pack(path,options),/Wrong planned prompt or settings/);
});
