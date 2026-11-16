"""Synthetic demo data. Every name here is invented; no real customer data."""

from __future__ import annotations

from datetime import date, timedelta

from .ledger import CREDIT, PAYMENT, Ledger

# (name, spoken form, descriptor, opted in to reminders, [(kind, amount, days ago)])
CUSTOMERS = [
    ("Ramesh Kumar", "रमेश कुमार", "दूध वाले", True,
     [(CREDIT, 450, 6), (PAYMENT, 200, 3), (CREDIT, 120, 1)]),
    ("Ramesh Gupta", "रमेश गुप्ता", None, False,
     [(CREDIT, 800, 9), (PAYMENT, 300, 4)]),
    ("Suresh", "सुरेश", None, False, [(CREDIT, 260, 2)]),
    ("Sunita", "सुनीता", None, True, [(CREDIT, 1500, 12), (PAYMENT, 1000, 5)]),
    ("Lakshmi", "लक्ष्मी", "टेलर", False, [(CREDIT, 90, 1)]),
    ("Imran", "इमरान", None, True, [(CREDIT, 340, 3), (PAYMENT, 340, 0)]),
    ("Kavita", "कविता", "स्कूल वाली", False, []),
]


def seed(ledger: Ledger, today: date | None = None) -> None:
    today = today or date.today()
    for name, spoken, desc, opt_in, entries in CUSTOMERS:
        c = ledger.add_customer(name, spoken, desc, opt_in)
        for kind, amount, ago in entries:
            ledger.add_entry(c.id, kind, amount, today - timedelta(days=ago))
