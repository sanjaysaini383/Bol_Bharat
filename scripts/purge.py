"""Delete stored data. Run after judging (DPDP: keep nothing longer than needed).

    python scripts/purge.py              # delete all transcripts
    python scripts/purge.py --all        # delete the whole ledger as well
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from munim.config import get_settings   # noqa: E402
from munim.crypto import load_key       # noqa: E402
from munim.ledger import Ledger         # noqa: E402


def main() -> None:
    s = get_settings()
    led = Ledger(s.data_dir / "khata.db", load_key(s.enc_key, s.data_dir))
    if "--all" in sys.argv:
        led.wipe_all()
        print("Ledger, customers and transcripts deleted.")
    else:
        print(f"{led.purge_transcripts(everything=True)} transcripts deleted.")


if __name__ == "__main__":
    main()
