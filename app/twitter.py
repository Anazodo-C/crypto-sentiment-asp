"""twitterapi.io client (third-party Twitter data provider).

NOT the official api.twitter.com v2 API. twitterapi.io is a paid,
per-call wrapper around Twitter data: https://docs.twitterapi.io - auth
is a single `X-API-Key` header (no OAuth/Bearer scheme), and there's no
tiered "free vs paid search access" restriction like official Twitter -
every valid key can call advanced_search, billed per tweet returned
(~$0.15/1k tweets, minimum $0.00015/request).

Every call here is still wrapped so a failure just marks Twitter data as
unavailable and the scoring engine falls back to CoinGecko-derived
social proxies instead of breaking the endpoint.
"""
from __future__ import annotations

import os
import time

import httpx

SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"


def _get_api_key() -> str | None:
    # Prefer the correctly-named var; fall back to the old name in case
    # it's already set as TWITTER_BEARER_TOKEN in an existing deploy.
    return os.getenv("TWITTERAPI_IO_KEY") or os.getenv("TWITTER_BEARER_TOKEN")


async def get_recent_mentions(client: httpx.AsyncClient, query: str) -> dict | None:
    api_key = _get_api_key()
    if not api_key:
        return None

    since_ts = int(time.time()) - 24 * 3600
    search_query = f"({query}) since_time:{since_ts}"

    try:
        resp = await client.get(
            SEARCH_URL,
            headers={"X-API-Key": api_key},
            params={"query": search_query, "queryType": "Latest", "cursor": ""},
        )
        if resp.status_code == 401:
            return {"available": False, "reason": "unauthorized - check TWITTERAPI_IO_KEY"}
        if resp.status_code == 403:
            return {"available": False, "reason": "forbidden - check account status/credits"}
        if resp.status_code == 429:
            return {"available": False, "reason": "rate limited"}
        if resp.status_code != 200:
            return {
                "available": False,
                "reason": f"unexpected status {resp.status_code}: {resp.text[:200]}",
            }

        data = resp.json()
        tweets = data.get("tweets", [])
        total_engagement = sum(
            (t.get("likeCount") or 0)
            + (t.get("retweetCount") or 0)
            + (t.get("quoteCount") or 0)
            for t in tweets
        )
        # Raw tweet text, so scoring.py can run keyword-based tone analysis
        # for News Tone instead of leaving it as a flat CoinGecko proxy /
        # Insufficient Data - this is real live text we're already paying
        # to fetch for Social Buzz, just not previously used for anything
        # beyond counting.
        texts = [t.get("text") for t in tweets if t.get("text")]
        return {
            "available": True,
            "mention_count": len(tweets),
            "total_engagement": total_engagement,
            "sample_size": len(tweets),
            "has_more": data.get("has_next_page", False),
            "texts": texts,
        }
    except Exception as e:
        return {"available": False, "reason": f"error: {e}"}
