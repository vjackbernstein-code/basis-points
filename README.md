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
(`.github/workflows/update.yml`) asks for a refresh every 30 minutes and
publishes it at `https://<username>.github.io/<repo>/`. **In practice GitHub
throttles frequent scheduled jobs: only ~6–7 runs actually fire per day**
(measured Sept 2026), i.e. a refresh every 3–4 hours, not every 30 minutes.
Budget the data work accordingly.

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
`data/screen_log.json`. Each cohort's forward return vs IWO is **frozen once**
when it reaches its horizon (`snapshot_readings`, stored as `read_1w` /
`read_4w` on the log entry) and never recomputed; `evaluate` then aggregates
every frozen reading for the current model version, reporting both the total
and the *independent* (non-overlapping) count. Scoring is frozen until the
record holds 12 independent 1-week and 3 independent 4-week readings.
*(Sep 16, 2026 fix: readings used to be recomputed each run from whatever
cohorts sat in the age window. That made the published number drift daily
without new evidence, and — because a window spans fewer days than the
independence gap — capped the independent count at 1 forever, making the
freeze criterion unsatisfiable.)* The
free tier allows 60 calls/minute.

**Budget allocation (operational, Sep 15, 2026 — scoring untouched).** Only
~6–7 runs fire per day (see the throttling note above), so the real budget is
~7 × 550 ≈ 3,900 calls/day. Spending priority per run:

1. quotes for current candidates (>4h stale);
2. quotes for tickers in screens published in the last 35 days — the cohorts
   the live track record must price forward, which the evaluation drops if
   their quotes go stale;
3. missing/outdated metrics and profiles for known band members;
4. **a reserved 55% of the run for discovering companies never profiled**,
   held *ahead* of routine upkeep;
5. routine quote upkeep for the rest of the band (>12h stale, oldest first);
6. leftover budget back to discovery; then insider data, slow metric refresh
   (3d) and slow profile refresh (48h blank / 7d band / 30d out-of-band).

A full-budget run is allowed whenever ≥2.5h have passed since the last one
(fixed clock hours were mostly missed by irregular firing); otherwise a
~40-call trickle. Bootstrap/catch-up always forces full.

*Why the reserve exists:* by mid-Sept 2026 the band had grown to ~1,100 names
and their 4-hourly quote upkeep consumed every call before discovery — which
is last in line — ever ran. Cataloguing collapsed to ~8 companies/day
(>1 year to finish). Relaxing band quotes to 12h and reserving a discovery
slice restored it to ~1–2 days.

## API keys (both optional; features light up when present)

| Key | Enables | Cloud (GitHub secret name) | Local file |
|---|---|---|---|
| Finnhub | small-cap scorecard, earnings calendar | `FINNHUB_API_KEY` | `data/finnhub.key` |
| FRED | economic calendar | `FRED_API_KEY` | `data/fred.key` |

Local key files are git-ignored and contain only the raw key text.

## Model v4 candidates (specified now, built only after the v3 freeze lifts)

**v3.1 (Sep 17, 2026)** corrected two scoring *defects* and restarted the
record: `industry_group` now maps the live "Communications" label (those
companies were ranked against the catch-all bucket rather than real peers),
and `_percentile_ranks` gives tied values the average of the ranks they span
(equal companies previously got unequal sub-scores decided by alphabetical
position). Both were done one week into the record on purpose — a scoring
correction costs a restart, and a restart is cheapest while the record is
young. Defects get fixed early; *opinion* changes (weights, new factors) still
wait for evidence.

The v3.1 scoring rules are frozen until the live record holds 12 independent
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

## Paper portfolios (`portfolio.py`) — SIMULATIONS

No money is ever involved. Ledger in `data/portfolio.json`, rendered on the
page under an unmistakable **SIMULATED** label.

**Five books run side by side** over the same screen, the same prices and the
same frictions, differing only in construction rules — because a single clever
portfolio can look good for reasons unrelated to its cleverness, and without a
plain control there is no way to tell:

| | Rules |
|---|---|
| **A Baseline** | equal weight, no stop, always fully invested — **the control** |
| **B Conviction** | weighted by score, capped 0.5x-2x equal weight |
| **C Risk-managed** | equal weight + sells a holding down `STOP_PCT` (20%) from entry |
| **D Regime-aware** | equal weight, exposure scaled by the small-cap tape (`REGIME_EXPOSURE`) |
| **E Combined** | B + C + D together |

Shared rules: hold the published top 25, rebalance weekly (Monday), charge
`COST_BPS` (25 bps) per side, never trade a name whose price is missing or
over 72h old, and restart every book when `MODEL_VERSION` changes.

Implementation notes worth keeping:
- **Marks before trades.** The book is valued at current prices *before* any
  sizing decision — sizing off stale marks mis-weights every position after a
  move.
- **Friction reserved from each slot**, so the last name bought is not left
  underweight.
- **The conviction cap is enforced after normalisation.** Clamping once and
  then renormalising pushes the top name back over its cap, making the
  advertised limit false; weights are clamped and redistributed until they
  both respect the bounds and sum to 1.
- **Stops are checked every day**, not only on rebalance days — a weekly stop
  would be fiction.

### Tracking progress (the panel at the top of the page)

`render_progress()` in `pipeline.py` is the tracker. It answers one question —
*how far has the experiment actually got* — and it answers it in **evidence**,
not profit:

- **independent readings against the freeze bar**, `smallcap.FREEZE_TARGET`
  (12 at one week, 3 at four weeks). Independent means non-overlapping; the
  larger total-readings count is deliberately not the number on the bar.
- **days to `smallcap.FREEZE_REVIEW_DATE`** (2026-12-14), the scheduled review.
- **the control book's return, and the best overlay's margin over it**, with
  the margin labelled as the highest of several and therefore upward-biased.
- a milestone checklist ending in an outcome that is not assumed to be
  favourable: *trade it, change it, or abandon it*.

The rule this panel exists to enforce: the headline number is a reading count,
not a return. A book up 20% after three weeks moves no bar on this page.

Two further honesty fixes live alongside it. The per-book **path** sparklines
share one vertical scale (`spark_svg(..., lo, hi)`) — scaled individually, a
book that moved 0.4% and one that moved 24% draw the identical picture, which
is exactly the comparison a column of them invites. And **retired books are
printed**: a model change restarts the ledger, and a restart that silently
dropped its bad run would leave a record made only of good stretches.

### The site's shape

Each section is **its own page**, with a tab bar across the top:

| Page | What it holds |
|---|---|
| `index.html` | Progress — the return chart and the evidence tracker |
| `portfolios.html` | The five simulated books |
| `screen.html` | Today's ranked screen |
| `market.html` | Market backdrop (context only, never scored) |
| `changes.html` | What entered and left the screen, and what the books did |
| `signals.html` | Filings, earnings, news matched to the band |
| `method.html` | The rules in full |

`smallcap.html` was the original address and still resolves, to the same
content as `index.html`, so old links do not break.

The tabs are **ordinary links**, not JavaScript. The site runs none, so this is
not a workaround but the better form: every section has an address that can be
linked, bookmarked and reached with the back button.

`build_sections()` returns only sections that produced content, and the tab bar
is built from that same set — **a tab can never offer a page that was not
written**. Pages whose sections fall silent are deleted, so the public cannot
reach a page the tabs no longer link to. Both are covered by tests.

Portfolios comes before the screen that feeds it: the books are the subject of
the experiment, the screen is one of its inputs.

`equity_chart()` draws the headline return chart at the top of the progress
panel — all five books plus the benchmark, each **rebased so its own start is
0%**. Rebasing is what makes six lines comparable at a glance; plotting dollars
would let a book that began later look like an outperformer purely because it
started somewhere else. Three things it must keep doing:

- **an empty record renders as empty**, never as a flat line at zero, which
  would read as a result;
- **the benchmark's legend swatch is dashed** because its line is dashed — a
  legend that does not match its chart is a legend to be checked twice;
- **axis labels sit inside the plot and grow on small screens.** The chart is
  scaled to roughly 40% of its authored width on a phone, where 11px text
  renders at about four real pixels.

### Staleness: what a static page can and cannot tell you

`staleness_alerts()` raises a banner — on **every** page, above the tabs — when
an input is older than `FRESH_LIMITS` allows, stating what each stale input
breaks rather than just its age. `render_freshness()` shows every input's age
on the Method page **always**, not only on failure: a panel that appears only
when something is wrong teaches nobody what normal looks like.

**The honest limit, stated on the page itself:** a static page is written once
and served unchanged until the next run, so it cannot know how long it has been
sitting in front of a reader, and nothing on it can detect that publishing has
*stopped*. What it can report is the age of the data it was BUILT from — and
that is where this system's characteristic failure actually lives: the job
keeps running and publishing on schedule while a source behind it has been
failing for days. Detecting a stopped job is the watchdog agent's work, not the
page's.

### Per-book pages (`site/book/<KEY>.html`)

One page per simulated book, reached from its name in the comparison table.
Each shows the rules it actually runs (derived from its spec, never restated by
hand), its own curve against the benchmark, and a holdings table with **weight,
average cost, return, where its stop sits and how much room is left** before
that stop fires. Conviction sizing becomes visible here: book B spans 5.99%
down to 2.35% where book A sits flat near 4%.

Each non-control page prints its gap against book A and says the gap is noise
at this length — stated up front so it cannot be quietly dropped later if it
turns out unflattering.

Two honesty fixes these pages forced:

- **Cost basis is re-averaged on top-ups.** `entry_px` used to stay at the
  first purchase, so buying at 10 and topping up at 20 reported +100% on a
  position sitting at 20. Selling part of a position still leaves the basis
  alone — the remaining shares keep theirs.
- **`friction_yr` is not shown as a rate on a short record** (`FRICTION_MIN_DAYS`,
  45). One rebalance three days in annualises to "48.5%/yr", a number nobody
  will ever pay printed beside real ones. Below the threshold the page reports
  what was actually spent.

### Per-company pages (`site/co/<TICKER>.html`)

One page per name on the screen, linked from its ticker. Each shows the
arithmetic that produced its treatment rather than a conclusion about it:

- the company **strictly from fetched fields**, captioned as such. There is no
  narrative business summary, and there must not be — nothing in this system
  reads about the business, so a paragraph that sounded like it had would be
  the most misleading thing on the page.
- each score part with the company's own inputs, plus the reminder that a mark
  is a percentile against *today's* eligible set and moves when the company
  does not.
- its weight in each book, with the rank-to-weight ramp spelled out.
- its stop, derived in four visible steps from its own volatility.

**These pages CALL `portfolio.target_weights()` and `portfolio.stop_distance()`
rather than restating their formulas.** A page that restated them would drift
from the code the moment either changed and would then be describing a system
that no longer exists. A test asserts page and code agree.

Stale pages are deleted each run — a public URL nobody revisits is where a
wrong number survives longest.

### Weekly decision review (agent, read-only)

`trig_013uXmZ6NT1i2GKzCaaTLBKm`, Saturdays 14:00 UTC, Opus, read-only. It
reviews the week's simulated decisions and sorts every finding into one of two
buckets — the distinction is the whole point of it:

| Bucket | Example | What happens |
|---|---|---|
| **Defect** — *this number is wrong* | stop computed off a stale volatility; a page claiming a rule the code does not follow; a figure in the wrong unit | fix now, no reason to wait |
| **Calibration** — *this number should be different* | "the stop multiple should be 2.5"; "momentum should weigh less" | recorded, **parked** until the December review |

The second bucket is parked because a model tuned while its own record is being
written will always look good and will always be lying: some tweak can always
be found that improves the record so far. The freeze covers the *scoring rules*
only — defects, honesty of the published numbers, risk-control correctness and
the site have never been frozen and should keep moving.

It is told there is **no per-company reasoning to review** — sizing is rank
alone, stops are the name's own volatility — so that it audits the *inputs* and
the *outputs* of those mechanical rules rather than inventing a judgement to
critique. It is also told that a week with nothing wrong is a good result: a
review that always finds something is a review nobody can trust.

### Honesty notes

- **Simulated stops flatter themselves.** Prices are sampled a few times a
  day, not continuously, so a simulated stop assumes an exit near the stop
  price while a real one gaps through. `STOP_SLIPPAGE_BPS` (75) charges extra,
  but books C and E should be read as an upper bound, not an estimate.
- **Observed:** with realistic score gaps the conviction tilt only spans about
  0.87x-1.13x equal weight — the top scores are bunched, so B is close to A by
  construction. Worth remembering before attributing any difference to skill.
- Simulations omit what hurts real traders most: the market moving against a
  real order, borrow costs, tax, and the discipline to follow a system through
  a drawdown. Hypothetical results are not a track record, and nothing here is
  investment advice.

## Security posture (reviewed Sep 16, 2026)

This system republishes text written by strangers, so every feed field is
treated as hostile input.

- **Escaping**: every external string reaching the page goes through `esc()`
  (`html.escape(quote=True)`), attribute contexts included.
- **Links**: `safe_link()` allowlists `http`/`https` at ingest. Escaping alone
  is *not* sufficient here — `html.escape` turns the quotes in
  `javascript:fetch('…')` into entities that the browser decodes back before
  using the address, so the payload survives. The scheme must be checked.
- **Content-Security-Policy**: the page emits `default-src 'none'` (plus the
  Google-Fonts style/font origins). It runs no JavaScript at all, so this
  costs nothing and neutralises any future escaping regression.
- **Input ceilings**: responses capped at 8 MB, gzip expansion at 32 MB
  (incremental, so a compression bomb is refused rather than OOM-ing the
  runner), feed titles at 300 chars (which also bounds the filing-title regex).
- **Vendor type confusion**: numeric/text fields are coerced at the cache
  boundary (`_num`, `_txt`) and `in_band` type-checks, because bad values get
  *committed* and would otherwise raise on every later run.
- **Dates**: validated at ingest (FRED calendar, Finnhub earnings) and the
  render is fenced, so one malformed vendor date degrades a column instead of
  aborting the publish.
- **Secrets**: `data/` is a git **allowlist** (only the three state files ship)
  so a future credential file cannot be swept in by `git add -A`; error strings
  are scrubbed of key material before being written to the public JSON.
- **Agents**: the scheduled watchdog's prompt declares repository data as
  untrusted third-party text and forbids treating it as instructions — the
  ingested newswires are self-serve, so a headline is attacker-controlled.

Not done deliberately: Actions are pinned to major tags rather than commit
SHAs (tedious, low value for a personal project).

## Roadmap ideas

- Email delivery of the daily screen (needs a Buttondown/Mailchimp account)
- Sector distribution panel for the screen
- Scheduled Claude-written commentary in the private research notes

## Disclaimer

Basis Points is an automated aggregator for general information only — not
investment advice. Headlines and excerpts belong to their publishers. Market
data comes from public endpoints, may be delayed, and should be verified
before acting on it.
