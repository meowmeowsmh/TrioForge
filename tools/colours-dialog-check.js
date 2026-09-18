// Check the Colours dialog: the note/pin colour pickers should be gone, the context-length
// buttons should be there and should drive the input bar's select.
const { spawn } = require('child_process');
const http = require('http');
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9422;
const profile = process.env.TEMP + '\\tf_modal_' + Date.now();
const chrome = spawn(CHROME, ['--headless=new','--disable-gpu','--ignore-certificate-errors',
  '--remote-debugging-port='+PORT,'--user-data-dir='+profile,'--no-first-run',
  '--window-size=1000,760','https://127.0.0.1:5003/'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const getJSON = p => new Promise((res, rej) => http.get({host:'127.0.0.1',port:PORT,path:p},
  r => { let d=''; r.on('data',c=>d+=c); r.on('end',()=>res(JSON.parse(d))); }).on('error',rej));
(async () => {
  let targets=null;
  for (let i=0;i<60;i++){ try{ targets=await getJSON('/json'); if(targets.some(t=>t.type==='page')) break; }catch(e){} await sleep(500); }
  const page=targets.find(t=>t.type==='page');
  const ws=new WebSocket(page.webSocketDebuggerUrl);
  let id=0; const pending=new Map();
  ws.onmessage=ev=>{ const m=JSON.parse(ev.data); if(m.id&&pending.has(m.id)){pending.get(m.id)(m);pending.delete(m.id);} };
  const send=(m,p)=>new Promise(res=>{const i=++id;pending.set(i,res);ws.send(JSON.stringify({id:i,method:m,params:p}));});
  await new Promise(r=>ws.onopen=r);
  await send('Page.enable',{});
  await send('Emulation.setDeviceMetricsOverride',{width:1000,height:760,deviceScaleFactor:1,mobile:false});
  await sleep(7000);
  const ev=async e=>{const r=await send('Runtime.evaluate',{expression:e,returnByValue:true,awaitPromise:true});
    return r.result&&r.result.result?r.result.result.value:JSON.stringify(r.result);};

  console.log('1. opening the Colours dialog');
  await ev(`(() => { if (typeof openSettingsModal==='function') openSettingsModal();
      else { const m=document.getElementById('settingsModal'); if (m) m.style.display='flex'; } })()`);
  await sleep(1200);

  console.log('2. state:', await ev(`(() => JSON.stringify({
      noteColourInput: !!document.getElementById('setNoteColor'),
      pinColourInput: !!document.getElementById('setPinColor'),
      contextButtons: document.querySelectorAll('#ctxBtnGrid button').length,
      selected: (document.getElementById('ctxSelect')||{}).value,
      labels: Array.prototype.map.call(document.querySelectorAll('#ctxBtnGrid button'), b => b.textContent)
  }))()`));

  console.log('3. clicking the 32K button');
  await ev(`(() => { const b = Array.prototype.find.call(
      document.querySelectorAll('#ctxBtnGrid button'), x => x.textContent === '32K');
      if (b) b.click(); })()`);
  await sleep(1500);
  console.log('4. after the click:', await ev(`(() => JSON.stringify({
      selectValue: (document.getElementById('ctxSelect')||{}).value,
      storedValue: localStorage.getItem('trio_llamacpp_ctx'),
      highlighted: Array.prototype.filter.call(document.querySelectorAll('#ctxBtnGrid button'),
          b => b.style.background.indexOf('31, 111, 235') !== -1).map(b => b.textContent)
  }))()`));

  console.log('5. no screenshot: this dialog sits over the chat, so a capture could contain');
  console.log('   private conversation. The state checks above are the verification.');
  ws.close(); chrome.kill(); process.exit(0);
})();
