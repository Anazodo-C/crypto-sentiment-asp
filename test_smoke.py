"""Smoke test using fixture data, since this sandbox can't reach
CoinGecko/alternative.me/Twitter directly (network allowlist blocks them).
Mocks the three external clients and drives the real FastAPI endpoint +
scoring engine end-to-end so we can verify: request/response shape, math,
category weighting, and graceful degradation when Twitter is unavailable.
"""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

FIXTURE_COIN = {
    "id": "solana",
    "symbol": "sol",
    "name": "Solana",
    "categories": ["Smart Contract Platform", "Layer 1 (L1)"],
    "community_score": 62.4,
    "developer_score": 81.2,
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
    "developer_data": {
        "commit_count_4_weeks": 145,
        "stars": 12000,
        "forks": 4200,
    },
    "market_data": {
        "price_change_percentage_30d_in_currency": {"usd": 18.4},
    },
}

FIXTURE_FNG = {"value": 62, "label": "Greed", "trend_7d": "rising"}

FIXTURE_GT_TOKEN = {
    "data": {
        "attributes": {
            "name": "Brand New Coin",
            "symbol": "bnc",
            "address": "0xNEWTOKEN0000000000000000000000000000001",
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
            "contract_address": "0xNEWTOKEN0000000000000000000000000000001",
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
        assert body5["sub_dimensions"]["community_health"]["confidence"] == "low"
        assert body5["sub_dimensions"]["social_buzz"]["score"] > 0  # on-chain tx proxy kicked in
        assert any("new/DEX-only path" in w for w in body5["warnings"])

        print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    run()
