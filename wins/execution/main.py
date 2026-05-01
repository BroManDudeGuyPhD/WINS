"""wins/execution/main.py — Execution service entrypoint (standalone stub)."""
import asyncio
from pathlib import Path

from wins.shared.logger import get_logger

log = get_logger("execution.main")

_HEARTBEAT = Path("/tmp/heartbeat")


async def main() -> None:
    log.info("WINS Execution service started (passive — driven by brain.cycle).")
    while True:
        _HEARTBEAT.touch()
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
