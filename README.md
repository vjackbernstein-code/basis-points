# Basis Points

An automated small-cap growth rating system. Two Python programs
(`pipeline.py` collects inputs; `smallcap.py` is the model) maintain a scored
screen of U.S. small-cap growth companies and publish it as a static site.
News feeds and press-release wires are collected **purely as analysis
inputs** matched against the small-cap universe — there is no client-facing
news product; the rating system is the product.

## What it produces

| File | What it is |
|---|---|
| `site/index.html` (also served as `smallcap.html`) | The product: ranked top-25 growth screen with factor breakdowns and flags, macro context strip, movers, earnings week, matched news & 8-K filings, economic calendar, live track record |
| `data/latest.json` | Structured data from the run |
| `data/smallcap.json`, `data/screen_log.json` | The rolling scorecard and the forward evaluation log |

## Running it

```bash
python3 pipeline.py
```

That's the whole thing. Open `site/index.html` in a browser to view.

To re-render without re-fetching (e.g. after editing commentary):

```bash
python3 pipeline.py --render-only
```

## Data sources

- **News & wires as signals** (RSS/Atom feeds — published for exactly this
  purpose): ~18 outlets (WSJ, FT, CNBC, MarketWatch, The Economist, NYT,
  Yahoo Finance, Seeking Alpha, Google News Business, crypto press, Fed press
  releases) plus corporate press-release wires (GlobeNewswire, PR Newswire)
  and the SEC's live filing streams. All of it is matched against the
  small-cap band: matched headlines appear in "In the news", and matched SEC
  filings drive three columns and row flags — **8-K** material events
  (`8-K` flag, 3d), **13D/13G** activist/5%+ stakes matched on the *subject*
  company (`act+` flag, 7d), and **S-1/424B** offerings as a dilution warning
  (`offer` flag, 7d). Activist filings use the `type=SC` prefix feed (the
  per-form filter is unreliable for schedules) and are sorted by form in
  `smallcap.record_filings`. Only headlines, links, and short feed-provided
  excerpts are used; everything links to the original publisher. Nothing here
  is scored — news and filings are context, not factors.
  (Business Wire was evaluated but its public RSS returns no usable headlines,
  so it is deferred; GlobeNewswire and PR Newswire cover the wire category.)
- **Market data**: Yahoo Finance chart API (primary), with automatic fallbacks
  to FRED (Federal Reserve official data), Frankfurter/ECB (foreign exchange),
  and CoinGecko (crypto). Fallback numbers carry an "as of" date when they are
  end-of-day rather than live.

Sources that fail simply drop out of that run — the site still renders.

## Scheduling options

**Option A — GitHub Actions + Pages (recommended; free, runs even when your
Mac is off):** push this folder to a GitHub repository, enable Pages
(Settings → Pages → Source: "GitHub Actions"). The included workflow
(`.github/workflows/update.yml`) refreshes the site every 30 minutes and
publishes it at `https://<username>.github.io/<repo>/`.

**Option B — local launchd job (Mac only, runs while the Mac is awake):**

```bash
cp ops/com.basispoints.update.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.basispoints.update.plist
```

To stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.basispoints.update.plist && rm ~/Library/LaunchAgents/com.basispoints.update.plist
```

Logs go to `data/launchd.log`.

## Small-cap growth screen (`smallcap.py`)

Universe from the SEC's public company list (keyless); measures from Finnhub.
**Model v3 (frozen Sep 5, 2026)** — eligibility: market cap $300M–$2B, listed
exchange (no OTC, no closed-end funds), one security per company (shortest
ticker = common stock), price ≥ $2, 10-day average volume ≥ 50k shares, TTM
revenue ≥ $50M (sub-floor names listed separately, unranked). Composite: 40%
growth (0.5 TTM revenue growth + 0.3 three-year growth + 0.2 acceleration,
ranked within industry group) + 40% volatility-scaled blended momentum + 20%
quality (graded cash-flow funding/runway, industry-relative leverage, margin
direction, dilution — self-measured from share-count history once old enough).
Publication requires positive TTM revenue growth; ≤5 names per industry group;
one-day churn penalty for newcomers; EV/Rev displayed but never scored; flags
for earnings proximity, newcomers, and net insider buying. Every trading day
the published screen plus IWO/IWM benchmark prices are logged to
`data/screen_log.json`; forward 1-week and 4-week cohort-vs-IWO returns
accumulate per model version (a live record, never backfilled), reporting both
daily and independent (non-overlapping) readings. Scoring is frozen until the
record holds 12 independent 1-week and 3 independent 4-week readings. The
free tier allows 60 calls/minute. **Cadence split (operational change,
Sep 6, 2026 — scoring untouched):** the news desk refreshes every 30 minutes,
but the scorecard takes its full ~550-call budget only ~3×/day (near 12:00,
17:00, 21:00 UTC ≈ pre-market/midday/post-close US time); every other run is
a ~40-call trickle that keeps the published screen's quotes fresh. Bootstrap
or catch-up (unprofiled companies, unmeasured band members) overrides to full
budget automatically. The scorecard (`data/smallcap.json`, committed between
cloud runs) bootstraps within the first day, then stays fresh on this rhythm.

## API keys (both optional; features light up when present)

| Key | Enables | Cloud (GitHub secret name) | Local file |
|---|---|---|---|
| Finnhub | small-cap scorecard, earnings calendar | `FINNHUB_API_KEY` | `data/finnhub.key` |
| FRED | economic calendar | `FRED_API_KEY` | `data/fred.key` |

Local key files are git-ignored and contain only the raw key text.

## Model v4 candidates (specified now, built only after the v3 freeze lifts)

The v3 scoring rules are frozen until the live record holds 12 independent
1-week and 3 independent 4-week readings. These candidates — each borrowed
from an established, documented approach — will be evaluated against that
record then, in this priority order:

1. **Earnings beat-streak** (Zacks-style estimate-revision proxy): did the
   company beat expectations the last 1–2 quarters? Free via Finnhub's
   earnings-surprises endpoint.
2. **Skip-week momentum + relative strength** (Jegadeesh–Titman / CAN SLIM):
   exclude the most recent week from the momentum window (short-term reversal),
   and measure returns relative to IWO rather than absolute.
3. **Accruals red-flag** (Sloan): earnings far above operating cash flow is a
   documented underperformance signal; we already hold both per-share figures.
4. **Gross-profitability level** (Novy-Marx): score margin *level*, not just
   direction, in the quality factor.
5. **Piotroski-style binary battery**: replace percentile quality ranks with
   summed pass/fail accounting checks (robust to outliers).
6. **Institutional sponsorship** (13F filings, free from SEC but heavy to
   parse): rising holder counts as a CAN SLIM-style "I" factor.

Shipped early because it is display-only and score-neutral: the CAN SLIM-style
market-context banner (the benchmark's own 13/26-week trend).

## Roadmap ideas

- Email delivery of the daily screen (needs a Buttondown/Mailchimp account)
- Sector distribution panel for the screen
- Scheduled Claude-written commentary in the private research notes

## Disclaimer

Basis Points is an automated aggregator for general information only — not
investment advice. Headlines and excerpts belong to their publishers. Market
data comes from public endpoints, may be delayed, and should be verified
before acting on it.
