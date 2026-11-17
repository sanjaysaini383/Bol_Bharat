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

    async def reset_demo(self):
        from munim.seed import seed
        self.ledger.wipe_all()
        seed(self.ledger)


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


def sent(s, kind):
    return [m for m in s.ws.sent if m["type"] == kind]


# ------------------------------------------- reconnects on a serverless host
async def test_a_resumed_session_does_not_greet_again_or_disclose_twice(seeded):
    s = Session(FakeSocket(), FakeApp(seeded))
    s.brain.today = lambda: TODAY
    await s._control({"type": "start", "lang": "hi", "mode": "ptt", "resume": True})
    assert sent(s, "reply") == []                     # no greeting on a resume
    await s._control({"type": "text", "text": "Suresh ka 340 udhaar"})
    await settle(s)
    assert "AI" not in sent(s, "reply")[-1]["text"]


async def test_a_new_session_still_greets_with_the_disclosure(seeded):
    s = Session(FakeSocket(), FakeApp(seeded))
    await s._control({"type": "start", "lang": "hi", "mode": "ptt"})
    assert "AI" in sent(s, "reply")[0]["text"]


# ---------------------------- ledger requests use the session's own socket
async def test_customer_page_over_the_socket(session, seeded):
    sid = next(c.id for c in seeded.customers() if c.name == "Suresh")
    await session._control({"type": "text", "text": "Suresh ka 340 udhaar"})
    await settle(session)
    await session._control({"type": "customer", "id": sid})
    page = sent(session, "customer")[-1]["data"]
    assert page["name"] == "Suresh" and page["balance"] == 340
    await session._control({"type": "customer", "id": "nonsense"})
    assert sent(session, "customer")[-1]["data"] is None


async def test_export_purge_and_reset_over_the_socket(session, seeded):
    await session._control({"type": "text", "text": "Suresh ka 340 udhaar"})
    await settle(session)
    await session._control({"type": "export"})
    csv = sent(session, "export")[-1]
    assert csv["filename"].endswith(".csv") and "Suresh,CREDIT,340" in csv["csv"]
    await session._control({"type": "purge"})
    assert sent(session, "purged")[-1]["deleted"] >= 1
    await session._control({"type": "reset"})
    assert {c.name for c in seeded.customers()} >= {"Ramesh Kumar", "Imran"}
