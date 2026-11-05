"use strict";
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {createConversationLinkJournal}=require('../src/conversation-links.cjs');
test('conversation URLs survive pruning/restart, are deduplicated and contain no query secrets',()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'docurandom-links-')),file=path.join(dir,'links.jsonl');
 try{const url='https://chatgpt.com/c/6aceaf4f-44cc-83ec-a402-61fadf210822';
 const first=createConversationLinkJournal(file);assert.equal(first.record('thread:thread_workspace_one',url+'?secret=never-save'),true);
 assert.equal(first.record('thread:thread_workspace_one',url),false);
 assert.equal(createConversationLinkJournal(file).record('thread:thread_workspace_one',url),false);
 assert.equal(first.record('thread:thread_workspace_one','https://chatgpt.com/'),false);
 assert.equal(first.record('thread:thread_workspace_one','https://foreign.example/c/a'),false);
 const contents=fs.readFileSync(file,'utf8');assert.equal(contents.includes('secret'),false);assert.equal(contents.trim().split('\n').length,1);
 }finally{fs.rmSync(dir,{recursive:true,force:true});}
});
