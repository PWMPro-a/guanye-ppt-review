import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {lstat, readFile, writeFile} from 'node:fs/promises';
import {basename, dirname, posix, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const signature = '89504e470d0a1a0a';
const MAX_PAGE_BYTES = 20 * 1024 ** 2;
const MAX_DECK_BYTES = 128 * 1024 ** 2;
const wide = {width:13 + 1/3, height:7.5};
const require = createRequire(import.meta.url);
const arr = value => value === undefined ? [] : Array.isArray(value) ? value : [value];

async function boundedFile(file, limit) {
 const stat = await lstat(file);
 assert(stat.isFile() && stat.size > 0 && stat.size <= limit, `Invalid or oversized file: ${basename(file)}`);
 const bytes = await readFile(file);
 assert(bytes.length === stat.size && bytes.length <= limit, `File changed: ${basename(file)}`);
 return bytes;
}

const PILOT='dc13c526-4c6a-52c9-8cf7-3dbe0c4c30b7';
const digest=value=>typeof value==='string' && /^[a-f0-9]{64}$/.test(value);
const recordLimit=64*1024;

export async function collectReceipts(plan, directory, arm, scope = 'deck') {
 assert(['baseline','reference','confirmed-feedback'].includes(arm),'Select one receipt arm');
 assert(['cover','page','deck'].includes(scope),'Unexpected receipt scope');
 assert.equal(plan.schema,scope==='deck'?'ppt-training-direct-deck-plan-v1':'ppt-training-direct-single-plan-v1','Unexpected receipt plan schema');
 assert(scope==='deck'?(plan.scope===undefined || plan.scope===scope):plan.scope===scope,'Receipt plan scope mismatch');
 const {pageCount,model,size,quality,responseFormat,outlineSha256}=plan;
 assert((scope==='deck'?Number.isInteger(pageCount) && pageCount>=20 && pageCount<=30:pageCount===1) && digest(outlineSha256),'Invalid deck plan');
 assert(['gpt-image-2.6-sunburst','gpt-image-2.5-sunburst'].includes(model) && size==='1536x864' && ['low','medium','high'].includes(quality) && ['key-default','url','b64_json'].includes(responseFormat),'Invalid deck settings');
 assert(Array.isArray(plan.pages) && plan.pages.length===pageCount,'Expected all planned receipt positions');
 const pages=[], receipts=[], requestIds=new Set();
 const readJson=async name=>{
  const bytes=await boundedFile(resolve(directory,name),recordLimit);
  return {value:JSON.parse(bytes.toString('utf8')),sha256:sha(bytes)};
 };
 async function load(position, expected, candidateArm) {
  const stem=`${scope}-${candidateArm}-${String(position).padStart(2,'0')}`;
  const dispatch=await readJson(`${stem}.dispatch.json`);
  const result=await readJson(`${stem}.result.json`);
  const {status,httpStatus,deliveredFormat,output,...fields}=result.value;
  const {status:dispatchStatus,...dispatchedFields}=dispatch.value;
  assert(status==='verified-16-by-9' && dispatchStatus==='dispatch-recorded' && Number.isInteger(httpStatus) && httpStatus>=200 && httpStatus<300,`Missing successful ${candidateArm} result at page ${position}`);
  assert.deepEqual(fields,dispatchedFields,`Dispatch and result disagree at page ${position}`);
  assert(fields.schema==='ppt-training-direct-page-v1' && fields.environment==='local-direct-pilot-not-gateway' && fields.workspaceId===PILOT && fields.scope===scope && fields.arm===candidateArm && fields.position===position && fields.pageCount===pageCount,`Wrong trial identity at page ${position}`);
  assert(fields.model===model && fields.size===size && fields.quality===quality && fields.responseFormat===responseFormat && fields.basePromptSha256===expected.basePromptSha256 && digest(fields.promptSha256),`Wrong planned prompt or settings at page ${position}`);
  assert(['url','b64_json'].includes(deliveredFormat) && (responseFormat==='key-default' || responseFormat===deliveredFormat),`Unexpected response format at page ${position}`);
  assert(typeof fields.requestId==='string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(fields.requestId) && !requestIds.has(fields.requestId),`Missing or duplicate request ID at page ${position}`);
  requestIds.add(fields.requestId);
  assert(output?.position===position && output.file===`${stem}.png` && digest(output.sha256),`Wrong result file at page ${position}`);
  if(candidateArm==='baseline') assert(fields.reference===undefined && fields.feedback===null,`Baseline contains reference or feedback at page ${position}`);
  else {
   const ref=fields.reference;
   assert(expected.reference && ref && typeof ref.pageId==='string' && ref.pageId && ['deckId','position','sha256'].every(key=>expected.reference[key]===ref[key]) && digest(ref.sha256) && (expected.reference.pageId===undefined || expected.reference.pageId===ref.pageId),`Wrong library reference at page ${position}`);
   if(candidateArm==='reference') assert(fields.feedback===null,`Unexpected feedback at page ${position}`);
  }
  return {fields,output,dispatchSha256:dispatch.sha256,resultSha256:result.sha256};
 }
 for(const [i,expected] of plan.pages.entries()) {
  const position=i+1;
  assert(expected?.position===position && digest(expected.basePromptSha256),`Invalid planned position or prompt at page ${position}`);
  const result=await load(position,expected,arm);
  let parentResultSha256, feedbackConfirmationId;
  if(arm==='confirmed-feedback') {
   const parent=await load(position,expected,'reference');
   const feedback=result.fields.feedback;
   const parentPng=await boundedFile(resolve(directory,parent.output.file),MAX_PAGE_BYTES);
   assert(parentPng.length===parent.output.bytes && sha(parentPng)===parent.output.sha256,`Changed reference-arm output at page ${position}`);
   assert(typeof feedback?.confirmationId==='string' && /^[A-Za-z0-9_-]{1,100}$/.test(feedback.confirmationId) && feedback.authority==='caller-declared' && digest(feedback.instructionsSha256) && feedback.parentSha256===parent.output.sha256 && feedback.parentInputSha256===parent.output.sha256 && result.fields.reference.pageId===parent.fields.reference.pageId && result.output.sha256!==parent.output.sha256,`Unbound or unchanged feedback result at page ${position}`);
   parentResultSha256=parent.resultSha256;
   feedbackConfirmationId=feedback.confirmationId;
  }
  pages.push(result.output);
  receipts.push({position,requestId:result.fields.requestId,dispatchSha256:result.dispatchSha256,resultSha256:result.resultSha256,...(parentResultSha256?{parentResultSha256,feedbackConfirmationId}:{})});
 }
 return {pages,pageCount,receiptEvidence:{arm,model,size,quality,responseFormat,outlineSha256,receipts}};
}

export async function pack(manifestPath, {fixture=false,receiptArm}={}) {
 assert(!(fixture && receiptArm),'Fixture mode cannot package receipts');
 const sharp = require('sharp');
 const PptxGenJS = require('pptxgenjs');
 const JSZip = require('jszip');
 const {xml2js} = require('xml-js');
 const manifestBytes = await boundedFile(manifestPath, 256*1024);
 const manifest = JSON.parse(manifestBytes.toString('utf8'));
 if(!receiptArm) assert.equal(manifest.schema, fixture ? 'ppt-training-local-deck-render-v1' : 'ppt-training-generated-pages-v1', 'Unexpected input schema for this mode');
 const {pages, pageCount, receiptEvidence} = receiptArm ? await collectReceipts(manifest,dirname(manifestPath),receiptArm) : manifest;
 assert(Number.isInteger(pageCount) && pageCount >= 20 && pageCount <= 30 && Array.isArray(pages) && pages.length === pageCount, 'Expected all 20-30 planned pages');
 const deck = new PptxGenJS();
 deck.defineLayout({name:'PILOT_WIDE',...wide});
 deck.layout = 'PILOT_WIDE';
 deck.author = 'Guanye';
 deck.title = 'PPT training review';
 const evidence = [], imageHashes = new Set(), names = new Set();
 let total = 0;
 for (const [index,page] of pages.entries()) {
  const position=index+1;
  assert(page?.position === position, `Missing or out-of-order page ${position}`);
  assert(typeof page.file === 'string' && /^[A-Za-z0-9_-]+\.png$/i.test(page.file) && !names.has(page.file.toLowerCase()), `Invalid or duplicate filename at page ${position}`);
  names.add(page.file.toLowerCase());
  assert(Number.isInteger(page.bytes) && page.bytes > 0 && page.bytes <= MAX_PAGE_BYTES && /^[a-f0-9]{64}$/.test(page.sha256), `Missing page identity at ${position}`);
  const raw = await boundedFile(resolve(dirname(manifestPath),page.file), Math.min(MAX_PAGE_BYTES,MAX_DECK_BYTES-total));
  total += raw.length;
  assert(raw.length === page.bytes && sha(raw) === page.sha256 && raw.subarray(0,8).toString('hex') === signature, `PNG identity mismatch at page ${position}`);
  assert(!imageHashes.has(page.sha256), `Duplicate page bytes at ${position}`);
  imageHashes.add(page.sha256);
  const image = sharp(raw,{failOn:'error',limitInputPixels:4096**2});
  const meta = await image.metadata();
  assert(meta.format === 'png' && (meta.pages ?? 1) === 1 && meta.width > 0 && meta.height > 0 && meta.width*9 === meta.height*16, `Page ${position} is not a single 16:9 PNG; no implicit crop or stretch`);
  await image.raw().toBuffer();
  deck.addSlide().addImage({data:`data:image/png;base64,${raw.toString('base64')}`,x:0,y:0,w:wide.width,h:wide.height});
  evidence.push({position,width:meta.width,height:meta.height,bytes:raw.length,sha256:page.sha256});
 }
 const bytes = Buffer.from(await deck.write({outputType:'nodebuffer',compression:true}));
 const zip = await JSZip.loadAsync(bytes);
 async function xml(file) {
  const entry=zip.file(file);
  assert(entry,`Missing PowerPoint part: ${file}`);
  return xml2js(await entry.async('string'),{compact:true});
 }
 async function related(part, id, type) {
  const rels = await xml(posix.join(posix.dirname(part),'_rels',posix.basename(part)+'.rels'));
  const matches = arr(rels.Relationships?.Relationship).map(r=>r._attributes).filter(r=>r?.Id===id);
  assert(matches.length===1 && matches[0].Type?.endsWith(`/${type}`) && !matches[0].TargetMode,`Invalid ${type} relation in ${part}`);
  const target = posix.normalize(posix.join(posix.dirname(part),matches[0].Target));
  assert(target.startsWith('ppt/') && zip.file(target),`Missing internal ${type} target`);
  return target;
 }
 const part='ppt/presentation.xml';
 const root=(await xml(part))['p:presentation'];
 const dims=root?.['p:sldSz']?._attributes;
 assert(Number(dims?.cx)>0 && Number(dims.cx)*9 === Number(dims.cy)*16,'Exported slide size is not 16:9');
 const order=arr(root?.['p:sldIdLst']?.['p:sldId']);
 assert.equal(order.length,evidence.length,'Exported slide count mismatch');
 for (const [i, element] of order.entries()) {
  const slidePart=await related(part,element._attributes?.['r:id'],'slide');
  const tree=(await xml(slidePart))['p:sld']?.['p:cSld']?.['p:spTree'];
  const pics=arr(tree?.['p:pic']);
  assert.equal(pics.length,1,`Exported slide ${i+1} needs exactly one image`);
  const imageId=pics[0]['p:blipFill']?.['a:blip']?._attributes?.['r:embed'];
  const media=await related(slidePart,imageId,'image');
  assert.equal(sha(await zip.file(media).async('nodebuffer')),evidence[i].sha256,`Exported image mismatch at slide ${i+1}`);
 }
 return {bytes, report:{schema:'ppt-training-deck-package-v1',sourceMode:fixture?'existing-material-fixture':receiptArm?'locally-verified-generation-receipts':'declared-generated-pages',...(receiptArm?{receiptEvidence}:{}),manifestSha256:sha(manifestBytes),artifactSha256:sha(bytes),pageCount,aspectRatio:'16:9',pageEvidence:evidence,limitations:['Raster slides; text is not editable.', 'Verifies package structure and embedded images, not model provenance or design scores.']}};
}

if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
 try {
  const [manifest, output, mode, ...extra] = process.argv.slice(2);
  assert(manifest && output && /\.pptx$/i.test(output) && (mode===undefined || mode==='--fixture' || /^--receipts=(baseline|reference|confirmed-feedback)$/.test(mode)) && !extra.length,'Usage: node package_ppt_training_deck.mjs manifest.json new.pptx [--fixture|--receipts=ARM]');
  const {bytes,report} = await pack(resolve(manifest),{fixture:mode==='--fixture',receiptArm:mode?.startsWith('--receipts=')?mode.slice('--receipts='.length):undefined});
  await writeFile(output,bytes,{flag:'wx'});
  await writeFile(`${output}.evidence.json`,JSON.stringify(report,null,2)+'\n',{flag:'wx'});
  console.log(JSON.stringify({pageCount:report.pageCount,sourceMode:report.sourceMode,artifactSha256:report.artifactSha256}));
 } catch (e) {
  console.error(e.message);
  process.exitCode=1;
 }
}
