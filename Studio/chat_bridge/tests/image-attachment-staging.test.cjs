'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {attachFiles}=require('../src/automation.cjs');
test('native file staging preserves PNG JPEG and WebP extensions and exact binary bytes',async()=>{
 for(const ext of ['png','jpg','webp']){
  const bytes=Buffer.from([137,80,78,71,13,10,26,10,11,12,13]);
  let attached=false,staged='';const states=[];
  const window={webContents:{debugger:{isAttached:()=>attached,attach:()=>{attached=true;},detach:()=>{attached=false;},
   sendCommand:async(command,payload)=>{
    if(command==='DOM.getDocument')return {root:{nodeId:1}};
    if(command==='DOM.querySelectorAll')return {nodeIds:[7]};
    if(command==='DOM.getAttributes')return {attributes:['type','file','multiple','']};
    if(command==='DOM.setFileInputFiles'){
     staged=payload.files[0];assert.equal(path.extname(staged),'.'+ext);assert.deepEqual(fs.readFileSync(staged),bytes);
    }
    return {};
   }},executeJavaScript:async()=>({attached:true,ready:true,alerts:[],visibleComposers:1})}};
  const cleanup=await attachFiles(window,[{name:'Canvas.'+ext,content_base64:bytes.toString('base64')}],{onAttachmentState:value=>states.push(value)});
  assert.match(path.basename(staged),new RegExp('Canvas-[a-f0-9]{10}\\.'+ext+'$'));
  assert.ok(states.some(s=>s?.status==='selection-attempted'&&s.files[0].target.endsWith('.'+ext)));
  cleanup();assert.equal(fs.existsSync(staged),false);
 }
});
