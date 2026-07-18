# Sentimento — OKX Crypto Sentiment ASP

Session resumption notes. Agent identity: **#6370** ("Sentimento", ASP role) on OKX.AI, wallet `0x8a52ad99f648bfa68a2e78805db868458615e7e8`. Live service: https://crypto-sentiment-asp.vercel.app

## Status as of this session

Resubmitted for review via `onchainos agent activate --agent-id 6370 --preferred-language en-US` — response matched the "submitted for review" shape (`submitApproval.approvalStatus: 2, success: true`).

As of the last check (**2026-07-18 22:32 UTC**, ~6.5h after it first showed "under review" at 16:01 UTC): still `approvalDisplayStatus: 2` — **"Listing under review"**, `approvalRemark: "AI quality review suggested pass"`. `updatedAt` keeps advancing between checks (record is actively being touched, not stalled). OKX's stated SLA is "up to 24 hours" — **do not resubmit yet**, we're well inside that window. `activate` is a no-op while already under review, so resubmitting now wouldn't do anything useful and could look like noise on OKX's side. Worth reconsidering only if it's still stuck past ~24h from 16:01 UTC on 2026-07-18 (i.e. past ~16:01 UTC on 2026-07-19).

To re-check status: `onchainos agent get-agents --agent-ids 6370` (read `approvalLabel` / `approvalRemark`). Needs `export PATH=~/.npm-global/bin:$PATH` first if `okx-a2a` isn't already on PATH in the shell.

## What the previous rejection said

> This Agent has not passed x402 standard validation... Integrate x402 on your server using the OKX Payment SDK... ensure unpaid requests return a standard 402 challenge... re-verify service availability, then resubmit.

## Root causes found and fixed (all deployed to production before resubmission)

1. **`GET`/`OPTIONS /sentiment` returned `405` instead of `402`** — OKX's `PaymentMiddlewareASGI` only gated the exact `"POST /sentiment"` route; a prober defaulting to `GET` saw a 405, not a valid 402 challenge. Fixed by adding a `GET /sentiment` alias (query-param driven) gated identically. (`app/x402.py`, `app/main.py`)
2. **`POST /` (bare domain root) also 405'd** — confirmed live via curl that OKX's reviewer probes the literal registered domain root, not `/sentiment` specifically. No `POST /` handler existed at all. Added one, gated identically, delegating to the same sentiment logic. **This was very likely the direct cause of the repeated rejection.** `GET /` is untouched and still serves the free frontend.
3. **402 response body was an opaque `{}`** — the real challenge only lived in the base64 `PAYMENT-REQUIRED` header. Added `PaymentRequiredBodyMiddleware` to rewrite the body into a readable `{"error": "Payment Required", "message": "..."}` while leaving the header untouched. Must be registered with `app.add_middleware()` *after* the OKX middleware — Starlette makes the most-recently-added middleware outermost (verified empirically, not from docs).
4. **Frontend had zero payment handling** — a 402 just broke the demo UI for every visitor once `X402_ENABLED=true` went live. Added an in-browser wallet payment flow: decode the challenge, connect `window.ethereum`, switch to X Layer, sign an EIP-3009 `TransferWithAuthorization` via `eth_signTypedData_v4`, retry with a `PAYMENT-SIGNATURE` header. No official OKX/x402.org browser SDK exists (both only document Node.js/private-key flows) — wire format inferred from OKX's seller SDK docs + the public x402 exact-EVM scheme spec, grounded against a real CLI-driven payment to this same endpoint.
5. **Mojibake in the frontend's decoded token name** (`USDâ®0` instead of `USD₮0`) — `atob()` returns raw bytes, not UTF-8 text; fixed by decoding through `TextDecoder('utf-8')` before `JSON.parse`.

## Verified live (production)

- `POST /sentiment`, `GET /sentiment`, `POST /` all return `402` + valid `PAYMENT-REQUIRED` header for unpaid requests
- A real end-to-end payment (0.1 USD₮0 on X Layer mainnet) was run via the `onchainos payment quote`/`pay` CLI flow and settled successfully — real `txHash`, paid SOL sentiment report returned
- `GET /`, `/health`, `/info` remain free and unaffected

## Not yet verified

- The **in-browser wallet payment flow** (`window.ethereum` + `eth_signTypedData_v4`) has not been click-tested with a real funded wallet — no browser/wallet available in this environment to do that. Per x402's verify-then-settle design, a malformed signature is cleanly rejected before any funds move, so this is low-risk to leave unverified, but should be clicked through manually before depending on it.

## Key facts for next time

- ASP service registration (`agent service-list --agent-id 6370`) has **no field to declare HTTP method** — just `endpoint`/`fee`/`serviceType`. There's no way to tell OKX's reviewer "this is POST-only" at the registration level; the only fix available is making the server itself tolerant of alternate methods/paths.
- Price token is **USD₮0 (USDT0)**, not USDG — confirmed from OKX's own facilitator response (`extra.name: "USD₮0"`), asset contract `0x779ded0c9e1022225f8e0630b35a9b54be713736` on X Layer (`eip155:196`).
- `okx-a2a` CLI required a user-writable npm global prefix (`~/.npm-global`, added to `~/.bash_profile`) to install without `sudo` — needed before any `agent activate`/`update` call can run (A2A communication readiness gate).
