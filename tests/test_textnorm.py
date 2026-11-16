import pytest

from munim.textnorm import detect_lang, key, name_key, romanize


@pytest.mark.parametrize("word", ["रमेश", "ರಮೇಶ್", "రమేష్", "রমেশ", "રમેશ", "രമേശ്", "Ramesh"])
def test_same_name_in_every_script(word):
    assert name_key(word) == "ramesh"


def test_spelling_variants_share_a_key():
    assert key("उधार") == key("udhaar") == key("udhar")
    assert key("जमा") == key("jama") == key("jamaa")
    assert key("हज़ार") == key("hazaar") == key("hajar")
    assert key("दिया") == key("diya") == key("dia")


def test_schwa_deletion():
    assert romanize("करना") == "karnaa"
    assert romanize("पचपन") == "pachpan"


@pytest.mark.parametrize("text,lang", [
    ("रमेश का 340 उधार", "hi"), ("ರಮೇಶ್ 340 ಉದ್ರಿ", "kn"), ("சுரேஷ் 200", "ta"),
    ("Ramesh ka 340 udhaar", "hi"), ("Ramesh gave 500", "en"), ("రమేష్ 200 జమ", "te"),
])
def test_detect_lang(text, lang):
    assert detect_lang(text) == lang


def test_bare_name_keeps_session_language():
    assert detect_lang("Suresh 150", "hi") == "hi"
    assert detect_lang("Suresh 150", "kn") == "kn"
