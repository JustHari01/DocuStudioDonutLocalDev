const {test}=require('node:test');const assert=require('node:assert/strict');
const {conversationImageProof,hash}=require('../src/conversation-image-source.cjs');
const {conversationTextProof,completedTextReceiptMatches,consumedAttachmentMatches}=require('../src/conversation-text-source.cjs');
const cid='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',url='https://chatgpt.com/c/'+cid;
function fixture(){return {conversation_id:cid,current_node:'a',mapping:{
 u:{id:'u',parent:null,message:{id:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',author:{role:'user'},create_time:100,content:{parts:['exact prompt']}}},
 t:{id:'t',parent:'u',message:{id:'tool',author:{role:'tool'},status:'finished_successfully',content:{parts:['There is no usable image target in the conversation.']}}},
 a:{id:'a',parent:'t',message:{id:'assistant',author:{role:'assistant'},channel:'final',recipient:'all',status:'finished_successfully',end_turn:true,content:{content_type:'text',parts:['']}}}}};}
test('empty ended image result requires a finished tool failure on the exact branch',()=>{
 const f=fixture();assert.equal(conversationImageProof(f,url,hash('exact prompt')).terminal_no_image,true);
 f.mapping.a.message.end_turn=false;assert.equal(conversationImageProof(f,url,hash('exact prompt')).terminal_no_image,false);
 f.mapping.a.message.end_turn=true;f.mapping.t.message.content.parts=['Working'];
 assert.equal(conversationImageProof(f,url,hash('exact prompt')).terminal_no_image,false);
});
test('an image pointer or unfinished tool cannot be converted to a negative result',()=>{
 const f=fixture();f.mapping.t.message.content.parts.push({content_type:'image_asset_pointer',asset_pointer:'image'});
 const p=conversationImageProof(f,url,hash('exact prompt'));assert.equal(p.terminal_no_image,false);assert.equal(p.image_message_ids.length,1);
 const g=fixture();g.mapping.t.message.status='in_progress';assert.equal(conversationImageProof(g,url,hash('exact prompt')).terminal_no_image,false);
});
test('empty final text is completed-invalid, never accepted as JSON and never pending indefinitely',()=>{
 const f=fixture(),p=conversationTextProof(f,url,hash('exact prompt'));
 assert.equal(p.status,'completed-invalid');assert.equal(p.output_text,'');assert.equal(p.source_proof.complete,true);
 f.mapping.a.message.end_turn=false;assert.equal(conversationTextProof(f,url,hash('exact prompt')).status,'pending');
});
test('text receipt retirement requires matching prompt and accepted user/time',()=>{
 const p=conversationTextProof(fixture(),url,hash('exact prompt')).source_proof;
 const receipt={outputMode:'text',promptSha256:hash('exact prompt'),userCount:0,createdAt:100000};
 assert.equal(completedTextReceiptMatches(p,receipt),true);
 assert.equal(completedTextReceiptMatches(p,{...receipt,promptSha256:hash('other')}),false);
 assert.equal(completedTextReceiptMatches(p,{...receipt,createdAt:200000}),false);
 assert.equal(completedTextReceiptMatches(p,{...receipt,acceptedUserKey:'id:cccccccc-cccc-4ccc-8ccc-cccccccccccc'}),false);
 assert.equal(completedTextReceiptMatches(p,{...receipt,outputMode:'generated_image'}),false);
});
test('only a confirmed image attachment consumed by that ended request is released',()=>{
 const f=fixture();f.mapping.u.message.content.parts.push({content_type:'image_asset_pointer',asset_pointer:'exact-image'});
 const p=conversationTextProof(f,url,hash('exact prompt')).source_proof;
 const pending={outputMode:'text',promptSha256:hash('exact prompt'),userCount:0,createdAt:100000};
 const attachment={status:'confirmed',files:[{sha256:'a'.repeat(64)}],updatedAt:99900};
 assert.equal(consumedAttachmentMatches(p,attachment,pending),true);
 assert.equal(consumedAttachmentMatches(p,{...attachment,updatedAt:100100},pending),false);
 assert.equal(consumedAttachmentMatches({...p,user_attachment_present:false},attachment,pending),false);
});
