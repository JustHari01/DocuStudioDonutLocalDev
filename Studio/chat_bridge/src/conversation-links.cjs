"use strict";
const fs=require('node:fs'),path=require('node:path');
function canonical(value){try{const u=new URL(value);return u.protocol==='https:'&&u.hostname==='chatgpt.com'
 &&/^\/c\/[a-f0-9-]{36}$/.test(u.pathname)?u.origin+u.pathname:'';}catch{return '';}}
function createConversationLinkJournal(file){
 const known=new Set();
 try{for(const line of fs.readFileSync(file,'utf8').split('\n')){try{const row=JSON.parse(line);if(canonical(row.url))known.add(row.thread_id+'|'+row.url);}catch{}}}catch(error){if(error.code!=='ENOENT')throw error;}
 return {record(identity,value){const url=canonical(value),thread=String(identity||'').replace(/^thread:/,'');
  if(!url||!/^[A-Za-z0-9_-]{8,160}$/.test(thread))return false;
  const key=thread+'|'+url;if(known.has(key))return false;
  fs.mkdirSync(path.dirname(file),{recursive:true});
  fs.appendFileSync(file,JSON.stringify({version:1,thread_id:thread,url,recorded_at:new Date().toISOString()})+'\n',{encoding:'utf8',mode:0o600});
  known.add(key);return true;
 }};
}
module.exports={createConversationLinkJournal,canonical};
