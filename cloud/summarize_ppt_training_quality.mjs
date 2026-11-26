import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const scopes = ['cover', 'page', 'deck'];
const arms = ['baseline', 'reference', 'confirmed-feedback'];
const dimensions = ['visual', 'layout', 'match'];
const thresholds = {minIndependentFamilies:5, highScore:4, minFinalHighScoreShare:0.8, minMeanDesignGain:0.1};
const average = values => values.reduce((a,b)=>a+b,0)/values.length;
const display = value => Number(value.toFixed(4));
const nonempty = value => typeof value==='string' && value.length>0 && value===value.trim();
const design = scores => (scores.visual+scores.layout)/2;
const completed = candidate => candidate.status === undefined || candidate.status === 'verified';
const high = candidate => completed(candidate) && dimensions.every(dim=>candidate.scores[dim]>=thresholds.highScore);

function compare(families, from, to) {
 const comparable = families.map(trials=>trials.filter(t=>completed(t.byArm[from]) && completed(t.byArm[to]))).filter(ts=>ts.length);
 const rows = comparable.flat().map(t=>({before:t.byArm[from].scores, after:t.byArm[to].scores}));
 const missingPairs = families.flat().length - rows.length;
 const gains = comparable.map(trials=>Object.fromEntries(dimensions.map(dim=>[dim, average(trials.map(t=>t.byArm[to].scores[dim]-t.byArm[from].scores[dim]))])));
 const rawMean = gains.length ? Object.fromEntries(dimensions.map(dim=>[dim,average(gains.map(g=>g[dim]))])) : null;
 const improvedFamilies = gains.filter(g=>design(g)>1e-9).length;
 const regressions = rows.filter(({before,after})=>design(after)-design(before)<-1e-9 || after.match<before.match).length;
 const matchRegressions = rows.filter(({before,after})=>after.match<before.match).length;
 return {
  meanGain: rawMean && Object.fromEntries(dimensions.map(dim=>[dim,display(rawMean[dim])])),
  comparableTrials:rows.length, comparableFamilies:comparable.length, missingPairs,
  improvedFamilies, regressions, matchRegressions,
  meetsGainThreshold: missingPairs===0 && rawMean!==null && design(rawMean)+1e-9>=thresholds.minMeanDesignGain && improvedFamilies>gains.length/2 && matchRegressions===0,
 };
}

const trialKey = ({scope,topic}) => JSON.stringify([scope,topic]);
const validSize = ({scope,pageCount}) => Number.isInteger(pageCount)
 && (scope==='deck'?pageCount>=20 && pageCount<=30:pageCount===1);
const textDigest = text => createHash('sha256').update(text, 'utf8').digest('hex');

function auditProtocol(protocol, trials) {
 assert(protocol?.schema==='ppt-training-prospective-protocol-v1' && Array.isArray(protocol.cases), 'Invalid prospective protocol');
 assert(nonempty(protocol.id) && protocol.cases.length>0, 'Protocol requires an ID and cases');
 const registered = new Map();
 for (const c of protocol.cases) {
  assert(c && scopes.includes(c.scope) && nonempty(c.topic) && nonempty(c.holdoutFamily)
   && validSize(c) && nonempty(c.outline), 'Invalid registered case');
  assert(!registered.has(trialKey(c)), 'Duplicate registered case');
  registered.set(trialKey(c),c);
 }
 const measured = new Map(trials.map(t=>[trialKey(t),t]));
 assert(measured.size===trials.length && measured.size===registered.size, 'Register every planned case, including incomplete arms');
 for (const [key,c] of registered) {
  const t=measured.get(key);
  assert(t && t.holdoutFamily===c.holdoutFamily && t.pageCount===c.pageCount && t.outlineSha256===textDigest(c.outline),
   `Measured case does not match preregistration: ${c.scope} / ${c.topic}`);
 }
 return {id:protocol.id,cases:registered.size};
}

export function summarize(input, protocol) {
 assert(input?.schema==='ppt-training-blind-scores-v1' && Array.isArray(input.trials), 'Invalid blind score input schema');
 assert(Array.isArray(input.trainingFamilies) && input.trainingFamilies.every(nonempty), 'List the training source families');
 const training=new Set(input.trainingFamilies), topics=new Set(), blindIds=new Set(), artifactHashes=new Set();
 const groups = Object.fromEntries(scopes.map(scope=>[scope,new Map()]));
 for (const t of input.trials) {
  assert(t && scopes.includes(t.scope) && nonempty(t.topic) && nonempty(t.holdoutFamily),'Invalid trial scope, topic, or holdout family');
  const key=trialKey(t);
  assert(!topics.has(key),'Duplicate topic within scope'); topics.add(key);
  assert(!training.has(t.holdoutFamily),'Holdout family is also present in training');
  assert(nonempty(t.model) && nonempty(t.settingsId),'Specify fixed model and settings for the paired trial');
  assert(validSize(t), 'Invalid trial page count');
  assert(Array.isArray(t.candidates) && t.candidates.length===arms.length,'Each trial needs all three arms');
  assert(t.blind===true || (t.blind===null && t.candidates.every(c=>c && !completed(c))), 'Scored trial not marked blind');
  const byArm=Object.create(null);
  for (const c of t.candidates) {
   assert(c && arms.includes(c.arm) && !byArm[c.arm],'Invalid or duplicate arm');
   assert(nonempty(c.blindId) && !blindIds.has(c.blindId),'Missing or reused blind ID'); blindIds.add(c.blindId);
   if(['failed','not-confirmed','not-run'].includes(c.status)) {
    assert(nonempty(c.failureReason) && c.scores===undefined, 'Incomplete arm requires a reason and must not have scores');
    assert(c.artifactSha256===undefined && c.pages===undefined, 'Incomplete arm cannot claim a verified artifact');
    byArm[c.arm]=c;
    continue;
   }
   assert(completed(c) && c.failureReason===undefined, 'Invalid completed-arm status');
   assert(typeof c.artifactSha256==='string' && /^[0-9a-f]{64}$/.test(c.artifactSha256),'Missing artifact hash');
   assert(!artifactHashes.has(c.artifactSha256),'Reused artifact hash'); artifactHashes.add(c.artifactSha256);
   assert(c.pages===t.pageCount,'Candidate has an incomplete or mismatched page count');
   assert(dimensions.every(dim=>Number.isFinite(c.scores?.[dim]) && c.scores[dim]>=0 && c.scores[dim]<=5),'Require all three scores on the 0–5 scale');
   if(c.arm==='confirmed-feedback') assert(nonempty(c.feedbackConfirmationId),'Feedback confirmation is missing');
   byArm[c.arm]=c;
  }
  const existing=groups[t.scope].get(t.holdoutFamily)??[];
  existing.push({byArm,model:t.model,settingsId:t.settingsId});
  groups[t.scope].set(t.holdoutFamily,existing);
 }
 const protocolAudit=protocol===undefined?null:auditProtocol(protocol,input.trials);
 const result={};
 for (const scope of scopes) {
  const families=[...groups[scope].values()];
  const trials=families.flat();
  const fixedRunProfile=new Set(trials.map(t=>JSON.stringify([t.model,t.settingsId]))).size===1;
  const byArm=Object.fromEntries(arms.map(arm=>[arm,{
   completed:trials.filter(t=>completed(t.byArm[arm])).length,
   failed:trials.filter(t=>t.byArm[arm].status==='failed').length,
   notConfirmed:trials.filter(t=>t.byArm[arm].status==='not-confirmed').length,
   notRun:trials.filter(t=>t.byArm[arm].status==='not-run').length,
   highScoring:trials.filter(t=>high(t.byArm[arm])).length,
   highScoreShare:families.length ? display(average(families.map(ts=>average(ts.map(t=>Number(high(t.byArm[arm]))))))) : null,
  }]));
  const reference={...compare(families,'baseline','reference'),...byArm.reference};
  const feedback={...compare(families,'reference','confirmed-feedback'),...byArm['confirmed-feedback']};
  result[scope]={trials:trials.length, independentFamilies:families.length, fixedRunProfile,
   baseline:byArm.baseline, reference, feedback,
   meetsPilotThreshold:fixedRunProfile && families.length>=thresholds.minIndependentFamilies && reference.meetsGainThreshold && feedback.meetsGainThreshold
    && average(families.map(ts=>average(ts.map(t=>Number(high(t.byArm['confirmed-feedback']))))))>=thresholds.minFinalHighScoreShare,
  };
 }
 return {schema:'ppt-training-blind-summary-v1',thresholds:{...thresholds},protocolAudit,scopes:result,
  allScopesMeetPilotThreshold:scopes.every(scope=>result[scope].meetsPilotThreshold),
  limitations:['Input scores, blind review, artifact hashes, model settings, feedback confirmation and source-family membership are caller attestations; this program does not audit underlying artifacts or ratings.', 'Gains are descriptive for verified pairs only; missing pairs prevent a pass and all registered trials count toward the high-score denominator. Not proof of statistical significance or model fine-tuning.']};
}

if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
 const [inputPath,outputPath,protocolPath,...extra]=process.argv.slice(2);
 assert(inputPath && outputPath && !extra.length,'Usage: node summarize_ppt_training_quality.mjs <blind-scores.json> <new-summary.json> [protocol.json]');
 const protocolBytes=protocolPath?await readFile(protocolPath):null;
 const report=summarize(JSON.parse(await readFile(inputPath,'utf8')),protocolBytes?JSON.parse(protocolBytes.toString('utf8')):undefined);
 if(protocolBytes) report.protocolAudit.sha256=createHash('sha256').update(protocolBytes).digest('hex');
 await writeFile(outputPath,JSON.stringify(report,null,2)+'\n',{encoding:'utf8',flag:'wx'});
 console.log(JSON.stringify({allScopesMeetPilotThreshold:report.allScopesMeetPilotThreshold}));
}