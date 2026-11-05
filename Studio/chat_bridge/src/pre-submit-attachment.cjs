"use strict";

function preSubmitAttachmentProof(record, expected, snapshot, page, ui) {
  const file = record?.files?.[0];
  if (!record || !["selection-attempted", "confirmed"].includes(record.status)
      || record.files.length !== 1 || !expected || !file
      || expected.name !== file.source || expected.sha256 !== file.sha256
      || expected.size !== file.size || !/^[a-f0-9]{64}$/.test(file.sha256)
      || snapshot?.readyState !== "complete" || snapshot?.visibilityState !== "visible"
      || snapshot?.stopControlCount !== 0 || snapshot?.streamingMarkerCount !== 0
      || !Number.isSafeInteger(snapshot.userCount) || snapshot.userCount < 0
      || !Number.isSafeInteger(snapshot.assistantCount) || snapshot.assistantCount < 0
      || snapshot.accountLoadFailed || snapshot.conversationLoadFailed
      || page?.authenticated !== true || page?.ready !== true || page.rateLimited !== false
      || page.accountLoadFailed || page.conversationLoadFailed
      || ui?.visibleComposers !== 1 || ui.visibleCards !== 1
      || ui.storedNameVisible !== true || ui.storedNameInDraft !== false
      || !Number.isSafeInteger(ui.selectedFiles) || ui.selectedFiles < 0 || ui.selectedFiles > 1
      || !Number.isSafeInteger(ui.draftCharacterCount) || ui.draftCharacterCount > 1) return null;
  return JSON.stringify([snapshot.url, snapshot.userCount, snapshot.assistantCount,
    snapshot.users?.at(-1)?.key || "", snapshot.assistants?.at(-1)?.key || "",
    ui.selectedFiles, ui.visibleCards, ui.draftCharacterCount, file.sha256]);
}

module.exports = { preSubmitAttachmentProof };
