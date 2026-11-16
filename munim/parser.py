"""Rule-based parser for shop talk (Hindi, Hinglish, English).

Most counter utterances follow a handful of patterns, so they are parsed
here in well under a millisecond with no network call. Anything this parser
is not confident about (other languages, unusual phrasing) is handed to the
LLM by the dialogue layer.

The two decisions that matter for scoring:

* direction - "Ramesh NE 200 diye" is a payment, "Ramesh KO 200 ka saamaan"
  is credit. The postposition decides, not the verb.
* tense - "kal 200 diya THA" is a payment dated yesterday, "kal de DEGA" is
  only a promise and must not touch the balance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .numbers import CURRENCY, merge_numbers
from .textnorm import Tok, key, name_key, tokenize


def K(words: str) -> set[str]:
    return {key(w) for w in words.split()}


POSTPOS = K("ka ke ki ko ne se का के की को ने से")
CREDIT_STRONG = K(
    "udhaar udhar udhari udhaari उधार उधारी credit क्रेडिट baaki baki बाकी bakaya बकाया "
    "saamaan saman samaan सामान owes owe due lena lene लेना लेने khate khaate खाते "
    # other languages: Kannada udri, Tamil kadan, Telugu appu, Malayalam kadam, Bengali dhar
    "udri ಉದ್ರಿ kadan கடன் appu అప్పు kadam കടം dhar ধার udhaari")
CREDIT_WEAK = K("likho likh likhna likhiye लिखो लिख chadha chadhao jodo jod add note")
PAYMENT_STRONG = K(
    "jama जमा payment पेमेंट paid pay chukaya chuka chukta चुकाया चुकता wapas vapas वापस "
    "lautaya lauta लौटाया mila mile mili मिला मिले मिली aaya aaye आया आए received receive "
    "returned return settle kaat kat kato kaato काट काटो bhara varavu வரவு വരവ്")
GIVE = K("diya diye die dia di de दिया दिए दिये दी दे gave given give")
TAKE = K("liya liye lia li le लिया लिए लिये ली ले took take taken bought buy "
         "kharida khareeda खरीदा")
FUTURE = K("dega degi denge dunga doonga dungi dedega dedenge देगा देगी देंगे दूंगा दूँगा "
           "lautayega chukayega karega karegi karenge करेगा करेंगे will aayega")
PAST = K("tha the thi था थे थी")
VERB_STEMS = K("likh kar hata mita bata kat kaat bhej chadha jod daal dal rakh laga bol "
               "लिख कर हटा मिटा बता काट भेज")
AUX_AFTER_STEM = K("de do di dena dijiye diya diye lo le liya dia दे दो दी देना दीजिए दिया लो")

TODAY = K("aaj आज today abhi अभी")
KAL = K("kal कल")
YESTERDAY = K("yesterday")
TOMORROW = K("tomorrow")
PARSO = K("parso parson परसों")
DIN = K("din दिन day days")
AGO = K("pehle pahle पहले ago")
LATER = K("baad बाद later")
WEEK = K("hafte hafta हफ्ते हफ्ता week saptah")
MONTH = K("mahine mahina महीने महीना month")
LAST = K("pichle pichhle pichla pichhla पिछले last")

QUERY = K("kitna kitne kitni कितना कितने कितनी total टोटल kul कुल hisaab hisab हिसाब "
          "balance बैलेंस batao bata bataiye बताओ बताइए how much what")
WHO = K("kaun कौन kiska kiske kiski किसका kinka sabse सबसे list who kis किस")
ALL = K("sab sabka sabhi saara sara pura poora all everything सब सारा पूरा सबका")
Q_PAYMENT = K("vasooli vasuli वसूली collection aamdani cash") | PAYMENT_STRONG
REMINDER = K("reminder रिमाइंडर yaad याद message मैसेज msg sms whatsapp takaza taqaza tagada")
ALLOW = K("anumati अनुमति permission ijazat izazat raazi razi raji allow chalu on haan han "
          "yes ok manzoor consent opt")
SEND = K("bhejo bhej bhejna send भेजो भेज karo")
UNDO = K("hata hatao hatado हटा हटाओ undo cancel कैंसिल mita mitao मिटा मिटाओ delete "
         "डिलीट radd रद्द remove")
DATA = K("data डेटा sabkuch")
NEG = K("nahi nahin nai नहीं no नो galat गलत sorry maaf arre not wrong mistake galti "
        "illa ಇಲ್ಲ beda ಬೇಡ illai இல்லை vendam வேண்டாம் kaadu కాదు vaddu వద్దు नाही nako नको "
        "nathi નથી ਨਹੀਂ alla അല്ല venda വേണ്ട")
DENY = K("mana मना band बंद off stop") | NEG
YES = K("haan han ha हाँ हां yes sahi सही theek thik ठीक ok okay correct bilkul pakka "
        "पक्का ji jee haudu ಹೌದು aamaam ஆமாம் avunu అవును hoy होय হ্যাঁ હા ਹਾਂ athe അതെ sari")
ORDINAL = {**{k: 0 for k in K("pehla pehle pehli pahla first पहला पहले पहली")},
           **{k: 1 for k in K("doosra dusra doosre dusre doosri second दूसरा दूसरे दूसरी")},
           **{k: 2 for k in K("teesra tisra teesre third तीसरा")}}
INTEREST = K("byaaj byaj ब्याज interest sood सूद loan लोन karz karza कर्ज़ कर्ज emi "
             "penalty jurmana जुर्माना finance")
THREAT = K("dhamki dhamkao dhamka धमकी धमकाओ threat threaten darao dara डराओ police पुलिस "
           "badnaam beizzati legal court notice kadak sakht warning chetavni gaali")
WALE = K("wale wala wali vale vala vali वाले वाला वाली")
HONORIFIC = K("ji जी bhai भाई bhaiya bhaiyya भैया sahab saab sahib साहब seth सेठ uncle "
              "aunty chacha chachi didi दीदी madam sir bhabhi babu")
WAKE = K("munim muneem munimji मुनीम मुनीमजी")
FILLER = K(
    "me mein main मैं में par pe hai hain h है हैं ho hua hui hue gaya gaye gayi गया गए गई "
    "kar karo kardo karna kariye कर करो kiya kiye किया dena dijiye lo aur और bhi भी ab to "
    "toh तो ye yeh ya यह ये wo woh vo वो वह is us iska uska unka inka iske uske mera mere "
    "apna apne hamara wala account dukaan dukan zara jara please plz bolo bol suno sun acha "
    "achha accha na of the a an for from has have had was on in his her their him my i put "
    "write entry worth naam nam नाम phir fir bas sirf baar liye and also it he she they "
    "that this with ek do one new naya upar tak log logo kya क्या hi ही abhi rupees amount "
    "paisa paise raha rahi rahe hu hoon hun wali vali at pe kuch mujhe humko hamein "
    "zyada jyada ज़्यादा ज्यादा most highest did does please khaata khata खाता"
) | CURRENCY

KNOWN = (POSTPOS | CREDIT_STRONG | CREDIT_WEAK | PAYMENT_STRONG | GIVE | TAKE | FUTURE
         | PAST | VERB_STEMS | AUX_AFTER_STEM | TODAY | KAL | YESTERDAY | TOMORROW | PARSO
         | DIN | AGO | LATER | WEEK | MONTH | LAST | QUERY | WHO | ALL | Q_PAYMENT
         | REMINDER | ALLOW | DENY | SEND | UNDO | DATA | NEG | YES | set(ORDINAL)
         | INTEREST | THREAT | WALE | WAKE | FILLER)

SEP_WORDS = K("aur और and phir fir lekin par magar")


@dataclass
class Draft:
    """One ledger line as understood from speech."""
    amount: int
    direction: str | None = None        # "credit" | "payment" | "promise"
    name: str | None = None             # as heard, original script
    name_keys: list[str] = field(default_factory=list)
    descriptor: str | None = None
    day_offset: int = 0


@dataclass
class Parse:
    intent: str
    drafts: list[Draft] = field(default_factory=list)
    query: str | None = None            # customer_balance | period_credit | ...
    period: str | None = None           # today | yesterday | week | month
    name: str | None = None
    name_keys: list[str] = field(default_factory=list)
    descriptor: str | None = None
    amount: int | None = None           # corrections
    direction: str | None = None
    day_offset: int | None = None
    ordinal: int | None = None
    confidence: float = 1.0
    source: str = "rules"


# --------------------------------------------------------------------------

def _prepare(text: str) -> list[Tok]:
    toks = tokenize(text)
    # Drop the wake phrase and its honorific ("Munim ji, ...").
    out, skip_ji = [], False
    for t in toks:
        if t.kind == "word" and t.key in WAKE:
            skip_ji = True
            continue
        if skip_ji and t.kind == "word" and t.key in K("ji जी"):
            skip_ji = False
            continue
        if skip_ji and t.kind == "sep":
            continue
        skip_ji = False
        out.append(t)
    toks = merge_numbers(out)
    # "likh do", "kar diya": the second verb is only an auxiliary.
    # "ke liye" is "for", not "took".
    for i, t in enumerate(toks):
        if t.kind != "word" or i == 0:
            continue
        prev = toks[i - 1]
        if t.key in AUX_AFTER_STEM and prev.kind == "word" and prev.key in VERB_STEMS:
            t.kind = "aux"
        elif t.key in K("liye lie") and prev.kind == "word" and prev.key in K("ke k"):
            t.kind = "aux"
    return toks


def _unknown(t: Tok) -> bool:
    return t.kind == "word" and t.key not in KNOWN


def _name_from(toks: list[Tok]) -> tuple[str | None, list[str], str | None]:
    """Pick the customer name (and "X wale" descriptor) out of a token run."""
    groups: list[list[int]] = []
    for i, t in enumerate(toks):
        if _unknown(t) or (t.kind == "word" and t.key in HONORIFIC and groups
                           and groups[-1][-1] == i - 1):
            if groups and groups[-1][-1] == i - 1:
                groups[-1].append(i)
            else:
                groups.append([i])
    if not groups:
        return None, [], None

    # Descriptor: the word before "wale" ("doodh wale Ramesh", "Ramesh doodh wala").
    desc_idx: set[int] = set()
    descriptor = None
    for i, t in enumerate(toks):
        if t.kind == "word" and t.key in WALE and i > 0 and _unknown(toks[i - 1]):
            desc_idx.add(i - 1)
            descriptor = f"{toks[i - 1].orig} {t.orig}"
            break

    def strip(g):
        return [i for i in g if i not in desc_idx]

    # Skip goods named after an amount: "200 ka doodh".
    def is_goods(g):
        i = g[0]
        return (i >= 2 and toks[i - 1].kind == "word" and toks[i - 1].key in POSTPOS
                and toks[i - 2].kind == "num")

    cands = [g for g in (strip(g) for g in groups) if g and not is_goods(g)]
    chosen = None
    for g in cands:                      # prefer the group before ka/ko/ne/se
        nxt = g[-1] + 1
        while nxt < len(toks) and toks[nxt].kind == "word" and toks[nxt].key in WALE:
            nxt += 1
        if nxt < len(toks) and toks[nxt].kind == "word" and toks[nxt].key in POSTPOS:
            chosen = g
            break
    if chosen is None and cands:
        chosen = cands[0]
    if chosen is None:
        if descriptor is None:
            return None, [], None
        d = sorted(desc_idx)
        return descriptor, [name_key(toks[i].orig) for i in d], descriptor

    words = [toks[i] for i in chosen]
    name = " ".join(t.orig for t in words)
    keys = [name_key(t.orig) for t in words if t.key not in HONORIFIC]
    keys += [name_key(toks[i].orig) for i in sorted(desc_idx)]
    return name, keys, descriptor


def _direction(seg: list[Tok]) -> tuple[str | None, float]:
    keys = [t.key for t in seg if t.kind == "word"]
    s = set(keys)
    if s & FUTURE:
        return "promise", 0.9
    cs, ps = s & CREDIT_STRONG, s & PAYMENT_STRONG
    if cs and ps:
        return "payment", 0.6            # "udhaar chukaya": repaid
    if cs:
        return "credit", 1.0
    if ps:
        return "payment", 1.0
    if s & GIVE:
        return ("credit", 0.85) if key("ko") in s else ("payment", 0.95)
    if s & TAKE:
        return ("payment", 0.9) if key("se") in s else ("credit", 0.9)
    for i, t in enumerate(seg[:-2]):     # "200 ka doodh": goods on credit
        if t.kind == "num" and seg[i + 1].kind == "word" and seg[i + 1].key in POSTPOS \
                and _unknown(seg[i + 2]):
            return "credit", 0.8
    if s & CREDIT_WEAK:
        return "credit", 0.8
    return None, 0.3


def _day_offset(seg: list[Tok], future: bool) -> tuple[int, set[int]]:
    """Relative date for a segment and the indices of numbers used by it."""
    used: set[int] = set()
    sign = 1 if future else -1
    for i, t in enumerate(seg):
        if t.kind != "word":
            continue
        if t.key in DIN and i > 0 and seg[i - 1].kind == "num":
            nxt = seg[i + 1].key if i + 1 < len(seg) and seg[i + 1].kind == "word" else ""
            used.add(i - 1)
            n = int(seg[i - 1].value)
            return (n if nxt in LATER or (future and nxt not in AGO) else -n), used
        if t.key in LAST and i + 1 < len(seg) and seg[i + 1].key in WEEK:
            return -7, used
        if t.key in PARSO:
            return 2 * sign, used
        if t.key in KAL:
            return sign, used
        if t.key in YESTERDAY:
            return -1, used
        if t.key in TOMORROW:
            return 1, used
    return 0, used


def _period(keys: set[str]) -> str | None:
    if keys & WEEK:
        return "week"
    if keys & MONTH:
        return "month"
    if keys & (KAL | YESTERDAY):
        return "yesterday"
    if keys & TODAY:
        return "today"
    return None


def _only_direction(s: set[str]) -> str | None:
    cs, ps = s & (CREDIT_STRONG | CREDIT_WEAK), s & PAYMENT_STRONG
    if cs and not ps:
        return "credit"
    if ps and not cs:
        return "payment"
    return None


def _entry(toks: list[Tok]) -> Parse:
    # Numbers that are part of a date ("3 din pehle") are not amounts.
    date_nums = {i - 1 for i, t in enumerate(toks)
                 if t.kind == "word" and t.key in DIN and i > 0 and toks[i - 1].kind == "num"}
    amounts = [i for i, t in enumerate(toks) if t.kind == "num" and i not in date_nums]
    if not amounts:
        return Parse("unknown", confidence=0.2)

    # One segment per amount. The words after an amount belong to it up to and
    # including its direction verb ("... 340 udhaar | kal 200 diya tha").
    bounds = [0]
    for prev, nxt in zip(amounts, amounts[1:]):
        cut, seen_dir, j = prev + 1, None, prev + 1
        while j < nxt:
            t = toks[j]
            if t.kind == "sep" or (t.kind == "word" and t.key in SEP_WORDS):
                cut = j + 1
                break
            if t.kind == "aux" or (t.kind == "word"
                                   and t.key in (PAST | FILLER | CURRENCY | POSTPOS)):
                if seen_dir is not None:
                    cut = j + 1
            elif t.kind == "word":
                d = ("c" if t.key in CREDIT_STRONG | CREDIT_WEAK | TAKE else
                     "p" if t.key in PAYMENT_STRONG | GIVE | FUTURE else None)
                if d is None or (seen_dir and d != seen_dir):
                    break
                seen_dir, cut = d, j + 1
            else:
                break
            j += 1
        bounds.append(cut)
    bounds.append(len(toks))

    drafts, conf = [], 1.0
    for n, a in enumerate(amounts):
        seg = toks[bounds[n]:bounds[n + 1]]
        direction, c = _direction(seg)
        conf = min(conf, c)
        offset, _ = _day_offset(seg, direction == "promise")
        if direction != "promise":
            offset = min(offset, 0)
        name, keys, desc = _name_from(seg)
        drafts.append(Draft(int(toks[a].value), direction, name, keys, desc, offset))

    # A name carries over to the lines that follow ("Ramesh ka 340, kal 200 diya").
    for i, d in enumerate(drafts):
        if not d.name_keys and i > 0:
            p = drafts[i - 1]
            d.name, d.name_keys, d.descriptor = p.name, p.name_keys, p.descriptor
    if not drafts[0].name_keys:
        for d in drafts[1:]:
            if d.name_keys:
                first = drafts[0]
                first.name, first.name_keys, first.descriptor = d.name, d.name_keys, d.descriptor
                break
    if any(not d.name_keys for d in drafts):
        conf = min(conf, 0.5)
    return Parse("entry", drafts=drafts, confidence=conf)


def _query(toks: list[Tok], s: set[str]) -> Parse:
    name, keys, desc = _name_from(toks)
    period = _period(s)
    p = Parse("query", period=period, name=name, name_keys=keys, descriptor=desc)
    if s & WHO:
        p.query = "top_debtors"
    elif keys:
        p.query = "customer_balance"
    elif period and s & (CREDIT_STRONG - K("baaki baki बाकी bakaya बकाया")) | (s & GIVE):
        p.query = "period_credit"
    elif period and s & Q_PAYMENT:
        p.query = "period_payment"
    elif period:
        p.query = "day_summary"
    else:
        p.query = "total_outstanding"
    return p


def parse(text: str) -> Parse:
    toks = _prepare(text)
    words = [t for t in toks if t.kind in ("word", "aux")]
    nums = [t for t in toks if t.kind == "num"]
    if not words and not nums:
        return Parse("empty", confidence=0.0)
    s = {t.key for t in toks if t.kind == "word"}

    if s & THREAT:
        return Parse("refuse_threat")
    if s & INTEREST:
        return Parse("refuse_interest")

    if s & UNDO and s & (DATA | ALL) and not nums:
        return Parse("wipe")

    first = words[0].key if words else ""
    if first in NEG or (s & K("galat गलत wrong")):
        rest = [t for t in toks if not (t.kind == "word" and t.key in NEG)]
        rs = {t.key for t in rest if t.kind == "word"}
        if rs & UNDO:
            return Parse("undo")
        name, keys, desc = _name_from(rest)
        rnums = [t for t in rest if t.kind == "num"]
        direction = _only_direction(rs) or (
            "payment" if rs & GIVE else "credit" if rs & TAKE else None)
        offset = None
        if rs & (KAL | PARSO | TODAY | YESTERDAY):
            offset = min(_day_offset(rest, False)[0], 0)
        if not (rnums or keys or direction or offset is not None):
            return Parse("no")
        return Parse("correction", amount=int(rnums[0].value) if rnums else None,
                     name=name, name_keys=keys, descriptor=desc,
                     direction=direction, day_offset=offset)

    if s & UNDO and not nums:
        return Parse("undo")

    if s & REMINDER:
        name, keys, desc = _name_from(toks)
        intent = "optout" if s & DENY else "optin" if s & (ALLOW - SEND) else "reminder"
        return Parse(intent, name=name, name_keys=keys, descriptor=desc)

    if not nums and (s & (QUERY | WHO) or (s & K("baaki baki बाकी bakaya hisaab hisab"))):
        return _query(toks, s)

    if nums:
        return _entry(toks)

    # No number and no command word: an answer to a question we asked.
    name, keys, desc = _name_from(toks)
    ordinal = next((ORDINAL[t.key] for t in words if t.key in ORDINAL), None)
    direction = _only_direction(s) or ("payment" if s & GIVE else None)
    if keys or ordinal is not None or direction:
        return Parse("choice", name=name, name_keys=keys, descriptor=desc,
                     ordinal=ordinal, direction=direction,
                     confidence=0.9 if (ordinal is not None or direction) else 0.5)
    if s & YES:
        return Parse("yes")
    if s & DENY:
        return Parse("no")
    return Parse("unknown", confidence=0.1)
