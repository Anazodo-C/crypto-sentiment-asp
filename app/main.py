from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from app import coingecko, feargreed, scoring, twitter, x402
from app.schemas import FearGreedContext, SentimentRequest, SentimentResponse

load_dotenv()
logger = logging.getLogger("crypto_sentiment_asp")

app = FastAPI(
    title="Crypto Sentiment ASP",
    description=(
        "A2MCP sentiment analysis service for OKX.AI. Given a token ticker/name, "
        "returns a 0-100 Sentiment Score across 5 sub-dimensions, per the "
        "crypto_sentiment.md methodology."
    ),
    version="1.0.0",
)

# x402 payment gate. If X402_ENABLED=true, this wraps POST /sentiment with
# OKX's PaymentMiddlewareASGI (see app/x402.py) - unpaid requests never
# reach the handler below at all. If disabled/unset, the route is free and
# runs exactly as written.
#
# This setup is wrapped defensively: a broken/misconfigured payment
# integration must NOT be able to take down every route in the app (health
# check included). If it fails, we log loudly and fall back to serving
# /sentiment for free rather than 500ing on every request.
x402_status = "disabled"
try:
    _x402_mw = x402.build_middleware()
    if _x402_mw:
        _middleware_class, _mw_kwargs = _x402_mw
        app.add_middleware(_middleware_class, **_mw_kwargs)
        x402_status = "enabled"
except Exception:
    logger.exception(
        "x402 payment middleware failed to initialize - falling back to a "
        "FREE /sentiment endpoint. Fix env vars / the okxweb3-app-x402 "
        "integration, then redeploy."
    )
    x402_status = "failed_fallback_free"


@app.get("/")
async def root():
    return {
        "name": "Crypto Sentiment ASP",
        "description": (
            "A2MCP sentiment analysis service for OKX.AI. POST a token ticker "
            "or name to /sentiment to get a 0-100 Sentiment Score."
        ),
        "endpoints": {
            "GET /health": "liveness check",
            "POST /sentiment": 'body: {"token": "SOL"}',
        },
        "x402_status": x402_status,
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "x402_status": x402_status,
    }


@app.post("/sentiment", response_model=SentimentResponse)
async def sentiment(req: SentimentRequest, request: Request):
    # Payment gating (if enabled) already happened in PaymentMiddlewareASGI
    # before this handler runs - an unpaid/unverified request never gets here.
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
        try:
            resolved = await coingecko.resolve_coin_id(client, req.token)
        except coingecko.CoinGeckoError as e:
            raise HTTPException(status_code=502, detail=f"token resolution failed: {e}")

        try:
            coin = await coingecko.get_coin_data(client, resolved["id"])
        except coingecko.CoinGeckoError as e:
            raise HTTPException(status_code=502, detail=f"coin data fetch failed: {e}")

        fng_data = await feargreed.get_fear_greed(client)
        if fng_data:
            fng = FearGreedContext(**fng_data, available=True)
        else:
            fng = FearGreedContext(available=False, note="Fear & Greed Index unavailable")
            warnings.append("Fear & Greed Index API unavailable at request time.")

        twitter_data = await twitter.get_recent_mentions(
            client, f"{resolved['name']} OR {resolved['symbol']}"
        )
        if not twitter_data or not twitter_data.get("available"):
            reason = (twitter_data or {}).get("reason", "no bearer token configured")
            warnings.append(f"Live X/Twitter data unavailable ({reason}); used CoinGecko proxy instead.")

    category = scoring.detect_category(coin.get("categories", []), req.category_hint)

    sub_scores = {
        "social_buzz": scoring.score_social_buzz(coin, twitter_data),
        "news_tone": scoring.score_news_tone(coin),
        "community_health": scoring.score_community_health(coin),
        "developer_activity": scoring.score_developer_activity(coin),
        "narrative_momentum": scoring.score_narrative_momentum(coin, category),
    }

    total = sum(s.score for s in sub_scores.values())
    assessment = scoring.composite_assessment(total)
    contrarian = scoring.contrarian_signals(total, fng)

    if coin.get("community_data", {}).get("reddit_subscribers") is None:
        warnings.append("No dedicated subreddit data found; Community Health score is confidence-discounted.")
    if not (coin.get("developer_data") or {}).get("commit_count_4_weeks"):
        warnings.append("No recent GitHub commit data found; Developer Activity score is confidence-discounted.")

    verdict = (
        f"{resolved['name']} ({resolved['symbol']}) scores {total:.1f}/100 ({assessment}). "
        f"Strongest signal: "
        f"{max(sub_scores.items(), key=lambda kv: kv[1].score)[0].replace('_', ' ')}. "
        f"Weakest signal: {min(sub_scores.items(), key=lambda kv: kv[1].score)[0].replace('_', ' ')}. "
        f"Treat proxy-based sub-dimensions (see confidence field) as directional, not precise."
    )

    markdown_report = scoring.build_markdown_report(
        resolved["name"], resolved["symbol"], total, sub_scores, fng, contrarian, verdict, warnings
    )

    return SentimentResponse(
        token_ticker=resolved["symbol"],
        token_name=resolved["name"],
        category=category,
        generated_at=datetime.now(timezone.utc).isoformat(),
        sentiment_score=round(total, 1),
        assessment=assessment,
        sub_dimensions=sub_scores,
        fear_greed=fng,
        contrarian_signals=contrarian,
        verdict=verdict,
        markdown_report=markdown_report,
        warnings=warnings,
    )
