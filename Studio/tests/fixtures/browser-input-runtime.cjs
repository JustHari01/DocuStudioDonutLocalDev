// Isolated Chromium fixture. No Google pages, accounts, real profiles or network requests.
const { app, BrowserWindow } = require('electron');
const http = require('node:http');
const token = require('node:crypto').randomBytes(24).toString('hex');
app.setPath('userData', process.argv[2]);
app.commandLine.appendSwitch('disable-background-networking');
process.stdout.on('error', () => app.exit(0));
process.stdin.on('end', () => app.exit(0));
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
let window;
app.whenReady().then(async () => {
  window = new BrowserWindow({ show: false, width: 900, height: 650,
    webPreferences: { offscreen: true, contextIsolation: true, nodeIntegration: false } });
  await window.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html>
    <meta charset="utf-8"><title>Owned browser input fixture</title>
    <style>body{margin:40px}input{width:350px;height:42px}#cover{position:fixed;inset:0;background:#ddd;display:none}</style>
    <label>Fixture title <input id="title" value="Before"></label><div id="cover"></div>
    <script>window.events=[];for(const type of ['mousedown','mouseup','keydown','keyup','input']){
      document.addEventListener(type,e=>events.push({type,target:e.target.id,key:e.key}));}</script>`));
  window.webContents.debugger.attach('1.3');
  await window.webContents.debugger.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true });
  let queue = Promise.resolve();
  const server = http.createServer((request, response) => {
    if (request.method !== 'POST' || request.url !== '/command' || request.headers.authorization !== 'Bearer ' + token) {
      response.writeHead(403).end(); return;
    }
    let body = '';
    request.on('data', chunk => { body += chunk; if (body.length > 1024 * 1024) request.destroy(); });
    request.on('end', () => {
    queue = queue.then(async () => {
      const { id, method, params = {} } = JSON.parse(body);
      try {
        let result;
        if (method === 'fixture.quit') {
          response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ id, result: {} }));
          server.close(() => app.quit()); return;
        } else if (method === 'fixture.snapshot') result = await window.webContents.executeJavaScript(
          '({value:document.querySelector("#title").value,focused:document.activeElement.id,events:window.events})');
        else if (method === 'fixture.overlay') result = await window.webContents.executeJavaScript(
          'document.querySelector("#cover").style.display="block";true');
        else result = await window.webContents.debugger.sendCommand(method, params);
        response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ id, result }));
      } catch (error) {
        response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ id, error: String(error.message || error) }));
      }
    }).catch(error => { send({ error: String(error) }); app.exit(1); });
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  send({ ready: true, endpoint: 'http://127.0.0.1:' + server.address().port + '/command', token });
}).catch(error => { send({ error: String(error) }); app.exit(1); });
