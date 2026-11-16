"""Regression eval for the understanding layer (no audio, no network).

Each case in eval/cases.jsonl is a short conversation and the ledger lines
that must be active afterwards. Scored the way the judges score:
exact amount, correct credit/payment direction, correct customer.

    python scripts/eval_text.py            # rules only
    python scripts/eval_text.py --llm      # allow the LLM fallback (needs LLM_API_KEY)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from munim.config import get_settings          # noqa: E402
from munim.dialogue import Brain               # noqa: E402
from munim.ledger import Ledger                # noqa: E402
from munim.llm import LLM                      # noqa: E402

TODAY = date(2026, 11, 16)


def fresh_ledger() -> Ledger:
    led = Ledger(":memory:", os.urandom(32))
    led.add_customer("Ramesh Kumar", "रमेश कुमार", "दूध वाले", opt_in=True)
    led.add_customer("Ramesh Gupta", "रमेश गुप्ता")
    led.add_customer("Suresh", "सुरेश")
    led.add_customer("Sunita", "सुनीता")
    return led


async def main() -> int:
    s = get_settings()
    llm = LLM(s.llm_api_key, s.llm_base_url, s.llm_model) if "--llm" in sys.argv and s.has_llm else None
    cases = [json.loads(line) for line in (ROOT / "eval" / "cases.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    passed, failures = 0, []
    for case in cases:
        led = fresh_ledger()
        brain = Brain(led, llm, today=lambda: TODAY)
        brain.greeting("hi")
        for text in case["say"]:
            await brain.handle(text)
        got = sorted([e["customer"], e["kind"], e["amount"]]
                     for e in led.snapshot(TODAY)["recent"] if e["status"] == "ACTIVE")
        want = sorted(case["expect"])
        if got == want:
            passed += 1
        else:
            failures.append((case["say"], want, got))
    if llm:
        await llm.close()
    print(f"\n{passed}/{len(cases)} conversations correct "
          f"({100 * passed / len(cases):.0f}%), mode: {'rules + LLM' if llm else 'rules only'}\n")
    for say, want, got in failures:
        print("FAIL", say, "\n  want", want, "\n  got ", got)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
