'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {conversationCapacityState,conversationCapacityError}=require('../src/conversation-capacity.cjs');
const {sourceHttpError,readConversationImageSource,readConversationTextSource}=require('../src/conversation-image-source.cjs');
test('capacity requires the provider notice and a dedicated new-chat action',()=>{
 const text="You've reached the maximum length for this conversation, but you can keep talking by starting a new chat.";
 assert.equal(conversationCapacityState(text,['Start new chat']).conversationFull,true);
 assert.equal(conversationCapacityState(text,['New chat']).conversationFull,false);
 assert.equal(conversationCapacityState('An ordinary reply',['Start new chat']).conversationFull,false);
 assert.equal(conversationCapacityError().code,'CHATGPT_CONVERSATION_FULL');
});
test('source throttling carries the real Retry-After instead of silently falling through to reload',async()=>{
 const response={ok:false,status:429,headers:new Map([['retry-after','1200']])};
 const error=sourceHttpError(response);
 assert.equal(error.statusCode,429);assert.equal(error.retryAfterSeconds,1200);
 assert.match(error.message,/retry-after-seconds=1200/);
 const args={chatUrl:'https://chatgpt.com/c/6aceaf4f-44cc-83ec-a402-61fadf210822',promptSha256:'a'.repeat(64),fetch:async()=>response};
 for(const read of [readConversationImageSource,readConversationTextSource])
  await assert.rejects(read(args),e=>e.statusCode===429&&e.retryAfterSeconds===1200);
});
