"""
wins/brain/calibration_cron.py

Weekly calibration job. Computes per-bucket confidence multipliers from
closed trade history and posts the report to Discord.

Run manually or schedule via cron:
    python -m wins.brain.calibration_cron

Recommended schedule: weekly, e.g. every Sunday at 09:00.
"""
import asyncio

from wins.shared.db import get_pool
from wins.shared.logger import get_logger
from wins.brain.calibration import compute_calibration
from wins.alerts.discord_bot import alert_calibration_report

log = get_logger("brain.calibration_cron")


async def run() -> None:
    pool = await get_pool()
    log.info("Running weekly calibration…")
    rows = await compute_calibration(pool)
    await alert_calibration_report(rows)
    log.info("Calibration complete.")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
