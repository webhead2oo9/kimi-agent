from __future__ import annotations

import logging
import sys

from app.runtime import build_app
from config.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)


def main() -> None:
    app = build_app(settings)
    app.run()


if __name__ == "__main__":
    main()
