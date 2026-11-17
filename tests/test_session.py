"""Session behaviour with the speech services replaced by stubs."""

import asyncio
import dataclasses
import json

import pytest

from munim.config import get_settings
from munim.session import Session

from conftest import TODAY


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        pass


class FakeSTT:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    def drop_audio(self):
        pass

    async def close(self):
        pass


class FakeApp:
    def __init__(self, ledger):
        self.settings = dataclasses.replace(get_settings(), gnani_api_key="", llm_api_key="")
        self.ledger = ledger
        self.llm = None

    async def broadcast_ledger(self):
        pass


@pytest.fixture
def session(seeded, monkeypatch):
    monkeypatch.setattr("munim.session.NO_SPEECH_WAIT_S", 0.05)
    monkeypatch.setattr("munim.session.TAIL_WAIT_S", 0.05)
    s = Session(FakeSocket(), FakeApp(seeded))
    s.brain.today = lambda: TODAY
    s.brain.greeting("hi")
    s.stt = FakeSTT()
    return s


async def settle(s):
    while s._tasks:
        await asyncio.gather(*list(s._tasks))


def lines(ledger):
    return sorted((e["customer"], e["kind"], e["amount"])
                  for e in ledger.snapshot(TODAY)["recent"] if e["status"] == "ACTIVE")


def heard(s):
    return [m["text"] for m in s.ws.sent if m["type"] == "stt"]


async def test_a_pause_inside_one_hold_is_still_one_utterance(session, seeded):
    """The server VAD splits "340 udhaar, <pause> kal 200 diya tha" in two."""
    s = session
    await s._control({"type": "ptt_down"})
    await s._on_stt({"type": "speech_start"})
    await s._on_stt({"type": "transcript", "text": "सुरेश का 340 udhaar।"})
    await settle(s)
    assert lines(seeded) == []                      # nothing is saved mid-hold
    await s._on_stt({"type": "speech_start"})
    await s._control({"type": "ptt_up"})
    await s._on_stt({"type": "transcript", "text": "कल 200 दिया था।"})
    await settle(s)
    assert heard(s) == ["सुरेश का 340 udhaar। कल 200 दिया था।"]
    assert lines(seeded) == [("Suresh", "CREDIT", 340), ("Suresh", "PAYMENT", 200)]


async def test_release_after_everything_was_transcribed_answers_at_once(session, seeded):
    s = session
    await s._control({"type": "ptt_down"})
    await s._on_stt({"type": "speech_start"})
    await s._on_stt({"type": "transcript", "text": "Suresh ka 340 udhaar"})
    await s._control({"type": "ptt_up"})            # no speech in flight: no waiting
    await settle(s)
    assert s.stt.commits == 1 and lines(seeded) == [("Suresh", "CREDIT", 340)]


async def test_a_hold_with_no_speech_goes_back_to_idle(session, seeded):
    s = session
    await s._control({"type": "ptt_down"})
    await s._control({"type": "ptt_up"})
    await s._on_stt({"type": "transcript", "text": ""})
    await settle(s)
    assert heard(s) == [] and lines(seeded) == []


async def test_hands_free_answers_each_utterance_and_carries_the_customer(session, seeded):
    s = session
    await s._control({"type": "mode", "mode": "handsfree"})
    await s._on_stt({"type": "transcript", "text": "Munim ji, Suresh ka 340 udhaar"})
    await settle(s)
    await s._on_stt({"type": "transcript", "text": "kal 200 diya tha"})
    await settle(s)
    assert len(heard(s)) == 2
    assert lines(seeded) == [("Suresh", "CREDIT", 340), ("Suresh", "PAYMENT", 200)]
