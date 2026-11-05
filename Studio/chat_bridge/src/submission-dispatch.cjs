"use strict";
// Transport observation is not a model response. It forbids proving absence
// from DOM alone once a real provider request has been dispatched.
function providerDispatchFields(value) {
  const at=Number(value?.providerDispatchObservedAt);
  if(!Number.isFinite(at) || at<=0)return {};
  return {providerDispatchObservedAt:at,
    providerRequestId:String(value.providerRequestId||'').slice(0,80),
    providerStatusCode:Math.min(599,Math.max(0,Number(value.providerStatusCode)||0)),
    providerError:/^net::ERR_[A-Z0-9_]+$/.test(value.providerError||'')?value.providerError:''};
}
function noteProviderDispatch(pending,details,{now=Date.now()}={}) {
  if(!pending?.sendAttempted || !pending.createdAt || now<pending.createdAt || details?.method!=='POST')return false;
  let url;try{url=new URL(details.url);}catch{return false;}
  if(!['https://chatgpt.com','https://chat.openai.com'].includes(url.origin)
    || !/^\/backend-api\/(?:f\/)?conversation\/?$/.test(url.pathname))return false;
  const id=String(details.id||'');
  if(pending.providerRequestId && pending.providerRequestId!==id)return false;
  Object.assign(pending,{providerDispatchObservedAt:pending.providerDispatchObservedAt||now,
    providerRequestId:id,providerStatusCode:Number(details.statusCode)||pending.providerStatusCode||0,
    providerError:/^net::ERR_[A-Z0-9_]+$/.test(details.error||'')?details.error:(pending.providerError||'')});
  return true;
}
function uncertainDispatchError(evidence) {
  const f=providerDispatchFields(evidence);
  if(!f.providerDispatchObservedAt)return null;
  return Object.assign(new Error(`CHATGPT_SUBMISSION_TRANSPORT_UNCERTAIN: Yêu cầu đã phát tới ChatGPT; HTTP=${f.providerStatusCode||'pending'}${f.providerError?';net='+f.providerError:''}; chưa xác minh kết quả máy chủ, giữ checkpoint, không gửi lại prompt.`),{code:'CHATGPT_SUBMISSION_TRANSPORT_UNCERTAIN'});
}
module.exports={providerDispatchFields,noteProviderDispatch,uncertainDispatchError};
