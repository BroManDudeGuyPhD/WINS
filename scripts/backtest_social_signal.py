"""
scripts/backtest_social_signal.py

Rigorous signal-quality analysis: does LunarCrush social data have
statistically meaningful predictive power over forward price returns?

No Claude, no DB — pure signal-vs-outcome statistics.

Usage:
    python scripts/backtest_social_signal.py [--days 365] [--tokens all]

Requires:
    LUNARCRUSH_API_KEY  (Individual plan)
    COINGECKO_API_KEY   (optional — raises free-tier rate limit)

LunarCrush API budget: ~2 calls per token (time-series bulk pull).
27 tokens = ~54 calls total. Well within 2,000/day.
"""
from __future__ import annotations

import argparse
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from statistics import mean, median, stdev, NormalDist

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

LUNARCRUSH_KEY = os.environ.get("LUNARCRUSH_API_KEY", "")

LUNARCRUSH_BASE = "https://lunarcrush.com/api4/public"

# Full trading universe
LC_SYMBOLS = [
    "BTC", "ETH", "SOL", "AVAX", "DOT", "LINK", "ARB", "OP", "INJ", "SUI",
    "APT", "NEAR", "FTM", "ATOM", "ALGO", "AAVE", "UNI", "SNX", "CRV", "LDO",
    "DYDX", "GMX", "PENDLE", "JUP", "PYTH", "WIF", "BONK",
]

TARGET_TOKENS = ["SOL", "SUI", "JUP", "ARB", "LINK"]   # live trading universe

_LC_SLEEP = 6.5   # 10/min rate limit → 1 call every 6 seconds

HORIZONS     = [1, 2, 5, 10, 14]   # forward return windows (days)
SIGNAL_LAGS  = [0, 1, 2, 3]        # days to lag the social signal
# LunarCrush v2 provides price OHLCV — no CoinGecko needed for this backtest
METRICS      = ["galaxy_score", "sentiment", "alt_rank_inv", "interactions",
                "social_dominance", "contributors_active", "posts_created"]


# ── Fetching ──────────────────────────────────────────────────────────────────

def _fetch_lunarcrush(symbol: str, days: int) -> list[dict]:
    """
    Fetch hourly time-series from LunarCrush v2 (1 API call per token).
    Returns data aggregated to daily rows with price + social metrics.
    LunarCrush provides OHLCV directly — no CoinGecko needed.
    """
    end_ts   = int(datetime.now(timezone.utc).timestamp())
    start_ts = end_ts - days * 86400

    for attempt in range(4):
        try:
            resp = httpx.get(
                f"{LUNARCRUSH_BASE}/coins/{symbol.upper()}/time-series/v2",
                headers={"Authorization": f"Bearer {LUNARCRUSH_KEY}"},
                params={"start": start_ts, "end": end_ts},
                timeout=30,
            )
            break
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            wait = (2 ** attempt) * 5
            print(f"    [LC] {symbol} connection error (attempt {attempt+1}/4): {exc} — retrying in {wait}s")
            time.sleep(wait)
    else:
        print(f"    [LC] {symbol}: all retries exhausted")
        return []
    if resp.status_code == 429:
        print(f"    [LC] {symbol} rate limited — waiting 30s")
        time.sleep(30)
        return _fetch_lunarcrush(symbol, days)
    if resp.status_code != 200:
        print(f"    [LC] {symbol} HTTP {resp.status_code}: {resp.text[:150]}")
        return []

    hourly = resp.json().get("data") or []
    if not hourly:
        print(f"    [LC] {symbol}: empty data")
        return []

    # Aggregate hourly → daily
    # Social metrics: daily mean. Price: daily close (last hourly close of the day).
    by_date: dict = {}
    for r in hourly:
        ts = r.get("time")
        if not ts:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(r)

    result = []
    for d, hours in sorted(by_date.items()):
        def _avg(key):
            vals = [h[key] for h in hours if h.get(key) is not None]
            return mean(vals) if vals else None

        ar = _avg("alt_rank")
        result.append({
            "date":                d,
            "price":               hours[-1].get("close"),   # daily close
            "volume":              sum(h.get("volume_24h") or 0 for h in hours),
            "galaxy_score":        _avg("galaxy_score"),
            "alt_rank":            ar,
            "alt_rank_inv":        -ar if ar is not None else None,
            "sentiment":           _avg("sentiment"),
            "interactions":        _avg("interactions"),
            "social_dominance":    _avg("social_dominance"),
            "contributors_active": _avg("contributors_active"),
            "posts_created":       _avg("posts_created"),
        })
    return result


# ── Feature engineering ───────────────────────────────────────────────────────

def _add_social_momentum(social: list[dict], window: int = 3) -> list[dict]:
    """
    Add delta_<metric> = metric(T) - metric(T-window).
    Rising social momentum may be more predictive than absolute level.
    """
    by_date = {r["date"]: r for r in social}
    dates   = sorted(by_date)
    for i, d in enumerate(dates):
        row = by_date[d]
        prev_date = dates[i - window] if i >= window else None
        for m in ["galaxy_score", "sentiment", "alt_rank_inv"]:
            if prev_date and by_date[prev_date].get(m) is not None and row.get(m) is not None:
                row[f"delta_{m}"] = row[m] - by_date[prev_date][m]
            else:
                row[f"delta_{m}"] = None
    return social


def _build_rows(
    social: list[dict],
    horizons: list[int],
    lags: list[int],
) -> list[dict]:
    """
    Build analysis rows from LunarCrush daily data (price + social in one series).
    Attaches social signals at each lag and forward price returns at each horizon.
    """
    price_by_date  = {r["date"]: r["price"] for r in social if r.get("price")}
    social_by_date = {r["date"]: r for r in social}
    all_dates      = sorted(price_by_date)

    rows = []
    for i, d in enumerate(all_dates):
        price_t = price_by_date[d]
        if not price_t:
            continue

        row: dict = {"date": d}

        # Forward returns
        for h in horizons:
            future_date  = d + timedelta(days=h)
            price_future = price_by_date.get(future_date)
            if price_future:
                row[f"fwd_{h}d"] = (price_future - price_t) / price_t * 100
            else:
                row[f"fwd_{h}d"] = None

        # Social signal at each lag
        for lag in lags:
            lag_date     = all_dates[i - lag] if i >= lag else None
            social_point = social_by_date.get(lag_date) if lag_date else None
            suffix       = f"_lag{lag}" if lag > 0 else ""
            all_metric_cols = METRICS + ["delta_galaxy_score", "delta_sentiment", "delta_alt_rank_inv"]
            for m in all_metric_cols:
                row[f"{m}{suffix}"] = social_point.get(m) if social_point else None

        if any(row[f"fwd_{h}d"] is not None for h in horizons):
            rows.append(row)

    return rows


# ── Statistics ────────────────────────────────────────────────────────────────

def _pearson(xs: list, ys: list) -> tuple[float, float] | None:
    """Returns (r, p_value) or None if insufficient data."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 15:
        return None
    n      = len(pairs)
    xs2, ys2 = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = mean(xs2), mean(ys2)
    sx, sy = stdev(xs2), stdev(ys2)
    if sx == 0 or sy == 0:
        return None
    r = sum((x - mx) * (y - my) for x, y in zip(xs2, ys2)) / ((n - 1) * sx * sy)
    r = max(-1.0, min(1.0, r))
    # t-statistic and two-tailed p-value
    if abs(r) == 1.0:
        return r, 0.0
    t_stat = r * math.sqrt(n - 2) / math.sqrt(1 - r**2)
    # approximate p-value using normal distribution (good for n > 30)
    p_val  = 2 * (1 - NormalDist().cdf(abs(t_stat)))
    return r, p_val


def _win_rate(returns: list[float]) -> float:
    return sum(1 for r in returns if r > 0) / len(returns) * 100 if returns else 0.0


def _expected_value(returns: list[float]) -> float:
    """Win rate × avg win - loss rate × avg loss."""
    wins   = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    wr     = len(wins) / len(returns) if returns else 0
    avg_w  = mean(wins)   if wins   else 0
    avg_l  = mean(losses) if losses else 0
    return wr * avg_w + (1 - wr) * avg_l


def _quartile_split(rows: list[dict], signal_col: str, outcome_col: str):
    """Returns (q1_returns, q4_returns) for bottom and top quartile by signal."""
    valid = [(r[signal_col], r[outcome_col]) for r in rows
             if r.get(signal_col) is not None and r.get(outcome_col) is not None]
    if len(valid) < 20:
        return [], []
    valid.sort(key=lambda x: x[0])
    q = len(valid) // 4
    return [v[1] for v in valid[:q]], [v[1] for v in valid[3*q:]]


def _significance_stars(p: float) -> str:
    if p < 0.001: return "***"
    if p < 0.01:  return "** "
    if p < 0.05:  return "*  "
    return "   "


# ── BTC regime ────────────────────────────────────────────────────────────────

def _classify_regime(btc_series: list[dict]) -> dict:
    """
    Simple regime: bull if BTC close is above its 30d rolling average, bear otherwise.
    Returns {date: 'bull'|'bear'}.
    """
    by_date = {r["date"]: r["price"] for r in btc_series if r.get("price")}
    dates   = sorted(by_date)
    regimes = {}
    window  = 30
    for i, d in enumerate(dates):
        lookback = dates[max(0, i - window):i + 1]
        avg      = mean(by_date[dd] for dd in lookback)
        regimes[d] = "bull" if by_date[d] >= avg else "bear"
    return regimes


# ── Reporting ─────────────────────────────────────────────────────────────────

def _fmt_corr(result) -> str:
    if result is None:
        return "  n/a      "
    r, p = result
    return f"{r:+.3f} {_significance_stars(p)}"


def _section(title: str) -> None:
    print(f"\n  {'─'*52}")
    print(f"  {title}")
    print(f"  {'─'*52}")


def _analyse_token(
    symbol: str,
    rows: list[dict],
    regimes: dict,
    horizons: list[int],
    lags: list[int],
) -> dict:
    """Full analysis for one token. Returns summary dict for aggregate report."""
    print(f"\n{'═'*60}")
    print(f"  {symbol}  ({len(rows)} days)")
    print(f"{'═'*60}")

    if len(rows) < 20:
        print("  Insufficient data.\n")
        return {}

    # ── 1. Correlation matrix: signal × horizon ───────────────────────────────
    _section("PEARSON r  (signal → forward return) | * p<.05  ** p<.01  *** p<.001")

    primary_signals = [
        ("galaxy_score",       "Galaxy Score   (lag 0)"),
        ("galaxy_score_lag1",  "Galaxy Score   (lag 1d)"),
        ("galaxy_score_lag2",  "Galaxy Score   (lag 2d)"),
        ("delta_galaxy_score", "ΔGalaxy Score  (3d momentum)"),
        ("sentiment",          "Sentiment      (lag 0)"),
        ("delta_sentiment",    "ΔSentiment     (3d momentum)"),
        ("alt_rank_inv",       "Alt Rank (inv) (lag 0)"),
        ("delta_alt_rank_inv", "ΔAlt Rank inv  (3d momentum)"),
        ("interactions",        "Interactions   (lag 0)"),
        ("social_dominance",   "Social Dominance (lag 0)"),
        ("contributors_active","Contributors   (lag 0)"),
    ]

    horizon_header = "  ".join(f"{h:>5}d" for h in horizons)
    print(f"  {'Signal':<36}  {horizon_header}")

    best_signal, best_r, best_h = None, 0.0, None

    for col, label in primary_signals:
        corrs = []
        for h in horizons:
            outcome = f"fwd_{h}d"
            xs = [r.get(col) for r in rows]
            ys = [r.get(outcome) for r in rows]
            result = _pearson(xs, ys)
            corrs.append(result)
            if result and abs(result[0]) > abs(best_r):
                best_r, best_signal, best_h = result[0], col, h
        print(f"  {label:<36}  " + "  ".join(_fmt_corr(c) for c in corrs))

    # ── 2. Quartile win-rate lift (galaxy_score, best horizon) ───────────────
    _section(f"QUARTILE WIN-RATE — galaxy_score vs {best_h}d forward return")

    outcome = f"fwd_{best_h}d"
    valid = [(r["galaxy_score"], r[outcome]) for r in rows
             if r.get("galaxy_score") is not None and r.get(outcome) is not None]
    if len(valid) >= 20:
        valid.sort(key=lambda x: x[0])
        q = len(valid) // 4
        buckets = [
            ("Q1 (weakest social)", valid[:q]),
            ("Q2",                  valid[q:2*q]),
            ("Q3",                  valid[2*q:3*q]),
            ("Q4 (strongest social)",valid[3*q:]),
        ]
        print(f"  {'Quartile':<26}  {'n':>4}  {'Win%':>6}  {'Avg%':>7}  {'EV%':>7}")
        for label, group in buckets:
            returns = [v[1] for v in group]
            if not returns:
                continue
            print(f"  {label:<26}  {len(returns):>4}  "
                  f"{_win_rate(returns):>5.1f}%  "
                  f"{mean(returns):>+6.2f}%  "
                  f"{_expected_value(returns):>+6.2f}%")

    # ── 3. Regime split ───────────────────────────────────────────────────────
    _section(f"REGIME SPLIT — does social signal work in bull vs bear? ({best_h}d)")

    bull_rows = [r for r in rows if regimes.get(r["date"]) == "bull" and r.get("galaxy_score") is not None]
    bear_rows = [r for r in rows if regimes.get(r["date"]) == "bear" and r.get("galaxy_score") is not None]

    for regime_label, regime_rows in [("Bull market", bull_rows), ("Bear market", bear_rows)]:
        if len(regime_rows) < 10:
            print(f"  {regime_label}: insufficient data (n={len(regime_rows)})")
            continue
        q1, q4 = _quartile_split(regime_rows, "galaxy_score", outcome)
        lift = _win_rate(q4) - _win_rate(q1) if q1 and q4 else 0
        print(f"  {regime_label} (n={len(regime_rows):3d}): "
              f"Q4 win={_win_rate(q4):4.1f}%  Q1 win={_win_rate(q1):4.1f}%  "
              f"lift={lift:+.1f}pp  Q4 EV={_expected_value(q4):+.2f}%")

    # ── 4. Simple rule: buy when galaxy_score > median ────────────────────────
    _section("SIMPLE TRADING RULE — buy when galaxy_score > median")

    gs_vals = [r["galaxy_score"] for r in rows if r.get("galaxy_score") is not None]
    med_gs  = median(gs_vals) if gs_vals else None
    summary = {}

    if med_gs is not None:
        print(f"  Median galaxy_score: {med_gs:.1f}")
        print(f"  {'Horizon':>8}  {'Above n':>7}  {'Above win%':>10}  {'Above EV%':>9}  "
              f"{'Below win%':>10}  {'Below EV%':>9}  {'Lift':>6}")
        for h in horizons:
            outcome = f"fwd_{h}d"
            above   = [r[outcome] for r in rows if r.get("galaxy_score", 0) > med_gs and r.get(outcome) is not None]
            below   = [r[outcome] for r in rows if r.get("galaxy_score", 0) <= med_gs and r.get(outcome) is not None]
            if not above or not below:
                continue
            lift = _win_rate(above) - _win_rate(below)
            print(f"  {h:>7}d  {len(above):>7}  {_win_rate(above):>9.1f}%  "
                  f"{_expected_value(above):>+8.2f}%  "
                  f"{_win_rate(below):>9.1f}%  "
                  f"{_expected_value(below):>+8.2f}%  "
                  f"{lift:>+5.1f}pp")
            summary[h] = {"lift": lift, "ev_above": _expected_value(above), "n": len(above)}

    return summary


# ── Aggregate ─────────────────────────────────────────────────────────────────

def _aggregate_report(all_summaries: dict[str, dict], horizons: list[int]) -> None:
    print(f"\n{'═'*60}")
    print("  AGGREGATE — ALL TOKENS COMBINED")
    print(f"{'═'*60}")
    print()
    print("  Is $150/mo worth it? Decision guide:")
    print()
    print(f"  {'Horizon':>8}  {'Avg Lift':>9}  {'Tokens +ve':>10}  {'Verdict'}")
    print(f"  {'─'*8}  {'─'*9}  {'─'*10}  {'─'*30}")

    for h in horizons:
        lifts = [s[h]["lift"] for s in all_summaries.values() if h in s]
        if not lifts:
            continue
        avg_lift = mean(lifts)
        positive = sum(1 for l in lifts if l > 0)

        if avg_lift >= 10:
            verdict = "STRONG SIGNAL — pay for it"
        elif avg_lift >= 5:
            verdict = "MODERATE — worth testing live"
        elif avg_lift >= 2:
            verdict = "WEAK — marginal, monitor"
        else:
            verdict = "NO SIGNAL — save your money"

        print(f"  {h:>7}d  {avg_lift:>+8.1f}pp  {positive:>3}/{len(lifts)} tokens  {verdict}")

    print()
    print("  Correlation interpretation:")
    print("    |r| < 0.10  → noise")
    print("    |r| 0.10–0.20 → weak, probably not worth paying for alone")
    print("    |r| 0.20–0.35 → moderate — pair with price signals")
    print("    |r| > 0.35   → strong — clear independent alpha")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",    type=int, default=365,
                        help="Days of history (default: 365)")
    parser.add_argument("--tokens",  type=str, default="all",
                        help="Comma-separated tokens, or 'all' (default: all), or 'targets' for live universe")
    parser.add_argument("--horizon", type=str, default=",".join(str(h) for h in HORIZONS),
                        help="Forward return horizons in days")
    args = parser.parse_args()

    if not LUNARCRUSH_KEY:
        print("ERROR: LUNARCRUSH_API_KEY not set.")
        return

    if args.tokens == "all":
        tokens = LC_SYMBOLS
    elif args.tokens == "targets":
        tokens = TARGET_TOKENS
    else:
        tokens = [t.strip().upper() for t in args.tokens.split(",")]

    horizons   = [int(h.strip()) for h in args.horizon.split(",")]
    fetch_days = args.days + max(horizons) + 5

    print(f"Social signal backtest  (LunarCrush only — no CoinGecko needed)")
    print(f"  History : {args.days}d  |  Horizons: {horizons}d  |  Lags tested: {SIGNAL_LAGS}")
    print(f"  Tokens  : {len(tokens)} ({', '.join(tokens)})")
    print(f"  LC key  : ...{LUNARCRUSH_KEY[-6:]}  |  {len(tokens)} API calls  |  "
          f"~{len(tokens) * _LC_SLEEP / 60:.1f} min runtime")
    print()

    # BTC fetched first — reused for regime classification and as a token if in list
    print("[BTC] Fetching for regime classification…", end=" ", flush=True)
    btc_series = _fetch_lunarcrush("BTC", fetch_days)
    regimes    = _classify_regime(btc_series) if btc_series else {}
    print(f"{len(btc_series)} days  |  "
          f"bull={sum(1 for v in regimes.values() if v == 'bull')}d  "
          f"bear={sum(1 for v in regimes.values() if v == 'bear')}d")
    time.sleep(_LC_SLEEP)

    all_summaries: dict[str, dict] = {}

    for i, symbol in enumerate(tokens):
        if symbol == "BTC":
            series = btc_series
            print(f"\n[{i+1}/{len(tokens)}] BTC  (reusing fetched data)")
        else:
            print(f"\n[{i+1}/{len(tokens)}] {symbol}")
            print(f"  Fetching LunarCrush…", end=" ", flush=True)
            series = _fetch_lunarcrush(symbol, fetch_days)
            print(f"{len(series)} days")
            time.sleep(_LC_SLEEP)

        if not series:
            continue

        series = _add_social_momentum(series)
        rows   = _build_rows(series, horizons, SIGNAL_LAGS)

        summary = _analyse_token(symbol, rows, regimes, horizons, SIGNAL_LAGS)
        if summary:
            all_summaries[symbol] = summary

    if all_summaries:
        _aggregate_report(all_summaries, horizons)


if __name__ == "__main__":
    main()
