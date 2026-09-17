// Reproduce: scroll the chat up (arrow shows), open the embedded Notes view, confirm the
// arrow is hidden, then return to chat and confirm it re-evaluates.
const { spawn } = require('child_process');
const http = require('http');
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9388;
const profile = process.env.TEMP + '\\tf_embed_' + Date.now();
const chrome = spawn(CHROME, ['--headless=new','--disable-gpu','--ignore-certificate-errors',
  '--remote-debugging-port='+PORT,'--user-data-dir='+profile,'--no-first-run',
  '--window-size=1000,640','https://127.0.0.1:5003/'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const getJSON = p => new Promise((res, rej) => http.get({host:'127.0.0.1',port:PORT,path:p},
  r => { let d=''; r.on('data',c=>d+=c); r.on('end',()=>res(JSON.parse(d))); }).on('error',rej));
(async () => {
  let targets=null;
  for (let i=0;i<40;i++){ try{ targets=await getJSON('/json'); if(targets.some(t=>t.type==='page')) break; }catch(e){} await sleep(500); }
  const page=targets.find(t=>t.type==='page');
  const ws=new WebSocket(page.webSocketDebuggerUrl);
  let id=0; const pending=new Map();
  ws.onmessage=ev=>{ const m=JSON.parse(ev.data); if(m.id&&pending.has(m.id)){pending.get(m.id)(m);pending.delete(m.id);} };
  const send=(m,p)=>new Promise(res=>{const i=++id;pending.set(i,res);ws.send(JSON.stringify({id:i,method:m,params:p}));});
  await new Promise(r=>ws.onopen=r);
  await sleep(7000);
  const ev=async e=>{const r=await send('Runtime.evaluate',{expression:e,returnByValue:true});
    return r.result&&r.result.result?r.result.result.value:JSON.stringify(r.result);};
  const st=`(()=>{const b=document.getElementById('scrollBottomBtn');const a=document.querySelector('.chat-area');
    return JSON.stringify({display:b?getComputedStyle(b).display:'n/a', embed:document.documentElement.classList.contains('view-embed'),
    chatHidden:document.getElementById('chatPanel')&&document.getElementById('chatPanel').style.display==='none'});})()`;
  console.log('1. chat, at the bottom              :', await ev(st));
  await ev(`(()=>{document.querySelector('.chat-area').scrollTop=0;})()`);
  await sleep(700);
  console.log('2. scrolled UP (arrow shows)         :', await ev(st));
  const opened = await ev(`(()=>{ if (typeof openEmbeddedView==='function'){ openEmbeddedView('/notes','notes'); return 'opened'; } return 'no fn'; })()`);
  console.log('3. ' + opened);
  await sleep(4000);
  console.log('4. embedded Notes view now           :', await ev(st));
  await ev(`(()=>{ if (typeof showChatView==='function') showChatView(); })()`);
  await sleep(800);
  console.log('5. back to chat (was scrolled up)   :', await ev(st));
  ws.close(); chrome.kill(); process.exit(0);
})();
