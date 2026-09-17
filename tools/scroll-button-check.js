// Verify the scroll-to-bottom arrow's visibility in the real app: at the bottom it must be
// hidden, scrolled up it must show, and back at the bottom hidden again.
const { spawn } = require('child_process');
const http = require('http');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9355;
const URL_ = process.argv[2] || 'https://127.0.0.1:5003/';
const profile = process.env.TEMP + '\\tf_sb_' + Date.now();

const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--ignore-certificate-errors',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile,
    '--no-first-run', '--window-size=1400,900', URL_
], { stdio: 'ignore' });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const getJSON = (p) => new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, r => {
        let d = ''; r.on('data', c => d += c); r.on('end', () => res(JSON.parse(d)));
    }).on('error', rej);
});

(async () => {
    let targets = null;
    for (let i = 0; i < 40; i++) {
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
    await sleep(7000);

    const ev = async (expr) => {
        const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
        return r.result && r.result.result ? r.result.result.value : JSON.stringify(r.result);
    };

    const state = `(() => {
        const b = document.getElementById('scrollBottomBtn');
        const a = document.querySelector('.chat-area');
        if (!b || !a) return 'button or chat area missing';
        return JSON.stringify({
            display: getComputedStyle(b).display,
            overflows: a.scrollHeight - a.clientHeight > 4,
            below: Math.round(a.scrollHeight - a.scrollTop - a.clientHeight)
        });
    })()`;

    console.log('on load (app scrolls to the bottom) :', await ev(state));
    await ev(`(() => { const a = document.querySelector('.chat-area'); a.scrollTop = 0; })()`);
    await sleep(600);
    console.log('after scrolling to the TOP          :', await ev(state));
    await ev(`(() => { const a = document.querySelector('.chat-area'); a.scrollTop = a.scrollHeight; })()`);
    await sleep(600);
    console.log('after scrolling back to the BOTTOM  :', await ev(state));
    // the stale case: force a re-render while scrolled up, then check it is not left behind
    await ev(`(() => { const a = document.querySelector('.chat-area'); a.scrollTop = 0; })()`);
    await sleep(300);
    const scrolled = await ev(state);
    await ev(`(() => { const a = document.querySelector('.chat-area'); a.scrollTop = a.scrollHeight; a.dispatchEvent(new Event('scroll')); })()`);
    await sleep(800);
    console.log('scrolled up, then jump to bottom    :', await ev(state));

    ws.close(); chrome.kill(); process.exit(0);
})();
