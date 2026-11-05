'use strict';
const fs=require('node:fs');
const {URL}=require('node:url');

function emptyPreSubmitAttachmentProof(record,expected,snapshot,page,ui){
 const file=record?.files?.length===1?record.files[0]:null;
 const home=value=>{try{const u=new URL(value);return u.origin==='https://chatgpt.com'&&u.pathname==='/';}catch{return false;}};
 if(!file||!expected||record.status!=='selection-attempted'||!home(record.chatUrl)
   ||!home(snapshot?.url)||!home(page?.url)||file.source!==expected.name
   ||file.size!==expected.size||file.sha256!==expected.sha256||!/^[a-f0-9]{64}$/.test(file.sha256||'')
   ||snapshot?.readyState!=='complete'||snapshot?.visibilityState!=='visible'
   ||snapshot.userCount!==0||snapshot.assistantCount!==0||snapshot.stopControlCount!==0||snapshot.streamingMarkerCount!==0
   ||snapshot.accountLoadFailed||snapshot.conversationLoadFailed||snapshot.connectionInterrupted
   ||page?.authenticated!==true||page.ready!==true||page.rateLimited!==false
   ||page.accountLoadFailed||page.conversationLoadFailed||page.connectionInterrupted
   ||ui?.visibleComposers!==1||ui.selectedFiles!==0||ui.visibleCards!==0
   ||ui.storedNameVisible!==false||ui.storedNameInDraft!==false
   ||!Number.isSafeInteger(ui.draftCharacterCount)||ui.draftCharacterCount>1)return null;
 let bytes;try{bytes=fs.readFileSync(file.path);}catch{return null;}
 const hash=require('node:crypto').createHash('sha256').update(bytes).digest('hex');
 if(bytes.length!==file.size||hash!==file.sha256||file.size<64000||file.size>8*1024*1024)return null;
 return JSON.stringify([snapshot.url,record.updatedAt,file.sha256,file.size,'empty-pre-submit-attachment']);
}

function reserveEmptyAttachmentRestage(store,identity,sha,proof,atomicWrite,now=Date.now){
 if(!/^thread:[A-Za-z0-9_-]{8,160}$/.test(identity)||!/^[a-f0-9]{64}$/.test(sha)||typeof proof!=='string'||!proof)return false;
 let state={};try{state=JSON.parse(fs.readFileSync(store,'utf8'));}catch(error){if(error.code!=='ENOENT')return false;}
 if(!state||typeof state!=='object'||Array.isArray(state))return false;
 const key=identity+':'+sha;
 if(state[key])return false;
 state[key]={count:1,at:now(),proofSha256:require('node:crypto').createHash('sha256').update(proof).digest('hex')};
 atomicWrite(store,state);return true;
}
module.exports={emptyPreSubmitAttachmentProof,reserveEmptyAttachmentRestage};
