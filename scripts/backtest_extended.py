"""
scripts/backtest_extended.py

Devil's advocate analysis: can we find evidence FOR LunarCrush social signals?

Extends backtest_social_signal.py with:
  1. 730-day history (vs 365) — more data, less regime bias
  2. Z-score normalized signals (rolling 60d) — relative change vs absolute level
  3. Extreme quantile analysis (top/bottom 5% and 10%) — maybe only spikes matter
  4. Rolling 90-day Pearson r — is the signal stable or a one-period artefact?
  5. Out-of-sample split — first 50% train, last 50% test — catches overfitting
  6. Explicit contrarian strategy for BTC/ETH/SOL — is the negative r actionable?
  7. Composite multi-metric signal — maybe no single metric is enough alone

Output saved to: output/backtest_extended_<date>.txt

Usage:
    python scripts/backtest_extended.py [--days 730] [--tokens large_cap|all|BTC,ETH,...]

Requires: LUNARCRUSH_API_KEY environment variable
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from statistics import mean, median, stdev, NormalDist

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

LUNARCRUSH_KEY  = os.environ.get("LUNARCRUSH_API_KEY", "")
LUNARCRUSH_BASE = "https://lunarcrush.com/api4/public"

LC_SYMBOLS = [
    "BTC", "ETH", "SOL", "AVAX", "DOT", "LINK", "ARB", "OP", "INJ", "SUI",
    "APT", "NEAR", "FTM", "ATOM", "ALGO", "AAVE", "UNI", "SNX", "CRV", "LDO",
    "DYDX", "GMX", "PENDLE", "JUP", "PYTH", "WIF", "BONK",
]
LARGE_CAP = ["BTC", "ETH", "SOL", "AVAX", "LINK", "DOT", "ATOM", "NEAR", "AAVE", "UNI"]

# Tokens where previous run found negative social_dominance correlation
# → explicitly test if contrarian strategy is actionable
CONTRARIAN_TOKENS = {"BTC", "ETH", "SOL"}

_LC_SLEEP      = 6.5
HORIZONS       = [1, 2, 5, 10, 14]
ROLLING_WIN    = 90   # days per rolling Pearson window (50% overlap)
ZSCORE_WIN     = 60   # trailing days for z-score normalisation
EXTREME_PCTS   = [5, 10]

METRICS = [
    "galaxy_score", "sentiment", "alt_rank_inv", "interactions",
    "social_dominance", "contributors_active", "posts_created",
]

# ── Tee output to file ────────────────────────────────────────────────────────

class _Tee:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            s.write(data)
    def flush(self):
        for s in self._streams:
            s.flush()


# ── Fetching ──────────────────────────────────────────────────────────────────

def _fetch_lunarcrush(symbol: str, days: int) -> list[dict]:
    """Fetch hourly LunarCrush time-series and aggregate to daily rows."""
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
            print(f"    [LC] {symbol} connect error attempt {attempt+1}/4 — retry in {wait}s: {exc}")
            time.sleep(wait)
    else:
        print(f"    [LC] {symbol}: all retries exhausted")
        return []

    if resp.status_code == 429:
        print(f"    [LC] {symbol}: rate limited — waiting 30s")
        time.sleep(30)
        return _fetch_lunarcrush(symbol, days)
    if resp.status_code != 200:
        print(f"    [LC] {symbol} HTTP {resp.status_code}: {resp.text[:200]}")
        return []

    hourly = resp.json().get("data") or []
    if not hourly:
        print(f"    [LC] {symbol}: empty data")
        return []

    by_date: dict = {}
    for r in hourly:
        ts = r.get("time")
        if not ts:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        by_date.setdefault(d, []).append(r)

    result = []
    for d, hours in sorted(by_date.items()):
        def _avg(key):
            vals = [h[key] for h in hours if h.get(key) is not None]
            return mean(vals) if vals else None

        ar = _avg("alt_rank")
        result.append({
            "date":                d,
            "price":               hours[-1].get("close"),
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

def _add_zscore(rows: list[dict], window: int = 60) -> list[dict]:
    """Add z_<metric> for each metric using a trailing rolling window (no look-ahead)."""
    by_date = {r["date"]: r for r in rows}
    dates   = sorted(by_date.keys())

    for metric in METRICS:
        vals_by_date = [(d, by_date[d].get(metric)) for d in dates]
        for i, (d, v) in enumerate(vals_by_date):
            window_vals = [
                vals_by_date[j][1]
                for j in range(max(0, i - window), i)
                if vals_by_date[j][1] is not None
            ]
            if len(window_vals) < 10 or v is None:
                by_date[d][f"z_{metric}"] = None
            else:
                m = mean(window_vals)
                s = stdev(window_vals) if len(window_vals) > 1 else 0
                by_date[d][f"z_{metric}"] = (v - m) / s if s > 0 else 0.0
    return rows


def _add_momentum(rows: list[dict], window: int = 3) -> list[dict]:
    """Add delta_<metric> = metric(T) - metric(T-window)."""
    by_date = {r["date"]: r for r in rows}
    dates   = sorted(by_date.keys())
    for i, d in enumerate(dates):
        prev = dates[i - window] if i >= window else None
        for m in ["galaxy_score", "sentiment", "social_dominance", "interactions"]:
            if prev and by_date[prev].get(m) is not None and by_date[d].get(m) is not None:
                by_date[d][f"delta_{m}"] = by_date[d][m] - by_date[prev][m]
            else:
                by_date[d][f"delta_{m}"] = None
    return rows


def _build_rows(social: list[dict], horizons: list[int]) -> list[dict]:
    """Merge forward-return windows into social rows; drop incomplete rows."""
    by_date  = {r["date"]: r for r in social}
    dates    = sorted(by_date.keys())

    rows = []
    for d in dates:
        row = dict(by_date[d])
        p0  = row.get("price")
        if not p0 or p0 <= 0:
            continue
        valid = True
        for h in horizons:
            target = d + timedelta(days=h)
            ph = None
            for off in range(3):
                c = target + timedelta(days=off)
                if c in by_date:
                    ph = by_date[c].get("price")
                    break
            if not ph or ph <= 0:
                valid = False
                break
            row[f"fwd_{h}d"] = (ph - p0) / p0 * 100
        if valid:
            rows.append(row)
    return rows


# ── Statistical helpers ───────────────────────────────────────────────────────

def _pearson(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 20:
        return None
    mx, my = mean(xs), mean(ys)
    num  = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dxs  = math.sqrt(sum((x - mx)**2 for x in xs))
    dys  = math.sqrt(sum((y - my)**2 for y in ys))
    if dxs == 0 or dys == 0:
        return None
    r = max(-1.0, min(1.0, num / (dxs * dys)))
    try:
        t = r * math.sqrt(n - 2) / math.sqrt(max(1e-15, 1 - r * r))
        p = 2 * (1 - NormalDist().cdf(abs(t)))
    except Exception:
        p = 1.0
    return r, p


def _stars(p: float) -> str:
    if p < 0.001: return "***"
    if p < 0.01:  return "** "
    if p < 0.05:  return "*  "
    return "   "


def _win_rate(rets: list[float]) -> float:
    return sum(1 for r in rets if r > 0) / len(rets) * 100 if rets else 0.0


# ── Analysis modules ──────────────────────────────────────────────────────────

def _rolling_stability(rows: list[dict], signal: str, horizon: int) -> dict:
    """
    Slide a ROLLING_WIN-day window (50% overlap) across all rows.
    Reports how stable the Pearson r is across time — high std_r = regime-dependent.
    """
    fwd = f"fwd_{horizon}d"
    pairs = [(r[signal], r[fwd]) for r in rows if r.get(signal) is not None and r.get(fwd) is not None]
    if len(pairs) < ROLLING_WIN * 2:
        return {}

    rs = []
    step = max(1, ROLLING_WIN // 2)
    for start in range(0, len(pairs) - ROLLING_WIN, step):
        chunk = pairs[start:start + ROLLING_WIN]
        res   = _pearson([v[0] for v in chunk], [v[1] for v in chunk])
        if res:
            rs.append(res[0])

    if not rs:
        return {}
    return {
        "mean_r":      mean(rs),
        "std_r":       stdev(rs) if len(rs) > 1 else 0.0,
        "pct_pos":     sum(1 for r in rs if r > 0) / len(rs),
        "min_r":       min(rs),
        "max_r":       max(rs),
        "n_windows":   len(rs),
    }


def _extreme_quantiles(rows: list[dict], signal: str, horizon: int) -> dict:
    """
    Top-pct% vs bottom-pct% signal values.
    Tests whether very high social readings have edge that median readings don't.
    """
    fwd = f"fwd_{horizon}d"
    pairs = sorted(
        [(r[signal], r[fwd]) for r in rows if r.get(signal) is not None and r.get(fwd) is not None],
        key=lambda x: x[0]
    )
    if len(pairs) < 40:
        return {}

    out = {}
    for pct in EXTREME_PCTS:
        k   = max(1, int(len(pairs) * pct / 100))
        bot = pairs[:k]
        top = pairs[-k:]
        mid = pairs[k:-k]

        def _s(bucket):
            rets = [v[1] for v in bucket]
            return {"n": len(rets), "win": _win_rate(rets), "avg": mean(rets) if rets else 0.0}

        ts = _s(top)
        bs = _s(bot)
        out[pct] = {
            "top": ts, "bottom": bs, "middle": _s(mid),
            "lift": ts["avg"] - bs["avg"],
        }
    return out


def _oos_split(rows: list[dict], signal: str, horizon: int) -> dict:
    """
    Chronological 50/50 train-test split.
    If test correlation is similar sign/magnitude to train → signal is real.
    If test collapses → the train correlation was data-mined noise.
    """
    fwd = f"fwd_{horizon}d"
    pairs = sorted(
        [(r["date"], r[signal], r[fwd]) for r in rows if r.get(signal) is not None and r.get(fwd) is not None],
        key=lambda x: x[0]
    )
    if len(pairs) < 60:
        return {}

    mid    = len(pairs) // 2
    train  = pairs[:mid]
    test   = pairs[mid:]

    def _c(chunk):
        xs = [v[1] for v in chunk]
        ys = [v[2] for v in chunk]
        return _pearson(xs, ys) or (0.0, 1.0)

    tr, tp = _c(train)
    te, tep = _c(test)
    return {
        "train_r": tr, "train_p": tp, "train_n": len(train),
        "test_r":  te, "test_p":  tep, "test_n":  len(test),
        "consistent":  (tr > 0) == (te > 0),
        "degradation": te - tr,
    }


def _contrarian_test(rows: list[dict], signal: str, horizon: int) -> dict:
    """
    For tokens with negative correlation (BTC/SOL/ETH):
    explicitly test the rule "buy when social is LOW, avoid when social is HIGH".
    Reports win rate and avg return for each half.
    """
    fwd = f"fwd_{horizon}d"
    pairs = [(r[signal], r[fwd]) for r in rows if r.get(signal) is not None and r.get(fwd) is not None]
    if len(pairs) < 40:
        return {}

    med  = median([v[0] for v in pairs])
    low  = [v[1] for v in pairs if v[0] <= med]
    high = [v[1] for v in pairs if v[0] > med]

    def _s(rets):
        return {"n": len(rets), "win": _win_rate(rets), "avg": mean(rets) if rets else 0.0}

    ls = _s(low)
    hs = _s(high)
    lift = ls["avg"] - hs["avg"]
    return {
        "low":  ls, "high": hs,
        "contrarian_lift": lift,
        "actionable": lift > 0.5,
    }


def _composite(rows: list[dict], horizon: int, contrarian_mode: bool) -> dict:
    """
    Equal-weight composite of z-scored metrics.
    Contrarian mode: invert social activity signals (high social → predict down).
    Bullish mode:    use social_dominance + sentiment as positive signals.
    """
    fwd = f"fwd_{horizon}d"
    if contrarian_mode:
        spec = [("z_social_dominance", -1), ("z_interactions", -1), ("z_sentiment", -1)]
    else:
        spec = [("z_social_dominance", +1), ("z_sentiment", +1), ("z_galaxy_score", +1)]

    pairs = []
    for r in rows:
        fy = r.get(fwd)
        if fy is None:
            continue
        zs = [sign * r[col] for col, sign in spec if r.get(col) is not None]
        if len(zs) == len(spec):
            pairs.append((mean(zs), fy))

    if len(pairs) < 40:
        return {}

    res = _pearson([v[0] for v in pairs], [v[1] for v in pairs])
    if not res:
        return {}
    r, p = res

    pairs.sort(key=lambda x: x[0])
    k       = max(1, len(pairs) // 4)
    q4_rets = [v[1] for v in pairs[-k:]]
    q1_rets = [v[1] for v in pairs[:k]]
    lift    = mean(q4_rets) - mean(q1_rets) if q4_rets and q1_rets else 0.0

    return {
        "r": r, "p": p, "n": len(pairs),
        "q4_win": _win_rate(q4_rets),
        "q1_win": _win_rate(q1_rets),
        "lift":   lift,
        "mode":   "contrarian" if contrarian_mode else "bullish",
    }


# ── Per-token report ──────────────────────────────────────────────────────────

def _analyse_token(symbol: str, rows: list[dict]) -> dict:
    contra_mode = symbol in CONTRARIAN_TOKENS

    print(f"\n{'═'*62}")
    print(f"  {symbol}  ({len(rows)} analysis rows)  "
          f"({'CONTRARIAN mode' if contra_mode else 'bullish mode'})")
    print(f"{'═'*62}")

    results = {"symbol": symbol, "n": len(rows), "rolling": {}, "extreme": {},
               "oos": {}, "contrarian": {}, "composite": {}}

    # ── 1. ROLLING STABILITY ──────────────────────────────────────────────────
    print(f"\n  [1] Rolling {ROLLING_WIN}d Pearson — social_dominance")
    print(f"  {'Hz':>4}  {'MeanR':>7}  {'StdR':>6}  {'%Pos':>5}  {'Range':>13}  Stability")
    print(f"  {'─'*58}")
    for h in [5, 10, 14]:
        rs = _rolling_stability(rows, "social_dominance", h)
        if not rs:
            continue
        results["rolling"][h] = rs
        mr, sr, ppos = rs["mean_r"], rs["std_r"], rs["pct_pos"]
        rng = f"[{rs['min_r']:+.2f},{rs['max_r']:+.2f}]"

        if abs(mr) > 0.15 and (ppos > 0.65 or ppos < 0.35) and sr < 0.15:
            label = "STABLE SIGNAL"
        elif abs(mr) > 0.10 and sr < 0.20:
            label = "weak consistent"
        elif sr > 0.20:
            label = "REGIME DEPENDENT"
        else:
            label = "noise"

        print(f"  {h:>3}d  {mr:>+7.3f}  {sr:>6.3f}  {ppos:>4.0%}  {rng:>13}  {label}")

    # ── 2. EXTREME QUANTILES ──────────────────────────────────────────────────
    print(f"\n  [2] Extreme quantile — social_dominance → 5d return")
    print(f"  {'Pct':>4}  {'Top_n':>5}  {'Top_win':>7}  {'Top_avg':>7}  "
          f"{'Bot_win':>7}  {'Bot_avg':>7}  {'Lift':>7}")
    print(f"  {'─'*58}")
    eq = _extreme_quantiles(rows, "social_dominance", 5)
    if eq:
        results["extreme"] = eq
        for pct, d in eq.items():
            top, bot = d["top"], d["bottom"]
            print(f"  {pct:>3}%  {top['n']:>5}  {top['win']:>6.1f}%  {top['avg']:>+6.2f}%  "
                  f"  {bot['win']:>6.1f}%  {bot['avg']:>+6.2f}%  {d['lift']:>+6.2f}pp")

    # ── 3. OUT-OF-SAMPLE SPLIT ────────────────────────────────────────────────
    print(f"\n  [3] Out-of-sample split (50/50 chronological) — social_dominance")
    print(f"  {'Hz':>4}  {'Train_r':>8}  {'Test_r':>8}  {'Degrad':>8}  {'Consistent':>11}  Verdict")
    print(f"  {'─'*65}")
    for h in [2, 5, 10, 14]:
        oos = _oos_split(rows, "social_dominance", h)
        if not oos:
            continue
        results["oos"][h] = oos
        tr, te = oos["train_r"], oos["test_r"]
        deg    = oos["degradation"]
        cons   = "YES" if oos["consistent"] else "NO "

        if oos["consistent"] and abs(te) > 0.12:
            label = "REAL SIGNAL"
        elif oos["consistent"] and abs(te) > 0.06:
            label = "weak consistent"
        elif not oos["consistent"]:
            label = "noise / overfit"
        else:
            label = "noise"

        print(f"  {h:>3}d  {tr:>+8.3f}  {te:>+8.3f}  {deg:>+8.3f}  {cons:>11}  {label}")

    # ── 4. CONTRARIAN (BTC/ETH/SOL only) ─────────────────────────────────────
    if contra_mode:
        print(f"\n  [4] Contrarian test — buy when social_dominance is LOW")
        print(f"  {'Hz':>4}  {'Low_win':>7}  {'High_win':>8}  {'Low_avg':>7}  "
              f"{'High_avg':>8}  {'Lift':>7}  Actionable")
        print(f"  {'─'*65}")
        for h in [5, 10, 14]:
            ct = _contrarian_test(rows, "social_dominance", h)
            if not ct:
                continue
            results["contrarian"][h] = ct
            ls, hs = ct["low"], ct["high"]
            lift   = ct["contrarian_lift"]
            act    = "YES" if ct["actionable"] else "no"
            print(f"  {h:>3}d  {ls['win']:>6.1f}%  {hs['win']:>7.1f}%  {ls['avg']:>+6.2f}%  "
                  f"  {hs['avg']:>+7.2f}%  {lift:>+6.2f}pp  {act}")

    # ── 5. COMPOSITE SIGNAL ───────────────────────────────────────────────────
    mode_str = "contrarian (social_dominance⁻¹ + interactions⁻¹ + sentiment⁻¹)" \
               if contra_mode else "bullish (social_dominance + sentiment + galaxy_score)"
    print(f"\n  [5] Composite z-score signal — {mode_str}")
    print(f"  {'Hz':>4}  {'r':>7}  {'sig':>4}  {'Q4_win':>7}  {'Q1_win':>7}  {'Lift':>7}")
    print(f"  {'─'*50}")
    for h in HORIZONS:
        comp = _composite(rows, h, contra_mode)
        if not comp:
            continue
        results["composite"][h] = comp
        r, p = comp["r"], comp["p"]
        print(f"  {h:>3}d  {r:>+7.3f}  {_stars(p)}  {comp['q4_win']:>6.1f}%  "
              f"{comp['q1_win']:>6.1f}%  {comp['lift']:>+6.2f}pp")

    return results


# ── Aggregate verdict ─────────────────────────────────────────────────────────

def _verdict(all_results: list[dict]) -> None:
    print(f"\n\n{'═'*62}")
    print(f"  DEVIL'S ADVOCATE VERDICT  ({len(all_results)} tokens, 730d history)")
    print(f"{'═'*62}")

    n = len(all_results)

    # OOS consistency
    oos_real   = sum(1 for r in all_results
                     if any(v.get("consistent") and abs(v.get("test_r", 0)) > 0.12
                            for v in r["oos"].values()))
    oos_weak   = sum(1 for r in all_results
                     if any(v.get("consistent") and abs(v.get("test_r", 0)) > 0.06
                            for v in r["oos"].values()))

    # Stable rolling signal
    stable     = sum(1 for r in all_results
                     if any(abs(v.get("mean_r", 0)) > 0.12
                            and (v.get("pct_pos", 0.5) > 0.65 or v.get("pct_pos", 0.5) < 0.35)
                            and v.get("std_r", 99) < 0.18
                            for v in r["rolling"].values()))

    # Extreme quantile edge
    extreme_edge = sum(1 for r in all_results
                       if any(abs(d.get("lift", 0)) > 1.0
                              for pct_data in [r.get("extreme", {})]
                              for d in pct_data.values()
                              if isinstance(d, dict) and "lift" in d))

    # Contrarian actionable
    contra_n   = sum(1 for r in all_results if r["symbol"] in CONTRARIAN_TOKENS)
    contra_ok  = sum(1 for r in all_results
                     if r["symbol"] in CONTRARIAN_TOKENS
                     and any(v.get("actionable") for v in r["contrarian"].values()))

    # Composite lifts
    comp_lifts = [
        v["lift"]
        for r in all_results
        for v in r["composite"].values()
        if isinstance(v, dict) and "lift" in v
    ]
    avg_comp_lift = mean(comp_lifts) if comp_lifts else 0.0

    print(f"\n  Metric                                         Result")
    print(f"  {'─'*55}")
    print(f"  Tokens with REAL OOS signal (|test_r|>0.12):  {oos_real}/{n}")
    print(f"  Tokens with weak OOS signal (|test_r|>0.06):  {oos_weak}/{n}")
    print(f"  Tokens with stable rolling correlation:        {stable}/{n}")
    print(f"  Tokens with extreme-quantile edge (>1pp lift): {extreme_edge}/{n}")
    print(f"  Contrarian tokens with actionable edge:        {contra_ok}/{contra_n}")
    print(f"  Avg composite signal lift:                     {avg_comp_lift:+.2f}pp")

    print(f"\n  ── OOS per token ────────────────────────────────────────────")
    for r in all_results:
        best_oos = max(
            [v for v in r["oos"].values() if isinstance(v, dict) and "test_r" in v],
            key=lambda v: abs(v.get("test_r", 0)),
            default=None
        )
        if best_oos:
            best_h = max(r["oos"].keys(), key=lambda h: abs(r["oos"][h].get("test_r", 0)))
            tr = best_oos["train_r"]
            te = best_oos["test_r"]
            cons = "✓" if best_oos["consistent"] else "✗"
            print(f"  {r['symbol']:<6} best OOS at {best_h}d:  "
                  f"train={tr:+.3f}  test={te:+.3f}  consistent={cons}")

    print(f"\n  ── Final verdict ─────────────────────────────────────────────")
    if oos_real >= n * 0.4:
        verdict = "STRONG SIGNAL — consider subscribing"
    elif oos_real >= n * 0.25 or (oos_weak >= n * 0.5 and avg_comp_lift > 1.5):
        verdict = "WEAK BUT REAL — token-specific use only, marginal ROI"
    elif contra_ok == contra_n and contra_n > 0:
        verdict = "CONTRARIAN SIGNAL ONLY — avoid high-social BTC/ETH/SOL entries"
    elif oos_real == 0 and stable <= 1 and abs(avg_comp_lift) < 0.5:
        verdict = "CONFIRMED NO SIGNAL — save the $150/mo"
    else:
        verdict = "AMBIGUOUS — see per-token detail"

    print(f"\n  >>> {verdict}")

    print(f"\n  Interpretation guide:")
    print(f"  OOS consistent = signal survived train/test split (not noise)")
    print(f"  Stable rolling = r sign consistent across all 90d windows")
    print(f"  Extreme edge   = top/bottom 5-10% of signal readings show >1pp return diff")
    print(f"  Contrarian     = for BTC/SOL/ETH, low social → better entry point")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",   type=int, default=730)
    parser.add_argument("--tokens", default="large_cap")
    args = parser.parse_args()

    if not LUNARCRUSH_KEY:
        print("ERROR: LUNARCRUSH_API_KEY not set")
        sys.exit(1)

    if args.tokens == "all":
        symbols = LC_SYMBOLS
    elif args.tokens == "large_cap":
        symbols = LARGE_CAP
    else:
        symbols = [s.strip().upper() for s in args.tokens.split(",")]

    # Tee output to timestamped file
    os.makedirs("output", exist_ok=True)
    ts_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
    outfile = f"output/backtest_extended_{ts_str}.txt"
    fh      = open(outfile, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)

    fetch_days = args.days + 20  # buffer for forward-return window

    print(f"Devil's advocate social signal backtest")
    print(f"  History  : {args.days}d (requesting {fetch_days}d with buffer)")
    print(f"  Tokens   : {len(symbols)} ({', '.join(symbols)})")
    print(f"  Tests    : rolling stability · extreme quantiles · OOS split · contrarian · composite")
    print(f"  LC key   : ...{LUNARCRUSH_KEY[-6:]}")
    print(f"  Runtime  : ~{len(symbols) * _LC_SLEEP / 60:.1f} min API  +  compute")
    print(f"  Output   : {outfile}")
    print(f"  Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Pre-fetch BTC for regime reference (reused if BTC in symbols)
    print(f"\n[BTC] Fetching {fetch_days}d regime data… ", end="", flush=True)
    btc_raw = _fetch_lunarcrush("BTC", fetch_days)
    print(f"{len(btc_raw)} hourly-agg days returned")
    if "BTC" not in symbols:
        time.sleep(_LC_SLEEP)

    all_results = []
    for idx, sym in enumerate(symbols, 1):
        if sym == "BTC":
            raw = btc_raw
            print(f"\n[{idx}/{len(symbols)}] BTC  (reusing pre-fetched data)")
        else:
            print(f"\n[{idx}/{len(symbols)}] {sym}")
            print(f"  Fetching {fetch_days}d… ", end="", flush=True)
            raw = _fetch_lunarcrush(sym, fetch_days)
            print(f"{len(raw)} days returned")

        if len(raw) < 120:
            print(f"  Insufficient data ({len(raw)} days) — skipping")
            if idx < len(symbols):
                time.sleep(_LC_SLEEP)
            continue

        # Feature engineering
        raw = _add_zscore(raw, window=ZSCORE_WIN)
        raw = _add_momentum(raw, window=3)
        rows = _build_rows(raw, HORIZONS)

        if len(rows) < 100:
            print(f"  Only {len(rows)} usable rows — skipping (need ≥100)")
            if idx < len(symbols):
                time.sleep(_LC_SLEEP)
            continue

        result = _analyse_token(sym, rows)
        all_results.append(result)

        if idx < len(symbols):
            time.sleep(_LC_SLEEP)

    _verdict(all_results)

    print(f"\nDone. {len(all_results)}/{len(symbols)} tokens analysed.")
    print(f"Full results saved to: {outfile}")
    fh.close()
    sys.stdout = sys.__stdout__
    print(f"Full results saved to: {outfile}")


if __name__ == "__main__":
    main()
