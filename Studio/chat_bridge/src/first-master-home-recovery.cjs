"use strict";
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {masterPromptCaption}=require('./prompt-transport.cjs');
const {homeConversationProof}=require('./home-conversation-proof.cjs');
const {conversationId}=require('./conversation-health.cjs');

function firstMasterProofHash(record,expected,stagingRoot){
  const file=record?.files?.length===1?record.files[0]:null;
  if(!file || !expected || file.source!==expected.name || file.size!==expected.size
    || file.sha256!==expected.sha256 || !/^[a-f0-9]{64}$/.test(file.sha256||'')
    || !Number.isSafeInteger(file.size) || file.size<64000 || file.size>8*1024*1024
    || file.source!==`DocuRandom-Master-Prompt-${file.sha256.slice(0,12)}.txt`
    || !new RegExp(`^DocuRandom-Master-Prompt-${file.sha256.slice(0,12)}-[a-f0-9]{10,32}\\.txt$`).test(file.target||''))return null;
  if(typeof stagingRoot!=='string' || typeof file.path!=='string')return null;
  let bytes;
  try{
    const root=fs.realpathSync(stagingRoot)+path.sep;
    const resolved=fs.realpathSync(file.path);
    if(!resolved.toLowerCase().startsWith(root.toLowerCase()) || path.basename(file.path)!==file.target)return null;
    bytes=fs.readFileSync(resolved);
  }catch{return null;}
  if(bytes.length!==file.size || crypto.createHash('sha256').update(bytes).digest('hex')!==file.sha256)return null;
  const chars=bytes.toString('utf8').length;
  if(chars<64000 || chars>8*1024*1024)return null;
  return crypto.createHash('sha256').update(masterPromptCaption(file.target,chars,file.sha256)
    .normalize('NFC').replace(/\s+/g,' ').trim()).digest('hex');
}

async function recoverFirstMasterHome(context,{pending,attachment,expected,stagingRoot,
  usedUrls,inventory,open,probe,discard,pause=ms=>new Promise(resolve=>setTimeout(resolve,ms)),onStatus}={}){
  if(!context || context.busy || conversationId(context.chatUrl) || pending?.submissionAccepted!==true
    || pending.assistantCount!==0 || pending.userCount!==0 || !(usedUrls instanceof Set))return null;
  const sha=firstMasterProofHash(attachment,expected,stagingRoot);
  if(!sha)return null;
  context.busy=true;
  try{
    let snapshot;
    try{snapshot=await inventory();}catch{return null;}
    const ownedIds=new Set([...usedUrls].map(conversationId).filter(Boolean));
    const candidates=[...new Set((snapshot?.recentConversationLinks||[]).map(x=>x?.url))]
      .filter(url=>conversationId(url) && !ownedIds.has(conversationId(url))).slice(0,3);
    for(const url of candidates){
      let reader=null;
      try{
        onStatus?.('recovering-first-url','Đang dò đúng chat đã nạp Master bằng mã tệp riêng; không gửi lại Master.');
        reader=await open(url);
        const options={url,promptSha256:sha};
        const first=await probe(reader),a=homeConversationProof(first.snapshot,first.page,options);
        if(!a)continue;
        await pause(1500);
        const second=await probe(reader);
        if(a!==homeConversationProof(second.snapshot,second.page,options))continue;
        const found=reader;reader=null;return found;
      }catch{/* Missing/partial history is not permission to send or change a binding. */}
      finally{if(reader){try{discard(reader);}catch{}}}
    }
  }finally{context.busy=false;}
  return null;
}
module.exports={firstMasterProofHash,recoverFirstMasterHome};
