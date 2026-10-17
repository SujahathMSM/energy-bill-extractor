"""Run the API on the host and port from settings (.env).

    python -m bill_extractor

Uses BILLX_HOST (default 127.0.0.1) and BILLX_PORT (default 8000), the same port setting that
docker compose uses, so one line in .env moves both.
"""

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("bill_extractor.api:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
