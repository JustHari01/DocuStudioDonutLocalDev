/* Google web tasks run only through the authenticated, profile-bound bridge. */
globalThis.GoogleWorkspace = (() => {
  const hosts = {
    search: ['www.google.com'], maps: ['www.google.com'], youtube: ['www.youtube.com'],
    gmail: ['mail.google.com'], drive: ['drive.google.com'], docs: ['docs.google.com'],
    sheets: ['docs.google.com'], slides: ['docs.google.com'], forms: ['docs.google.com'],
    calendar: ['calendar.google.com'], keep: ['keep.google.com'], photos: ['photos.google.com'],
    translate: ['translate.google.com'], news: ['news.google.com'], books: ['books.google.com'],
    earth: ['earth.google.com'], finance: ['www.google.com'], contacts: ['contacts.google.com'],
    tasks: ['tasks.google.com'], meet: ['meet.google.com'], chat: ['chat.google.com'],
    sites: ['sites.google.com'], groups: ['groups.google.com'], blogger: ['www.blogger.com'],
    classroom: ['classroom.google.com'], arts: ['artsandculture.google.com'],
  };
  function allowed(app, raw) {
    let u;try {u=new URL(raw);}catch{return false;}
    if (u.protocol !== 'https:' || u.username || u.password || u.port || !hosts[app]?.includes(u.hostname)) return false;
    if (['docs','sheets','slides','forms'].includes(app)) {
      const prefix = {docs:'document',sheets:'spreadsheets',slides:'presentation',forms:'forms'}[app];
      return u.pathname.startsWith('/'+prefix+'/');
    }
    if (app === 'maps') return u.pathname.startsWith('/maps');
    if (app === 'finance') return u.pathname.startsWith('/finance');
    if (app === 'search') return ['/', '/search', '/webhp'].includes(u.pathname);
    return true;
  }
  function urlState(raw) {
    let u;try {u=new URL(raw);}catch{return 'network_error';}
    if(u.protocol==='chrome-error:')return 'network_error';
    if(u.hostname==='consent.google.com')return 'permission_required';
    if(u.hostname==='accounts.google.com') {
      if(/consent|oauth\/.*approval/i.test(u.pathname))return 'permission_required';
      return /accountchooser/i.test(u.pathname)?'account_required':'auth_required';
    }
    if((u.hostname==='www.google.com' && /^\/sorry(?:\/|$)/.test(u.pathname)))return 'verification_required';
    return '';
  }
  function pageError(state) {
    const error=new Error('GOOGLE_'+state.toUpperCase());error.page_status=state;return error;
  }
  function targetKey(app,raw) {
    const u=new URL(raw);
    const file=u.pathname.match(/^\/(document|spreadsheets|presentation|forms)\/(?:u\/\d+\/)?d\/([\w-]+)/);
    if(file)return u.origin+'/'+file[1]+'/d/'+file[2];
    if(app==='drive')return u.origin+u.pathname.replace(/\/$/,'');
    return null;
  }
  function sameOwnedPage(owner, raw) {
    const expected=owner.url || owner.requested_url;
    if(!expected)return false;
    const file=targetKey(owner.app,expected);
    return file ? file===targetKey(owner.app,raw) :
      new URL(expected).href.split('#')[0]===new URL(raw).href.split('#')[0];
  }
  // Self-contained: this function is serialized into the selected tab.
  async function inPage(action, params) {
    const visible = e => Boolean(e?.isConnected && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
    const all = s => Array.from(document.querySelectorAll(s));
    const text = e => String(e?.innerText || e?.textContent || '').replace(/\s+/g,' ').trim();
    const labels = e => String(e.getAttribute('aria-label') || e.getAttribute('title') || text(e)).trim();
    let blocked='';
    if(location.hostname==='consent.google.com')blocked='permission_required';
    else if(location.hostname==='accounts.google.com')blocked=/accountchooser/i.test(location.pathname)?'account_required':'auth_required';
    else if(location.hostname==='www.google.com' && /^\/sorry(?:\/|$)/.test(location.pathname))blocked='verification_required';
    let httpStatus=0;
    try {httpStatus=Number(performance.getEntriesByType('navigation')[0]?.responseStatus)||0;}catch{}
    if(!blocked && httpStatus===429)blocked='rate_limited';
    if(!blocked && httpStatus>=500)blocked='service_unavailable';
    if(!blocked && httpStatus===401)blocked='auth_required';
    if(!blocked && httpStatus===403)blocked='permission_required';
    if(!blocked && /^\/(document|spreadsheets|presentation)\/(?:u\/\d+\/)?d\//.test(location.pathname) &&
       !document.querySelector('.docs-title-input') && /you need access|bạn cần quyền truy cập|アクセス権が必要/i.test(document.body?.innerText||''))blocked='permission_required';
    if(blocked)return {status:blocked,page_status:blocked,http_status:httpStatus,url:location.href};
    if (action === 'snapshot') return {
      status:'ok',url:location.href,title:document.title,ready:document.readyState,
      text: String(document.body?.innerText || '').slice(0, 6000),
      links:all('a[href]').filter(visible).slice(0,60).map(e=>({text:labels(e).slice(0,180),url:e.href})),
      fields:all('input:not([type=password]):not([type=hidden]),textarea,[contenteditable=true]')
        .filter(visible).slice(0,25).map(e=>({label:labels(e).slice(0,100),tag:e.tagName,role:e.getAttribute('role')})),
      editorTitle:document.querySelector('.docs-title-input')?.value || '',
      items:all('[role=row][data-id]').filter(visible).slice(0,150).map(e=>({id:e.getAttribute('data-id'),text:text(e).slice(0,400)})),
      upload_complete:/upload(?:s)?\s+complete|upload completed|đã tải|tải lên hoàn tất|アップロード.{0,20}完了/i.test(document.body?.innerText || ''),
      upload_in_progress:all('[role=progressbar]').filter(visible).some(e=>{
        const value=Number(e.getAttribute('aria-valuenow')),max=Number(e.getAttribute('aria-valuemax')||100);
        return !e.hasAttribute('aria-valuenow') || value<max;
      }),
    };
    if (action === 'scroll') {
      const area = all('[role=main],main').find(e=>visible(e) && e.scrollHeight>e.clientHeight+100) || document.scrollingElement;
      if (!area) throw new Error('GOOGLE_SCROLL_AREA_NOT_READY');
      const before=area.scrollTop;
      area.scrollBy({top:Math.min(1200,Math.max(100,Number(params.pixels)||600)),behavior:'instant'});
      return {status:'ok',before,after:area.scrollTop,url:location.href};
    }
    if (action === 'set_title') {
      const editor=all('.docs-title-input,input[aria-label="Document title"],input[aria-label="Tên tài liệu"]')
        .filter(visible);
      if (editor.length !== 1) throw new Error('GOOGLE_TITLE_EDITOR_NOT_UNIQUE');
      const e=editor[0]; e.focus(); e.select();
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,params.title);
      e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));e.blur();
      return {status:'ok',title:e.value,url:location.href};
    }
    if (action === 'focus_editor') {
      if (location.pathname.startsWith('/spreadsheets/')) {
        const box=document.querySelector('#t-name-box');if(!box)throw new Error('GOOGLE_SHEET_CELL_BOX_MISSING');
        box.focus();box.value='A1';box.dispatchEvent(new Event('input',{bubbles:true}));
        return {status:'ok',focus:'cell_address'};
      }
      const iframe=document.querySelector('iframe.docs-texteventtarget-iframe');
      const frame=iframe?.contentDocument;
      const candidates=Array.from(frame?.querySelectorAll('[role="textbox"][contenteditable="true"],textarea')||[]);
      const editor=candidates.length===1?candidates[0]:candidates.length===0&&frame?.body?.isContentEditable?frame.body:null;
      if(!editor)throw new Error('GOOGLE_DOC_EDITOR_MISSING');
      iframe.focus();editor.focus();return {status:'ok',focus:'document',
        focused:document.activeElement===iframe&&frame.activeElement===editor,
        editable:editor.isContentEditable||editor.tagName==='TEXTAREA'};
    }
    if (action === 'verify_content') {
      const match=location.pathname.match(/^\/(document|spreadsheets)\/(?:u\/\d+\/)?d\/([\w-]+)/);
      if(!match)throw new Error('GOOGLE_EXACT_DOCUMENT_REQUIRED');
      const format=match[1]==='document'?'txt':'csv';
      const response=await fetch('/'+match[1]+'/d/'+match[2]+'/export?format='+format,{credentials:'include',cache:'no-store'});
      if(!response.ok)return {matched:false,http_status:response.status,
        retry_after:response.headers.get('retry-after')};
      if(/text\/html/i.test(response.headers.get('content-type')||''))return {matched:false};
      const content=await response.text();
      if(format==='txt')return {matched:content.replace(/\s+/g,' ').includes(String(params.text).replace(/\s+/g,' '))};
      const rows=[];let row=[],value='',quoted=false;
      for(let i=0;i<content.length;i++) {
        const ch=content[i];
        if(ch==='"') {if(quoted && content[i+1]==='"'){value+='"';i++;}else quoted=!quoted;}
        else if(ch===','&&!quoted){row.push(value);value='';}
        else if(ch==='\n'&&!quoted){row.push(value);rows.push(row);row=[];value='';}
        else if(ch!=='\r')value+=ch;
      }
      if(value||row.length){row.push(value);rows.push(row);}
      return {matched:params.cells.every((r,y)=>r.every((v,x)=>rows[y]?.[x]===v))};
    }
    if (action === 'set_field') {
      if (typeof params.selector !== 'string' || params.selector.length > 500) throw new Error('GOOGLE_FIELD_SELECTOR_REQUIRED');
      const found=all(params.selector).filter(visible);
      if (found.length !== 1 || found[0].type === 'password') throw new Error('GOOGLE_FIELD_NOT_UNIQUE');
      const e=found[0];
      if (!['INPUT','TEXTAREA'].includes(e.tagName) && !e.isContentEditable) throw new Error('GOOGLE_FIELD_NOT_EDITABLE');
      e.focus();
      if (e.isContentEditable) { document.execCommand('selectAll',false);document.execCommand('insertText',false,params.text); }
      else { const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto,'value').set.call(e,params.text);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true})); }
      return {status:'ok',url:location.href};
    }
    if (action === 'open_link') {
      const u=new URL(params.url,location.href);
      if (u.origin !== location.origin || u.protocol !== 'https:') throw new Error('GOOGLE_LINK_OUTSIDE_APP');
      const links=all('a[href]').filter(e=>visible(e) && e.href===u.href);
      if (links.length!==1) throw new Error('GOOGLE_LINK_NOT_UNIQUE');
      links[0].click();return {status:'ok',url:u.href};
    }
    throw new Error('GOOGLE_ACTION_UNSUPPORTED');
  }
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  let receiptWrites=Promise.resolve();
  function writeReceipt(id,fields) {
    const pending=receiptWrites.then(async()=>{
      const receipts=(await chrome.storage.local.get('googleTaskReceipts')).googleTaskReceipts||{};
      const row={...receipts[id],...fields};receipts[id]=row;
      const keys=Object.keys(receipts);
      for(const key of keys.slice(0,Math.max(0,keys.length-500)))if(receipts[key].status==='complete')delete receipts[key];
      await chrome.storage.local.set({googleTaskReceipts:receipts});return row;
    });receiptWrites=pending.catch(()=>{});return pending;
  }
  let creatingSession=null;
  async function browserSession() {
    const value=await chrome.storage.session.get('googleBrowserSession');
    if(value.googleBrowserSession)return value.googleBrowserSession;
    if(!creatingSession)creatingSession=(async()=>{
      const id=crypto.randomUUID();await chrome.storage.session.set({googleBrowserSession:id});return id;
    })().finally(()=>{creatingSession=null;});
    return await creatingSession;
  }
  async function tabFor(params) {
    if (!Number.isInteger(params.tab_id) || params.tab_id<1) throw new Error('GOOGLE_EXPLICIT_TAB_REQUIRED');
    const receipts=(await chrome.storage.local.get('googleTaskReceipts')).googleTaskReceipts || {};
    const owner=receipts[params.owner_request_id];
    if(!owner || !['google_open','google_create'].includes(owner.command) || owner.app!==params.app ||
       owner.tab_id!==params.tab_id || owner.browser_session_id!==await browserSession())throw new Error('GOOGLE_TAB_OWNERSHIP_UNCERTAIN');
    const tab=await chrome.tabs.get(params.tab_id);
    const state=urlState(tab.url);if(state)throw pageError(state);
    if (!allowed(params.app, tab.url)) throw new Error('GOOGLE_TAB_APP_MISMATCH');
    if(!sameOwnedPage(owner,tab.url))throw new Error('GOOGLE_TAB_DOCUMENT_CHANGED');
    return tab;
  }
  async function inject(tab, action, params) {
    const rows=await chrome.scripting.executeScript({target:{tabId:tab.id},world:'ISOLATED',func:inPage,args:[action,params]});
    if (!rows?.length || rows[0].error || !rows[0].result) throw new Error('GOOGLE_PAGE_RESULT_UNCERTAIN');
    return rows[0].result;
  }
  async function loaded(tabId, app, created=false) {
    const deadline=Date.now()+60000;
    while(Date.now()<deadline) {
      const tab=await chrome.tabs.get(tabId);
      const state=urlState(tab.url||'about:blank');if(state)throw pageError(state);
      if (allowed(app,tab.url || 'about:blank') && tab.status==='complete' && (!created || /\/d\/[\w-]+/.test(tab.url))) return tab;
      await sleep(400);
    }
    throw new Error('GOOGLE_PAGE_LOAD_PENDING');
  }
  async function operation(command, p, save) {
    if (command==='google_open' || command==='google_create') {
      if (!allowed(p.app,p.url)) throw new Error('GOOGLE_URL_OUTSIDE_APP');
      if (command==='google_create' && !['docs','sheets','slides','forms'].includes(p.app)) throw new Error('GOOGLE_CREATE_APP_UNSUPPORTED');
      const win=await chrome.windows.create({url:p.url,type:'normal',focused:false,state:'minimized'});
      const tabId=win.tabs?.[0]?.id;
      if (!tabId) throw new Error('GOOGLE_CREATED_TAB_UNKNOWN');
      await save({status:'submitted',tab_id:tabId,window_id:win.id,browser_session_id:await browserSession(),
        requested_url:p.url,purpose:p.purpose||''});
      const tab=await loaded(tabId,p.app,command==='google_create');
      const result={status:'complete',tab_id:tabId,window_id:win.id,url:tab.url,app:p.app};
      const snapshot=await inject(tab,'snapshot',p);
      if(snapshot.status!=='ok') {
        const error=pageError(snapshot.page_status||snapshot.status);error.http_status=snapshot.http_status;
        throw error;
      }
      if (command==='google_create') {
        await save({...result,status:'created'}); // Cloud artifact URL is durable before rename.
        if (p.title && !['forms','docs','sheets'].includes(p.app)) {
          let renamed=false;
          for(let i=0;i<15;i++) {
            try {
              const renamedState=await inject(tab,'set_title',p);
              if(renamedState.status!=='ok')throw pageError(renamedState.page_status||renamedState.status);
              renamed=renamedState.title===p.title;if(renamed)break;
            } catch(error) {if(error.page_status)throw error;await sleep(500);}
          }
          result.title_saved=renamed;
        }
      }
      return result;
    }
    const tab=await tabFor(p);
    if (command==='google_snapshot') return await inject(tab,'snapshot',p);
    if (command==='google_editor_probe') {
      if(!['focus_editor','verify_content'].includes(p.action))throw new Error('GOOGLE_READBACK_ACTION_UNSUPPORTED');
      return await inject(tab,p.action,p);
    }
    if (command==='google_action') {
      if(!['scroll','set_title','set_field','open_link'].includes(p.action))throw new Error('GOOGLE_ACTION_UNSUPPORTED');
      const result=await inject(tab,p.action,p);
      return result.status==='ok'?{...result,status:'complete'}:result;
    }
    if (command==='google_upload') {
      if (p.app!=='drive' || !/^http:\/\/127\.0\.0\.1:17843\/file\/[A-Za-z0-9_-]{32,}\//.test(p.file_url || '')) throw new Error('GOOGLE_LOCAL_UPLOAD_REQUIRED');
      const rows=await chrome.scripting.executeScript({target:{tabId:tab.id},world:'ISOLATED',func:async params=>{
        const res=await fetch(params.file_url,{cache:'no-store'});if(!res.ok)throw new Error('GOOGLE_LOCAL_FILE_UNAVAILABLE');
        const blob=await res.blob();if(blob.size>25*1024*1024)throw new Error('GOOGLE_UPLOAD_FILE_TOO_LARGE');
        const file=new File([blob],params.filename,{type:params.mime || blob.type});
        const data=new DataTransfer();data.items.add(file);
        const input=Array.from(document.querySelectorAll('input[type=file]')).filter(e=>!e.disabled);
        if(input.length===1) {input[0].files=data.files;input[0].dispatchEvent(new Event('change',{bubbles:true}));}
        else {const target=document.querySelector('[role=main]') || document.body;
          for(const type of ['dragenter','dragover','drop'])target.dispatchEvent(new DragEvent(type,{dataTransfer:data,bubbles:true,cancelable:true}));}
        return {submitted:true,filename:file.name,bytes:file.size};
      },args:[p]});
      if(!rows?.[0]?.result?.submitted)throw new Error('GOOGLE_UPLOAD_SUBMISSION_UNCERTAIN');
      return {status:'submitted',...rows[0].result,tab_id:tab.id,url:tab.url};
    }
    throw new Error('GOOGLE_COMMAND_UNSUPPORTED');
  }
  let serial=Promise.resolve();
  async function dispatch(command,p) {
    if(command==='google_session')return {session_id:await browserSession()};
    if(command==='google_inventory') {
      const hash=async value=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)))).map(x=>x.toString(16).padStart(2,'0')).join('');
      const session=await browserSession();
      const receipts=(await chrome.storage.local.get('googleTaskReceipts')).googleTaskReceipts||{};
      const liveTabs=await chrome.tabs.query({});
      const owned=liveTabs.filter(t=>Object.values(receipts).some(r=>
        ['google_open','google_create'].includes(r.command)&&r.browser_session_id===session&&
        r.tab_id===t.id&&allowed(r.app,t.url||'')&&sameOwnedPage(r,t.url))).map(t=>t.id);
      const tabs=await Promise.all(liveTabs.map(async t=>({id:t.id,url_digest:await hash(t.url||'')})));
      return {session_id:session,tabs,owned_tab_ids:owned};
    }
    if (command==='google_job') {
      if(!/^[a-f0-9]{64}$/.test(p.request_id||''))throw new Error('GOOGLE_REQUEST_ID_REQUIRED');
      let row=(await chrome.storage.local.get('googleTaskReceipts')).googleTaskReceipts?.[p.request_id];
      if(!row)return {status:'missing'};
      // Only inspect a tab from this browser session. Never navigate or create here.
      if(p.observe && !inflight.has(p.request_id) && ['google_open','google_create'].includes(row.command) &&
         ['submitted','created'].includes(row.status) && row.browser_session_id===await browserSession() && row.tab_id) {
        let tab;try {tab=await chrome.tabs.get(row.tab_id);}catch{return {...row,error:'GOOGLE_OWNED_TAB_GONE'};}
        const state=urlState(tab.url);
        if(state)return {...row,page_status:state,error:'GOOGLE_'+state.toUpperCase()};
        if(!allowed(row.app,tab.url))return {...row,error:'GOOGLE_TAB_APP_MISMATCH'};
        if(row.url && !sameOwnedPage(row,tab.url))return {...row,error:'GOOGLE_TAB_DOCUMENT_CHANGED'};
        if(row.command==='google_create' && !/\/d\/[\w-]+/.test(new URL(tab.url).pathname))return row;
        // A lost creation reply with no recorded document ID needs reconciliation;
        // a user may have navigated this tab, so don't adopt an arbitrary document.
        if(row.command==='google_create' && !row.url)return {...row,error:'GOOGLE_CREATED_DOCUMENT_UNCERTAIN'};
        if(tab.status!=='complete')return row;
        const snapshot=await inject(tab,'snapshot',{app:row.app});
        if(snapshot.status!=='ok')return {...row,page_status:snapshot.page_status||snapshot.status,http_status:snapshot.http_status};
        row=await writeReceipt(p.request_id,{status:row.command==='google_create'?'created':'complete',
          url:tab.url,error:'',page_status:'',http_status:0,observed_at:new Date().toISOString()});
      }
      return row;
    }
    if (command==='google_verify_upload') {
      const tab=await tabFor(p);if(p.app!=='drive')throw new Error('GOOGLE_DRIVE_REQUIRED');
      const snapshot=await inject(tab,'snapshot',p);
      if(snapshot.status!=='ok')return snapshot;
      const matches=(snapshot.items || []).filter(i=>/^[\w-]{15,}$/.test(i.id) && i.text.includes(p.filename));
      const ids=[...new Set(matches.map(i=>i.id))];
      if(ids.length!==1 || !snapshot.upload_complete || snapshot.upload_in_progress)return {status:'pending'};
      const receipts=(await chrome.storage.local.get('googleTaskReceipts')).googleTaskReceipts || {};
      const row=receipts[p.upload_request_id];
      if(!row || row.command!=='google_upload' || row.app!=='drive' || row.tab_id!==tab.id ||
         row.owner_request_id!==p.owner_request_id || row.browser_session_id!==await browserSession() ||
         (row.filename && row.filename!==p.filename))throw new Error('GOOGLE_UPLOAD_RECEIPT_MISMATCH');
      return await writeReceipt(p.upload_request_id,{status:'complete',artifact_url:'https://drive.google.com/file/d/'+ids[0]+'/view',
        verified_at:new Date().toISOString()});
    }
    if (['google_snapshot','google_editor_probe'].includes(command)) return await operation(command,p,async()=>{});
    if (command==='google_close') {
      const tab=await tabFor(p);await chrome.tabs.remove(tab.id);return {closed:true};
    }
    if (!/^[a-f0-9]{64}$/.test(p.request_id || '')) throw new Error('GOOGLE_REQUEST_ID_REQUIRED');
    const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify([command,p]))))).map(x=>x.toString(16).padStart(2,'0')).join('');
    const receipts=(await chrome.storage.local.get('googleTaskReceipts')).googleTaskReceipts || {};
    const old=receipts[p.request_id];
    if (old) {
      if (old.payload_sha256!==hash)throw new Error('GOOGLE_RECEIPT_REQUEST_MISMATCH');
      return old; // Including submitted/created: never replay an uncertain mutation.
    }
    let row={status:'submitted',command,app:p.app,payload_sha256:hash,created_at:new Date().toISOString(),browser_session_id:await browserSession(),
      ...(Number.isInteger(p.tab_id)?{tab_id:p.tab_id,owner_request_id:p.owner_request_id}: {})};
    async function save(fields) {row=await writeReceipt(p.request_id,{...row,...fields});}
    await save({});
    inflight.add(p.request_id);
    try {const value=await operation(command,p,save);await save(value);return row;}
    catch(error) {await save({error:String(error?.message || error),page_status:error.page_status||'',
      http_status:error.http_status||0});return row;}
    finally {inflight.delete(p.request_id);}
  }
  const inflight=new Set();
  return {allowed,inPage,urlState,execute(command,p={}) {
    if (['google_snapshot','google_job','google_verify_upload','google_close','google_editor_probe','google_session','google_inventory'].includes(command))return dispatch(command,p);
    const pending=serial.then(()=>dispatch(command,p));serial=pending.catch(()=>{});return pending;
  }};
})();
