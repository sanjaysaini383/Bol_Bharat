"""Audio eval: spoken shop entries, with shop noise, through the real speech stack.

Each case in eval/audio_cases.jsonl is spoken by Timbre v3.0, mixed with
shop noise at a chosen signal-to-noise ratio, streamed through Prisma v3.0
the way the app streams a push-to-talk hold, and handed to the same
dialogue code the app runs. The ledger is then scored the way the judges
score: exact amount, credit/payment direction, customer.

    python scripts/eval_audio.py                    # clean, 10 dB and 5 dB
    python scripts/eval_audio.py --snr clean 0      # choose the conditions
    python scripts/eval_audio.py --only three-forty --show
    python scripts/eval_audio.py --report docs/audio-eval.md

Synthesised speech and transcripts are cached under eval/audio/ (git-ignored),
so only new audio spends Gnani credits. A re-run after a parser change is free.

The speech is synthetic (stock Timbre voice, no cloning), so this measures
number formats, code-mix and noise, not accents. Test real voices too.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import math
import os
import random
import sys
import wave
from array import array
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from munim.config import get_settings              # noqa: E402
from munim.dialogue import Brain                   # noqa: E402
from munim.gnani_stt import FRAME_BYTES, GnaniSTT  # noqa: E402
from munim.gnani_tts import GnaniTTS               # noqa: E402
from munim.ledger import Ledger                    # noqa: E402
from munim.llm import LLM                          # noqa: E402

TODAY = date(2026, 11, 16)
RATE = 16000
AUDIO = ROOT / "eval" / "audio"
CACHE = AUDIO / "stt_cache.json"

# Counter chatter for the noise bed. Spoken by the stock voice, then
# pitch-shifted so it does not sound like the speaker being tested.
BABBLE = ["भैया एक किलो चीनी और आधा किलो दाल देना",
          "यह बिस्कुट का पैकेट कितने का है",
          "अरे जल्दी करो, बस आने वाली है",
          "दो पैकेट दूध और एक ब्रेड दे दो, बाकी पैसे रख लो"]


def fresh_ledger() -> Ledger:
    led = Ledger(":memory:", os.urandom(32))
    led.add_customer("Ramesh Kumar", "रमेश कुमार", "दूध वाले", opt_in=True)
    led.add_customer("Ramesh Gupta", "रमेश गुप्ता")
    led.add_customer("Suresh", "सुरेश")
    led.add_customer("Sunita", "सुनीता")
    return led


# ------------------------------------------------------------------ audio
def read_wav(path: Path) -> array:
    with wave.open(str(path), "rb") as w:
        a = array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a


def write_wav(path: Path, pcm: array) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def rms(a: array) -> float:
    return math.sqrt(sum(x * x for x in a) / max(1, len(a)))


def resample(a: array, factor: float) -> array:
    """Play ``a`` at ``factor`` speed (changes pitch and tempo together)."""
    n = int(len(a) / factor)
    out = array("h", bytes(2 * n))
    last = len(a) - 1
    for i in range(n):
        x = i * factor
        j = int(x)
        f = x - j
        out[i] = int(a[j] * (1 - f) + a[min(j + 1, last)] * f)
    return out


async def speak(tts: GnaniTTS, text: str, lang: str) -> array:
    """Timbre audio for ``text`` at 16 kHz, cached on disk."""
    name = hashlib.sha1(f"{lang}|{text}".encode()).hexdigest()[:16]
    path = AUDIO / "tts" / f"{name}.wav"
    if path.exists():
        return read_wav(path)
    pcm = array("h")
    pcm.frombytes(b"".join([c async for c in tts.synthesize(text, lang)]))
    if tts.sample_rate != RATE:
        pcm = resample(pcm, tts.sample_rate / RATE)
    write_wav(path, pcm)
    return pcm


async def noise_bed(tts: GnaniTTS, seconds: int = 12) -> array:
    """Shop noise: overlapping customer chatter, traffic rumble and a horn."""
    path = AUDIO / "noise_shop.wav"
    if path.exists():
        return read_wav(path)
    rnd = random.Random(7)
    n = seconds * RATE
    mix = [0.0] * n
    for text, factor in zip(BABBLE, (0.84, 1.16, 0.92, 1.24)):
        voice = resample(await speak(tts, text, "hi"), factor)
        gain = 1.0 / (rms(voice) or 1)
        at = rnd.randrange(RATE)
        while at < n:                                  # loop each talker with gaps
            for i, x in enumerate(voice):
                if at + i >= n:
                    break
                mix[at + i] += x * gain
            at += len(voice) + rnd.randrange(RATE // 4, RATE)
    brown = 0.0
    for i in range(n):                                 # traffic: leaky brown noise
        brown = 0.985 * brown + rnd.uniform(-1, 1) * 0.12
        horn = 0.0
        t = (i / RATE) % 5.0
        if 3.0 < t < 3.45:                             # two-tone horn every 5 s
            horn = 0.5 * (math.sin(2 * math.pi * 420 * t) + math.sin(2 * math.pi * 520 * t))
        mix[i] += brown * 1.4 + horn * 0.6          # chatter stays the loudest part
    peak = max(abs(x) for x in mix) or 1
    bed = array("h", (int(x / peak * 20000) for x in mix))
    write_wav(path, bed)
    return bed


def add_noise(speech: array, bed: array, snr_db: float, offset: int) -> array:
    gain = rms(speech) / ((rms(bed) or 1) * 10 ** (snr_db / 20))
    pad = RATE // 4                                    # noise starts before the speech
    out = array("h", bytes(2 * (len(speech) + 2 * pad)))
    for i in range(len(out)):
        s = speech[i - pad] if pad <= i < pad + len(speech) else 0
        out[i] = max(-32768, min(32767, int(s + gain * bed[(offset + i) % len(bed)])))
    return out


# -------------------------------------------------------------------- STT
class Recogniser:
    """One Prisma stream, driven like a push-to-talk hold, with a disk cache."""

    def __init__(self, settings, lang: str, keyterms: list[str], cache: dict,
                 need_lang: bool = False):
        self.s, self.lang, self.keyterms, self.cache = settings, lang, keyterms, cache
        self.need_lang = need_lang                     # the detected language matters
        self.stt: GnaniSTT | None = None
        self.parts: list[str] = []
        self.detected: str | None = None
        self.got = asyncio.Event()
        self.error: str | None = None

    async def _on_event(self, e: dict) -> None:
        if e.get("type") == "transcript":
            if (e.get("text") or "").strip():
                self.parts.append(e["text"].strip())
                self.detected = e.get("detected_language_code")
            self.got.set()
        elif e.get("type") == "error":
            self.error = e.get("message")
            self.got.set()

    async def transcribe(self, pcm: array) -> tuple[str, str | None]:
        """(transcript, language the recogniser detected)."""
        raw = pcm.tobytes()
        key = hashlib.sha1(raw + f"|{self.lang}|{self.s.stt_format}|{self.keyterms}".encode()).hexdigest()
        hit = self.cache.get(key)
        if isinstance(hit, list):
            return hit[0], hit[1]
        if isinstance(hit, str) and not self.need_lang:
            return hit, None                           # cached before the tag was kept
        if self.stt is None:
            self.stt = GnaniSTT(self.s, self._on_event, self.lang, self.keyterms)
            await self.stt.connect()
        self.parts, self.error, self.detected = [], None, None
        self.got.clear()
        for i in range(0, len(raw), FRAME_BYTES):      # real-time cadence
            await self.stt.send_audio(raw[i:i + FRAME_BYTES])
            await asyncio.sleep(FRAME_BYTES / 2 / RATE)
        await self.stt.commit()
        try:                                           # VAD may split a hold in two
            await asyncio.wait_for(self.got.wait(), 6)
            while True:
                self.got.clear()
                await asyncio.wait_for(self.got.wait(), 1.0)
        except asyncio.TimeoutError:
            pass
        if self.error:
            raise RuntimeError(f"STT error: {self.error}")
        text = " ".join(self.parts)
        self.cache[key] = [text, self.detected]
        return text, self.detected

    async def close(self) -> None:
        if self.stt:
            await self.stt.close()


# -------------------------------------------------------------------- eval
async def run_case(case, cond, settings, tts, bed, cache, llm, stt_lang) -> dict:
    led = fresh_ledger()
    brain = Brain(led, llm if case.get("llm") else None, today=lambda: TODAY,
                  log_transcripts=False)
    lang = case.get("lang", "hi")
    brain.greeting(lang)
    rec = Recogniser(settings, stt_lang or "auto", led.keyterms(), cache,
                     need_lang=lang not in ("hi", "en"))
    heard, replies = [], []
    try:
        for n, text in enumerate(case["say"]):
            pcm = await speak(tts, text, lang)
            if cond != "clean":
                offset = int(hashlib.sha1(f"{case['id']}{n}".encode()).hexdigest(), 16) % len(bed)
                pcm = add_noise(pcm, bed, float(cond), offset)
            t, detected = await rec.transcribe(pcm)
            heard.append(t)
            if t:
                # As the app does: the script decides the language, and the
                # recogniser's tag only tells Marathi from Hindi.
                hint = "mr" if (detected or "").startswith("mr") else None
                replies.append((await brain.handle(t, hint, followup=n > 0)).text)
    finally:
        await rec.close()
    got = sorted([e["customer"], e["kind"], e["amount"]]
                 for e in led.snapshot(TODAY)["recent"] if e["status"] == "ACTIVE")
    want = sorted(case["expect"])
    return {"id": case["id"], "cond": cond, "ok": got == want, "want": want, "got": got,
            "said": case["say"], "heard": heard, "replies": replies,
            "wrong_write": any(e not in want for e in got),
            "amounts_ok": sorted(e[2] for e in got) == sorted(e[2] for e in want),
            "direction_ok": sorted(e[1:] for e in got) == sorted(e[1:] for e in want)}


def label(cond: str) -> str:
    return "clean" if cond == "clean" else f"{float(cond):g} dB SNR"


def report(results, conds, args, settings) -> str:
    lines = ["# Audio eval: spoken entries under shop noise", "",
             f"Run on {date.today().isoformat()} with `python scripts/eval_audio.py`.", "",
             "Each case is spoken by Timbre v3.0, mixed with shop noise (overlapping customer",
             "chatter, traffic rumble, a horn), streamed through Prisma v3.0 as a push-to-talk",
             "hold, and scored on the final ledger: exact amount, credit or payment, customer.",
             "", f"STT language: `{args.lang}` · format: `{settings.stt_format}` · "
             f"LLM fallback: {'on for the non-Hindi cases' if args.llm else 'off'}", "",
             "| Condition | Conversations correct | Amounts exact | Direction correct "
             "| Wrong line written |",
             "|---|---|---|---|---|"]
    for c in conds:
        rs = [r for r in results if r["cond"] == c]
        if not rs:
            continue
        n = len(rs)
        lines.append(f"| {label(c)} | {sum(r['ok'] for r in rs)} / {n} | "
                     f"{sum(r['amounts_ok'] for r in rs)} / {n} | "
                     f"{sum(r['direction_ok'] for r in rs)} / {n} | "
                     f"{sum(r['wrong_write'] for r in rs)} / {n} |")
    lines += ["", "*Wrong line written* counts conversations that left a line in the ledger which",
              "should not be there. A conversation can fail without one: the agent asked a",
              "question or wrote nothing, which the shopkeeper hears and can retry."]
    fails = [r for r in results if not r["ok"]]
    lines += ["", "## Failures" if fails else "No failures.", ""]
    for r in fails:
        lines += [f"- **{r['id']}** at {label(r['cond'])}: said {r['said']}, "
                  f"heard {r['heard']}; wanted {r['want']}, got {r['got']}"]
    lines += ["", "## What this does not measure", "",
              "The speech is synthetic and comes from the same vendor as the recogniser, so real",
              "voices, accents and phone microphones will score lower. The noise is generated, not",
              "recorded in a shop. Treat the table as a regression check on number formats,",
              "code-mix and noise handling, not as field accuracy."]
    return "\n".join(lines) + "\n"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snr", nargs="+", default=["clean", "10", "5"],
                    help="conditions: 'clean' or a signal-to-noise ratio in dB")
    ap.add_argument("--lang", default="auto", help="STT language (auto = indic-auto)")
    ap.add_argument("--only", nargs="+", help="case ids to run")
    ap.add_argument("--llm", action="store_true", help="include the cases that need the LLM")
    ap.add_argument("--show", action="store_true", help="print every transcript")
    ap.add_argument("--jobs", type=int, default=3, help="parallel STT streams")
    ap.add_argument("--report", help="write a markdown report to this path")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.has_speech:
        raise SystemExit("Set GNANI_API_KEY in .env first.")
    cases = [json.loads(line) for line in (ROOT / "eval" / "audio_cases.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    llm = None
    if args.llm and settings.has_llm:
        llm = LLM(settings.llm_api_key, settings.llm_base_url, settings.llm_model,
                  8.0, settings.llm_reasoning_effort)
    cases = [c for c in cases if llm or not c.get("llm")]

    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    tts = GnaniTTS(dataclasses.replace(settings, tts_sample_rate=RATE))
    bed = await noise_bed(tts)
    for c in cases:                                    # synthesise before timing anything
        for text in c["say"]:
            await speak(tts, text, c.get("lang", "hi"))
    await tts.close()

    sem = asyncio.Semaphore(args.jobs)

    async def guarded(case, cond):
        async with sem:
            try:
                return await run_case(case, cond, settings, tts, bed, cache, llm,
                                      None if args.lang == "auto" else args.lang)
            finally:
                AUDIO.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")

    results = await asyncio.gather(*(guarded(c, cond) for cond in args.snr for c in cases))
    if llm:
        await llm.close()

    for cond in args.snr:
        rs = [r for r in results if r["cond"] == cond]
        print(f"\n== {label(cond)}: {sum(r['ok'] for r in rs)}/{len(rs)} conversations correct, "
              f"amounts exact {sum(r['amounts_ok'] for r in rs)}/{len(rs)}, "
              f"wrong line written {sum(r['wrong_write'] for r in rs)}/{len(rs)}")
        for r in rs:
            if args.show or not r["ok"]:
                print(f"  {'ok  ' if r['ok'] else 'FAIL'} {r['id']:18} heard {r['heard']}")
                if not r["ok"]:
                    print(f"       want {r['want']}  got {r['got']}")
    if args.report:
        Path(args.report).write_text(report(results, args.snr, args, settings), encoding="utf-8")
        print(f"\nwrote {args.report}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
