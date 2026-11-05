'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const {emptyPreSubmitAttachmentProof,reserveEmptyAttachmentRestage}=require('../src/pre-submit-restage.cjs');

function fixture(t){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'restage-proof-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const bytes=Buffer.alloc(70000,65),file=path.join(root,'master.txt');fs.writeFileSync(file,bytes);
 const sha=crypto.createHash('sha256').update(bytes).digest('hex');
 const record={status:'selection-attempted',chatUrl:'https://chatgpt.com/',updatedAt:123,
  files:[{source:'master.txt',path:file,sha256:sha,size:bytes.length}]};
 return {root,record,expected:{name:'master.txt',sha256:sha,size:bytes.length},
  snapshot:{url:record.chatUrl,readyState:'complete',visibilityState:'visible',userCount:0,assistantCount:0,stopControlCount:0,streamingMarkerCount:0},
  page:{url:record.chatUrl,authenticated:true,ready:true,rateLimited:false},
  ui:{visibleComposers:1,selectedFiles:0,visibleCards:0,storedNameVisible:false,storedNameInDraft:false,draftCharacterCount:1}};
}
test('empty first Master proof requires exact staged bytes and fully idle empty home',t=>{
 const f=fixture(t),proof=(s=f.snapshot,p=f.page,u=f.ui,r=f.record)=>emptyPreSubmitAttachmentProof(r,f.expected,s,p,u);
 assert.ok(proof());
 for(const s of [{...f.snapshot,userCount:1},{...f.snapshot,assistantCount:1},{...f.snapshot,streamingMarkerCount:1},
  {...f.snapshot,url:'https://chatgpt.com/c/6aceaf4f-44cc-83ec-a402-61fadf210822'},
  {...f.snapshot,conversationLoadFailed:true}])assert.equal(proof(s),null);
 assert.equal(proof(f.snapshot,{...f.page,rateLimited:true}),null);
 assert.equal(proof(f.snapshot,f.page,{...f.ui,visibleCards:1}),null);
 assert.equal(proof(f.snapshot,f.page,{...f.ui,draftCharacterCount:5}),null);
 fs.writeFileSync(f.record.files[0].path,Buffer.alloc(70000,66));assert.equal(proof(),null);
});
test('restage reservation cannot repeat across process restart or broken journal',t=>{
 const f=fixture(t),store=path.join(f.root,'journal.json'),identity='thread:thread_fixture_123456';
 const proof=emptyPreSubmitAttachmentProof(f.record,f.expected,f.snapshot,f.page,f.ui);
 const write=(p,v)=>fs.writeFileSync(p,JSON.stringify(v));
 assert.equal(reserveEmptyAttachmentRestage(store,identity,f.expected.sha256,proof,write),true);
 assert.equal(reserveEmptyAttachmentRestage(store,identity,f.expected.sha256,proof,write),false);
 fs.writeFileSync(store,'bad-json');assert.equal(reserveEmptyAttachmentRestage(store,identity,f.expected.sha256,proof,write),false);
});
