# Crypto Sentiment ASP

An A2MCP (Agent-to-MCP) Agent Service Provider for OKX.AI: given a token,
returns a 0-100 Sentiment Score across 5 sub-dimensions (Social Buzz,
News Tone, Community Health, Liquidity Health, Narrative Momentum),
following the scoring methodology in `crypto_sentiment.md` (with
Developer Activity swapped for Liquidity Health — see below).

**Problem it solves:** narrative drives crypto prices as much as
fundamentals, but reading "the room" across Twitter, Reddit, GitHub, and
market psychology takes real time. This ASP does it in one call, so
other agents (trading bots, research agents, portfolio assistants) can
pull a structured sentiment read instead of doing it themselves.

**Two lookup paths, POST /sentiment body:**

- **Established coins** — `{"token": "SOL"}` (ticker or name). Resolved via
  CoinGecko's main coin database.
- **Brand-new / DEX-only tokens** — `{"contract_address": "0x...", "chain": "base"}`.
  This is the ASP's primary intended use case: CoinGecko's main database
  requires manual/automated review and lags new listings by hours to days,
  so ticker lookup simply doesn't work for a token that launched an hour
  ago. This path uses GeckoTerminal instead (CoinGecko's own DEX-tracking
  product), which indexes new pools within minutes, keyed by chain +
  contract address. Supported `chain` values include `ethereum`, `bsc`,
  `base`, `solana`, `arbitrum`, `polygon`, `avalanche`, `x-layer` (see
  `app/geckoterminal.py`'s alias map — the X Layer slug specifically is
  unconfirmed, verify it against `GET /networks` if it 404s).

The two paths return the same response shape, but score differently
honestly: a genuinely unlisted brand-new token has no CoinGecko
community data at all (not "low", nonexistent), so News Tone and
Community Health come back explicitly marked `"Insufficient Data"` with
low confidence rather than a misleading estimated number. Liquidity
Health and Social Buzz instead lean on GeckoTerminal's pool
reserve/volume and on-chain transaction counts — real signal available
even with no established social presence yet. If GeckoTerminal's own
data links the token to a CoinGecko coin id (`coingecko_coin_id` — common
even for fairly new tokens, since GeckoTerminal indexes far faster and
cross-references CoinGecko once a listing exists), the ASP opportunistically
enriches News Tone, Community Health, and Liquidity Health with real
CoinGecko data instead of falling back to Insufficient Data — check the
`warnings` array for `"also listed on CoinGecko"` to see when this kicked in.

**GeckoTerminal not live-tested**: same caveat as CoinGecko/Fear&Greed
below — this sandbox's network is allowlisted and blocked
`api.geckoterminal.com`, so `app/geckoterminal.py` is written against
GeckoTerminal's documented, stable public API shape but hasn't been hit
live. Test the contract-address path for real after deploying, and
double check the `x-layer` network slug specifically (least certain
entry in the alias map).

## What's real vs. proxy (read this before demoing)

This ships honest about its data sources rather than overclaiming:

| Sub-dimension | Source | Confidence |
|---|---|---|
| Community Health | CoinGecko `community_score` if populated; else log-scaled Reddit/Telegram/X follower counts directly (the `_score` field is frequently null even for established coins — falling back to a flat default there was producing near-identical scores across unrelated tokens) | **Direct** if `community_score` present, else **proxy** |
| Liquidity Health | CoinGecko `liquidity_score`, or 24h volume/market cap ratio (established coins); pool reserve depth + volume turnover via GeckoTerminal (new/DEX tokens) | Direct or **proxy** depending on data availability |
| Social Buzz | Live tweet search via twitterapi.io if a valid key is set; falls back to log-scaled follower count + sentiment votes (established) or log-scaled on-chain tx count (new tokens) | Direct if the API call succeeds, else **proxy** |
| News Tone | Keyword bullish/bearish tone tally over live tweet text we already fetch for Social Buzz, if available; else CoinGecko `public_interest_score` + sentiment votes + price momentum | **Proxy** either way (no live headline/news API wired up) |
| Narrative Momentum | CoinGecko category + market cap rank + 30d price momentum (established); 24h price change + pool volume via GeckoTerminal (new tokens) | **Proxy** (no live narrative/news source) |

**Why Liquidity Health replaced Developer Activity:** GitHub commit
activity is a weak-to-irrelevant sentiment signal for most of today's
tokens — anonymous devs, no public repo, pure memecoins — and it was
also the one dimension that flatly didn't apply to this ASP's primary
use case (brand-new DEX tokens almost never have a linked GitHub repo,
so it was always `"Insufficient Data"` on that path anyway). Liquidity
depth and volume turnover are real signals of market health/confidence
and are computable for established coins and new tokens alike.

Every response includes a `confidence` field per sub-dimension and a
`basis` string explaining exactly what data produced the score, plus a
top-level `warnings` array when something degraded. Don't strip these out
for the demo — judges penalize overclaiming more than a scoped-down but
honest system.

**Social data source:** uses [twitterapi.io](https://twitterapi.io) (a
third-party Twitter data provider, auth via a single `X-API-Key` header,
billed per tweet returned) rather than the official Twitter API - it has
no tiered search restrictions, so a valid key gets live mention/engagement
data. If the key is missing or rejected, the code falls back to a
CoinGecko-derived proxy automatically. Check the `warnings` field in a
live response to see which path it took, and set `TWITTERAPI_IO_KEY` (not
the old `TWITTER_BEARER_TOKEN` name) in your deploy env vars.

## Project structure

```
app/
  main.py          FastAPI app, the /sentiment endpoint
  schemas.py       Request/response models
  coingecko.py     CoinGecko client (free, keyless)
  feargreed.py     Fear & Greed Index client (free, keyless)
  twitter.py       X API client with fallback
  scoring.py       The 5-dimension scoring engine (the actual IP)
  x402.py          Payment gate - STUB, see below
api/index.py       Vercel ASGI entrypoint (re-exports app/main.py's app)
vercel.json        Vercel build/routing config
test_smoke.py      End-to-end test using fixture data (mocks all 3 external APIs)
```

## Run locally

```bash
cd okx-sentiment-asp
pip install -r requirements.txt
cp .env.example .env   # fill in TWITTER_BEARER_TOKEN at minimum
uvicorn app.main:app --reload --port 8000
```

Test it:

```bash
curl -X POST http://localhost:8000/sentiment \
  -H "Content-Type: application/json" \
  -d '{"token": "SOL"}'
```

Run the smoke test (no network needed, uses fixtures):

```bash
python3 test_smoke.py
```

**Important — validate live network calls before you demo.** This was
built in a sandboxed environment where outbound calls to
`api.coingecko.com` and `api.alternative.me` were blocked by network
allowlisting, so the CoinGecko/Fear&Greed integration is written against
documented API shapes but has not been hit live. Run the curl command
above from your real dev machine/host first and check the response
before you rely on it in front of judges.

## Deploy (Vercel)

This repo is already set up for Vercel: `vercel.json` + `api/index.py`
re-export the FastAPI app as an ASGI function; Vercel's `@vercel/python`
runtime detects and serves it directly, no adapter package needed.

**Via CLI (fastest):**

```bash
npm install -g vercel     # if you don't have it
cd okx-sentiment-asp
vercel login
vercel                    # first deploy, follow prompts (link/create project)
vercel env add TWITTERAPI_IO_KEY production
vercel env add X402_RECEIVING_ADDRESS production
vercel env add X402_PRICE_USDC production
vercel env add X402_NETWORK production
vercel env add X402_ENABLED production
vercel --prod             # deploy to production once env vars are set
```

**Via dashboard:** push this folder to a GitHub repo, then "New Project"
on vercel.com, import the repo, and add the same env vars under
Settings > Environment Variables before your first production deploy.

**After deploying**, hit your live URL to confirm it actually works
end-to-end (this is the live-network check flagged above — Vercel's
runtime has normal outbound internet access, unlike the sandbox this was
built in):

```bash
curl -X POST https://<your-project>.vercel.app/sentiment \
  -H "Content-Type: application/json" \
  -d '{"token": "SOL"}'
```

Check the `warnings` array in the response — it'll tell you whether the
X/Twitter search call actually worked on your free-tier key, or whether
it silently fell back to the CoinGecko proxy path.

**Cold starts:** Vercel serverless functions spin down when idle. The
first call after inactivity will be slower (a few hundred ms to ~1-2s
extra) — worth knowing if you're timing a live demo for judges.

Other Python hosts (Render, Fly.io, Railway) also work if you'd rather
run `uvicorn app.main:app` directly instead of the Vercel adapter —
just ignore `vercel.json`/`api/` in that case.

## x402 payment integration — attempted, currently blocked, shipping free

`app/x402.py` was written to wrap `POST /sentiment` with what OKX's docs
describe as their official seller SDK (`okxweb3-app-x402`'s
`PaymentMiddlewareASGI`, with `OKXAuthConfig`/`OKXFacilitatorClient` for
verify+settle against OKX's facilitator on X Layer).

**This does not currently work, and the reason is worth recording.**
`pip install okxweb3-app-x402` resolves to a PyPI package whose metadata
lists **Coinbase** as author and `github.com/coinbase/x402` as its
homepage — i.e. it's the generic, network-agnostic x402 protocol library,
not an OKX-specific facilitator wrapper. The `OKXAuthConfig` /
`OKXFacilitatorClient` / `OKXFacilitatorConfig` classes described in
OKX's own SDK reference docs do not exist in this package
(`ImportError: cannot import name 'OKXAuthConfig' from 'x402.http'`).
Either OKX distributes their real facilitator SDK a different way (a
private index, something unlocked after dev-portal signup, a differently
-named package), or the docs describe something not yet matched by what's
publicly installable. This was not resolved before the deadline.

**Current behavior**: `x402.build_middleware()` is wrapped in a try/except
in `app/main.py` specifically so this kind of failure can't take down the
whole service — if it throws, the app logs it, exposes the real exception
via `GET /health`'s `x402_error` field (faster to debug than digging
through Vercel's log tab), and **falls back to serving `/sentiment` for
free** rather than 500ing every route. Practically: leave
`X402_ENABLED=false` in your deploy env vars and register the ASP as
**free** — free A2MCP services are explicitly supported per the OKX.AI
tutorial, and this can be revisited and flipped on later without
re-registering from scratch.

**If you pick this back up later**: don't restart from OKX's Python SDK
reference doc as ground truth — it didn't match the real package. Instead
start from whatever the OKX Developer Portal actually hands you after
signup (real package name, real code samples), or from
`github.com/coinbase/x402`'s own Python folder if a generic (non-OKX
-specific) facilitator turns out to be sufficient.

## Registering as an ASP on OKX.AI

From the tutorial at https://www.okx.ai/tutorial/asp, once your service
is deployed and reachable:

1. Install an agent runtime (OpenClaw/Hermes/Claude Code/Codex) if you
   haven't already.
2. Send your agent: `npx skills add okx/onchainos-skills --yes -g`
3. Log into Agentic Wallet: "Log in to Agentic Wallet on Onchain OS with
   my email"
4. Register: "Help me register an A2MCP ASP on OKX.AI using OKX Agent
   Identity from Onchain OS" - you'll need your deployed endpoint URL.
5. List it: "Help me list my ASP on OKX.AI using Onchain OS"
6. Review takes up to 24 hours - **this is the biggest deadline risk**,
   flagged back in Stage 0. Submit as early as possible.
7. Post on X with #OKXAI introducing the ASP with a <=90s demo, then
   submit the Google form before Jul 17 23:59 UTC.

## Known gaps / honest roadmap

- News Tone and Narrative Momentum are proxies, not live headline/CT
  analysis - the biggest quality gap vs. the original `crypto_sentiment.md`
  vision. A real upgrade path is CryptoPanic or NewsAPI for headlines.
- Token resolution (`coingecko.resolve_coin_id`) does simple ticker
  matching; ambiguous tickers (e.g. multiple coins named "SOL"-adjacent)
  could resolve to the wrong coin. Fine for a demo, worth hardening later.
- x402 verification is a stub (see above) - the most important thing to
  finish next if this needs to be a real paid listing.
- CoinGecko free tier has rate limits; under real load you'd want a
  CoinGecko API key or caching layer.
