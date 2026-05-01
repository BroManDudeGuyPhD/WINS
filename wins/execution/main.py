"""wins/execution/main.py — Execution service entrypoint (standalone stub)."""
import asyncio
from wins.shared.logger import get_logger

log = get_logger("execution.main")


async def main() -> None:
    log.info("WINS Execution service started (passive — driven by brain.cycle).")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
