"""One browser connection: the real-time loop.

    mic PCM --> Prisma v3.0 (stream) --> Brain --> Timbre v3.0 (stream) --> speaker

Everything is streamed over WebSockets. The server never waits for a whole
audio file in either direction.

Browser -> server
    binary              PCM16 mono 16 kHz microphone audio
    {"type":"start"}    open the session (lang, mode)
    {"type":"ptt_down"} push-to-talk pressed (also interrupts the agent)
    {"type":"ptt_up"}   released: commit the utterance immediately
    {"type":"interrupt"} barge-in: stop speaking now
    {"type":"text"}     typed utterance (keyboard fallback / testing)

Server -> browser
    binary              4-byte turn id + PCM16 mono reply audio
    stt / reply / ledger / state / metrics / audio_start / audio_end / audio_stop
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time

from fastapi import WebSocket, WebSocketDisconnect

from .config import LANGS
from .dialogue import Brain, Reply
from .gnani_stt import GnaniSTT
from .gnani_tts import GnaniTTS
from .parser import WAKE
from .textnorm import tokenize

log = logging.getLogger("munim.session")

FOLLOW_UP_WINDOW_S = 8.0     # hands-free: no wake phrase needed right after a reply
NO_SPEECH_WAIT_S = 3.0       # after release with nothing heard yet
TAIL_WAIT_S = 1.2            # after release, for the last part of a split utterance
CONTINUE_WINDOW_S = 12.0     # "kal 200 diya tha" this soon after an entry is the same customer


class Session:
    def __init__(self, ws: WebSocket, app):
        self.ws = ws
        self.app = app
        self.s = app.settings
        self.brain = Brain(app.ledger, app.llm, self.s.default_lang, self.s.big_amount,
                           self.s.shop_name)
        self.stt: GnaniSTT | None = None
        self.tts: GnaniTTS | None = None
        self.lang_pref = "auto"
        self.mode = "ptt"
        self.accepting = False
        self.turn = 0
        self.t_end: float | None = None      # when the user stopped speaking
        self.last_reply_at = 0.0
        self._speak_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._heard = 0
        self._started = False
        # Push-to-talk: one hold is one utterance. The server's VAD may close
        # it early at a pause ("... 340 udhaar, <pause> kal 200 diya tha"), so
        # transcripts that arrive while the button is down wait here.
        self._held: list[dict] = []
        self._speech_open = False            # speech_start seen, transcript still to come

    # ------------------------------------------------------------- transport
    async def send(self, payload: dict) -> None:
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
            except (RuntimeError, WebSocketDisconnect):
                pass

    async def _send_audio(self, turn: int, chunk: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(struct.pack("<I", turn) + chunk)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        await self.send({"type": "hello", "speech": self.s.has_speech, "llm": self.s.has_llm,
                         "rate": self.s.tts_sample_rate, "langs": list(LANGS),
                         "default_lang": self.s.default_lang})
        await self.send({"type": "ledger", "data": self.app.ledger.snapshot()})
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    if self.accepting and self.stt is not None:
                        await self.stt.send_audio(msg["bytes"])
                elif msg.get("text"):
                    try:
                        await self._control(json.loads(msg["text"]))
                    except ValueError:
                        continue
        except WebSocketDisconnect:
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        for t in list(self._tasks) + ([self._speak_task] if self._speak_task else []):
            t.cancel()
        if self.stt:
            await self.stt.close()
        if self.tts:
            await self.tts.close()

    # --------------------------------------------------------------- control
    async def _control(self, m: dict) -> None:
        kind = m.get("type")
        if kind == "start":
            await self._start(m.get("lang") or "auto", m.get("mode") or "ptt")
        elif kind == "mode":
            self.mode = "handsfree" if m.get("mode") == "handsfree" else "ptt"
            self.accepting = self.mode == "handsfree"
            if self.stt and not self.accepting:
                self.stt.drop_audio()
        elif kind == "ptt_down":
            await self.interrupt()
            self.accepting = True
            self._held, self._speech_open = [], False
        elif kind == "ptt_up":
            if self.mode == "ptt":
                self.accepting = False
            self.t_end = time.monotonic()
            await self.send({"type": "state", "phase": "thinking"})
            if self.stt:
                await self.stt.commit()
                if self._held and not self._speech_open:
                    self._flush_held()           # everything was already transcribed
                else:
                    self._spawn(self._no_speech_watchdog(self._heard))
        elif kind == "interrupt":
            await self.interrupt()
        elif kind == "text":
            text = str(m.get("text") or "").strip()[:500]
            if text:
                self.t_end = time.monotonic()
                self._spawn(self._turn(text, None, None, typed=True))

    async def _start(self, lang: str, mode: str) -> None:
        lang = lang if lang in LANGS else "auto"
        changed = lang != self.lang_pref or not self._started
        self.lang_pref = lang
        self.mode = "handsfree" if mode == "handsfree" else "ptt"
        self.accepting = self.mode == "handsfree"
        if lang != "auto":
            self.brain.lang = lang
        if self.s.has_speech and changed:
            if self.stt:
                await self.stt.close()
            # Customer names from the ledger are fed to the recogniser.
            self.stt = GnaniSTT(self.s, self._on_stt, lang, self.app.ledger.keyterms())
            if self.tts is None:
                self.tts = GnaniTTS(self.s)
            results = await asyncio.gather(self.stt.connect(), self.tts.connect(),
                                           return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    log.error("speech connect failed: %s", r)
                    await self.send({"type": "error",
                                     "message": f"Gnani connection failed: {r}"})
        if not self._started:
            self._started = True
            greeting = self.brain.greeting(self.brain.lang)
            await self.send(self._reply_payload(greeting))
            self.t_end = None
            self._speak(greeting)
        await self.send({"type": "state", "phase": "idle"})

    async def _no_speech_watchdog(self, heard: int) -> None:
        await asyncio.sleep(TAIL_WAIT_S if self._held else NO_SPEECH_WAIT_S)
        if self._heard == heard:            # nothing more was recognised
            if self._held:
                self._flush_held()
            else:
                await self.send({"type": "state", "phase": "idle"})

    def _flush_held(self) -> None:
        """Run one turn on everything heard during a push-to-talk hold."""
        parts, self._held = self._held, []
        if not parts:
            return
        text = " ".join(p["text"] for p in parts)
        self._spawn(self._turn(text, parts[-1].get("detected_language_code"),
                               parts[-1].get("latency")))

    # ------------------------------------------------------------ STT events
    async def _on_stt(self, e: dict) -> None:
        kind = e.get("type")
        if kind == "speech_start":
            self._speech_open = True
            await self.send({"type": "state", "phase": "listening"})
        elif kind == "processing":
            await self.send({"type": "state", "phase": "thinking"})
        elif kind == "transcript":
            self._heard += 1
            self._speech_open = False
            text = (e.get("text") or "").strip()
            if self.mode == "ptt":
                if text:
                    self._held.append({**e, "text": text})
                if self.accepting:
                    return                       # button still down: keep collecting
                if self._held:
                    self._flush_held()
                else:
                    await self.send({"type": "state", "phase": "idle"})
                return
            # Hands-free: the server's VAD ended the utterance. The live API
            # sends no ``processing`` event, so estimate the end of speech: the
            # VAD waited min_silence_ms, then the STT took ``latency`` ms.
            waited = (e.get("latency") or 0) + self.s.min_silence_ms
            self.t_end = time.monotonic() - waited / 1000
            if not text:
                await self.send({"type": "state", "phase": "idle"})
                return
            self._spawn(self._turn(text, e.get("detected_language_code"), e.get("latency")))
        elif kind == "error":
            log.warning("STT error: %s", e.get("message"))
            await self.send({"type": "error", "message": f"STT: {e.get('message')}"})

    def _addressed(self, text: str) -> bool:
        """Hands-free: act only on speech meant for the agent."""
        if self.mode != "handsfree" or self.brain.awaiting_answer:
            return True
        if time.monotonic() - self.last_reply_at < FOLLOW_UP_WINDOW_S:
            return True
        return any(t.kind == "word" and t.key in WAKE for t in tokenize(text))

    # ------------------------------------------------------------------ turn
    async def _turn(self, text: str, lang_hint: str | None, stt_ms, typed: bool = False):
        async with self._turn_lock:
            if not typed and not self._addressed(text):
                await self.send({"type": "ignored", "text": text})
                await self.send({"type": "state", "phase": "idle"})
                return
            hint = None if self.lang_pref == "auto" else self.lang_pref
            # Script decides the language; the STT tag only settles Hindi/Marathi.
            if lang_hint and lang_hint.startswith("mr"):
                hint = "mr"
            t0 = time.monotonic()
            speaking = self._speak_task is not None and not self._speak_task.done()
            followup = speaking or t0 - self.last_reply_at < CONTINUE_WINDOW_S
            try:
                reply = await self.brain.handle(text, hint, followup=followup)
            except Exception:
                log.exception("turn failed")
                await self.send({"type": "error", "message": "Internal error"})
                await self.send({"type": "state", "phase": "idle"})
                return
            brain_ms = int((time.monotonic() - t0) * 1000)
            await self.send({"type": "stt", "text": text, "lang": reply.lang, "ms": stt_ms})
            await self.send(self._reply_payload(reply))
            if reply.changed:
                await self.app.broadcast_ledger()
            self._speak(reply, stt_ms=stt_ms, brain_ms=brain_ms)

    def _reply_payload(self, r: Reply) -> dict:
        return {"type": "reply", "text": r.text, "lang": r.lang, "kind": r.kind,
                "path": r.path, "entries": r.entries, "reminder": r.reminder,
                "statement": r.statement}

    # ---------------------------------------------------------------- speech
    def _speak(self, reply: Reply, stt_ms=None, brain_ms=None) -> None:
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        self.turn += 1
        self._speak_task = asyncio.create_task(
            self._speak_run(self.turn, reply, self.t_end, stt_ms, brain_ms))

    async def _speak_run(self, turn: int, reply: Reply, t_end, stt_ms, brain_ms) -> None:
        metrics = {"type": "metrics", "turn": turn, "stt_ms": stt_ms, "brain_ms": brain_ms,
                   "path": reply.path, "ttfa_ms": None}
        if self.tts is None:
            self.last_reply_at = time.monotonic()
            await self.send(metrics)
            await self.send({"type": "state", "phase": "idle"})
            return
        await self.send({"type": "audio_start", "turn": turn, "rate": self.tts.sample_rate})
        first = True
        try:
            async for chunk in self.tts.synthesize(reply.text, reply.lang):
                if first:
                    first = False
                    if t_end is not None:
                        # Time to first audio: end of user speech -> first reply audio.
                        metrics["ttfa_ms"] = int((time.monotonic() - t_end) * 1000)
                    await self.send(metrics)
                await self._send_audio(turn, chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("TTS failed: %s", e)
            await self.send({"type": "error", "message": f"TTS: {e}"})
        finally:
            self.last_reply_at = time.monotonic()
            await self.send({"type": "audio_end", "turn": turn})

    async def interrupt(self) -> None:
        """Barge-in: stop the reply that is being spoken."""
        task = self._speak_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            await self.send({"type": "audio_stop", "turn": self.turn})
