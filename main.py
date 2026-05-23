#!/usr/bin/env python3

import asyncio
import logging
import sys

from application import run


def setup_logging():
    level = logging.DEBUG if "--debug" in sys.argv else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


if __name__ == "__main__":
    setup_logging()
    asyncio.run(run())
