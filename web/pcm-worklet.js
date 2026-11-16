// Runs on the audio thread. Downsamples the microphone to 16 kHz PCM16 and
// posts 512-sample blocks: exactly one 1,024-byte Prisma frame (32 ms) each.
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.acc = 0;
    this.sum = 0;
    this.count = 0;
    this.out = new Int16Array(512);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      // Averaging the samples in each output step is a cheap low-pass filter.
      this.sum += ch[i];
      this.count++;
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        let v = this.sum / this.count;
        this.sum = 0;
        this.count = 0;
        v = v < -1 ? -1 : v > 1 ? 1 : v;
        this.out[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.n === 512) {
          const copy = this.out.slice();
          this.port.postMessage(copy.buffer, [copy.buffer]);
          this.n = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCapture);
