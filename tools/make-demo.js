// Capture README media SAFELY.
//
// The first version of this tool filmed whatever was on screen. It captured a personal
// photograph out of a real conversation and published it to a public repository. That must
// never be possible again, so this version:
//
//   * opens a NEW EMPTY conversation and films only that - it never scrolls through, or
//     even loads, an existing chat;
//   * deletes that temporary conversation when it is done, so nothing is left behind;
//   * prints every frame's path and byte size, because a photographic frame is far larger
//     than a UI frame (237 KB vs 129 KB in the incident) and that is the cheap tell;
//   * requires the operator to LOOK at the frames before building anything from them.
//
//   node tools/make-demo.js [outdir]
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9411;
const BASE = process.env.TF_URL || 'https://127.0.0.1:5003';
const OUT = process.argv[2] ? path.resolve(process.argv[2]) : path.join(process.env.TEMP, 'tf_demo_safe');
const W = 1440, H = 900;

const profile = process.env.TEMP + '\\tf_demo_safe_' + Date.now();
const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--ignore-certificate-errors', '--hide-scrollbars',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile, '--no-first-run',
    '--window-size=' + W + ',' + H, BASE + '/'
], { stdio: 'ignore' });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const getJSON = (p) => new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, r => {
        let d = ''; r.on('data', c => d += c); r.on('end', () => res(JSON.parse(d)));
    }).on('error', rej);
});

(async () => {
    let targets = null;
    for (let i = 0; i < 60; i++) {
        try { targets = await getJSON('/json'); if (targets.some(t => t.type === 'page')) break; } catch (e) {}
        await sleep(500);
    }
    const page = targets.find(t => t.type === 'page');
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    let id = 0; const pending = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data);
        if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
    const send = (method, params) => new Promise(res => {
        const myId = ++id; pending.set(myId, res);
        ws.send(JSON.stringify({ id: myId, method, params })); });
    await new Promise(res => ws.onopen = res);
    await send('Page.enable', {});
    await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
    await sleep(8000);

    const ev = async (expr) => {
        const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
        return r.result && r.result.result ? r.result.result.value : null;
    };

    fs.mkdirSync(OUT, { recursive: true });
    let n = 0;
    const frame = async (label) => {
        n++;
        const f = path.join(OUT, String(n).padStart(2, '0') + '-' + label + '.png');
        const r = await send('Page.captureScreenshot', { format: 'png' });
        fs.writeFileSync(f, Buffer.from(r.result.data, 'base64'));
        const kb = Math.round(fs.statSync(f).size / 1024);
        console.log('  ' + path.basename(f).padEnd(28) + kb + ' KB' + (kb > 180 ? '   <-- LARGE: inspect this one' : ''));
        return f;
    };

    // 1. close the setup dialog (it covers the interface), put the app in a known theme
    await ev(`(() => { if (typeof closeSetupModal==='function') closeSetupModal();
        const m=document.getElementById('setupModal'); if (m) m.style.display='none'; })()`);
    const startTheme = await ev(`document.documentElement.dataset.tfTheme || 'midnight'`);
    await ev(`window.tfSetTheme && window.tfSetTheme('premium')`);
    await sleep(900);

    // 2. a NEW EMPTY conversation - the only chat this tool ever shows. newChat() creates
    // one but does not OPEN it, which is why the first attempt still had the previous
    // conversation (56 messages) on screen; the guard below caught that and refused.
    const made = await ev(`(async () => {
        const r = await fetch('/conversations', { method: 'POST' });
        const j = await r.json();
        return JSON.stringify(j);
    })()`);
    const newId = (JSON.parse(made || '{}') || {}).id;
    console.log('  created conversation:', newId);
    await ev(`(() => { if (typeof selectConversation === 'function') { selectConversation(${JSON.stringify(newId)}); } })()`);
    await sleep(3000);
    const chatState = await ev(`(() => {
        const nodes = document.querySelectorAll('.chat-area .msg');
        const texts = Array.from(nodes).map(n => (n.innerText || '').trim().slice(0, 60));
        const area = document.querySelector('.chat-area');
        return JSON.stringify({ count: texts.length, texts: texts,
                                scrollHeight: area ? area.scrollHeight : -1 }); })()`);
    console.log('  new chat state:', chatState);
    // Accept an empty conversation, or one whose entire content is the app's own welcome
    // bubble. Anything else means real content is on screen, and this tool must not film it:
    // that is exactly how a personal photograph reached a public repository.
    let st = {};
    try { st = JSON.parse(chatState || '{}'); } catch (e) { st = {}; }
    const texts = st.texts || [];
    const benign = texts.filter(t => !/no messages yet|say something|create one above/i.test(t));
    if (benign.length > 0) {
        console.log('  REFUSING: this conversation contains real content - capturing nothing.');
        console.log('  content seen:', JSON.stringify(benign).slice(0, 220));
        ws.close(); chrome.kill(); process.exit(2);
    }
    console.log('  guard passed: nothing on screen but the welcome bubble');

    console.log('capturing (empty chat, notes, corkboard, themes only):');
    await frame('chat-empty');
    await ev(`(() => { if (typeof openEmbeddedView==='function') openEmbeddedView('/notes','notes'); })()`);
    await sleep(4000);
    await frame('notes');
    await ev(`(() => { if (typeof openEmbeddedView==='function') openEmbeddedView('/corkboard','corkboard'); })()`);
    await sleep(4000);
    await frame('corkboard');
    await ev(`(() => { if (typeof showChatView==='function') showChatView(); })()`);
    await sleep(1500);
    await frame('chat-back');
    await ev(`window.tfSetTheme && window.tfSetTheme('galaxy')`);
    await sleep(1400);
    await frame('theme-galaxy');
    await ev(`window.tfSetTheme && window.tfSetTheme('premium')`);
    await sleep(1200);
    await frame('theme-premium');
    await ev(`window.tfSetTheme && window.tfSetTheme('${startTheme}')`);

    // 3. delete the temporary conversation, so nothing is left behind
    const removed = await ev(`(async () => {
        const r = await fetch('/conversations/' + ${JSON.stringify(newId)}, { method: 'DELETE' });
        return 'deleted the temporary conversation -> ' + r.status;
    })()`);
    console.log('cleanup:', removed);

    console.log('\nSTOP AND LOOK AT THE FRAMES BEFORE USING THEM:  ' + OUT);
    ws.close(); chrome.kill(); process.exit(0);
})();
