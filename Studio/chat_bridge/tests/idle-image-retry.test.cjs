"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {idleImageRetryProof}=require('../src/idle-image-retry.cjs');
const url='https://chatgpt.com/c/6acd0d03-cf90-83ec-a71d-03b170c2116b';
const snapshot={url,readyState:'complete',latestUserTextSha256:'a'.repeat(64),
 userCount:1,assistantCount:0,users:[{key:'exact-first-user'}],assistants:[],imageNodes:[],stopControlCount:0,streamingMarkerCount:0};
const page={url,authenticated:true,ready:true};
const now=Date.now(),pending={createdAt:now-20*60_000,submissionAccepted:true,sendAttempted:true,
 outputMode:'generated_image',userCount:0,assistantCount:0,userKey:'',assistantKey:''};
const options={chatUrl:url,promptSha256:'a'.repeat(64),now};
test('an aged exact image receipt without any response has a stable retry proof',()=>{
 assert.ok(idleImageRetryProof(snapshot,page,pending,options));
});
test('live generation, a late answer or image and another prompt keep the old image receipt protected',()=>{
 for(const change of [{stopControlCount:1},{streamingMarkerCount:1},{assistantCount:1},{userCount:2},
   {imageNodes:[{src:'late-image'}]},{latestUserTextSha256:'b'.repeat(64)},{conversationLoadFailed:true}]){
   assert.equal(idleImageRetryProof({...snapshot,...change},page,pending,options),null);
 }
 assert.equal(idleImageRetryProof(snapshot,page,{...pending,createdAt:now-60_000},options),null);
 assert.equal(idleImageRetryProof(snapshot,page,{...pending,createdAt:0},options),null);
 assert.equal(idleImageRetryProof(snapshot,page,{...pending,outputMode:'text'},options),null);
});
