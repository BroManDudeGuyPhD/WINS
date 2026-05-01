"""
wins/brain/main.py
Entry point for the wins-brain service.
Runs the decision cycle on a schedule (every DECISION_INTERVAL_MINUTES).
"""
import asyncio
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from wins.shared.config import DECISION_INTERVAL_MINUTES, TRADE_MODE
from wins.shared.logger import get_logger
from wins.brain.cycle import run_cycle

log = get_logger("brain.main")

_HEARTBEAT = Path("/tmp/heartbeat")


async def main() -> None:
    log.info(f"WINS Brain starting. mode={TRADE_MODE} interval={DECISION_INTERVAL_MINUTES}min")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=DECISION_INTERVAL_MINUTES,
        id="decision_cycle",
        max_instances=1,
    )
    scheduler.start()

    await run_cycle()

    try:
        while True:
            _HEARTBEAT.touch()
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("WINS Brain stopped.")


if __name__ == "__main__":
    asyncio.run(main())
