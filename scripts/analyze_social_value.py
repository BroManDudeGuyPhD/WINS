"""
scripts/analyze_social_value.py

Hard-data verdict: is LunarCrush ($150/mo) worth keeping?

Runs a battery of statistical tests against the cached social_history table.
Each test has a numerical threshold. The script aggregates them into a
KEEP / MARGINAL / KILL verdict with a dollar-ROI calculation.

Usage:
    # Run against the stg DB (set DATABASE_URL or pass --db).
    python scripts/analyze_social_value.py

    # Export to a local CSV first, then analyze offline (re-runnable, free):
    python scripts/analyze_social_value.py --export output/social_history.csv
    python scripts/analyze_social_value.py --csv    output/social_history.csv

    # Tweak the cost model:
    python scripts/analyze_social_value.py --csv ... --portfolio 25000 --trades-per-year 60

Pure stdlib + asyncpg. No scipy / pandas — keeps deploy footprint clean.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, median, pstdev, stdev, NormalDist


# ── Config ────────────────────────────────────────────────────────────────────

COST_PER_YEAR     = 1800.0     # $150/mo × 12
SIGNALS           = ["galaxy_score", "sentiment", "alt_rank_inv", "social_dominance"]
HORIZONS          = [1, 3, 7, 14]   # forward return windows (days)
LAGS              = [0, 1, 2]       # signal lag (days)
TRADING_TOKENS    = {"SOL", "SUI", "JUP", "ARB", "LINK"}   # excludes BTC/ETH macro
MIN_PAIRS         = 30              # min sample for any correlation
SIG_LEVEL         = 0.05
WARMUP_DAYS       = 30              # drop first N days/token (illiquid listing period)
MAX_DAILY_RET_PCT = 50.0            # clip daily returns above this (data-quality filter)


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_csv(path: str) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        out.append({
            "token":            r["token"],
            "date":             date.fromisoformat(r["date"]),
            "galaxy_score":     _f(r.get("galaxy_score")),
            "sentiment":        _f(r.get("sentiment")),
            "alt_rank":         _f(r.get("alt_rank")),
            "alt_rank_inv":     -_f(r["alt_rank"]) if r.get("alt_rank") else None,
            "social_dominance": _f(r.get("social_dominance")),
            "price_close":      _f(r.get("price_close")),
        })
    return out


def _f(v):
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


async def _load_db(db_url: str) -> list[dict]:
    import asyncpg
    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch("""
            SELECT token, date, galaxy_score, sentiment, alt_rank,
                   social_dominance, price_close
              FROM social_history
             WHERE price_close IS NOT NULL
             ORDER BY token, date
        """)
    finally:
        await conn.close()
    return [
        {
            "token":            r["token"],
            "date":             r["date"],
            "galaxy_score":     r["galaxy_score"],
            "sentiment":        r["sentiment"],
            "alt_rank":         r["alt_rank"],
            "alt_rank_inv":     -r["alt_rank"] if r["alt_rank"] is not None else None,
            "social_dominance": r["social_dominance"],
            "price_close":      r["price_close"],
        }
        for r in rows
    ]


def _export_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["token", "date", "galaxy_score", "sentiment", "alt_rank",
                    "social_dominance", "price_close"])
        for r in rows:
            w.writerow([
                r["token"], r["date"].isoformat(),
                r.get("galaxy_score"), r.get("sentiment"), r.get("alt_rank"),
                r.get("social_dominance"), r.get("price_close"),
            ])


# ── Feature engineering ───────────────────────────────────────────────────────

def _build_panel(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by token, sort by date, attach forward returns and lagged signals."""
    by_token: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_token[r["token"]].append(r)

    # Drop warmup period for each token (early-listing illiquidity)
    for tok in list(by_token):
        by_token[tok].sort(key=lambda r: r["date"])
        by_token[tok] = by_token[tok][WARMUP_DAYS:]

    for tok, series in by_token.items():
        price_by_idx = [r["price_close"] for r in series]

        for i, row in enumerate(series):
            # Forward returns (filter implausible moves — listing artifacts)
            for h in HORIZONS:
                j = i + h
                if j < len(series) and price_by_idx[i] and price_by_idx[j]:
                    ret = (price_by_idx[j] - price_by_idx[i]) / price_by_idx[i] * 100
                    # Drop returns whose annualized magnitude is implausible
                    # (e.g., 100% in 1d, 200% in 7d). Listing/illiquidity noise, not signal.
                    if abs(ret) > MAX_DAILY_RET_PCT * math.sqrt(h):
                        row[f"fwd_{h}d"] = None
                    else:
                        row[f"fwd_{h}d"] = ret
                else:
                    row[f"fwd_{h}d"] = None

            # Lagged signals (for predictive separation)
            for sig in SIGNALS:
                for lag in LAGS:
                    if lag == 0:
                        continue
                    k = i - lag
                    row[f"{sig}_lag{lag}"] = series[k][sig] if k >= 0 else None

            # 3-day momentum on key signals
            for sig in ["galaxy_score", "sentiment", "alt_rank_inv"]:
                k = i - 3
                if k >= 0 and series[k].get(sig) is not None and row.get(sig) is not None:
                    row[f"d3_{sig}"] = row[sig] - series[k][sig]
                else:
                    row[f"d3_{sig}"] = None

    return dict(by_token)


# ── Statistics primitives ─────────────────────────────────────────────────────

def _pairs(rows, xcol, ycol):
    return [(r[xcol], r[ycol]) for r in rows
            if r.get(xcol) is not None and r.get(ycol) is not None]


def _pearson(pairs):
    n = len(pairs)
    if n < MIN_PAIRS:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = mean(xs), mean(ys)
    sx, sy = stdev(xs), stdev(ys)
    if sx == 0 or sy == 0:
        return None
    r = sum((x - mx) * (y - my) for x, y in pairs) / ((n - 1) * sx * sy)
    r = max(-1.0, min(1.0, r))
    if abs(r) >= 1.0:
        return r, 0.0, n
    t = r * math.sqrt(n - 2) / math.sqrt(1 - r * r)
    p = 2 * (1 - NormalDist().cdf(abs(t)))
    return r, p, n


def _rank(vals):
    """Average-rank: ties get average rank. Returns list of ranks aligned to input."""
    indexed = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and vals[indexed[j+1]] == vals[indexed[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    return ranks


def _spearman(pairs, min_n: int = MIN_PAIRS):
    """Spearman rank correlation = Pearson on ranks. Returns (rho, p, n)."""
    n = len(pairs)
    if n < min_n:
        return None
    rx = _rank([p[0] for p in pairs])
    ry = _rank([p[1] for p in pairs])
    # Inline Pearson with relaxed threshold
    if len(rx) < 2:
        return None
    mx, my = mean(rx), mean(ry)
    sx, sy = stdev(rx), stdev(ry)
    if sx == 0 or sy == 0:
        return None
    r = sum((x - mx) * (y - my) for x, y in zip(rx, ry)) / ((n - 1) * sx * sy)
    r = max(-1.0, min(1.0, r))
    if abs(r) >= 1.0:
        return r, 0.0, n
    t = r * math.sqrt(n - 2) / math.sqrt(1 - r * r)
    p = 2 * (1 - NormalDist().cdf(abs(t)))
    return r, p, n


def _welch_t(a: list[float], b: list[float]):
    """Welch's t-test for unequal variance. Returns (t, p, df)."""
    if len(a) < 5 or len(b) < 5:
        return None
    ma, mb = mean(a), mean(b)
    va, vb = stdev(a)**2, stdev(b)**2
    if va == 0 and vb == 0:
        return None
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return None
    t = (ma - mb) / se
    # Welch–Satterthwaite df
    num = (va / len(a) + vb / len(b)) ** 2
    den = (va / len(a))**2 / (len(a)-1) + (vb / len(b))**2 / (len(b)-1)
    df = num / den if den > 0 else len(a) + len(b) - 2
    p = 2 * (1 - NormalDist().cdf(abs(t)))   # large-n approximation
    return t, p, df


def _quintile(rows, sig_col, ret_col, q=5):
    """Returns list of q buckets, each a list of forward returns."""
    valid = [(r[sig_col], r[ret_col]) for r in rows
             if r.get(sig_col) is not None and r.get(ret_col) is not None]
    if len(valid) < q * 5:
        return None
    valid.sort(key=lambda x: x[0])
    size = len(valid) // q
    return [[v[1] for v in valid[i*size:(i+1)*size if i < q-1 else len(valid)]] for i in range(q)]


def _stars(p):
    if p is None: return "    "
    if p < 0.001: return "*** "
    if p < 0.01:  return "**  "
    if p < 0.05:  return "*   "
    return "ns  "


# ── TESTS ────────────────────────────────────────────────────────────────────
#
# Each test returns a dict: {name, score (0..1), pass: bool, detail: str}
# Final verdict aggregates scores. Thresholds chosen from quant lit:
#   IC > 0.05 over many tokens is publishable alpha;
#   quintile-spread t-stat > 2 is the standard significance bar;
#   Sharpe > 1 net of cost is keep-the-license territory.
# ─────────────────────────────────────────────────────────────────────────────


def test_information_coefficient(panel) -> dict:
    """
    TEST 1 — Information Coefficient (Spearman rank correlation).
    The single most-used metric in quant finance for "does this signal predict?"

    Average IC across (signal × horizon × token) cells. Higher → better signal.
    Threshold:  |avg IC| > 0.05  → meaningful (PASS),  > 0.10 strong, < 0.03 noise.
    """
    print("\n┌─ TEST 1 ─ INFORMATION COEFFICIENT (Spearman rank IC) ────────────")
    print("│  Avg rank correlation between signal and forward return.")
    print("│  Reference:  >0.05 = real alpha,  >0.10 = strong,  <0.03 = noise.\n")

    print(f"  {'Token':<6} {'Signal':<18} " + " ".join(f"{h:>2}d-IC" for h in HORIZONS))

    ics_per_signal = defaultdict(list)
    sig_p_per_signal = defaultdict(list)
    rows_per_token = []

    for tok in sorted(TRADING_TOKENS):
        if tok not in panel: continue
        for sig in SIGNALS:
            cells = []
            for h in HORIZONS:
                pairs = _pairs(panel[tok], sig, f"fwd_{h}d")
                res = _spearman(pairs)
                if res:
                    rho, p, n = res
                    cells.append(f"{rho:+.3f}")
                    ics_per_signal[sig].append(rho)
                    sig_p_per_signal[sig].append(p)
                else:
                    cells.append("  n/a")
            print(f"  {tok:<6} {sig:<18} " + "  ".join(f"{c:>5}" for c in cells))

    print()
    print(f"  {'Signal':<18} {'avg|IC|':>8} {'avg IC':>8} {'%sig':>6}  Verdict")
    sig_scores = {}
    for sig in SIGNALS:
        ics = ics_per_signal[sig]
        ps  = sig_p_per_signal[sig]
        if not ics: continue
        avg_ic = mean(ics)
        avg_abs = mean(abs(x) for x in ics)
        pct_sig = sum(1 for p in ps if p < SIG_LEVEL) / len(ps) * 100
        verdict = ("STRONG" if avg_abs > 0.10 else
                   "REAL  " if avg_abs > 0.05 else
                   "WEAK  " if avg_abs > 0.03 else
                   "NOISE ")
        sig_scores[sig] = avg_abs
        print(f"  {sig:<18} {avg_abs:>7.3f} {avg_ic:>+7.3f}  {pct_sig:>4.0f}%  {verdict}")

    best_abs = max(sig_scores.values()) if sig_scores else 0
    overall_avg = mean(sig_scores.values()) if sig_scores else 0
    pass_ = best_abs > 0.05
    score = min(1.0, best_abs / 0.10)   # 0.10 IC = full score
    detail = f"best |IC|={best_abs:.3f}, avg |IC|={overall_avg:.3f}"
    return {"name": "Information Coefficient", "pass": pass_, "score": score, "detail": detail}


def test_pearson_with_correction(panel) -> dict:
    """
    TEST 2 — Pearson correlation with Bonferroni multiple-testing correction.
    Catches data-mining bias: when you test M signal/horizon/token combos,
    p < 0.05 happens ~5% of the time by chance. Adjust threshold to 0.05/M.
    """
    print("\n┌─ TEST 2 ─ PEARSON CORRELATION (Bonferroni-corrected) ───────────")
    print("│  M tests run; only correlations with p < 0.05/M survive.\n")

    n_tests = len(TRADING_TOKENS) * len(SIGNALS) * len(HORIZONS)
    threshold = SIG_LEVEL / n_tests
    print(f"  Tests run: {n_tests}    Bonferroni threshold: p < {threshold:.5f}")

    survivors = []
    all_results = []
    for tok in sorted(TRADING_TOKENS):
        if tok not in panel: continue
        for sig in SIGNALS:
            for h in HORIZONS:
                res = _pearson(_pairs(panel[tok], sig, f"fwd_{h}d"))
                if res:
                    r, p, n = res
                    all_results.append((tok, sig, h, r, p, n))
                    if p < threshold:
                        survivors.append((tok, sig, h, r, p, n))

    print(f"  Surviving correlations: {len(survivors)} / {len(all_results)}")
    print()
    if survivors:
        print(f"  {'Token':<6} {'Signal':<18} {'Horizon':>8} {'r':>7} {'p':>10}  {'n':>4}")
        for tok, sig, h, r, p, n in sorted(survivors, key=lambda x: -abs(x[3])):
            print(f"  {tok:<6} {sig:<18} {h:>6}d  {r:+.3f}  {p:.2e}  {n:>4}")
    else:
        print("  None — every signal/horizon combo dies under correction.")

    pct = len(survivors) / len(all_results) * 100 if all_results else 0
    score = min(1.0, len(survivors) / 5)   # 5 surviving correlations = full credit
    pass_ = len(survivors) >= 3
    return {"name": "Bonferroni-corrected Pearson", "pass": pass_, "score": score,
            "detail": f"{len(survivors)}/{len(all_results)} survive ({pct:.0f}%)"}


def test_quintile_spread(panel) -> dict:
    """
    TEST 3 — Quintile spread (Q5 - Q1) with Welch's t-test.
    Industry-standard alpha presentation: split by signal into 5 buckets,
    measure the forward-return gap top minus bottom, test significance.
    """
    print("\n┌─ TEST 3 ─ QUINTILE SPREAD — Q5 (high signal) − Q1 (low signal) ──")
    print("│  Larger spread + significant t-stat = real signal-driven separation.\n")

    print(f"  {'Token':<6} {'Signal':<18} {'Hzn':>4} {'Q1 ret':>8} {'Q5 ret':>8} "
          f"{'spread':>8} {'t':>6} {'p':>8}")

    spreads = []
    sig_t   = []
    for tok in sorted(TRADING_TOKENS):
        if tok not in panel: continue
        for sig in ["galaxy_score", "sentiment", "alt_rank_inv"]:   # primary 3
            for h in [3, 7]:                                         # most-watched horizons
                buckets = _quintile(panel[tok], sig, f"fwd_{h}d", q=5)
                if not buckets: continue
                q1, q5 = buckets[0], buckets[-1]
                spread = mean(q5) - mean(q1)
                tres = _welch_t(q5, q1)
                if not tres:
                    continue
                t, p, df = tres
                spreads.append(spread)
                sig_t.append(p < SIG_LEVEL)
                stars = _stars(p)
                print(f"  {tok:<6} {sig:<18} {h:>3}d  "
                      f"{mean(q1):+7.2f}%  {mean(q5):+7.2f}%  "
                      f"{spread:+7.2f}pp  {t:+5.2f}  {p:.4f} {stars}")

    if not spreads:
        return {"name": "Quintile spread", "pass": False, "score": 0,
                "detail": "no data"}
    avg_spread = mean(spreads)
    pct_sig = sum(sig_t) / len(sig_t) * 100
    print(f"\n  Avg Q5−Q1 spread: {avg_spread:+.2f}pp    "
          f"% of pairs with p<.05: {pct_sig:.0f}%")

    pass_ = avg_spread > 1.0 and pct_sig > 30
    score = min(1.0, max(0, avg_spread) / 3.0) * (pct_sig / 100)
    return {"name": "Quintile spread", "pass": pass_, "score": score,
            "detail": f"avg spread {avg_spread:+.2f}pp, {pct_sig:.0f}% significant"}


def test_long_short_strategy(panel) -> dict:
    """
    TEST 4 — Quintile-based trading simulation.
    Each day, hold every (token, day) row whose galaxy_score is in the top
    quintile *for that token's full history*. Compute equal-weighted daily
    returns from price_close. Sharpe + total return are the bottom line.
    """
    print("\n┌─ TEST 4 ─ TRADING SIMULATION — top-quintile galaxy_score (1d hold) ─")
    print("│  Hold the token only on days when galaxy_score is in its top 20%.\n")

    # Compute daily returns for each token from price_close
    strat_returns = []   # daily mean return across tokens that triggered
    bnh_returns   = []   # equal-weight buy-and-hold daily return
    days = sorted({r["date"] for tok in TRADING_TOKENS if tok in panel for r in panel[tok]})

    # Per-token galaxy_score thresholds (top 20%)
    thresholds = {}
    for tok in TRADING_TOKENS:
        if tok not in panel: continue
        gs = sorted(r["galaxy_score"] for r in panel[tok] if r["galaxy_score"] is not None)
        if not gs: continue
        thresholds[tok] = gs[int(len(gs) * 0.80)]

    # Build per-token daily return series
    daily_ret = defaultdict(dict)   # daily_ret[token][date] = pct change
    daily_gs  = defaultdict(dict)
    for tok in TRADING_TOKENS:
        if tok not in panel: continue
        ser = panel[tok]
        for i in range(1, len(ser)):
            p0, p1 = ser[i-1]["price_close"], ser[i]["price_close"]
            if p0 and p1:
                ret = (p1 - p0) / p0 * 100
                # Clip to filter listing/illiquidity artifacts
                ret = max(-MAX_DAILY_RET_PCT, min(MAX_DAILY_RET_PCT, ret))
                daily_ret[tok][ser[i]["date"]] = ret
            daily_gs[tok][ser[i-1]["date"]] = ser[i-1]["galaxy_score"]   # signal at t, return at t+1

    for d in days:
        # Buy-and-hold: equal weight across tokens for which we have a return
        rets_bnh = [daily_ret[tok][d] for tok in TRADING_TOKENS
                    if d in daily_ret.get(tok, {})]
        if rets_bnh:
            bnh_returns.append(mean(rets_bnh))

        # Strategy: only tokens whose prior-day galaxy_score is in top 20%
        triggered = [daily_ret[tok][d] for tok in TRADING_TOKENS
                     if d in daily_ret.get(tok, {})
                     and daily_gs.get(tok, {}).get(d - timedelta(days=1)) is not None
                     and daily_gs[tok][d - timedelta(days=1)] >= thresholds.get(tok, 999)]
        if triggered:
            strat_returns.append(mean(triggered))
        else:
            strat_returns.append(0.0)   # cash on flat days

    def _ann_sharpe(rets):
        if len(rets) < 30 or stdev(rets) == 0: return 0.0
        return mean(rets) / stdev(rets) * math.sqrt(365)

    def _total(rets):
        eq = 1.0
        for r in rets:
            eq *= (1 + r / 100)
        return (eq - 1) * 100

    s_total = _total(strat_returns)
    b_total = _total(bnh_returns)
    s_sharpe = _ann_sharpe(strat_returns)
    b_sharpe = _ann_sharpe(bnh_returns)
    s_days   = sum(1 for r in strat_returns if r != 0)
    win_rate_s = sum(1 for r in strat_returns if r > 0) / max(s_days, 1) * 100

    print(f"  Period:                 {days[0]} → {days[-1]}  ({len(days)} days)")
    print(f"  Triggered days (in mkt):{s_days} / {len(strat_returns)}  ({s_days/len(strat_returns)*100:.0f}%)")
    print(f"  Strategy total return:  {s_total:+.1f}%")
    print(f"  Buy-and-hold return:    {b_total:+.1f}%")
    print(f"  Strategy ann. Sharpe:   {s_sharpe:.2f}")
    print(f"  Buy-and-hold Sharpe:    {b_sharpe:.2f}")
    print(f"  Strategy win-rate (in-market): {win_rate_s:.1f}%")
    print(f"  Edge over BNH (return): {s_total - b_total:+.1f}pp   "
          f"(Sharpe diff: {s_sharpe - b_sharpe:+.2f})")

    pass_  = s_sharpe > b_sharpe and (s_total - b_total) > 5
    score  = max(0, min(1.0, (s_sharpe - b_sharpe) / 0.5))
    return {"name": "Quintile strategy vs BNH", "pass": pass_, "score": score,
            "detail": f"Δreturn {s_total-b_total:+.1f}pp, ΔSharpe {s_sharpe-b_sharpe:+.2f}",
            "edge_pct_per_year": (s_total - b_total) / (len(days) / 365)}


def test_cross_sectional_rank(panel) -> dict:
    """
    TEST 5 — Cross-sectional ranking IC.
    On every date, rank the 5 trading tokens by signal. Does the ranking
    predict the cross-section of next-day returns? This is how multi-asset
    quant strategies are actually evaluated.
    """
    print("\n┌─ TEST 5 ─ CROSS-SECTIONAL RANK IC ──────────────────────────────")
    print("│  Each day, rank tokens by signal — does the ranking predict")
    print("│  which token outperforms? Mean daily IC > 0.05 = real alpha.\n")

    # Build {date: {token: (signal, fwd_1d)}} for each signal
    print(f"  {'Signal':<18}  {'Days':>5}  {'Mean IC':>8}  {'%>0':>5}  {'t-stat':>7}  Verdict")
    for sig in SIGNALS:
        by_date: dict[date, list[tuple[str, float, float]]] = defaultdict(list)
        for tok in TRADING_TOKENS:
            if tok not in panel: continue
            for r in panel[tok]:
                s, ret = r.get(sig), r.get("fwd_1d")
                if s is not None and ret is not None:
                    by_date[r["date"]].append((tok, s, ret))

        ics = []
        for d, items in by_date.items():
            if len(items) < 4: continue
            res = _spearman([(it[1], it[2]) for it in items], min_n=4)
            if res:
                ics.append(res[0])
        if not ics:
            continue
        m = mean(ics)
        sd = stdev(ics) if len(ics) > 1 else 0
        pct_pos = sum(1 for x in ics if x > 0) / len(ics) * 100
        t = m / (sd / math.sqrt(len(ics))) if sd > 0 else 0
        verdict = "STRONG" if abs(m) > 0.10 and abs(t) > 2 else \
                  "REAL  " if abs(m) > 0.05 and abs(t) > 2 else \
                  "WEAK  " if abs(m) > 0.03 else "NOISE "
        print(f"  {sig:<18}  {len(ics):>5}  {m:>+7.3f}  {pct_pos:>4.0f}%  {t:>+6.2f}  {verdict}")

    # Use galaxy_score for verdict (most-watched signal)
    by_date = defaultdict(list)
    for tok in TRADING_TOKENS:
        if tok not in panel: continue
        for r in panel[tok]:
            s, ret = r.get("galaxy_score"), r.get("fwd_1d")
            if s is not None and ret is not None:
                by_date[r["date"]].append((s, ret))
    ics = []
    for d, items in by_date.items():
        if len(items) < 4: continue
        res = _spearman(items, min_n=4)
        if res:
            ics.append(res[0])
    if not ics:
        return {"name": "Cross-sectional rank IC", "pass": False, "score": 0,
                "detail": "no data"}
    m = mean(ics)
    sd = stdev(ics)
    t = m / (sd / math.sqrt(len(ics))) if sd > 0 else 0
    pass_ = abs(m) > 0.05 and abs(t) > 2
    score = min(1.0, abs(m) / 0.10)
    return {"name": "Cross-sectional rank IC", "pass": pass_, "score": score,
            "detail": f"galaxy_score: mean IC={m:+.3f}, t={t:+.2f}, n={len(ics)}d"}


def test_regime_split(panel) -> dict:
    """
    TEST 6 — Regime conditioning.
    Classify each day as BTC bull (close > 30d MA) or bear. Re-run the
    quintile spread test in each regime. Many social signals fail in bear.
    """
    print("\n┌─ TEST 6 ─ REGIME SPLIT (BTC bull vs bear) ──────────────────────")
    print("│  Does the signal work only in bull markets? That matters for")
    print("│  whether the alpha will survive 2026 if we re-enter bear.\n")

    if "BTC" not in panel:
        print("  No BTC data — skipping.")
        return {"name": "Regime split", "pass": False, "score": 0, "detail": "no BTC"}

    btc = panel["BTC"]
    regimes = {}
    closes = [r["price_close"] for r in btc]
    for i, r in enumerate(btc):
        lookback = closes[max(0, i-30):i+1]
        valid = [x for x in lookback if x]
        if not valid or not r["price_close"]:
            continue
        regimes[r["date"]] = "bull" if r["price_close"] >= mean(valid) else "bear"

    bull_days = sum(1 for v in regimes.values() if v == "bull")
    bear_days = sum(1 for v in regimes.values() if v == "bear")
    print(f"  Regime split: bull={bull_days}d  bear={bear_days}d")
    print()
    print(f"  {'Token':<6} {'Regime':<6} {'Q1':>8} {'Q5':>8} {'Spread':>8} {'p':>8}")

    spreads_bull, spreads_bear = [], []
    for tok in sorted(TRADING_TOKENS):
        if tok not in panel: continue
        for label in ["bull", "bear"]:
            sub = [r for r in panel[tok] if regimes.get(r["date"]) == label]
            if len(sub) < 50: continue
            buckets = _quintile(sub, "galaxy_score", "fwd_3d", q=5)
            if not buckets: continue
            q1, q5 = buckets[0], buckets[-1]
            sp = mean(q5) - mean(q1)
            tres = _welch_t(q5, q1)
            p = tres[1] if tres else 1.0
            (spreads_bull if label == "bull" else spreads_bear).append(sp)
            print(f"  {tok:<6} {label:<6} {mean(q1):+7.2f}% {mean(q5):+7.2f}% "
                  f"{sp:+7.2f}pp  {p:.4f} {_stars(p)}")

    avg_bull = mean(spreads_bull) if spreads_bull else 0
    avg_bear = mean(spreads_bear) if spreads_bear else 0
    print(f"\n  Avg spread (bull): {avg_bull:+.2f}pp")
    print(f"  Avg spread (bear): {avg_bear:+.2f}pp")
    if avg_bear > 0.5 and avg_bull > 0.5:
        regime_verdict = "Works in BOTH regimes — robust"
    elif avg_bull > 0.5:
        regime_verdict = "Works only in BULL — fair-weather signal"
    elif avg_bear > 0.5:
        regime_verdict = "Works only in BEAR — surprising, validate"
    else:
        regime_verdict = "Works in NEITHER — fundamentally weak"
    print(f"  Verdict: {regime_verdict}")

    pass_ = avg_bull > 0.5 and avg_bear > 0
    score = min(1.0, max(0, (avg_bull + avg_bear) / 2) / 2.0)
    return {"name": "Regime split", "pass": pass_, "score": score,
            "detail": f"bull {avg_bull:+.2f}pp, bear {avg_bear:+.2f}pp"}


# ── Verdict ───────────────────────────────────────────────────────────────────

def render_verdict(results: list[dict], strategy_test: dict, portfolio: float, trades: int):
    print("\n" + "═" * 70)
    print("  FINAL VERDICT — Is LunarCrush ($150/mo = $1,800/yr) worth it?")
    print("═" * 70)

    print(f"\n  {'#':<2} {'Test':<32} {'Score':>6}  {'Pass':>5}  Detail")
    print(f"  {'─'*2} {'─'*32} {'─'*6}  {'─'*5}  {'─'*30}")
    for i, t in enumerate(results, 1):
        ok = "✓" if t["pass"] else "✗"
        print(f"  {i:<2} {t['name']:<32} {t['score']:>5.2f}  {ok:>5}  {t['detail']}")

    n_pass = sum(1 for t in results if t["pass"])
    avg_score = mean(t["score"] for t in results)

    print(f"\n  Tests passed: {n_pass}/{len(results)}    Avg score: {avg_score:.2f}")

    # ── ROI math ──
    edge_pct_yr = strategy_test.get("edge_pct_per_year", 0)
    edge_dollars = edge_pct_yr / 100 * portfolio
    print()
    print(f"  ── COST/BENEFIT ON A ${portfolio:,.0f} PORTFOLIO ──")
    print(f"  Strategy edge over BNH (test 4):   {edge_pct_yr:+.1f}% / year")
    print(f"  Implied dollar edge:               ${edge_dollars:+,.0f} / year")
    print(f"  Cost of LunarCrush:                ${COST_PER_YEAR:,.0f} / year")
    breakeven_portfolio = COST_PER_YEAR / (edge_pct_yr / 100) if edge_pct_yr > 0 else float('inf')
    print(f"  Break-even portfolio size:         "
          + (f"${breakeven_portfolio:,.0f}" if math.isfinite(breakeven_portfolio) else "NEVER (negative edge)"))
    print(f"  Net annual P&L from this signal:   ${edge_dollars - COST_PER_YEAR:+,.0f}")
    print()

    # ── Decision ──
    if n_pass >= 4 and avg_score >= 0.5 and edge_dollars > COST_PER_YEAR:
        verdict = "🟢 KEEP — signal is real and pays for itself"
    elif n_pass >= 3 and avg_score >= 0.35:
        verdict = "🟡 MARGINAL — real but weak; only worth it on a larger portfolio"
    else:
        verdict = "🔴 KILL — signal too weak to justify $150/mo. Cancel and use the budget elsewhere."

    print(f"  ▶  {verdict}")
    print()
    if n_pass < 4 or edge_dollars <= COST_PER_YEAR:
        print("  Reasoning: cancelling LunarCrush saves $1,800/yr that more than")
        print("  offsets the marginal alpha. Re-evaluate once your portfolio")
        print(f"  exceeds ${breakeven_portfolio:,.0f}." if math.isfinite(breakeven_portfolio)
              else "  No portfolio size makes it pay — the signal does not work as priced.")
    print("═" * 70)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main_async(args):
    if args.csv:
        rows = _load_csv(args.csv)
        print(f"Loaded {len(rows)} rows from {args.csv}")
    else:
        db = args.db or os.environ.get("DATABASE_URL")
        if not db:
            print("ERROR: pass --db, --csv, or set DATABASE_URL")
            sys.exit(1)
        rows = await _load_db(db)
        print(f"Loaded {len(rows)} rows from database")
        if args.export:
            _export_csv(rows, args.export)
            print(f"Exported to {args.export}")

    panel = _build_panel(rows)
    print(f"Tokens: {sorted(panel.keys())}")

    # Allow CLI override of the trading-universe under test
    global TRADING_TOKENS
    if args.universe:
        if args.universe == "all":
            TRADING_TOKENS = {t for t in panel if t not in ("BTC", "ETH")}
        else:
            TRADING_TOKENS = {t.strip().upper() for t in args.universe.split(",")}
    print(f"Trading universe under test: {sorted(TRADING_TOKENS)}")
    print(f"Date range: {min(r['date'] for r in rows)} → {max(r['date'] for r in rows)}")

    results = [
        test_information_coefficient(panel),
        test_pearson_with_correction(panel),
        test_quintile_spread(panel),
    ]
    strategy = test_long_short_strategy(panel)
    results.append(strategy)
    results.append(test_cross_sectional_rank(panel))
    results.append(test_regime_split(panel))

    render_verdict(results, strategy, args.portfolio, args.trades_per_year)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db",     help="postgres URL (default: $DATABASE_URL)")
    p.add_argument("--csv",    help="load from CSV instead of DB")
    p.add_argument("--export", help="when loading from DB, dump to this CSV path")
    p.add_argument("--universe", default=None,
                   help="comma-separated tokens to evaluate (default: live trading set). "
                        "Pass 'all' to test every token in the data (excl. BTC/ETH).")
    p.add_argument("--portfolio",       type=float, default=10000,
                   help="portfolio size in $ (default 10000) for ROI calc")
    p.add_argument("--trades-per-year", type=int,   default=50,
                   help="expected trades/yr (informational)")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
