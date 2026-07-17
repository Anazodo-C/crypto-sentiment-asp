"""x402 payment gate stub.

x402 is the HTTP 402 "Payment Required" pattern: an unpaid request gets a
402 response with payment instructions (amount, address, network); the
caller's agent then submits payment and retries with proof (typically an
X-PAYMENT header containing a signed payment payload), which we should
verify before serving the paid result.

STATUS: this is a stub, not yet wired to OKX's Payment SDK / a real
facilitator. UNVALIDATED ASSUMPTION (per Stage 0): the exact verification
call and header format OKX's SDK expects. Do this next, following:
https://web3.okx.com/onchainos/dev-docs/okxai/howtomcp

Until X402_ENABLED=true is set (see .env.example), this gate is a no-op
and the endpoint is free - which is fine for building/testing/demoing the
scoring logic today, but must be flipped on with real verification wired
in before this counts as a genuine paid A2MCP listing.
"""
from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import JSONResponse


def is_enabled() -> bool:
    return os.getenv("X402_ENABLED", "false").lower() == "true"


def payment_required_response() -> JSONResponse:
    price = os.getenv("X402_PRICE_USDC", "0.5")
    address = os.getenv("X402_RECEIVING_ADDRESS", "UNSET")
    network = os.getenv("X402_NETWORK", "x-layer")
    return JSONResponse(
        status_code=402,
        content={
            "x402Version": 1,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": network,
                    "maxAmountRequired": price,
                    "asset": "USDC",
                    "payTo": address,
                    "description": "Crypto sentiment analysis (per-call)",
                }
            ],
        },
    )


async def check_payment(request: Request) -> bool:
    """Returns True if payment is verified (or gate disabled). False -> caller
    should return payment_required_response().

    TODO before this is real: verify the X-PAYMENT header against OKX's
    facilitator/Payment SDK instead of just checking presence.
    """
    if not is_enabled():
        return True
    payment_header = request.headers.get("X-PAYMENT")
    if not payment_header:
        return False
    # PLACEHOLDER - replace with real OKX Payment SDK verification call.
    return True
