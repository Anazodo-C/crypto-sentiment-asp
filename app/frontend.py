"""Single-page frontend for the Crypto Sentiment ASP, served at GET /.

Deliberately a single self-contained HTML string (no build step, no
external JS framework) so it works as-is inside a Vercel Python function -
no separate static build/deploy pipeline needed. Calls the same POST
/sentiment endpoint any other agent would call.

The logo is embedded as base64 (see app/assets.py) rather than served
from /public - Vercel's @vercel/python builder only bundles files it can
trace as Python imports, so a binary asset only ever read from disk at
runtime silently doesn't make it into the deployed function. Embedding it
as a string literal in a .py module sidesteps that failure mode entirely.
"""

from app.assets import LOGO_PNG_BASE64

_LOGO_DATA_URI = f"data:image/png;base64,{LOGO_PNG_BASE64}"

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sentimento</title>
<link rel="icon" href="__LOGO_DATA_URI__">
<style>
  :root {
    /* OKX brand palette: black, white, signature bright green. No other
       hues - the assessment colors below (amber/red) are functional data
       indicators (bad/neutral signal), not decoration. */
    --bg: #000000;
    --panel: #111111;
    --panel-2: #181818;
    --border: #2a2a2a;
    --text: #ffffff;
    --muted: #8a8a8a;
    --accent: #00d975;
    --accent-2: #00d975;
    --warn: #f4b740;
    --bad: #f87171;
    --good: #00d975;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    min-height: 100vh;
  }
  .wrap { max-width: 960px; margin: 0 auto; padding: 32px 20px 80px; position: relative; }
  header { text-align: left; margin-bottom: 28px; position: relative; padding-left: 64px; min-height: 52px; }
  .logo-corner {
    position: absolute; top: 0; left: 0;
    width: 52px; height: 52px; border-radius: 14px;
    box-shadow: 0 0 0 1px var(--border);
  }
  header h1 {
    font-size: 1.8rem; margin: 0 0 6px; letter-spacing: -0.01em; font-weight: 800;
    color: var(--text);
  }
  header p { color: var(--muted); margin: 0; font-size: 0.95rem; }

  .search-box {
    display: flex; gap: 10px; margin: 0 auto 8px;
    max-width: 640px;
  }
  input#token-input {
    flex: 1;
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 14px 16px;
    border-radius: 10px;
    font-size: 1rem;
    outline: none;
  }
  input#token-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(0, 217, 117, 0.15); }
  button#analyze-btn {
    background: var(--accent);
    color: #000000;
    border: none;
    padding: 14px 22px;
    border-radius: 10px;
    font-weight: 700;
    font-size: 1rem;
    cursor: pointer;
    transition: transform 0.12s ease, opacity 0.12s ease;
  }
  button#analyze-btn:hover:not(:disabled) {
    transform: translateY(-1px);
    opacity: 0.9;
  }
  button#analyze-btn:disabled { opacity: 0.6; cursor: default; }

  #status { text-align: center; color: var(--muted); margin: 24px 0; min-height: 1.2em; }
  #error-box {
    display: none;
    max-width: 640px; margin: 16px auto; padding: 14px 18px;
    background: #2a1418; border: 1px solid #5c2530; border-radius: 10px;
    color: var(--bad); font-size: 0.92rem;
  }

  #report { display: none; }

  .verdict-card {
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 28px;
    margin-bottom: 18px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.25);
  }
  .verdict-top { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }
  .token-name { font-size: 1.4rem; font-weight: 700; }
  .token-ticker { color: var(--muted); font-weight: 500; }
  .category-badge {
    background: var(--panel-2); border: 1px solid var(--border);
    padding: 4px 12px; border-radius: 999px; font-size: 0.78rem; color: var(--muted);
    text-transform: capitalize;
  }
  .score-row { display: flex; align-items: baseline; gap: 14px; margin-bottom: 14px; }
  .score-number { font-size: 3rem; font-weight: 800; line-height: 1; }
  .score-max { color: var(--muted); font-size: 1.1rem; }
  .assessment-pill {
    padding: 6px 16px; border-radius: 999px; font-weight: 600; font-size: 0.95rem;
  }
  .signal-line {
    color: var(--text); line-height: 1.6; font-size: 0.95rem; margin-bottom: 8px;
  }
  .signal-line b { font-weight: 700; }
  .verdict-caveat { color: var(--muted); line-height: 1.55; font-size: 0.88rem; }

  .disclaimer {
    max-width: 100%;
    background: #1c1710; border: 1px solid #3d2f14; color: var(--warn);
    border-radius: 10px; padding: 12px 16px; font-size: 0.82rem; line-height: 1.5;
    margin-bottom: 22px;
  }

  .cards-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 14px; margin-bottom: 22px; max-width: 100%;
  }
  .dim-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    padding: 18px; min-width: 0; transition: transform 0.15s ease, border-color 0.15s ease;
  }
  .dim-card:hover { transform: translateY(-2px); border-color: var(--accent-2); }
  .dim-label { font-size: 0.82rem; color: var(--muted); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.03em; }
  .dim-score { font-size: 1.6rem; font-weight: 700; margin-bottom: 4px; }
  .dim-score span { font-size: 0.95rem; color: var(--muted); font-weight: 500; }
  .dim-assessment { font-size: 0.88rem; font-weight: 600; margin-bottom: 8px; }
  .conf-badge {
    display: inline-block; font-size: 0.68rem; padding: 2px 8px; border-radius: 999px;
    margin-bottom: 10px; font-weight: 600; letter-spacing: 0.02em;
  }
  .conf-high { background: #113325; color: var(--good); }
  .conf-medium { background: #2e2610; color: var(--warn); }
  .conf-low { background: #2a1c14; color: #d89a5c; }
  .dim-basis { font-size: 0.78rem; color: var(--muted); line-height: 1.4; }

  .extra-row { display: flex; gap: 14px; flex-wrap: wrap; }
  .extra-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    padding: 16px 18px; flex: 1 1 260px; min-width: 0;
    transition: border-color 0.15s ease;
  }
  .extra-card:hover { border-color: var(--accent); }
  .extra-card h3 { margin: 0 0 8px; font-size: 0.85rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }
  .extra-card p, .extra-card li { font-size: 0.88rem; line-height: 1.5; margin: 0 0 4px; }
  .extra-card ul { margin: 0; padding-left: 18px; }

  .docs {
    max-width: 640px; margin: 48px auto 0; background: var(--panel);
    border: 1px solid var(--border); border-radius: 14px; padding: 4px 20px;
  }
  .docs summary {
    cursor: pointer; padding: 14px 0; font-weight: 600; font-size: 0.92rem;
    color: var(--text); list-style: none;
  }
  .docs summary::-webkit-details-marker { display: none; }
  .docs summary::before { content: '+ '; color: var(--accent); font-weight: 800; }
  .docs[open] summary::before { content: '- '; }
  .docs-body { padding: 0 0 18px; color: var(--muted); font-size: 0.86rem; line-height: 1.6; }
  .docs-body p { margin: 0 0 10px; }
  .docs-body code {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 4px;
    padding: 1px 5px; color: var(--accent-2); font-size: 0.84em;
  }

  footer { text-align: center; color: var(--muted); font-size: 0.78rem; margin-top: 24px; }
  footer a { color: var(--accent-2); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="__LOGO_DATA_URI__" alt="Sentimento" class="logo-corner">
    <h1>Sentimento</h1>
    <p>Paste a ticker or a contract address. We auto-detect the chain and score sentiment across 5 dimensions.</p>
  </header>

  <div class="search-box">
    <input id="token-input" type="text" placeholder="e.g. SOL, or a contract address (0x... / Solana mint)" autocomplete="off">
    <button id="analyze-btn">Analyze</button>
  </div>

  <div id="status"></div>
  <div id="error-box"></div>

  <div id="report">
    <div class="verdict-card">
      <div class="verdict-top">
        <div>
          <span class="token-name" id="r-name"></span>
          <span class="token-ticker" id="r-ticker"></span>
        </div>
        <div class="category-badge" id="r-category"></div>
      </div>
      <div class="score-row">
        <div class="score-number" id="r-score"></div>
        <div class="score-max">/100</div>
        <div class="assessment-pill" id="r-assessment"></div>
      </div>
      <div class="signal-line" id="r-signals"></div>
      <div class="verdict-caveat" id="r-verdict"></div>
    </div>

    <div class="disclaimer" id="r-disclaimer"></div>

    <div class="cards-row" id="r-cards"></div>

    <div class="extra-row">
      <div class="extra-card">
        <h3>Fear &amp; Greed Context</h3>
        <p id="r-fng"></p>
      </div>
      <div class="extra-card">
        <h3>Contrarian Signals</h3>
        <ul id="r-contrarian"></ul>
      </div>
      <div class="extra-card" id="r-warnings-card" style="display:none;">
        <h3>Data Warnings</h3>
        <ul id="r-warnings"></ul>
      </div>
    </div>
  </div>

  <details class="docs">
    <summary>How this works</summary>
    <div class="docs-body">
      <p><b>Established coins</b> (e.g. <code>SOL</code>, <code>BTC</code>) are resolved via CoinGecko's main coin database.</p>
      <p><b>New / DEX-only tokens</b> — paste a contract address (<code>0x...</code> or a Solana mint) and the chain is auto-detected via GeckoTerminal, which indexes new pools within minutes of launch instead of the hours-to-days CoinGecko's curated listing process takes. This is Sentimento's primary use case.</p>
      <p>Every sub-dimension carries a <code>confidence</code> level and a <code>basis</code> string explaining exactly what data produced it — proxy-derived and Insufficient Data scores are labeled as such rather than presented as precise measurements.</p>
      <p>This is an A2MCP endpoint for OKX.AI &middot; <code>POST /sentiment</code> &middot; <a href="/docs">full API docs</a></p>
    </div>
  </details>

  <footer>
    Sentimento &middot; for research/educational use only, not financial advice
  </footer>
</div>

<script>
const input = document.getElementById('token-input');
const btn = document.getElementById('analyze-btn');
const statusEl = document.getElementById('status');
const errorBox = document.getElementById('error-box');
const report = document.getElementById('report');

const DIM_LABELS = {
  social_buzz: 'Social Buzz',
  news_tone: 'News Tone',
  community_health: 'Community Health',
  liquidity_health: 'Liquidity Health',
  narrative_momentum: 'Narrative Momentum',
};

function assessmentColor(a) {
  const good = ['Euphoric', 'Bullish', 'Viral', 'High', 'Thriving', 'Healthy', 'Very Active', 'Active',
                'Peak Narrative', 'Strong Alignment', 'Very Positive', 'Positive'];
  const bad = ['Bearish', 'Capitulation', 'Dead', 'Declining', 'Inactive', 'Counter-Narrative',
               'Very Negative', 'Negative'];
  if (good.includes(a)) return 'var(--good)';
  if (bad.includes(a)) return 'var(--bad)';
  return 'var(--warn)';
}

async function analyze() {
  const value = input.value.trim();
  if (!value) return;
  btn.disabled = true;
  statusEl.textContent = 'Fetching live data and scoring…';
  errorBox.style.display = 'none';
  report.style.display = 'none';

  try {
    const resp = await fetch('/sentiment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: value }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || ('Request failed with status ' + resp.status));
    }
    render(data);
    statusEl.textContent = '';
  } catch (err) {
    statusEl.textContent = '';
    errorBox.style.display = 'block';
    errorBox.textContent = err.message || String(err);
  } finally {
    btn.disabled = false;
  }
}

function render(data) {
  document.getElementById('r-name').textContent = data.token_name;
  document.getElementById('r-ticker').textContent = ' (' + data.token_ticker + ')';
  document.getElementById('r-category').textContent = data.category.replace(/-/g, ' ');

  document.getElementById('r-score').textContent = data.sentiment_score.toFixed(1);
  document.getElementById('r-score').style.color = assessmentColor(data.assessment);

  const pill = document.getElementById('r-assessment');
  pill.textContent = data.assessment;
  const color = assessmentColor(data.assessment);
  pill.style.background = color + '22';
  pill.style.color = color;

  const strongDim = data.sub_dimensions[data.strongest_signal];
  const weakDim = data.sub_dimensions[data.weakest_signal];
  const strongLabel = DIM_LABELS[data.strongest_signal] || data.strongest_signal;
  const weakLabel = DIM_LABELS[data.weakest_signal] || data.weakest_signal;
  document.getElementById('r-signals').innerHTML =
    `Strongest signal: <b style="color:var(--good)">${strongLabel}</b>` +
    (strongDim ? ` (${strongDim.score}/20)` : '') +
    ` &nbsp;&middot;&nbsp; Weakest signal: ` +
    `<b style="color:${weakDim ? assessmentColor(weakDim.assessment) : 'var(--warn)'}">${weakLabel}</b>` +
    (weakDim ? ` (${weakDim.score}/20)` : '');

  document.getElementById('r-verdict').textContent = data.verdict;
  document.getElementById('r-disclaimer').textContent = '⚠ ' + data.disclaimer;

  const cardsRow = document.getElementById('r-cards');
  cardsRow.innerHTML = '';
  for (const key of Object.keys(DIM_LABELS)) {
    const d = data.sub_dimensions[key];
    if (!d) continue;
    const card = document.createElement('div');
    card.className = 'dim-card';
    card.innerHTML = `
      <div class="dim-label">${DIM_LABELS[key]}</div>
      <div class="dim-score">${d.score}<span>/${d.max_score}</span></div>
      <div class="dim-assessment" style="color:${assessmentColor(d.assessment)}">${d.assessment}</div>
      <div class="conf-badge conf-${d.confidence}">${d.confidence} confidence</div>
      <div class="dim-basis">${d.basis}</div>
    `;
    cardsRow.appendChild(card);
  }

  const fng = data.fear_greed;
  document.getElementById('r-fng').textContent = fng.available
    ? `${fng.value}/100 (${fng.label}), 7d trend: ${fng.trend_7d}`
    : (fng.note || 'Unavailable at request time.');

  const contrarianList = document.getElementById('r-contrarian');
  contrarianList.innerHTML = '';
  (data.contrarian_signals || []).forEach(c => {
    const li = document.createElement('li');
    li.textContent = `${c.signal} — ${c.condition}`;
    contrarianList.appendChild(li);
  });

  const warningsCard = document.getElementById('r-warnings-card');
  const warningsList = document.getElementById('r-warnings');
  warningsList.innerHTML = '';
  if (data.warnings && data.warnings.length) {
    data.warnings.forEach(w => {
      const li = document.createElement('li');
      li.textContent = w;
      warningsList.appendChild(li);
    });
    warningsCard.style.display = 'block';
  } else {
    warningsCard.style.display = 'none';
  }

  report.style.display = 'block';
}

btn.addEventListener('click', analyze);
input.addEventListener('keydown', (e) => { if (e.key === 'Enter') analyze(); });
</script>
</body>
</html>
"""

INDEX_HTML = INDEX_HTML.replace("__LOGO_DATA_URI__", _LOGO_DATA_URI)
