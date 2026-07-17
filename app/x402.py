"""x402 payment gate using OKX's official seller SDK (`okxweb3-app-x402`).

This wraps the /sentiment route with OKX's PaymentMiddlewareASGI, which:
  - returns HTTP 402 + a PAYMENT-REQUIRED header when no payment is attached
  - verifies (and settles) the payment against OKX's facilitator when a
    PAYMENT-SIGNATURE / X-PAYMENT header is present
  - on success, lets the request through to the real handler and attaches
    a PAYMENT-RESPONSE header with the settlement receipt

HISTORY: this was previously shipped disabled after an
"ImportError: cannot import name 'OKXAuthConfig' from 'x402.http'" that we
misdiagnosed as `okxweb3-app-x402` on PyPI actually being a different,
unrelated Coinbase package with no OKX-specific classes. Re-reading OKX's
actual Python SDK reference (web3.okx.com/onchainos/dev-docs/payments/
sdk-python) confirms OKXAuthConfig/OKXFacilitatorClient/OKXFacilitatorConfig
are real, current, documented exports - this code below matches that spec
exactly. The real bug was in requirements.txt: a separately-added
`x402[evm]` dependency pulled in Coinbase's own generic `x402` package,
which shadowed the OKX-specific `x402` module vendored inside
okxweb3-app-x402 itself. Fixed by requesting the `evm` extra from
okxweb3-app-x402 directly instead (see requirements.txt).

NOT LIVE-TESTED FROM THIS SANDBOX: `okxweb3-app-x402` requires Python
>=3.11, and the sandbox this was built in only has Python 3.10, so the
package cannot be pip-installed or exercised locally here. Vercel's default
Python runtime is 3.12, so it installs fine there - verify the actual paid
flow end-to-end after deploying (see README's testing checklist).

Required env vars when X402_ENABLED=true:
  OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE  - from
    https://web3.okx.com/onchain-os/dev-portal (separate from your
    Agentic Wallet login)
  X402_RECEIVING_ADDRESS                        - your EVM wallet address
    (`onchainos wallet status` / `wallet addresses` in your agent session)
  X402_PRICE_USDC                               - human-readable USD price,
    e.g. "0.1"
"""
from __future__ import annotations

import os


def is_enabled() -> bool:
    return os.getenv("X402_ENABLED", "false").lower() == "true"


def build_middleware():
    """Returns (middleware_class, kwargs_dict) for app.add_middleware(...),
    or None if x402 is disabled. Raises RuntimeError with a clear message
    if enabled but misconfigured, so a bad deploy fails loudly at startup
    rather than silently serving unpaid requests as if they were paid.
    """
    if not is_enabled():
        return None

    required = ["OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "X402_RECEIVING_ADDRESS"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            "X402_ENABLED=true but missing required env var(s): "
            + ", ".join(missing)
            + ". Get OKX_API_KEY/SECRET_KEY/PASSPHRASE from "
            "https://web3.okx.com/onchain-os/dev-portal, and "
            "X402_RECEIVING_ADDRESS from your Agentic Wallet."
        )

    # Imported lazily so the app can still start with X402_ENABLED=false
    # even if this package isn't installed (e.g. during local dev on
    # Python <3.11).
    from x402 import x402ResourceServer
    from x402.http import (
        OKXAuthConfig,
        OKXFacilitatorClient,
        OKXFacilitatorConfig,
        PaymentOption,
        RouteConfig,
    )
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.mechanisms.evm.exact.server import ExactEvmScheme

    auth = OKXAuthConfig(
        api_key=os.environ["OKX_API_KEY"],
        secret_key=os.environ["OKX_SECRET_KEY"],
        passphrase=os.environ["OKX_PASSPHRASE"],
    )
    facilitator = OKXFacilitatorClient(OKXFacilitatorConfig(auth=auth))

    server = x402ResourceServer(facilitator)
    server.register("eip155:196", ExactEvmScheme())  # X Layer
    server.initialize()

    price = os.getenv("X402_PRICE_USDC", "0.1")
    pay_to = os.environ["X402_RECEIVING_ADDRESS"]

    routes = {
        "POST /sentiment": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=pay_to,
                price=f"${price}",
                network="eip155:196",
            ),
            resource="/sentiment",
            description="Crypto sentiment analysis (per-call)",
        )
    }

    return PaymentMiddlewareASGI, {"routes": routes, "server": server}
