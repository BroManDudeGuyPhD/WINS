"""
wins/ingestion/collector.py
Pulls market data, news, social, on-chain, and GitHub signals.
Returns a SignalBundle per target token every decision cycle.
"""
import asyncio
import httpx
import asyncpg
from decimal import Decimal
from datetime import datetime, timezone
import random

from wins.shared.config import (
    COINGECKO_API_KEY, LUNARCRUSH_API_KEY, GITHUB_TOKEN,
    TARGET_TOKENS, MACRO_TOKENS,
)
from wins.shared.models import MarketSnapshot, SignalBundle
from wins.shared.logger import get_logger

log = get_logger("ingestion")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
LUNARCRUSH_BASE = "https://lunarcrush.com/api4/public"

# CoinGecko uses full coin IDs, not ticker symbols.
# https://api.coingecko.com/api/v3/coins/list (subset — extend as needed)
COINGECKO_IDS: dict[str, str] = {
    "BTC":    "bitcoin",
    "ETH":    "ethereum",
    "SOL":    "solana",
    "AVAX":   "avalanche-2",
    "DOT":    "polkadot",
    "LINK":   "chainlink",
    "ARB":    "arbitrum",
    "OP":     "optimism",
    "INJ":    "injective-protocol",
    "SUI":    "sui",
    "APT":    "aptos",
    "NEAR":   "near",
    "FTM":    "fantom",
    "ATOM":   "cosmos",
    "ALGO":   "algorand",
    "AAVE":   "aave",
    "UNI":    "uniswap",
    "SNX":    "havven",
    "CRV":    "curve-dao-token",
    "LDO":    "lido-dao",
    "DYDX":   "dydx",
    "GMX":    "gmx",
    "PENDLE": "pendle",
    "JUP":    "jupiter-exchange-solana",
    "PYTH":   "pyth-network",
    "WIF":    "dogwifcoin",
    "BONK":   "bonk",
}

def _symbol_to_cg_id(symbol: str) -> str | None:
    return COINGECKO_IDS.get(symbol.upper())


# ─── CoinGecko ───────────────────────────────────────────────────────────────

async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET with exponential backoff on 429."""
    for attempt in range(4):
        resp = await client.get(url, **kwargs)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        wait = (2 ** attempt) + random.uniform(0, 1)
        log.warning(f"429 from {url} — retrying in {wait:.1f}s (attempt {attempt + 1}/4)")
        await asyncio.sleep(wait)
    resp.raise_for_status()
    return resp


async def fetch_prices(client: httpx.AsyncClient, symbols: list[str]) -> dict[str, MarketSnapshot]:
    """Fetch price, volume, market cap for a list of symbols."""
    # Map symbols to CoinGecko IDs, skipping any unmapped ones
    id_to_symbol: dict[str, str] = {}
    for sym in symbols:
        cg_id = _symbol_to_cg_id(sym)
        if cg_id:
            id_to_symbol[cg_id] = sym
        else:
            log.warning(f"No CoinGecko ID mapped for {sym} — skipping.")

    if not id_to_symbol:
        return {}

    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    resp = await _get_with_retry(
        client,
        f"{COINGECKO_BASE}/simple/price",
        params={
            "ids": ",".join(id_to_symbol.keys()),
            "vs_currencies": "usd",
            "include_market_cap": "true",
            "include_24hr_vol": "true",
            "include_24hr_change": "true",
        },
        headers=headers,
        timeout=15,
    )
    data = resp.json()

    snapshots: dict[str, MarketSnapshot] = {}
    for cg_id, symbol in id_to_symbol.items():
        if cg_id not in data:
            log.warning(f"CoinGecko returned no data for {symbol} (id={cg_id})")
            continue
        row = data[cg_id]
        snapshots[symbol] = MarketSnapshot(
            token          = symbol,
            price_usd      = Decimal(str(row.get("usd", 0))),
            volume_24h_usd = Decimal(str(row.get("usd_24h_vol", 0))),
            change_24h_pct = Decimal(str(row.get("usd_24h_change", 0))),
            market_cap_usd = Decimal(str(row.get("usd_market_cap", 0))),
        )
    return snapshots


async def fetch_btc_dominance(client: httpx.AsyncClient) -> Decimal:
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    resp = await _get_with_retry(
        client,
        f"{COINGECKO_BASE}/global",
        headers=headers,
        timeout=15,
    )
    pct = resp.json()["data"]["market_cap_percentage"].get("btc", 0)
    return Decimal(str(pct))


# ─── LunarCrush (social sentiment) ───────────────────────────────────────────

async def fetch_social_summary(client: httpx.AsyncClient, symbol: str) -> tuple[str, dict, bool]:
    """
    Returns (summary_for_claude, raw_fields_dict, fetch_ok).
    Uses time-series/v2 for a 2-day window to get the full social metric set
    (social_dominance, sentiment, interactions_24h, galaxy_score, alt_rank).
    The v1 snapshot endpoint does not return sentiment/interactions/social_dominance.
    """
    import time as _time
    if not LUNARCRUSH_API_KEY:
        return "", {}, True  # no key configured — absence is expected, not an error
    try:
        now = int(_time.time())
        resp = await _get_with_retry(
            client,
            f"{LUNARCRUSH_BASE}/coins/{symbol.upper()}/time-series/v2",
            headers={"Authorization": f"Bearer {LUNARCRUSH_API_KEY}"},
            params={"start": now - 2 * 86400, "end": now},
            timeout=20,
        )
        body = resp.json()
        rows = body.get("data", [])
        if not rows:
            log.warning(f"LunarCrush time-series returned empty data for {symbol}. Body: {str(body)[:300]}")
            return "", {}, False
        # Use the most recent row
        latest = rows[-1]
        raw = {
            "galaxy_score":     latest.get("galaxy_score"),
            "alt_rank":         latest.get("alt_rank"),
            "sentiment":        latest.get("sentiment"),
            "interactions_24h": latest.get("interactions_24h") or latest.get("interactions"),
            "social_dominance": latest.get("social_dominance"),
        }
        dom = raw["social_dominance"]
        summary = (
            f"Galaxy score: {raw['galaxy_score'] or 'n/a'}, "
            f"AltRank: {raw['alt_rank'] or 'n/a'}, "
            f"Sentiment: {raw['sentiment'] or 'n/a'}, "
            f"24h interactions: {raw['interactions_24h'] or 'n/a'}"
            + (f", social dominance: {dom:.4f}%" if dom is not None else "")
        )
        return summary, raw, True
    except httpx.HTTPStatusError as exc:
        log.warning(
            f"LunarCrush HTTP {exc.response.status_code} for {symbol}: "
            f"{exc.response.text[:300]}"
        )
        return "", {}, False
    except Exception as exc:
        log.warning(f"LunarCrush fetch failed for {symbol}: {exc}")
        return "", {}, False


# ─── Social filter (percentile-based pre-Claude gate) ────────────────────────

# Backtest-derived directionality (730-day OOS-confirmed signal).
# "contrarian": high social_dominance → negative forward returns → skip buys
# "bullish":    high social_dominance → positive forward returns → boost buys
_SOCIAL_DIRECTION: dict[str, str] = {
    "SOL": "contrarian",
    "SUI": "bullish",
}
_CONTRARIAN_SKIP_ABOVE = 60.0   # skip SOL buy when social in top 40%
_BULLISH_SKIP_BELOW    = 40.0   # skip SUI buy when social in bottom 40%
_BULLISH_BOOST_ABOVE   = 70.0   # boost SUI when social in top 30%


async def _social_dominance_percentile(
    pool: asyncpg.Pool, token: str, value: float
) -> float | None:
    """Return 0-100 percentile rank of `value` within last 90 days of social_history."""
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE social_dominance <= $2) * 100.0 / NULLIF(COUNT(*), 0) AS pct
        FROM social_history
        WHERE token = $1
          AND date >= CURRENT_DATE - INTERVAL '90 days'
          AND social_dominance IS NOT NULL
        """,
        token, value,
    )
    if row is None or row["pct"] is None:
        return None
    return float(row["pct"])


async def apply_social_filter(pool: asyncpg.Pool, bundles: list[SignalBundle]) -> None:
    """
    Enriches each bundle with social_dominance_pct and social_filter_verdict.
    Mutates bundles in place. Runs after collect_signal_bundles().
    Degrades gracefully if social_history table is empty or missing.
    """
    for bundle in bundles:
        raw_dom = bundle.social_raw.get("social_dominance")
        if raw_dom is None:
            continue  # v1 endpoint didn't return social_dominance — proceed normally

        bundle.social_dominance = float(raw_dom)
        direction = _SOCIAL_DIRECTION.get(bundle.token)
        if direction is None:
            continue  # no backtest signal for this token

        try:
            pct = await _social_dominance_percentile(pool, bundle.token, bundle.social_dominance)
        except Exception as exc:
            log.warning(f"Social filter skipped for {bundle.token}: {exc}")
            continue

        if pct is None:
            log.info(f"Social filter: no history yet for {bundle.token} — proceeding without filter")
            continue

        bundle.social_dominance_pct = pct

        if direction == "contrarian" and pct >= _CONTRARIAN_SKIP_ABOVE:
            bundle.social_filter_verdict = "skip"
            log.info(
                f"Social filter SKIP {bundle.token}: contrarian, "
                f"social_dominance at {pct:.0f}th pct (threshold >{_CONTRARIAN_SKIP_ABOVE:.0f})"
            )
        elif direction == "bullish" and pct <= _BULLISH_SKIP_BELOW:
            bundle.social_filter_verdict = "skip"
            log.info(
                f"Social filter SKIP {bundle.token}: bullish but social weak, "
                f"social_dominance at {pct:.0f}th pct (threshold <{_BULLISH_SKIP_BELOW:.0f})"
            )
        elif direction == "bullish" and pct >= _BULLISH_BOOST_ABOVE:
            bundle.social_filter_verdict = "boost"
            bundle.social_summary += (
                f" [Social confirming: dominance at {pct:.0f}th percentile (90d), "
                f"historically strong entry zone for {bundle.token}]"
            )
            log.info(
                f"Social filter BOOST {bundle.token}: social confirming, "
                f"social_dominance at {pct:.0f}th pct"
            )


# ─── GitHub developer activity ───────────────────────────────────────────────

# Maps token symbol → GitHub org/repo (extend as needed)
GITHUB_REPOS: dict[str, str] = {
    "SOL":  "solana-labs/solana",
    "AVAX": "ava-labs/avalanchego",
    "DOT":  "paritytech/polkadot-sdk",
    "LINK": "smartcontractkit/chainlink",
    "ARB":  "OffchainLabs/arbitrum",
    "OP":   "ethereum-optimism/optimism",
    "NEAR": "near/nearcore",
    "AAVE": "aave/aave-v3-core",
    "UNI":  "Uniswap/v3-core",
}


async def fetch_github_summary(client: httpx.AsyncClient, symbol: str) -> str:
    repo = GITHUB_REPOS.get(symbol)
    if not repo:
        return ""
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        commits_resp = await client.get(
            f"https://api.github.com/repos/{repo}/commits",
            params={"per_page": 10},
            headers=headers,
            timeout=15,
        )
        commits_resp.raise_for_status()
        commits = commits_resp.json()
        count = len(commits)
        latest_msg = commits[0]["commit"]["message"][:100] if commits else "none"
        return f"Recent commits (last 10 fetched): {count}. Latest: {latest_msg}"
    except Exception as exc:
        log.warning(f"GitHub fetch failed for {symbol}: {exc}")
        return ""


# ─── News (RSS-based placeholder) ────────────────────────────────────────────

async def fetch_news_summary(client: httpx.AsyncClient, symbol: str) -> str:
    """Stub: wire up CoinDesk/Decrypt/TheBlock RSS feeds and Haiku summarisation here."""
    return ""


# ─── Main bundle assembly ─────────────────────────────────────────────────────

async def collect_signal_bundles() -> list[SignalBundle]:
    """
    Called once per decision cycle.
    Returns a SignalBundle for each target token.
    """
    all_symbols = TARGET_TOKENS + MACRO_TOKENS
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        prices_task    = fetch_prices(client, all_symbols)
        dominance_task = fetch_btc_dominance(client)

        prices, btc_dominance = await asyncio.gather(prices_task, dominance_task)

    btc_snapshot = prices.get("BTC")
    if btc_snapshot:
        btc_snapshot = btc_snapshot.model_copy(update={"btc_dominance": btc_dominance})

    if not btc_snapshot:
        log.error("Could not fetch BTC macro snapshot — aborting cycle.")
        return []

    async def _fetch_bundle(client: httpx.AsyncClient, symbol: str) -> SignalBundle | None:
        market = prices.get(symbol)
        if not market:
            log.warning(f"No price data for {symbol}, skipping.")
            return None
        (social, social_raw, social_ok), github, news = await asyncio.gather(
            fetch_social_summary(client, symbol),
            fetch_github_summary(client, symbol),
            fetch_news_summary(client, symbol),
        )
        return SignalBundle(
            token           = symbol,
            market          = market,
            macro           = btc_snapshot,
            news_summary    = news,
            social_summary  = social,
            social_raw      = social_raw,
            social_data_ok  = social_ok,
            github_summary  = github,
        )

    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(
            *[_fetch_bundle(client, sym) for sym in TARGET_TOKENS],
            return_exceptions=True,
        )

    bundles: list[SignalBundle] = []
    for sym, res in zip(TARGET_TOKENS, results):
        if isinstance(res, Exception):
            log.warning(f"Bundle fetch failed for {sym}: {res}")
        elif res is not None:
            bundles.append(res)

    log.info(f"Collected signal bundles for {len(bundles)} tokens.")
    return bundles
