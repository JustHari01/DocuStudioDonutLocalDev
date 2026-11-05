"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const {isExplicitImageFailure} = require('../src/image-terminal.cjs');
const {conversationImageProof, readConversationImageSource, hash} = require('../src/conversation-image-source.cjs');
const {existingImageResult} = require('../src/image-retry.cjs');

const cid = '6ad18cca-7e08-83ec-8f72-86633ab60581';
const url = 'https://chatgpt.com/c/' + cid;
const prompt = '[DOCURANDOM_STAGE=thumbnail]\nCreate a new industrial thumbnail';
const digest = hash(prompt);
const failures = [
  'Xin lỗi, công cụ tạo ảnh hiện không thực hiện được yêu cầu trong lượt này.',
  'Không thể tạo ảnh trong lượt này vì công cụ tạo ảnh tiếp tục nhận nhầm yêu cầu tạo ảnh mới từ đầu thành chỉnh sửa ảnh có sẵn.',
  'Không thể tạo thumbnail trong lượt này vì công cụ tạo ảnh gặp lỗi khi xử lý yêu cầu tạo ảnh mới.',
  'KHÔNG TẠO ĐƯỢC: công cụ thiếu ảnh nguồn.',
  'Không tạo được ảnh trong lượt này.',
  'Image generation failed: initialization failed',
  'Công cụ tạo ảnh tiếp tục nhận nhầm yêu cầu tạo ảnh mới từ đầu thành chỉnh sửa ảnh có sẵn và không xuất được thumbnail. Đây là lỗi xử lý của công cụ, không phải do thiếu ảnh nguồn.',
];
const idle = {readyState:'complete', userCount:1, assistantCount:1, stopControlCount:0, streamingMarkerCount:0};
const image = text => ({candidateCount:0, latestAssistantAfterUser:true, assistantText:text});
function source(text) {
  return {conversation_id:cid, current_node:'a', mapping:{
    u:{id:'u', parent:null, message:{id:'u', author:{role:'user'}, content:{parts:[prompt]}}},
    a:{id:'a', parent:'u', message:{id:'a', author:{role:'assistant'}, channel:'final',
      status:'finished_successfully', end_turn:true, content:{parts:[text]}}},
  }};
}

test('observed DaiTa9/14 answers finish exact no-image source and DOM reads', async () => {
  for (const text of failures) {
    assert.equal(isExplicitImageFailure(text.normalize('NFD')), true);
    const s = source(text);
    const proof = conversationImageProof(s,url,digest);
    assert.equal(proof.terminal_no_image,true);
    assert.equal(proof.terminal_detail,text);
    const calls = [];
    const result = await readConversationImageSource({chatUrl:url,promptSha256:digest,
      fetch:async address => {calls.push(address);return {ok:true,json:async()=>s};}});
    assert.equal(result.status,'no-image');
    assert.deepEqual(calls,['https://chatgpt.com/backend-api/conversation/'+cid]);
    assert.equal(existingImageResult(idle,image(text)).status,'no-image');
  }
});

test('new failure wording still requires completed visible final and exact prompt', () => {
  for (const mutate of [
    s => {s.mapping.a.message.status='in_progress';},
    s => {s.mapping.a.message.end_turn=false;},
    s => {s.mapping.a.message.channel='analysis';},
    s => {s.mapping.a.message.metadata={is_visually_hidden_from_conversation:true};},
    s => {s.mapping.u.message.content.parts=['another video'];},
    s => {s.mapping.u2={id:'u2',parent:'a',message:{author:{role:'user'},content:{parts:[prompt]}}};s.current_node='u2';},
  ]) {
    const s=source(failures[0]);mutate(s);
    assert.notEqual(conversationImageProof(s,url,digest)?.terminal_no_image,true);
  }
  for (const change of [{stopControlCount:1},{streamingMarkerCount:1},{readyState:'loading'}])
    assert.equal(existingImageResult({...idle,...change},image(failures[0])).status,'pending');
  assert.equal(existingImageResult(idle,{...image(failures[0]),latestAssistantAfterUser:false}).status,'pending');
});

test('earlier generated image always prevents absence despite a later negative answer', () => {
  const s=source(failures[1]);
  s.mapping.prior={id:'prior',parent:'u',message:{id:'prior',author:{role:'assistant'},
    content:{parts:[{content_type:'image_asset_pointer',asset_pointer:'sediment://already-generated'}]}}};
  const proof=conversationImageProof(s,url,digest);
  assert.equal(proof.terminal_no_image,false);
  assert.deepEqual(proof.image_message_ids,['prior']);
  assert.equal(existingImageResult(idle,{...image(failures[1]),candidateCount:1}).status,'pending');
});

test('quoted warnings and non-image failures cannot retire an image request', () => {
  for (const text of ['Đã tạo ảnh. Trước đó báo KHÔNG TẠO ĐƯỢC.',
    'Nếu báo: Xin lỗi, công cụ tạo ảnh hiện không thực hiện được yêu cầu trong lượt này.',
    'Xin lỗi, công cụ tạo ảnh hiện đang thực hiện yêu cầu trong lượt này.',
    'Nếu báo Không thể tạo thumbnail, hãy đợi.', 'Không thể tạo kịch bản.',
    'Đang tạo ảnh, chưa hoàn tất.', 'Không thể tạo ảnh hưởng tới video.']) {
    assert.equal(isExplicitImageFailure(text),false,text);
    assert.equal(conversationImageProof(source(text),url,digest).terminal_no_image,false);
    assert.equal(existingImageResult(idle,image(text)).status,'pending');
  }
});
