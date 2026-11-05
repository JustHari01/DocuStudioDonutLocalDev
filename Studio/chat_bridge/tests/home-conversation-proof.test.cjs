"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {homeConversationProof}=require('../src/home-conversation-proof.cjs');
const url='https://chatgpt.com/c/6acd08c9-49e4-83ec-b47f-0eda02c1b3a8';
const snapshot={url,readyState:'complete',userCount:1,users:[{key:'exact-first-user'}],latestUserTextSha256:'a'.repeat(64)};
const page={url,authenticated:true,ready:true};
test('the exact single first prompt can identify an accepted home turn without submitting anything',()=>{
  assert.ok(homeConversationProof(snapshot,page,{url,promptSha256:'a'.repeat(64)}));
  assert.ok(homeConversationProof({...snapshot,latestUserTextSha256:'b'.repeat(64),latestUserContentSha256:'a'.repeat(64)},page,{url,promptSha256:'a'.repeat(64)}));
});
test('another prompt, conversation, incomplete history or failed account cannot bind a receipt',()=>{
  for(const change of [{userCount:2},{latestUserTextSha256:'b'.repeat(64)},
    {users:[]},{conversationLoadFailed:true},{connectionInterrupted:true},{url:'https://chatgpt.com/'}]){
    assert.equal(homeConversationProof({...snapshot,...change},page,{url,promptSha256:'a'.repeat(64)}),null);
  }
  assert.equal(homeConversationProof(snapshot,{...page,ready:false},{url,promptSha256:'a'.repeat(64)}),null);
});
