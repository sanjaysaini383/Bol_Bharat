import pytest

from munim.parser import parse


def lines(text):
    return [(d.name, d.amount, d.direction, d.day_offset) for d in parse(text).drafts]


def test_the_problem_statement_sentence():
    assert lines("Ramesh ka 340 udhaar, kal 200 diya tha") == [
        ("Ramesh", 340, "credit", 0), ("Ramesh", 200, "payment", -1)]


def test_same_sentence_in_devanagari_without_comma():
    assert lines("रमेश का 340 उधार कल 200 दिया था") == [
        ("रमेश", 340, "credit", 0), ("रमेश", 200, "payment", -1)]


@pytest.mark.parametrize("text,direction", [
    ("Ramesh ne 200 diye", "payment"),            # NE + gave: he paid
    ("Ramesh ko 200 ka saamaan diya", "credit"),  # KO + gave: goods on credit
    ("Ramesh ko 200 ka doodh", "credit"),
    ("Ramesh se 200 mile", "payment"),
    ("Ramesh ne 200 ka saamaan liya", "credit"),
    ("Ramesh ka 200 jama karo", "payment"),
    ("Ramesh ne 200 udhaar chukaya", "payment"),
    ("Ramesh ka 200 likh do", "credit"),
    ("Ramesh ke khate me 200 udhaar likh do", "credit"),
    ("Ramesh gave 200", "payment"),
    ("Ramesh took 200 on credit", "credit"),
    ("Ramesh 200", None),                         # not said: must ask
])
def test_direction(text, direction):
    assert parse(text).drafts[0].direction == direction


def test_future_tense_is_a_promise_not_a_payment():
    d = parse("Ramesh kal 200 de dega").drafts[0]
    assert (d.direction, d.day_offset) == ("promise", 1)


def test_relative_dates():
    assert lines("parso Mohan ne 100 diye")[0][3] == -2
    assert lines("3 din pehle Mohan ne 100 diye") == [("Mohan", 100, "payment", -3)]


def test_two_customers_in_one_breath():
    assert lines("Suresh ne 500 diye aur Mohan ka 200 udhaar") == [
        ("Suresh", 500, "payment", 0), ("Mohan", 200, "credit", 0)]


def test_descriptor_and_wake_phrase():
    d = parse("Munim ji, doodh wale Ramesh ka dedh sau likh do").drafts[0]
    assert (d.name, d.amount, d.descriptor) == ("Ramesh", 150, "doodh wale")


def test_honorific_is_not_part_of_the_key():
    assert parse("Gupta ji ka 340 udhaar").drafts[0].name_keys == ["gupta"]


@pytest.mark.parametrize("text,amount,direction,has_name", [
    ("nahi, 430", 430, None, False),
    ("nahi nahi, char sau tees", 430, None, False),
    ("nahi, jama tha", None, "payment", False),
    ("galat, Suresh", None, None, True),
    ("no, 430", 430, None, False),
    ("ಇಲ್ಲ, 430", 430, None, False),
])
def test_corrections(text, amount, direction, has_name):
    p = parse(text)
    assert p.intent == "correction"
    assert (p.amount, p.direction, bool(p.name_keys)) == (amount, direction, has_name)


@pytest.mark.parametrize("text,query,period", [
    ("Ramesh ka total?", "customer_balance", None),
    ("Ramesh pe kitna baaki hai", "customer_balance", None),
    ("aaj kitna udhaar gaya", "period_credit", "today"),
    ("kal kitna jama hua", "period_payment", "yesterday"),
    ("aaj ka hisaab", "day_summary", "today"),
    ("kul baaki kitna hai", "total_outstanding", None),
    ("sabse zyada kiska baaki hai", "top_debtors", None),
    ("how much does Ramesh owe", "customer_balance", None),
])
def test_queries(text, query, period):
    p = parse(text)
    assert (p.intent, p.query, p.period) == ("query", query, period)


@pytest.mark.parametrize("text,intent", [
    ("hata do", "undo"), ("cancel karo", "undo"), ("nahi, hata do", "undo"),
    ("mera sara data delete karo", "wipe"),
    ("Ramesh ko reminder bhejo", "reminder"),
    ("Ramesh reminder ke liye raazi hai", "optin"),
    ("Ramesh ko reminder band karo", "optout"),
    ("Ramesh pe byaaj laga do", "refuse_interest"),
    ("Ramesh ko dhamki wala message bhejo", "refuse_threat"),
    ("haan", "yes"), ("nahi", "no"), ("", "empty"),
])
def test_intents(text, intent):
    assert parse(text).intent == intent
