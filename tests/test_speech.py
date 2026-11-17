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
from munim.gnani_stt import FRAME_BYTES, GnaniSTT, hint_terms, language_header
from munim.gnani_tts import GnaniTTS


@pytest.fixture
async def mock_gnani():
    seen = {"stt_headers": None, "frames": [], "tts_requests": [], "stt_connects": 0}

    async def handler(ws):
        path = ws.request.path
        if path == "/stt":
            seen["stt_connects"] += 1
            seen["stt_headers"] = dict(ws.request.headers)
            if "," in ws.request.headers.get("language_code", ""):
                # What the live API does (checked 2026-11-16), despite the docs.
                await ws.send(json.dumps({"type": "error", "message":
                    "Invalid language_code: exactly one locale is required (e.g. hi-IN)."}))
                await ws.close(1008)
                return
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
                # The live server sends two completes per reply (checked 2026-11-16).
                await ws.send(json.dumps({"type": "complete", "data": {
                    "chunk_index": 20, "audio": "", "is_final": True}}))
                await ws.send(json.dumps({"type": "complete", "message": "Streaming completed",
                                          "request_id": "r1", "data": None}))

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
    assert h["language_code"] == "hi-IN"                 # one locale: the live API rejects lists
    assert "रमेश कुमार" in json.loads(h["keyterms"])     # names sent as \u-escaped JSON
    assert h["keyterms"].isascii()
    assert seen["frames"] and all(n == FRAME_BYTES for n in seen["frames"])
    assert sum(seen["frames"]) == 3072                   # tail padded to a full frame
    assert events[-1]["text"] == "रमेश का 340 उधार"


@pytest.mark.parametrize("lang,header", [("hi", "hi-IN"), ("en", "en-IN"), ("kn", "kn-IN"),
                                         ("auto", "indic-auto"), (None, "indic-auto")])
def test_language_header_is_a_single_locale(lang, header):
    assert language_header(lang) == header


async def test_stt_rejected_stream_backs_off(mock_gnani, monkeypatch):
    """A config error must not turn into a reconnect loop while the mic is open."""
    settings, seen = mock_gnani
    monkeypatch.setattr("munim.gnani_stt.language_header", lambda lang: "hi-IN,en-IN")
    errors = []

    async def on_event(e):
        if e["type"] == "error":
            errors.append(e)

    stt = GnaniSTT(settings, on_event, "hi")
    for _ in range(40):                          # ~1.3 s of mic audio at real-time pace
        await stt.send_audio(b"\x00" * FRAME_BYTES)
        await asyncio.sleep(0.032)
    await stt.close()
    assert seen["stt_connects"] == 1 and len(errors) == 1


async def test_tts_streams_pcm(mock_gnani):
    settings, seen = mock_gnani
    tts = GnaniTTS(settings)
    chunks = [c async for c in tts.synthesize("रमेश: 340 उधार।", "hi")]
    await tts.close()
    req = seen["tts_requests"][0]
    assert req["model"] == "timbre-v3.0" and req["language"] == "hi-IN"
    assert req["audio_config"]["sample_rate"] == settings.tts_sample_rate
    assert len(chunks) == 20 and all(len(c) % 2 == 0 for c in chunks)


async def test_tts_back_to_back_replies_on_one_socket(mock_gnani):
    """The second complete of reply 1 must not end reply 2 before it starts."""
    settings, seen = mock_gnani
    tts = GnaniTTS(settings)
    first = [c async for c in tts.synthesize("greeting", "hi")]
    second = [c async for c in tts.synthesize("सुरेश: 340 उधार।", "hi")]
    third = [c async for c in tts.synthesize("third", "hi")]
    await tts.close()
    assert len(first) == len(second) == len(third) == 20


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


# Hints decide the language of the transcript (found while recording the demo:
# Hindi hints turned a Kannada entry into Hindi words).
NAMES = ["सुरेश", "Suresh", "रमेश गुप्ता", "Ramesh Gupta"]


def test_hindi_english_and_auto_keep_the_measured_hints():
    for lang in ("hi", "en", "auto", None):
        terms = hint_terms(lang, NAMES)
        assert terms[:4] == NAMES and "उधार" in terms and "udhaar" in terms


def test_other_languages_get_their_own_words_and_names_in_their_script():
    terms = hint_terms("kn", NAMES)
    assert terms[0] == "ಉದ್ರಿ" and "ಸಾಲ" in terms
    assert "ಸುರೇಶ" in terms and "ರಮೇಶ ಗುಪ್ತಾ" in terms and "Suresh" in terms
    assert not any("ऀ" <= ch <= "ॿ" for term in terms for ch in term)   # no Devanagari


def test_a_script_that_cannot_spell_a_name_keeps_the_latin_spelling():
    terms = hint_terms("ta", ["घनश्याम", "Ghanshyam", "सुरेश", "Suresh"])   # Tamil has no gha
    assert "Ghanshyam" in terms and "Suresh" in terms
    assert all(term.isascii() or "஀" <= term[0] <= "௿" for term in terms)


def test_marathi_keeps_devanagari_names():
    assert "सुरेश" in hint_terms("mr", NAMES) and "हिशोब" in hint_terms("mr", NAMES)


async def test_session_language_reaches_the_keyterms_header(mock_gnani):
    settings, seen = mock_gnani
    stt = GnaniSTT(settings, lambda e: asyncio.sleep(0), "kn", NAMES)
    await stt.connect()
    await stt.close()
    sent = json.loads(seen["stt_headers"]["keyterms"])
    assert seen["stt_headers"]["language_code"] == "kn-IN"
    assert "ಉದ್ರಿ" in sent and "उधार" not in sent and "सुरेश" not in sent
