// Regenerate the README's screenshots and demo GIF from the running app.
//
//   node tools/make-demo.js                 -> chat.png, notes.png, cork_board.png + frames/
//   then ffmpeg assembles frames/ into demo.gif (see the command in the commit / README).
//
// Drives the real app in headless Chrome over the DevTools protocol, so what it captures is
// what the app actually renders - not a mock-up. The Notes and Cork Board views are opened
// through the app's own embedded-view function, exactly as the tabs do, so the screenshots
// show the real interface. The theme is left as it was found.
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9399;
const BASE = process.env.TF_URL || 'https://127.0.0.1:5003';
const OUT = path.resolve(__dirname, '..');
const FRAMES = path.join(OUT, 'frames');
const W = 1440, H = 900;

const profile = process.env.TEMP + '\\tf_demo_' + Date.now();
const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--ignore-certificate-errors', '--hide-scrollbars',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile, '--no-first-run',
    '--force-device-scale-factor=1', '--window-size=' + W + ',' + H, BASE + '/'
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
    ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    };
    const send = (method, params) => new Promise(res => {
        const myId = ++id; pending.set(myId, res);
        ws.send(JSON.stringify({ id: myId, method, params }));
    });
    await new Promise(res => ws.onopen = res);
    await send('Page.enable', {});
    // Pin the viewport exactly. --window-size gives the WINDOW, which is smaller than the
    // page area, so the captures came out 1424x749 - not the 1440x900 the README uses.
    await send('Emulation.setDeviceMetricsOverride', {
        width: W, height: H, deviceScaleFactor: 1, mobile: false
    });
    await sleep(8000);                          // let the app finish loading

    const ev = async (expr) => {
        const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
        return r.result && r.result.result ? r.result.result.value : null;
    };
    const shot = async (file) => {
        const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
        fs.writeFileSync(file, Buffer.from(r.result.data, 'base64'));
        return file;
    };

    fs.mkdirSync(FRAMES, { recursive: true });
    let frameNo = 0;
    const frame = async (label) => {
        frameNo++;
        const f = path.join(FRAMES, String(frameNo).padStart(2, '0') + '-' + label + '.png');
        await shot(f);
        console.log('  frame ' + f.replace(OUT + path.sep, ''));
    };

    // the theme in use, so it can be put back afterwards
    const startTheme = await ev(`document.documentElement.dataset.tfTheme || 'midnight'`);
    console.log('theme in use:', startTheme);

    // The app opens the Setup dialog on load when a local service is missing, and it covers
    // the interface - the first capture came out with it across the conversation. Close it
    // so the screenshots show the app itself.
    await ev(`(() => {
        if (typeof closeSetupModal === 'function') { closeSetupModal(); }
        const m = document.getElementById('setupModal');
        if (m) { m.style.display = 'none'; }
    })()`);
    await sleep(1200);
    const modal = await ev(`(() => { const m = document.getElementById('setupModal');
        return m ? getComputedStyle(m).display : 'absent'; })()`);
    console.log('setup dialog display:', modal);

    console.log('capturing...');
    await frame('chat');
    await shot(path.join(OUT, 'chat.png'));

    // scroll the conversation a little, so the demo shows movement
    await ev(`(() => { const a = document.querySelector('.chat-area');
        if (a) { a.scrollTop = Math.max(0, a.scrollHeight - a.clientHeight - 900); } })()`);
    await sleep(900);
    await frame('chat-scrolled');

    // the Notes view, opened exactly the way its tab does it
    await ev(`(() => { if (typeof openEmbeddedView === 'function')
        { openEmbeddedView('/notes','notes'); } })()`);
    await sleep(4500);
    await frame('notes');
    await shot(path.join(OUT, 'notes.png'));

    // the Cork Board view, same mechanism
    await ev(`(() => { if (typeof openEmbeddedView === 'function')
        { openEmbeddedView('/corkboard','corkboard'); } })()`);
    await sleep(4500);
    await frame('corkboard');
    await shot(path.join(OUT, 'cork_board.png'));

    // back to the chat, and one other theme, to show the app is themeable
    await ev(`(() => { if (typeof showChatView === 'function') { showChatView(); } })()`);
    await sleep(1600);
    await frame('back-to-chat');

    const other = startTheme === 'galaxy' ? 'premium' : 'galaxy';
    await ev(`window.tfSetTheme && window.tfSetTheme('${other}')`);
    await sleep(1600);
    await frame('theme-' + other);
    await ev(`window.tfSetTheme && window.tfSetTheme('${startTheme}')`);
    await sleep(1200);
    await frame('theme-back');

    console.log('done. stills: chat.png, notes.png, cork_board.png; frames in frames/');

    ws.close(); chrome.kill(); process.exit(0);
})();
