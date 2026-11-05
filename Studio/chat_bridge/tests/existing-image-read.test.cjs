"use strict";
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const test=require("node:test");
const assert=require("node:assert/strict");
const {existingImageResult}=require("../src/image-retry.cjs");

function readerFixture(snapshots,{changeUrl=false}={}){
  let probes=0,reloads=0,imageReads=0;
  let url="https://chatgpt.com/c/saved-image";
  const automation={
    withRenderedWindowStage:(_window,_signal,action)=>action(),
    chatTurnSnapshot:async()=>{
      const state=snapshots[Math.min(probes++,snapshots.length-1)];
      if(changeUrl)url="https://chatgpt.com/c/other-video";
      return state;
    },
    reloadSavedChatPage:async(_window,target,options)=>{
      assert.equal(target,"https://chatgpt.com/c/saved-image");
      assert.equal(options.allowHomeReload,false);
      reloads++;
    },
    generatedImageSnapshot:async()=>{
      imageReads++;
      return {ready:true,dataBase64:"aW1hZ2U=",afterLatestUser:true,width:1280,height:720};
    },
  };
  const sandbox={module:{exports:{}},Date,Promise,setTimeout,
    require:id=>id==="./automation.cjs"?automation:{existingImageResult}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../src/existing-image-read.cjs"),"utf8"),sandbox);
  return {read:options=>sandbox.module.exports.readExistingImage({webContents:{getURL:()=>url}},options),
    counts:()=>({probes,reloads,imageReads})};
}

const failed={conversationLoadFailed:true,userCount:0,assistantCount:0,readyState:"complete"};
const ready={userCount:1,assistantCount:1,readyState:"complete",stopControlCount:0,streamingMarkerCount:0};

test("same saved page can hydrate after a post-reload error banner",async()=>{
  const f=readerFixture([failed,failed,failed,ready]);
  assert.equal((await f.read({timeoutMs:100,pollMs:1})).status,"ready");
  assert.equal(f.counts().reloads,1);
  assert.equal(f.counts().imageReads,1);
});

test("persistent loading failure ends after one reload and a bounded wait",async()=>{
  const f=readerFixture([failed]);
  await assert.rejects(f.read({timeoutMs:15,pollMs:1}),/CHATGPT_CONVERSATION_LOAD_FAILED/);
  assert.equal(f.counts().reloads,1);
  assert.equal(f.counts().imageReads,0);
});

test("a changed conversation cannot supply another video's thumbnail",async()=>{
  const f=readerFixture([ready],{changeUrl:true});
  await assert.rejects(f.read({timeoutMs:100,pollMs:1}),/CHATGPT_CONVERSATION_CHANGED/);
  assert.equal(f.counts().reloads,0);
  assert.equal(f.counts().imageReads,0);
});
