from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from app import coingecko, feargreed, geckoterminal, scoring, twitter, x402
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
x402_error: str | None = None
try:
    _x402_mw = x402.build_middleware()
    if _x402_mw:
        _middleware_class, _mw_kwargs = _x402_mw
        app.add_middleware(_middleware_class, **_mw_kwargs)
        x402_status = "enabled"
except Exception as e:
    # Surface the real error via the API itself (repr + type), not just
    # logs - Vercel's log tab has repeatedly been hard to get a straight
    # answer from mid-build, and this needs to be debuggable without it.
    x402_error = f"{type(e).__name__}: {e}"
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
        "x402_error": x402_error,
    }


async def _lookup_established_coin(req: SentimentRequest, client: httpx.AsyncClient, warnings: list[str]):
    """Path A: ticker/name lookup via CoinGecko's main coin database. Best
    for established, already-listed coins."""
    try:
        resolved = await coingecko.resolve_coin_id(client, req.token)
    except coingecko.CoinGeckoError as e:
        raise HTTPException(status_code=502, detail=f"token resolution failed: {e}")

    try:
        coin = await coingecko.get_coin_data(client, resolved["id"])
    except coingecko.CoinGeckoError as e:
        raise HTTPException(status_code=502, detail=f"coin data fetch failed: {e}")

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

    if coin.get("community_data", {}).get("reddit_subscribers") is None:
        warnings.append("No dedicated subreddit data found; Community Health score is confidence-discounted.")
    if not (coin.get("developer_data") or {}).get("commit_count_4_weeks"):
        warnings.append("No recent GitHub commit data found; Developer Activity score is confidence-discounted.")

    return resolved["name"], resolved["symbol"], category, sub_scores


async def _lookup_new_token(req: SentimentRequest, client: httpx.AsyncClient, warnings: list[str]):
    """Path B: contract address + chain lookup via GeckoTerminal. Best for
    brand-new / DEX-only tokens that CoinGecko's main DB hasn't indexed yet
    - this ASP's primary use case."""
    network = geckoterminal.resolve_network(req.chain)
    try:
        gt_token = await geckoterminal.get_token(client, network, req.contract_address)
    except geckoterminal.GeckoTerminalError as e:
        raise HTTPException(status_code=502, detail=f"GeckoTerminal lookup failed: {e}")

    gt_pools = await geckoterminal.get_token_pools(client, network, req.contract_address)
    if not gt_pools:
        warnings.append("No trading pool data found - token may be extremely new, illiquid, or unindexed.")

    attrs = (gt_token.get("data") or {}).get("attributes") or {}
    name = attrs.get("name") or req.contract_address
    symbol = (attrs.get("symbol") or "?").upper()

    twitter_data = await twitter.get_recent_mentions(client, f"{name} OR {symbol}")
    if not twitter_data or not twitter_data.get("available"):
        reason = (twitter_data or {}).get("reason", "no bearer token configured")
        warnings.append(
            f"Live X/Twitter data unavailable ({reason}); used on-chain transaction "
            "count as a Social Buzz proxy instead."
        )

    category = req.category_hint or "other"
    sub_scores = {
        "social_buzz": scoring.score_social_buzz_dex(gt_token, gt_pools, twitter_data),
        "news_tone": scoring.insufficient_data_score(
            "News Tone", "no headline/news source covers unlisted tokens this new"
        ),
        "community_health": scoring.insufficient_data_score(
            "Community Health", "no CoinGecko community data exists yet for an unlisted token"
        ),
        "developer_activity": scoring.insufficient_data_score(
            "Developer Activity", "no linked GitHub repo - common for brand-new/anonymous-dev tokens"
        ),
        "narrative_momentum": scoring.score_narrative_momentum_dex(gt_token, gt_pools, category),
    }

    warnings.append(
        "This token was looked up by contract address (new/DEX-only path): News Tone, "
        "Community Health, and Developer Activity have no real data source for a token "
        "this new and are marked Insufficient Data rather than estimated."
    )

    return name, symbol, category, sub_scores


@app.post("/sentiment", response_model=SentimentResponse)
async def sentiment(req: SentimentRequest, request: Request):
    # Payment gating (if enabled) already happened in PaymentMiddlewareASGI
    # before this handler runs - an unpaid/unverified request never gets here.
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
        fng_data = await feargreed.get_fear_greed(client)
        if fng_data:
            fng = FearGreedContext(**fng_data, available=True)
        else:
            fng = FearGreedContext(available=False, note="Fear & Greed Index unavailable")
            warnings.append("Fear & Greed Index API unavailable at request time.")

        if req.contract_address and req.chain:
            name, symbol, category, sub_scores = await _lookup_new_token(req, client, warnings)
        else:
            name, symbol, category, sub_scores = await _lookup_established_coin(req, client, warnings)

    total = sum(s.score for s in sub_scores.values())
    assessment = scoring.composite_assessment(total)
    contrarian = scoring.contrarian_signals(total, fng)

    verdict = (
        f"{name} ({symbol}) scores {total:.1f}/100 ({assessment}). "
        f"Strongest signal: "
        f"{max(sub_scores.items(), key=lambda kv: kv[1].score)[0].replace('_', ' ')}. "
        f"Weakest signal: {min(sub_scores.items(), key=lambda kv: kv[1].score)[0].replace('_', ' ')}. "
        f"Treat proxy-based and Insufficient Data sub-dimensions (see confidence field) "
        f"as directional at best, not precise."
    )

    return SentimentResponse(
        token_ticker=symbol,
        token_name=name,
        category=category,
        generated_at=datetime.now(timezone.utc).isoformat(),
        sentiment_score=round(total, 1),
        assessment=assessment,
        sub_dimensions=sub_scores,
        fear_greed=fng,
        contrarian_signals=contrarian,
        verdict=verdict,
        warnings=warnings,
    )
