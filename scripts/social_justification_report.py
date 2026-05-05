"""
scripts/social_justification_report.py

Unified status view:
  1. MAIN book — open positions + P&L from trade_log (the live bot)
  2. SOCIAL-JUSTIFICATION book — paper trades from sj_trades, vs backtest expectation
     (median 7d +2.7%, 60% beat baseline)

Usage:
    python scripts/social_justification_report.py
    python scripts/social_justification_report.py --by-token
"""
import argparse
import asyncio
import os
import sys

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

BACKTEST_MEDIAN_PCT = 2.72   # median 7d return on the long-fade setup, from study
BACKTEST_HIT_RATE   = 0.596  # share of setups beating per-token baseline


SUMMARY_SQL = """
SELECT
  COUNT(*) FILTER (WHERE status = 'open')                           AS n_open,
  COUNT(*) FILTER (WHERE status = 'closed')                         AS n_closed,
  COUNT(*) FILTER (WHERE status = 'closed' AND pnl_pct > 0)         AS n_wins,
  ROUND(AVG(pnl_pct) FILTER (WHERE status = 'closed')::numeric, 4)  AS mean_pnl,
  ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_pct) FILTER (WHERE status = 'closed'))::numeric, 4) AS med_pnl,
  ROUND(STDDEV(pnl_pct) FILTER (WHERE status = 'closed')::numeric, 4) AS sd_pnl,
  ROUND(SUM(pnl_pct) FILTER (WHERE status = 'closed')::numeric, 4)  AS sum_pnl,
  MIN(entry_date)                                                   AS first_entry,
  MAX(entry_date)                                                   AS last_entry
FROM sj_trades;
"""

BY_TOKEN_SQL = """
SELECT token,
  COUNT(*)                                                  AS n,
  COUNT(*) FILTER (WHERE status = 'closed')                 AS n_closed,
  COUNT(*) FILTER (WHERE status = 'closed' AND pnl_pct > 0) AS n_wins,
  ROUND((100*AVG(pnl_pct) FILTER (WHERE status = 'closed'))::numeric, 2) AS mean_pnl_pct,
  ROUND((100*PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_pct) FILTER (WHERE status = 'closed'))::numeric, 2) AS med_pnl_pct
FROM sj_trades GROUP BY token ORDER BY n DESC, token;
"""

RECENT_SQL = """
SELECT token, entry_date, status, planned_exit_date, exit_date,
  ROUND((100*entry_z)::numeric, 1)        AS entry_z_x100,
  ROUND((100*entry_prior_r7)::numeric, 2) AS prior_r7_pct,
  ROUND((100*pnl_pct)::numeric, 2)        AS pnl_pct
FROM sj_trades ORDER BY entry_date DESC LIMIT 25;
"""


MAIN_OPEN_SQL = """
SELECT token, side, qty, entry_price, stop_loss_price, target_price, ts_open, trade_mode
FROM trade_log
WHERE ts_close IS NULL
ORDER BY ts_open DESC
"""

MAIN_SUMMARY_SQL = """
SELECT
  COUNT(*) FILTER (WHERE ts_close IS NULL)                              AS n_open,
  COUNT(*) FILTER (WHERE ts_close IS NOT NULL)                          AS n_closed,
  COUNT(*) FILTER (WHERE ts_close IS NOT NULL AND pnl_usd > 0)          AS n_wins,
  ROUND(SUM(pnl_usd) FILTER (WHERE ts_close IS NOT NULL)::numeric, 2)   AS sum_pnl_usd,
  ROUND(AVG(pnl_pct) FILTER (WHERE ts_close IS NOT NULL)::numeric, 4)   AS mean_pnl_pct,
  MIN(ts_open)                                                          AS first_trade,
  MAX(ts_open)                                                          AS last_trade
FROM trade_log
"""

SYSTEM_STATE_SQL = """
SELECT capital_usd, run_starting_capital, trade_mode, phase, system_paused, pause_reason, open_positions
FROM system_state ORDER BY ts DESC LIMIT 1
"""


def fmt_pct(v) -> str:
    return "n/a" if v is None else f"{float(v)*100:+.2f}%"


def fmt_usd(v) -> str:
    return "n/a" if v is None else f"${float(v):+,.2f}"


async def render_main_book(conn) -> None:
    print("=== MAIN book (live bot — trade_log) ===")
    state = await conn.fetchrow(SYSTEM_STATE_SQL)
    if state:
        paused = "PAUSED" if state["system_paused"] else "running"
        capital = float(state["capital_usd"]) if state["capital_usd"] is not None else 0.0
        starting = float(state["run_starting_capital"]) if state["run_starting_capital"] is not None else None
        dd_pct = ((capital - starting) / starting * 100) if starting else None
        print(f"phase:           {state['phase']}  ({state['trade_mode']}, {paused})")
        print(f"capital:         ${capital:,.2f}" + (f"  (run start ${starting:,.2f}, {dd_pct:+.2f}%)" if starting else ""))
        if state["pause_reason"]:
            print(f"pause reason:    {state['pause_reason']}")

    s = await conn.fetchrow(MAIN_SUMMARY_SQL)
    n_open   = s["n_open"] or 0
    n_closed = s["n_closed"] or 0
    n_wins   = s["n_wins"] or 0
    win_rate = (n_wins / n_closed) if n_closed else None
    print(f"open positions:  {n_open}")
    print(f"closed trades:   {n_closed}")
    if n_closed:
        print(f"win rate:        {win_rate:.1%}")
        print(f"mean pnl:        {fmt_pct(s['mean_pnl_pct'])}")
        print(f"sum pnl:         {fmt_usd(s['sum_pnl_usd'])}")
        print(f"date range:      {s['first_trade']} → {s['last_trade']}")

    if n_open:
        print("\n--- open positions ---")
        opens = await conn.fetch(MAIN_OPEN_SQL)
        print(f"{'token':<7}{'side':<6}{'qty':>14}{'entry':>14}{'stop':>14}{'target':>14}  opened")
        for r in opens:
            print(
                f"{r['token']:<7}{r['side']:<6}"
                f"{float(r['qty'] or 0):>14.6f}{float(r['entry_price'] or 0):>14.6f}"
                f"{float(r['stop_loss_price'] or 0):>14.6f}{float(r['target_price'] or 0):>14.6f}  "
                f"{r['ts_open']:%Y-%m-%d %H:%M}  {r['trade_mode']}"
            )


async def main(by_token: bool) -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set"); sys.exit(1)
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await render_main_book(conn)
        print()
        print("=== SOCIAL-JUSTIFICATION book (paper — sj_trades) ===")
        s = await conn.fetchrow(SUMMARY_SQL)
        n_open   = s["n_open"]
        n_closed = s["n_closed"]
        n_wins   = s["n_wins"] or 0
        win_rate = (n_wins / n_closed) if n_closed else None

        print(f"open positions:  {n_open}")
        print(f"closed trades:   {n_closed}")
        if n_closed:
            print(f"win rate:        {win_rate:.1%}  (backtest baseline-beat rate: {BACKTEST_HIT_RATE:.1%})")
            print(f"mean pnl:        {fmt_pct(s['mean_pnl'])}")
            print(f"median pnl:      {fmt_pct(s['med_pnl'])}  (backtest median: +{BACKTEST_MEDIAN_PCT:.2f}%)")
            print(f"stddev pnl:      {fmt_pct(s['sd_pnl'])}")
            print(f"sum pnl (eq-wt): {fmt_pct(s['sum_pnl'])}")
            print(f"date range:      {s['first_entry']} → {s['last_entry']}")

            # Verdict
            if s["med_pnl"] is not None:
                med_pct = float(s["med_pnl"]) * 100
                if med_pct >= BACKTEST_MEDIAN_PCT * 0.5:
                    verdict = "ON TRACK — live median within 50% of backtest"
                elif med_pct > 0:
                    verdict = "UNDERPERFORMING — positive but well below backtest"
                else:
                    verdict = "BROKEN — live median negative; signal may not generalize"
                print(f"verdict:         {verdict}")
        else:
            print("(no closed trades yet — wait for the first 7d hold cycles to complete)")

        if by_token:
            print("\n--- by token ---")
            rows = await conn.fetch(BY_TOKEN_SQL)
            print(f"{'token':<8}{'n':>4}{'closed':>8}{'wins':>6}{'mean%':>9}{'med%':>9}")
            for r in rows:
                print(
                    f"{r['token']:<8}{r['n']:>4}{r['n_closed']:>8}{r['n_wins'] or 0:>6}"
                    f"{(r['mean_pnl_pct'] if r['mean_pnl_pct'] is not None else 0):>9}"
                    f"{(r['med_pnl_pct']  if r['med_pnl_pct']  is not None else 0):>9}"
                )

        print("\n--- 25 most recent setups ---")
        recent = await conn.fetch(RECENT_SQL)
        print(f"{'token':<7}{'entry':<12}{'status':<8}{'plan_exit':<12}{'exit':<12}{'z':>7}{'prior_r7%':>11}{'pnl%':>8}")
        for r in recent:
            print(
                f"{r['token']:<7}{str(r['entry_date']):<12}{r['status']:<8}"
                f"{str(r['planned_exit_date']):<12}{str(r['exit_date'] or '-'):<12}"
                f"{(r['entry_z_x100'] or 0)/100:>7.2f}"
                f"{(r['prior_r7_pct'] if r['prior_r7_pct'] is not None else 0):>11}"
                f"{(r['pnl_pct'] if r['pnl_pct'] is not None else 0):>8}"
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--by-token", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.by_token))
