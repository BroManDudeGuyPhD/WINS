"""
wins/brain/calibration.py

Tracks whether Claude's confidence scores match realized win rates.
Computes a per-bucket multiplier so risk.py can adjust effective confidence.

Buckets:
  low   0.65 – 0.75
  mid   0.75 – 0.85
  high  0.85+

Multiplier = realized win rate in that bucket.
  effective_confidence = raw_confidence × multiplier

Enforcement is withheld until MIN_TRADES_TO_ENFORCE closed trades exist per
bucket — before that threshold the numbers are display-only.
"""
from __future__ import annotations

from decimal import Decimal

import asyncpg

from wins.shared.logger import get_logger

log = get_logger("brain.calibration")

MIN_TRADES_TO_ENFORCE = 30

_BUCKETS: list[tuple[str, float, float]] = [
    ("low",  0.65, 0.75),
    ("mid",  0.75, 0.85),
    ("high", 0.85, 1.01),
]


def _bucket_for(confidence: Decimal) -> str:
    c = float(confidence)
    if c < 0.75:
        return "low"
    if c < 0.85:
        return "mid"
    return "high"


# ---------------------------------------------------------------------------
# Compute and store
# ---------------------------------------------------------------------------

async def compute_calibration(pool: asyncpg.Pool) -> list[dict]:
    """
    Pull all closed buy trades joined with their decision confidence,
    group by bucket, compute win rates, and insert a new calibration_result row
    per bucket. Returns the computed rows for immediate reporting.
    """
    rows = await pool.fetch(
        """
        SELECT d.confidence, t.pnl_pct
        FROM trade_log t
        JOIN decision_log d ON d.id = t.decision_id
        WHERE t.ts_close IS NOT NULL
          AND t.side = 'buy'
          AND t.pnl_pct IS NOT NULL
          AND d.confidence IS NOT NULL
        """
    )

    results = []
    for bucket, lo, hi in _BUCKETS:
        group = [r for r in rows if lo <= float(r["confidence"]) < hi]
        count = len(group)
        wins = sum(1 for r in group if float(r["pnl_pct"]) > 0)
        win_rate = wins / count if count > 0 else 0.0
        multiplier = win_rate
        enforced = count >= MIN_TRADES_TO_ENFORCE

        await pool.execute(
            """
            INSERT INTO calibration_result
              (bucket, trade_count, win_count, win_rate, multiplier, enforced)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            bucket, count, wins,
            round(win_rate, 4), round(multiplier, 4), enforced,
        )

        results.append({
            "bucket":      bucket,
            "trade_count": count,
            "win_count":   wins,
            "win_rate":    win_rate,
            "multiplier":  multiplier,
            "enforced":    enforced,
        })

        status = "enforced" if enforced else f"display-only ({count}/{MIN_TRADES_TO_ENFORCE} trades)"
        log.info(
            f"Calibration [{bucket}]: {count} trades, "
            f"win_rate={win_rate:.1%}, multiplier={multiplier:.3f} [{status}]"
        )

    return results


# ---------------------------------------------------------------------------
# Read — called each cycle by cycle.py
# ---------------------------------------------------------------------------

async def get_calibration_multipliers(pool: asyncpg.Pool) -> dict[str, Decimal]:
    """
    Returns the latest enforced multiplier per bucket as {bucket: multiplier}.
    Buckets below MIN_TRADES_TO_ENFORCE are excluded — they return no entry,
    meaning risk.py will use raw confidence unchanged for that bucket.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (bucket) bucket, multiplier, enforced
        FROM calibration_result
        ORDER BY bucket, ts DESC
        """
    )
    return {
        r["bucket"]: Decimal(str(r["multiplier"]))
        for r in rows
        if r["enforced"]
    }


# ---------------------------------------------------------------------------
# Apply — pure function, used by risk.py
# ---------------------------------------------------------------------------

def apply_calibration(raw: Decimal, multipliers: dict[str, Decimal]) -> Decimal:
    """
    Return effective confidence after applying the bucket multiplier.
    If the bucket has no enforced multiplier, returns raw unchanged.
    """
    bucket = _bucket_for(raw)
    mult = multipliers.get(bucket)
    if mult is None:
        return raw
    return raw * mult


# ---------------------------------------------------------------------------
# Format for Discord
# ---------------------------------------------------------------------------

_BUCKET_LABELS = {"low": "0.65–0.75", "mid": "0.75–0.85", "high": "0.85+"}


def format_calibration_report(rows: list[dict]) -> str:
    lines = ["**Confidence Calibration Report**", ""]
    for r in rows:
        label    = _BUCKET_LABELS.get(r["bucket"], r["bucket"])
        status   = "enforced" if r["enforced"] else f"display-only ({r['trade_count']}/{MIN_TRADES_TO_ENFORCE})"
        lines.append(
            f"`{label}`  n={r['trade_count']}  "
            f"win={r['win_rate']:.1%}  mult={r['multiplier']:.3f}  [{status}]"
        )
    if not any(r["enforced"] for r in rows):
        lines.append("")
        lines.append(
            f"_Multiplier enforcement inactive — need {MIN_TRADES_TO_ENFORCE} "
            "closed trades per bucket._"
        )
    return "\n".join(lines)
