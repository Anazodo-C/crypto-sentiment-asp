# Crypto Sentiment ASP

An A2MCP (Agent-to-MCP) Agent Service Provider for OKX.AI: given a token
ticker or name, returns a 0-100 Sentiment Score across 5 sub-dimensions
(Social Buzz, News Tone, Community Health, Developer Activity, Narrative
Momentum), following the scoring methodology in `crypto_sentiment.md`.

**Problem it solves:** narrative drives crypto prices as much as
fundamentals, but reading "the room" across Twitter, Reddit, GitHub, and
market psychology takes real time. This ASP does it in one call, so
other agents (trading bots, research agents, portfolio assistants) can
pull a structured sentiment read instead of doing it themselves.

## What's real vs. proxy (read this before demoing)

This ships honest about its data sources rather than overclaiming:

| Sub-dimension | Source | Confidence |
|---|---|---|
| Community Health | CoinGecko `community_score` + Reddit/Telegram subscriber counts | **Direct** measurement |
| Developer Activity | CoinGecko `developer_score` + GitHub commits/stars/forks | **Direct** measurement |
| Social Buzz | Live tweet search via twitterapi.io if a valid key is set; falls back to CoinGecko follower count + sentiment votes | Direct if the API call succeeds, else **proxy** |
| News Tone | CoinGecko `public_interest_score` + sentiment votes + price momentum | **Proxy** (no live headline source wired up) |
| Narrative Momentum | CoinGecko category + market cap rank + 30d price momentum | **Proxy** (no live narrative/news source) |

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

## x402 payment integration - NOT DONE YET

`app/x402.py` is a stub. `X402_ENABLED=false` by default so you can build
and demo the scoring logic without a live payment rail blocking you.
Before this counts as a real paid A2MCP listing, you need to:

1. Follow the OKX.AI ASP tutorial (https://www.okx.ai/tutorial/asp) to
   install Onchain OS, log into the Agentic Wallet, and get your
   receiving address / Agent Identity.
2. Wire real payment verification into `check_payment()` in `x402.py` -
   right now it just checks that an `X-PAYMENT` header is present, it
   does NOT verify it. That's the single highest-risk placeholder in
   this codebase - don't ship it as-is if you're actually taking payment.
3. Set `X402_ENABLED=true` and the other `X402_*` env vars once verified.

If you're short on time, list it as a **free A2MCP endpoint** instead
(just remove/skip the payment gate entirely) - free services are
explicitly supported and don't need x402 at all, per the OKX.AI tutorial.
This is the safer choice if today is your only build day.

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
