"""Single-page frontend for the Crypto Sentiment ASP, served at GET /.

Deliberately a single self-contained HTML string (no build step, no
external JS framework) so it works as-is inside a Vercel Python function -
no separate static build/deploy pipeline needed. Calls the same POST
/sentiment endpoint any other agent would call.
"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Crypto Sentiment ASP</title>
<style>
  :root {
    --bg: #0b0e14;
    --panel: #131824;
    --panel-2: #1a2130;
    --border: #253048;
    --text: #e6ebf5;
    --muted: #8b96ab;
    --accent: #6ee7b7;
    --accent-2: #60a5fa;
    --warn: #f4b740;
    --bad: #f87171;
    --good: #34d399;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    min-height: 100vh;
  }
  .wrap { max-width: 960px; margin: 0 auto; padding: 32px 20px 80px; }
  header { text-align: center; margin-bottom: 28px; }
  header h1 { font-size: 1.6rem; margin: 0 0 6px; letter-spacing: -0.01em; }
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
  input#token-input:focus { border-color: var(--accent-2); }
  button#analyze-btn {
    background: var(--accent);
    color: #06281d;
    border: none;
    padding: 14px 22px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 1rem;
    cursor: pointer;
  }
  button#analyze-btn:disabled { opacity: 0.6; cursor: default; }
  .hint {
    text-align: center; color: var(--muted); font-size: 0.82rem; max-width: 640px;
    margin: 0 auto 32px;
  }

  #status { text-align: center; color: var(--muted); margin: 24px 0; min-height: 1.2em; }
  #error-box {
    display: none;
    max-width: 640px; margin: 16px auto; padding: 14px 18px;
    background: #2a1418; border: 1px solid #5c2530; border-radius: 10px;
    color: var(--bad); font-size: 0.92rem;
  }

  #report { display: none; }

  .verdict-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 28px;
    margin-bottom: 18px;
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
  .verdict-text { color: var(--text); line-height: 1.55; font-size: 0.98rem; }

  .disclaimer {
    max-width: 100%;
    background: #1c1710; border: 1px solid #3d2f14; color: var(--warn);
    border-radius: 10px; padding: 12px 16px; font-size: 0.82rem; line-height: 1.5;
    margin-bottom: 22px;
  }

  .cards-row {
    display: flex; gap: 14px; overflow-x: auto; padding-bottom: 6px; margin-bottom: 22px;
  }
  .dim-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    padding: 18px; flex: 1 1 0; min-width: 180px;
  }
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
    padding: 16px 18px; flex: 1 1 260px;
  }
  .extra-card h3 { margin: 0 0 8px; font-size: 0.85rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }
  .extra-card p, .extra-card li { font-size: 0.88rem; line-height: 1.5; margin: 0 0 4px; }
  .extra-card ul { margin: 0; padding-left: 18px; }

  footer { text-align: center; color: var(--muted); font-size: 0.78rem; margin-top: 40px; }
  footer a { color: var(--accent-2); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Crypto Sentiment ASP</h1>
    <p>Paste a ticker or a contract address. We auto-detect the chain and score sentiment across 5 dimensions.</p>
  </header>

  <div class="search-box">
    <input id="token-input" type="text" placeholder="e.g. SOL, or a contract address (0x... / Solana mint)" autocomplete="off">
    <button id="analyze-btn">Analyze</button>
  </div>
  <div class="hint">Established coins are resolved via CoinGecko. New / DEX-only tokens are resolved via GeckoTerminal with the chain auto-detected.</div>

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
      <div class="verdict-text" id="r-verdict"></div>
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

  <footer>
    A2MCP endpoint for OKX.AI &middot; POST /sentiment &middot; <a href="/docs">API docs</a>
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
