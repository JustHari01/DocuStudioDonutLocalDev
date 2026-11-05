"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {imageReceiptProof,repairStalledImage}=require('../src/stalled-image-recovery.cjs');
const url='https://chatgpt.com/c/6ace2222-1234-83ec-a358-abcdef123456',hash='a'.repeat(64),draftHash='b'.repeat(64);
function setup(){
 const pending={createdAt:1000,sendAttempted:true,submissionAccepted:true,outputMode:'generated_image',
   userCount:6,assistantCount:7,userKey:'old-user',assistantKey:'old-assistant',acceptanceSignal:'user-turn'};
 let time=7200000;
 const value={snapshot:{url,readyState:'complete',userCount:1,assistantCount:0,
   users:[{key:'exact-image'}],assistants:[],latestUserTextSha256:hash,latestAssistantAfterUser:false,
   stopControlCount:1,streamingMarkerCount:0,imageNodes:[]},page:{url,authenticated:true,ready:true},
   draft:{url,actualLength:0,sha256:'empty',visibleComposers:1}};
 const options={chatUrl:url,promptSha256:hash,draftSha256:draftHash};
 const observations=new Map(),calls={stop:0,persist:0};
 const args={pending,options,observations,key:'thread:1000',now:()=>time,probe:async()=>structuredClone(value),
  health:()=>({classification:'unknown'}),persist:()=>calls.persist++,pause:async ms=>{time+=ms;},
  stop:async(_proof,mark)=>{mark();calls.stop++;value.snapshot.stopControlCount=0;return true;}};
 return {pending,value,options,args,calls,advance:ms=>{time+=ms;},now:()=>time};
}
test('exact image fingerprint survives changed DOM baseline counts',()=>{
 const f=setup();assert.equal(imageReceiptProof(f.value,f.pending,{...f.options,now:f.now()}).kind,'stalled-generating');
});
test('wrong chat/prompt, a late response/image, manual draft, streaming and quota forbid repair',()=>{
 const f=setup();
 for(const mutate of [v=>v.snapshot.url='https://chatgpt.com/',v=>v.snapshot.latestUserTextSha256='c'.repeat(64),
  v=>v.snapshot.imageNodes=[{}],v=>v.snapshot.latestAssistantAfterUser=true,v=>v.draft.actualLength=10,
  v=>v.snapshot.streamingMarkerCount=1,v=>v.page.rateLimited=true,v=>v.page.authenticated=false]){
  const v=structuredClone(f.value);mutate(v);assert.equal(imageReceiptProof(v,f.pending,{...f.options,now:f.now()}),null);
 }
 assert.equal(imageReceiptProof(f.value,{...f.pending,outputMode:'text'},{...f.options,now:f.now()}),null);
 assert.equal(imageReceiptProof(f.value,{...f.pending,createdAt:f.now()-60000},{...f.options,now:f.now()}),null);
});
test('unchanged observations persist and only then stop once and authorize one replacement',async()=>{
 const f=setup();assert.equal((await repairStalledImage(f.args)).status,'observing');assert.equal(f.calls.stop,0);
 f.advance(90000);const result=await repairStalledImage(f.args);
 assert.equal(result.safe_to_retry,true);assert.equal(result.reason,'stopped-stalled-image');assert.equal(f.calls.stop,1);
 assert.ok([...f.args.observations.values()][0].stopAttemptedAt);assert.ok(f.calls.persist>=2);
 await repairStalledImage(f.args);assert.equal(f.calls.stop,1);
});
test('a stalled stop that never clears is never dispatched twice, including after restart',async()=>{
 const f=setup();f.args.stop=async(_p,mark)=>{mark();f.calls.stop++;return true;};
 await repairStalledImage(f.args);f.advance(90000);
 assert.equal((await repairStalledImage(f.args)).status,'stop-pending');
 f.args.observations=new Map(JSON.parse(JSON.stringify([...f.args.observations])));
 await repairStalledImage(f.args);assert.equal(f.calls.stop,1);
});
test('changed history between observation calls keeps receipt protected',async()=>{
 const f=setup();await repairStalledImage(f.args);f.advance(90000);f.value.snapshot.users[0].key='manual-turn';
 assert.equal((await repairStalledImage(f.args)).safe_to_retry,false);assert.equal(f.calls.stop,0);
});
test('429 and authentication holds never click Stop or create an observation',async()=>{
 const f=setup();f.args.health=()=>({classification:'rate-limited',blocking:{retryAfterSeconds:3600}});
 await assert.rejects(repairStalledImage(f.args),/429.*3600/);assert.equal(f.calls.stop,0);assert.equal(f.args.observations.size,0);
 f.args.health=()=>({classification:'authentication-required'});
 await assert.rejects(repairStalledImage(f.args),/BLOCKED/);assert.equal(f.calls.stop,0);
});
test('legacy acknowledgement without an echoed prompt requires exact unchanged history and an aged receipt',()=>{
 const f=setup();Object.assign(f.value.snapshot,{userCount:6,assistantCount:7,stopControlCount:0,
  users:[{key:'old-user'}],assistants:[{key:'old-assistant'}],latestUserTextSha256:'c'.repeat(64)});
 assert.equal(imageReceiptProof(f.value,f.pending,{...f.options,now:f.now()}).kind,'legacy-echo-mismatch');
 assert.equal(imageReceiptProof(f.value,{...f.pending,acceptanceSignal:'exact-user-prompt'},{...f.options,now:f.now()}),null);
 assert.equal(imageReceiptProof(f.value,{...f.pending,acceptanceSignal:'exact-source-user-prompt'},{...f.options,now:f.now()}),null);
 f.value.snapshot.users[0].key='other-user';assert.equal(imageReceiptProof(f.value,f.pending,{...f.options,now:f.now()}),null);
});
test('image appearing after Stop forbids clearing or resubmitting the old receipt',async()=>{
 const f=setup();await repairStalledImage(f.args);f.advance(90000);
 f.args.stop=async(_p,mark)=>{mark();f.calls.stop++;f.value.snapshot.stopControlCount=0;f.value.snapshot.imageNodes=[{}];return true;};
 assert.equal((await repairStalledImage(f.args)).safe_to_retry,false);assert.equal(f.calls.stop,1);
});
