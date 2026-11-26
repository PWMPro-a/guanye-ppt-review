import assert from 'node:assert/strict';
import {createHash,randomBytes} from 'node:crypto';
import {createRequire} from 'node:module';
import {lstat,mkdir,readFile,writeFile} from 'node:fs/promises';
import {dirname,join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {collectReceipts,pack} from './package_ppt_training_deck.mjs';

const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const require=createRequire(import.meta.url);
const arms=['baseline','reference','confirmed-feedback'];
const pngLimit=20*1024**2;
const planLimit=256*1024;
const json=value=>JSON.stringify(value,null,2)+'\n';

async function regular(file,limit) {
 const info=await lstat(file);
 assert(info.isFile() && info.size>0 && info.size<=limit,'Expected a bounded regular file');
 const data=await readFile(file);
 assert(data.length===info.size,'File size changed while reading');
 return data;
}

export async function publishBlindPacket(planPath,out,{brief}={}) {
 assert(typeof brief==='string' && brief.trim() && brief.length<=30000,'A shared reviewer brief is required');
 const planBytes=await regular(planPath,planLimit);
 const plan=JSON.parse(planBytes), planSha256=sha(planBytes);
 assert(['ppt-training-direct-deck-plan-v1','ppt-training-direct-single-plan-v1'].includes(plan?.schema),'Unsupported blind packet plan');
 assert.equal(sha(brief),plan.outlineSha256,'Reviewer brief does not match the registered outline');
 const scope=plan.schema==='ppt-training-direct-deck-plan-v1'?'deck':plan.scope;
 assert(scope==='deck'?plan.schema==='ppt-training-direct-deck-plan-v1':scope==='cover'||scope==='page','Unsupported blind scope');
 const candidates=[], requestIds=new Set(), pageHashes=new Set(), blindIds=new Set();
 const referencePages=new Map();
 for(const arm of arms) {
  let bytes, artifactSha256, receiptEvidence, pages;
  if(scope==='deck') {
   const packaged=await pack(planPath,{receiptArm:arm});
   ({bytes}=packaged);
   assert.equal(packaged.report.manifestSha256,planSha256,'Plan changed while packaging');
   ({artifactSha256,receiptEvidence}=packaged.report);
   pages=packaged.report.pageEvidence;
  } else {
   ({pages,receiptEvidence}=await collectReceipts(plan,dirname(planPath),arm,scope));
   const [page]=pages;
   const raw=await regular(join(dirname(planPath),page.file),pngLimit);
   assert(raw.length===page.bytes && sha(raw)===page.sha256 && raw.subarray(0,8).toString('hex')==='89504e470d0a1a0a','PNG identity mismatch');
   artifactSha256=page.sha256;
   const image=require('sharp')(raw,{failOn:'error',limitInputPixels:4096**2});
   const meta=await image.metadata();
   assert(meta.format==='png' && (meta.pages??1)===1 && meta.width>0 && meta.height>0 && meta.width*9===meta.height*16 && meta.width===page.width && meta.height===page.height,'Invalid PNG dimensions');
   bytes=await image.png().toBuffer(); // strips input metadata; no resize, crop, or visible labels
  }
  for(const page of pages) {
   assert(!pageHashes.has(page.sha256),'Candidate reuses page bytes');
   pageHashes.add(page.sha256);
  }
  for(const row of receiptEvidence.receipts) {
   assert(!requestIds.has(row.requestId),'Candidate reuses a generation request');
   requestIds.add(row.requestId);
   if(arm==='reference') referencePages.set(row.position,row.resultSha256);
   if(arm==='confirmed-feedback') assert.equal(row.parentResultSha256,referencePages.get(row.position),'Feedback parent changed during packet preparation');
  }
  let blindId;
  do {blindId=`P-${randomBytes(6).toString('hex')}`;} while(blindIds.has(blindId));
  blindIds.add(blindId);
  const file=`${blindId}.${scope==='deck'?'pptx':'png'}`;
  candidates.push({arm,blindId,file,bytes,artifactSha256,reviewerSha256:sha(bytes),receipts:receiptEvidence.receipts,
   feedbackConfirmationIds:[...new Set(receiptEvidence.receipts.map(r=>r.feedbackConfirmationId).filter(Boolean))]});
 }
 candidates.sort((a,b)=>a.blindId.localeCompare(b.blindId,'en'));
 const review={schema:'ppt-training-blind-review-packet-v1',scope,pageCount:plan.pageCount,brief,
  scoreScale:{min:0,max:5,dimensions:['visual','layout','match']},
  instructions:'只按共同题目独立评分，核对内容、错漏、可读性与排版；整套还须检查页序和风格一致性。提交并锁定评分后再揭盲。',
  candidates:candidates.map(({blindId,file})=>({blindId,file}))};
 const mapping={schema:'ppt-training-blind-packet-key-v1',scope,pageCount:plan.pageCount,planSha256,outlineSha256:plan.outlineSha256,
  model:plan.model,size:plan.size,quality:plan.quality,responseFormat:plan.responseFormat,reviewSha256:sha(json(review)),candidates:candidates.map(({bytes,...record})=>record),
  limitations:['Local receipts are not provider-signed. Human confirmation and scores are not established by this packet.']};
 await mkdir(out); // validate all candidates before creating any output; never reuse a packet path
 const reviewer=join(out,'reviewer'), privateDir=join(out,'private');
 await mkdir(reviewer); await mkdir(privateDir);
 for(const c of candidates) await writeFile(join(reviewer,c.file),c.bytes,{flag:'wx'});
 await writeFile(join(privateDir,'mapping.json'),json(mapping),{flag:'wx'});
 await writeFile(join(reviewer,'review.json'),json(review),{flag:'wx'}); // publish reviewer manifest last
 return {review,mapping};
}

if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
 const [planPath,briefPath,out,...extra]=process.argv.slice(2);
 assert(planPath && briefPath && out && !extra.length,'Usage: node publish_ppt_training_blind_packet.mjs <plan.json> <shared-brief.txt> <new-packet-directory>');
 const brief=new TextDecoder('utf-8',{fatal:true}).decode(await regular(briefPath,120000));
 const {review}=await publishBlindPacket(resolve(planPath),resolve(out),{brief});
 console.log(JSON.stringify({scope:review.scope,pageCount:review.pageCount,candidates:review.candidates.length,reviewerDirectory:resolve(out,'reviewer')}));
}
