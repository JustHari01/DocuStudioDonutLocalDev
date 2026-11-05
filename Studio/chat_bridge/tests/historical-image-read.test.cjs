"use strict";
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const test=require('node:test'),assert=require('node:assert/strict');
const {existingImageResult}=require('../src/image-retry.cjs');
const hash='a'.repeat(64);
const idle={readyState:'complete',stopControlCount:0,streamingMarkerCount:0,
  latestUserTextSha256:'user',latestAssistantTextSha256:'assistant'};
function fixture({imageAt=-1,busy=false,changedTail=false,changedUrl=false,endAt=2}={}) {
  let position=9,reads=0,scrolls=0,restored=false;
  const automation={
    chatTurnSnapshot:async()=>({...idle,...(busy?{stopControlCount:1}:{}),
      ...(restored && changedTail?{latestUserTextSha256:'new-user'}:{})}),
    generatedImageSnapshot:async(_w,options)=>{
      assert.equal(options.expectedUserPromptSha256,hash);
      assert.equal(options.allowEarlierMatchingPrompts,true);reads++;
      return position===imageAt?{ready:true,dataBase64:'aW1hZ2U=',matchedUserPromptSha256:hash,
        matchedPromptCount:2,historyImageCount:1}:{candidateCount:0,matchedPromptCount:1,historyImageCount:0};
    },
    executePageProbe:async(_w,source)=>{
      if(source.includes('let expanded=0'))return 0;
      scrolls++;
      if(source.includes('const action="start"')){position=0;return {available:true,top:9,atBottom:true};}
      if(source.includes('const action="restore"')){position=9;restored=true;return {available:true};}
      const before=position;position=Math.min(endAt,position+1);
      return {available:true,changed:before!==position,atEnd:position>=endAt};
    },
  };
  const sandbox={module:{exports:{}},Date,Promise,setTimeout,
    require:id=>id==='./automation.cjs'?automation:{existingImageResult}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/historical-image-read.cjs'),'utf8'),sandbox);
  return {read:opts=>sandbox.module.exports.readHistoricalImage({webContents:{getURL:()=>changedUrl && reads?'https://chatgpt.com/c/other':'https://chatgpt.com/c/saved'}},
    {expectedUserPromptSha256:hash,pollMs:0,...opts}),counts:()=>({reads,scrolls,restored,position})};
}
test('an earlier exact-prompt image is read without any submission and scroll is restored',async()=>{
  const f=fixture({imageAt:1}),r=await f.read();
  assert.equal(r.status,'ready');assert.equal(r.generated_image.data_base64,'aW1hZ2U=');
  assert.equal(r.history_scan.promptsSubmitted,0);assert.equal(r.history_scan.stable,true);
  assert.equal(f.counts().position,9);assert.equal(f.counts().restored,true);
});
test('completed empty history is distinct from an incomplete bounded read',async()=>{
  let r=await fixture().read();assert.equal(r.status,'no-image');assert.equal(r.history_scan.complete,true);
  r=await fixture({endAt:10}).read({maxSteps:1});assert.equal(r.status,'pending');assert.equal(r.history_scan.complete,false);
});
test('a busy chat or missing ownership hash cannot start a historical scan',async()=>{
  const f=fixture({busy:true});assert.equal(await f.read(),null);assert.equal(f.counts().scrolls,0);
  const g=fixture();assert.equal(await g.read({expectedUserPromptSha256:''}),null);assert.equal(g.counts().scrolls,0);
});
test('a changed tail cannot reuse an image and a changed route fails closed',async()=>{
  const f=fixture({imageAt:0,changedTail:true});assert.equal((await f.read()).status,'pending');
  const g=fixture({changedUrl:true});await assert.rejects(g.read(),/CHATGPT_CONVERSATION_CHANGED/);
});
