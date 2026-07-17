"""CoinGecko client - free, keyless public API.

Provides community_score, developer_score, sentiment votes, market data,
and categories, which cover most of the sentiment sub-dimensions without
needing gated social APIs.

NOTE: not live-tested in the build sandbox (outbound network is
allowlisted there and blocked api.coingecko.com). Verify against the
real endpoint once deployed - shape below is based on CoinGecko's
documented, stable public API.
"""
from __future__ import annotations

import httpx

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


class CoinGeckoError(Exception):
    pass


async def resolve_coin_id(client: httpx.AsyncClient, token: str) -> dict:
    """Resolve a ticker or free-text token name to a CoinGecko coin id.

    Uses /search, then picks the top match. Falls back to treating the
    input as already being a coin id if search returns nothing usable.
    """
    resp = await client.get(f"{COINGECKO_BASE}/search", params={"query": token})
    if resp.status_code != 200:
        raise CoinGeckoError(f"search failed: {resp.status_code}")
    data = resp.json()
    coins = data.get("coins", [])
    if not coins:
        return {"id": token.lower(), "symbol": token.upper(), "name": token}

    # Prefer exact ticker match, then highest market cap rank (lowest number).
    exact = [c for c in coins if c.get("symbol", "").lower() == token.lower()]
    candidates = exact or coins
    candidates.sort(key=lambda c: (c.get("market_cap_rank") is None, c.get("market_cap_rank") or 1e9))
    top = candidates[0]
    return {"id": top["id"], "symbol": top["symbol"].upper(), "name": top["name"]}


async def get_coin_data(client: httpx.AsyncClient, coin_id: str) -> dict:
    resp = await client.get(
        f"{COINGECKO_BASE}/coins/{coin_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "true",
            "developer_data": "true",
            "sparkline": "false",
        },
    )
    if resp.status_code == 429:
        raise CoinGeckoError("rate limited by CoinGecko - retry later or add API key")
    if resp.status_code != 200:
        raise CoinGeckoError(f"coin lookup failed: {resp.status_code}")
    return resp.json()
