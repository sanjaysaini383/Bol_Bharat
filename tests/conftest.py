import os
from datetime import date

import pytest

from munim.dialogue import Brain
from munim.ledger import Ledger

TODAY = date(2026, 11, 16)


@pytest.fixture
def ledger():
    return Ledger(":memory:", os.urandom(32))


@pytest.fixture
def seeded(ledger):
    ledger.add_customer("Ramesh Kumar", "रमेश कुमार", "दूध वाले", opt_in=True)
    ledger.add_customer("Ramesh Gupta", "रमेश गुप्ता", None)
    ledger.add_customer("Suresh", "सुरेश", None)
    ledger.add_customer("Sunita", "सुनीता", None, opt_in=False)
    return ledger


@pytest.fixture
def brain(seeded):
    b = Brain(seeded, today=lambda: TODAY)
    b.greeting("hi")
    return b
