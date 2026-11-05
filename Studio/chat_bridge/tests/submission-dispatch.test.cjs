const test=require('node:test'),assert=require('node:assert/strict');
const {providerDispatchFields,noteProviderDispatch,uncertainDispatchError}=require('../src/submission-dispatch.cjs');
test('provider stream failure stays uncertain through receipt serialization',()=>{
 const p={createdAt:1000,sendAttempted:true,submissionAccepted:false};
 const event={method:'POST',url:'https://chatgpt.com/backend-api/f/conversation?private-secret',id:123,statusCode:200,responseHeaders:{'Set-Cookie':['secret']}};
 assert(noteProviderDispatch(p,event,{now:1100}));assert(noteProviderDispatch(p,{...event,error:'net::ERR_FAILED'},{now:1200}));
 const restored=JSON.parse(JSON.stringify({...p,...providerDispatchFields(p)}));
 assert.equal(uncertainDispatchError(restored).code,'CHATGPT_SUBMISSION_TRANSPORT_UNCERTAIN');
 assert.match(uncertainDispatchError(restored).message,/HTTP=200;net=net::ERR_FAILED/);
 assert.doesNotMatch(JSON.stringify(restored),/private|secret|Set-Cookie|url|responseHeaders/);
 assert.equal(restored.providerDispatchObservedAt,1100);
});
test('sidebar, preparation, other origins and requests before dispatch cannot claim a provider send',()=>{
 for(const url of ['https://chatgpt.com/backend-api/f/conversation/prepare','https://chatgpt.com/backend-api/settings/user','https://evil.test/backend-api/f/conversation']){
  const p={createdAt:1000,sendAttempted:true};assert.equal(noteProviderDispatch(p,{url,method:'POST'},{now:1100}),false);assert.equal(uncertainDispatchError(p),null);
 }
 const p={createdAt:1000,sendAttempted:true};assert.equal(noteProviderDispatch(p,{url:'https://chatgpt.com/backend-api/f/conversation',method:'POST'},{now:999}),false);
 assert.deepEqual(providerDispatchFields({providerDispatchObservedAt:'invalid'}),{});
});
