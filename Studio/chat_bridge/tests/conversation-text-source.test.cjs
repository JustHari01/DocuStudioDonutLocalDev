const {test}=require('node:test');const assert=require('node:assert/strict');
const {hash,conversationTextProof}=require('../src/conversation-text-source.cjs');
const cid='6ad8d458-e524-83ec-a97d-40505d6d2854',url='https://chatgpt.com/c/'+cid;
function fixture(){return {conversation_id:cid,current_node:'a2',mapping:{
 u1:{id:'u1',parent:null,message:{id:'user1',author:{role:'user'},content:{parts:['verify this thumbnail']}}},
 a1:{id:'a1',parent:'u1',message:{id:'answer1',author:{role:'assistant'},status:'finished_successfully',end_turn:true,content:{parts:['{"passed":true}']}}},
 u2:{id:'u2',parent:'a1',message:{id:'user2',author:{role:'user'},content:{parts:['/summon automobile']}}},
 a2:{id:'a2',parent:'u2',message:{id:'answer2',author:{role:'assistant'},status:'finished_successfully',end_turn:true,content:{parts:['unrelated Minecraft answer']}}}}};}
test('pins exact user and its own final response before a later user turn',()=>{
 const p=conversationTextProof(fixture(),url,hash('verify this thumbnail'));
 assert.equal(p.output_text,'{"passed":true}');assert.equal(p.source_proof.assistant_message_id,'answer1');
});
test('does not accept missing, ambiguous, partial, or another chat',()=>{
 let f=fixture();assert.equal(conversationTextProof(f,url,hash('missing')).status,'pending');
 f.mapping.a1.message.status='in_progress';assert.equal(conversationTextProof(f,url,hash('verify this thumbnail')).status,'pending');
 f=fixture();f.mapping.u2.message.content.parts=['verify this thumbnail'];
 assert.equal(conversationTextProof(f,url,hash('verify this thumbnail')).status,'pending');
 f=fixture();f.conversation_id='other';assert.equal(conversationTextProof(f,url,hash('verify this thumbnail')),null);
});

test('accepts only an identical-label URL auto-link with the complete expected prompt',()=>{
 const original='Read https://studio.youtube.com then analyze these facts';
 const f=fixture();f.mapping.u1.message.content.parts=['Read [https://studio.youtube.com](https://studio.youtube.com) then analyze these facts'];
 assert.equal(conversationTextProof(f,url,hash(original)).status,'pending');
 const result=conversationTextProof(f,url,hash(original),original);
 assert.equal(result.output_text,'{"passed":true}');assert.equal(result.source_proof.request_format,'identical-url-autolink');
 f.mapping.u1.message.content.parts=['Read [https://studio.youtube.com](https://evil.example) then analyze these facts'];
 assert.equal(conversationTextProof(f,url,hash(original),original).status,'pending');
 f.mapping.u1.message.content.parts=['Read [the same site](https://studio.youtube.com) then analyze these facts'];
 assert.equal(conversationTextProof(f,url,hash(original),original).status,'pending');
});
