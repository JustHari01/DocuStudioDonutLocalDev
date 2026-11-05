'use strict';
const path=require('node:path');
function requestAttachments(value,outputMode='',maxBytes=8*1024*1024){
 const fail=(message,statusCode=400)=>{throw Object.assign(Error(message),{statusCode});};
 if(value===undefined)return [];
 if(!Array.isArray(value)||value.length>1)fail('Only one attachment is supported');
 return value.map(item=>{
  if(!item||typeof item!=='object')fail('Invalid attachment');
  const name=typeof item.name==='string'?path.basename(item.name.trim()):'';
  const ext=path.extname(name).toLowerCase();
  const types={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'};
  const image=['generated_image','thumbnail_confirmation','verified_json'].includes(outputMode)&&Object.hasOwn(types,ext);
  if(!name||name.length>180||(!/\.(txt|md)$/i.test(name)&&!image))fail('Master prompt attachment must be a .txt or .md file');
  const mime=typeof item.mime_type==='string'&&item.mime_type.length<=100?item.mime_type:'text/plain';
  const encoded=typeof item.content_base64==='string'?item.content_base64:'';
  if(!encoded||!/^[A-Za-z0-9+/]*={0,2}$/.test(encoded))fail('Attachment data is invalid');
  const bytes=Buffer.from(encoded,'base64');
  if(!bytes.length||bytes.length>maxBytes)fail('Attachment exceeds the 8 MB safety limit',413);
  if(image){
   const detected=bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))?'image/png'
    :bytes[0]===255&&bytes[1]===216&&bytes[2]===255?'image/jpeg'
    :bytes.subarray(0,4).toString()==='RIFF'&&bytes.subarray(8,12).toString()==='WEBP'?'image/webp':'';
   if(mime!==types[ext]||detected!==mime)fail('Thumbnail image format or MIME does not match its bytes');
  }
  return {name,mime_type:mime,content_base64:bytes.toString('base64')};
 });
}
module.exports={requestAttachments};
