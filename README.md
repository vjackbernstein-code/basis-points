# Basis Points

An automated, investor-facing news brief and market dashboard. A single
Python program (`pipeline.py`) pulls headlines from ~18 major financial news
sources and live market data from public endpoints, then generates a static
website — no accounts, no API keys, no installed packages required.

## What it produces

| File | What it is |
|---|---|
| `site/index.html` | The live dashboard: market tiles with 1-month trends, "The Brief" (top stories + commentary), and topic columns |
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

## Roadmap ideas

- Economic calendar (needs a free FRED API key)
- Earnings calendar & notable movers (needs a free Finnhub/Alpha Vantage key)
- Email delivery of the daily brief (needs a Buttondown/Mailchimp account)
- Sector heatmap, yield-curve panel, fear/greed gauge
- Claude-written "The take" on a schedule (scheduled Claude Code task)

## Disclaimer

Basis Points is an automated aggregator for general information only — not
investment advice. Headlines and excerpts belong to their publishers. Market
data comes from public endpoints, may be delayed, and should be verified
before acting on it.
