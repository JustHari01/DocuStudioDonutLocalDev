"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {permitted,idle}=require('../src/image-history-view.cjs');
test('image history actions allow only display controls, never send or generation retry',()=>{
  assert.equal(permitted('expand','Show more'),true);
  assert.equal(permitted('canvas','Canvas view'),true);
  assert.equal(permitted('previous','Previous response'),true);
  assert.equal(permitted('image','View image'),true);
  for(const label of ['Try again','Retry','Send','Create image','Delete','Edit image','Show more then Send']){
    for(const action of ['expand','canvas','previous','image'])assert.equal(permitted(action,label),false);
  }
});
test('view recovery excludes streaming, failed and incomplete renderers',()=>{
  const ready={readyState:'complete',stopControlCount:0,streamingMarkerCount:0};
  assert.equal(idle(ready),true);
  for(const changes of [{stopControlCount:1},{streamingMarkerCount:1},{conversationLoadFailed:true},{accountLoadFailed:true},{readyState:'loading'}]){
    assert.equal(idle({...ready,...changes}),false);
  }
});
