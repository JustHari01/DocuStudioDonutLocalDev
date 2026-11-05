"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");
const { generatedImageSnapshot, chatTurnSnapshot } = require("../src/automation.cjs");
const { existingImageResult } = require("../src/image-retry.cjs");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "DocuRandomImageSmoke-")));
app.commandLine.appendSwitch("disable-gpu-shader-disk-cache");
async function main() {
  await app.whenReady();
  const window = new BrowserWindow({show:false, width:900, height:700,
    webPreferences:{sandbox:true, nodeIntegration:false, contextIsolation:true, backgroundThrottling:false}});
  try {
    await window.loadURL("data:text/html," + encodeURIComponent('<main></main>'));
    await window.webContents.executeJavaScript(`(async () => {
      const canvas = document.createElement('canvas'); canvas.width=1280; canvas.height=720;
      const ctx=canvas.getContext('2d'); ctx.fillStyle='red'; ctx.fillRect(0,0,1280,720);
      const old=canvas.toDataURL('image/png');
      ctx.fillStyle='blue'; ctx.fillRect(0,0,1280,720); const fresh=canvas.toDataURL('image/png');
      const user=(id, text)=>'<div data-chatgpt-search-message-ids="'+id+'"><div class="group/user-message">'+text+'</div></div>';
      document.querySelector('main').innerHTML=user('u1','Create image')
        +'<div data-chatgpt-selection-message-id="a1"><div class="MarkdownRoot-fixture">KHÔNG TẠO ĐƯỢC</div><img id="old-image" width="320" height="180" src="'+old+'"></div>'
        +user('u2','Try again <img id="attachment" width="320" height="180" src="'+old+'">')
        +'<div role="progressbar" style="display:none">stale hidden activity</div>'
        +'<section id="image-card"><img id="new-image" width="320" height="180" src="'+fresh+'"><button>Edit</button></section>';
      await Promise.all([...document.images].map(img=>img.decode()));
    })()`);
    let image = await generatedImageSnapshot(window);
    let snapshot = await chatTurnSnapshot(window);
    assert.equal(snapshot.userCount,2);
    assert.equal(snapshot.assistantCount,1); // The new card has no text/wrapper.
    assert.equal(image.afterLatestUser,true);
    assert.equal(image.candidateCount,1);
    assert.equal(image.busy,false);
    assert.equal(existingImageResult(snapshot,image).status,"ready");
    const newSource = await window.webContents.executeJavaScript("document.querySelector('#new-image').src.split(',')[1]");
    assert.equal(image.dataBase64,newSource);
    await window.webContents.executeJavaScript("document.querySelector('#new-image').remove()");
    image=await generatedImageSnapshot(window);
    assert.equal(image.candidateCount,0);
    assert.equal(image.latestAssistantAfterUser,false);
    assert.equal(existingImageResult(snapshot,image).status,"pending", "the earlier failed turn must not authorize a resend");
    await window.webContents.executeJavaScript(`(() => {
      const card=document.querySelector('#image-card');
      card.setAttribute('data-chatgpt-selection-message-id','image-only-assistant');
      const img=document.createElement('img'); img.src=document.querySelector('#old-image').src; img.width=320;img.height=180;card.append(img);
    })()`);
    await window.webContents.executeJavaScript("document.querySelector('#image-card img').decode()");
    snapshot=await chatTurnSnapshot(window);
    assert.equal(snapshot.assistantCount,2, "modern image-only assistant wrappers count as turns");
    image=await generatedImageSnapshot(window);
    assert.equal(existingImageResult(snapshot,image).status,"ready");
    await window.webContents.executeJavaScript("document.querySelector('[role=progressbar]').style.display='block'");
    image=await generatedImageSnapshot(window);
    assert.equal(existingImageResult(snapshot,image).status,"pending", "visible activity must keep the reader waiting");
    console.log("Image-only, sibling-card, source-attachment, stale-answer and progress DOM smoke passed.");
  } finally { window.destroy(); }
}
main().then(()=>app.quit()).catch(error=>{console.error(error);app.exit(1);});
