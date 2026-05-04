"""
scripts/test_social_filter.py

Counterfactual test: does the production social pre-filter actually save P&L?

Replays the exact filter logic from wins/ingestion/collector.py against the
730-day social_history. For each day where the filter would have triggered
"skip", compare the forward returns it avoided vs the forward returns it
allowed. If "skip" days have materially worse forward returns, the filter
is earning its keep.

Filter rules (mirrored from collector.py — keep in sync):
    SOL: contrarian — skip when social_dominance >= 60th percentile of trailing 90d
    SUI: bullish    — skip when social_dominance <= 40th percentile,
                      boost  when social_dominance >= 70th percentile

Usage:
    python scripts/test_social_filter.py --csv output/social_history.csv
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, stdev, NormalDist

# Reuse loaders + stats from the main analysis script
from analyze_social_value import _load_csv, _welch_t


# ── Filter rules — must match wins/ingestion/collector.py ─────────────────────

SOCIAL_DIRECTION = {
    "SOL": "contrarian",
    "SUI": "bullish",
}

# Candidate additions discovered by widening the universe (2026-05-04 audit).
# Use --candidates to load these on top of the production rules.
CANDIDATE_DIRECTION = {
    "SNX": "contrarian",   # r=-0.22 @14d, p=2e-9
    "LDO": "contrarian",   # r=-0.18 @14d, p=1e-6
    "FTM": "bullish",      # r=+0.19 @14d, p=4e-5
    "APT": "bullish",      # r=+0.18 @14d, p=2e-6
}
CONTRARIAN_SKIP_ABOVE = 60.0
BULLISH_SKIP_BELOW    = 40.0
BULLISH_BOOST_ABOVE   = 70.0

LOOKBACK_DAYS = 90
HORIZONS      = [1, 3, 7, 14]


# ── Replay ────────────────────────────────────────────────────────────────────

def percentile(values: list[float], target: float) -> float:
    """Pct of values <= target. Matches the SQL: COUNT FILTER WHERE social_dominance <= $2."""
    if not values:
        return None
    return sum(1 for v in values if v <= target) * 100.0 / len(values)


def replay_filter(rows_by_token: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """
    For each (token, date), compute the production filter verdict using
    a trailing 90-day percentile (the same logic as
    _social_dominance_percentile + apply_social_filter). Returns rows enriched
    with `verdict` ∈ {skip, boost, proceed} plus pct/value.
    """
    out: dict[str, list[dict]] = {}
    for token, series in rows_by_token.items():
        if token not in SOCIAL_DIRECTION:
            continue
        direction = SOCIAL_DIRECTION[token]
        series = sorted(series, key=lambda r: r["date"])

        enriched = []
        for i, r in enumerate(series):
            sd = r.get("social_dominance")
            if sd is None:
                continue

            # Trailing 90-day window — values on or before this date, exclusive lookback only
            cutoff = r["date"] - timedelta(days=LOOKBACK_DAYS)
            window = [
                s["social_dominance"] for s in series[:i+1]
                if s["social_dominance"] is not None and s["date"] >= cutoff
            ]
            if len(window) < 30:
                continue

            pct = percentile(window, sd)

            verdict = "proceed"
            if direction == "contrarian" and pct >= CONTRARIAN_SKIP_ABOVE:
                verdict = "skip"
            elif direction == "bullish" and pct <= BULLISH_SKIP_BELOW:
                verdict = "skip"
            elif direction == "bullish" and pct >= BULLISH_BOOST_ABOVE:
                verdict = "boost"

            enriched.append({
                **r,
                "social_pct": pct,
                "verdict":    verdict,
                "direction":  direction,
            })

        out[token] = enriched
    return out


def attach_forward_returns(series: list[dict]) -> list[dict]:
    """Compute forward returns for each row in a token series."""
    by_date = {r["date"]: r for r in series}
    dates = sorted(by_date)
    for i, d in enumerate(dates):
        row = by_date[d]
        for h in HORIZONS:
            j = i + h
            if j < len(dates):
                p0 = row.get("price_close")
                p1 = by_date[dates[j]].get("price_close")
                if p0 and p1:
                    ret = (p1 - p0) / p0 * 100
                    if abs(ret) > 50 * math.sqrt(h):  # listing artifact filter
                        row[f"fwd_{h}d"] = None
                    else:
                        row[f"fwd_{h}d"] = ret
                else:
                    row[f"fwd_{h}d"] = None
            else:
                row[f"fwd_{h}d"] = None
    return list(by_date[d] for d in dates)


# ── Reporting ─────────────────────────────────────────────────────────────────

def report(token: str, rows: list[dict]):
    direction = SOCIAL_DIRECTION[token]
    print(f"\n{'═' * 72}")
    print(f"  {token} — direction: {direction.upper()}")
    print(f"{'═' * 72}")

    counts = defaultdict(int)
    for r in rows:
        counts[r["verdict"]] += 1
    total = sum(counts.values())
    print(f"  Days analyzed: {total}")
    for v in ["skip", "proceed", "boost"]:
        n = counts.get(v, 0)
        if n:
            print(f"    {v:<8} {n:>4} ({n/total*100:>4.1f}%)")

    print()
    print(f"  {'Verdict':<10} {'Horizon':>8} {'n':>5} {'mean ret':>10} {'win%':>6} "
          f"{'vs proceed':>12} {'p (Welch)':>10}")
    print(f"  {'─'*10} {'─'*8} {'─'*5} {'─'*10} {'─'*6} {'─'*12} {'─'*10}")

    summary = {}
    for h in HORIZONS:
        groups = defaultdict(list)
        for r in rows:
            ret = r.get(f"fwd_{h}d")
            if ret is None:
                continue
            groups[r["verdict"]].append(ret)

        proceed = groups.get("proceed", [])
        for v in ["skip", "proceed", "boost"]:
            rets = groups.get(v, [])
            if len(rets) < 5:
                continue
            avg = mean(rets)
            win = sum(1 for r in rets if r > 0) / len(rets) * 100
            if v != "proceed" and proceed:
                diff = avg - mean(proceed)
                tres = _welch_t(rets, proceed)
                p = tres[1] if tres else 1.0
                stars = ("***" if p < 0.001 else "** " if p < 0.01
                         else "*  " if p < 0.05 else "ns ")
                diff_str = f"{diff:+6.2f}pp"
                p_str    = f"{p:.4f} {stars}"
            else:
                diff_str = ""
                p_str    = ""
            print(f"  {v:<10} {h:>6}d  {len(rets):>5}  {avg:>+8.2f}%  "
                  f"{win:>5.1f}%  {diff_str:>12}  {p_str:>10}")
            summary[(v, h)] = {"avg": avg, "n": len(rets), "win": win}
    return summary


def verdict(all_summaries: dict, daily_calls_saved: int):
    print(f"\n{'═' * 72}")
    print("  IS THE FILTER EARNING ITS KEEP?")
    print(f"{'═' * 72}")

    # The filter "earns" if skip-day forward returns are materially worse than
    # proceed-day returns (we avoided losers). Use 3d horizon (typical hold).
    print()
    print("  Filter saves money when 'skip' days have WORSE forward returns")
    print("  than 'proceed' days (we successfully avoided them).\n")

    skip_savings_per_day = []
    for (token, summary) in all_summaries.items():
        for h in [3, 7]:
            if ("skip", h) in summary and ("proceed", h) in summary:
                skip = summary[("skip", h)]
                proc = summary[("proceed", h)]
                # Edge = how much worse the skipped days were than proceed days,
                # capped at zero (filter doesn't profit if skipped days were better)
                edge = proc["avg"] - skip["avg"]
                print(f"  {token} @ {h}d: skip avg {skip['avg']:+.2f}% vs "
                      f"proceed {proc['avg']:+.2f}%  → "
                      f"avoided loss: {edge:+.2f}pp/skip-day")
                if h == 3:
                    skip_savings_per_day.append((token, edge, skip['n']))

    print()
    if not skip_savings_per_day:
        print("  Insufficient data.")
        return

    total_savings_pp = sum(edge * n for _, edge, n in skip_savings_per_day)
    total_skips      = sum(n for _, _, n in skip_savings_per_day)
    if total_skips == 0:
        print("  No skips triggered.")
        return
    avg_pp_per_skip = total_savings_pp / total_skips

    # Annualized — over the 730-day window we have N skips
    skips_per_year = total_skips / 2  # 730 days ≈ 2 years
    print(f"  Total 'skip' events (3d horizon): {total_skips} over ~2 years")
    print(f"  Avg avoided-loss per skip-day:    {avg_pp_per_skip:+.2f}pp")
    print(f"  Annualized skip rate:             ~{skips_per_year:.0f} skips/yr")

    # ROI math: assume each "skip" would have been a position sized at X% of portfolio.
    # WINS sizes 5–10% per trade typically. Use 7.5% as midpoint.
    POS_PCT = 0.075
    PORTFOLIO = 10000
    avoided_loss_per_year = avg_pp_per_skip / 100 * POS_PCT * PORTFOLIO * skips_per_year
    print()
    print(f"  Assuming 7.5% position size on a $10k portfolio:")
    print(f"  Annual P&L saved by filter:       ${avoided_loss_per_year:+,.0f}")
    print(f"  LunarCrush cost:                  $1,800")
    print(f"  Net:                              ${avoided_loss_per_year - 1800:+,.0f}")
    print()

    # Claude API savings — each skip avoids one Opus call
    OPUS_COST_PER_CALL = 0.20  # rough mid-range for cached Opus call
    api_savings = total_skips * OPUS_COST_PER_CALL / 2  # per year
    print(f"  Bonus — Claude API calls suppressed: ~{int(skips_per_year)}/yr")
    print(f"  Estimated API savings (~$0.20/call): ${api_savings:.0f}/yr")
    print()
    total_value = avoided_loss_per_year + api_savings
    print(f"  TOTAL annual value of pre-filter:  ${total_value:+,.0f}")
    print(f"  vs LunarCrush cost:                 ${1800}")
    print()
    if total_value > 1800:
        print(f"  ▶  🟢 KEEP — filter generates ${total_value - 1800:+,.0f}/yr net")
    elif total_value > 900:
        print(f"  ▶  🟡 MARGINAL — covers half its cost. Consider downgrading plan")
        print(f"     or cancel & maintain via the cached daily SOL/SUI dominance.")
    else:
        print(f"  ▶  🔴 KILL — filter saves ${total_value:.0f}/yr; LunarCrush costs $1,800.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", required=True, help="path to social_history.csv export")
    p.add_argument("--holdout-from", default=None,
                   help="ISO date (YYYY-MM-DD): only score rows on/after this date "
                        "(out-of-sample test of the production rules)")
    p.add_argument("--candidates", action="store_true",
                   help="extend the filter map with discovered candidate tokens "
                        "(SNX/LDO contrarian, FTM/APT bullish)")
    args = p.parse_args()
    if args.candidates:
        SOCIAL_DIRECTION.update(CANDIDATE_DIRECTION)

    rows = _load_csv(args.csv)
    by_token = defaultdict(list)
    for r in rows:
        by_token[r["token"]].append(r)

    # Attach forward returns first (uses the full series), then replay filter
    for tok in by_token:
        by_token[tok] = attach_forward_returns(by_token[tok])

    enriched = replay_filter(dict(by_token))

    # Holdout filter — keep ALL data for percentile windows, but only report
    # statistics on rows on/after the holdout date.
    if args.holdout_from:
        holdout = date.fromisoformat(args.holdout_from)
        for tok in enriched:
            enriched[tok] = [r for r in enriched[tok] if r["date"] >= holdout]
        print(f"HOLDOUT TEST — scoring rows from {holdout} onward "
              f"(production rules fixed, never refit)")

    print(f"Loaded {len(rows)} rows; replaying filter on tokens: {sorted(enriched.keys())}")

    all_summaries = {}
    for tok in sorted(enriched.keys()):
        all_summaries[tok] = report(tok, enriched[tok])

    verdict(all_summaries, daily_calls_saved=0)


if __name__ == "__main__":
    main()
