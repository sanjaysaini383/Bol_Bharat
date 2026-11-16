"""Money guardrails that hold regardless of what was said or parsed.

1. Reminder text is checked against a pressure/threat word list before it
   can leave the device, even though it is built from a fixed template.
2. Reminders are only produced for customers who opted in (enforced in the
   dialogue layer and re-checked here).
3. Requests for interest, loans, penalties or intimidation are refused by
   the parser before any other handling.
"""

from __future__ import annotations

from .textnorm import key, tokenize

_PRESSURE = {key(w) for w in (
    # Hindi / Hinglish
    "dhamki धमकी turant तुरंत fauran फौरन abhi jaldi warna वरना nahi to anjaam अंजाम "
    "police पुलिस court कोर्ट kanooni कानूनी karwai कार्रवाई jurmana जुर्माना byaaj ब्याज "
    "badnaam बदनाम sharm शर्म beizzati chetavni चेतावनी aakhri आखिरी majboor "
    # English
    "immediately urgent final warning legal action penalty interest shame threat "
    "consequences deadline last notice police court or else must"
).split()}
_PRESSURE -= {key("abhi"), key("or"), key("nahi"), key("to"), key("else")}


def pressure_words(text: str) -> list[str]:
    return [t.orig for t in tokenize(text) if t.kind == "word" and t.key in _PRESSURE]


def reminder_allowed(opt_in: bool, text: str) -> tuple[bool, str]:
    """Final gate before a reminder is shown for sending."""
    if not opt_in:
        return False, "customer has not opted in"
    bad = pressure_words(text)
    if bad:
        return False, "pressure language: " + ", ".join(bad)
    return True, ""
