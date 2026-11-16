"""End-to-end conversations against a real (in-memory, encrypted) ledger."""

from datetime import timedelta

from munim.dialogue import Brain
from munim.ledger import CREDIT, PAYMENT

from conftest import TODAY


def cust(ledger, name):
    return next(c for c in ledger.customers() if c.name == name)


def bal(ledger, name):
    return ledger.balance(cust(ledger, name).id)


async def test_ps_sentence_updates_ledger(brain, seeded):
    r = await brain.handle("Suresh ka 340 udhaar, kal 200 diya tha")
    assert r.kind == "entry" and r.changed
    assert bal(seeded, "Suresh") == 140
    assert [(e["kind"], e["amount"], e["date"]) for e in r.entries] == [
        (CREDIT, 340, TODAY.isoformat()),
        (PAYMENT, 200, (TODAY - timedelta(days=1)).isoformat())]
    assert "140" in r.text


async def test_first_turn_discloses_ai_in_users_language(seeded):
    brain = Brain(seeded, today=lambda: TODAY)
    r = await brain.handle("Suresh ka 100 udhaar")
    assert "AI" in r.text and "मुनीम" in r.text
    r = await brain.handle("Suresh ka 100 udhaar")
    assert "AI" not in r.text                       # only once per language
    r = await brain.handle("Suresh gave 50")
    assert r.lang == "en" and "AI" in r.text        # new language: disclose again


async def test_amount_correction(brain, seeded):
    await brain.handle("Suresh ka 340 udhaar")
    r = await brain.handle("nahi, 430")
    assert bal(seeded, "Suresh") == 430
    assert r.entries[0]["amount"] == 430


async def test_correction_picks_the_amount_it_sounds_like(brain, seeded):
    await brain.handle("Suresh ka 340 udhaar, kal 200 diya tha")
    await brain.handle("nahi, 430")                 # fixes 340, not 200
    assert bal(seeded, "Suresh") == 230


async def test_direction_correction(brain, seeded):
    await brain.handle("Suresh ka 300 udhaar")
    await brain.handle("nahi, jama tha")
    assert bal(seeded, "Suresh") == -300


async def test_name_correction_moves_the_entry(brain, seeded):
    await brain.handle("Suresh ka 300 udhaar")
    await brain.handle("nahi, Sunita")
    assert bal(seeded, "Suresh") == 0 and bal(seeded, "Sunita") == 300


async def test_shared_name_asks_then_resolves(brain, seeded):
    r = await brain.handle("Ramesh ka 500 udhaar")
    assert r.kind == "ask" and not r.changed
    assert bal(seeded, "Ramesh Kumar") == 0 and bal(seeded, "Ramesh Gupta") == 0
    r = await brain.handle("doodh wale")
    assert r.kind == "entry" and bal(seeded, "Ramesh Kumar") == 500


async def test_shared_name_resolved_by_ordinal_and_by_surname(brain, seeded):
    await brain.handle("Ramesh ka 100 udhaar")
    await brain.handle("doosra")
    assert bal(seeded, "Ramesh Gupta") == 100
    await brain.handle("Ramesh ka 50 udhaar")
    await brain.handle("Gupta")
    assert bal(seeded, "Ramesh Gupta") == 150


async def test_full_name_needs_no_question(brain, seeded):
    r = await brain.handle("Ramesh Gupta ka 100 udhaar")
    assert r.kind == "entry" and bal(seeded, "Ramesh Gupta") == 100


async def test_missing_direction_is_asked_not_guessed(brain, seeded):
    r = await brain.handle("Suresh 150")
    assert r.kind == "ask" and bal(seeded, "Suresh") == 0
    await brain.handle("jama")
    assert bal(seeded, "Suresh") == -150


async def test_undo(brain, seeded):
    await brain.handle("Suresh ka 300 udhaar")
    r = await brain.handle("hata do")
    assert r.changed and bal(seeded, "Suresh") == 0
    r = await brain.handle("hata do")
    assert not r.changed


async def test_new_customer_is_created_and_removed_on_undo(brain, seeded):
    r = await brain.handle("Mohan ne 300 diye")
    assert bal(seeded, "Mohan") == -300
    await brain.handle("hata do")
    assert all(c.name != "Mohan" for c in seeded.customers())


async def test_similar_name_is_confirmed_before_use(brain, seeded):
    r = await brain.handle("Sures ka 20 udhaar")
    assert r.kind == "ask"
    await brain.handle("haan")
    assert bal(seeded, "Suresh") == 20


async def test_similar_name_rejected_creates_new_account(brain, seeded):
    await brain.handle("Sures ka 20 udhaar")
    await brain.handle("nahi")
    assert bal(seeded, "Suresh") == 0 and bal(seeded, "Sures") == 20


async def test_large_amount_needs_confirmation(brain, seeded):
    r = await brain.handle("Suresh ka 25000 udhaar")
    assert r.kind == "ask" and bal(seeded, "Suresh") == 0
    await brain.handle("nahi, 2500")
    assert bal(seeded, "Suresh") == 2500


async def test_promise_does_not_change_balance(brain, seeded):
    await brain.handle("Suresh ka 300 udhaar")
    r = await brain.handle("Suresh kal 100 de dega")
    assert bal(seeded, "Suresh") == 300
    assert seeded.snapshot(TODAY)["promises"][0]["amount"] == 100


async def test_queries(brain, seeded):
    await brain.handle("Suresh ka 340 udhaar")
    await brain.handle("Sunita ne 100 diye")
    assert "340" in (await brain.handle("Suresh ka total?")).text
    assert "340" in (await brain.handle("aaj kitna udhaar gaya")).text
    assert "100" in (await brain.handle("aaj kitna jama hua")).text
    assert "340" in (await brain.handle("kul baaki kitna hai")).text
    assert "सुरेश" in (await brain.handle("sabse zyada kiska baaki hai")).text


async def test_hindi_script_entry_matches_latin_customer(brain, seeded):
    await brain.handle("सुरेश का तीन सौ चालीस उधार")
    assert bal(seeded, "Suresh") == 340


async def test_english_entry(brain, seeded):
    r = await brain.handle("Suresh gave three-forty")
    assert r.lang == "en" and bal(seeded, "Suresh") == -340


async def test_reminder_only_with_opt_in(brain, seeded):
    await brain.handle("Sunita ka 200 udhaar")
    r = await brain.handle("Sunita ko reminder bhejo")
    assert r.kind == "refuse" and r.reminder is None
    await brain.handle("Sunita reminder ke liye raazi hai")
    r = await brain.handle("Sunita ko reminder bhejo")
    assert r.reminder and "200" in r.reminder["text"]


async def test_refuses_interest_and_threats(brain, seeded):
    await brain.handle("Suresh ka 200 udhaar")
    for text in ("Suresh pe byaaj laga do", "Suresh ko dhamki wala message bhejo",
                 "add interest to Suresh"):
        r = await brain.handle(text)
        assert r.kind == "refuse" and not r.changed
    assert bal(seeded, "Suresh") == 200


async def test_wipe_needs_spoken_confirmation(brain, seeded):
    await brain.handle("Suresh ka 200 udhaar")
    r = await brain.handle("mera sara data delete karo")
    assert r.kind == "ask" and seeded.customers()
    await brain.handle("haan")
    assert seeded.customers() == []


async def test_noise_does_not_touch_ledger(brain, seeded):
    before = seeded.snapshot(TODAY)
    for text in ("", "hmm", "achha theek hai"):
        assert not (await brain.handle(text)).changed
    after = seeded.snapshot(TODAY)
    assert before["recent"] == after["recent"]
