const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {createRequire}=require('node:module');
const {sourceImageReceiptMatches}=require('../src/conversation-image-source.cjs');
const mainPath=path.join(__dirname,'../src/main.cjs'),src=fs.readFileSync(mainPath,'utf8');
const start=src.indexOf('if (request.method === "POST" && url.pathname === "/v1/session/existing-image")');
const end=src.indexOf('if (request.method === "POST" && url.pathname === "/v1/session/repair-conversation")',start);
const route=src.slice(start,end),digest='a'.repeat(64),cid='6aceaf4f-44cc-83ec-a402-61fadf210822';
async function run(source,{busy=false,sourceOnly=false}={}){
 const saved={chatUrl:'https://chatgpt.com/c/'+cid,pendingSubmission:{outputMode:'generated_image',promptSha256:digest,createdAt:1000}};
 const savedTurns=new Map([['thread:video',saved]]);let result,reads=0,dom=0;
 const scope={request:{method:'POST'},url:{pathname:'/v1/session/existing-image'},response:{},
  readJson:async()=>({thread_id:'video',image_prompt_sha256:digest,source_only:sourceOnly}),
  normalizeTurnIdentity:id=>'thread:'+id,savedTurns,isExactChatConversationUrl:()=>true,
  turnConversations:new Map(),turnControllers:new Map(busy?[['thread:video',{}]]:[]),existingImageReaders:new Set(),
  contextWindowUsable:()=>false,readConversationImageSource:async()=>{reads++;return source;},
  session:{fromPartition:()=>({fetch:()=>{throw Error('unexpected live request');}})},PARTITION:'test',conversationReadHeaders:{},
  retiredImageReceipts:new Map(),persistState:()=>{},saveTurnContext:()=>{},sendJson:(_r,status,data)=>{result={status,data};},
  createConversation:async()=>{dom++;throw Error('The SPA is unavailable');},mainWindow:null,
  require:createRequire(mainPath)};
 await vm.runInNewContext('(async()=>{'+route+'})()',scope);
 return {result,reads,dom,savedTurns};
}
test('exact owned image is read before any failed SPA navigation',async()=>{
 const source={status:'ready',source_proof:{complete:true,prompt_sha256:digest},generated_image:{data_base64:'eA=='}};
 const r=await run(source);assert.equal(r.result.status,200);assert.equal(r.result.data,source);
 assert.equal(r.dom,0);assert.equal(r.reads,1);assert.equal(r.savedTurns.get('thread:video').pendingSubmission,null);
});
test('one source-only migration read never opens DOM or clears an uncertain receipt',async()=>{
 const r=await run({status:'pending'},{sourceOnly:true});assert.equal(r.dom,0);assert.equal(r.result.data.status,'pending');
 assert.ok(r.savedTurns.get('thread:video').pendingSubmission);
});
test('source collector cannot steal a live submission owner',async()=>{
 const r=await run({status:'ready'},{busy:true});assert.equal(r.result.status,409);assert.equal(r.reads,0);assert.equal(r.dom,0);
});
test('old identical negative does not retire a newer unknown image send',()=>{
 const old='11111111-1111-4111-8111-111111111111',fresh='22222222-2222-4222-8222-222222222222';
 const proof={complete:true,prompt_sha256:digest,terminal_no_image:true,latest_request_user_id:old};
 const pending={outputMode:'generated_image',promptSha256:digest,userKey:'data-message-id:'+old};
 assert.equal(sourceImageReceiptMatches(proof,pending,'no-image'),false);
 assert.equal(sourceImageReceiptMatches({...proof,latest_request_user_id:fresh},pending,'no-image'),true);
 assert.equal(sourceImageReceiptMatches({...proof,latest_request_user_id:fresh},{...pending,acceptedUserKey:'data-message-id:'+old},'no-image'),false);
});
