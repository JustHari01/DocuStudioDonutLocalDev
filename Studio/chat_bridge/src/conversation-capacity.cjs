'use strict';
function conversationCapacityState(text,actionTexts=[]){
 const body=String(text||'').replace(/\s+/g,' ');
 const full=/(?:you[’']ve reached the maximum length for this conversation|conversation has reached (?:its|the) maximum length|đã đạt (?:độ dài|giới hạn độ dài) tối đa (?:của|cho) (?:cuộc )?hội thoại)/i.test(body);
 const action=actionTexts.some(v=>/^(?:start (?:a )?new chat|bắt đầu (?:cuộc )?(?:trò chuyện|chat) mới)$/i.test(String(v||'').trim()));
 return {conversationFull:full&&action};
}
function conversationCapacityError(){
 return Object.assign(Error('CHATGPT_CONVERSATION_FULL: Hội thoại đã đạt độ dài tối đa; giữ checkpoint và nguyên liệu, cần tiếp tục bằng lượt phục hồi có giới hạn.'),{code:'CHATGPT_CONVERSATION_FULL'});
}
module.exports={conversationCapacityState,conversationCapacityError};
