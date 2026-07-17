"""GeckoTerminal client - free, keyless public API for DEX/on-chain token data.

Why this exists: CoinGecko's main coin database (app/coingecko.py) requires
manual/automated listing review and typically lags brand-new token launches
by hours to days. That's exactly the wrong tool for this ASP's primary use
case - fetching data on new coins across chains by contract address, often
within minutes of launch. GeckoTerminal is CoinGecko's own DEX-tracking
product and indexes new pools/tokens far faster, keyed by chain + contract
address instead of a curated coin id.

NOTE: not live-tested in the build sandbox (outbound network there is
allowlisted and blocked api.geckoterminal.com, same as it blocked
CoinGecko/alternative.me earlier in this build). This is a long-standing
stable public API; verify against the real endpoint once deployed.
"""
from __future__ import annotations

import httpx

GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"

# Common aliases -> GeckoTerminal network slugs. Not exhaustive - if a chain
# isn't in this map, we pass the user's input through as-is (GeckoTerminal's
# own network list at /networks is the source of truth; ask the user to
# check that if a lookup 404s on an unmapped chain).
NETWORK_ALIASES = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "binance-smart-chain": "bsc",
    "bnb": "bsc",
    "polygon": "polygon_pos",
    "matic": "polygon_pos",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "base": "base",
    "solana": "solana",
    "sol": "solana",
    "avalanche": "avax",
    "avax": "avax",
    # X Layer's exact GeckoTerminal slug is unconfirmed (not verifiable from
    # this sandbox) - "x-layer" / "xlayer" are the most likely candidates.
    # If this 404s, check https://api.geckoterminal.com/api/v2/networks for
    # the real slug and update this map.
    "x-layer": "xlayer",
    "xlayer": "xlayer",
}


class GeckoTerminalError(Exception):
    pass


def resolve_network(chain: str) -> str:
    return NETWORK_ALIASES.get(chain.strip().lower(), chain.strip().lower())


async def get_token(client: httpx.AsyncClient, network: str, address: str) -> dict:
    """Fetch token metadata + current market snapshot."""
    resp = await client.get(f"{GECKOTERMINAL_BASE}/networks/{network}/tokens/{address}")
    if resp.status_code == 404:
        raise GeckoTerminalError(
            f"token not found on network '{network}' - check the contract address "
            "and chain, or the token may not have an indexed pool yet"
        )
    if resp.status_code == 429:
        raise GeckoTerminalError("rate limited by GeckoTerminal - retry later")
    if resp.status_code != 200:
        raise GeckoTerminalError(f"token lookup failed: {resp.status_code}")
    return resp.json()


async def get_token_pools(client: httpx.AsyncClient, network: str, address: str) -> list[dict]:
    """Fetch the token's trading pools (price momentum, volume, tx counts) -
    this is the main signal source for brand-new tokens with no other
    history. Returns [] on any failure rather than raising, since pool data
    is supplementary - a missing pools list shouldn't kill the whole request.
    """
    try:
        resp = await client.get(
            f"{GECKOTERMINAL_BASE}/networks/{network}/tokens/{address}/pools"
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("data", [])
    except Exception:
        return []


async def detect_network(client: httpx.AsyncClient, address: str) -> str | None:
    """Auto-detect which chain a contract address lives on, so the caller
    doesn't have to specify `chain` up front - searches GeckoTerminal's
    cross-network pool search and reads the network off the first match.

    Returns the GeckoTerminal network slug, or None if no pool anywhere
    matches this address (e.g. genuinely not found, or too new/no pool yet).
    """
    try:
        resp = await client.get(
            f"{GECKOTERMINAL_BASE}/search/pools", params={"query": address}
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        if not data:
            return None
        # Each result has a relationship to its network; take the first hit.
        network = (
            data[0].get("relationships", {}).get("network", {}).get("data", {}).get("id")
        )
        return network
    except Exception:
        return None
