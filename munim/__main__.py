"""Run the server:  python -m munim"""

import logging

import uvicorn

from .config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    uvicorn.run("munim.server:api", host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
