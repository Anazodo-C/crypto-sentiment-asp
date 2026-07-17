"""Smoke test using fixture data, since this sandbox can't reach
CoinGecko/alternative.me/Twitter directly (network allowlist blocks them).
Mocks the three external clients and drives the real FastAPI endpoint +
scoring engine end-to-end so we can verify: request/response shape, math,
category weighting, and graceful degradation when Twitter is unavailable.
"""
import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

# Real GeckoTerminal /search/pools response for Base USDC, captured live by
# the user on 2026-07-17 - this is what caught detect_network() assuming a
# relationships.network field that doesn't actually exist. Truncated to the
# fields that matter for parsing.
REAL_GT_SEARCH_RESPONSE = {
    "data": [
        {
            "id": "base_0xd0b53d9277642d899df5c87a3966a349a798f224",
            "type": "pool",
            "attributes": {"name": "USDC / WETH 0.05%"},
            "relationships": {
                "base_token": {
                    "data": {"id": "base_0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "type": "token"}
                },
                "quote_token": {
                    "data": {"id": "base_0x4200000000000000000000000000000000000006", "type": "token"}
                },
            },
        }
    ]
}


def test_detect_network_against_real_response():
    """Regression test for the detect_network parsing bug - runs the real
    parsing function against a real captured API response, no mocking of
    our own code, only the HTTP layer."""
    from app import geckoterminal

    class FakeResponse:
        status_code = 200
        def json(self):
            return REAL_GT_SEARCH_RESPONSE

    class FakeClient:
        async def get(self, url, params=None):
            return FakeResponse()

    network = asyncio.run(
        geckoterminal.detect_network(FakeClient(), "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    )
    assert network == "base", f"expected 'base', got {network!r}"
    print("detect_network() correctly parsed 'base' from real API response")

FIXTURE_COIN = {
    "id": "solana",
    "symbol": "sol",
    "name": "Solana",
    "categories": ["Smart Contract Platform", "Layer 1 (L1)"],
    "community_score": 62.4,
    "liquidity_score": 74.0,
    "public_interest_score": 0.045,
    "sentiment_votes_up_percentage": 71.5,
    "sentiment_votes_down_percentage": 28.5,
    "market_cap_rank": 5,
    "community_data": {
        "twitter_followers": 2800000,
        "reddit_subscribers": 350000,
        "reddit_average_posts_48h": 12.3,
        "telegram_channel_user_count": None,
    },
    "market_data": {
        "price_change_percentage_30d_in_currency": {"usd": 18.4},
        "total_volume": {"usd": 1_200_000_000},
        "market_cap": {"usd": 90_000_000_000},
    },
}

# A coin with almost nothing populated - simulates CoinGecko returning a
# real but sparse record (common in practice: many of the *_score /
# community_data / market_data fields are null for large numbers of
# coins). This is the regression fixture for the "never fabricate data"
# requirement: every affected dimension below must come back as
# score=None / confidence="unavailable", never a plausible-looking
# invented number.
SPARSE_COIN = {
    "id": "obscure-coin",
    "symbol": "obs",
    "name": "Obscure Coin",
    "categories": ["Layer 1 (L1)"],
    "community_score": None,
    "liquidity_score": None,
    "public_interest_score": None,
    "sentiment_votes_up_percentage": None,
    "market_cap_rank": None,
    "community_data": {},
    "market_data": {},
}

FIXTURE_FNG = {"value": 62, "label": "Greed", "trend_7d": "rising"}

FIXTURE_GT_TOKEN = {
    "data": {
        "attributes": {
            "name": "Brand New Coin",
            "symbol": "bnc",
            "address": "0x1234567890abcdef1234567890abcdef12345678",
        }
    }
}

FIXTURE_GT_POOLS = [
    {
        "attributes": {
            "price_change_percentage": {"h24": "42.5"},
            "volume_usd": {"h24": "250000"},
            "transactions": {"h24": {"buys": 310, "sells": 190}},
        }
    }
]


def run():
    with patch("app.main.coingecko.resolve_coin_id", new=AsyncMock(
        return_value={"id": "solana", "symbol": "SOL", "name": "Solana"}
    )), patch("app.main.coingecko.get_coin_data", new=AsyncMock(
        return_value=FIXTURE_COIN
    )), patch("app.main.feargreed.get_fear_greed", new=AsyncMock(
        return_value=FIXTURE_FNG
    )), patch("app.main.twitter.get_recent_mentions", new=AsyncMock(
        return_value={"available": False, "reason": "forbidden - free tier likely lacks search access"}
    )):
        from app.main import app
        client = TestClient(app)

        # 1. health check
        r = client.get("/health")
        assert r.status_code == 200, r.text
        print("health check OK:", r.json())

        # 2. sentiment call, Twitter unavailable -> should fall back gracefully
        r = client.post("/sentiment", json={"token": "SOL"})
        assert r.status_code == 200, r.text
        body = r.json()
        print("\n--- sentiment response (fallback path) ---")
        print("score:", body["sentiment_score"], "assessment:", body["assessment"])
        print("category:", body["category"])
        print("warnings:", body["warnings"])
        for k, v in body["sub_dimensions"].items():
            print(f"  {k}: {v['score']}/20 conf={v['confidence']} - {v['basis']}")
        assert 0 <= body["sentiment_score"] <= 100
        assert body["sub_dimensions"]["social_buzz"]["confidence"] == "low"
        assert any("X/Twitter" in w for w in body["warnings"])

        # 3. now simulate Twitter being available -> higher confidence path
        with patch("app.main.twitter.get_recent_mentions", new=AsyncMock(
            return_value={"available": True, "mention_count": 340, "total_engagement": 5200, "sample_size": 100}
        )):
            r2 = client.post("/sentiment", json={"token": "SOL"})
            body2 = r2.json()
            print("\n--- sentiment response (live twitter path) ---")
            print("social_buzz:", body2["sub_dimensions"]["social_buzz"])
            assert body2["sub_dimensions"]["social_buzz"]["confidence"] == "high"

        # 4. malformed / unresolvable token should still degrade gracefully via resolve fallback
        print("\n--- edge case: category hint override ---")
        r3 = client.post("/sentiment", json={"token": "SOL", "category_hint": "meme"})
        body3 = r3.json()
        assert body3["category"] == "meme"
        print("category override respected:", body3["category"])

        # 5. missing both token and contract_address/chain -> validation error
        print("\n--- edge case: no lookup path provided ---")
        r4 = client.post("/sentiment", json={})
        assert r4.status_code == 422, r4.text
        print("correctly rejected with 422:", r4.json()["detail"][0]["msg"])

    # 6. new-token path via GeckoTerminal (contract_address + chain)
    with patch("app.main.geckoterminal.get_token", new=AsyncMock(
        return_value=FIXTURE_GT_TOKEN
    )), patch("app.main.geckoterminal.get_token_pools", new=AsyncMock(
        return_value=FIXTURE_GT_POOLS
    )), patch("app.main.feargreed.get_fear_greed", new=AsyncMock(
        return_value=FIXTURE_FNG
    )), patch("app.main.twitter.get_recent_mentions", new=AsyncMock(
        return_value={"available": False, "reason": "no bearer token configured"}
    )):
        from app.main import app
        client = TestClient(app)

        print("\n--- new-token path (contract_address + chain) ---")
        r5 = client.post("/sentiment", json={
            "contract_address": "0x1234567890abcdef1234567890abcdef12345678",
            "chain": "base",
        })
        assert r5.status_code == 200, r5.text
        body5 = r5.json()
        print("token:", body5["token_name"], body5["token_ticker"])
        print("score:", body5["sentiment_score"], "assessment:", body5["assessment"])
        for k, v in body5["sub_dimensions"].items():
            print(f"  {k}: {v['score']}/20 conf={v['confidence']} - {v['basis']}")
        assert body5["token_name"] == "Brand New Coin"
        assert body5["token_ticker"] == "BNC"
        assert body5["sub_dimensions"]["news_tone"]["assessment"] == "Insufficient Data"
        assert body5["sub_dimensions"]["news_tone"]["score"] is None  # no fabricated number
        assert body5["sub_dimensions"]["community_health"]["confidence"] == "unavailable"
        assert body5["sub_dimensions"]["community_health"]["score"] is None
        assert body5["sub_dimensions"]["social_buzz"]["score"] > 0  # on-chain tx proxy kicked in
        assert body5["dimensions_scored"] < 5  # news_tone + community_health are unavailable
        # liquidity_health: fixture's gt_token has no total_reserve_in_usd/fdv_usd,
        # but the pool fixture does have 24h volume, so it should score off that
        # rather than falling back to Insufficient Data.
        assert "liquidity_health" in body5["sub_dimensions"]
        assert any("new/DEX-only path" in w for w in body5["warnings"])

        # 7. single free-form input that LOOKS like an address -> should
        # auto-route to the new-token path with chain auto-detection
        with patch("app.main.geckoterminal.detect_network", new=AsyncMock(
            return_value="base"
        )):
            print("\n--- auto-detect path (bare address in 'token' field) ---")
            r6 = client.post("/sentiment", json={
                "token": "0x1234567890abcdef1234567890abcdef12345678",
            })
            assert r6.status_code == 200, r6.text
            body6 = r6.json()
            print("token:", body6["token_name"], body6["token_ticker"])
            assert body6["token_name"] == "Brand New Coin"
            assert any("auto-detected as 'base'" in w for w in body6["warnings"])
            print("chain auto-detection OK")

        # 8. address given but no pool found anywhere -> clear 404, not a crash
        with patch("app.main.geckoterminal.detect_network", new=AsyncMock(
            return_value=None
        )):
            print("\n--- auto-detect path: undetectable chain ---")
            r7 = client.post("/sentiment", json={
                "token": "0x1234567890abcdef1234567890abcdef12345678",
            })
            assert r7.status_code == 404, r7.text
            print("correctly returned 404:", r7.json()["detail"])

        print()
        test_detect_network_against_real_response()

    test_no_fabricated_data_when_sparse()

    print("\nALL SMOKE TESTS PASSED")


def test_no_fabricated_data_when_sparse():
    """Regression test for the 'never fabricate data' requirement. A prior
    version of the scoring engine substituted invented placeholder numbers
    (a flat 8.0, a hardcoded '4' for a missing sentiment component, etc.)
    when real data was unavailable. This proves that no longer happens:
    every dimension backed by missing data must come back as score=None /
    confidence="unavailable", never a plausible-looking number."""
    with patch("app.main.coingecko.resolve_coin_id", new=AsyncMock(
        return_value={"id": "obscure-coin", "symbol": "OBS", "name": "Obscure Coin"}
    )), patch("app.main.coingecko.get_coin_data", new=AsyncMock(
        return_value=SPARSE_COIN
    )), patch("app.main.feargreed.get_fear_greed", new=AsyncMock(
        return_value=FIXTURE_FNG
    )), patch("app.main.twitter.get_recent_mentions", new=AsyncMock(
        return_value={"available": False, "reason": "no bearer token configured"}
    )):
        from app.main import app
        client = TestClient(app)

        print("\n--- sparse-data regression: no fabricated numbers ---")
        r = client.post("/sentiment", json={"token": "OBS"})
        assert r.status_code == 200, r.text
        body = r.json()
        for k, v in body["sub_dimensions"].items():
            print(f"  {k}: {v['score']} conf={v['confidence']} - {v['basis']}")

        # Social Buzz: no twitter, no follower count, no sentiment votes -> None
        assert body["sub_dimensions"]["social_buzz"]["score"] is None
        assert body["sub_dimensions"]["social_buzz"]["confidence"] == "unavailable"

        # News Tone: no public_interest, no sentiment votes, no price data -> None
        assert body["sub_dimensions"]["news_tone"]["score"] is None
        assert body["sub_dimensions"]["news_tone"]["confidence"] == "unavailable"

        # Liquidity Health: no liquidity_score, no volume/mcap -> None
        assert body["sub_dimensions"]["liquidity_health"]["score"] is None
        assert body["sub_dimensions"]["liquidity_health"]["confidence"] == "unavailable"

        # Community Health: no community_score AND no community_data
        # counts present at all -> None (not a fabricated 0 from
        # coalescing missing fields to zero).
        assert body["sub_dimensions"]["community_health"]["score"] is None
        assert body["sub_dimensions"]["community_health"]["confidence"] == "unavailable"

        # dimensions_scored must honestly reflect that most dimensions
        # couldn't be measured, not silently claim a full composite.
        # Only narrative_momentum survives, since category classification
        # is real data even when nothing else is.
        assert body["dimensions_scored"] == 1
        assert any("had real data available" in w for w in body["warnings"])

        print("dimensions_scored:", body["dimensions_scored"])
        print("sentiment_score:", body["sentiment_score"])
        print("no fabricated data confirmed")


if __name__ == "__main__":
    run()
