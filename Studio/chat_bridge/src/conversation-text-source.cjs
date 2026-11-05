'use strict';
const crypto=require('node:crypto');
const hash=s=>crypto.createHash('sha256').update(String(s||'').normalize('NFC').replace(/\s+/gu,' ').trim()).digest('hex');
// Lexical's composer serializes an auto-linked bare URL as [URL](URL).
// This exact formatting equivalence cannot change either label or destination.
const plainAutoLinks=s=>String(s).replace(/\[(https:\/\/[^\s\[\]()]+)\]\(\1\)/gu,'$1');
const text=m=>(m?.content?.parts||[]).filter(p=>typeof p==='string').join('\n');
const id=url=>/^https:\/\/chatgpt\.com\/c\/([a-f0-9-]{36})(?:[?#].*)?$/.exec(String(url||''))?.[1]||'';
function conversationTextProof(source,chatUrl,promptSha256,expectedPrompt){
 const cid=id(chatUrl),mapping=source?.mapping;
 if(!cid||source?.conversation_id!==cid||!mapping||Array.isArray(mapping)||source.context_truncation_continuation||!/^[a-f0-9]{64}$/.test(promptSha256||''))return null;
 const branch=[],seen=new Set();let node=mapping[source.current_node];
 while(node&&!seen.has(node.id)){seen.add(node.id);branch.unshift(node);node=mapping[node.parent];}
 const expectedVerified=typeof expectedPrompt==='string'&&hash(expectedPrompt)===promptSha256;
 const requests=branch.filter(n=>n.message?.author?.role==='user'&&(hash(text(n.message))===promptSha256
   ||expectedVerified&&hash(plainAutoLinks(text(n.message)))===hash(plainAutoLinks(expectedPrompt))));
 if(requests.length!==1){
   let mismatch;
   if(typeof expectedPrompt==='string'&&hash(expectedPrompt)===promptSha256){
     const norm=s=>String(s).normalize('NFC').replace(/\s+/gu,' ').trim(),expected=norm(expectedPrompt);
     const candidates=branch.filter(n=>n.message?.author?.role==='user'&&norm(text(n.message)).startsWith(expected.slice(0,200)));
     if(candidates.length){const actual=norm(text(candidates.at(-1).message));let at=0;while(at<expected.length&&at<actual.length&&expected[at]===actual[at])at++;
       mismatch={first_difference:at,expected_length:expected.length,actual_length:actual.length,
         expected_context:expected.slice(Math.max(0,at-40),at+80),actual_context:actual.slice(Math.max(0,at-40),at+80)};}
   }
   return {status:'pending',reason:'exact-request-missing-or-ambiguous',conversation_id:cid,...(mismatch?{mismatch}:{}),
   observed_users:branch.filter(n=>n.message?.author?.role==='user').map(n=>({id:n.message.id||n.id,
     sha256:hash(text(n.message)),characters:text(n.message).length}))};
 }
 const user=requests[0],segment=[];
 for(const n of branch.slice(branch.indexOf(user)+1)){
  if(n.message?.author?.role==='user')break;segment.push(n);
 }
 const answers=segment.filter(n=>n.message?.author?.role==='assistant'&&n.message.status==='finished_successfully'
   &&(!n.message.channel||n.message.channel==='final')&&n.message.end_turn!==false
   &&(!n.message.recipient||n.message.recipient==='all')
   &&!n.message.metadata?.is_visually_hidden_from_conversation);
 if(answers.length!==1)return {status:'pending',reason:'exact-final-answer-not-unique',conversation_id:cid,user_message_id:user.message.id||user.id};
 const answer=answers[0],output=text(answer.message);
 // A provider can finish a tool turn with an empty final message. That is a
 // completed invalid response, not evidence that it is still generating.
 const invalid=!output.trim()||!['text',undefined].includes(answer.message.content?.content_type);
 if(invalid&&answer.message.end_turn!==true)return {status:'pending',reason:'empty-final-not-ended',conversation_id:cid};
 return {status:invalid?'completed-invalid':'completed',output_text:output,
   ...(invalid?{reason:'empty-or-non-text-final-response'}:{}),source_proof:{complete:true,conversation_id:cid,prompt_sha256:promptSha256,
   user_message_id:user.message.id||user.id,assistant_message_id:answer.message.id||answer.id,
   user_created_at:Number(user.message.create_time)||0,terminal_ended:answer.message.end_turn===true,
   user_attachment_present:(user.message.content?.parts||[]).some(p=>p&&typeof p==='object'
     &&(p.content_type==='image_asset_pointer'||p.asset_pointer)),
   assistant_text_sha256:hash(output),source_user_text_sha256:hash(text(user.message)),
   request_format:hash(text(user.message))===promptSha256?'exact':'identical-url-autolink',no_later_user_response_used:true}};
}
function consumedAttachmentMatches(proof,attachment,pending){
 return completedTextReceiptMatches(proof,pending)&&proof.user_attachment_present===true
   &&attachment?.status==='confirmed'&&Array.isArray(attachment.files)&&attachment.files.length>0
   &&Number.isFinite(attachment.updatedAt)&&attachment.updatedAt<=pending.createdAt;
}
function completedTextReceiptMatches(proof,pending){
 if(!proof?.terminal_ended||!pending||pending.outputMode!=='text'
   ||proof.prompt_sha256!==pending.promptSha256)return false;
 const key=value=>String(value||'').match(/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}/i)?.[0]||'';
 const accepted=key(pending.acceptedUserKey),before=key(pending.userKey);
 if(accepted)return proof.user_message_id===accepted;
 if(before)return proof.user_message_id!==before&&proof.user_created_at*1000>=pending.createdAt-10000;
 return pending.userCount===0&&proof.user_created_at*1000>=pending.createdAt-10000;
}
async function readConversationTextSource({chatUrl,promptSha256,expectedPrompt,fetch,headers={}}){
 const cid=id(chatUrl);if(!cid)return null;
 const response=await fetch('https://chatgpt.com/backend-api/conversation/'+cid,{headers:{...headers,'Cache-Control':'no-cache'},cache:'no-store',redirect:'error',signal:AbortSignal.timeout(12000)});
 if(!response.ok)throw Object.assign(Error('CHATGPT_TEXT_SOURCE_UNAVAILABLE: HTTP '+response.status),{statusCode:response.status});
 return conversationTextProof(await response.json(),chatUrl,promptSha256,expectedPrompt);
}
module.exports={conversationTextProof,readConversationTextSource,completedTextReceiptMatches,consumedAttachmentMatches,hash};
