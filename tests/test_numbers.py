import pytest

from munim.numbers import _HI_0_99, find_amounts

CASES = {
    # digits in every form the STT may produce
    "340": [340], "₹450": [450], "1,200": [1200], "२००": [200], "௩௪௦": [340],
    "300 40": [340], "3 सौ 40": [340], "2 hazaar 500": [2500], "3:40": [340],
    # Hindi words
    "teen sau chaalis": [340], "तीन सौ चालीस": [340], "pachpan rupaye": [55],
    "ninyanve": [99], "barah sau pachas": [1250], "ek hazaar do sau": [1200],
    "do hazaar paanch sau": [2500], "ek lakh bees hazaar": [120000],
    "gyarah sau": [1100], "पच्चीस सौ": [2500], "unnis": [19], "saat sau": [700],
    # fractions
    "dedh sau": [150], "dhai hazaar": [2500], "saade teen sau": [350],
    "sava sau": [125], "paune do sau": [175], "सवा दो सौ": [225],
    "saade paanch hazaar": [5500], "डेढ़ हज़ार": [1500],
    # the recogniser wrote half of it as digits (seen in the 5 dB noise eval)
    "सवाल 200 udhaar": [225], "sava 200": [225], "साढ़े 400": [450],
    "paune 300": [275], "saade 2000": [2500], "sawal kya hai": [],
    # English
    "three-forty": [340], "three forty five": [345], "twelve fifty": [1250],
    "three hundred and forty five": [345], "two thousand": [2000],
    "five hundred": [500], "ninety nine": [99],
    # words that only look like numbers
    "likh do": [], "hata do": [], "teen sau do": [300],
    "Ramesh ke saath 200": [200], "ek baar batao": [],
    # several amounts in one breath
    "Ramesh ka 340 udhaar, kal 200 diya tha": [340, 200],
    "teen sau do sau": [300, 200],
    "paanch sau ka saamaan": [500], "Ramesh ko do rupaye": [2],
}


@pytest.mark.parametrize("text,expected", CASES.items())
def test_amounts(text, expected):
    assert find_amounts(text) == expected


def test_all_hindi_numbers_round_trip():
    for value, word in enumerate(_HI_0_99[1:], start=1):
        assert find_amounts(f"{word} रुपये") == [value], word
