# Basis Points

An automated, investor-facing news brief and market dashboard. A single
Python program (`pipeline.py`) pulls headlines from ~18 major financial news
sources and live market data from public endpoints, then generates a static
website — no accounts, no API keys, no installed packages required.

## What it produces

| File | What it is |
|---|---|
| `site/index.html` | The live dashboard: market tiles with 1-month trends, "The Brief" (top stories + commentary), and topic columns |
| `site/smallcap.html` | The small-cap growth screen: ranked top-25 (transparent composite score), movers, earnings this week, news mentions |
| `site/archive/YYYY-MM-DD.html` | A daily snapshot of the brief |
| `site/archive/index.html` | Archive listing |
| `site/artifact.html` | Same page formatted for claude.ai Artifact publishing |
| `data/latest.json` | All structured data from the run (for tooling/commentary) |

## Running it

```bash
python3 pipeline.py
```

That's the whole thing. Open `site/index.html` in a browser to view.

To re-render without re-fetching (e.g. after editing commentary):

```bash
python3 pipeline.py --render-only
```

## Editorial commentary ("The take")

If `data/take.md` exists and was modified within the last 24 hours, its
paragraphs render in the brief as **The take**. Otherwise an auto-generated
"Signal scan" (theme frequency analysis) appears instead. This is how a human
or Claude session adds real analysis on top of the automated aggregation.

## Data sources

- **Headlines** (RSS/Atom feeds — published by the outlets for exactly this
  purpose): CNBC, MarketWatch, WSJ, FT, The Economist, NYT Business/DealBook,
  Yahoo Finance, Seeking Alpha, Google News Business, CoinDesk, Cointelegraph,
  Federal Reserve press releases, SEC EDGAR 8-K filings.
  Only headlines, links, and short feed-provided excerpts are used; everything
  links to the original publisher.
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
free tier allows 60 calls/minute, so each run spends a ~550-call budget over
~10 minutes; the scorecard (`data/smallcap.json`, committed between cloud
runs) bootstraps within the first day, then stays fresh on rolling updates.

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

- Email delivery of the daily brief (needs a Buttondown/Mailchimp account)
- Sector heatmap, yield-curve panel, fear/greed gauge
- Claude-written "The take" on a schedule (scheduled Claude Code task)
- Junk-filter for Google News local-news strays

## Disclaimer

Basis Points is an automated aggregator for general information only — not
investment advice. Headlines and excerpts belong to their publishers. Market
data comes from public endpoints, may be delayed, and should be verified
before acting on it.
