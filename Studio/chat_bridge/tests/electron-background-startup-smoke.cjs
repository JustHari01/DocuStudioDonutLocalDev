'use strict';
const {app,BrowserWindow}=require('electron');
const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const base=process.env.DOCURANDOM_BACKGROUND_TEST_SRC||path.join(__dirname,'../src');
const {prepareWindowForBackground}=require(path.join(base,'automation.cjs'));
const proof=process.argv[2];app.setPath('userData',path.join(path.dirname(proof),'isolated-visibility-fixture'));
app.on('window-all-closed',()=>{});
app.whenReady().then(async()=>{
 let w;
 try{
  w=new BrowserWindow({show:false,width:900,height:700,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  await w.loadURL('data:text/html,<html><body>Local renderer startup fixture</body></html>');
  const execute=w.webContents.executeJavaScript.bind(w.webContents);let probes=0;
  w.webContents.executeJavaScript=(...args)=>{probes++;return new Promise(resolve=>setTimeout(resolve,1200)).then(()=>execute(...args));};
  const t=Date.now();const state=await prepareWindowForBackground(w);
  assert.equal(state.visibilityState,'visible');assert.equal(state.hidden,false);assert.equal(probes,1);assert.ok(Date.now()-t>=1100);
  fs.writeFileSync(proof,JSON.stringify({pass:true,slowStartupProbeMs:1200,probes,visible:true,externalRequests:0,isolatedProfile:true},null,2));
 }catch(error){fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));process.exitCode=1;}
 finally{if(w&&!w.isDestroyed())w.destroy();app.quit();}
});
