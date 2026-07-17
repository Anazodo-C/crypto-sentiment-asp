"""Scoring engine implementing the crypto_sentiment.md 5x20 methodology
against real, programmatically-available data sources.

Honesty note (per hackathon template Stage 4 guidance - don't overclaim):
the original crypto_sentiment.md framework assumes a human/LLM agent doing
live WebSearch across CT, Reddit, Discord, Telegram, GitHub, and news for
every call. That's too slow/fragile for a metered, real-time A2MCP
endpoint. This engine instead derives each sub-dimension from the best
available *structured* data source, and is explicit in `basis` and
`confidence` fields about what's a direct measurement vs. a proxy:

  Social Buzz        -> Twitter API (if available) else CoinGecko
                         community_data + sentiment votes (proxy)
  News Tone           -> keyword bullish/bearish tone analysis of live X
                         posts (if available) else CoinGecko
                         public_interest_score + sentiment votes + 30d
                         price momentum (proxy - no live headline source
                         wired up)
  Community Health    -> CoinGecko community_score if populated, else a
                         log-scaled proxy from raw community_data counts
                         (Reddit/Telegram/Twitter followers) - the
                         *_score fields are frequently null even for
                         established coins, so falling back to a flat
                         default there was producing near-identical
                         scores across unrelated tokens. Using the raw
                         counts directly keeps scores differentiated.
  Liquidity Health    -> CoinGecko liquidity_score, or 24h volume /
                         market cap ratio (established coins); pool
                         reserve depth + volume turnover (new/DEX
                         tokens via GeckoTerminal). Replaces Developer
                         Activity - GitHub activity is a weak/irrelevant
                         sentiment signal for the majority of today's
                         tokens (anonymous devs, no repo, memecoins),
                         and this dimension is computable for both
                         established and brand-new tokens alike, unlike
                         dev activity which barely applied to DEX-only
                         tokens at all.
  Narrative Momentum  -> CoinGecko categories + market_cap_rank +
                         price_change_percentage_30d (proxy)
"""
from __future__ import annotations

from typing import Optional

from app.schemas import ContrarianSignal, FearGreedContext, SubDimensionScore

CATEGORY_WEIGHTS = {
    "meme": {"social": 0.30, "news": 0.20, "community": 0.20, "dev": 0.10, "narrative": 0.20},
    "layer1": {"social": 0.15, "news": 0.20, "community": 0.20, "dev": 0.25, "narrative": 0.20},
    "layer2": {"social": 0.15, "news": 0.20, "community": 0.20, "dev": 0.25, "narrative": 0.20},
    "defi": {"social": 0.15, "news": 0.20, "community": 0.25, "dev": 0.25, "narrative": 0.15},
    "ai-depin": {"social": 0.20, "news": 0.15, "community": 0.15, "dev": 0.25, "narrative": 0.25},
    "other": {"social": 0.20, "news": 0.20, "community": 0.20, "dev": 0.20, "narrative": 0.20},
}
# NOTE: these top-level category weights are used only to pick which
# category's *classification thresholds* apply, matching the framework's
# "CATEGORY-SPECIFIC WEIGHTING" table. Each sub-dimension is still scored
# 0-20 independently, per the framework's fixed 5x20 structure.


def _clip(v: float, lo: float = 0, hi: float = 20) -> float:
    return max(lo, min(hi, v))


def detect_category(categories: list[str], hint: Optional[str]) -> str:
    if hint:
        return hint
    cats = [c.lower() for c in (categories or [])]
    if any("meme" in c for c in cats):
        return "meme"
    if any("layer 1" in c or "smart contract platform" in c for c in cats):
        return "layer1"
    if any("layer 2" in c for c in cats):
        return "layer2"
    if any("defi" in c or "decentralized finance" in c for c in cats):
        return "defi"
    if any("ai" in c or "depin" in c for c in cats):
        return "ai-depin"
    return "other"


def score_social_buzz(coin: dict, twitter: Optional[dict]) -> SubDimensionScore:
    sentiment_up = coin.get("sentiment_votes_up_percentage")
    community = coin.get("community_data", {}) or {}
    twitter_followers = community.get("twitter_followers") or 0
    reddit_subs = community.get("reddit_subscribers") or 0

    sources = ["coingecko"]
    confidence = "medium"

    if twitter and twitter.get("available"):
        mentions = twitter.get("mention_count", 0)
        engagement = twitter.get("total_engagement", 0)
        # crude volume+engagement heuristic, scaled to 0-20
        raw = min(20, (mentions / 10) + (engagement / 500))
        basis = f"{mentions} mentions / 24h, {engagement} total likes+RTs (live X API)"
        sources.append("twitter")
        confidence = "high"
        score = _clip(raw)
    else:
        # Proxy: follower count (log-scaled) + sentiment vote skew
        import math

        follower_component = min(12, math.log10(max(twitter_followers, 1)) * 2.2)
        sentiment_component = (sentiment_up / 100 * 8) if sentiment_up is not None else 4
        score = _clip(follower_component + sentiment_component)
        reason = twitter.get("reason") if twitter else "no TWITTER_BEARER_TOKEN configured"
        basis = (
            f"proxy from {twitter_followers:,} X followers + "
            f"{sentiment_up if sentiment_up is not None else 'n/a'}% positive sentiment votes "
            f"(live X search unavailable: {reason})"
        )
        confidence = "low"

    label = (
        "Viral" if score >= 17 else "High" if score >= 13 else "Moderate" if score >= 9
        else "Low" if score >= 5 else "Dead"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=label, confidence=confidence, basis=basis,
        data_sources=sources,
    )


_BULLISH_WORDS = [
    "moon", "bullish", "pump", "breakout", "rally", "surge", "undervalued",
    "accumulate", "buy the dip", "adoption", "partnership", "listing",
    "ath", "all time high", "gem", "🚀",
]
_BEARISH_WORDS = [
    "dump", "rug", "scam", "bearish", "crash", "sell off", "exploit",
    "hack", "delist", "insolvent", "ponzi", "rekt", "capitulation", "fud",
]


def score_news_tone_from_tweets(texts: list[str]) -> SubDimensionScore:
    """Real News Tone signal from live X post text we already fetch for
    Social Buzz - a simple bullish/bearish keyword tally. This isn't
    traditional news-headline sentiment (no headline API is wired up),
    but it's an actual measurement of live external text rather than a
    numeric fallback, and basis/data_sources say exactly what it is.
    """
    if not texts:
        return insufficient_data_score("News Tone", "no tweet text available to analyze")

    lowered = [t.lower() for t in texts if t]
    bullish_hits = sum(1 for t in lowered for w in _BULLISH_WORDS if w in t)
    bearish_hits = sum(1 for t in lowered for w in _BEARISH_WORDS if w in t)
    total_hits = bullish_hits + bearish_hits

    if total_hits == 0:
        score = 10.0
        confidence = "medium"
    else:
        tone_ratio = (bullish_hits - bearish_hits) / total_hits  # -1..1
        score = _clip(10 + tone_ratio * 10)
        confidence = "medium"

    label = (
        "Very Positive" if score >= 17 else "Positive" if score >= 13 else "Neutral"
        if score >= 9 else "Negative" if score >= 5 else "Very Negative"
    )
    basis = (
        f"keyword tone analysis of {len(texts)} live X posts: "
        f"{bullish_hits} bullish-keyword hits, {bearish_hits} bearish-keyword hits "
        f"(live social text, not traditional news headlines - no headline API wired up)"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=label, confidence=confidence, basis=basis,
        data_sources=["twitter"],
    )


def score_news_tone(coin: dict) -> SubDimensionScore:
    public_interest = coin.get("public_interest_score") or 0
    sentiment_up = coin.get("sentiment_votes_up_percentage")
    price_change_30d = (coin.get("market_data", {}) or {}).get(
        "price_change_percentage_30d_in_currency", {}
    ).get("usd")

    interest_component = min(10, public_interest * 100)  # public_interest_score is tiny (0-1ish)
    sentiment_component = (sentiment_up / 100 * 6) if sentiment_up is not None else 3
    momentum_component = 4
    if price_change_30d is not None:
        momentum_component = _clip(4 + price_change_30d / 20, 0, 4)  # +/-20% -> +/-1

    score = _clip(interest_component + sentiment_component + momentum_component)
    label = (
        "Very Positive" if score >= 17 else "Positive" if score >= 13 else "Neutral"
        if score >= 9 else "Negative" if score >= 5 else "Very Negative"
    )
    basis = (
        f"proxy from CoinGecko public_interest_score={public_interest}, "
        f"sentiment_up={sentiment_up}%, 30d price change={price_change_30d}% "
        f"(no live headline/news API wired up)"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=label, confidence="low", basis=basis,
        data_sources=["coingecko"],
    )


def score_community_health(coin: dict) -> SubDimensionScore:
    community_score = coin.get("community_score") or 0
    community = coin.get("community_data", {}) or {}
    reddit_subs = community.get("reddit_subscribers") or 0
    telegram = community.get("telegram_channel_user_count") or 0
    twitter_followers = community.get("twitter_followers") or 0

    if community_score:
        # CoinGecko's own derived score is populated - trust it directly.
        score = _clip(community_score / 100 * 20)
        confidence = "high"
        basis = (
            f"CoinGecko community_score={community_score}, "
            f"reddit_subscribers={reddit_subs}, telegram_users={telegram}"
        )
    else:
        # community_score is frequently null across CoinGecko's API today,
        # even for well-known coins. Falling back to a flat default here
        # (the old behavior) produced identical scores across unrelated
        # tokens. Instead, log-scale the raw counts directly - these
        # remain populated far more often and actually vary per token.
        import math

        raw = (
            math.log10(max(reddit_subs, 1)) * 3.2
            + math.log10(max(telegram, 1)) * 2.2
            + math.log10(max(twitter_followers, 1)) * 1.6
        )
        score = _clip(raw)
        confidence = "medium" if (reddit_subs or telegram or twitter_followers) else "low"
        basis = (
            f"community_score unavailable; proxy from reddit_subscribers={reddit_subs}, "
            f"telegram_users={telegram}, twitter_followers={twitter_followers} (log-scaled)"
        )

    label = (
        "Thriving" if score >= 17 else "Healthy" if score >= 13 else "Moderate"
        if score >= 9 else "Declining" if score >= 5 else "Dead"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=label, confidence=confidence, basis=basis,
        data_sources=["coingecko"],
    )


def score_liquidity_health(coin: dict) -> SubDimensionScore:
    """Replaces Developer Activity. GitHub commit counts are a weak fit for
    "sentiment" on most of today's tokens (anonymous devs, no repo,
    memecoins), and this dimension only worked for the tiny subset of
    tokens with a linked GitHub repo anyway. Liquidity depth relative to
    market cap, plus trading volume turnover, is a real signal of market
    health/confidence and - unlike dev activity - is computable for
    brand-new DEX tokens too (see score_liquidity_health_dex below).
    """
    liquidity_score = coin.get("liquidity_score")
    market_data = coin.get("market_data", {}) or {}
    volume = (market_data.get("total_volume") or {}).get("usd")
    mcap = (market_data.get("market_cap") or {}).get("usd")

    if liquidity_score:
        score = _clip(liquidity_score / 100 * 20)
        confidence = "high"
        basis = f"CoinGecko liquidity_score={liquidity_score}"
    elif volume and mcap:
        ratio = volume / mcap
        score = _clip(ratio * 100)  # ~20% daily turnover saturates the score
        confidence = "medium"
        basis = (
            f"proxy from 24h volume/market cap ratio={ratio:.3f} "
            f"(${volume:,.0f} volume / ${mcap:,.0f} mcap)"
        )
    else:
        score = 8.0
        confidence = "low"
        basis = "no liquidity_score or volume/market cap data available"

    label = (
        "Deep" if score >= 17 else "Healthy" if score >= 13 else "Adequate"
        if score >= 9 else "Thin" if score >= 5 else "Illiquid"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=label, confidence=confidence, basis=basis,
        data_sources=["coingecko"],
    )


def score_narrative_momentum(coin: dict, category: str) -> SubDimensionScore:
    market_data = coin.get("market_data", {}) or {}
    rank = coin.get("market_cap_rank")
    price_change_30d = market_data.get("price_change_percentage_30d_in_currency", {}).get("usd")

    hot_categories = {"ai-depin", "layer2", "defi"}  # simplifying assumption, documented
    fit_component = 12 if category in hot_categories else 8
    momentum_component = 4
    if price_change_30d is not None:
        momentum_component = _clip(4 + price_change_30d / 15, 0, 8)
    rank_component = 0
    if rank is not None:
        rank_component = 4 if rank <= 50 else 2 if rank <= 200 else 0

    score = _clip(fit_component * (20 / 20) * 0.4 + momentum_component + rank_component)
    label = (
        "Peak Narrative" if score >= 17 else "Strong Alignment" if score >= 13
        else "Moderate Alignment" if score >= 9 else "Weak Alignment" if score >= 5
        else "Counter-Narrative"
    )
    basis = (
        f"proxy from category={category}, market_cap_rank={rank}, "
        f"30d price change={price_change_30d}% (no live narrative/news source)"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=label, confidence="low", basis=basis,
        data_sources=["coingecko"],
    )


def composite_assessment(total: float) -> str:
    if total >= 80:
        return "Euphoric"
    if total >= 65:
        return "Bullish"
    if total >= 50:
        return "Neutral-Positive"
    if total >= 35:
        return "Neutral-Negative"
    if total >= 20:
        return "Bearish"
    return "Capitulation"


def contrarian_signals(total: float, fng: FearGreedContext) -> list[ContrarianSignal]:
    signals = []
    if fng.value is not None:
        if fng.value <= 24 and total <= 34:
            signals.append(ContrarianSignal(
                condition="Extreme Fear + weak sentiment score",
                signal="Potential Contrarian Buy Zone",
                note="Historically these conditions have preceded recoveries, but confirm with fundamentals - not a recommendation.",
            ))
        if fng.value >= 75 and total >= 80:
            signals.append(ContrarianSignal(
                condition="Extreme Greed + euphoric sentiment score",
                signal="Potential Overheating / Distribution Risk",
                note="Historically these conditions have preceded corrections - not a recommendation.",
            ))
    if not signals:
        signals.append(ContrarianSignal(
            condition="No extreme reading detected",
            signal="No strong contrarian signal",
            note="Sentiment and market fear/greed are within normal ranges.",
        ))
    return signals


# ---------------------------------------------------------------------------
# DEX / new-token scoring path (contract_address + chain, via GeckoTerminal)
#
# Brand-new tokens don't have CoinGecko community_score / developer_score /
# sentiment votes - that data simply doesn't exist yet, and for many
# DEX-only tokens (anonymous devs, no GitHub, no dedicated subreddit) it may
# never exist. Rather than force these into the CoinGecko-shaped scorers
# above (which would silently produce misleadingly confident numbers from
# missing-field defaults), these run on GeckoTerminal's pool data - built
# for exactly this - and are explicit and low-confidence about the
# dimensions that have no real signal at all for a token this new.
# ---------------------------------------------------------------------------

def insufficient_data_score(dimension: str, reason: str) -> SubDimensionScore:
    """A flat, honest mid-low score for a dimension with no real signal for
    this token - e.g. no GitHub repo exists for most brand-new DEX tokens.
    Deliberately not 0 (that implies confirmed bad activity) and not ~10
    (that implies genuine neutrality) - it's explicitly "unknown", which the
    low confidence + basis text make clear rather than letting a numeric
    default masquerade as a measurement.
    """
    return SubDimensionScore(
        score=8.0,
        assessment="Insufficient Data",
        confidence="low",
        basis=reason,
        data_sources=[],
    )


def score_social_buzz_dex(gt_token: dict, gt_pools: list[dict], twitter: Optional[dict]) -> SubDimensionScore:
    attrs = (gt_token.get("data") or {}).get("attributes") or {}

    if twitter and twitter.get("available"):
        mentions = twitter.get("mention_count", 0)
        engagement = twitter.get("total_engagement", 0)
        score = _clip(min(20, (mentions / 10) + (engagement / 500)))
        basis = f"{mentions} mentions / 24h, {engagement} total likes+RTs (live X API)"
        return SubDimensionScore(
            score=round(score, 1), assessment=_buzz_label(score), confidence="high",
            basis=basis, data_sources=["twitter"],
        )

    # Fallback: on-chain buy/sell transaction count as an activity proxy -
    # the closest thing to "buzz" GeckoTerminal can measure for a token with
    # no established social presence yet.
    total_buys = total_sells = 0
    for pool in gt_pools[:3]:
        tx = (pool.get("attributes") or {}).get("transactions", {}) or {}
        h24 = tx.get("h24", {}) or {}
        total_buys += h24.get("buys") or 0
        total_sells += h24.get("sells") or 0
    total_tx = total_buys + total_sells
    # Log-scaled, not linear: the old `total_tx / 20` formula saturated at
    # the max score (20) for almost any token with normal trading activity
    # (400+ tx/24h is common even for modest tokens), which is why this
    # kept returning the identical top score across unrelated tokens.
    # log10 scaling spreads real-world volumes (tens to tens-of-thousands
    # of tx/day) across the full 0-20 range instead.
    import math

    score = _clip(math.log10(max(total_tx, 1)) * 5.5)
    basis = (
        f"proxy from {total_tx} on-chain buy/sell transactions in 24h across top pools "
        f"(log-scaled; no X/Twitter data available: {(twitter or {}).get('reason', 'not configured')})"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=_buzz_label(score), confidence="low",
        basis=basis, data_sources=["geckoterminal"],
    )


def _buzz_label(score: float) -> str:
    return (
        "Viral" if score >= 17 else "High" if score >= 13 else "Moderate"
        if score >= 9 else "Low" if score >= 5 else "Dead"
    )


def score_narrative_momentum_dex(gt_token: dict, gt_pools: list[dict], category: str) -> SubDimensionScore:
    if not gt_pools:
        return insufficient_data_score(
            "Narrative Momentum",
            "no trading pool data available yet - token may be too new or too illiquid to price",
        )
    top_pool = (gt_pools[0].get("attributes") or {})
    price_change_h24 = (top_pool.get("price_change_percentage") or {}).get("h24")
    volume_h24 = (top_pool.get("volume_usd") or {}).get("h24")

    momentum_component = 8.0
    if price_change_h24 is not None:
        try:
            momentum_component = _clip(8 + float(price_change_h24) / 10, 0, 16)
        except (TypeError, ValueError):
            pass
    volume_component = 0.0
    if volume_h24:
        try:
            v = float(volume_h24)
            volume_component = 4.0 if v > 100_000 else 2.0 if v > 10_000 else 0.0
        except (TypeError, ValueError):
            pass

    score = _clip(momentum_component + volume_component)
    label = (
        "Peak Narrative" if score >= 17 else "Strong Alignment" if score >= 13
        else "Moderate Alignment" if score >= 9 else "Weak Alignment" if score >= 5
        else "Counter-Narrative"
    )
    basis = (
        f"proxy from 24h price change={price_change_h24}%, 24h pool volume=${volume_h24} "
        f"(GeckoTerminal top pool), category={category}"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=label, confidence="low", basis=basis,
        data_sources=["geckoterminal"],
    )


def score_liquidity_health_dex(gt_token: dict, gt_pools: list[dict]) -> SubDimensionScore:
    """DEX-side Liquidity Health: pool reserve depth relative to market
    cap/FDV, plus 24h volume turnover relative to that reserve. Unlike
    Developer Activity (which was Insufficient Data for essentially every
    DEX-only token, since almost none have a linked GitHub repo), this is
    computable for any token with an indexed pool - i.e. this ASP's
    primary use case.
    """
    attrs = (gt_token.get("data") or {}).get("attributes") or {}

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    reserve = _f(attrs.get("total_reserve_in_usd"))
    mcap = _f(attrs.get("fdv_usd")) or _f(attrs.get("market_cap_usd"))

    volume_h24 = 0.0
    for pool in gt_pools[:3]:
        v = _f(((pool.get("attributes") or {}).get("volume_usd") or {}).get("h24"))
        if v:
            volume_h24 += v

    if reserve is None and not volume_h24:
        return insufficient_data_score(
            "Liquidity Health",
            "no pool reserve or volume data available - token may be too new/illiquid to assess",
        )

    import math

    reserve_component = 0.0
    if reserve is not None and mcap:
        ratio = reserve / mcap if mcap else 0
        reserve_component = _clip(ratio * 100, 0, 12)
    elif reserve:
        reserve_component = _clip(math.log10(max(reserve, 1)) * 1.5, 0, 12)

    volume_component = 0.0
    if reserve and volume_h24:
        turnover = volume_h24 / reserve
        volume_component = _clip(turnover * 20, 0, 8)
    elif volume_h24:
        volume_component = _clip(math.log10(max(volume_h24, 1)) * 1.2, 0, 8)

    score = _clip(reserve_component + volume_component)
    label = (
        "Deep" if score >= 17 else "Healthy" if score >= 13 else "Adequate"
        if score >= 9 else "Thin" if score >= 5 else "Illiquid"
    )
    basis = (
        f"proxy from pool reserve=${reserve if reserve is not None else 'n/a'}, "
        f"24h volume=${volume_h24:.0f}, mcap/fdv=${mcap if mcap is not None else 'n/a'} "
        f"(GeckoTerminal)"
    )
    return SubDimensionScore(
        score=round(score, 1), assessment=label,
        confidence="medium" if reserve else "low",
        basis=basis, data_sources=["geckoterminal"],
    )
