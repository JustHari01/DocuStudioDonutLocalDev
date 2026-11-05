"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const { ConnectionInterruptionTracker } = require('../src/connection-interruption.cjs');
const { waitForAnswer } = require('../src/automation.cjs');
const { replaceFailedIdleReader } = require('../src/renderer-recovery.cjs');

test('long High reasoning without a connection error has no new time limit', () => {
  const tracker = new ConnectionInterruptionTracker();
  assert.equal(tracker.update({interrupted:false,signature:'stop'}, 0), false);
  assert.equal(tracker.update({interrupted:false,signature:'stop'}, 86400000), false);
});
test('a stale interruption recovers, while actual answer progress resets its grace', () => {
  const tracker = new ConnectionInterruptionTracker({staleMs:120000});
  assert.equal(tracker.update({interrupted:true,signature:'a'}, 0), false);
  assert.equal(tracker.update({interrupted:true,signature:'ab'}, 119999), false);
  assert.equal(tracker.update({interrupted:true,signature:'ab'}, 239998), false);
  assert.equal(tracker.update({interrupted:true,signature:'ab'}, 239999), true);
  tracker.update({interrupted:false,signature:'ab'}, 240000);
  assert.equal(tracker.update({interrupted:true,signature:'ab'}, 250000), false);
});
test('an unrecoverable interrupted Stop yields with the accepted receipt intact', async () => {
  const receipt={assistantCount:1,submissionAccepted:true,userCount:1};
  const window={isDestroyed:()=>false,webContents:{getURL:()=> 'https://chatgpt.com/c/exact',
    executeJavaScript:async()=>({url:'https://chatgpt.com/c/exact',count:1,lastAssistantKey:'old',text:'',
      generating:true,stopControlCount:1,streamingMarkerCount:0,connectionInterrupted:true})}};
  await assert.rejects(waitForAnswer(window,1,{submissionEvidence:receipt,
    connectionInterruptionStaleMs:0,pollIntervalMs:1}), /CHATGPT_CONVERSATION_LOAD_FAILED/);
  assert.equal(receipt.submissionAccepted,true);
});
test('idle main reader replacement creates the new window before destroying the old one', () => {
  const events=[]; const fresh={};
  const old={isDestroyed:()=>false,destroy:()=>events.push('destroy')};
  assert.equal(replaceFailedIdleReader(old,{code:'CHATGPT_PAGE_PROBE_TIMEOUT'},
    {createWindow:()=>{events.push('create');return fresh;}}),fresh);
  assert.deepEqual(events,['create','destroy']);
  assert.equal(replaceFailedIdleReader(old,{code:'CHATGPT_PAGE_PROBE_TIMEOUT'},
    {protectActiveTurn:true,createWindow:()=>assert.fail('active owner replaced')}),old);
  assert.equal(replaceFailedIdleReader(old,new Error('rate limited'),
    {createWindow:()=>assert.fail('unrelated failure replaced')}),old);
  assert.equal(replaceFailedIdleReader(old,{code:'CHATGPT_PAGE_PROBE_IN_FLIGHT',pendingForMs:50},
    {createWindow:()=>assert.fail('fresh authentication probe replaced')}),old);
});
