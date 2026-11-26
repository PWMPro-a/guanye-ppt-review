import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash, randomUUID} from 'node:crypto';
import {createRequire} from 'node:module';
import {mkdtemp, readFile, readdir, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {publishBlindPacket} from './cloud/publish_ppt_training_blind_packet.mjs';

const sharp=createRequire(import.meta.url)('sharp');
const sha=v=>createHash('sha256').update(v).digest('hex');
const arms=['baseline','reference','confirmed-feedback'];
const brief='A public brief shared across all candidates.';

async function fixture(scope='page') {
 const root=await mkdtemp(join(tmpdir(),'guanye-blind-'));
 const planPath=join(root,'plan.json');
 const plan={schema:'ppt-training-direct-single-plan-v1',scope,pageCount:1,model:'gpt-image-2.6-sunburst',size:'1536x864',quality:'medium',responseFormat:'key-default',outlineSha256:sha(brief),pages:[{position:1,basePromptSha256:sha('shared prompt'),reference:{deckId:'library-deck',position:scope==='cover'?1:2,sha256:sha('approved input')}}]};
 await writeFile(planPath,JSON.stringify(plan));
 const records={};
 for(const [i,arm] of arms.entries()) {
  const stem=`${scope}-${arm}-01`, file=`${stem}.png`;
  const bytes=await sharp({create:{width:32,height:18,channels:3,background:{r:i+10,g:2,b:30}}}).png().toBuffer();
  const parent=records.reference?.output.sha256;
  const output={position:1,file,bytes:bytes.length,sha256:sha(bytes),width:32,height:18};
  const dispatch={schema:'ppt-training-direct-page-v1',environment:'local-direct-pilot-not-gateway',workspaceId:'dc13c526-4c6a-52c9-8cf7-3dbe0c4c30b7',scope,arm,position:1,pageCount:1,model:plan.model,size:plan.size,quality:plan.quality,responseFormat:plan.responseFormat,requestId:randomUUID(),basePromptSha256:plan.pages[0].basePromptSha256,promptSha256:sha(`outbound ${arm}`),...(i?{reference:{...plan.pages[0].reference,pageId:'verified-library-page'}}:{}),feedback:i===2?{authority:'caller-declared',confirmationId:'human-revision-1',parentSha256:parent,parentInputSha256:parent,instructionsSha256:sha('revision')}:null,status:'dispatch-recorded'};
  const result={...dispatch,status:'verified-16-by-9',httpStatus:200,deliveredFormat:'b64_json',output};
  await writeFile(join(root,file),bytes);
  await writeFile(join(root,`${stem}.dispatch.json`),JSON.stringify(dispatch));
  await writeFile(join(root,`${stem}.result.json`),JSON.stringify(result));
  records[arm]={dispatch,result,output};
 }
 return {root,planPath,plan,records};
}

for(const scope of ['cover','page']) test(`publishes blinded ${scope} copies with a separate provenance key`,async()=>{
 const {root,planPath,records}=await fixture(scope);
 const out=join(root,'packet');
 const {mapping,review}=await publishBlindPacket(planPath,out,{brief});
 assert.equal(review.candidates.length,3);
 assert.deepEqual((await readdir(join(out,'reviewer'))).sort(),['review.json',...review.candidates.map(c=>c.file)].sort());
 const publicText=await readFile(join(out,'reviewer','review.json'),'utf8');
 assert.doesNotMatch(publicText,/baseline|confirmed-feedback|human-revision-1|library-deck|sunburst/);
 assert.deepEqual(review.candidates.map(c=>c.blindId),[...review.candidates.map(c=>c.blindId)].sort());
 assert.equal(new Set(review.candidates.map(c=>c.blindId)).size,3);
 for(const c of mapping.candidates) {
  assert.equal(c.artifactSha256,records[c.arm].output.sha256);
  assert.equal(c.reviewerSha256,sha(await readFile(join(out,'reviewer',c.file))));
 }
 assert.equal(mapping.candidates.find(c=>c.arm==='confirmed-feedback').feedbackConfirmationIds[0],'human-revision-1');
 assert.deepEqual(JSON.parse(await readFile(join(out,'private','mapping.json'),'utf8')),mapping);
 await assert.rejects(publishBlindPacket(planPath,out,{brief}),{code:'EEXIST'});
});

test('does not create an output packet for changed source bytes, incomplete results, or a local render manifest',async()=>{
 const {root,planPath,plan,records}=await fixture();
 const out=join(root,'packet');
 const resultPath=join(root,'page-reference-01.result.json');
 await writeFile(resultPath,JSON.stringify({...records.reference.result,status:'request-outcome-unknown'}));
 const publish=()=>publishBlindPacket(planPath,out,{brief});
 await assert.rejects(publishBlindPacket(planPath,out,{brief:'Different brief'}),/Reviewer brief does not match/);
 await assert.rejects(publish(),/Missing successful/);
 await writeFile(resultPath,JSON.stringify(records.reference.result));
 await writeFile(join(root,'page-reference-01.png'),'not a PNG');
 await assert.rejects(publish(),/PNG identity mismatch/);
 await writeFile(planPath,JSON.stringify({...plan,schema:'ppt-training-local-deck-render-v1'}));
 await assert.rejects(publish(),/Unsupported blind packet plan/);
 assert(!((await readdir(root)).includes('packet')));
});
