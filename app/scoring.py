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
  News Tone           -> CoinGecko public_interest_score + sentiment
                         votes + recent price momentum (proxy - no
                         live headline source wired up)
  Community Health    -> CoinGecko community_score + community_data
                         (Reddit/Telegram subscriber counts) (direct)
  Developer Activity  -> CoinGecko developer_score + developer_data
                         (GitHub stars/forks/commits) (direct)
  Narrative Momentum  -> CoinGecko categories + market_cap_rank +
                         price_change_percentage_30d (proxy)
"""
from __future__ import annotations

from datetime import datetime, timezone
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
    telegram = community.get("telegram_channel_user_count")

    score = _clip(community_score / 100 * 20) if community_score else 8
    label = (
        "Thriving" if score >= 17 else "Healthy" if score >= 13 else "Moderate"
        if score >= 9 else "Declining" if score >= 5 else "Dead"
    )
    basis = (
        f"CoinGecko community_score={community_score}, "
        f"reddit_subscribers={community.get('reddit_subscribers')}, "
        f"telegram_users={telegram if telegram is not None else 'n/a'}"
    )
    confidence = "high" if community_score else "low"
    return SubDimensionScore(
        score=round(score, 1), assessment=label, confidence=confidence, basis=basis,
        data_sources=["coingecko"],
    )


def score_developer_activity(coin: dict) -> SubDimensionScore:
    dev_score = coin.get("developer_score") or 0
    dev = coin.get("developer_data", {}) or {}
    commits_4w = dev.get("commit_count_4_weeks")

    score = _clip(dev_score / 100 * 20) if dev_score else 6
    label = (
        "Very Active" if score >= 17 else "Active" if score >= 13 else "Moderate"
        if score >= 9 else "Low" if score >= 5 else "Inactive"
    )
    basis = (
        f"CoinGecko developer_score={dev_score}, commits_4w={commits_4w}, "
        f"stars={dev.get('stars')}, forks={dev.get('forks')}"
    )
    confidence = "high" if dev_score else "low"
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


def build_markdown_report(token_name: str, ticker: str, total: float, subs: dict, fng: FearGreedContext, contrarian: list[ContrarianSignal], verdict: str, warnings: list[str]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# {token_name} ({ticker}) — Sentiment Analysis",
        "",
        f"**Generated:** {now}",
        "**Agent:** Crypto Sentiment ASP v1.0 (A2MCP)",
        f"**Sentiment Score:** {total:.1f}/100",
        "",
        "> DISCLAIMER: For educational/research purposes only. Not financial advice. "
        "Cryptocurrency is highly volatile. Always DYOR.",
        "",
        "---", "",
        f"## Sentiment Score: {total:.1f}/100", "",
        "| Sub-Dimension | Score | Confidence | Assessment |",
        "|---|---|---|---|",
    ]
    names = {
        "social_buzz": "Social Buzz", "news_tone": "News Tone",
        "community_health": "Community Health", "developer_activity": "Developer Activity",
        "narrative_momentum": "Narrative Momentum",
    }
    for key, label in names.items():
        s = subs[key]
        lines.append(f"| {label} | {s.score}/20 | {s.confidence} | {s.assessment} |")

    lines += ["", "---", "", "## Market Fear & Greed Context", ""]
    if fng.available and fng.value is not None:
        lines.append(f"Fear & Greed Index: **{fng.value}/100** ({fng.label}), 7d trend: {fng.trend_7d}")
    else:
        lines.append("Fear & Greed Index unavailable at request time.")

    lines += ["", "---", "", "## Sub-Dimension Basis", ""]
    for key, label in names.items():
        s = subs[key]
        lines.append(f"- **{label}**: {s.basis} (sources: {', '.join(s.data_sources)})")

    lines += ["", "---", "", "## Contrarian Signals", ""]
    for c in contrarian:
        lines.append(f"- **{c.signal}** — {c.condition}. {c.note}")

    lines += ["", "---", "", "## Sentiment Verdict", "", verdict]

    if warnings:
        lines += ["", "---", "", "## Data Warnings", ""]
        for w in warnings:
            lines.append(f"- {w}")

    lines += [
        "", "---", "",
        "*DISCLAIMER: For educational/research purposes only. Not financial advice. "
        "Cryptocurrency is highly volatile. Always DYOR. Several sub-dimensions above "
        "are structured-data proxies rather than live social/news scraping - see basis "
        "notes and confidence levels for what's directly measured vs. approximated.*",
    ]
    return "\n".join(lines)


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
    score = _clip(min(20, total_tx / 20))
    basis = (
        f"proxy from {total_tx} on-chain buy/sell transactions in 24h across top pools "
        f"(no X/Twitter data available: {(twitter or {}).get('reason', 'not configured')})"
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
