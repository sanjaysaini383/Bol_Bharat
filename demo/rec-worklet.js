// Taps everything the page plays, in 0.1 s chunks, each stamped with the
// audio clock time of its first sample. Used only by record_demo.py.
class DemoTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = Math.round(sampleRate / 10);
    this.buf = new Float32Array(this.size);
    this.n = 0;
    this.t0 = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    for (let i = 0; i < 128; i++) {
      if (this.n === 0) this.t0 = currentTime + i / sampleRate;
      this.buf[this.n++] = ch ? ch[i] : 0;
      if (this.n === this.size) {
        this.port.postMessage({ t0: this.t0, pcm: this.buf });
        this.buf = new Float32Array(this.size);
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor('demo-tap', DemoTap);
