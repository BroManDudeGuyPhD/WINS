"""
wins/backtest/harness.py

Replays historical price data through make_decision() and scores each
decision against actual prices N days later.

Usage:
    python -m wins.backtest.harness [--days 90] [--real] [--tokens SOL,SUI] [--horizon 2]

Default is mock brain (no API spend). Pass --real to use live Claude.
Social, news, and on-chain signals are stubbed to empty strings — price +
macro only — until enough signal_log history exists.
"""
from __future__ import annotations

import argparse
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import requests

from wins.brain.mock_decision import mock_decision
from wins.ingestion.collector import COINGECKO_IDS
from wins.shared.config import TARGET_TOKENS
from wins.shared.models import MarketSnapshot, SignalBundle

COINGECKO_API = "https://api.coingecko.com/api/v3"
_RATE_SLEEP = 1.5  # seconds between free-tier CoinGecko requests


@dataclass
class BacktestResult:
    ts: datetime
    token: str
    action: str
    confidence: float
    signal_type: str
    entry_price: float
    future_price: float
    horizon_days: int
    pnl_pct: float
    win: bool
    macro_gate: str
    risk_flag: str


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def _fetch_ohlcv(coingecko_id: str, days: int) -> list[dict]:
    """Return daily price/volume/market_cap points from CoinGecko."""
    resp = requests.get(
        f"{COINGECKO_API}/coins/{coingecko_id}/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    prices = data["prices"]
    volumes = data["total_volumes"]
    caps = data["market_caps"]

    return [
        {
            "ts": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
            "price": price,
            "volume_24h": volumes[i][1] if i < len(volumes) else 0.0,
            "market_cap": caps[i][1] if i < len(caps) else 0.0,
        }
        for i, (ts_ms, price) in enumerate(prices)
    ]


# ---------------------------------------------------------------------------
# Bundle construction
# ---------------------------------------------------------------------------

def _build_bundle(
    token: str,
    day: dict,
    prev_day: dict,
    btc_day: dict,
    prev_btc_day: dict,
) -> SignalBundle:
    def _chg(cur: float, prev: float) -> Decimal:
        if prev == 0:
            return Decimal("0")
        return Decimal(str(round((cur - prev) / prev * 100, 4)))

    market = MarketSnapshot(
        token=token,
        price_usd=Decimal(str(round(day["price"], 8))),
        volume_24h_usd=Decimal(str(round(day["volume_24h"], 2))),
        change_24h_pct=_chg(day["price"], prev_day["price"]),
        market_cap_usd=Decimal(str(round(day["market_cap"], 2))),
    )
    macro = MarketSnapshot(
        token="BTC",
        price_usd=Decimal(str(round(btc_day["price"], 2))),
        volume_24h_usd=Decimal(str(round(btc_day["volume_24h"], 2))),
        change_24h_pct=_chg(btc_day["price"], prev_btc_day["price"]),
        market_cap_usd=Decimal(str(round(btc_day["market_cap"], 2))),
    )
    return SignalBundle(
        token=token,
        market=market,
        macro=macro,
        news_summary="",
        social_summary="",
        social_raw={},
        onchain_summary="",
        github_summary="",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_backtest(
    tokens: list[str],
    days: int = 90,
    use_mock: bool = True,
    horizon_days: int = 2,
) -> list[BacktestResult]:
    if use_mock:
        def _decide(bundle: SignalBundle, _account: dict, _as_of: str):
            return mock_decision(bundle), "mock", 0, 0, 0
    else:
        from wins.brain.decision import make_decision as _real
        _decide = _real

    fetch_days = days + horizon_days + 2  # extra buffer for alignment

    print(f"Fetching BTC ({fetch_days}d)…")
    btc_data = _fetch_ohlcv("bitcoin", fetch_days)
    time.sleep(_RATE_SLEEP)

    results: list[BacktestResult] = []

    for token in tokens:
        cg_id = COINGECKO_IDS.get(token)
        if not cg_id:
            print(f"  {token}: no CoinGecko ID — skipping")
            continue

        print(f"Fetching {token} / {cg_id} ({fetch_days}d)…")
        token_data = _fetch_ohlcv(cg_id, fetch_days)
        time.sleep(_RATE_SLEEP)

        # We need: index 0 as prev, index 1..N-horizon as decision points,
        # index i+horizon as future price.
        usable = min(len(token_data), len(btc_data)) - horizon_days - 1
        if usable < 5:
            print(f"  {token}: only {len(token_data)} data points — skipping")
            continue

        print(f"  {token}: running {usable - 1} decisions…")

        for i in range(1, usable):
            day = token_data[i]
            prev_day = token_data[i - 1]

            # Align BTC by nearest timestamp
            btc_idx = min(
                range(len(btc_data)),
                key=lambda j: abs((btc_data[j]["ts"] - day["ts"]).total_seconds()),
            )
            btc_prev_idx = max(0, btc_idx - 1)

            bundle = _build_bundle(
                token, day, prev_day,
                btc_data[btc_idx], btc_data[btc_prev_idx],
            )
            account_state = {"capital_usd": 10_000.0, "open_positions": 0}

            decision, *_ = _decide(bundle, account_state, day["ts"].isoformat())
            if decision is None:
                continue

            future_price = token_data[i + horizon_days]["price"]
            entry = float(decision.entry_price) or day["price"]
            pnl_pct = (future_price - entry) / entry * 100 if entry > 0 else 0.0

            action_val = decision.action.value
            if action_val == "buy":
                win = future_price > entry
            elif action_val == "sell":
                win = future_price < entry
            else:
                win = False

            results.append(BacktestResult(
                ts=day["ts"],
                token=token,
                action=action_val,
                confidence=float(decision.confidence),
                signal_type=decision.signal_type.value,
                entry_price=entry,
                future_price=future_price,
                horizon_days=horizon_days,
                pnl_pct=pnl_pct,
                win=win,
                macro_gate=decision.macro_gate.value,
                risk_flag=decision.risk_flag.value,
            ))

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(results: list[BacktestResult], horizon_days: int) -> None:
    buys = [r for r in results if r.action == "buy"]
    holds = [r for r in results if r.action == "hold"]
    sells = [r for r in results if r.action == "sell"]

    print()
    print("=" * 62)
    print(f"BACKTEST REPORT  —  {len(results)} decisions, {horizon_days}d outcome horizon")
    print("=" * 62)
    print(f"  Buys: {len(buys)}   Holds: {len(holds)}   Sells: {len(sells)}")

    if buys:
        win_rate = sum(1 for r in buys if r.win) / len(buys) * 100
        avg_pnl = sum(r.pnl_pct for r in buys) / len(buys)
        print(f"  Buy win rate : {win_rate:.1f}%")
        print(f"  Buy avg P&L  : {avg_pnl:+.2f}%")
    else:
        print("  No buy decisions to score.")

    if buys:
        _section("By Confidence Bucket (buys)", [
            ("0.65–0.75", [r for r in buys if 0.65 <= r.confidence < 0.75]),
            ("0.75–0.85", [r for r in buys if 0.75 <= r.confidence < 0.85]),
            ("0.85+",     [r for r in buys if r.confidence >= 0.85]),
        ])

        sig_groups = defaultdict(list)
        for r in buys:
            sig_groups[r.signal_type].append(r)
        _section("By Signal Type (buys)", sorted(sig_groups.items()))

        tok_groups = defaultdict(list)
        for r in buys:
            tok_groups[r.token].append(r)
        _section("By Token (buys)", sorted(tok_groups.items()))

    print("=" * 62)


def _section(title: str, groups: list[tuple[str, list[BacktestResult]]]) -> None:
    rows = [(label, g) for label, g in groups if g]
    if not rows:
        return
    print(f"\n  {title}:")
    for label, group in rows:
        wr = sum(1 for r in group if r.win) / len(group) * 100
        avg = sum(r.pnl_pct for r in group) / len(group)
        print(f"    {label:12s}  n={len(group):3d}  win={wr:5.1f}%  avg_pnl={avg:+.2f}%")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="WINS Backtest Harness")
    parser.add_argument("--days",    type=int, default=90,  help="Days of history (default: 90)")
    parser.add_argument("--horizon", type=int, default=2,   help="Outcome horizon in days (default: 2)")
    parser.add_argument("--tokens",  type=str, default="",  help="Comma-separated tokens (default: all TARGET_TOKENS)")
    parser.add_argument("--real",    action="store_true",   help="Use live Claude API instead of mock brain")
    args = parser.parse_args()

    use_mock = not args.real
    tokens = [t.strip().upper() for t in args.tokens.split(",")] if args.tokens else list(TARGET_TOKENS)

    print(f"Backtest: {args.days}d history, {args.horizon}d horizon, {'mock' if use_mock else 'real Claude'}")
    print(f"Tokens: {tokens}")
    print()

    results = run_backtest(tokens=tokens, days=args.days, use_mock=use_mock, horizon_days=args.horizon)
    print_report(results, args.horizon)


if __name__ == "__main__":
    main()
