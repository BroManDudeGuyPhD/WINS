"""
wins/brain/main.py
Entry point for the wins-brain service.
Runs the decision cycle on a schedule (every DECISION_INTERVAL_MINUTES).
"""
import asyncio
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from wins.shared.config import (
    DECISION_INTERVAL_MINUTES, MECHANICAL_INTERVAL_MINUTES,
    CALIBRATION_DAY_OF_WEEK, CALIBRATION_HOUR_UTC, TRADE_MODE,
)
from wins.shared.db import get_pool
from wins.shared.logger import get_logger
from wins.brain.cycle import run_cycle, run_guardian_cycle
from wins.brain.calibration import compute_calibration
from wins.alerts.discord_bot import alert_calibration_report

log = get_logger("brain.main")

_HEARTBEAT = Path("/tmp/heartbeat")


async def run_calibration() -> None:
    """Weekly calibration, in-process. Uses the shared pool and does NOT close
    it (unlike the standalone calibration_cron script)."""
    try:
        pool = await get_pool()
        rows = await compute_calibration(pool)
        await alert_calibration_report(rows)
        log.info("Weekly calibration complete.")
    except Exception as exc:
        log.warning(f"Weekly calibration failed: {exc}")


async def main() -> None:
    log.info(
        f"WINS Brain starting. mode={TRADE_MODE} "
        f"decision={DECISION_INTERVAL_MINUTES}min guardian={MECHANICAL_INTERVAL_MINUTES}min"
    )

    scheduler = AsyncIOScheduler()
    # Expensive LLM cycle: entries + discretionary exits.
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=DECISION_INTERVAL_MINUTES,
        id="decision_cycle",
        max_instances=1,
    )
    # Cheap no-LLM guardian: enforce SL / target / trailing-stop frequently.
    scheduler.add_job(
        run_guardian_cycle,
        "interval",
        minutes=MECHANICAL_INTERVAL_MINUTES,
        id="guardian_cycle",
        max_instances=1,
    )
    # Weekly confidence calibration + Discord report.
    scheduler.add_job(
        run_calibration,
        "cron",
        day_of_week=CALIBRATION_DAY_OF_WEEK,
        hour=CALIBRATION_HOUR_UTC,
        id="calibration",
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
