"""Streaming text-to-speech: Gnani Timbre v3.0 over WebSocket.

Protocol (docs.gnani.ai/api/TTS/tts-websocket):
* one JSON request per sentence, audio returns as base64 chunks
* text cannot be streamed in increments, so replies are kept to one short
  sentence and sent in a single request
* there is no cancel message: barge-in closes the socket. A fresh socket is
  opened straight away so the next reply does not pay for the handshake.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import AsyncIterator

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .config import Settings
from .replies import TTS_LANG

log = logging.getLogger("munim.tts")


def _strip_wav(chunk: bytes) -> bytes:
    """Return raw PCM if a chunk arrives wrapped in a WAV header."""
    if chunk[:4] == b"RIFF":
        i = chunk.find(b"data")
        if i != -1:
            return chunk[i + 8:]
    return chunk


class GnaniTTS:
    def __init__(self, settings: Settings):
        self._s = settings
        self._ws: ClientConnection | None = None
        self._lock = asyncio.Lock()
        self._warm: asyncio.Task | None = None
        self._closed = False

    @property
    def sample_rate(self) -> int:
        return self._s.tts_sample_rate

    async def connect(self) -> None:
        async with self._lock:
            if self._ws is not None or self._closed:
                return
            self._ws = await connect(
                self._s.tts_url, open_timeout=8, max_size=2 ** 24,
                additional_headers={"X-API-Key-ID": self._s.gnani_api_key,
                                    "Content-Type": "application/json"})

    def _rewarm(self) -> None:
        async def go():
            try:
                await self.connect()
            except Exception as e:
                log.warning("TTS reconnect failed: %s", e)
        if not self._closed:
            self._warm = asyncio.create_task(go())

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def synthesize(self, text: str, lang: str) -> AsyncIterator[bytes]:
        """Yield PCM16 mono chunks at ``sample_rate`` for one reply."""
        request = json.dumps({
            "text": text,
            "model": self._s.tts_model,
            "voice": self._s.tts_voice,
            "language": TTS_LANG.get(lang, "hi-IN"),
            "speed": self._s.tts_speed,
            "audio_config": {"sample_rate": self._s.tts_sample_rate, "num_channels": 1,
                             "sample_width": 2, "encoding": "linear_pcm",
                             "container": "raw"},
        }, ensure_ascii=False)

        for attempt in (1, 2):                    # one retry on a stale socket
            if self._ws is None:
                await self.connect()
            try:
                await self._ws.send(request)
                break
            except ConnectionClosed:
                self._ws = None
                if attempt == 2:
                    raise

        ws, done, carry = self._ws, False, b""
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    chunk = raw
                else:
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "error":
                        raise RuntimeError(f"TTS error: {msg.get('message')}")
                    data = msg.get("data") or {}
                    if kind == "complete" or (data.get("is_final") and not data.get("audio")):
                        done = True
                        break
                    if kind != "audio" or not data.get("audio"):
                        continue
                    chunk = base64.b64decode(data["audio"])
                chunk = carry + _strip_wav(chunk)
                carry = chunk[len(chunk) - (len(chunk) % 2):]   # keep samples whole
                chunk = chunk[:len(chunk) - len(carry)]
                if chunk:
                    yield chunk
        finally:
            if not done:
                # Interrupted (barge-in) or failed: this socket still has audio
                # in flight, so discard it and warm a new one.
                if self._ws is ws:
                    await self._drop()
                self._rewarm()

    async def close(self) -> None:
        self._closed = True
        if self._warm:
            self._warm.cancel()
        await self._drop()
