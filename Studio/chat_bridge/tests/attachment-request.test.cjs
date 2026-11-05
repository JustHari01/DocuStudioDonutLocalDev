'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {requestAttachments}=require('../src/attachment-request.cjs');
const png=Buffer.from([137,80,78,71,13,10,26,10,0,0,0,0]);
const image={name:'canvas.png',mime_type:'image/png',content_base64:png.toString('base64')};
test('images are restricted to thumbnail image and confirmation modes',()=>{
 for(const mode of ['generated_image','thumbnail_confirmation'])assert.deepEqual(requestAttachments([image],mode),[image]);
 assert.throws(()=>requestAttachments([image],'text'),/txt or .md/);
 assert.throws(()=>requestAttachments([{...image,mime_type:'image/jpeg'}],'generated_image'),/MIME/);
 assert.throws(()=>requestAttachments([{...image,content_base64:Buffer.from('bad').toString('base64')}],'generated_image'),/MIME/);
});
test('text transport and byte/count limits remain enforced',()=>{
 const text={name:'master.txt',mime_type:'text/plain',content_base64:Buffer.from('master').toString('base64')};
 assert.deepEqual(requestAttachments([text]),[text]);
 assert.throws(()=>requestAttachments([image,image],'generated_image'),/one attachment/);
 assert.throws(()=>requestAttachments([image],'generated_image',4),e=>e.statusCode===413);
 assert.deepEqual(requestAttachments(undefined),[]);
});
