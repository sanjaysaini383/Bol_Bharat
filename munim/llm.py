"""LLM fallback parser.

Used only when the rule parser is not confident: languages other than
Hindi/English, or unusual phrasing. It speaks the OpenAI-compatible chat
API, so any provider works; the default is Groq's free tier.

The model only *extracts*. It never writes the spoken reply, never sees the
customer list, and every amount it returns is checked against the numbers
actually present in the transcript before the ledger is touched.
"""

from __future__ import annotations

import json
import logging

import httpx

from .numbers import find_amounts
from .parser import Draft, Parse
from .textnorm import name_key, romanize, tokenize

log = logging.getLogger("munim.llm")

SYSTEM = """You extract ledger actions from one thing an Indian kirana shopkeeper said to \
his voice khata (credit ledger). The transcript may be in Hindi, English, Kannada, Tamil, \
Telugu, Marathi, Bengali, Gujarati, Punjabi or Malayalam, often mixed with English, and may \
contain speech-recognition mistakes.

Reply with ONE JSON object and nothing else:
{
 "intent": "entry" | "query" | "correction" | "undo" | "reminder" | "optin" | "optout" | \
"yes" | "no" | "refuse_interest" | "refuse_threat" | "unknown",
 "entries": [{"customer": str, "descriptor": str|null, "amount": int,
              "direction": "credit" | "payment" | "promise" | null, "day_offset": int}],
 "query": "customer_balance" | "period_credit" | "period_payment" | "day_summary" | \
"total_outstanding" | "top_debtors" | null,
 "period": "today" | "yesterday" | "week" | "month" | null,
 "customer": str|null,
 "correction": {"amount": int|null, "customer": str|null,
                "direction": "credit"|"payment"|null, "day_offset": int|null} | null
}

Rules:
- "credit": the customer took goods or money on credit and now owes the shop more.
- "payment": the customer paid money to the shop and now owes less.
- "promise": the customer WILL pay later (future tense). It is not a payment.
- If the direction is not clearly stated, use null. Never guess.
- "customer" is the name exactly as written in the transcript, in the same script. Put \
words like "milk seller" / "doodh wale" / "school ke paas" in "descriptor".
- day_offset: 0 today, -1 yesterday, -2 day before yesterday; positive only for promises.
- amount: a whole number of rupees. Convert number words in any language to digits. Use \
only amounts that were actually said.
- One utterance can hold several entries; a name carries over to later entries.
- "correction": the speaker is fixing the previous entry ("no, 430", "illa, Suresh").
- "undo": remove the last entry. "yes"/"no": a bare confirmation.
- "reminder": send a reminder to a customer. "optin"/"optout": the customer agreed to, or \
refused, reminders.
- "refuse_interest": asks to add interest, a loan, a late fee or a penalty.
- "refuse_threat": asks to threaten, shame or pressure a customer.
- Anything else, including medical, legal or general questions: "unknown".

Examples:
"ರಮೇಶ್ 340 ಉದ್ರಿ, ನಿನ್ನೆ 200 ಕೊಟ್ಟಿದ್ದ" -> {"intent":"entry","entries":[{"customer":"ರಮೇಶ್",\
"descriptor":null,"amount":340,"direction":"credit","day_offset":0},{"customer":"ರಮೇಶ್",\
"descriptor":null,"amount":200,"direction":"payment","day_offset":-1}],"query":null,\
"period":null,"customer":null,"correction":null}
"சுரேஷ் கணக்கில் எவ்வளவு பாக்கி" -> {"intent":"query","entries":[],\
"query":"customer_balance","period":null,"customer":"சுரேஷ்","correction":null}
"కాదు, 430" -> {"intent":"correction","entries":[],"query":null,"period":null,\
"customer":null,"correction":{"amount":430,"customer":null,"direction":null,"day_offset":null}}
"""

_DIRECTIONS = {"credit", "payment", "promise"}
_QUERIES = {"customer_balance", "period_credit", "period_payment", "day_summary",
            "total_outstanding", "top_debtors"}
_INTENTS = {"entry", "query", "correction", "undo", "reminder", "optin", "optout", "yes",
            "no", "refuse_interest", "refuse_threat", "unknown"}


def _keys(name: str | None) -> list[str]:
    return [name_key(t.orig) for t in tokenize(name or "") if t.kind == "word"]


def _int(v) -> int | None:
    try:
        n = int(round(float(v)))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def to_parse(data: dict, transcript: str) -> Parse | None:
    """Validate raw model output into a Parse. None means: do not trust it."""
    intent = data.get("intent")
    if intent not in _INTENTS:
        return None
    said = set(find_amounts(transcript))
    p = Parse(intent, source="llm", confidence=0.85)

    if intent == "entry":
        for e in data.get("entries") or []:
            amount = _int(e.get("amount"))
            if amount is None:
                return None
            if said and amount not in said:
                log.warning("LLM amount %s not in transcript %s", amount, sorted(said))
                return None
            direction = e.get("direction") if e.get("direction") in _DIRECTIONS else None
            try:
                offset = int(e.get("day_offset") or 0)
            except (TypeError, ValueError):
                offset = 0
            if direction != "promise":
                offset = min(offset, 0)
            name = (e.get("customer") or "").strip() or None
            desc = (e.get("descriptor") or "").strip() or None
            p.drafts.append(Draft(amount, direction, name,
                                  _keys(name) + _keys(desc), desc, max(offset, -365)))
        if not p.drafts or (said and len(p.drafts) > len(said) + 1):
            return None
    elif intent == "query":
        p.query = data.get("query") if data.get("query") in _QUERIES else None
        p.period = data.get("period") if data.get("period") in (
            "today", "yesterday", "week", "month") else None
        p.name = (data.get("customer") or "").strip() or None
        p.name_keys = _keys(p.name)
        if p.query is None:
            p.query = "customer_balance" if p.name_keys else "total_outstanding"
    elif intent == "correction":
        c = data.get("correction") or {}
        p.amount = _int(c.get("amount"))
        if p.amount is not None and said and p.amount not in said:
            return None
        p.name = (c.get("customer") or "").strip() or None
        p.name_keys = _keys(p.name)
        p.direction = c.get("direction") if c.get("direction") in ("credit", "payment") else None
        if c.get("day_offset") is not None:
            try:
                p.day_offset = min(int(c["day_offset"]), 0)
            except (TypeError, ValueError):
                pass
        if not (p.amount or p.name_keys or p.direction or p.day_offset is not None):
            return None
    elif intent in ("reminder", "optin", "optout"):
        p.name = (data.get("customer") or "").strip() or None
        p.name_keys = _keys(p.name)
    return p


class LLM:
    def __init__(self, api_key: str, base_url: str, model: str, timeout_s: float = 4.0):
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"})

    async def warm(self) -> None:
        """Open the TLS connection early so the first real call is fast."""
        try:
            await self._client.get("/models")
        except httpx.HTTPError:
            pass

    async def parse(self, transcript: str, today: str, context: str = "") -> Parse | None:
        user = (f"Today: {today}\n{context}"
                f"Transcript: {transcript}\nRomanised: {romanize(transcript)}")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
        }
        try:
            r = await self._client.post("/chat/completions", json=body)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return to_parse(json.loads(content), transcript)
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            log.warning("LLM parse failed: %s", e)
            return None

    async def close(self) -> None:
        await self._client.aclose()
