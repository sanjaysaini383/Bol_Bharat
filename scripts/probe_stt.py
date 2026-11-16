"""Stream a WAV file through Prisma v3.0 exactly as the app does, and show
what the agent would understand. Use it to build an audio regression set
and to compare output formats under noise.

    python scripts/probe_stt.py eval/audio/ramesh_340.wav
    python scripts/probe_stt.py clip.wav --lang hi --format codemix

The WAV must be 16 kHz, 16-bit, mono (ffmpeg -i in.m4a -ar 16000 -ac 1 out.wav).
Record your own voice only: no customer recordings.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from munim.config import get_settings          # noqa: E402
from munim.gnani_stt import FRAME_BYTES, GnaniSTT  # noqa: E402
from munim.parser import parse                 # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--lang", default="auto")
    ap.add_argument("--format", default=None, help="transcribe | codemix | verbatim | transliterate")
    args = ap.parse_args()

    s = get_settings()
    if not s.has_speech:
        raise SystemExit("Set GNANI_API_KEY in .env first.")
    if args.format:
        s = dataclasses.replace(s, stt_format=args.format)

    with wave.open(args.wav, "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (16000, 2, 1):
            raise SystemExit("WAV must be 16 kHz, 16-bit, mono.")
        pcm = w.readframes(w.getnframes())

    done = asyncio.Event()
    sent_at = 0.0

    async def on_event(e: dict) -> None:
        if e.get("type") == "transcript":
            text = e.get("text", "")
            p = parse(text)
            print(f"\ntranscript : {text}")
            print(f"language   : {e.get('detected_language_code')}")
            print(f"stt latency: {e.get('latency')} ms (reported), "
                  f"{(time.monotonic() - sent_at) * 1000:.0f} ms after commit")
            print(f"understood : {p.intent} "
                  f"{[(d.name, d.amount, d.direction, d.day_offset) for d in p.drafts]}")
            done.set()
        elif e.get("type") == "error":
            print("error:", e.get("message"))
            done.set()

    stt = GnaniSTT(s, on_event, args.lang)
    await stt.connect()
    for i in range(0, len(pcm), FRAME_BYTES):       # real-time cadence, 32 ms frames
        await stt.send_audio(pcm[i:i + FRAME_BYTES])
        await asyncio.sleep(0.032)
    sent_at = time.monotonic()
    await stt.commit()
    try:
        await asyncio.wait_for(done.wait(), 10)
    except asyncio.TimeoutError:
        print("no transcript within 10 s")
    await stt.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
