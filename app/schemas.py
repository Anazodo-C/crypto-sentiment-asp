"""Pydantic models for the crypto sentiment ASP.

Output shape is designed to be consumed by another agent (A2MCP caller),
so it's structured JSON first, with an optional human-readable markdown
report (matching crypto_sentiment.md's OUTPUT FORMAT) as a secondary field.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

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
    # Single free-form input, accepts EITHER a ticker/name (established
    # coin, resolved via CoinGecko) OR a raw contract address (new/DEX-only
    # token, resolved via GeckoTerminal with the chain auto-detected). The
    # frontend only ever needs to fill in this one field - see
    # app/main.py's `_looks_like_address` for the routing heuristic.
    token: Optional[str] = Field(
        default=None,
        description="Ticker/name (e.g. 'SOL') OR a contract address (e.g. '0x...' or a Solana mint)",
    )

    # Explicit path B fields, still supported directly for callers that
    # already know the chain (skips the auto-detect network search).
    contract_address: Optional[str] = Field(
        default=None, description="Token contract address, e.g. '0x...' or a Solana mint address"
    )
    chain: Optional[str] = Field(
        default=None,
        description=(
            "Chain the contract lives on, e.g. 'ethereum', 'bsc', 'base', "
            "'solana', 'arbitrum', 'polygon', 'x-layer'. Optional - if "
            "omitted while contract_address is set, the chain is "
            "auto-detected."
        ),
    )

    category_hint: Optional[
        Literal["meme", "layer1", "layer2", "defi", "ai-depin", "other"]
    ] = Field(
        default=None,
        description="Optional override for category-specific sub-dimension weighting.",
    )

    @model_validator(mode="after")
    def _one_lookup_path(self):
        if not self.token and not self.contract_address:
            raise ValueError(
                "Provide either 'token' (ticker, name, or a contract address) "
                "or 'contract_address'."
            )
        return self


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

    warnings: list[str] = []
