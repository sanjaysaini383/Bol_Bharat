"""Spoken amounts -> integers.

Handles the forms a shopkeeper actually uses, in one pass over the tokens:

* digits                       340, 1,200, ३४०, "300 40", "3 सौ 40"
* Hindi words (all 0-99 are irregular)   teen sau chaalis, बारह सौ
* fractions                    dedh sau (150), dhai hazaar (2500),
                               saade teen sau (350), sava sau (125),
                               paune do sau (175)
* English                      three hundred forty, three-forty, twelve fifty
"""

from __future__ import annotations

from .textnorm import Tok, key

_HI_0_99 = (
    "शून्य एक दो तीन चार पांच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह "
    "सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस "
    "उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस "
    "चालीस इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस "
    "उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर "
    "तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी इक्यासी बयासी "
    "तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे इक्यानवे बानवे "
    "तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()

# Common alternate spellings, Latin and Devanagari.
_HI_VARIANTS = {
    0: "shunya zero sifar", 1: "ek", 2: "do", 3: "teen tin",
    4: "char chaar", 5: "panch paanch पाँच", 6: "chhe che chah chhah cheh छः छे छै",
    7: "saat sat", 8: "aath ath", 9: "nau", 10: "das dus",
    11: "gyarah gyara igyarah", 12: "barah bara", 13: "terah tera",
    14: "chaudah chauda chodah", 15: "pandrah pandra pandhra पन्द्रह",
    16: "solah sola", 17: "satrah satra", 18: "atharah athara attharah",
    19: "unnis unnees", 20: "bees bis", 21: "ikkis ikkees",
    22: "bais baees baais", 23: "teis teyis tais", 24: "chaubis chobis",
    25: "pachis pachees pacchis pachchis", 26: "chhabbis chabbis",
    27: "sattais sataais", 28: "atthais athais", 29: "untis unatis",
    30: "tees tis", 31: "iktis ikattis", 32: "battis", 33: "taintis tetis",
    34: "chauntis chautis", 35: "paintis petis", 36: "chhattis chattis",
    37: "saintis", 38: "adtis artis", 39: "untalis", 40: "chalis chaalis",
    41: "iktalis", 42: "bayalis byalis", 43: "taintalis tetalis",
    44: "chavalis chauvalis chawalis", 45: "paintalis petalis",
    46: "chhiyalis chiyalis", 47: "saintalis", 48: "adtalis artalis",
    49: "unchas", 50: "pachas pachaas", 51: "ikyavan ikyawan",
    52: "bavan bawan", 53: "tirpan", 54: "chauvan chauwan", 55: "pachpan",
    56: "chhappan chappan", 57: "sattavan satawan", 58: "atthavan athawan",
    59: "unsath", 60: "saath sath", 61: "iksath", 62: "basath",
    63: "tirsath", 64: "chaunsath chausath", 65: "painsath", 66: "chhiyasath",
    67: "sadsath sarsath", 68: "adsath arsath", 69: "unhattar", 70: "sattar",
    71: "ikhattar", 72: "bahattar", 73: "tihattar", 74: "chauhattar",
    75: "pachhattar pachattar", 76: "chhihattar", 77: "sathattar",
    78: "athhattar", 79: "unasi unyasi", 80: "assi", 81: "ikyasi",
    82: "bayasi", 83: "tirasi", 84: "chaurasi", 85: "pachasi",
    86: "chhiyasi", 87: "sattasi", 88: "atthasi athasi", 89: "navasi nawasi",
    90: "nabbe nabe navve", 91: "ikyanve ikyanave", 92: "banve banave",
    93: "tiranve tiranave", 94: "chauranve", 95: "pachanve pachanave",
    96: "chhiyanve", 97: "sattanve", 98: "atthanve", 99: "ninyanve ninyanave",
}

_EN = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}

UNITS: dict[str, tuple[float, str]] = {}      # key -> (value, "hi" | "en")
for _v, _w in enumerate(_HI_0_99):
    UNITS.setdefault(key(_w), (_v, "hi"))
for _v, _ws in _HI_VARIANTS.items():
    for _w in _ws.split():
        UNITS.setdefault(key(_w), (_v, "hi"))
for _k in list(UNITS):                        # gyarah -> gyara
    if _k.endswith("h") and len(_k) > 3:
        UNITS.setdefault(_k[:-1], UNITS[_k])
for _w, _v in _EN.items():
    UNITS.setdefault(key(_w), (_v, "en"))
for _w in "dedh derh डेढ़ डेढ".split():
    UNITS[key(_w)] = (1.5, "hi")
for _w in "dhai adhai ढाई अढ़ाई".split():
    UNITS[key(_w)] = (2.5, "hi")


def _keys(words: str) -> set[str]:
    return {key(w) for w in words.split()}


MULT: dict[str, int] = {}
for _ws, _m in (
    ("sau sou sao so सौ hundred hundreds", 100),
    ("hazaar hazar hajar hajaar हज़ार हजार thousand thousands", 1000),
    ("lakh lac lakhs laakh लाख", 100_000),
    ("crore karod करोड़ करोड", 10_000_000),
):
    for _w in _ws.split():
        MULT[key(_w)] = _m

FRACMOD = {}
for _ws, _d in (("saade sadhe saadhe sade साढ़े साढे", 0.5),
                ("sava sawa savaa सवा sawal saval सवाल", 0.25),
                ("paune pone paun पौने", -0.25)):
    for _w in _ws.split():
        FRACMOD[key(_w)] = _d

CURRENCY = _keys("rupaye rupay rupaiye rupee rupees rs rupiya rupya rupaya "
                 "रुपये रुपए रुपया रूपये रुपयों paise पैसे")

# Words that are numbers only next to other number words:
#   do   = 2 / "give"        ek   = 1 / "a"       so = sau / English "so"
#   tera = 13 / "your"       bara = 12 / "big"
_AMBIGUOUS = _keys("do ek so tera bara")
_SIXTY_OR_WITH = key("saath")
_AND = key("and")


def _classify(t: Tok):
    if t.kind == "num":
        return ("d", t.value, "")
    if t.kind != "word":
        return None
    if t.key in MULT:
        return ("m", MULT[t.key], "")
    if t.key in FRACMOD:
        return ("f", FRACMOD[t.key], "")
    if t.key in UNITS:
        v, lang = UNITS[t.key]
        return ("u", v, lang)
    return None


def _numberish(t: Tok | None) -> bool:
    return t is not None and (_classify(t) is not None or t.key in CURRENCY)


def _is_number_start(toks: list[Tok], i: int) -> bool:
    t = toks[i]
    c = _classify(t)
    if c is None:
        return False
    prev = toks[i - 1] if i > 0 else None
    nxt = toks[i + 1] if i + 1 < len(toks) else None
    if t.kind == "word" and t.key in _AMBIGUOUS:
        if t.key == key("so"):
            return False                     # "so" is only a multiplier mid-span
        strong_next = nxt is not None and (
            nxt.key in CURRENCY or (_classify(nxt) is not None
                                    and nxt.key not in _AMBIGUOUS))
        return strong_next
    if t.kind == "word" and t.key == _SIXTY_OR_WITH and prev is not None \
            and prev.key in ("ke", "k"):
        return False                         # "ke saath" = with
    return True


def _lowest_place(n: float) -> int:
    n, p = int(n), 1
    while n and n % (p * 10) == 0:
        p *= 10
    return p


def _parse_span(toks: list[Tok], i: int) -> tuple[float, int]:
    """Parse one number starting at toks[i]. Returns (value, tokens used)."""
    total, cur = 0.0, None
    tail_at = None          # index of a small addend after a round hundred
    en_small = False        # last unit was English 1-19 on its own
    used = 0
    j = i
    n = len(toks)
    while j < n:
        t = toks[j]
        c = _classify(t)
        if c is None:
            if t.kind == "word" and t.key == _AND and cur is not None \
                    and j + 1 < n and _classify(toks[j + 1]) is not None:
                j += 1
                continue
            break
        cls, v, lang = c
        if j > i and t.kind == "word" and t.key in _AMBIGUOUS and cls == "u"                 and not _numberish(toks[j + 1] if j + 1 < n else None):
            break                # "teen sau do" = "give 300", not 302
        if cls == "f":
            if cur is not None or j + 1 >= n:
                break
            nc = _classify(toks[j + 1])
            if nc is None:
                break
            if nc[0] == "m":
                cur = 1 + v          # paune sau = 75, sava sau = 125
            elif nc[0] in ("u", "d") and nc[1] < 100:
                cur = nc[1] + v      # saade teen = 3.5
                j += 1
            elif nc[0] == "d" and nc[1] % 100 == 0:
                # The recogniser wrote part of it as digits: "sava 200" for
                # "sava do sau" (225), "saade 2000" for 2500.
                cur = nc[1] + v * _lowest_place(nc[1])
                j += 1
            else:
                break
            en_small = False
        elif cls == "u":
            if cur is None:
                cur, tail_at = v, None
                en_small = lang == "en" and 1 <= v <= 19
            elif cur >= 100 and cur % 100 == 0 and v < 100 and v == int(v):
                cur, tail_at = cur + v, j
                en_small = False
            elif en_small and lang == "en" and 10 <= v <= 99:
                cur, tail_at = cur * 100 + v, None   # "three forty" = 340
                en_small = False
            elif lang == "en" and 1 <= v <= 9 and cur % 10 == 0 \
                    and 20 <= cur % 100 <= 90:
                cur += v                              # "forty five"
            else:
                break
        elif cls == "d":
            if cur is None:
                cur, tail_at = v, None
            elif cur >= 100 and 0 < v < _lowest_place(cur):
                cur, tail_at = cur + v, (j if v < 100 else None)
            else:
                break
            en_small = False
        else:  # multiplier
            if v == 100:
                if cur is None:
                    cur = 100
                elif cur < 100:
                    cur = cur * 100
                elif tail_at is not None:
                    # "teen sau do sau": the "do" starts a second number.
                    used = tail_at - i
                    return total + cur - toks_value(toks[tail_at]), used
                else:
                    break
            else:
                base = 1 if cur is None else cur
                if base >= v:
                    break
                total += base * v
                cur = None
            tail_at, en_small = None, False
        j += 1
        used = j - i
    return total + (cur or 0.0), used


def toks_value(t: Tok) -> float:
    c = _classify(t)
    return c[1] if c else 0.0


def merge_numbers(toks: list[Tok]) -> list[Tok]:
    """Collapse every spoken or written number into a single ``num`` token."""
    out: list[Tok] = []
    i, n = 0, len(toks)
    while i < n:
        if _is_number_start(toks, i):
            value, used = _parse_span(toks, i)
            if used > 0 and value > 0:
                span = toks[i:i + used]
                out.append(Tok(
                    " ".join(t.orig for t in span), "", "num",
                    float(int(value + 0.5)),
                    spelled=any(t.kind == "word" for t in span),
                ))
                i += used
                continue
        t = toks[i]
        if t.kind == "num":
            out.append(Tok(t.orig, "", "num", float(int(t.value + 0.5))))
        else:
            out.append(t)
        i += 1
    return out


def find_amounts(text: str) -> list[int]:
    """All numbers in a transcript (used to validate LLM output)."""
    from .textnorm import tokenize
    return [int(t.value) for t in merge_numbers(tokenize(text)) if t.kind == "num"]
