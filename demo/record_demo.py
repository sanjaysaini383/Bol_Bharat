"""Record the demo video: a live session with the real app, captions on screen.

    python demo/record_demo.py            # writes demo/out/munim-ji-demo.mp4 and .srt

What is real: the app in a browser, the server, Prisma v3.0 hearing the audio
through the app's microphone path, the dialogue code, Timbre v3.0 speaking the
replies. What is staged: the shopkeeper's voice is a stock Timbre voice played
into the microphone input (no cloning, no real customer audio), and the button
is pressed by this script.

Needs: GNANI_API_KEY in .env, Google Chrome and ffmpeg. Chrome is driven over
its DevTools protocol with the `websockets` package the app already uses.
Speech clips are cached in demo/assets/, so a retake only spends credits on the
live session itself. The session runs on a throwaway ledger (demo/out/data).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from websockets.asyncio.client import connect        # noqa: E402

import eval_audio                                    # noqa: E402
from munim.config import get_settings                # noqa: E402
from munim.gnani_tts import GnaniTTS                 # noqa: E402

PORT, DEBUG_PORT = 8765, 9377
BASE = f"http://127.0.0.1:{PORT}"
CHROME = next((c for c in (
    os.environ.get("CHROME", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome") if c and Path(c).exists()), "chrome")
DEMO = ROOT / "demo"
ASSETS, OUT = DEMO / "assets", DEMO / "out"
RATE = 24000
SHOPKEEPER, NARRATOR = "Shivani", "Lakshmi"          # stock voices; the agent is Avinash

INTRO = ("Munim-ji is a voice khata for kirana stores. The shopkeeper holds one button "
         "and talks. This is a live session.")
OUTRO = ("Every reply you heard was generated live. With shop noise at ten decibels, "
         "all twenty-four test conversations stay correct.")

SAME = "Sunita ne five hundred ka payment kiya"
SCENES = [
    {"title": "The first turn says it is an AI",
     "note": "In the user's language, before anything else.",
     "turns": [{"start": True,
                "agent_en": "Hello, I am Munim-ji, your AI ledger assistant. Go ahead, what should I note?"}]},
    {"title": "One sentence, two entries",
     "note": "Amount, credit or payment, and the date: “kal” is yesterday.",
     "turns": [{"say": "सुरेश का तीन सौ चालीस उधार, कल दो सौ दिया था",
                "en": "Suresh: 340 on credit. Yesterday he paid 200.",
                "agent_en": "Suresh: 340 credit, yesterday's 200 paid. Balance due 400 rupees."}]},
    {"title": "Fixed by voice",
     "note": "“Nahi, 430” corrects the amount it sounds like: 340, not 200.",
     "turns": [{"say": "नहीं, चार सौ तीस", "en": "No, four hundred thirty.",
                "agent_en": "Corrected. Suresh: 430 credit. Balance due 490 rupees."}]},
    {"title": "Two customers share a name",
     "note": "One short question instead of a guess.",
     "turns": [{"say": "रमेश का पाँच सौ उधार", "en": "Ramesh: 500 on credit.",
                "agent_en": "Which one? Ramesh Kumar the milkman, or Ramesh Gupta?"},
               {"say": "गुप्ता", "en": "Gupta.",
                "agent_en": "Ramesh Gupta: 500 credit. Balance due 1000 rupees."}]},
    {"title": "Code-mix, over shop noise",
     "note": "Customer chatter and traffic are playing, 10 dB below the speaker.",
     "turns": [{"say": SAME, "en": "Sunita made a payment of five hundred.", "noise": 10,
                "agent_en": "Sunita: 500 paid. Account settled."}]},
    {"title": "Said twice is not written twice",
     "note": "People repeat themselves over noise. Munim-ji asks first.",
     "turns": [{"say": SAME, "en": "Sunita made a payment of five hundred.", "noise": 10,
                "agent_en": "Sunita's 500 payment was just noted. Add one more?"},
               {"say": "नहीं", "en": "No.", "agent_en": "Okay, cancelled."}]},
    {"title": "No lending",
     "note": "Interest, loans and penalties are refused by voice.",
     "turns": [{"say": "सुरेश पर ब्याज लगा दो", "en": "Add interest on Suresh.",
                "agent_en": "Sorry, I do not add interest, loans or penalties. "
                            "I only record credit and payments."}]},
    {"title": "Reminders need consent",
     "note": "Suresh has not opted in, so nothing is prepared.",
     "turns": [{"say": "सुरेश को रिमाइंडर भेजो", "en": "Send Suresh a reminder.",
                "agent_en": "Suresh has not agreed to reminders, so I will not send one."}]},
    {"title": "Barge-in, then a spoken answer",
     "note": "Talk over the reply and it stops. Answers are SQL and templates, no LLM.",
     "turns": [{"say": "सुरेश का खाता सुनाओ", "en": "Read out Suresh's account.", "cut_after": 2.4,
                "agent_en": "Suresh: today 430 credit, yesterday's 200 paid … (interrupted)"},
               {"say": "आज कितना उधार गया?", "en": "How much went on credit today?",
                "agent_en": "Today 930 rupees went on credit, in 2 entries."}]},
    {"title": "Other languages",
     "note": "Kannada, picked in the app. The reply and the AI disclosure switch language too.",
     "turns": [{"say": "ಸುರೇಶ್ ಗೆ ಮುನ್ನೂರು ರೂಪಾಯಿ ಸಾಲ ಬರೆಯಿರಿ", "lang": "kn", "pick_lang": 4,
                "en": "Write 300 rupees credit for Suresh.",
                "agent_en": "Hello, I am Munim-ji, your AI ledger assistant. "
                            "Suresh: 300 credit. Balance due 790 rupees."}]},
]

# Runs in the app frame before the app's own code: a microphone we can play
# clips into, and a tap on everything the page sends to the speakers.
INIT_JS = r"""
(() => {
  if (location.pathname.startsWith('/__demo')) return;
  const AC = window.AudioContext;
  const connect = AudioNode.prototype.connect;
  AudioNode.prototype.connect = function (dest, ...rest) {
    if (dest instanceof AudioDestinationNode && this.context.__rec) connect.call(this, this.context.__rec);
    return connect.call(this, dest, ...rest);
  };
  window.AudioContext = function (...args) {
    if (!window.__ctx) {
      const c = new AC(...args);
      c.__rec = c.createGain();                 // everything audible also goes here
      c.__mic = c.createMediaStreamDestination();
      window.__ctx = c;
    }
    return window.__ctx;
  };
  window.AudioContext.prototype = AC.prototype;
  navigator.mediaDevices.getUserMedia = async () => window.__ctx.__mic.stream;

  const bufs = {};
  let tap = null, chunks = [];
  window.__replies = 0;
  window.__demo = {
    init() { new window.AudioContext({ latencyHint: 'interactive' }); return window.__ctx.sampleRate; },
    watch() {
      new MutationObserver(() => { window.__replies += 1; })
        .observe(document.getElementById('said'), { childList: true, characterData: true, subtree: true });
    },
    async load(name) {
      const r = await fetch('/__demo/assets/' + name);
      bufs[name] = await window.__ctx.decodeAudioData(await r.arrayBuffer());
      return bufs[name].duration;
    },
    play(name, toMic, gain) {
      return new Promise((done) => {
        const ctx = window.__ctx, s = ctx.createBufferSource(), g = ctx.createGain();
        s.buffer = bufs[name];
        g.gain.value = gain;
        connect.call(s, g);
        connect.call(g, ctx.__rec);
        if (toMic) connect.call(s, ctx.__mic);
        s.onended = done;
        s.start();
      });
    },
    async startRec() {
      // Resolves with the wall-clock time (ms) of the first recorded sample.
      const ctx = window.__ctx;
      await ctx.audioWorklet.addModule('/__demo/rec-worklet.js');
      tap = new AudioWorkletNode(ctx, 'demo-tap');
      const mute = ctx.createGain();
      mute.gain.value = 0;
      connect.call(ctx.__rec, tap);
      connect.call(tap, mute);
      connect.call(mute, ctx.destination);       // a worklet only runs when it reaches the output
      chunks = [];
      return new Promise((started) => {
        tap.port.onmessage = (e) => {
          if (!chunks.length) started(Date.now() - (ctx.currentTime - e.data.t0) * 1000);
          chunks.push(e.data.pcm);
        };
      });
    },
    stopRec() {
      // 16-bit mono WAV of everything recorded, as base64.
      tap.port.onmessage = null;
      const n = chunks.reduce((s, c) => s + c.length, 0), rate = window.__ctx.sampleRate;
      const out = new DataView(new ArrayBuffer(44 + 2 * n));
      const text = (at, s) => { for (let i = 0; i < s.length; i++) out.setUint8(at + i, s.charCodeAt(i)); };
      text(0, 'RIFF'); out.setUint32(4, 36 + 2 * n, true); text(8, 'WAVEfmt ');
      out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
      out.setUint32(24, rate, true); out.setUint32(28, 2 * rate, true);
      out.setUint16(32, 2, true); out.setUint16(34, 16, true); text(36, 'data'); out.setUint32(40, 2 * n, true);
      let at = 44;
      for (const c of chunks) {
        for (let i = 0; i < c.length; i++, at += 2) {
          out.setInt16(at, Math.max(-1, Math.min(1, c[i])) * 32767, true);
        }
      }
      const bytes = new Uint8Array(out.buffer);
      let s = '';
      for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      return btoa(s);
    },
  };
})();
"""


# ------------------------------------------------------------------ assets
def wav_write(path: Path, pcm: array, rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def wav_read(path: Path) -> array:
    with wave.open(str(path), "rb") as w:
        a = array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a


async def clip(text: str, lang: str, voice: str) -> str:
    """A spoken clip in demo/assets, synthesised once."""
    name = f"{voice.lower()}-{hashlib.sha1(f'{lang}|{text}'.encode()).hexdigest()[:12]}.wav"
    path = ASSETS / name
    if not path.exists():
        s = dataclasses.replace(get_settings(), tts_voice=voice, tts_sample_rate=RATE, tts_speed=1.0)
        tts = GnaniTTS(s)
        pcm = array("h")
        pcm.frombytes(b"".join([c async for c in tts.synthesize(text, lang)]))
        await tts.close()
        wav_write(path, pcm)
    return name


def with_noise(name: str, snr_db: float) -> str:
    """The clip with shop noise under it, and a little noise before and after."""
    out = name.replace(".wav", f"-noise{snr_db:g}.wav")
    if (ASSETS / out).exists():
        return out
    speech = wav_read(ASSETS / name)
    bed = eval_audio.resample(eval_audio.read_wav(eval_audio.AUDIO / "noise_shop.wav"),
                              eval_audio.RATE / RATE)
    gain = eval_audio.rms(speech) / ((eval_audio.rms(bed) or 1) * 10 ** (snr_db / 20))
    lead, tail = int(0.45 * RATE), int(0.25 * RATE)
    mixed = array("h", bytes(2 * (lead + len(speech) + tail)))
    for i in range(len(mixed)):
        s = speech[i - lead] if lead <= i < lead + len(speech) else 0
        mixed[i] = max(-32768, min(32767, int(s + gain * bed[(RATE + i) % len(bed)])))
    wav_write(ASSETS / out, mixed)
    return out


# ------------------------------------------------------------------ server
def start_server() -> subprocess.Popen:
    env = dict(os.environ, PORT=str(PORT), HOST="127.0.0.1", DATA_DIR=str(OUT / "data"),
               ACCESS_TOKEN="", DEMO_SEED="1",
               KHATA_ENC_KEY="")        # the throwaway ledger gets its own key file
    log = open(OUT / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(DEMO / "serve.py")], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/api/health", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise SystemExit("server did not start; see demo/out/server.log")
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/api/demo/reset", method="POST"))
    return proc


# ------------------------------------------------------------------ chrome
class Chrome:
    """A headless Chrome tab driven over the DevTools protocol."""

    def __init__(self):
        self.proc = None
        self.ws = None
        self._id = 0
        self._waiting: dict[int, asyncio.Future] = {}
        self.on_frame = None

    async def start(self) -> None:
        # A fresh profile every take: the app remembers the chosen language.
        shutil.rmtree(OUT / "chrome-profile", ignore_errors=True)
        self.proc = subprocess.Popen([
            CHROME, "--headless=new", f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={OUT / 'chrome-profile'}",
            "--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream",
            "--window-size=1280,720", "--force-device-scale-factor=1.5",
            "--hide-scrollbars", "--no-first-run",
            "--disable-extensions", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=1))
                url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except (OSError, ValueError, StopIteration) as e:
                last = repr(e)
                await asyncio.sleep(0.25)
        else:
            self.proc.kill()
            raise SystemExit(f"Chrome did not start: {last}")
        self.ws = await connect(url, max_size=None)
        asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m:
                fut = self._waiting.pop(m["id"], None)
                if fut and not fut.done():
                    fut.set_result(m)
            elif m.get("method") == "Page.screencastFrame" and self.on_frame:
                self.on_frame(m["params"])
            elif m.get("method") == "Runtime.exceptionThrown":
                d = m["params"]["exceptionDetails"]
                print("  [browser error]", d.get("exception", {}).get("description") or d.get("text"))

    async def send(self, method: str, **params):
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._waiting[self._id] = fut
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        m = await fut
        if "error" in m:
            raise RuntimeError(f"{method}: {m['error']}")
        return m["result"]

    async def js(self, expr: str, *args):
        """Evaluate in the stage page; ``app`` is the app's window. Awaits promises."""
        code = ("(async () => { const app = document.getElementById('app')?.contentWindow; "
                f"const a = {json.dumps(list(args), ensure_ascii=False)}; return ({expr}); }})()")
        r = await asyncio.wait_for(self.send(
            "Runtime.evaluate", expression=code, awaitPromise=True, returnByValue=True), 40)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError(d.get("exception", {}).get("description") or d.get("text"))
        return r["result"].get("value")

    async def mouse(self, kind: str, x: float, y: float) -> None:
        await self.send("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left",
                        buttons=0 if kind == "mouseReleased" else 1, clickCount=1)

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.send("Browser.close"), 3)
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------- recording
async def record() -> dict:
    frames_dir = OUT / "frames"
    if frames_dir.exists():
        for f in frames_dir.iterdir():
            f.unlink()
    frames_dir.mkdir(parents=True, exist_ok=True)

    # every clip is ready before the take starts
    intro, outro = await clip(INTRO, "en", NARRATOR), await clip(OUTRO, "en", NARRATOR)
    for scene in SCENES:
        for t in scene["turns"]:
            if "say" in t:
                t["clip"] = await clip(t["say"], t.get("lang", "hi"), SHOPKEEPER)
                if t.get("noise") is not None:
                    t["clip"] = with_noise(t["clip"], t["noise"])
    names = {intro, outro} | {t["clip"] for s in SCENES for t in s["turns"] if "clip" in t}

    subs: list[tuple[float, float, str]] = []
    ttfa: list[int] = []
    heard: list[dict] = []
    frames: list[tuple[float, Path]] = []

    chrome = Chrome()
    await chrome.start()
    try:
        js = chrome.js
        await chrome.send("Page.enable")
        await chrome.send("Runtime.enable")
        await chrome.send("Emulation.setDeviceMetricsOverride", width=1280, height=720,
                          deviceScaleFactor=0, mobile=False)
        await chrome.send("Page.addScriptToEvaluateOnNewDocument", source=INIT_JS)
        await chrome.send("Page.navigate", url=f"{BASE}/__demo/stage.html")

        async def until(check, timeout, step=0.04):
            end = time.time() + timeout
            while time.time() < end:
                if await check():
                    return True
                await asyncio.sleep(step)
            return False

        async def ready():
            try:
                return await js("!!(app && app.__demo && app.document.getElementById('startBtn')"
                                " && app.document.getElementById('list').children.length)")
            except RuntimeError:
                return False
        if not await until(ready, 20, 0.2):
            raise RuntimeError("the app did not load in the stage")
        await js("app.__demo.init()")
        # Headless Chrome keeps audio suspended until a user gesture.
        await chrome.send("Runtime.evaluate", awaitPromise=True, userGesture=True,
                          expression="document.getElementById('app').contentWindow.__ctx.resume()")
        if await js("app.__ctx.state") != "running":
            raise RuntimeError("the page's audio did not start")
        await js("app.__demo.watch()")
        for n in names:
            await js("app.__demo.load(a[0])", n)

        def on_frame(ev):
            path = frames_dir / f"{len(frames):06d}.jpg"
            frames.append((ev["metadata"]["timestamp"], path))
            path.write_bytes(base64.b64decode(ev["data"]))
            asyncio.ensure_future(chrome.send("Page.screencastFrameAck", sessionId=ev["sessionId"]))
        chrome.on_frame = on_frame

        async def centre(selector: str) -> tuple[float, float]:
            """Centre of an element inside the app, in stage coordinates."""
            x, y = await js("(() => { const f = document.getElementById('app').getBoundingClientRect();"
                            " const r = app.document.querySelector(a[0]).getBoundingClientRect();"
                            " return [f.x + r.x + r.width / 2, f.y + r.y + r.height / 2]; })()", selector)
            return x, y

        phase = lambda: js("app.document.body.dataset.phase")      # noqa: E731
        replies = lambda: js("app.__replies")                      # noqa: E731
        mx, my = await centre("#mic")
        px, py = await js("(() => { const r = document.querySelector('.phone').getBoundingClientRect();"
                          " return [r.x, r.y]; })()")
        await js("stage.touch(false, a[0], a[1])", mx - px, my - py)

        async def press():
            await chrome.mouse("mouseMoved", mx, my)
            await chrome.mouse("mousePressed", mx, my)
            await js("stage.touch(true)")

        async def release():
            await chrome.mouse("mouseReleased", mx, my)
            await js("stage.touch(false)")

        async def say(t: dict) -> None:
            """Hold the button, speak the clip into the microphone, let go."""
            await js("stage.caption('Shopkeeper', a[0], a[1])", t["say"], t["en"])
            await press()
            await asyncio.sleep(0.22)
            t0 = time.time()
            await js("app.__demo.play(a[0], true, 1.0)", t["clip"])
            subs.append((t0, time.time(), f"Shopkeeper: {t['say']}\n{t['en']}"))
            await asyncio.sleep(0.12)
            await release()

        async def reply(t: dict, before: int) -> None:
            """Show the agent's reply as it arrives and wait while it is spoken."""
            if not await until(lambda: _gt(replies, before), 12):
                raise RuntimeError(f"no reply to {t.get('say', 'start')!r}")
            text = await js("app.document.getElementById('said').textContent")
            t0 = time.time()
            await js("stage.caption('Munim-ji', a[0], a[1])", text, t.get("agent_en", ""))
            await until(lambda: _eq(phase, "speaking"), 4)
            read_meta = lambda: js("app.document.getElementById('meta').textContent")   # noqa: E731
            await until(lambda: _ne(read_meta, t.get("meta_before")), 1.5)
            meta = await read_meta()
            m = re.search(r"bola ([\d.]+)s", meta)
            if m and not t.get("start"):
                ttfa.append(int(float(m.group(1)) * 1000))
                await js("stage.metric(a[0])",
                         f"<span>end of speech → first audio</span> {float(m.group(1)):.2f} s")
            if t.get("cut_after"):
                await asyncio.sleep(t["cut_after"])
            else:
                await until(lambda: _eq(phase, "idle"), 25)
                await asyncio.sleep(0.25)
            subs.append((t0, time.time(), f"Munim-ji: {text}\n{t.get('agent_en', '')}"))
            heard.append({"said": t.get("say"), "reply": text, "meta": meta})
            print(f"  > {t.get('say', '(start)')}\n  < {text}   [{meta}]")

        await chrome.send("Page.startScreencast", format="jpeg", quality=92,
                          maxWidth=1920, maxHeight=1080, everyNthFrame=1)
        rec_start = await js("app.__demo.startRec()") / 1000
        await asyncio.sleep(0.6)

        # ---- title card
        t_start = time.time()
        await js("stage.intro(true, a[0])", INTRO)
        await asyncio.sleep(0.35)
        t0 = time.time()
        await js("app.__demo.play(a[0], false, 1.0)", intro)
        subs.append((t0, time.time(), INTRO))
        await js("stage.intro(false)")

        # ---- the session
        for n, scene in enumerate(SCENES, start=1):
            await js("stage.scene(a[0], a[1], a[2], a[3])", n, len(SCENES), scene["title"], scene["note"])
            for t in scene["turns"]:
                before = await replies()
                t["meta_before"] = await js("app.document.getElementById('meta').textContent")
                if t.get("start"):
                    await js("stage.caption(null)")
                    await asyncio.sleep(0.9)
                    sx, sy = await centre("#startBtn")
                    await chrome.mouse("mouseMoved", sx, sy)
                    await chrome.mouse("mousePressed", sx, sy)
                    await chrome.mouse("mouseReleased", sx, sy)
                else:
                    if t.get("pick_lang"):               # open the language sheet, tap the language
                        for selector in ("#langBtn", f"#langList button:nth-child({t['pick_lang']})"):
                            x, y = await centre(selector)
                            await chrome.mouse("mouseMoved", x, y)
                            await chrome.mouse("mousePressed", x, y)
                            await chrome.mouse("mouseReleased", x, y)
                            await asyncio.sleep(0.6)
                        await asyncio.sleep(0.9)         # the recogniser reconnects in that language
                    await say(t)
                await reply(t, before)

        # ---- end card
        speed = (f"median {statistics.median(ttfa) / 1000:.2f} s from end of speech to first audio, "
                 f"over {len(ttfa)} replies") if ttfa else "—"
        await js("stage.outro(true, a[0], a[1])", OUTRO, speed)
        await asyncio.sleep(0.3)
        t0 = time.time()
        await js("app.__demo.play(a[0], false, 1.0)", outro)
        subs.append((t0, time.time(), OUTRO))
        await asyncio.sleep(2.8)                     # hold the end card; keeps the video over 2:00
        t_end = time.time()

        await chrome.send("Page.stopScreencast")
        (OUT / "audio.wav").write_bytes(base64.b64decode(await js("app.__demo.stopRec()")))
    finally:
        await chrome.close()

    return {"frames": frames, "t_start": t_start, "t_end": t_end, "rec_start": rec_start,
            "subs": subs, "ttfa": ttfa, "heard": heard}


async def _gt(fn, n):
    return await fn() > n


async def _eq(fn, value):
    return await fn() == value


async def _ne(fn, value):
    return await fn() != value


# ----------------------------------------------------------------- assembly
def stamp(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def assemble(r: dict) -> Path:
    frames = [(ts, p) for ts, p in r["frames"] if ts <= r["t_end"]]
    first = max((i for i, (ts, _) in enumerate(frames) if ts <= r["t_start"]), default=0)
    frames = frames[first:]
    lines = []
    for (ts, p), nxt in zip(frames, frames[1:] + [(r["t_end"], None)]):
        start = max(ts, r["t_start"])
        lines += [f"file '{p.as_posix()}'", f"duration {max(0.001, nxt[0] - start):.4f}"]
    lines.append(f"file '{frames[-1][1].as_posix()}'")
    (OUT / "frames.txt").write_text("\n".join(lines), encoding="utf-8")

    duration = r["t_end"] - r["t_start"]
    out = OUT / "munim-ji-demo.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(OUT / "frames.txt"),
           "-ss", f"{r['t_start'] - r['rec_start']:.3f}", "-i", str(OUT / "audio.wav"),
           "-filter_complex",
           "[0:v]fps=30,scale=1920:1080:flags=lanczos,format=yuv420p[v];"
           "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]",
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
           "-c:a", "aac", "-b:a", "160k", "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)

    srt = []
    for i, (a, b, text) in enumerate(r["subs"], start=1):
        srt.append(f"{i}\n{stamp(a - r['t_start'])} --> {stamp(b - r['t_start'])}\n{text.strip()}\n")
    (OUT / "munim-ji-demo.srt").write_text("\n".join(srt), encoding="utf-8")
    (OUT / "take.json").write_text(json.dumps(
        {"duration_s": round(duration, 1), "ttfa_ms": r["ttfa"], "turns": r["heard"],
         "frames": len(frames)}, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if not get_settings().has_speech:
        raise SystemExit("Set GNANI_API_KEY in .env first.")
    OUT.mkdir(parents=True, exist_ok=True)
    server = start_server()
    try:
        result = asyncio.run(record())
    finally:
        server.terminate()
    out = assemble(result)
    print(f"\n{out}\n{result['t_end'] - result['t_start']:.1f} s, {len(result['frames'])} frames, "
          f"time to first audio (ms): {result['ttfa']}")


if __name__ == "__main__":
    main()
