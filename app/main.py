from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app import coingecko, feargreed, geckoterminal, scoring, twitter, x402
from app.frontend import INDEX_HTML
from app.schemas import FearGreedContext, SentimentRequest, SentimentResponse

load_dotenv()
logger = logging.getLogger("crypto_sentiment_asp")

# EVM addresses are unambiguous (0x + 40 hex). Solana mint addresses are
# base58, 32-44 chars - that overlaps in *length* with some tickers/names
# but not in *alphabet* (base58 excludes 0, O, I, l, and a plain ticker like
# "SOL" or "PEPE" is far shorter), so false positives in practice are rare.
_EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _looks_like_address(value: str) -> bool:
    value = value.strip()
    return bool(_EVM_ADDRESS_RE.match(value) or _SOLANA_ADDRESS_RE.match(value))

app = FastAPI(
    title="Sentimento",
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


_PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


@app.get("/", response_class=HTMLResponse)
async def root():
    """Human-facing report UI: paste a ticker or contract address, get a
    rendered sentiment report. Other agents should call POST /sentiment
    directly - see GET /info for a machine-readable description."""
    return INDEX_HTML


@app.get("/sentimento.png")
async def logo():
    """Serves the logo from /public. vercel.json rewrites every path to
    this function (no separate static-file pipeline), so the asset has to
    be served through a route rather than relying on Vercel's default
    /public handling."""
    return FileResponse(
        _PUBLIC_DIR / "sentimento-256.png",
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/info")
async def info():
    return {
        "name": "Sentimento",
        "description": (
            "A2MCP sentiment analysis service for OKX.AI. POST a token ticker, "
            "name, or contract address to /sentiment to get a 0-100 Sentiment Score."
        ),
        "endpoints": {
            "GET /health": "liveness check",
            "POST /sentiment": 'body: {"token": "SOL"} or {"token": "0x..."} or '
                                '{"contract_address": "0x...", "chain": "base"}',
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

    # News Tone: prefer real keyword-tone analysis of live tweet text over
    # the CoinGecko-derived proxy, when we have tweets to analyze.
    tweet_texts = (twitter_data or {}).get("texts") or []
    news_tone = (
        scoring.score_news_tone_from_tweets(tweet_texts)
        if tweet_texts
        else scoring.score_news_tone(coin)
    )

    category = scoring.detect_category(coin.get("categories", []), req.category_hint)
    sub_scores = {
        "social_buzz": scoring.score_social_buzz(coin, twitter_data),
        "news_tone": news_tone,
        "community_health": scoring.score_community_health(coin),
        "liquidity_health": scoring.score_liquidity_health(coin),
        "narrative_momentum": scoring.score_narrative_momentum(coin, category),
    }

    return resolved["name"], resolved["symbol"], category, sub_scores


async def _lookup_new_token(
    address: str, chain: str | None, req: SentimentRequest, client: httpx.AsyncClient, warnings: list[str]
):
    """Path B: contract address lookup via GeckoTerminal. Best for
    brand-new / DEX-only tokens that CoinGecko's main DB hasn't indexed yet
    - this ASP's primary use case. If `chain` isn't given, it's
    auto-detected by searching GeckoTerminal across all networks."""
    if chain:
        network = geckoterminal.resolve_network(chain)
    else:
        network = await geckoterminal.detect_network(client, address)
        if not network:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Couldn't auto-detect a chain for contract address '{address}' - "
                    "it may be too new to have an indexed pool yet, or you can pass "
                    "'chain' explicitly to skip auto-detection."
                ),
            )
        warnings.append(f"Chain auto-detected as '{network}' from the contract address.")

    try:
        gt_token = await geckoterminal.get_token(client, network, address)
    except geckoterminal.GeckoTerminalError as e:
        raise HTTPException(status_code=502, detail=f"GeckoTerminal lookup failed: {e}")

    gt_pools = await geckoterminal.get_token_pools(client, network, address)
    if not gt_pools:
        warnings.append("No trading pool data found - token may be extremely new, illiquid, or unindexed.")

    attrs = (gt_token.get("data") or {}).get("attributes") or {}
    name = attrs.get("name") or address
    symbol = (attrs.get("symbol") or "?").upper()

    twitter_data = await twitter.get_recent_mentions(client, f"{name} OR {symbol}")
    if not twitter_data or not twitter_data.get("available"):
        reason = (twitter_data or {}).get("reason", "no bearer token configured")
        warnings.append(
            f"Live X/Twitter data unavailable ({reason}); used on-chain transaction "
            "count as a Social Buzz proxy instead."
        )

    # Many tokens that are "new" to CoinGecko's curated database are
    # actually already linked to a CoinGecko coin id in GeckoTerminal's own
    # data (GeckoTerminal indexes far faster and often cross-references
    # CoinGecko once a listing exists). When that link is present, use it
    # to get real Community Health / Liquidity data instead of flatly
    # marking every DEX-path lookup as Insufficient Data - only genuinely
    # unlisted tokens should fall back to that.
    enriched_coin = None
    cg_id = attrs.get("coingecko_coin_id")
    if cg_id:
        try:
            enriched_coin = await coingecko.get_coin_data(client, cg_id)
        except coingecko.CoinGeckoError:
            enriched_coin = None

    category = req.category_hint or "other"
    insufficient_dims: list[str] = []

    tweet_texts = (twitter_data or {}).get("texts") or []
    if tweet_texts:
        news_tone = scoring.score_news_tone_from_tweets(tweet_texts)
    elif enriched_coin:
        news_tone = scoring.score_news_tone(enriched_coin)
    else:
        news_tone = scoring.insufficient_data_score(
            "News Tone", "no headline/news source covers unlisted tokens this new"
        )
        insufficient_dims.append("News Tone")

    if enriched_coin:
        community_health = scoring.score_community_health(enriched_coin)
        category = req.category_hint or scoring.detect_category(
            enriched_coin.get("categories", []), None
        )
    else:
        community_health = scoring.insufficient_data_score(
            "Community Health", "no CoinGecko community data exists yet for an unlisted token"
        )
        insufficient_dims.append("Community Health")

    if enriched_coin:
        liquidity_health = scoring.score_liquidity_health(enriched_coin)
    else:
        liquidity_health = scoring.score_liquidity_health_dex(gt_token, gt_pools)
        if liquidity_health.assessment == "Insufficient Data":
            insufficient_dims.append("Liquidity Health")

    sub_scores = {
        "social_buzz": scoring.score_social_buzz_dex(gt_token, gt_pools, twitter_data),
        "news_tone": news_tone,
        "community_health": community_health,
        "liquidity_health": liquidity_health,
        "narrative_momentum": scoring.score_narrative_momentum_dex(gt_token, gt_pools, category),
    }

    if enriched_coin:
        warnings.append(
            f"Token is also listed on CoinGecko (id='{cg_id}') - enriched Community Health"
            + (" and News Tone" if not tweet_texts else "")
            + " with real CoinGecko data instead of marking them Insufficient Data."
        )
    if insufficient_dims:
        warnings.append(
            "This token was looked up by contract address (new/DEX-only path): "
            + ", ".join(insufficient_dims)
            + " have no real data source for a token this new/unlisted and "
            "are marked Insufficient Data rather than estimated."
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

        if req.contract_address:
            # Caller already knows it's a contract address (chain optional -
            # auto-detected below if not given).
            name, symbol, category, sub_scores = await _lookup_new_token(
                req.contract_address, req.chain, req, client, warnings
            )
        elif req.token and _looks_like_address(req.token):
            # Single free-form input that happens to look like an address -
            # this is the path the frontend's one input box uses.
            name, symbol, category, sub_scores = await _lookup_new_token(
                req.token, req.chain, req, client, warnings
            )
        else:
            name, symbol, category, sub_scores = await _lookup_established_coin(req, client, warnings)

    total = sum(s.score for s in sub_scores.values())
    assessment = scoring.composite_assessment(total)
    contrarian = scoring.contrarian_signals(total, fng)

    strongest_signal = max(sub_scores.items(), key=lambda kv: kv[1].score)[0]
    weakest_signal = min(sub_scores.items(), key=lambda kv: kv[1].score)[0]

    # Name/score/assessment are already shown in the verdict card header -
    # restating them here just duplicated the same three facts twice on
    # screen. This is now just the caveat; strongest/weakest signal are
    # structured fields the frontend highlights directly instead.
    verdict = (
        "Treat proxy-based and Insufficient Data sub-dimensions (see confidence field) "
        "as directional at best, not precise."
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
        strongest_signal=strongest_signal,
        weakest_signal=weakest_signal,
        verdict=verdict,
        warnings=warnings,
    )
