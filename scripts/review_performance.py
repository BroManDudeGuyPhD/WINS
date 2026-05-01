"""
scripts/review_performance.py
Monthly signal accuracy review.
Prints a summary of decision accuracy vs actual outcomes from the trade log.
Run manually: python scripts/review_performance.py
"""
import asyncio
import asyncpg
from wins.shared.config import DATABASE_URL


async def main() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL)

    print("\n=== WINS Performance Review ===\n")

    # Win rate
    rows = await pool.fetch("""
        SELECT
            COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins,
            COUNT(*) FILTER (WHERE pnl_usd < 0) AS losses,
            COUNT(*) FILTER (WHERE pnl_usd = 0)  AS breakeven,
            COUNT(*)                              AS total,
            SUM(pnl_usd)                          AS total_pnl,
            AVG(pnl_pct)                          AS avg_pnl_pct
        FROM trade_log
        WHERE ts_close IS NOT NULL
    """)
    r = rows[0]
    print(f"Closed trades: {r['total']}")
    print(f"  Wins:     {r['wins']}")
    print(f"  Losses:   {r['losses']}")
    if r['total']:
        print(f"  Win rate: {r['wins']/r['total']*100:.1f}%")
    print(f"  Total PnL: ${r['total_pnl'] or 0:.2f}")
    print(f"  Avg return per trade: {r['avg_pnl_pct'] or 0:.2f}%")

    # Confidence calibration
    print("\nConfidence calibration (predicted vs actual):")
    cal_rows = await pool.fetch("""
        SELECT
            ROUND(d.confidence::numeric, 1) AS confidence_bucket,
            COUNT(*)                         AS decisions,
            COUNT(t.id)                      AS executed,
            AVG(t.pnl_pct)                   AS avg_pnl_pct
        FROM decision_log d
        LEFT JOIN trade_log t ON t.decision_id = d.id AND t.ts_close IS NOT NULL
        WHERE d.action = 'buy'
        GROUP BY 1
        ORDER BY 1
    """)
    for row in cal_rows:
        print(f"  confidence={row['confidence_bucket']}: "
              f"{row['decisions']} decisions, {row['executed']} executed, "
              f"avg_pnl={row['avg_pnl_pct'] or 0:.2f}%")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
