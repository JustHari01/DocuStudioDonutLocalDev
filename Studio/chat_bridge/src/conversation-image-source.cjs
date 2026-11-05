"use strict";
const crypto=require('node:crypto');
const {isExplicitImageFailure}=require('./image-terminal.cjs');
const HASH=/^[a-f0-9]{64}$/;
const idFromUrl=url=>/^https:\/\/chatgpt\.com\/c\/([a-f0-9-]{36})(?:[?#].*)?$/.exec(String(url||''))?.[1]||'';
const hash=text=>crypto.createHash('sha256').update(String(text||'').normalize('NFC').replace(/\s+/gu,' ').trim()).digest('hex');
const LEGACY_RETRY='[DOCURANDOM_STAGE=thumbnail-retry]\nHãy thử tạo ảnh MỚI TỪ ĐẦU thêm đúng một lần, theo toàn bộ prompt thumbnail ngay trước. Giữ nguyên bố cục và mọi chữ Nhật. Đây là text-to-image, không chỉnh sửa ảnh và không cần ảnh nguồn. Hãy dùng công cụ tạo ảnh, trả đúng một ảnh.';
const textOf=message=>(message?.content?.parts||[]).filter(p=>typeof p==='string').join('\n');
function sourceHttpError(response){
 const status=Number(response?.status)||0;
 if(status!==429)return Error('CHATGPT_IMAGE_SOURCE_UNAVAILABLE: HTTP '+status);
 const value=String(response.headers?.get?.('retry-after')||'').trim();
 const parsed=/^\d+$/.test(value)?Number(value):Math.ceil((Date.parse(value)-Date.now())/1000);
 const wait=Number.isFinite(parsed)&&parsed>0?Math.min(parsed,172800):60;
 return Object.assign(Error('CHATGPT_RATE_LIMITED: HTTP 429; retry-after-seconds='+wait+'; giữ nguyên biên nhận.'),
   {code:'CHATGPT_RATE_LIMITED',statusCode:429,retryAfterSeconds:wait});
}
function imageReference(value){
 if(!value||typeof value!=='object')return false;
 if(value.content_type==='image_asset_pointer'||(typeof value.asset_pointer==='string'&&value.asset_pointer))return true;
 return Object.values(value).some(v=>v&&typeof v==='object'&&imageReference(v));
}
function conversationImageProof(source,chatUrl,promptSha256){
 const cid=idFromUrl(chatUrl),mapping=source?.mapping;
 if(!cid||!HASH.test(promptSha256||'')||!mapping||Array.isArray(mapping)
   ||source.conversation_id!==cid||source.context_truncation_continuation)return null;
 const nodes=Object.values(mapping),users=nodes.filter(n=>n.message?.author?.role==='user'&&hash(textOf(n.message))===promptSha256);
 if(!users.length)return null;
 const ids=new Set(users.map(n=>n.id));
 // Older Bridge builds used this exact context-only follow-up. Bind it only
 // to the immediately preceding matching request, never another video/chat.
 for(const n of nodes.filter(n=>n.message?.author?.role==='user'&&hash(textOf(n.message))===hash(LEGACY_RETRY))){
  let prior=mapping[n.parent];const seen=new Set();
  while(prior&&prior.message?.author?.role!=='user'&&!seen.has(prior.id)){seen.add(prior.id);prior=mapping[prior.parent];}
  if(prior&&ids.has(prior.id))ids.add(n.id);
 }
 const owner=node=>{const seen=new Set();while(node&&!seen.has(node.id)){seen.add(node.id);
   if(node.message?.author?.role==='user')return ids.has(node.id)?node.id:'';
   node=mapping[node.parent];}return '';};
 const associated=nodes.filter(n=>owner(n)),imageNodes=associated.filter(n=>n.message?.author?.role!=='user'&&imageReference(n.message));
 const final=associated.filter(n=>n.message?.author?.role==='assistant'&&n.message.status==='finished_successfully'
   &&(!n.message.channel||n.message.channel==='final')&&n.message.end_turn!==false
   &&n.message.metadata?.is_visually_hidden_from_conversation!==true
   &&isExplicitImageFailure(textOf(n.message)));
 // A newer accepted image prompt still awaiting an answer cannot be retired
 // merely because an older attempt finished. Follow the server's active branch.
 const current=mapping[source.current_node];
 const branch=[],seen=new Set();let cursor=current;
 while(cursor&&!seen.has(cursor.id)){seen.add(cursor.id);branch.unshift(cursor);cursor=mapping[cursor.parent];}
 const latestRequest=[...branch].reverse().find(n=>ids.has(n.id));
 const requestIndex=latestRequest?branch.indexOf(latestRequest):-1;
 const replySegment=[];
 if(requestIndex>=0)for(const n of branch.slice(requestIndex+1)){
   if(n.message?.author?.role==='user')break;replySegment.push(n);
 }
 let currentFinal=[...replySegment].reverse().find(n=>final.includes(n));
 // Some finished image-tool failures leave a deliberately empty final answer.
 // Require both the exact failed tool result and an ended active-branch final;
 // an empty/in-progress assistant alone never authorizes another generation.
 if(!currentFinal&&!imageNodes.length){
  const ended=replySegment.at(-1),message=ended?.message;
  const toolFailure=replySegment.find(n=>n.message?.author?.role==='tool'
    &&n.message.status==='finished_successfully'
    &&/no usable image target|image generation failed|image tool.*failed/i.test(textOf(n.message)));
  if(ended?.id===source.current_node&&message?.author?.role==='assistant'
    &&message.status==='finished_successfully'&&message.end_turn===true
    &&message.channel==='final'&&!textOf(message).trim()&&toolFailure){
   currentFinal=ended;
   currentFinal={...ended,message:{...message,content:{parts:['CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: '+textOf(toolFailure.message)]}}};
  }
 }
 return {conversation_id:cid,prompt_sha256:promptSha256,matched_user_ids:[...ids],
   latest_request_user_id:latestRequest?.message?.id||latestRequest?.id||'',
   latest_request_prompt_sha256:latestRequest?hash(textOf(latestRequest.message)):'',
   legacy_retry_alias_verified:!!latestRequest&&hash(textOf(latestRequest.message))===hash(LEGACY_RETRY),
   latest_request_created_at:latestRequest?.message?.create_time||0,
   image_message_ids:imageNodes.map(n=>n.message.id||n.id),node_count:nodes.length,
   complete:true,terminal_no_image:!imageNodes.length&&!!latestRequest&&!!currentFinal,
   terminal_message_id:currentFinal?.message?.id||'',terminal_detail:currentFinal?textOf(currentFinal.message).slice(0,1000):'',ownerForMessage:messageId=>{
     const n=nodes.find(n=>n.message?.id===messageId||n.id===messageId);return n?owner(n):'';}};
}
async function readConversationImageSource({chatUrl,promptSha256,fetch,headers={}}){
 const cid=idFromUrl(chatUrl);if(!cid||!HASH.test(promptSha256||''))return null;
 // These are the signed-in Web client's observed read routes. No Responses,
 // send, generation or edit endpoint is called and no auth header is persisted.
 const deadline=AbortSignal.timeout(12_000);
 const request=async(path,options={})=>{
  const bytesRoute=/^\/api\/library\/files\/libfile_[a-f0-9]{32}\/content$/.test(path);
  // Library's normal <img> request follows a signed bytes redirect. Send no
  // copied Authorization header on that route; Electron scopes cookies by
  // destination domain just as it does for the already-observed image element.
  const response=await fetch('https://chatgpt.com'+path,{...options,
    headers:bytesRoute?{Accept:'image/png,image/jpeg,image/webp'}:{...headers,...options.headers},
    signal:deadline,redirect:bytesRoute?'follow':'error'});
  if(!response.ok)throw sourceHttpError(response);
  return response;
 };
 const source=await (await request('/backend-api/conversation/'+cid)).json();
 const proof=conversationImageProof(source,chatUrl,promptSha256);if(!proof)return null;
 const publicProof={...proof};delete publicProof.ownerForMessage;
 if(proof.terminal_no_image)return {status:'no-image',source_proof:publicProof,detail:'Exact image request finished without an image: '+proof.terminal_detail};
 if(!proof.image_message_ids.length)return {status:'pending',source_proof:publicProof};
 let cursor=null;
 for(let page=0;page<3;page++){
  const listing=await (await request('/backend-api/files/library',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({cursor,limit:50,categories:['image'],source:null,include_hidden_files:false,
     include_saved_entities:true,include_sites:false,trashed_only:false,external_account_keys:{}})})).json();
  if(!Array.isArray(listing.items))return null;
  const matches=listing.items.filter(item=>item.origination_thread_id===cid&&item.access_kind==='owned'
   &&item.state==='ready'&&/^libfile_[a-f0-9]{32}$/.test(item.id||'')&&/^image\/(png|jpeg|webp)$/.test(item.mime_type||'')
   &&proof.image_message_ids.includes(item.origination_message_id)&&proof.ownerForMessage(item.origination_message_id));
  if(matches.length>1)return {status:'pending',source_proof:publicProof,detail:'Multiple exact owned image candidates; preserve receipt'};
  if(matches.length===1){
   const item=matches[0];if(item.width<320||item.height<180||item.width*item.height>60_000_000)return null;
   const response=await request('/api/library/files/'+item.id+'/content');
   const mime=(response.headers.get('content-type')||'').split(';')[0];if(mime!==item.mime_type)return null;
   const chunks=[];let size=0;
   for await(const chunk of response.body){size+=chunk.length;if(size>20*1024*1024)throw Error('Image source exceeds 20 MB');chunks.push(Buffer.from(chunk));}
   const bytes=Buffer.concat(chunks);if(!bytes.length)return null;
   return {status:'ready',source_proof:{...publicProof,file_id:item.id,origin_message_id:item.origination_message_id},
    generated_image:{data_base64:bytes.toString('base64'),mime_type:mime,width:item.width,height:item.height,size}};
  }
  if(!listing.cursor)break;cursor=listing.cursor;
 }
 // A missing Library result never grants a new generation: the source already
 // contains image pointers, so it remains a collection problem.
 return {status:'pending',source_proof:publicProof,detail:'Exact image exists but its owned Library file is not available yet'};
}
function conversationTextProof(source,chatUrl,promptSha256,receipt){
 const cid=idFromUrl(chatUrl),mapping=source?.mapping;
 if(!cid||!HASH.test(promptSha256||'')||!mapping||Array.isArray(mapping)
   ||source.conversation_id!==cid||source.context_truncation_continuation)return null;
 const branch=[],seen=new Set();let node=mapping[source.current_node];
 while(node&&!seen.has(node.id)){seen.add(node.id);branch.unshift(node);node=mapping[node.parent];}
 const user=[...branch].reverse().find(n=>n.message?.author?.role==='user'&&hash(textOf(n.message))===promptSha256);
 if(!user)return null;
 const keyId=value=>String(value||'').match(/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}/i)?.[0]||'';
 const before=keyId(receipt?.userKey),accepted=keyId(receipt?.acceptedUserKey),userId=user.message.id||user.id;
 if((before&&userId===before)||(accepted&&userId!==accepted))return null;
 const tail=branch.slice(branch.indexOf(user)+1),nextUser=tail.findIndex(n=>n.message?.author?.role==='user');
 if(nextUser>=0&&!before&&!accepted)return null;
 const replies=(nextUser>=0?tail.slice(0,nextUser):tail).filter(n=>n.message?.author?.role==='assistant'
   &&(!n.message.channel||n.message.channel==='final')
   &&(!n.message.recipient||n.message.recipient==='all')
   &&n.message.metadata?.is_visually_hidden_from_conversation!==true);
 const last=replies.at(-1),message=last?.message;
 if(!message||(nextUser<0&&source.current_node!==last.id)||message.status!=='finished_successfully'||message.end_turn===false
   ||message.content?.content_type&&message.content.content_type!=='text')return null;
 const text=textOf(message).trim();if(!text)return null;
 return {text,source_proof:{conversation_id:cid,prompt_sha256:promptSha256,
   user_message_id:user.message.id||user.id,assistant_message_id:message.id||last.id,complete:true}};
}
function sourceImageReceiptMatches(proof,pending,status){
 if(!proof?.complete||pending?.outputMode!=='generated_image'
   ||(proof.prompt_sha256!==pending.promptSha256
      &&!(proof.legacy_retry_alias_verified&&proof.latest_request_prompt_sha256===pending.promptSha256)))return false;
 // An existing owned image completes collection; this grants no new send.
 if(status==='ready')return true;
 if(!proof.terminal_no_image||status!=='no-image')return false;
 const keyId=value=>String(value||'').match(/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}/i)?.[0]||'';
 const latest=proof.latest_request_user_id,accepted=keyId(pending.acceptedUserKey),before=keyId(pending.userKey);
 if(accepted)return latest===accepted;
 if(before)return !!latest&&latest!==before;
 return pending.userCount===0&&proof.matched_user_ids?.length===1
   &&Number(proof.latest_request_created_at)*1000>=Number(pending.createdAt)-10_000;
}
async function readConversationTextSource({chatUrl,promptSha256,fetch,headers={},signal,receipt}){
 const cid=idFromUrl(chatUrl);if(!cid||!HASH.test(promptSha256||''))return null;
 const deadline=signal?AbortSignal.any([signal,AbortSignal.timeout(10_000)]):AbortSignal.timeout(10_000);
 const response=await fetch('https://chatgpt.com/backend-api/conversation/'+cid,
   {headers,signal:deadline,redirect:'error'});
 if(!response.ok){if(response.status===429)throw sourceHttpError(response);return null;}
 return conversationTextProof(await response.json(),chatUrl,promptSha256,receipt);
}
function conversationSubmissionProof(source,chatUrl,promptSha256,baseline={}){
 const cid=idFromUrl(chatUrl),mapping=source?.mapping;
 if(!cid||!HASH.test(promptSha256||'')||!mapping||Array.isArray(mapping)
   ||source.conversation_id!==cid||source.context_truncation_continuation)return null;
 const branch=[],seen=new Set();let node=mapping[source.current_node];
 while(node&&!seen.has(node.id)){seen.add(node.id);branch.unshift(node);node=mapping[node.parent];}
 const user=[...branch].reverse().find(n=>n.message?.author?.role==='user');
 if(!user||hash(textOf(user.message))!==promptSha256)return null;
 const priorIds=String(baseline.beforeUserKey||'').match(/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}/gi)||[];
 if(!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(user.message.id||user.id))return null;
 if(Number(baseline.beforeUser)>0){
  // A repeated image prompt can be identical to the old request. Its text
  // alone is never delivery proof: require a different stable message ID.
  if(!priorIds.length||priorIds.includes(user.id)||priorIds.includes(user.message.id))return null;
 }else{
  // Zero DOM turns inside an existing saved chat may be an unloaded shell.
  // Only a genuinely new home-page composer permits this empty baseline.
  try{const previous=new URL(baseline.initialUrl);if(previous.origin!=='https://chatgpt.com'||previous.pathname!=='/')return null;}
  catch{return null;}
 }
 return {signal:'exact-source-user-prompt',userKey:'data-message-id:'+(user.message.id||user.id),
   userCount:branch.filter(n=>n.message?.author?.role==='user').length,
   assistantCount:branch.filter(n=>n.message?.author?.role==='assistant').length,
   source_proof:{conversation_id:cid,prompt_sha256:promptSha256,user_message_id:user.message.id||user.id}};
}
async function readConversationSubmissionSource({chatUrl,promptSha256,baseline,fetch,headers={},signal}){
 const cid=idFromUrl(chatUrl);if(!cid||!HASH.test(promptSha256||''))return null;
 const deadline=signal?AbortSignal.any([signal,AbortSignal.timeout(5_000)]):AbortSignal.timeout(5_000);
 const response=await fetch('https://chatgpt.com/backend-api/conversation/'+cid,{headers,signal:deadline,redirect:'error'});
 if(!response.ok){if(response.status===429)throw sourceHttpError(response);return null;}
 return conversationSubmissionProof(await response.json(),chatUrl,promptSha256,baseline);
}
module.exports={conversationImageProof,readConversationImageSource,conversationTextProof,readConversationTextSource,
 conversationSubmissionProof,readConversationSubmissionSource,sourceImageReceiptMatches,sourceHttpError,hash};
