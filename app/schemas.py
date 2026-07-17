"""Pydantic models for the crypto sentiment ASP.

Output shape is designed to be consumed by another agent (A2MCP caller),
so it's structured JSON first, with an optional human-readable markdown
report (matching crypto_sentiment.md's OUTPUT FORMAT) as a secondary field.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Assessment = Literal[
    "Euphoric", "Bullish", "Neutral-Positive", "Neutral-Negative", "Bearish", "Capitulation"
]


class SubDimensionScore(BaseModel):
    score: float = Field(..., ge=0, le=20)
    max_score: int = 20
    assessment: str
    confidence: Literal["high", "medium", "low"] = "medium"
    basis: str  # one-line explanation of what data drove this score
    data_sources: list[str]


class FearGreedContext(BaseModel):
    value: Optional[int] = None
    label: Optional[str] = None
    trend_7d: Optional[str] = None
    available: bool = True
    note: Optional[str] = None


class ContrarianSignal(BaseModel):
    condition: str
    signal: str
    note: str


class SentimentRequest(BaseModel):
    token: str = Field(..., description="Ticker or CoinGecko slug, e.g. 'SOL' or 'solana'")
    category_hint: Optional[
        Literal["meme", "layer1", "layer2", "defi", "ai-depin", "other"]
    ] = Field(
        default=None,
        description="Optional override for category-specific sub-dimension weighting.",
    )


class SentimentResponse(BaseModel):
    token_ticker: str
    token_name: str
    category: str
    generated_at: str

    sentiment_score: float = Field(..., ge=0, le=100)
    assessment: Assessment

    sub_dimensions: dict[str, SubDimensionScore]
    fear_greed: FearGreedContext
    contrarian_signals: list[ContrarianSignal]

    verdict: str
    disclaimer: str = (
        "For educational/research purposes only. Not financial advice. "
        "Cryptocurrency is highly volatile. Always DYOR."
    )

    markdown_report: str
    warnings: list[str] = []
