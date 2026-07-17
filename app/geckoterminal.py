"""GeckoTerminal client - free, keyless public API for DEX/on-chain token data.

Why this exists: CoinGecko's main coin database (app/coingecko.py) requires
manual/automated listing review and typically lags brand-new token launches
by hours to days. That's exactly the wrong tool for this ASP's primary use
case - fetching data on new coins across chains by contract address, often
within minutes of launch. GeckoTerminal is CoinGecko's own DEX-tracking
product and indexes new pools/tokens far faster, keyed by chain + contract
address instead of a curated coin id.

Verified live against real GeckoTerminal responses on 2026-07-17 (this
sandbox's own network is allowlisted and blocked api.geckoterminal.com,
so the user ran the curl calls directly and pasted back the output).
get_token / get_token_pools matched the originally-assumed shape exactly.
detect_network did NOT: pool/token relationship objects don't carry a
`network` field at all - the network is embedded as a prefix on the id
itself, e.g. `"base_0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"` -> network
`base`. Fixed below to parse that instead of a nonexistent relationships
field.
"""
from __future__ import annotations

import re

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


# GeckoTerminal ids are formatted "{network}_{address}", e.g.
# "base_0x833589fcd6edb6e08f4c7c32d4f71b54bda02913" or
# "eth_0xdac17f958d2ee523a2206206994597c13d831ec". The network segment can
# itself contain underscores (e.g. "polygon_pos"), so this greedily captures
# everything before the final "_<address>" rather than splitting on the
# first underscore.
_ID_SUFFIX_RE = re.compile(
    r"^(?P<network>.+)_(?P<address>0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$"
)


def _parse_network_from_id(gt_id: str) -> tuple[str, str] | None:
    m = _ID_SUFFIX_RE.match(gt_id or "")
    if not m:
        return None
    return m.group("network"), m.group("address")


async def detect_network(client: httpx.AsyncClient, address: str) -> str | None:
    """Auto-detect which chain a contract address lives on, so the caller
    doesn't have to specify `chain` up front - searches GeckoTerminal's
    cross-network pool search and reads the network off of whichever
    pool's base/quote token id actually matches the input address.

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

        target = address.strip().lower()
        for pool in data:
            rels = pool.get("relationships", {}) or {}
            for token_key in ("base_token", "quote_token"):
                token_id = (rels.get(token_key) or {}).get("data", {}).get("id")
                parsed = _parse_network_from_id(token_id) if token_id else None
                if parsed and parsed[1].lower() == target:
                    return parsed[0]

        # Didn't find an exact address match in any relationship (unexpected
        # shape, or the match was on something not covered above) - fall
        # back to the first result's own network as a best-effort guess.
        parsed = _parse_network_from_id(data[0].get("id", ""))
        return parsed[0] if parsed else None
    except Exception:
        return None
