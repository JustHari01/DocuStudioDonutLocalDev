const test = require('node:test');
const assert = require('node:assert/strict');
const {preSubmitAttachmentProof} = require('../src/pre-submit-attachment.cjs');
const file = {source:'master.txt',sha256:'a'.repeat(64),size:400000};
const record = {status:'selection-attempted',files:[file]};
const expected = {name:file.source,sha256:file.sha256,size:file.size};
const snapshot = {url:'https://chatgpt.com/c/exact',readyState:'complete',visibilityState:'visible',stopControlCount:0,streamingMarkerCount:0,userCount:5,assistantCount:4,users:[{key:'u'}],assistants:[{key:'a'}]};
const page = {authenticated:true,ready:true,rateLimited:false};
const ui = {visibleComposers:1,visibleCards:1,storedNameVisible:true,storedNameInDraft:false,selectedFiles:0,draftCharacterCount:1};
test('reuse proof is stable and tied to the same staged master bytes',()=>{
  assert.ok(preSubmitAttachmentProof(record,expected,snapshot,page,ui));
  assert.equal(preSubmitAttachmentProof(record,{...expected,sha256:'b'.repeat(64)},snapshot,page,ui),null);
  assert.equal(preSubmitAttachmentProof(record,{...expected,size:1},snapshot,page,ui),null);
  assert.equal(preSubmitAttachmentProof(record,{...expected,name:'another.txt'},snapshot,page,ui),null);
});
test('incomplete, busy and quoted-card UI cannot authorize reuse',()=>{
  for(const change of [{readyState:'loading'},{visibilityState:'hidden'},{stopControlCount:1},{streamingMarkerCount:1},{conversationLoadFailed:true}])
    assert.equal(preSubmitAttachmentProof(record,expected,{...snapshot,...change},page,ui),null);
  assert.equal(preSubmitAttachmentProof(record,expected,snapshot,{...page,rateLimited:true},ui),null);
  for(const change of [{visibleCards:0},{visibleComposers:0},{storedNameVisible:false},{storedNameInDraft:true}])
    assert.equal(preSubmitAttachmentProof(record,expected,snapshot,page,{...ui,...change}),null);
});
