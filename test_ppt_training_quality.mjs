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
