import os
import sqlite3
from datetime import date

import pytest

from munim.guardrails import pressure_words, reminder_allowed
from munim.ledger import CORRECTED, CREDIT, PAYMENT, Ledger
from munim.llm import to_parse
from munim.replies import T, say

D = date(2026, 11, 16)


def test_names_and_transcripts_are_encrypted_on_disk(tmp_path):
    path = tmp_path / "khata.db"
    led = Ledger(path, os.urandom(32))
    led.add_customer("Ramesh Kumar", "रमेश कुमार", "दूध वाले")
    led.log_utterance("Ramesh ka 340 udhaar", "hi")
    raw = path.read_bytes()
    for secret in (b"Ramesh", "रमेश".encode(), "दूध".encode(), b"udhaar"):
        assert secret not in raw


def test_wrong_key_cannot_read(tmp_path):
    path = tmp_path / "khata.db"
    Ledger(path, os.urandom(32)).add_customer("Ramesh")
    with pytest.raises(Exception):
        Ledger(path, os.urandom(32))


def test_balance_ignores_corrected_entries(ledger):
    c = ledger.add_customer("Ramesh")
    e = ledger.add_entry(c.id, CREDIT, 340, D)
    ledger.add_entry(c.id, PAYMENT, 200, D)
    assert ledger.balance(c.id) == 140
    ledger.set_status([e.id], CORRECTED)
    ledger.add_entry(c.id, CREDIT, 430, D, replaces=e.id)
    assert ledger.balance(c.id) == 230


def test_amount_must_be_positive(ledger):
    c = ledger.add_customer("Ramesh")
    with pytest.raises(ValueError):
        ledger.add_entry(c.id, CREDIT, 0, D)


def test_find_exact_and_similar(seeded):
    exact, _ = seeded.find(["ramesh"])
    assert len(exact) == 2
    exact, _ = seeded.find(["ramesh", "gupta"])
    assert [c.name for c in exact] == ["Ramesh Gupta"]
    exact, similar = seeded.find(["sures"])
    assert not exact and [c.name for c in similar] == ["Suresh"]
    assert seeded.find(["mohan"]) == ([], [])


def test_keyterms_include_both_scripts(seeded):
    terms = seeded.keyterms()
    assert "सुरेश" in terms and "Suresh" in terms


def test_purge_and_wipe(seeded):
    seeded.log_utterance("hello", "en")
    assert seeded.purge_transcripts(everything=True) == 1
    seeded.wipe_all()
    assert seeded.customers() == [] and seeded.snapshot(D)["recent"] == []


# ---- guardrails -----------------------------------------------------------

def test_every_reminder_template_is_polite():
    for lang in T:
        text = say(lang, "reminder_text", name="Ramesh", shop="Kirana Store", amt=340)
        assert pressure_words(text) == [], (lang, pressure_words(text))


def test_reminder_gate():
    assert reminder_allowed(True, "Please pay when convenient")[0]
    assert not reminder_allowed(False, "Please pay when convenient")[0]
    assert not reminder_allowed(True, "Pay immediately or we go to the police")[0]
    assert not reminder_allowed(True, "तुरंत पैसे दो वरना पुलिस")[0]


def test_all_languages_have_all_reply_keys():
    for lang, table in T.items():
        assert set(table) == set(T["hi"]), lang


# ---- LLM output validation --------------------------------------------------

def test_llm_amount_must_appear_in_transcript():
    data = {"intent": "entry", "entries": [
        {"customer": "ರಮೇಶ್", "amount": 340, "direction": "credit", "day_offset": 0}]}
    assert to_parse(data, "ರಮೇಶ್ 340 ಉದ್ರಿ").drafts[0].amount == 340
    data["entries"][0]["amount"] = 3400          # hallucinated
    assert to_parse(data, "ರಮೇಶ್ 340 ಉದ್ರಿ") is None


def test_llm_cannot_date_a_payment_in_the_future_or_invent_intents():
    data = {"intent": "entry", "entries": [
        {"customer": "Ramesh", "amount": 200, "direction": "payment", "day_offset": 3}]}
    assert to_parse(data, "Ramesh 200").drafts[0].day_offset == 0
    assert to_parse({"intent": "transfer_money"}, "x") is None
