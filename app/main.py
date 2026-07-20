from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import ValidationError

from app import coingecko, feargreed, geckoterminal, scoring, twitter, x402
from app.frontend import INDEX_HTML
from app.schemas import FearGreedContext, SentimentRequest, SentimentResponse

load_dotenv()
# Explicit level, not left to the ASGI host's default: without this, INFO-level
# logs (including the x402 verify/settle outcome logging in app/x402.py) can be
# silently dropped depending on how the runtime configures the root logger,
# which is exactly the kind of gap that left the 2026-07-19 settlement failure
# undiagnosable from Vercel's log stream.
logging.basicConfig(level=logging.INFO)
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
        # Registered AFTER the OKX middleware so it wraps OUTSIDE it -
        # Starlette's add_middleware() makes the most-recently-added
        # middleware outermost (verified empirically: the reverse
        # assumption was tried first and silently failed to see OKX's
        # response at all). It needs to be outside to see and rewrite the
        # 402 response OKX's inner middleware produces.
        app.add_middleware(_middleware_class, **_mw_kwargs)
        app.add_middleware(x402.PaymentRequiredBodyMiddleware)
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


# A request to a payment-gated route with a verified payment attached but a
# missing/invalid body (e.g. no `token`) would otherwise fall through to
# FastAPI's default RequestValidationError handling and return a bare 422 -
# see app/x402.py's malformed_request_response() docstring for why that
# reads as an x402 standards violation to a generic prober. Scoped to only
# the payment-gated paths; every other route keeps FastAPI's normal 422.
_X402_GATED_PATHS = {"/", "/sentiment"}


@app.exception_handler(RequestValidationError)
async def _malformed_body_on_gated_route(request: Request, exc: RequestValidationError):
    if x402_status == "enabled" and request.url.path in _X402_GATED_PATHS:
        return x402.malformed_request_response(str(request.url))
    return await request_validation_exception_handler(request, exc)


_PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


@app.get("/", response_class=HTMLResponse)
async def root():
    """Human-facing report UI: paste a ticker or contract address, get a
    rendered sentiment report. Other agents should call POST /sentiment
    directly - see GET /info for a machine-readable description."""
    return INDEX_HTML


@app.post("/", response_model=SentimentResponse)
async def root_post(req: SentimentRequest):
    # Alias of POST /sentiment at the bare registered domain root. Exists
    # because OKX's ASP reviewer probes the literal registered endpoint URL
    # for x402 compliance, and - confirmed live via curl - that probe hits
    # https://crypto-sentiment-asp.vercel.app/ directly rather than
    # .../sentiment, so without this alias it 405s (no POST / handler
    # existed at all) instead of returning the 402 challenge. Gated
    # identically to POST /sentiment in app/x402.py's routes.
    return await _sentiment_impl(req)


@app.get("/sentimento.png")
async def logo():
    """Serves the logo from /public for callers that want a real URL
    instead of the data: URI embedded in the page itself. NOTE: this may
    not actually work on Vercel (see app/assets.py docstring for why) -
    the page's own <img> tag does not depend on this route at all."""
    return FileResponse(
        _PUBLIC_DIR / "sentimento-nobg.png",
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

    # get_coin_data and the Twitter lookup both depend only on `resolved`,
    # not on each other - measured live (2026-07-20) this pair alone added
    # a full extra sequential network hop to every request when chained.
    async def _get_coin():
        try:
            return await coingecko.get_coin_data(client, resolved["id"])
        except coingecko.CoinGeckoError as e:
            raise HTTPException(status_code=502, detail=f"coin data fetch failed: {e}")

    coin, twitter_data = await asyncio.gather(
        _get_coin(),
        twitter.get_recent_mentions(client, f"{resolved['name']} OR {resolved['symbol']}"),
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

    attrs = (gt_token.get("data") or {}).get("attributes") or {}
    name = attrs.get("name") or address
    symbol = (attrs.get("symbol") or "?").upper()
    cg_id = attrs.get("coingecko_coin_id")

    # get_token_pools, the Twitter lookup, and the CoinGecko enrich call all
    # depend only on gt_token's attrs (network/address/name/symbol/cg_id),
    # not on each other - previously 3 sequential hops, now one batch.
    async def _get_enriched():
        # Many tokens that are "new" to CoinGecko's curated database are
        # actually already linked to a CoinGecko coin id in GeckoTerminal's
        # own data (GeckoTerminal indexes far faster and often
        # cross-references CoinGecko once a listing exists). When that link
        # is present, use it to get real Community Health / Liquidity data
        # instead of flatly marking every DEX-path lookup as Insufficient
        # Data - only genuinely unlisted tokens should fall back to that.
        if not cg_id:
            return None
        try:
            return await coingecko.get_coin_data(client, cg_id)
        except coingecko.CoinGeckoError:
            return None

    gt_pools, twitter_data, enriched_coin = await asyncio.gather(
        geckoterminal.get_token_pools(client, network, address),
        twitter.get_recent_mentions(client, f"{name} OR {symbol}"),
        _get_enriched(),
    )

    if not gt_pools:
        warnings.append("No trading pool data found - token may be extremely new, illiquid, or unindexed.")

    if not twitter_data or not twitter_data.get("available"):
        reason = (twitter_data or {}).get("reason", "no bearer token configured")
        warnings.append(
            f"Live X/Twitter data unavailable ({reason}); used on-chain transaction "
            "count as a Social Buzz proxy instead."
        )

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
    return await _sentiment_impl(req)


@app.get("/sentiment", response_model=SentimentResponse)
async def sentiment_get(
    request: Request,
    token: str | None = None,
    contract_address: str | None = None,
    chain: str | None = None,
    category_hint: str | None = None,
):
    # GET alias for the same resource, query-param driven. Exists because
    # some 402/x402 validators (and the OKX A2MCP prober per its docs)
    # default to probing with GET - without this, a POST-only route 405s
    # on that probe instead of returning the payment challenge, which
    # reads as "no valid 402" and fails x402 standard validation even
    # though the real POST route is fully compliant. Registered as its own
    # "GET /sentiment" entry in app/x402.py's payment middleware routes so
    # it's gated identically to POST.
    try:
        req = SentimentRequest(
            token=token, contract_address=contract_address, chain=chain, category_hint=category_hint
        )
    except ValidationError as e:
        # Query params (not body), so this raises pydantic's ValidationError
        # directly rather than FastAPI's RequestValidationError - the global
        # handler above doesn't see it, so the same 402-instead-of-422 logic
        # is applied here explicitly. See app/x402.py's malformed_request_response().
        if x402_status == "enabled":
            return x402.malformed_request_response(str(request.url))
        raise HTTPException(status_code=422, detail=e.errors())
    return await _sentiment_impl(req)


async def _sentiment_impl(req: SentimentRequest):
    warnings: list[str] = []

    # Per-call timeout kept tight (3s, down from 6s): the lookup paths are
    # now internally parallelized wherever calls don't depend on each other
    # (see _lookup_established_coin / _lookup_new_token), so the remaining
    # critical path is normally only 1-2 sequential hops - a single call
    # eating 6s of an overall ~5s target budget is no longer tolerable.
    #
    # OVERALL_DEADLINE is a hard ceiling on top of that: even with
    # parallelization, a genuinely slow upstream on the critical-path hop
    # could still blow the budget. Racing against a fixed deadline turns
    # that into a clean, fast 504 instead of an unpredictable multi-second
    # hang - measured live (2026-07-20) this whole phase (verify + lookup +
    # settle) needs to fit well under 5s total end to end.
    OVERALL_DEADLINE = 4.0
    async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
        if req.contract_address:
            # Caller already knows it's a contract address (chain optional -
            # auto-detected below if not given).
            lookup = _lookup_new_token(req.contract_address, req.chain, req, client, warnings)
        elif req.token and _looks_like_address(req.token):
            # Single free-form input that happens to look like an address -
            # this is the path the frontend's one input box uses.
            lookup = _lookup_new_token(req.token, req.chain, req, client, warnings)
        else:
            lookup = _lookup_established_coin(req, client, warnings)

        # Fear & Greed has no dependency on the token lookup, so it runs
        # concurrently with it instead of adding its latency in front.
        try:
            (name, symbol, category, sub_scores), fng_data = await asyncio.wait_for(
                asyncio.gather(lookup, feargreed.get_fear_greed(client)),
                timeout=OVERALL_DEADLINE,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Sentiment computation exceeded the {OVERALL_DEADLINE}s response budget "
                    "- an upstream data source was too slow. Please retry."
                ),
            )
        if fng_data:
            fng = FearGreedContext(**fng_data, available=True)
        else:
            fng = FearGreedContext(available=False, note="Fear & Greed Index unavailable")
            warnings.append("Fear & Greed Index API unavailable at request time.")

    # Composite score is computed ONLY from dimensions that returned a
    # real, non-None score. A dimension with no real data (score=None,
    # confidence="unavailable") contributes nothing - it is never treated
    # as 0 (which would imply "confirmed bad") or silently dropped without
    # comment. sentiment_score is the percentage of the achievable total
    # among only the scored dimensions, so a token with 4/5 real
    # dimensions is compared on the same 0-100 scale as one with 5/5,
    # rather than being unfairly dragged down by a dimension nobody could
    # actually measure.
    scored = {k: v for k, v in sub_scores.items() if v.score is not None}
    dimensions_scored = len(scored)

    if dimensions_scored == 0:
        total_pct = 0.0
        strongest_signal = None
        weakest_signal = None
        warnings.append(
            "No real data was available for ANY sub-dimension - this score is not "
            "meaningful and should not be used for any decision."
        )
    else:
        total_raw = sum(v.score for v in scored.values())
        total_pct = total_raw / (dimensions_scored * 20) * 100
        strongest_signal = max(scored.items(), key=lambda kv: kv[1].score)[0]
        weakest_signal = min(scored.items(), key=lambda kv: kv[1].score)[0]
        if dimensions_scored < len(sub_scores):
            missing = [k.replace("_", " ") for k in sub_scores if k not in scored]
            warnings.append(
                f"Only {dimensions_scored}/{len(sub_scores)} dimensions had real data "
                f"available ({', '.join(missing)} unavailable) - sentiment_score reflects "
                "only what could actually be measured, not a full 5-dimension composite."
            )

    assessment = scoring.composite_assessment(total_pct)
    contrarian = scoring.contrarian_signals(total_pct, fng)

    # Name/score/assessment are already shown in the verdict card header -
    # restating them here just duplicated the same three facts twice on
    # screen. This is now just the caveat; strongest/weakest signal are
    # structured fields the frontend highlights directly instead.
    verdict = (
        "Treat proxy-based sub-dimensions (see confidence field) as directional at "
        "best, not precise. Dimensions marked Insufficient Data were excluded from "
        "this score entirely rather than estimated."
    )

    return SentimentResponse(
        token_ticker=symbol,
        token_name=name,
        category=category,
        generated_at=datetime.now(timezone.utc).isoformat(),
        sentiment_score=round(total_pct, 1),
        assessment=assessment,
        sub_dimensions=sub_scores,
        fear_greed=fng,
        contrarian_signals=contrarian,
        strongest_signal=strongest_signal,
        weakest_signal=weakest_signal,
        dimensions_scored=dimensions_scored,
        verdict=verdict,
        warnings=warnings,
    )
