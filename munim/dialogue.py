"""Dialogue manager: turns a transcript into ledger changes and one short reply.

Built for a counter in rush hour:

* An entry is saved and read back in one line. There is no "shall I save?"
  step; a wrong entry is fixed by voice ("nahi, 430") or removed ("hata do").
* A question is asked only when guessing would risk a wrong ledger line:
  two customers share the name, the direction was not said, the name is
  close to an existing one, or the amount is unusually large.
* The ledger is only ever changed by code in this file, never by the LLM.
* A noisy counter makes people repeat themselves, so the same line said
  twice within a minute and a half is questioned, not written twice.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Callable

from .guardrails import reminder_allowed
from .ledger import ACTIVE, CORRECTED, CREDIT, PAYMENT, UNDONE, Customer, Ledger, tok_match
from .parser import Draft, Parse, parse
from .replies import balance_tail, date_prefix, help_text, say, when
from .textnorm import detect_lang, romanize, to_script

log = logging.getLogger("munim.dialogue")

RULE_LANGS = {"hi", "en"}
DUP_WINDOW_S = 90.0          # the same line again within this time is questioned
_DANDA_LANGS = {"hi", "bn", "pa"}


def _stop(lang: str) -> str:
    return "।" if lang in _DANDA_LANGS else "."


@dataclass
class Reply:
    text: str
    lang: str
    kind: str                       # entry | query | ask | refuse | info | reprompt
    path: str = "rules"             # rules | llm
    changed: bool = False           # ledger was modified
    entries: list[dict] = field(default_factory=list)
    reminder: dict | None = None
    statement: dict | None = None   # customer whose page the screen should open


@dataclass
class Batch:
    """Ledger lines from one utterance, saved together once every line is clear."""
    drafts: list[Draft]
    i: int = 0
    assigned: dict[int, Customer] = field(default_factory=dict)
    by_name: dict[tuple, Customer] = field(default_factory=dict)
    not_similar: set = field(default_factory=set)
    big_ok: set = field(default_factory=set)
    new_customers: list[int] = field(default_factory=list)
    replaces: list[int] = field(default_factory=list)
    keep: list[int] = field(default_factory=list)   # untouched lines of a corrected batch
    utterance_id: int | None = None
    asks: int = 0
    dup_ok: bool = False            # the user confirmed a repeated line


class Brain:
    def __init__(self, ledger: Ledger, llm=None, default_lang: str = "hi",
                 big_amount: int = 20000, shop_name: str = "Kirana Store",
                 today: Callable[[], date] = date.today, log_transcripts: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.ledger = ledger
        self.llm = llm
        self.lang = default_lang
        self.big_amount = big_amount
        self.shop_name = shop_name
        self.today = today
        self.log_transcripts = log_transcripts
        self.pending: dict | None = None
        self.history: list[dict] = []        # undo stack
        self.disclosed: set[str] = set()
        self.clock = clock
        self.last_reply: Reply | None = None
        self._recent: list[tuple[float, int]] = []   # (when, entry id) for the duplicate guard
        self._last_cid: int | None = None            # customer of the last saved utterance

    # ------------------------------------------------------------ public API
    def greeting(self, lang: str | None = None) -> Reply:
        """First turn of a session: the AI disclosure, in the session language."""
        lang = lang or self.lang
        self.disclosed.add(lang)
        return Reply(f"{say(lang, 'disclose')} {say(lang, 'greet')}", lang, "info")

    @property
    def awaiting_answer(self) -> bool:
        return self.pending is not None

    async def handle(self, text: str, lang_hint: str | None = None,
                     followup: bool = False) -> Reply:
        """``followup``: this came right after the previous reply, so a line
        with no name ("kal 200 diya tha") may belong to the same customer."""
        lang = detect_lang(text, lang_hint or self.lang)
        self.lang = lang
        reply = await self._handle(text, lang, followup)
        reply.text = self._names_for(reply.text, reply.lang)
        self.last_reply = reply
        if lang not in self.disclosed:
            # A new language counts as a first turn: disclose again in it.
            self.disclosed.add(lang)
            reply.text = f"{say(lang, 'disclose')} {reply.text}"
        return reply

    def _names_for(self, text: str, lang: str) -> str:
        """Customer names as the listener reads them: Latin in an English
        reply, the reply's own script elsewhere (names are stored in Devanagari)."""
        if lang in ("hi", "mr"):
            return text
        for c in sorted(self.ledger.customers(), key=lambda c: -len(c.spoken or "")):
            if c.spoken and c.spoken in text:
                shown = c.name if lang == "en" else to_script(c.spoken, lang)
                if shown:
                    text = text.replace(c.spoken, shown)
        return text

    # --------------------------------------------------------------- routing
    async def _handle(self, text: str, lang: str, followup: bool = False) -> Reply:
        p = parse(text)
        if p.intent == "empty":
            return Reply(say(lang, "reprompt"), lang, "reprompt")
        if p.intent in ("refuse_threat", "refuse_interest"):
            return Reply(say(lang, p.intent), lang, "refuse")
        if p.intent == "repeat":                   # shop noise drowned the reply
            last = self.last_reply
            if last is None:
                return Reply(say(lang, "greet"), lang, "info")
            return Reply(last.text, last.lang, "ask" if self.pending else "info",
                         reminder=last.reminder, statement=last.statement)
        if p.intent == "help":
            return Reply(help_text(lang), lang, "info")

        uid = self.ledger.log_utterance(text, lang) if self.log_transcripts else None

        if self.pending is not None:
            r = await self._answer_pending(p, text, lang)
            if r is not None:
                return r

        # "Suresh ka 340 udhaar" ... "kal 200 diya tha": same customer.
        carry = None
        if followup and p.intent == "entry" and p.continues and self._last_cid \
                and not any(d.name_keys for d in p.drafts):
            carry = self.ledger.customer(self._last_cid)
            if carry is not None:
                p.confidence = p.base_confidence

        path = "rules"
        if carry is None and self._needs_llm(p, lang):
            p2 = await self.llm.parse(text, self.today().isoformat())
            if p2 is not None:
                p, path = p2, "llm"
        if p.intent in ("refuse_threat", "refuse_interest"):
            return Reply(say(lang, p.intent), lang, "refuse", path)

        r = self._dispatch(p, lang, uid, carry)
        r.path = path
        return r

    def _needs_llm(self, p: Parse, lang: str) -> bool:
        if self.llm is None:
            return False
        if p.intent == "unknown":
            return True
        if p.intent == "entry" and p.confidence < 0.7:
            return True
        if lang in RULE_LANGS:
            return False
        # Other languages: trust the rules only for clear-cut cases.
        if p.intent in ("yes", "no", "undo", "wipe"):
            return False
        if p.intent == "correction" and p.amount is not None and not p.name_keys:
            return False
        if p.intent == "entry" and p.confidence >= 0.95:
            return not all(d.name_keys and self.ledger.find(d.name_keys)[0]
                           for d in p.drafts)
        return True

    def _dispatch(self, p: Parse, lang: str, uid: int | None,
                  carry: Customer | None = None) -> Reply:
        if p.intent == "entry":
            b = Batch(p.drafts, utterance_id=uid)
            if carry is not None:
                b.assigned = {i: carry for i in range(len(p.drafts))}
            return self._run(b, lang)
        if p.intent == "query":
            return self._query(p, lang)
        if p.intent == "correction":
            return self._correct(p, lang, uid)
        if p.intent == "undo":
            return self._undo(lang)
        if p.intent == "wipe":
            self.pending = {"type": "wipe"}
            return Reply(say(lang, "wipe_confirm"), lang, "ask")
        if p.intent in ("reminder", "optin", "optout"):
            return self._reminder(p, lang)
        if p.intent == "choice" and p.name_keys:
            p.query = "customer_balance"
            return self._query(p, lang)
        if p.intent == "choice" and p.direction and self.history:
            return self._correct(p, lang, uid)      # "payment tha"
        if p.intent == "yes":
            return Reply(say(lang, "greet"), lang, "info")
        if p.intent == "no":
            return Reply(say(lang, "cancelled"), lang, "info")
        return Reply(say(lang, "reprompt"), lang, "reprompt")

    # ---------------------------------------------------------------- entries
    def _run(self, b: Batch, lang: str) -> Reply:
        """Resolve each line; stop and ask at the first thing that is unclear."""
        while b.i < len(b.drafts):
            d = b.drafts[b.i]
            cust = b.assigned.get(b.i)
            if cust is None:
                if not d.name_keys:
                    return self._ask(b, "name", say(lang, "ask_name", amt=d.amount), lang)
                sig = tuple(d.name_keys)
                cust = b.by_name.get(sig)
                if cust is None:
                    exact, similar = self.ledger.find(d.name_keys)
                    if len(exact) == 1:
                        cust = exact[0]
                    elif len(exact) > 1:
                        return self._ask(b, "which", self._which(exact, lang), lang,
                                         candidates=exact)
                    elif similar and sig not in b.not_similar:
                        best = max(similar, key=lambda c: max(
                            SequenceMatcher(None, d.name_keys[0], k).ratio()
                            for k in c.name_keys or [""]))
                        return self._ask(b, "similar",
                                         say(lang, "ask_similar", name=best.label),
                                         lang, candidates=[best])
                    else:
                        cust = self._new_customer(d)
                        b.new_customers.append(cust.id)
                    b.by_name[sig] = cust
                b.assigned[b.i] = cust
            if d.direction is None:
                return self._ask(b, "dir", say(lang, "ask_dir", name=cust.say,
                                               amt=d.amount), lang)
            if d.amount >= self.big_amount and b.i not in b.big_ok \
                    and d.direction != "promise":
                return self._ask(b, "big", say(lang, "ask_big", name=cust.say, amt=d.amount,
                                               kind=say(lang, d.direction).format(amt="").strip()),
                                 lang)
            b.i += 1
        dup = None if (b.replaces or b.dup_ok) else self._duplicate(b)
        if dup is not None:
            d, c = b.drafts[dup], b.assigned[dup]
            return self._ask(b, "dup", say(
                lang, "ask_dup", name=c.say, amt=d.amount,
                kind=say(lang, d.direction).format(amt="").strip()), lang)
        self.pending = None
        return self._commit(b, lang)

    def _duplicate(self, b: Batch) -> int | None:
        """Index of a line that repeats one saved moments ago, if any."""
        now, today = self.clock(), self.today()
        self._recent = [(t, eid) for t, eid in self._recent if now - t <= DUP_WINDOW_S]
        for _, eid in self._recent:
            e = self.ledger.entry(eid)
            if e is None or e.status != ACTIVE:
                continue
            for i, d in enumerate(b.drafts):
                kind = {"credit": CREDIT, "payment": PAYMENT}.get(d.direction)
                if kind == e.kind and d.amount == e.amount \
                        and b.assigned[i].id == e.customer_id \
                        and today + timedelta(days=d.day_offset) == e.txn_date:
                    return i
        return None

    def _ask(self, b: Batch, kind: str, text: str, lang: str, **extra) -> Reply:
        self.pending = {"type": kind, "batch": b, **extra}
        return Reply(text, lang, "ask")

    def _which(self, cands: list[Customer], lang: str) -> str:
        labels = [c.label for c in cands[:3]]
        opts = f" {say(lang, 'or')} ".join(labels) if len(labels) == 2 else \
            ", ".join(labels[:-1]) + f" {say(lang, 'or')} {labels[-1]}"
        return say(lang, "ask_which", opts=opts)

    def _new_customer(self, d: Draft) -> Customer:
        raw = d.name or "Customer"
        shown = romanize(raw).title()
        spoken = raw if not raw.isascii() else None
        desc = d.descriptor if d.descriptor and d.descriptor != raw else None
        return self.ledger.add_customer(shown, spoken, desc)

    def _commit(self, b: Batch, lang: str) -> Reply:
        today = self.today()
        made, promises, chips, kept = [], [], [], []
        parts: dict[int, list[str]] = {}
        promise_lines = []
        for i, d in enumerate(b.drafts):
            c = b.assigned[i]
            if d.direction == "promise":
                due = today + timedelta(days=d.day_offset) if d.day_offset > 0 else None
                promises.append(self.ledger.add_promise(c.id, d.amount, due))
                promise_lines.append(say(lang, "promise", name=c.say, amt=d.amount,
                                         when=when(lang, d.day_offset if d.day_offset > 0 else None)))
                chips.append({"customer": c.name, "kind": "PROMISE", "amount": d.amount,
                              "date": due.isoformat() if due else None})
                continue
            kind = CREDIT if d.direction == "credit" else PAYMENT
            when_ = today + timedelta(days=d.day_offset)
            replaces = b.replaces[i] if i < len(b.replaces) else None
            e = self.ledger.add_entry(c.id, kind, d.amount, when_, b.utterance_id, replaces)
            made.append(e.id)
            self._recent.append((self.clock(), e.id))
            if kind == PAYMENT:                    # a promised payment arrived
                kept += self.ledger.keep_promises(c.id, d.amount)
            parts.setdefault(c.id, []).append(
                date_prefix(lang, d.day_offset) + say(lang, d.direction, amt=d.amount))
            chips.append({"customer": c.name, "kind": kind, "amount": d.amount,
                          "date": when_.isoformat()})
        if b.replaces:
            self.ledger.set_status(b.replaces, CORRECTED)
        self.history.append({"entries": b.keep + made, "promises": promises,
                             "new_customers": b.new_customers, "kept": kept})
        who = {c.id for c in b.assigned.values()}
        self._last_cid = next(iter(who)) if len(who) == 1 else None
        self.history = self.history[-20:]

        stop = _stop(lang)
        out = [say(lang, "fixed")] if b.replaces else []
        for cid, lines in parts.items():
            c = self.ledger.customer(cid)
            new = say(lang, "new_cust") + " " if cid in b.new_customers else ""
            out.append(f"{new}{c.say}: {', '.join(lines)}{stop} "
                       f"{balance_tail(lang, self.ledger.balance(cid))}")
        out.extend(promise_lines)
        return Reply(" ".join(out), lang, "entry", changed=True, entries=chips)

    def _drop_batch(self, b: Batch | None) -> None:
        """Abandon a half-resolved batch and any customers created for it."""
        if b is None:
            return
        for cid in b.new_customers:
            if self.ledger.entry_count(cid) == 0:
                self.ledger.deactivate_customer(cid)

    # ------------------------------------------------- answers to our questions
    async def _answer_pending(self, p: Parse, text: str, lang: str) -> Reply | None:
        pend = self.pending
        kind = pend["type"]
        b: Batch | None = pend.get("batch")

        # A fresh command replaces the open question.
        if p.intent in ("entry", "query", "wipe", "reminder", "optin", "optout"):
            self._drop_batch(b)
            self.pending = None
            return None
        if p.intent == "undo":
            self._drop_batch(b)
            self.pending = None
            return Reply(say(lang, "cancelled"), lang, "info")

        # Answers in languages the rules do not cover.
        if p.intent in ("unknown", "choice") and self.llm is not None \
                and lang not in RULE_LANGS and kind in ("dir", "big", "similar", "wipe", "dup"):
            hint = {"dir": "The assistant asked: credit or payment? Answer as a correction "
                           "with only the direction set.",
                    }.get(kind, "The assistant asked a yes/no question.")
            p2 = await self.llm.parse(text, self.today().isoformat(), f"Context: {hint}\n")
            if p2 is not None:
                p = p2

        if kind == "wipe":
            self.pending = None
            if p.intent == "yes":
                self.ledger.wipe_all()
                self.history.clear()
                return Reply(say(lang, "wipe_done"), lang, "info", changed=True)
            return Reply(say(lang, "cancelled"), lang, "info")

        if kind == "resolve":                      # which customer, for a query/reminder
            c = self._pick(pend["candidates"], p)
            if c is None and p.intent == "yes" and len(pend["candidates"]) == 1:
                c = pend["candidates"][0]
            self.pending = None
            if c is None:
                return Reply(say(lang, "cancelled"), lang, "info")
            return pend["resume"](c, lang)

        if kind == "dup":
            if p.intent == "yes":
                b.dup_ok = True
                return self._run(b, lang)
            if p.intent == "no":
                return self._cancel(b, lang)
            if p.intent == "correction":           # "nahi, 430": fix the saved line instead
                self._drop_batch(b)
                self.pending = None
                return None
            return self._retry(b, lang)

        d = b.drafts[b.i]

        if p.intent == "correction":               # "nahi, 4300" while we were asking
            if p.amount is not None:
                d.amount = p.amount
            if p.direction:
                d.direction = p.direction
            if p.day_offset is not None:
                d.day_offset = p.day_offset
            if p.name_keys:
                self._rename(b, d, p)
            return self._run(b, lang)

        if kind == "which":
            c = self._pick(pend["candidates"], p)
            if c is None and p.intent == "no":
                return self._cancel(b, lang)
            if c is None:
                return self._retry(b, lang)
            self._assign(b, d, c)
            return self._run(b, lang)

        if kind == "similar":
            if p.intent == "yes":
                self._assign(b, d, pend["candidates"][0])
            elif p.intent == "no":
                b.not_similar.add(tuple(d.name_keys))
            elif p.intent == "choice" and p.name_keys:
                self._rename(b, d, p)
            else:
                return self._retry(b, lang)
            return self._run(b, lang)

        if kind == "dir":
            if p.direction in ("credit", "payment"):
                d.direction = p.direction
                return self._run(b, lang)
            if p.intent == "no":
                return self._cancel(b, lang)
            return self._retry(b, lang)

        if kind == "big":
            if p.intent == "yes":
                b.big_ok.add(b.i)
                return self._run(b, lang)
            if p.intent == "no":
                return self._cancel(b, lang)
            return self._retry(b, lang)

        if kind == "name":
            if p.name_keys:
                self._rename(b, d, p)
                return self._run(b, lang)
            if p.intent == "no":
                return self._cancel(b, lang)
            return self._retry(b, lang)

        return None

    def _pick(self, cands: list[Customer], p: Parse) -> Customer | None:
        if p.ordinal is not None and p.ordinal < len(cands):
            return cands[p.ordinal]
        q = [k for k in p.name_keys if k]
        if not q:
            return None
        full = [c for c in cands if set(c.name_keys) == set(q)]
        if len(full) == 1:
            return full[0]
        scores = []
        for c in cands:
            pool = c.name_keys + c.desc_keys
            scores.append(sum(1 for k in q if any(tok_match(k, t) == 2 for t in pool)))
        # Count only tokens that tell the candidates apart.
        shared = min(scores)
        best = max(scores)
        if best > shared and scores.count(best) == 1:
            return cands[scores.index(best)]
        return None

    def _assign(self, b: Batch, d: Draft, c: Customer) -> None:
        sig = tuple(d.name_keys)
        b.by_name[sig] = c
        for i, other in enumerate(b.drafts):
            if i >= b.i and tuple(other.name_keys) == sig:
                b.assigned[i] = c

    def _rename(self, b: Batch, d: Draft, p: Parse) -> None:
        old = tuple(d.name_keys)
        for i, other in enumerate(b.drafts):
            if i >= b.i and tuple(other.name_keys) == old:
                other.name, other.name_keys, other.descriptor = p.name, list(p.name_keys), p.descriptor
                b.assigned.pop(i, None)

    def _retry(self, b: Batch, lang: str) -> Reply:
        b.asks += 1
        if b.asks >= 2:
            return self._cancel(b, lang)
        return self._run(b, lang)                  # asks the same question again

    def _cancel(self, b: Batch, lang: str) -> Reply:
        self._drop_batch(b)
        self.pending = None
        return Reply(say(lang, "cancelled"), lang, "info")

    # ------------------------------------------------------ corrections / undo
    def _correct(self, p: Parse, lang: str, uid: int | None) -> Reply:
        if not self.history or not self.history[-1]["entries"]:
            return Reply(say(lang, "nothing_undo"), lang, "info")
        last = self.history[-1]
        entries = [e for e in (self.ledger.entry(i) for i in last["entries"]) if e]
        today = self.today()

        if p.name_keys:
            targets = entries                      # the whole utterance was for someone else
        elif len(entries) > 1 and p.amount is not None:
            # "nahi, 430" after two lines: fix the amount it sounds most like.
            targets = [max(entries, key=lambda e: SequenceMatcher(
                None, str(e.amount), str(p.amount)).ratio())]
        elif len(entries) > 1:
            targets = [entries[-1]]
        else:
            targets = entries

        drafts, b = [], Batch([], utterance_id=uid)
        for n, e in enumerate(targets):
            amount = p.amount if (p.amount is not None and len(targets) == 1) else e.amount
            direction = p.direction or ("credit" if e.kind == CREDIT else "payment")
            offset = p.day_offset if p.day_offset is not None else (e.txn_date - today).days
            d = Draft(amount, direction, p.name, list(p.name_keys), p.descriptor, offset)
            if not p.name_keys:
                c = self.ledger.customer(e.customer_id)
                d.name, d.name_keys = c.name, list(c.name_keys)
                b.assigned[n] = c
            drafts.append(d)
            if amount == e.amount:
                b.big_ok.add(n)
        b.drafts = drafts
        b.replaces = [e.id for e in targets]
        b.keep = [e.id for e in entries if e not in targets]
        b.new_customers = []
        self._stale_customers = list(last["new_customers"])
        self.ledger.set_promise_status(last.get("kept", []), ACTIVE)
        self.history.pop()
        reply = self._run(b, lang)
        if self.pending is None:
            self._cleanup_stale()
        return reply

    def _cleanup_stale(self) -> None:
        """Remove a customer that existed only because a name was misheard."""
        for cid in getattr(self, "_stale_customers", []):
            if self.ledger.customer(cid) and self.ledger.active_count(cid) == 0:
                self.ledger.deactivate_customer(cid)
        self._stale_customers = []

    def _undo(self, lang: str) -> Reply:
        if not self.history:
            return Reply(say(lang, "nothing_undo"), lang, "info")
        last = self.history.pop()
        touched = {e.customer_id for e in (self.ledger.entry(i) for i in last["entries"]) if e}
        self.ledger.set_status(last["entries"], UNDONE)
        self.ledger.void_promises(last["promises"])
        self.ledger.set_promise_status(last.get("kept", []), ACTIVE)
        for cid in last["new_customers"]:
            if self.ledger.active_count(cid) == 0:
                self.ledger.deactivate_customer(cid)
                touched.discard(cid)
        text = say(lang, "undone")
        if len(touched) == 1:
            c = self.ledger.customer(next(iter(touched)))
            if c:
                text += f" {c.say}: {balance_tail(lang, self.ledger.balance(c.id))}"
        return Reply(text, lang, "info", changed=True)

    # ---------------------------------------------------------------- queries
    def _resolve(self, p: Parse, lang: str,
                 resume: Callable[[Customer, str], Reply]) -> Reply:
        exact, similar = self.ledger.find(p.name_keys)
        if len(exact) == 1:
            return resume(exact[0], lang)
        if len(exact) > 1:
            self.pending = {"type": "resolve", "candidates": exact, "resume": resume}
            return Reply(self._which(exact, lang), lang, "ask")
        if similar:
            self.pending = {"type": "resolve", "candidates": similar[:1], "resume": resume}
            return Reply(say(lang, "ask_similar", name=similar[0].label), lang, "ask")
        return Reply(say(lang, "unknown_cust", name=p.name or ""), lang, "info")

    def _query(self, p: Parse, lang: str) -> Reply:
        today = self.today()
        if p.query == "customer_balance" and p.name_keys:
            return self._resolve(p, lang, lambda c, lg: Reply(
                f"{c.say}: {balance_tail(lg, self.ledger.balance(c.id))}", lg, "query"))
        if p.query == "statement" and p.name_keys:
            return self._resolve(p, lang, self._statement)
        if p.query == "promises":
            rows = self.ledger.open_promises()[:4]
            if not rows:
                return Reply(say(lang, "q_no_promises"), lang, "query")
            items = []
            for _, c, amount, due in rows:
                days = (due - today).days if due else None
                items.append(f"{c.say} {amount}" + (
                    f" {when(lang, days)}" if days is not None and days >= 0 else ""))
            return Reply(", ".join(items) + _stop(lang), lang, "query")
        if p.query == "top_debtors":
            top = self.ledger.top_debtors(3)
            if not top:
                return Reply(say(lang, "q_none"), lang, "query")
            return Reply(say(lang, "q_top", list=", ".join(f"{c.say} {b}" for c, b in top)),
                         lang, "query")
        period = p.period or "today"
        start, end = {
            "today": (today, today),
            "yesterday": (today - timedelta(days=1),) * 2,
            "week": (today - timedelta(days=6), today),
            "month": (today.replace(day=1), today),
        }[period]
        label = say(lang, period)
        if p.query == "period_credit":
            amt, n = self.ledger.sum_period(CREDIT, start, end)
            return Reply(say(lang, "q_credit", period=label, amt=amt, n=n), lang, "query")
        if p.query == "period_payment":
            amt, n = self.ledger.sum_period(PAYMENT, start, end)
            return Reply(say(lang, "q_payment", period=label, amt=amt, n=n), lang, "query")
        if p.query == "day_summary":
            c, _ = self.ledger.sum_period(CREDIT, start, end)
            pay, _ = self.ledger.sum_period(PAYMENT, start, end)
            return Reply(say(lang, "q_summary", period=label, c=c, p=pay), lang, "query")
        amt, n = self.ledger.total_outstanding()
        if n == 0:
            return Reply(say(lang, "q_none"), lang, "query")
        return Reply(say(lang, "q_total", amt=amt, n=n), lang, "query")

    def _statement(self, c: Customer, lang: str) -> Reply:
        """Read a customer's last three lines and open their page on screen."""
        today = self.today()
        lines = []
        for row in self.ledger.statement(c.id, 3):
            days = (date.fromisoformat(row["date"]) - today).days
            prefix = date_prefix(lang, days) if days < 0 else f"{say(lang, 'today')} "
            lines.append(prefix + say(lang, "credit" if row["kind"] == CREDIT else "payment",
                                      amt=row["amount"]))
        body = f"{', '.join(lines)}{_stop(lang)} " if lines else ""
        return Reply(f"{c.say}: {body}{balance_tail(lang, self.ledger.balance(c.id))}",
                     lang, "query", statement={"id": c.id, "name": c.name})

    # -------------------------------------------------------------- reminders
    def _reminder(self, p: Parse, lang: str) -> Reply:
        if not p.name_keys:
            return Reply(say(lang, "reprompt"), lang, "reprompt")

        def act(c: Customer, lg: str) -> Reply:
            if p.intent == "optin":
                self.ledger.set_opt_in(c.id, True)
                return Reply(say(lg, "optin", name=c.say), lg, "info", changed=True)
            if p.intent == "optout":
                self.ledger.set_opt_in(c.id, False)
                return Reply(say(lg, "optout", name=c.say), lg, "info", changed=True)
            if not c.opt_in:
                return Reply(say(lg, "rem_not_opted", name=c.say), lg, "refuse")
            due = self.ledger.balance(c.id)
            if due <= 0:
                return Reply(say(lg, "rem_nothing", name=c.say), lg, "info")
            text = say(lg, "reminder_text", name=c.say, shop=self.shop_name, amt=due)
            ok, why = reminder_allowed(c.opt_in, text)
            if not ok:
                log.error("reminder blocked: %s", why)
                return Reply(say(lg, "refuse_threat"), lg, "refuse")
            return Reply(say(lg, "rem_ready", name=c.say), lg, "info",
                         reminder={"customer": c.name, "amount": due, "text": text})

        return self._resolve(p, lang, act)
