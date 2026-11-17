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
        assert set(table) | {"help"} == set(T["hi"]), lang    # "help" is composed if absent


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


def test_help_exists_in_every_language():
    from munim.replies import help_text
    for lang in T:
        assert "340" in help_text(lang) and "200" in help_text(lang), lang


def test_statement_has_running_balance_newest_first(ledger):
    from datetime import date
    c = ledger.add_customer("Suresh", "सुरेश")
    ledger.add_entry(c.id, "CREDIT", 300, date(2026, 11, 14))
    ledger.add_entry(c.id, "PAYMENT", 100, date(2026, 11, 15))
    gone = ledger.add_entry(c.id, "CREDIT", 999, date(2026, 11, 15))
    ledger.set_status([gone.id], "UNDONE")
    ledger.add_entry(c.id, "CREDIT", 40, date(2026, 11, 16))
    rows = ledger.statement(c.id)
    assert [(r["kind"], r["amount"], r["balance"]) for r in rows] == [
        ("CREDIT", 40, 240), ("PAYMENT", 100, 200), ("CREDIT", 300, 300)]
    assert rows[0]["balance"] == ledger.balance(c.id)


def test_export_rows_cover_every_line(ledger):
    from datetime import date
    c = ledger.add_customer("Suresh", "सुरेश")
    ledger.add_entry(c.id, "CREDIT", 300, date(2026, 11, 14))
    e = ledger.add_entry(c.id, "PAYMENT", 100, date(2026, 11, 15))
    ledger.set_status([e.id], "UNDONE")
    rows = ledger.export_rows()
    assert [r[:5] for r in rows] == [["2026-11-14", "Suresh", "CREDIT", 300, "ACTIVE"],
                                     ["2026-11-15", "Suresh", "PAYMENT", 100, "UNDONE"]]


def test_payment_closes_the_promises_it_covers(ledger):
    from datetime import date
    c = ledger.add_customer("Suresh", "सुरेश")
    a = ledger.add_promise(c.id, 200, date(2026, 11, 17))
    b = ledger.add_promise(c.id, 500, None)
    assert ledger.keep_promises(c.id, 250) == [a]
    assert [pid for pid, *_ in ledger.open_promises()] == [b]
