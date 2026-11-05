"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {conversationSubmissionProof,readConversationSubmissionSource,hash}=require('../src/conversation-image-source.cjs');
const {waitForSubmissionAccepted}=require('../src/automation.cjs');
const cid='6aceaf4f-44cc-83ec-a402-61fadf210822',url='https://chatgpt.com/c/'+cid;
const old='11111111-1111-4111-8111-111111111111',fresh='22222222-2222-4222-8222-222222222222';
const prompt='[DOCURANDOM_STAGE=thumbnail]\nCreate exactly one new image.',digest=hash(prompt);
const baseline={beforeUser:3,beforeUserKey:'data-chatgpt-search-message-ids:'+old,initialUrl:url};
function fixture(id=fresh){return {conversation_id:cid,current_node:id,mapping:{
 [id]:{id,parent:null,message:{id,author:{role:'user'},content:{parts:[prompt]}}}}};}
test('full exact source prompt with a new stable user ID proves delivery without a DOM echo',()=>{
 assert.equal(conversationSubmissionProof(fixture(),url,digest,baseline).signal,'exact-source-user-prompt');
 assert.equal(conversationSubmissionProof(fixture(old),url,digest,baseline),null);
 assert.equal(conversationSubmissionProof(fixture(),url,hash('different package'),baseline),null);
 assert.equal(conversationSubmissionProof(fixture(),url,digest,{...baseline,beforeUserKey:'generic-message'}),null);
});
test('an unloaded old conversation cannot masquerade as an empty new chat',()=>{
 assert.equal(conversationSubmissionProof(fixture(),url,digest,{beforeUser:0,initialUrl:url}),null);
 assert.equal(conversationSubmissionProof(fixture(),url,digest,{beforeUser:0,initialUrl:'https://chatgpt.com/'}).signal,'exact-source-user-prompt');
 const s=fixture();s.conversation_id='foreign';assert.equal(conversationSubmissionProof(s,url,digest,baseline),null);
});
test('an exact user prompt on an inactive branch is not delivery of the current request',()=>{
 const s=fixture();s.mapping.other={id:'other',message:{author:{role:'user'},content:{parts:['different request']}}};s.current_node='other';
 assert.equal(conversationSubmissionProof(s,url,digest,baseline),null);
});
test('submission reader uses only the existing signed-in read endpoint; provider errors supply no proof',async()=>{
 const calls=[];
 const proof=await readConversationSubmissionSource({chatUrl:url,promptSha256:digest,baseline,
  fetch:async(address,options)=>{calls.push({address,options});return {ok:true,json:async()=>fixture()};}});
 assert.equal(proof.signal,'exact-source-user-prompt');assert.equal(calls.length,1);
 assert.equal(calls[0].address,'https://chatgpt.com/backend-api/conversation/'+cid);
 assert.equal(calls[0].options.method,undefined);assert.equal(calls[0].options.redirect,'error');
 for(const status of [401,403,404])assert.equal(await readConversationSubmissionSource({chatUrl:url,promptSha256:digest,baseline,fetch:async()=>({ok:false,status})}),null);
 await assert.rejects(readConversationSubmissionSource({chatUrl:url,promptSha256:digest,baseline,
  fetch:async()=>({ok:false,status:429})}),e=>e.statusCode===429&&e.code==='CHATGPT_RATE_LIMITED');
});
function badEchoWindow(){return {isDestroyed:()=>false,webContents:{executeJavaScript:async()=>({
 assistantCount:3,userCount:3,userKey:baseline.beforeUserKey,readyState:'complete',visibilityState:'visible',
 latestUserTextSha256:hash('collapsed partial bubble'),latestUserContentSha256:hash('text with Show more'),
 users:[{key:baseline.beforeUserKey}],visibleComposers:1,actualLength:0})}};}
test('real acknowledgement loop falls back to exact source proof when DOM hashes never match',async()=>{
 let reads=0;
 const proof=await waitForSubmissionAccepted(badEchoWindow(),{beforeUser:3,beforeUserKey:baseline.beforeUserKey,
  initialUrl:url,expectedPromptSha256:digest,timeoutMs:5000,
  checkSubmissionAccepted:async options=>{reads++;assert.equal(options.baseline.beforeUserKey,baseline.beforeUserKey);
   return conversationSubmissionProof(fixture(),url,options.promptSha256,options.baseline);}});
 assert.equal(proof.signal,'exact-source-user-prompt');assert.equal(reads,1);
});
test('failed source reads leave the send uncertain rather than acknowledging old prompt text',async()=>{
 let reads=0;
 await assert.rejects(waitForSubmissionAccepted(badEchoWindow(),{beforeUser:3,beforeUserKey:baseline.beforeUserKey,
  initialUrl:url,expectedPromptSha256:digest,timeoutMs:3400,
  checkSubmissionAccepted:async()=>{reads++;throw Error('provider unavailable');}}),/chưa xác nhận/);
 assert.equal(reads,1);
});
