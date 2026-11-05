"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {conversationImageProof,readConversationImageSource,conversationTextProof,hash}=require('../src/conversation-image-source.cjs');
const cid='6aceaf4f-44cc-83ec-a402-61fadf210822',url='https://chatgpt.com/c/'+cid,prompt='[DOCURANDOM_STAGE=thumbnail]\nCreate exact Thai motor image',digest=hash(prompt);
function fixture(image=false){return {conversation_id:cid,context_truncation_continuation:null,current_node:'a',mapping:{
 u:{id:'u',parent:null,message:{id:'u',author:{role:'user'},content:{parts:[prompt]}}},
 a:{id:'a',parent:'u',message:{id:'a',author:{role:'assistant'},status:'finished_successfully',content:{parts:image?[{content_type:'image_asset_pointer',asset_pointer:'sediment://file_owned'}]:['KHÔNG TẠO ĐƯỢC: initialization failed']}}}}};}
const item={id:'libfile_'+'a'.repeat(32),origination_thread_id:cid,origination_message_id:'a',access_kind:'owned',state:'ready',mime_type:'image/png',width:1280,height:720};
test('complete source proves exact finished negative, never an unmatched prompt',()=>{
 assert.equal(conversationImageProof(fixture(),url,digest).terminal_no_image,true);
 assert.equal(conversationImageProof(fixture(),url,hash('another video')),null);
 const source=fixture();source.context_truncation_continuation='other';assert.equal(conversationImageProof(source,url,digest),null);
 source.context_truncation_continuation=null;source.conversation_id='other';assert.equal(conversationImageProof(source,url,digest),null);
});
test('an image on an earlier response branch prevents false absence',()=>{
 const source=fixture();source.mapping.old={id:'old',parent:'u',message:{id:'old',author:{role:'assistant'},content:{parts:[{content_type:'image_asset_pointer',asset_pointer:'sediment://earlier'}]}}};
 const proof=conversationImageProof(source,url,digest);assert.equal(proof.terminal_no_image,false);assert.deepEqual(proof.image_message_ids,['old']);
});
test('a newer unanswered exact request cannot inherit an old failure',()=>{
 const source=fixture();source.mapping.u2={id:'u2',parent:'a',message:{id:'u2',author:{role:'user'},content:{parts:[prompt]}}};source.current_node='u2';
 assert.equal(conversationImageProof(source,url,digest).terminal_no_image,false);
});

test('legacy retry owns its generated image only after the exact full prompt',()=>{
 const retry='[DOCURANDOM_STAGE=thumbnail-retry]\nHãy thử tạo ảnh MỚI TỪ ĐẦU thêm đúng một lần, theo toàn bộ prompt thumbnail ngay trước. Giữ nguyên bố cục và mọi chữ Nhật. Đây là text-to-image, không chỉnh sửa ảnh và không cần ảnh nguồn. Hãy dùng công cụ tạo ảnh, trả đúng một ảnh.';
 const s=fixture();
 s.mapping.retry={id:'retry',parent:'a',message:{id:'retry',author:{role:'user'},content:{parts:[retry]}}};
 s.mapping.image={id:'image',parent:'retry',message:{id:'image',author:{role:'assistant'},status:'finished_successfully',content:{parts:[{content_type:'image_asset_pointer',asset_pointer:'sediment://legacy_owned'}]}}};
 s.current_node='image';
 const proof=conversationImageProof(s,url,digest);
 assert.equal(proof.legacy_retry_alias_verified,true);
 assert.equal(proof.terminal_no_image,false);
 assert.ok(proof.image_message_ids.includes('image'));
 s.mapping.u.message.content.parts=['a different full prompt'];
 assert.equal(conversationImageProof(s,url,digest),null);
});
test('reference image attachments are not generated thumbnails',()=>{
 const source=fixture();source.mapping.u.message.content.parts.push({content_type:'image_asset_pointer',asset_pointer:'sediment://reference'});
 assert.equal(conversationImageProof(source,url,digest).terminal_no_image,true);
});
test('a completed audit after an explicit negative image does not make that image turn pending forever',()=>{
 const s=fixture();s.mapping.audit={id:'audit',parent:'a',message:{author:{role:'user'},content:{parts:['audit the script']}}};
 s.mapping.plan={id:'plan',parent:'audit',message:{author:{role:'assistant'},status:'finished_successfully',content:{parts:['audit result']}}};s.current_node='plan';
 assert.equal(conversationImageProof(s,url,digest).terminal_no_image,true);
});
test('text source returns only the completed final for the exact latest user request',()=>{
 const s=fixture();s.mapping.a.message.channel='final';s.mapping.a.message.content.parts=['actual final answer'];
 assert.equal(conversationTextProof(s,url,digest).text,'actual final answer');
 assert.equal(conversationTextProof(s,url,hash('other prompt')),null);
 s.mapping.a.message.channel='analysis';assert.equal(conversationTextProof(s,url,digest),null);
 s.mapping.a.message.channel='final';s.mapping.a.message.status='in_progress';assert.equal(conversationTextProof(s,url,digest),null);
 s.mapping.a.message.status='finished_successfully';s.mapping.a.message.metadata={is_visually_hidden_from_conversation:true};
 assert.equal(conversationTextProof(s,url,digest),null);
});

test('historical final text is recoverable after an audit only with its own new-user receipt',()=>{
 const s=fixture(),old='11111111-1111-4111-8111-111111111111',fresh='22222222-2222-4222-8222-222222222222';
 s.mapping.u.message.id=fresh;
 s.mapping.audit={id:'audit',parent:'a',message:{author:{role:'user'},content:{parts:['audit later']}}};
 s.mapping.result={id:'result',parent:'audit',message:{author:{role:'assistant'},status:'finished_successfully',content:{parts:['audit result']}}};s.current_node='result';
 assert.equal(conversationTextProof(s,url,digest),null);
 assert.equal(conversationTextProof(s,url,digest,{userKey:'data-message-id:'+old}).text,'KHÔNG TẠO ĐƯỢC: initialization failed');
 assert.equal(conversationTextProof(s,url,digest,{userKey:'data-message-id:'+fresh}),null);
});
test('only exact owned file is downloaded; foreign chat and borrowed images are ignored',async()=>{
 const calls=[],fetch=async(address,options)=>{calls.push([address,options.method||'GET']);
  if(address.endsWith('/conversation/'+cid))return {ok:true,json:async()=>fixture(true)};
  if(address.endsWith('/files/library'))return {ok:true,json:async()=>({items:[{...item,id:'libfile_'+'b'.repeat(32),origination_thread_id:'foreign'},{...item,id:'libfile_'+'c'.repeat(32),access_kind:'shared'},item]})};
  assert.equal(address,'https://chatgpt.com/api/library/files/'+item.id+'/content');return {ok:true,headers:new Map([['content-type','image/png']]),body:(async function*(){yield Buffer.from('image bytes')})()};};
 const result=await readConversationImageSource({chatUrl:url,promptSha256:digest,fetch});assert.equal(result.status,'ready');
 assert.equal(Buffer.from(result.generated_image.data_base64,'base64').toString(),'image bytes');
 assert.deepEqual(calls.map(x=>x[1]),['GET','POST','GET']);
});
test('missing library file does not convert an existing image to a fresh generation',async()=>{
 const fetch=async address=>({ok:true,json:async()=>address.includes('/conversation/')?fixture(true):{items:[]}});
 assert.equal((await readConversationImageSource({chatUrl:url,promptSha256:digest,fetch})).status,'pending');
});
test('provider/auth failures never supply absence proof',async()=>{
 for(const status of [401,403,404,429])await assert.rejects(readConversationImageSource({chatUrl:url,promptSha256:digest,
  fetch:async()=>({ok:false,status})}),new RegExp('HTTP '+status));
});
test('ambiguous candidates and wrong MIME remain unconfirmed',async()=>{
 for(const ambiguous of [true,false]){
  const fetch=async address=>address.includes('/conversation/')?{ok:true,json:async()=>fixture(true)}:
   address.endsWith('/files/library')?{ok:true,json:async()=>({items:ambiguous?[item,{...item,id:'libfile_'+'b'.repeat(32)}]:[item]})}:
   {ok:true,headers:new Map([['content-type','text/html']])};
  const result=await readConversationImageSource({chatUrl:url,promptSha256:digest,fetch});assert.ok(result===null||result.status==='pending');
 }
});
