"""The khata: customers, credit and payment entries, promises.

SQLite on the device/server. Names, descriptors and transcripts are
encrypted with AES-256-GCM before they touch the disk; amounts are stored
as integer rupees so sums are exact.

Entries are never edited or deleted. A correction marks the old row
CORRECTED and adds a new one; an undo marks it UNDONE. The paper khata's
struck-through line is kept as an audit trail.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from .crypto import Box
from .textnorm import name_key, skeleton, tokenize

CREDIT, PAYMENT = "CREDIT", "PAYMENT"
ACTIVE, CORRECTED, UNDONE = "ACTIVE", "CORRECTED", "UNDONE"

_HONORIFICS = {name_key(w) for w in (
    "ji जी bhai भाई bhaiya bhaiyya भैया sahab saab sahib साहब seth सेठ uncle "
    "aunty anti chacha chachi didi दीदी madam sir bhabhi babu shri shree श्री "
    "mr mrs anna akka amma garu avare"
).split()}
_DESC_FILLER = {name_key(w) for w in "wale wala wali vale vala vali वाले वाला वाली ke ka ki".split()}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, name BLOB NOT NULL, spoken BLOB, descriptor BLOB,
  opt_in INTEGER NOT NULL DEFAULT 0, opt_in_at TEXT,
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  kind TEXT NOT NULL CHECK(kind IN ('CREDIT','PAYMENT')),
  amount INTEGER NOT NULL CHECK(amount > 0),
  txn_date TEXT NOT NULL, created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE', replaces_id INTEGER, utterance_id INTEGER);
CREATE TABLE IF NOT EXISTS promises(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  amount INTEGER NOT NULL, due_date TEXT, created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE');
CREATE TABLE IF NOT EXISTS utterances(
  id INTEGER PRIMARY KEY, text BLOB NOT NULL, lang TEXT,
  created_at TEXT NOT NULL, purge_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_entries_cust ON entries(customer_id, status);
CREATE INDEX IF NOT EXISTS ix_entries_date ON entries(txn_date, status);
"""


def name_tokens(text: str | None) -> list[str]:
    """Matching keys for a name or descriptor, honorifics removed."""
    keys = [name_key(t.orig) for t in tokenize(text or "") if t.kind == "word"]
    return [k for k in keys if k and k not in _HONORIFICS and k not in _DESC_FILLER]


def tok_match(a: str, b: str) -> int:
    """2 = same name, 1 = similar enough to ask about, 0 = different."""
    if a == b:
        return 2
    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio >= 0.8 and skeleton(a) == skeleton(b):
        return 2
    return 1 if ratio >= 0.75 else 0


@dataclass
class Customer:
    id: int
    name: str
    spoken: str | None
    descriptor: str | None
    opt_in: bool
    name_keys: list[str] = field(default_factory=list)
    desc_keys: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        base = self.spoken or self.name
        return f"{self.descriptor} {base}" if self.descriptor else base

    @property
    def say(self) -> str:
        return self.spoken or self.name


@dataclass
class Entry:
    id: int
    customer_id: int
    kind: str
    amount: int
    txn_date: date
    status: str


class Ledger:
    def __init__(self, path: Path | str, key: bytes, retention_hours: int = 72):
        self._box = Box(key)
        self._lock = threading.RLock()
        self._retention = timedelta(hours=retention_hours)
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._cache: dict[int, Customer] = {}
        self._load()

    # ------------------------------------------------------------ customers
    def _load(self) -> None:
        self._cache.clear()
        for r in self._db.execute("SELECT * FROM customers WHERE active=1"):
            self._cache[r["id"]] = self._customer(r)

    def _customer(self, r: sqlite3.Row) -> Customer:
        name = self._box.open(r["name"]) or ""
        spoken = self._box.open(r["spoken"])
        desc = self._box.open(r["descriptor"])
        keys = name_tokens(name)
        for k in name_tokens(spoken):
            if k not in keys:
                keys.append(k)
        return Customer(r["id"], name, spoken, desc, bool(r["opt_in"]),
                        keys, name_tokens(desc))

    def customers(self) -> list[Customer]:
        return list(self._cache.values())

    def customer(self, cid: int) -> Customer | None:
        return self._cache.get(cid)

    def add_customer(self, name: str, spoken: str | None = None,
                     descriptor: str | None = None, opt_in: bool = False) -> Customer:
        now = _now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO customers(name,spoken,descriptor,opt_in,opt_in_at,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (self._box.seal(name), self._box.seal(spoken),
                 self._box.seal(descriptor), int(opt_in), now if opt_in else None, now))
            self._db.commit()
            r = self._db.execute("SELECT * FROM customers WHERE id=?", (cur.lastrowid,)).fetchone()
            c = self._customer(r)
            self._cache[c.id] = c
            return c

    def deactivate_customer(self, cid: int) -> None:
        with self._lock:
            self._db.execute("UPDATE customers SET active=0 WHERE id=?", (cid,))
            self._db.commit()
            self._cache.pop(cid, None)

    def set_opt_in(self, cid: int, on: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE customers SET opt_in=?, opt_in_at=? WHERE id=?",
                             (int(on), _now() if on else None, cid))
            self._db.commit()
            if cid in self._cache:
                self._cache[cid].opt_in = on

    def find(self, query_keys: list[str]) -> tuple[list[Customer], list[Customer]]:
        """Customers matching a spoken name. Returns (exact, similar)."""
        q = [k for k in query_keys if k not in _HONORIFICS and k not in _DESC_FILLER]
        if not q:
            return [], []
        exact, similar = [], []
        for c in self._cache.values():
            pool = c.name_keys + c.desc_keys
            grades = [max((tok_match(k, p) for p in pool), default=0) for k in q]
            if min(grades) == 2:
                exact.append(c)
            elif min(grades) == 1 and len(q) == 1:
                similar.append(c)
        return exact, similar

    def keyterms(self, limit: int = 80) -> list[str]:
        """Names for the STT to favour. The ledger trains the ear."""
        seen, out = set(), []
        for c in sorted(self._cache.values(), key=lambda c: -c.id):
            for term in (c.spoken, c.name):
                if term and term not in seen:
                    seen.add(term)
                    out.append(term)
        return out[:limit]

    # -------------------------------------------------------------- entries
    def add_entry(self, cid: int, kind: str, amount: int, txn_date: date,
                  utterance_id: int | None = None, replaces: int | None = None) -> Entry:
        if amount <= 0:
            raise ValueError("amount must be positive")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO entries(customer_id,kind,amount,txn_date,created_at,"
                "replaces_id,utterance_id) VALUES(?,?,?,?,?,?,?)",
                (cid, kind, int(amount), txn_date.isoformat(), _now(), replaces, utterance_id))
            self._db.commit()
            return Entry(cur.lastrowid, cid, kind, int(amount), txn_date, ACTIVE)

    def entry(self, eid: int) -> Entry | None:
        r = self._db.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        return Entry(r["id"], r["customer_id"], r["kind"], r["amount"],
                     date.fromisoformat(r["txn_date"]), r["status"])

    def set_status(self, entry_ids: list[int], status: str) -> None:
        with self._lock:
            self._db.executemany("UPDATE entries SET status=? WHERE id=?",
                                 [(status, e) for e in entry_ids])
            self._db.commit()

    def entry_count(self, cid: int) -> int:
        return self._db.execute("SELECT COUNT(*) FROM entries WHERE customer_id=?",
                                (cid,)).fetchone()[0]

    def active_count(self, cid: int) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM entries WHERE customer_id=? AND status='ACTIVE'",
            (cid,)).fetchone()[0]

    def balance(self, cid: int) -> int:
        """What the customer owes. Negative means advance paid."""
        r = self._db.execute(
            "SELECT COALESCE(SUM(CASE kind WHEN 'CREDIT' THEN amount ELSE -amount END),0)"
            " FROM entries WHERE customer_id=? AND status='ACTIVE'", (cid,)).fetchone()
        return int(r[0])

    def balances(self) -> dict[int, int]:
        rows = self._db.execute(
            "SELECT customer_id, SUM(CASE kind WHEN 'CREDIT' THEN amount ELSE -amount END)"
            " FROM entries WHERE status='ACTIVE' GROUP BY customer_id")
        return {r[0]: int(r[1]) for r in rows if r[0] in self._cache}

    def sum_period(self, kind: str, start: date, end: date) -> tuple[int, int]:
        r = self._db.execute(
            "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM entries"
            " WHERE status='ACTIVE' AND kind=? AND txn_date BETWEEN ? AND ?",
            (kind, start.isoformat(), end.isoformat())).fetchone()
        return int(r[0]), int(r[1])

    def total_outstanding(self) -> tuple[int, int]:
        owing = [b for b in self.balances().values() if b > 0]
        return sum(owing), len(owing)

    def top_debtors(self, n: int = 3) -> list[tuple[Customer, int]]:
        rows = sorted(((b, cid) for cid, b in self.balances().items() if b > 0), reverse=True)
        return [(self._cache[cid], b) for b, cid in rows[:n]]

    # ------------------------------------------------------------- promises
    def add_promise(self, cid: int, amount: int, due: date | None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO promises(customer_id,amount,due_date,created_at) VALUES(?,?,?,?)",
                (cid, int(amount), due.isoformat() if due else None, _now()))
            self._db.commit()
            return cur.lastrowid

    def void_promises(self, ids: list[int]) -> None:
        with self._lock:
            self._db.executemany("UPDATE promises SET status='UNDONE' WHERE id=?",
                                 [(i,) for i in ids])
            self._db.commit()

    # ---------------------------------------------------------- transcripts
    def log_utterance(self, text: str, lang: str) -> int:
        now = datetime.now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO utterances(text,lang,created_at,purge_at) VALUES(?,?,?,?)",
                (self._box.seal(text), lang, now.isoformat(timespec="seconds"),
                 (now + self._retention).isoformat(timespec="seconds")))
            self._db.commit()
            return cur.lastrowid

    def purge_transcripts(self, everything: bool = False) -> int:
        with self._lock:
            if everything:
                cur = self._db.execute("DELETE FROM utterances")
            else:
                cur = self._db.execute("DELETE FROM utterances WHERE purge_at <= ?", (_now(),))
            self._db.commit()
            return cur.rowcount

    def wipe_all(self) -> None:
        with self._lock:
            for t in ("entries", "promises", "utterances", "customers"):
                self._db.execute(f"DELETE FROM {t}")
            self._db.commit()
            self._db.execute("VACUUM")
            self._cache.clear()

    # ------------------------------------------------------------- snapshot
    def snapshot(self, today: date | None = None) -> dict:
        """Everything the screen shows, in one payload."""
        today = today or date.today()
        bal = self.balances()
        outstanding, owing = self.total_outstanding()
        last = {r[0]: r[1] for r in self._db.execute(
            "SELECT customer_id, MAX(created_at) FROM entries WHERE status='ACTIVE'"
            " GROUP BY customer_id")}
        customers = [{
            "id": c.id, "name": c.name, "spoken": c.spoken, "descriptor": c.descriptor,
            "balance": bal.get(c.id, 0), "opt_in": c.opt_in, "last": last.get(c.id),
        } for c in self._cache.values()]
        customers.sort(key=lambda c: (c["last"] or "", c["id"]), reverse=True)
        recent = []
        for r in self._db.execute(
                "SELECT * FROM entries ORDER BY id DESC LIMIT 40"):
            c = self._cache.get(r["customer_id"])
            if c is None:
                continue
            recent.append({
                "id": r["id"], "customer": c.name, "descriptor": c.descriptor,
                "kind": r["kind"], "amount": r["amount"], "date": r["txn_date"],
                "status": r["status"], "at": r["created_at"],
            })
        promises = []
        for r in self._db.execute(
                "SELECT * FROM promises WHERE status='ACTIVE' ORDER BY "
                "COALESCE(due_date,'9999') LIMIT 20"):
            c = self._cache.get(r["customer_id"])
            if c:
                promises.append({"id": r["id"], "customer": c.name,
                                 "amount": r["amount"], "due": r["due_date"]})
        return {
            "today": today.isoformat(),
            "outstanding": outstanding, "owing": owing,
            "today_credit": self.sum_period(CREDIT, today, today)[0],
            "today_payment": self.sum_period(PAYMENT, today, today)[0],
            "customers": customers, "recent": recent, "promises": promises,
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
