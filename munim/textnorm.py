"""Script-independent text normalisation.

Prisma returns Hindi in Devanagari, Tamil in Tamil script, English in Latin,
and so on. Everything downstream (number words, shop vocabulary, customer
names) is matched on one romanised "key", so the same word matches however
it was written: उधार, udhaar and udhar all give the key ``udhar``.

The nine Indic Unicode blocks share one layout (inherited from ISCII), so a
character from any of them maps onto Devanagari by its offset in the block.
One romaniser therefore covers all ten supported languages.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

INDIC_START, INDIC_END = 0x0900, 0x0D7F

BLOCK_LANG = {
    0x0900: "hi", 0x0980: "bn", 0x0A00: "pa", 0x0A80: "gu",
    0x0B80: "ta", 0x0C00: "te", 0x0C80: "kn", 0x0D00: "ml",
}

_VOWELS = {
    0x05: "a", 0x06: "aa", 0x07: "i", 0x08: "ee", 0x09: "u", 0x0A: "oo",
    0x0B: "ri", 0x0D: "e", 0x0E: "e", 0x0F: "e", 0x10: "ai", 0x11: "o",
    0x12: "o", 0x13: "o", 0x14: "au",
}
_CONS = {
    0x15: "k", 0x16: "kh", 0x17: "g", 0x18: "gh", 0x19: "n", 0x1A: "ch",
    0x1B: "chh", 0x1C: "j", 0x1D: "jh", 0x1E: "n", 0x1F: "t", 0x20: "th",
    0x21: "d", 0x22: "dh", 0x23: "n", 0x24: "t", 0x25: "th", 0x26: "d",
    0x27: "dh", 0x28: "n", 0x29: "n", 0x2A: "p", 0x2B: "ph", 0x2C: "b",
    0x2D: "bh", 0x2E: "m", 0x2F: "y", 0x30: "r", 0x31: "r", 0x32: "l",
    0x33: "l", 0x34: "l", 0x35: "v", 0x36: "sh", 0x37: "sh", 0x38: "s",
    0x39: "h",
    0x58: "k", 0x59: "kh", 0x5A: "g", 0x5B: "z", 0x5C: "r", 0x5D: "rh",
    0x5E: "f", 0x5F: "y",
}
_MATRAS = {
    0x3E: "aa", 0x3F: "i", 0x40: "ee", 0x41: "u", 0x42: "oo", 0x43: "ri",
    0x45: "e", 0x46: "e", 0x47: "e", 0x48: "ai", 0x49: "o", 0x4A: "o",
    0x4B: "o", 0x4C: "au",
}
_NASALS = {0x01: "n", 0x02: "n", 0x70: "n"}
_NUKTA = {"j": "z", "ph": "f", "d": "r", "dh": "rh", "s": "sh"}
_ML_CHILLU = {0x7A: "n", 0x7B: "n", 0x7C: "r", 0x7D: "l", 0x7E: "l", 0x7F: "k"}
_VIRAMA, _NUKTA_SIGN, _VISARGA = 0x4D, 0x3C, 0x03


def _is_indic(ch: str) -> bool:
    return INDIC_START <= ord(ch) <= INDIC_END


def _romanize_word(word: str) -> str:
    # Each unit is [consonant, vowel]. vowel None = inherent "a", "" = none.
    units: list[list] = []
    for ch in word:
        cp = ord(ch)
        if not (INDIC_START <= cp <= INDIC_END):
            units.append([ch.lower(), ""])
            continue
        off = cp & 0x7F
        block = cp & 0xFF80
        if block == 0x0D00 and off in _ML_CHILLU:
            units.append([_ML_CHILLU[off], ""])
        elif block == 0x0B80 and off == 0x1A:
            units.append(["s", None])      # Tamil ச is usually "s"
        elif off in _CONS:
            units.append([_CONS[off], None])
        elif off in _VOWELS:
            units.append(["", _VOWELS[off]])
        elif off in _MATRAS:
            if units:
                units[-1][1] = _MATRAS[off]
        elif off == _VIRAMA:
            if units:
                units[-1][1] = ""
        elif off == _NUKTA_SIGN:
            if units:
                units[-1][0] = _NUKTA.get(units[-1][0], units[-1][0])
        elif off in _NASALS:
            units.append(["n", ""])
        elif off == _VISARGA:
            units.append(["h", ""])
        # anything else (om, avagraha, addak ...) is dropped

    n = len(units)
    if n == 0:
        return ""
    # Schwa deletion: the inherent "a" is silent at the end of a word and in
    # the middle when vowels sit on both sides (करना -> karna, not karana).
    if n > 1 and units[-1][1] is None:
        units[-1][1] = ""
    for i in range(n - 2, 0, -1):
        cons, vowel = units[i]
        if vowel is None and cons and units[i - 1][1] != "" \
                and units[i + 1][1] != "" and units[i + 1][0]:
            units[i][1] = ""
    return "".join(c + ("a" if v is None else v) for c, v in units)


def romanize(text: str) -> str:
    """Romanise any supported Indic script; Latin text passes through."""
    out, word = [], []
    for ch in text:
        if _is_indic(ch) or ch.isalnum():
            word.append(ch)
        else:
            if word:
                out.append(_romanize_word("".join(word)))
                word = []
            out.append(ch)
    if word:
        out.append(_romanize_word("".join(word)))
    return "".join(out)


_KEY_SUBS = (
    ("w", "v"), ("z", "j"), ("q", "k"), ("ph", "f"),
    ("aa", "a"), ("ee", "i"), ("oo", "u"), ("ai", "e"), ("iy", "i"),
)
_REPEAT = re.compile(r"(.)\1+")


def key(word: str) -> str:
    """Spelling-tolerant matching key for one word (any script)."""
    w = romanize(word).lower()
    w = re.sub(r"[^a-z0-9]", "", w)
    for a, b in _KEY_SUBS:
        w = w.replace(a, b)
    return _REPEAT.sub(r"\1", w)


_ASPIRATE = re.compile(r"(?<=[tdbgkj])h")


def name_key(word: str) -> str:
    """Looser key for people's names: also ignores aspiration (Sudha/Suda)."""
    return _ASPIRATE.sub("", key(word))


def skeleton(k: str) -> str:
    """Consonant skeleton of a key, keeping a leading vowel."""
    return k[:1] + re.sub(r"[aeiou]", "", k[1:])


# --------------------------------------------------------------------------
# Tokeniser
# --------------------------------------------------------------------------

@dataclass
class Tok:
    orig: str            # as transcribed (any script)
    key: str             # matching key, "" for separators
    kind: str            # "word" | "num" | "sep"
    value: float = 0.0   # for kind == "num"
    spelled: bool = False  # number came from words, not digits

    def __repr__(self) -> str:  # compact, for test failures
        return f"<{self.kind}:{self.value:g}>" if self.kind == "num" else f"<{self.key or '|'}>"


_TIME_WORDS = {"baje", "bje", "am", "pm", "oclock", "clock"}
_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d{2,3}\b)")
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _ascii_digits(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if INDIC_START <= cp <= INDIC_END and 0x66 <= (cp & 0x7F) <= 0x6F:
            out.append(chr(ord("0") + (cp & 0x7F) - 0x66))
        elif INDIC_START <= cp <= INDIC_END and (cp & 0x7F) in (0x64, 0x65):
            out.append(" | ")   # danda: sentence break
        else:
            out.append(ch)
    return "".join(out)


def tokenize(text: str) -> list[Tok]:
    text = unicodedata.normalize("NFC", text or "")
    text = _ascii_digits(text)
    text = text.replace("₹", " rs ").replace("/-", " ")
    text = _DIGIT_COMMA.sub("", text)
    low = text.lower()
    if not any(w in re.findall(r"[a-z]+", low) for w in _TIME_WORDS):
        # "three-forty" is sometimes transcribed as a clock time.
        text = _CLOCK.sub(lambda m: m.group(1) + m.group(2), text)
    text = re.sub(r"['’]s", "", text)      # Sunita's -> Sunita
    text = re.sub(r"[-_/]", " ", text)
    # Keep combining marks attached to their letters when tokenising.
    toks: list[Tok] = []
    for raw in _split(text):
        if raw[0].isdigit():
            toks.append(Tok(raw, "", "num", float(raw)))
        elif raw in ",;.!?|":
            if toks and toks[-1].kind != "sep":
                toks.append(Tok(raw, "", "sep"))
        else:
            k = key(raw)
            if k:
                toks.append(Tok(raw, k, "word"))
    while toks and toks[-1].kind == "sep":
        toks.pop()
    return toks


def _split(text: str) -> list[str]:
    """Split into digit runs, letter runs (with their marks) and punctuation."""
    out, cur, mode = [], [], ""
    for ch in text:
        cat = unicodedata.category(ch)
        if ch.isdigit() or (ch == "." and mode == "d" and cur):
            m = "d"
        elif cat[0] in "LM":
            m = "w"
        elif ch in ",;.!?|":
            m = "p"
        else:
            m = ""
        if m != mode or m == "p":
            if cur:
                out.append("".join(cur))
            cur = []
        if m:
            cur.append(ch)
        mode = m
    if cur:
        out.append("".join(cur))
    return [t.rstrip(".") or t for t in out if t]


# --------------------------------------------------------------------------
# Language guess from script
# --------------------------------------------------------------------------

_HINGLISH = {key(w) for w in (
    "udhaar jama baaki hai ka ke ki ko ne se kitna kal aaj nahi diya diye liya likho likh "
    "hisaab khata rupaye hata karo kya haan parso tha wale wala pehla doosra pe laga do "
    "bhejo sabse zyada kiska theek sahi galat mera sara dega gaya mila wapas kul aur"
).split()}
_ENGLISH = {key(w) for w in (
    "the is was has have gave paid pay owes owe how much what who put add account took "
    "from of for his her yesterday today remove delete undo send did does please and on "
    "credit payment balance total wrong not sorry which first second"
).split()}


# The Indic Unicode blocks share one layout, so a Devanagari letter moves to
# another script by a fixed offset. Tamil is left out: it spells names with a
# smaller alphabet, so an offset copy is not how a Tamil reader writes them.
_SCRIPT_BASE = {"bn": 0x0980, "pa": 0x0A00, "gu": 0x0A80, "te": 0x0C00, "kn": 0x0C80,
                "ml": 0x0D00}


def to_script(text: str, lang: str) -> str | None:
    """Devanagari text re-spelled in ``lang``'s script, or None if that script
    has no such letter."""
    base = _SCRIPT_BASE.get(lang)
    if base is None:
        return None
    out = []
    for ch in text:
        code = ord(ch)
        if 0x0900 <= code <= 0x097F:
            ch = chr(code - 0x0900 + base)
            if unicodedata.category(ch) == "Cn":       # unassigned in that script
                return None
        out.append(ch)
    return "".join(out)


def detect_lang(text: str, hint: str | None = None) -> str:
    """Reply language for a transcript.

    Script decides when there is one. Latin text is English only when it
    looks like English; romanised Hinglish stays in the session language.
    """
    counts: dict[str, int] = {}
    latin = 0
    for ch in text:
        cp = ord(ch)
        if INDIC_START <= cp <= INDIC_END:
            lang = BLOCK_LANG.get(cp & 0xFF80)
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    hint = (hint or "").split("-")[0].lower() or None
    if counts:
        lang = max(counts, key=counts.get)
        if sum(counts.values()) * 10 >= 3 * (sum(counts.values()) + latin):
            # Hindi and Marathi share Devanagari; trust the hint between them.
            if lang == "hi" and hint == "mr":
                return "mr"
            return lang
    if latin:
        words = {key(w) for w in re.findall(r"[A-Za-z]+", text)}
        if words & _HINGLISH:
            return hint if hint and hint != "en" else "hi"
        if words & _ENGLISH:
            return "en"
    return hint or "hi"
