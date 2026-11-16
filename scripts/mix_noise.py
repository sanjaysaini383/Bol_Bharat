"""Mix shop/street noise under a clean recording at a chosen SNR.

    python scripts/mix_noise.py clean.wav noise.wav out.wav --snr 5

Both inputs: 16 kHz, 16-bit, mono WAV. The noise loops if it is shorter.
Run the result through scripts/probe_stt.py to test "numbers under noise"
at 15, 10 and 5 dB.
"""

from __future__ import annotations

import argparse
import math
import wave
from array import array


def read(path: str) -> array:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (16000, 2, 1):
            raise SystemExit(f"{path}: must be 16 kHz, 16-bit, mono")
        a = array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a


def rms(a: array) -> float:
    return math.sqrt(sum(x * x for x in a) / max(1, len(a)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("speech")
    ap.add_argument("noise")
    ap.add_argument("out")
    ap.add_argument("--snr", type=float, default=10.0, help="signal-to-noise ratio in dB")
    args = ap.parse_args()

    speech, noise = read(args.speech), read(args.noise)
    if not noise:
        raise SystemExit("noise file is empty")
    gain = rms(speech) / (rms(noise) * 10 ** (args.snr / 20) or 1)
    out = array("h", (
        max(-32768, min(32767, int(s + gain * noise[i % len(noise)])))
        for i, s in enumerate(speech)))
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(out.tobytes())
    print(f"wrote {args.out} at {args.snr:g} dB SNR")


if __name__ == "__main__":
    main()
