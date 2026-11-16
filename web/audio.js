// Microphone capture and streamed playback. No libraries.

const PREROLL_BLOCKS = 8;   // ~250 ms kept so the first syllable is never clipped

export class Mic {
  constructor(ctx) {
    this.ctx = ctx;
    this.onChunk = null;     // (ArrayBuffer of PCM16 @16 kHz, rms 0..1) => void
    this.preroll = [];
    this.ready = false;
  }

  async start() {
    if (this.ready) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,   // lets the user talk over the agent
        noiseSuppression: true,   // shop and street noise
        autoGainControl: true,
      },
    });
    await this.ctx.audioWorklet.addModule('pcm-worklet.js');
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, 'pcm-capture');
    this.node.port.onmessage = (e) => {
      const buf = e.data;
      this.preroll.push(buf);
      if (this.preroll.length > PREROLL_BLOCKS) this.preroll.shift();
      if (this.onChunk) this.onChunk(buf, rms(buf));
    };
    // The worklet must be connected to run; a muted gain keeps it silent.
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    src.connect(this.node).connect(mute).connect(this.ctx.destination);
    this.ready = true;
  }

  takePreroll() {
    const blocks = this.preroll.slice(0, -1);
    this.preroll = [];
    return blocks;
  }
}

function rms(buf) {
  const a = new Int16Array(buf);
  let s = 0;
  for (let i = 0; i < a.length; i += 4) s += a[i] * a[i];
  return Math.sqrt(s / (a.length / 4)) / 32768;
}

// Plays PCM16 chunks as they arrive, back to back, and can stop instantly.
export class Player {
  constructor(ctx) {
    this.ctx = ctx;
    this.turn = -1;
    this.rate = 24000;
    this.next = 0;
    this.sources = new Set();
    this.finished = true;
    this.onDone = null;
  }

  get playing() { return this.sources.size > 0 || (this.turn !== -1 && !this.finished); }

  begin(turn, rate) {
    this.stop();
    this.turn = turn;
    this.rate = rate;
    this.next = 0;
    this.finished = false;
  }

  // frame: 4-byte little-endian turn id, then PCM16 mono
  push(frame) {
    const turn = new DataView(frame).getUint32(0, true);
    if (turn !== this.turn) return;          // audio from an interrupted reply
    const pcm = new Int16Array(frame, 4);
    if (!pcm.length) return;
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const buffer = this.ctx.createBuffer(1, f32.length, this.rate);
    buffer.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    const at = Math.max(this.ctx.currentTime + 0.03, this.next);
    src.start(at);
    this.next = at + buffer.duration;
    this.sources.add(src);
    src.onended = () => { this.sources.delete(src); this._check(); };
  }

  end(turn) {
    if (turn !== this.turn) return;
    this.finished = true;
    this._check();
  }

  _check() {
    if (this.finished && this.sources.size === 0 && this.turn !== -1) {
      this.turn = -1;
      if (this.onDone) this.onDone();
    }
  }

  stop() {
    for (const s of this.sources) { s.onended = null; try { s.stop(); } catch (_) {} }
    this.sources.clear();
    this.turn = -1;
    this.finished = true;
  }
}

// Short confirmation tones: heard without looking at the screen.
export function earcon(ctx, kind) {
  const tones = { saved: [660, 880], ask: [520, 520], error: [300, 220], start: [440] }[kind];
  if (!tones) return;
  const t0 = ctx.currentTime;
  tones.forEach((f, i) => {
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = 'sine';
    o.frequency.value = f;
    const t = t0 + i * 0.09;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.12, t + 0.012);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.085);
    o.connect(g).connect(ctx.destination);
    o.start(t);
    o.stop(t + 0.1);
  });
}
