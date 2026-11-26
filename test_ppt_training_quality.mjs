import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {summarize} from './cloud/summarize_ppt_training_quality.mjs';

const plans = ['baseline','reference','confirmed-feedback'];
function trial(scope, topic, family, scores=[3,4.5,4.75]) {
 const pageCount=scope==='deck'?22:1;
 return { scope, topic, holdoutFamily:family, pageCount, blind:true, model:'fixture', settingsId:'fixed-settings',
  candidates: plans.map((arm,i)=>({arm, blindId:`${topic}-blind-${(i+1)%3}`, artifactSha256:createHash('sha256').update(`${scope}:${topic}:${i}`).digest('hex'), pages:pageCount,
   scores:{visual:scores[i],layout:scores[i],match:5},...(i===2?{feedbackConfirmationId:`${topic}-confirmed`}:{})})) };
}
const evaluate = (trials, trainingFamilies=[]) => summarize({schema:'ppt-training-blind-scores-v1',trials,trainingFamilies});
test('paired results report gains independently for cover, single page, and whole deck',()=>{
 const trials=['cover','page','deck'].flatMap(scope=>Array.from({length:5},(_,i)=>trial(scope,`${scope}-${i}`,`family-${i}`)));
 const report=evaluate(trials);
 assert.equal(report.allScopesMeetPilotThreshold,true);
 assert.equal(report.scopes.deck.independentFamilies,5);
 assert.equal(report.scopes.deck.reference.meanGain.visual,1.5);
 assert.equal(report.scopes.deck.feedback.meanGain.visual,0.25);
});
test('one high-scoring draft followed by worse feedback is not a learning pass',()=>{
 const r=evaluate([trial('cover','historical','one',[4,4.5,4])]);
 assert.equal(r.allScopesMeetPilotThreshold,false);
 assert.equal(r.scopes.cover.feedback.meanGain.visual,-0.5);
 assert.equal(r.scopes.cover.feedback.regressions,1);
 assert.equal(r.scopes.cover.reference.highScoring,1);
 assert.equal(r.scopes.deck.trials,0);
});
test('duplicate topics, reused training families and incomplete or unblinded arms fail closed',()=>{
 const t=trial('deck','a','source');
 assert.throws(()=>evaluate([t],['source']),/holdout/i);
 assert.throws(()=>evaluate([t,t]),/duplicate/i);
 assert.throws(()=>evaluate([{...t,blind:false}]),/blind/i);
 assert.throws(()=>evaluate([{...t,candidates:t.candidates.slice(0,2)}]),/arm/i);
 const incomplete=structuredClone(t); incomplete.candidates[1].pages=20;
 assert.throws(()=>evaluate([incomplete]),/page/i);
 const unconfirmed=structuredClone(t); delete unconfirmed.candidates[2].feedbackConfirmationId;
 assert.throws(()=>evaluate([unconfirmed]),/confirmation/i);
});
test('many prompts from one family do not meet the independent-family threshold',()=>{
 const r=evaluate(Array.from({length:6},(_,i)=>trial('page',`repeat-${i}`,'same')));
 assert.equal(r.scopes.page.independentFamilies,1);
 assert.equal(r.scopes.page.meetsPilotThreshold,false);
});
test('reused bytes cannot count as a distinct candidate or topic',()=>{
 const first=trial('deck','first','one'), second=trial('deck','second','two');
 second.candidates[0].artifactSha256=first.candidates[1].artifactSha256;
 assert.throws(()=>evaluate([first,second]),/reused artifact/i);
 first.candidates[0].artifactSha256=first.candidates[1].artifactSha256;
 assert.throws(()=>evaluate([first]),/reused artifact/i);
});


test('failed or unresolved runs stay in the high-score denominator without invented scores',()=>{
 const trials=Array.from({length:5},(_,i)=>trial('deck',`planned-${i}`,`family-${i}`));
 trials[4].candidates[2]={arm:'confirmed-feedback',blindId:'pending-feedback',status:'not-confirmed',failureReason:'Provider response not observed'};
 trials[4].candidates[0]={arm:'baseline',blindId:'failed-baseline',status:'failed',failureReason:'Only 21 of 22 pages rendered'};
 const r=evaluate(trials).scopes.deck;
 assert.equal(r.trials,5);
 assert.equal(r.baseline.failed,1);
 assert.equal(r.feedback.completed,4);
 assert.equal(r.feedback.notConfirmed,1);
 assert.equal(r.feedback.highScoreShare,0.8);
 assert.equal(r.reference.missingPairs,1);
 assert.equal(r.feedback.comparableTrials,4);
 assert.equal(r.feedback.meanGain.visual,0.25);
 assert.equal(r.meetsPilotThreshold,false);
});
test('an incomplete arm cannot have an invented score or claim a verified artifact',()=>{
 const t=trial('page','missing','family');
 Object.assign(t.candidates[1],{status:'failed',failureReason:'No usable preview'});
 assert.throws(()=>evaluate([t]),/must not have scores/);
 delete t.candidates[1].scores;
 assert.throws(()=>evaluate([t]),/verified artifact/);
 delete t.candidates[1].artifactSha256;
 delete t.candidates[1].pages;
 delete t.candidates[1].failureReason;
 assert.throws(()=>evaluate([t]),/requires a reason/);
});


test('preregistration prevents favorable-only subsampling and outline or family substitution',()=>{
 const t=trial('deck','library','prospective-library');
 const other=trial('page','repair','prospective-repair');
 const outline='A library program with explicit service boundaries.';
 const outlineSha256=createHash('sha256').update(outline).digest('hex');
 const protocol={schema:'ppt-training-prospective-protocol-v1',id:'pilot-1',cases:[t,other].map(x=>({
  scope:x.scope,topic:x.topic,holdoutFamily:x.holdoutFamily,pageCount:x.pageCount,outline,
 }))};
 const results={schema:'ppt-training-blind-scores-v1',trainingFamilies:[],trials:[t,other].map(x=>({...x,outlineSha256}))};
 assert.deepEqual(summarize(results,protocol).protocolAudit,{id:'pilot-1',cases:2});
 assert.throws(()=>summarize({...results,trials:results.trials.slice(0,1)},protocol),/every planned case/);
 for(const change of [{pageCount:21},{holdoutFamily:'different'},{outlineSha256:'0'.repeat(64)}]) {
  const changed=structuredClone(results);
  if(change.pageCount) for(const candidate of changed.trials[0].candidates) candidate.pages=change.pageCount;
  Object.assign(changed.trials[0],change);
  assert.throws(()=>summarize(changed,protocol),/does not match preregistration/);
 }
});
test('planned cases without any model dispatch are explicitly not run',()=>{
 const t=trial('cover','not-run','prospective');
 t.blind=null;
 t.candidates=t.candidates.map(({arm,blindId})=>({arm,blindId,status:'not-run',failureReason:'Model dispatch unavailable'}));
 const r=evaluate([t]).scopes.cover;
 assert.equal(r.baseline.notRun,1);
 assert.equal(r.baseline.failed,0);
 assert.equal(r.feedback.highScoreShare,0);
 assert.equal(r.reference.meanGain,null);
 assert.equal(r.meetsPilotThreshold,false);
});
