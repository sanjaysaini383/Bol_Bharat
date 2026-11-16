"""Speech clients against a local mock of the Gnani WebSocket protocol.

This checks framing, headers, commit, streaming and barge-in handling.
It does not replace a run against the real API (scripts/probe_stt.py).
"""

import asyncio
import base64
import dataclasses
import json

import pytest
from websockets.asyncio.server import serve

from munim.config import get_settings
from munim.gnani_stt import FRAME_BYTES, GnaniSTT
from munim.gnani_tts import GnaniTTS


@pytest.fixture
async def mock_gnani():
    seen = {"stt_headers": None, "frames": [], "tts_requests": []}

    async def handler(ws):
        path = ws.request.path
        if path == "/stt":
            seen["stt_headers"] = dict(ws.request.headers)
            await ws.send(json.dumps({"type": "connected"}))
            async for msg in ws:
                if isinstance(msg, bytes):
                    seen["frames"].append(len(msg))
                elif json.loads(msg).get("type") == "commit":
                    await ws.send(json.dumps({"type": "processing"}))
                    await ws.send(json.dumps({
                        "type": "transcript", "text": "रमेश का 340 उधार", "is_final": True,
                        "latency": 340, "detected_language_code": "hi-IN"}))
        else:
            async for msg in ws:
                req = json.loads(msg)
                seen["tts_requests"].append(req)
                await ws.send(json.dumps({"type": "start", "request_id": "r1"}))
                for i in range(20):
                    await ws.send(json.dumps({"type": "audio", "data": {
                        "chunk_index": i, "is_final": False,
                        "audio": base64.b64encode(b"\x01\x02" * 240).decode()}}))
                    await asyncio.sleep(0.005)
                await ws.send(json.dumps({"type": "complete", "data": {
                    "chunk_index": 20, "audio": "", "is_final": True}}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        settings = dataclasses.replace(
            get_settings(), gnani_api_key="test-key",
            stt_url=f"ws://127.0.0.1:{port}/stt", tts_url=f"ws://127.0.0.1:{port}/tts")
        yield settings, seen


async def test_stt_frames_headers_and_commit(mock_gnani):
    settings, seen = mock_gnani
    events = []
    got = asyncio.Event()

    async def on_event(e):
        events.append(e)
        if e["type"] == "transcript":
            got.set()

    stt = GnaniSTT(settings, on_event, "hi", ["रमेश कुमार", "Suresh"])
    await stt.connect()
    await stt.send_audio(b"\x00" * 3000)       # not a multiple of the frame size
    await stt.commit()
    await asyncio.wait_for(got.wait(), 2)
    await stt.close()

    h = seen["stt_headers"]
    assert h["model"] == "prisma-v3.0" and h["x-api-key-id"] == "test-key"
    assert h["language_code"] == "hi-IN,en-IN"
    assert "रमेश कुमार" in json.loads(h["keyterms"])     # names sent as \u-escaped JSON
    assert h["keyterms"].isascii()
    assert seen["frames"] and all(n == FRAME_BYTES for n in seen["frames"])
    assert sum(seen["frames"]) == 3072                   # tail padded to a full frame
    assert events[-1]["text"] == "रमेश का 340 उधार"


async def test_tts_streams_pcm(mock_gnani):
    settings, seen = mock_gnani
    tts = GnaniTTS(settings)
    chunks = [c async for c in tts.synthesize("रमेश: 340 उधार।", "hi")]
    await tts.close()
    req = seen["tts_requests"][0]
    assert req["model"] == "timbre-v3.0" and req["language"] == "hi-IN"
    assert req["audio_config"]["sample_rate"] == settings.tts_sample_rate
    assert len(chunks) == 20 and all(len(c) % 2 == 0 for c in chunks)


async def test_tts_barge_in_then_next_reply_is_clean(mock_gnani):
    settings, seen = mock_gnani
    tts = GnaniTTS(settings)
    gen = tts.synthesize("first reply", "en")
    await gen.__anext__()
    await gen.aclose()                          # user spoke over the agent
    chunks = [c async for c in tts.synthesize("second reply", "en")]
    await tts.close()
    assert len(chunks) == 20                    # no leftover audio from the first reply
    assert [r["text"] for r in seen["tts_requests"]][-1] == "second reply"
