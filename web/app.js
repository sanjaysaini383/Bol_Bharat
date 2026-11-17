import { Mic, Player, earcon } from './audio.js';

const $ = (id) => document.getElementById(id);
const rupee = (n) => '₹' + Math.abs(n).toLocaleString('en-IN');

const LANGS = [
  ['auto', 'Auto', 'Apne aap pehchane'], ['hi', 'हिन्दी', 'Hindi / Hinglish'],
  ['en', 'English', 'English'], ['kn', 'ಕನ್ನಡ', 'Kannada'], ['ta', 'தமிழ்', 'Tamil'],
  ['te', 'తెలుగు', 'Telugu'], ['mr', 'मराठी', 'Marathi'], ['bn', 'বাংলা', 'Bengali'],
  ['gu', 'ગુજરાતી', 'Gujarati'], ['pa', 'ਪੰਜਾਬੀ', 'Punjabi'], ['ml', 'മലയാളം', 'Malayalam'],
];

const token = new URLSearchParams(location.search).get('k');
const qs = token ? `?k=${encodeURIComponent(token)}` : '';

const state = {
  ws: null, connected: false, started: false, speech: false,
  mode: 'ptt', lang: localStorage.getItem('munim.lang') || 'auto',
  holding: false, streaming: false, tab: 'customers', ledger: null,
  lastChanged: new Set(), loud: 0, retry: 0,
  floor: 0.01,                 // running estimate of the room's noise level
  ttfa: [], llmTurns: 0, bargeIns: 0,
};

let ctx = null, mic = null, player = null;

// ------------------------------------------------------------------ helpers
function setPhase(phase) {
  document.body.dataset.phase = phase;
  const hints = {
    idle: state.mode === 'handsfree' ? '“Munim ji…” bolkar shuru karein' : 'Dabakar boliye',
    listening: 'Sun raha hoon…', thinking: 'Likh raha hoon…',
    speaking: 'Beech mein bolne ke liye dabayein',
  };
  $('hint').textContent = hints[phase] || '';
}

function banner(text) {
  $('banner').hidden = !text;
  $('banner').textContent = text || '';
}

function send(obj) {
  if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(obj));
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function dayLabel(iso, today) {
  if (!iso) return '';
  const d = Math.round((new Date(today) - new Date(iso)) / 86400000);
  if (d === 0) return 'aaj';
  if (d === 1) return 'kal';
  if (d === -1) return 'kal (aane wala)';
  if (d > 1 && d < 7) return `${d} din pehle`;
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

// ------------------------------------------------------------------- render
function renderLedger() {
  const L = state.ledger;
  if (!L) return;
  $('total').textContent = rupee(L.outstanding);
  $('owing').textContent = L.owing ? `${L.owing} grahak par` : 'Sab hisaab barabar';
  $('tCredit').textContent = rupee(L.today_credit);
  $('tPay').textContent = rupee(L.today_payment);

  const list = $('list');
  list.replaceChildren();
  if (state.tab === 'customers') {
    if (!L.customers.length) list.append(el('p', 'empty', 'Abhi koi grahak nahi. Bolkar pehli entry likhiye.'));
    for (const c of L.customers) {
      const row = el('div', 'row-item');
      row.dataset.id = c.id;
      row.addEventListener('click', () => openCustomer(c.id));
      if (state.lastChanged.has(c.name)) row.classList.add('flash');
      const left = el('div');
      const name = el('div', 'name', c.name);
      if (c.opt_in) name.append(el('span', 'tag ok', 'REMINDER OK'));
      left.append(name);
      const detail = [c.spoken, c.descriptor].filter(Boolean).join(' · ');
      if (detail) left.append(el('div', 'detail', detail));
      const amt = el('div', 'amt ' + (c.balance > 0 ? 'due' : c.balance < 0 ? 'adv' : 'zero'),
        rupee(c.balance));
      amt.append(el('small', '', c.balance > 0 ? 'BAAKI' : c.balance < 0 ? 'ADVANCE' : 'BARABAR'));
      row.append(left, amt);
      list.append(row);
    }
  } else if (state.tab === 'entries') {
    if (!L.recent.length) list.append(el('p', 'empty', 'Koi entry nahi.'));
    for (const e of L.recent) {
      const row = el('div', 'row-item' + (e.status !== 'ACTIVE' ? ' void' : ''));
      const left = el('div');
      const name = el('div', 'name', e.customer);
      if (e.status === 'CORRECTED') name.append(el('span', 'tag', 'SUDHARA'));
      if (e.status === 'UNDONE') name.append(el('span', 'tag', 'HATAYA'));
      left.append(name, el('div', 'detail',
        `${e.kind === 'CREDIT' ? 'Udhaar' : 'Jama'} · ${dayLabel(e.date, L.today)}`));
      const amt = el('div', 'amt ' + (e.kind === 'CREDIT' ? 'due' : 'adv'),
        (e.kind === 'CREDIT' ? '+' : '−') + rupee(e.amount));
      row.append(left, amt);
      list.append(row);
    }
  } else {
    if (!L.promises.length) list.append(el('p', 'empty', 'Koi vaada note nahi. Boliye: “Ramesh kal 200 de dega”.'));
    for (const p of L.promises) {
      const row = el('div', 'row-item');
      const left = el('div');
      left.append(el('div', 'name', p.customer),
        el('div', 'detail', p.due ? `Dene ka vaada · ${dayLabel(p.due, L.today)}` : 'Dene ka vaada'));
      row.append(left, el('div', 'amt zero', rupee(p.amount)));
      list.append(row);
    }
  }
  state.lastChanged.clear();
}

function renderReply(m) {
  const said = $('said');
  said.textContent = m.text;
  said.className = 'said' + (m.kind === 'ask' ? ' ask' : m.kind === 'refuse' ? ' refuse' : '');
  const chips = $('chips');
  chips.replaceChildren();
  for (const e of m.entries || []) {
    const chip = el('span', 'entry-chip ' + e.kind);
    const label = { CREDIT: 'udhaar', PAYMENT: 'jama', PROMISE: 'vaada' }[e.kind];
    chip.append(el('span', '', e.customer), el('b', '', rupee(e.amount)), el('small', '', label));
    chips.append(chip);
    state.lastChanged.add(e.customer);
  }
  if (ctx) {
    if (m.kind === 'entry') {
      // Udhaar and jama sound different, so the direction is heard even
      // when the spoken reply is lost in shop noise.
      const kinds = new Set((m.entries || []).map((e) => e.kind));
      const one = kinds.size === 1 ? [...kinds][0] : null;
      earcon(ctx, one === 'CREDIT' ? 'credit' : one === 'PAYMENT' ? 'payment' : 'saved');
      navigator.vibrate?.(one === 'PAYMENT' ? [25, 60, 25] : 30);
    }
    else if (m.kind === 'ask') earcon(ctx, 'ask');
    else if (m.kind === 'refuse' || m.kind === 'reprompt') earcon(ctx, 'error');
  }
  if (m.reminder) {
    $('reminderText').textContent = m.reminder.text;
    $('reminderSheet').showModal();
  }
  if (m.statement) openCustomer(m.statement.id);
}

// One customer's page, like a page of the paper khata: every line with the
// balance after it. Opened by voice ("Ramesh ka khata sunao") or by a tap.
async function openCustomer(id) {
  let c;
  try {
    const r = await fetch(`api/customer/${id}${qs}`);
    if (!r.ok) return;
    c = await r.json();
  } catch (_) { return; }
  $('custName').textContent = c.name;
  $('custDetail').textContent = [c.spoken, c.descriptor, c.opt_in ? 'reminder OK' : null]
    .filter(Boolean).join(' · ');
  const bal = $('custBal');
  bal.className = 'amt ' + (c.balance > 0 ? 'due' : c.balance < 0 ? 'adv' : 'zero');
  bal.replaceChildren(rupee(c.balance),
    el('small', '', c.balance > 0 ? 'BAAKI' : c.balance < 0 ? 'ADVANCE' : 'BARABAR'));
  const promises = $('custPromises');
  promises.replaceChildren();
  for (const p of c.promises) {
    promises.append(el('p', 'promise-line',
      `Vaada: ${rupee(p.amount)}${p.due ? ' · ' + dayLabel(p.due, c.today) : ''}`));
  }
  const rows = $('custRows');
  rows.replaceChildren();
  if (!c.entries.length) rows.append(el('p', 'empty', 'Koi entry nahi.'));
  for (const e of c.entries) {
    const row = el('div', 'stmt-row');
    const credit = e.kind === 'CREDIT';
    const what = el('span', 'what ' + (credit ? 'credit' : 'pay'), (credit ? '+' : '−') + rupee(e.amount));
    what.append(el('small', '', credit ? 'udhaar' : 'jama'));
    row.append(el('span', 'when', dayLabel(e.date, c.today)), what,
      el('span', 'bal', rupee(e.balance)));
    rows.append(row);
  }
  const sheet = $('custSheet');
  if (!sheet.open) sheet.showModal();
}

function renderMetrics(m) {
  const parts = [];
  if (m.ttfa_ms != null) parts.push(`suna → bola ${(m.ttfa_ms / 1000).toFixed(2)}s`);
  if (m.stt_ms != null) parts.push(`stt ${Math.round(m.stt_ms)}ms`);
  if (m.brain_ms != null) parts.push(`samjha ${m.brain_ms}ms`);
  if (m.path) parts.push(m.path === 'llm' ? 'LLM' : 'rules');
  $('meta').textContent = parts.join(' · ');
  if (m.ttfa_ms != null) {
    state.ttfa.push(m.ttfa_ms);
    if (m.path === 'llm') state.llmTurns += 1;
    renderStats();
  }
}

// Session figures for the menu: how fast the replies have been.
function renderStats() {
  const a = [...state.ttfa].sort((x, y) => x - y);
  if (!a.length) return;
  const sec = (ms) => (ms / 1000).toFixed(2) + 's';
  const pick = (q) => a[Math.min(a.length - 1, Math.floor(q * a.length))];
  $('stats').hidden = false;
  $('stats').textContent =
    `Is session mein ${a.length} jawab · suna → bola: median ${sec(pick(0.5))}, ` +
    `sabse tez ${sec(a[0])}, sabse dheema ${sec(a[a.length - 1])} · ` +
    `LLM ${state.llmTurns} · beech mein toka ${state.bargeIns} baar`;
}

// ---------------------------------------------------------------- websocket
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws${qs}`);
  ws.binaryType = 'arraybuffer';
  state.ws = ws;

  ws.onopen = () => {
    state.connected = true;
    state.retry = 0;
    $('conn').className = 'conn on';
    $('conn').title = 'Connected';
    if (state.started) send({ type: 'start', lang: state.lang, mode: state.mode });
  };

  ws.onclose = (e) => {
    state.connected = false;
    $('conn').className = 'conn off';
    $('conn').title = 'Disconnected';
    setPhase('idle');
    if (e.code === 4401) { banner('Access token galat hai. Link mein ?k=… lagayein.'); return; }
    setTimeout(connect, Math.min(8000, 500 * 2 ** state.retry++));
  };

  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) { player?.push(ev.data); return; }
    const m = JSON.parse(ev.data);
    switch (m.type) {
      case 'hello':
        state.speech = m.speech;
        banner(m.speech ? '' :
          'Voice band hai: server par GNANI_API_KEY set nahi hai. Menu se likhkar try karein.');
        break;
      case 'ledger':
        state.ledger = m.data;
        renderLedger();
        if (!state.linked) {            // a link like …/#c=3 opens that customer's page
          state.linked = true;
          const id = /^#c=(\d+)$/.exec(location.hash)?.[1];
          if (id) openCustomer(id);
        }
        break;
      case 'state':
        if (m.phase === 'idle' && player?.playing) break;
        if (!(state.holding && m.phase !== 'listening')) setPhase(m.phase);
        break;
      case 'stt':
        $('heard').textContent = m.text;
        $('heard').classList.remove('muted');
        break;
      case 'ignored':
        $('heard').textContent = m.text;
        $('heard').classList.add('muted');
        break;
      case 'reply': renderReply(m); break;
      case 'metrics': renderMetrics(m); break;
      case 'audio_start': player?.begin(m.turn, m.rate); setPhase('speaking'); break;
      case 'audio_end': player?.end(m.turn); if (!player?.playing) setPhase('idle'); break;
      case 'audio_stop': player?.stop(); break;
      case 'error': banner(m.message); if (ctx) earcon(ctx, 'error'); setPhase('idle'); break;
    }
  };
}

// -------------------------------------------------------------------- audio
async function ensureAudio() {
  if (!ctx) {
    ctx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: 'interactive' });
    player = new Player(ctx);
    player.onDone = () => setPhase('idle');
    mic = new Mic(ctx);
    mic.onChunk = onMicChunk;
  }
  if (ctx.state === 'suspended') await ctx.resume();
  if (state.speech && !mic.ready) {
    try {
      await mic.start();
    } catch (err) {
      banner('Microphone ki anumati nahi mili. Browser settings mein mic allow karein.');
      throw err;
    }
  }
}

function onMicChunk(buf, level) {
  if (!state.connected) return;
  if (state.mode === 'ptt') {
    if (state.streaming) state.ws.send(buf);
    return;
  }
  // Track the room's noise level: drop to any quieter block at once, creep
  // up slowly, so speech does not raise it but a noisy street does.
  state.floor = Math.min(level, state.floor * 1.003 + 1e-5);
  // Hands-free. While the agent is speaking the mic is not streamed (so it
  // never hears itself); loud speech over the reply counts as a barge-in.
  // "Loud" is measured against the room, so shop noise alone does not
  // cut the agent off.
  if (player.playing) {
    const loudAt = Math.min(0.35, Math.max(0.09, state.floor * 4));
    state.loud = level > loudAt ? state.loud + 1 : 0;
    if (state.loud >= 5) {            // ~160 ms of sustained speech
      state.loud = 0;
      bargeIn();
      for (const b of mic.takePreroll()) state.ws.send(b);
      state.ws.send(buf);
    }
    return;
  }
  state.ws.send(buf);
}

function bargeIn() {
  if (player?.playing) {
    player.stop();                    // stop locally first: no network wait
    send({ type: 'interrupt' });
    state.bargeIns += 1;
    renderStats();
  }
}

// ------------------------------------------------------------ push-to-talk
async function pttDown() {
  if (!state.started || state.holding) return;
  state.holding = true;
  try { await ensureAudio(); } catch (_) { state.holding = false; return; }
  if (!state.speech) { state.holding = false; $('menuSheet').showModal(); $('typeInput').focus(); return; }
  if (state.mode === 'handsfree') { bargeIn(); state.holding = false; return; }
  if (!state.holding) return;         // released while the mic prompt was open
  if (player.playing) { state.bargeIns += 1; renderStats(); }
  player.stop();
  send({ type: 'ptt_down' });
  for (const b of mic.takePreroll()) state.ws.send(b);
  state.streaming = true;
  setPhase('listening');
  navigator.vibrate?.(15);
}

function pttUp() {
  if (!state.holding) return;
  state.holding = false;
  if (!state.streaming) return;
  state.streaming = false;
  send({ type: 'ptt_up' });
  setPhase('thinking');
}

const micBtn = $('mic');
micBtn.addEventListener('pointerdown', (e) => { e.preventDefault(); micBtn.setPointerCapture(e.pointerId); pttDown(); });
micBtn.addEventListener('pointerup', pttUp);
micBtn.addEventListener('pointercancel', pttUp);
micBtn.addEventListener('contextmenu', (e) => e.preventDefault());
document.addEventListener('keydown', (e) => {
  if (e.code === 'Space' && !e.repeat && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)
      && !document.querySelector('dialog[open]')) { e.preventDefault(); pttDown(); }
});
document.addEventListener('keyup', (e) => { if (e.code === 'Space') pttUp(); });

// --------------------------------------------------------------- controls
$('startBtn').addEventListener('click', async () => {
  $('startBtn').disabled = true;
  try { await ensureAudio(); } catch (_) { /* text-only still works */ }
  state.started = true;
  $('welcome').hidden = true;
  send({ type: 'start', lang: state.lang, mode: state.mode });
  navigator.wakeLock?.request('screen').catch(() => {});
});

$('modeBtn').addEventListener('click', async () => {
  if (!state.speech) return;
  try { await ensureAudio(); } catch (_) { return; }
  state.mode = state.mode === 'ptt' ? 'handsfree' : 'ptt';
  $('modeBtn').setAttribute('aria-pressed', String(state.mode === 'handsfree'));
  micBtn.setAttribute('aria-label', state.mode === 'handsfree' ? 'Tap to interrupt' : 'Hold to talk');
  send({ type: 'mode', mode: state.mode });
  setPhase('idle');
});

document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('on', x === b));
  state.tab = b.dataset.tab;
  renderLedger();
}));

function renderLangs() {
  const list = $('langList');
  list.replaceChildren();
  for (const [code, native, english] of LANGS) {
    const b = el('button', code === state.lang ? 'on' : '', native);
    b.append(el('small', '', english));
    b.addEventListener('click', () => {
      state.lang = code;
      localStorage.setItem('munim.lang', code);
      $('langBtn').textContent = native;
      $('langSheet').close();
      if (state.started) send({ type: 'start', lang: code, mode: state.mode });
    });
    list.append(b);
  }
}
$('langBtn').textContent = (LANGS.find((l) => l[0] === state.lang) || LANGS[0])[1];
$('langBtn').addEventListener('click', () => { renderLangs(); $('langSheet').showModal(); });
$('menuBtn').addEventListener('click', () => $('menuSheet').showModal());
document.querySelectorAll('[data-close]').forEach((b) =>
  b.addEventListener('click', () => b.closest('dialog').close()));
document.querySelectorAll('dialog').forEach((d) =>
  d.addEventListener('click', (e) => { if (e.target === d) d.close(); }));

$('typeForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = $('typeInput').value.trim();
  if (!text) return;
  try { await ensureAudio(); } catch (_) {}
  $('heard').textContent = text;
  $('heard').classList.remove('muted');
  send({ type: 'text', text });
  $('typeInput').value = '';
  $('menuSheet').close();
});

$('exportBtn').href = `api/export.csv${qs}`;

$('resetBtn').addEventListener('click', async () => {
  if (!confirm('Poora khata mitakar demo data dobara load karein?')) return;
  await fetch(`/api/demo/reset${qs}`, { method: 'POST' });
  $('menuSheet').close();
});
$('purgeBtn').addEventListener('click', async () => {
  const r = await fetch(`/api/purge${qs}`, { method: 'POST' });
  const j = await r.json();
  alert(`${j.deleted} transcripts mitaye gaye.`);
});

$('shareBtn').addEventListener('click', async () => {
  const text = $('reminderText').textContent;
  if (navigator.share) { try { await navigator.share({ text }); } catch (_) {} }
  else { await navigator.clipboard.writeText(text); $('shareBtn').textContent = 'Copied'; }
});
$('copyBtn').addEventListener('click', async () => {
  await navigator.clipboard.writeText($('reminderText').textContent);
  $('copyBtn').textContent = 'Copied';
  setTimeout(() => { $('copyBtn').textContent = 'Copy'; }, 1500);
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state.started) {
    navigator.wakeLock?.request('screen').catch(() => {});
    ctx?.resume();
  }
});

if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});

setPhase('idle');
connect();
