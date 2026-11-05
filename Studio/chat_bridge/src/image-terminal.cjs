"use strict";

// Classify the assistant's own final answer, never a quote elsewhere in the
// page. Completion, exact request ownership and image absence are checked by
// the callers; this predicate alone never authorizes another generation.
function isExplicitImageFailure(text) {
  const answer = String(text || "").normalize("NFC").trim();
  return /^(?:KHÔNG TẠO ĐƯỢC(?=\s|[:.!…]|$)|Image generation failed\b|Không (?:tạo được|thể tạo(?: được)?)\s+(?:ảnh(?!\s+hưởng\b)|thumbnail)\b)/iu.test(answer)
    || /^Công cụ tạo ảnh\s[^\n]{0,500}?\bkhông\s+(?:thể\s+)?(?:tạo|xuất|trả)\s+(?:ra\s+)?(?:được\s+)?(?:thumbnail\b|ảnh(?!\s+hưởng\b))/iu.test(answer)
    || /^(?:Xin lỗi,\s*)?công cụ tạo ảnh (?:hiện )?không thực hiện được yêu cầu trong lượt này[.!]?$/iu.test(answer);
}

module.exports = { isExplicitImageFailure };
