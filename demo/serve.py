"""The app server plus the demo stage at /__demo (used only by record_demo.py)."""

import logging
import sys
from pathlib import Path

import uvicorn
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from munim.config import get_settings   # noqa: E402
from munim.server import api            # noqa: E402

api.router.routes.insert(0, Mount("/__demo", StaticFiles(directory=ROOT / "demo"), name="demo"))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    uvicorn.run(api, host=s.host, port=s.port, log_level="info")
