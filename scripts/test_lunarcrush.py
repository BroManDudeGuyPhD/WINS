"""
scripts/test_lunarcrush.py
Quick local script to diagnose LunarCrush API issues without running the full system.

Usage:
    python scripts/test_lunarcrush.py [SYMBOL ...]

    # Test BTC and SOL (defaults):
    python scripts/test_lunarcrush.py

    # Test specific tokens:
    python scripts/test_lunarcrush.py BTC ETH SOL
"""
import asyncio
import json
import os
import sys
import httpx

LUNARCRUSH_BASE = "https://lunarcrush.com/api4/public"
API_KEY = os.environ.get("LUNARCRUSH_API_KEY", "")

SYMBOLS = sys.argv[1:] if len(sys.argv) > 1 else ["BTC", "SOL", "ETH"]

URL_VARIANTS = [
    "{base}/coins/{symbol}/v1",         # uppercase — expected correct form
    "{base}/coins/{symbol_lower}/v1",   # lowercase — likely wrong but worth checking
]


async def probe(client: httpx.AsyncClient, symbol: str) -> None:
    print(f"\n{'='*60}")
    print(f"  Symbol: {symbol}")
    print(f"{'='*60}")

    if not API_KEY:
        print("  ERROR: LUNARCRUSH_API_KEY not set in environment.")
        return

    for url_template in URL_VARIANTS:
        url = url_template.format(
            base=LUNARCRUSH_BASE,
            symbol=symbol.upper(),
            symbol_lower=symbol.lower(),
        )
        print(f"\n  URL: {url}")
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=15,
            )
            print(f"  Status: {resp.status_code}")
            try:
                body = resp.json()
                if resp.status_code == 200:
                    data = body.get("data", {})
                    print("  Fields present in data:", list(data.keys()) if data else "(empty)")
                    interesting = {
                        k: data.get(k)
                        for k in ("galaxy_score", "alt_rank", "sentiment", "interactions_24h", "symbol", "name")
                    }
                    print("  Key fields:", json.dumps(interesting, indent=4))
                else:
                    print("  Error body:", json.dumps(body, indent=4)[:500])
            except Exception:
                print("  Non-JSON body:", resp.text[:500])
        except Exception as exc:
            print(f"  Exception: {exc}")


async def main() -> None:
    print(f"LunarCrush API probe — key suffix: ...{API_KEY[-6:] if API_KEY else '(not set)'}")
    async with httpx.AsyncClient() as client:
        for sym in SYMBOLS:
            await probe(client, sym)
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
