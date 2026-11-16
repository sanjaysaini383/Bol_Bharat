"""Streaming speech-to-text: Gnani Prisma v3.0 over WebSocket.

Protocol (docs.gnani.ai/api/STT/stt-websocket):
* all configuration is sent as WebSocket upgrade headers
* audio is raw PCM16 mono in frames of exactly 1,024 bytes
* the server runs VAD and sends one final ``transcript`` per utterance
  (there are no partial transcripts)
* ``{"type": "commit"}`` closes the open utterance at once. Push-to-talk
  sends it on button release, so the reply does not wait out the silence
  timer. That is the biggest single latency saving in the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .config import Settings

log = logging.getLogger("munim.stt")

FRAME_BYTES = 1024
SAMPLE_RATE = 16000

# Shop vocabulary the recogniser should favour, on top of customer names.
DOMAIN_TERMS = ["उधार", "जमा", "बाकी", "खाता", "हिसाब", "udhaar", "jama", "khata",
                "मुनीम जी", "रिमाइंडर", "हटा दो"]

EventHandler = Callable[[dict], Awaitable[None]]


def language_header(lang: str | None) -> str:
    """Language hint for the session: the chosen language plus English."""
    if not lang or lang == "auto":
        return "indic-auto"
    if lang == "en":
        return "en-IN,hi-IN"
    return f"{lang}-IN,en-IN"


class GnaniSTT:
    def __init__(self, settings: Settings, on_event: EventHandler,
                 lang: str | None = None, keyterms: list[str] | None = None):
        self._s = settings
        self._on_event = on_event
        self._lang = lang
        self._keyterms = keyterms or []
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task | None = None
        self._buf = bytearray()
        self._lock = asyncio.Lock()
        self._closed = False

    def _headers(self) -> dict[str, str]:
        terms, size = [], 0
        for t in self._keyterms + DOMAIN_TERMS:
            size += len(json.dumps(t))
            if len(terms) >= 120 or size > 3000:    # keep the upgrade request small
                break
            terms.append(t)
        return {
            "x-api-key-id": self._s.gnani_api_key,
            "model": self._s.stt_model,
            "language_code": language_header(self._lang),
            "format": self._s.stt_format,
            "x-sample-rate": str(SAMPLE_RATE),
            "x-vad-threshold": str(self._s.vad_threshold),
            "x-min-silence-ms": str(self._s.min_silence_ms),
            "x-idle-timeout-sec": "900",
            # JSON with \u escapes keeps non-ASCII names valid in a header.
            "keyterms": json.dumps(terms, ensure_ascii=True),
        }

    async def connect(self) -> None:
        async with self._lock:
            if self._ws is not None or self._closed:
                return
            self._ws = await connect(self._s.stt_url, additional_headers=self._headers(),
                                     open_timeout=8, max_size=2 ** 22)
            self._reader = asyncio.create_task(self._read(self._ws))
            log.info("STT connected (%s, %d keyterms)", language_header(self._lang),
                     len(self._keyterms))

    async def _read(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                await self._on_event(event)
        except ConnectionClosed as e:
            log.info("STT closed: %s", e)
        except Exception:
            log.exception("STT reader failed")
        finally:
            if self._ws is ws:
                self._ws = None

    async def _send(self, payload: bytes | str) -> bool:
        for attempt in (1, 2):
            if self._ws is None:
                try:
                    await self.connect()
                except Exception as e:
                    log.warning("STT connect failed: %s", e)
                    return False
            try:
                await self._ws.send(payload)
                return True
            except (ConnectionClosed, AttributeError):
                self._ws = None                      # 15-minute cap or idle close
        return False

    async def send_audio(self, pcm: bytes) -> None:
        """Queue PCM16 audio and forward it in exact 1,024-byte frames."""
        self._buf.extend(pcm)
        while len(self._buf) >= FRAME_BYTES:
            frame = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            if not await self._send(frame):
                self._buf.clear()
                return

    async def commit(self) -> None:
        """End the current utterance now (push-to-talk released)."""
        if self._buf:
            self._buf.extend(b"\x00" * (FRAME_BYTES - len(self._buf)))
            await self._send(bytes(self._buf))
            self._buf.clear()
        await self._send(json.dumps({"type": "commit"}))

    def drop_audio(self) -> None:
        self._buf.clear()

    async def close(self) -> None:
        self._closed = True
        if self._reader:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
