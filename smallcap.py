#!/usr/bin/env python3
"""
Small-cap growth scorecard for Basis Points — model v2.

Universe: the SEC's public list of all listed U.S. companies (keyless).
Measures: Finnhub free tier (politely rate-limited). State: data/smallcap.json
and data/screen_log.json, committed between cloud runs by the workflow.

Model v2 (fixed rules, disclosed on the page; not investment advice):

  Eligibility   market cap $300M-$2B; listed exchange (no OTC); price >= $2;
                10-day average volume >= 50k shares; trailing-12-month revenue
                >= $50M (revenue-per-share x shares outstanding). Names failing
                only the revenue floor are shown separately, unranked.

  Growth (40%)  0.7 x trailing-12-month revenue growth
                + 0.3 x acceleration (latest quarter's yoy growth minus TTM)

  Momentum (40%)  blended price return (0.6 x 13-week + 0.4 x 26-week),
                divided by 3-month volatility (annualized daily std, floor 15)
                so one violent spike doesn't dominate

  Quality (20%) 0.5 x funding (self-funded if operating cash flow positive,
                else cash runway in months, capped at 36)
                + 0.3 x margin direction (gross margin TTM minus last FY;
                operating margin as fallback)
                + 0.2 x low dilution (5-year gap between total revenue growth
                and per-share revenue growth)

  Each factor is percentile-ranked within the eligible set; missing
  sub-measures fall to a neutral 0.5 rank. Composite = 100 x weighted rank.

  Publication   at most 5 names per industry in the top 25; a name absent
                from the previous run's top-40 candidates carries a 3% score
                penalty for one day (reduces churn). Flags: "new" (entered
                the published list today), "E-Nd" (reports earnings in N
                days), "ins+" (net insider open-market purchases, last 30d).

  Evaluation    every run logs the published screen and an IWM (Russell 2000
                ETF) benchmark price; forward 1-week and 4-week cohort
                returns vs the benchmark accumulate on the page as the log
                ages. A live track record, not a backtest.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE_PATH = BASE / "data" / "smallcap.json"
LOG_PATH = BASE / "data" / "screen_log.json"

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
UA = "BasisPointsAggregator/1.0 (personal research project)"
FINNHUB = "https://finnhub.io/api/v1"

MCAP_MIN, MCAP_MAX = 300.0, 2000.0      # $ millions
PX_MIN = 2.0                            # dollars
ADV_MIN = 0.05                          # 10-day avg volume, millions of shares
REV_FLOOR = 50.0                        # $ millions, trailing 12 months
VOL_FLOOR = 15.0                        # volatility floor for momentum scaling
SCREEN_SIZE = 25
CANDIDATES = 40
SECTOR_CAP = 5
NEWCOMER_PENALTY = 0.97
DEFAULT_BUDGET = 550
CALL_BUDGET = int(os.environ.get("SMALLCAP_BUDGET", str(DEFAULT_BUDGET)))
CALL_INTERVAL = 1.1                     # seconds between Finnhub calls (55/min)

# Quote staleness thresholds. The screen is judged daily, not intraday, so a
# half-day-old price is harmless for the wider band; only the published
# candidates and the cohorts the track record must price are kept tighter.
QUOTE_STALE_SCREEN_H = 4
QUOTE_STALE_COHORT_H = 12
QUOTE_STALE_BAND_H = 12
COHORT_LOOKBACK_DAYS = 35               # covers the 4-week evaluation window
DISCOVERY_RESERVE = 0.55                # share of a run held for new companies
                                        # while the market map is incomplete

# Cadence (operational only — never touches scoring). NOTE: the workflow asks
# for a run every 30 minutes, but GitHub throttles frequent scheduled jobs and
# in practice only ~6-7 fire per day (measured Sept 2026), so the real daily
# budget is ~7 runs' worth. A full run is therefore allowed whenever enough
# time has passed since the last one, rather than on fixed clock hours which
# irregular firing would mostly miss. Bootstrap/catch-up always overrides full.
HOURS_BETWEEN_FULL = 2.5
TRICKLE_BUDGET = 40
BENCHES = ("IWO", "IWM")                # Russell 2000 Growth (primary) + Russell 2000
MODEL_VERSION = "v3.1"                  # stamped on log entries; the track record is
                                        # reported per version, never blended.
                                        # v3.1 (Sep 17, 2026): two scoring
                                        # DEFECTS corrected — "Communications"
                                        # companies were ranked against no peer
                                        # group, and tied factor values were
                                        # ranked by alphabetical position. Done
                                        # now, one week into the record, because
                                        # a correction costs a restart and a
                                        # restart is cheapest while the record
                                        # is young.
# label, min age, max age (days) at which a cohort's reading is frozen, and how
# far apart two readings must be to count as independent evidence.
HORIZONS = (("1w", 6, 9, 7), ("4w", 25, 31, 28))
MIN_PRICEABLE = 20                      # names a cohort must still be able to
                                        # price for its reading to be trusted
# What a published name is assumed to have lost when it vanishes from the data
# entirely. A convention, not a measurement — but every alternative is worse:
# excluding it silently biases the record UPWARD exactly when a holding fails,
# and in small caps failure is the usual reason a name disappears.
DELIST_ASSUMED_LOSS = 0.60
# Carrying stale prices keeps names in the average, but a reading built mostly
# from an old sweep is re-measuring yesterday. Require this many genuinely
# fresh prices before freezing one. Skipping is safe: the horizon window is
# several days wide, so the reading is simply retried tomorrow.
MIN_FRESH = 15
# The freeze, stated as numbers the page can render rather than only as prose:
# scoring does not change until the live record holds this many INDEPENDENT
# (non-overlapping) readings. The total reading count is always larger and is
# deliberately not the number that counts.
FREEZE_TARGET = {"1w": 12, "4w": 3}
FREEZE_REVIEW_DATE = "2026-12-14"       # the scheduled review of that evidence
MIN_GROUP = 8                           # industry-relative ranks need this many peers

# Coarse industry groups: vendor tags are fragmented ("Banking" vs "Financial
# Services"), so the sector cap and industry-relative ranks use these instead.
INDUSTRY_GROUPS = [
    ("Financials",  ("bank", "financial", "insurance", "capital market", "credit", "thrift")),
    ("Health",      ("biotech", "pharma", "health", "life science", "medical")),
    # "Communications" is a live vendor label that matched nothing before, so
    # those companies fell into "Other" and were never ranked against peers
    ("Telecom",     ("telecom", "communication")),
    ("Technology",  ("software", "technology", "semiconductor", "internet",
                     "electronic", "computer")),
    ("Energy",      ("energy", "oil", "gas", "coal", "pipeline")),
    ("Materials",   ("chemical", "metal", "mining", "paper", "packaging")),
    ("Industrials", ("machin", "aerospace", "defense", "industrial", "construction",
                     "engineer", "transport", "airline", "marine", "road", "rail",
                     "commercial service", "professional service", "electrical",
                     "building", "trading companies")),
    ("Consumer",    ("retail", "consumer", "hotel", "restaurant", "leisure", "textile",
                     "apparel", "auto", "household", "food", "beverage", "tobacco",
                     "media", "entertainment", "distributor")),
    ("Real Estate", ("real estate", "reit")),
    ("Utilities",   ("utilit",)),
]


def industry_group(ind):
    low = (ind or "").lower()
    for group, words in INDUSTRY_GROUPS:
        if any(w in low for w in words):
            return group
    return "Other"


def _name_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())[:24]


def read_key(env_name, file_name):
    """API key from the environment (cloud) or a git-ignored local file."""
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    path = BASE / "data" / file_name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def _age_h(iso):
    if not iso:
        return 1e9
    return (_now() - datetime.fromisoformat(iso)).total_seconds() / 3600


class Finnhub:
    """Minimal, politely rate-limited Finnhub client."""

    def __init__(self, key):
        self.key = key
        self.calls = 0
        self._last = 0.0

    def get(self, path, **params):
        wait = CALL_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        params["token"] = self.key
        url = f"{FINNHUB}/{path}?{urllib.parse.urlencode(params)}"
        for attempt in (0, 1):
            self._last = time.monotonic()
            self.calls += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    time.sleep(4)
                    continue
                raise


# ------------------------------------------------------------- state ---------


def load_cache():
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    else:
        cache = {}
    cache.setdefault("universe", {})
    cache.setdefault("universe_fetched", None)
    for k in ("profiles", "metrics", "quotes", "insider", "earn_map"):
        cache.setdefault(k, {})
    cache.setdefault("earnings", [])
    cache.setdefault("earnings_fetched", None)
    cache.setdefault("last_screen", [])
    cache.setdefault("bench", {})
    return cache


def save_cache(cache):
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, separators=(",", ":")),
                          encoding="utf-8")


def load_log():
    if LOG_PATH.exists():
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    return {}


def save_log(log):
    LOG_PATH.write_text(json.dumps(log, separators=(",", ":")), encoding="utf-8")


def refresh_universe(cache):
    """SEC master ticker list, refreshed weekly. Keyless."""
    if cache["universe"] and _age_h(cache.get("universe_fetched")) < 24 * 7:
        return
    req = urllib.request.Request(SEC_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    universe = {}
    for row in raw.values():
        t = row.get("ticker", "")
        # common shares only: skip units/warrants/preferreds (dashed or long tickers)
        if t and t.isalpha() and len(t) <= 5:
            universe[t] = row.get("title", "").title()[:60]
    cache["universe"] = universe
    cache["universe_fetched"] = _iso()


# ------------------------------------------------------------- fetch ---------


def _num(v):
    """Coerce an untrusted vendor field to a number, or None.

    These values are written into the scorecard and committed, so a string
    where a number belongs would persist across runs and raise on every later
    one — silently wiping the entire screen behind a one-line warning.
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _txt(v, limit):
    """Same idea for text fields: never slice something that isn't a string."""
    return (v if isinstance(v, str) else "" if v is None else str(v))[:limit]


def in_band(profile):
    if not profile:
        return False
    mcap = profile.get("mcap")
    if isinstance(mcap, bool) or not isinstance(mcap, (int, float)) or not mcap:
        return False
    exch = profile.get("exch")
    exch = exch if isinstance(exch, str) else ""
    return MCAP_MIN <= mcap <= MCAP_MAX and "OTC" not in exch.upper()


def _fetch_profile(fh, cache, ticker):
    try:
        p = fh.get("stock/profile2", symbol=ticker)
    except Exception:  # noqa: BLE001 — record the attempt; retry on schedule
        p = {}
    prev = cache["profiles"].get(ticker) or {}
    entry = {
        "mcap": _num(p.get("marketCapitalization")),
        "shares": _num(p.get("shareOutstanding")),
        "exch": _txt(p.get("exchange"), 40),
        "ind": _txt(p.get("finnhubIndustry"), 64),   # long enough that no
        #                          industry keyword can fall past the cut
        "name": _txt(p.get("name"), 60) or _txt(cache["universe"].get(ticker), 60),
        "t": _iso(),
    }
    # own share-count history (dilution measurement improves as this grows)
    hist = list(prev.get("shist") or [])
    if entry["shares"]:
        day = _now().strftime("%Y-%m-%d")
        if not hist or hist[-1][0] != day:
            hist.append([day, round(entry["shares"], 3)])
        hist = hist[-8:]
    entry["shist"] = hist
    cache["profiles"][ticker] = entry


def _fetch_metrics(fh, cache, ticker):
    try:
        m = fh.get("stock/metric", symbol=ticker, metric="all").get("metric", {})
    except Exception:  # noqa: BLE001
        m = {}
    cache["metrics"][ticker] = {
        "rev_g": m.get("revenueGrowthTTMYoy"),
        "rev_gq": m.get("revenueGrowthQuarterlyYoy"),
        "r13": m.get("13WeekPriceReturnDaily"),
        "r26": m.get("26WeekPriceReturnDaily"),
        "vol": m.get("3MonthADReturnStd"),
        "hi52": m.get("52WeekHigh"),
        "adv": m.get("10DayAverageTradingVolume"),
        "rps": m.get("revenuePerShareTTM"),
        "cfps": m.get("cashFlowPerShareTTM"),
        "cashps": m.get("cashPerSharePerShareQuarterly"),
        "gm_t": m.get("grossMarginTTM"),
        "gm_a": m.get("grossMarginAnnual"),
        "om_t": m.get("operatingMarginTTM"),
        "om_a": m.get("operatingMarginAnnual"),
        "rg5": m.get("revenueGrowth5Y"),
        "rsg5": m.get("revenueShareGrowth5Y"),
        "rg3": m.get("revenueGrowth3Y"),
        "dte": m.get("totalDebt/totalEquityQuarterly"),
        "ev_rev": m.get("evRevenueTTM"),
        "t": _iso(),
    }


def _fetch_quote(fh, cache, ticker):
    try:
        q = fh.get("quote", symbol=ticker)
    except Exception:  # noqa: BLE001
        q = {}
    cache["quotes"][ticker] = {
        "px": q.get("c") or None,
        "dp": q.get("dp"),
        "t": _iso(),
    }


def _fetch_insider(fh, cache, ticker):
    """Net open-market insider purchases (code P), last 30 days."""
    try:
        rows = fh.get("stock/insider-transactions", symbol=ticker).get("data", [])
    except Exception:  # noqa: BLE001
        rows = []
    floor = (_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    net_p = 0
    for r in rows:
        if (r.get("transactionCode") == "P"
                and (r.get("transactionDate") or "") >= floor):
            net_p += r.get("change") or 0
    cache["insider"][ticker] = {"net30": net_p, "t": _iso()}


def refresh_earnings(fh, cache):
    if cache.get("earn_map") and _age_h(cache.get("earnings_fetched")) < 12:
        return
    start = _now().strftime("%Y-%m-%d")
    end = (_now() + timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        cal = fh.get("calendar/earnings", **{"from": start, "to": end})
        rows = cal.get("earningsCalendar", [])
    except Exception:  # noqa: BLE001
        return
    earn_map, keep = {}, []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym or not in_band(cache["profiles"].get(sym)):
            continue
        try:                    # this date is cached AND committed, so a bad
            datetime.fromisoformat(r.get("date") or "")   # one would persist
        except (TypeError, ValueError):                   # and break rendering
            continue
        earn_map[sym] = r["date"]
        keep.append({"date": r["date"], "ticker": sym,
                     "name": cache["profiles"][sym].get("name") or sym,
                     "hour": r.get("hour") or ""})
    keep.sort(key=lambda r: (r["date"], r["ticker"]))
    cache["earn_map"] = earn_map
    cache["earnings"] = keep[:12]
    cache["earnings_fetched"] = _iso()


def _regime_label(r13, r26):
    if r13 <= -8:
        return "correction"
    if r13 < 2:
        return "flat/choppy"
    return "uptrend" if (r26 or 0) > 0 else "early rebound"


def refresh_regime(fh, cache):
    """Market-context banner data: the benchmark's own trend. Display-only —
    never touches scores (a CAN SLIM-style 'M' as context, not a gate)."""
    if cache.get("regime") and _age_h(cache["regime"].get("t")) < 12:
        return
    try:
        m = fh.get("stock/metric", symbol=BENCHES[0], metric="all").get("metric", {})
    except Exception:  # noqa: BLE001
        return
    r13 = m.get("13WeekPriceReturnDaily")
    if r13 is None:
        return
    r26 = m.get("26WeekPriceReturnDaily")
    cache["regime"] = {"r13": r13, "r26": r26,
                       "label": _regime_label(r13, r26), "t": _iso()}


def refresh_bench(fh, cache):
    out = {}
    for sym in BENCHES:
        try:
            q = fh.get("quote", symbol=sym)
            if q.get("c"):
                out[sym.lower()] = q["c"]
        except Exception:  # noqa: BLE001
            pass
    if out:
        out["t"] = _iso()
        cache["bench"] = out


# ------------------------------------------------------------- model ---------


def rev_ttm(cache, ticker):
    """Trailing-12-month revenue in $ millions, or None if not computable."""
    m = cache["metrics"].get(ticker) or {}
    p = cache["profiles"].get(ticker) or {}
    rps, shares = m.get("rps"), p.get("shares")
    # a genuine zero must survive as 0.0, not be read as "unknown" — treating
    # it as unknown made pre-revenue companies vanish from the page entirely:
    # not scored, and not listed under the revenue floor either
    if rps is None or shares is None:
        return None
    try:
        return float(rps) * float(shares)
    except (TypeError, ValueError):
        return None


def _base_eligible(cache, ticker):
    """Everything except the revenue floor."""
    p = cache["profiles"].get(ticker)
    m = cache["metrics"].get(ticker)
    q = cache["quotes"].get(ticker)
    if not (in_band(p) and m and q and q.get("px")):
        return False
    if (p.get("ind") or "").strip() in ("", "N/A"):
        return False    # closed-end funds and shells carry no industry tag
    return (q["px"] >= PX_MIN
            and (m.get("adv") or 0) >= ADV_MIN
            and m.get("rev_g") is not None
            and m.get("r13") is not None)


def _eligible(cache, ticker):
    rt = rev_ttm(cache, ticker)
    return _base_eligible(cache, ticker) and rt is not None and rt >= REV_FLOOR


def below_floor(cache):
    """Names passing every filter except the $50M revenue floor."""
    out = []
    for t in cache["metrics"]:
        if not _base_eligible(cache, t):
            continue
        rt = rev_ttm(cache, t)
        if rt is not None and rt < REV_FLOOR:
            m, p = cache["metrics"][t], cache["profiles"][t]
            out.append({"ticker": t, "name": p.get("name") or t,
                        "rev_ttm": rt, "rev_g": m.get("rev_g"),
                        "r13": m.get("r13")})
    out.sort(key=lambda r: -(r["r13"] or -999))
    return out[:8]


def _percentile_ranks(values):
    """Ranks in [0,1]; None values sit at a neutral 0.5.

    Tied values share the average of the ranks they span. Otherwise two
    companies with identical factor values received materially different
    sub-scores decided by nothing but their alphabetical position.
    """
    known = sorted(((v, i) for i, v in enumerate(values) if v is not None),
                   key=lambda x: x[0])
    ranks = [0.5] * len(values)
    n = len(known)
    if n < 2:
        return ranks
    pos = 0
    while pos < n:
        end = pos
        while end + 1 < n and known[end + 1][0] == known[pos][0]:
            end += 1
        shared = ((pos + end) / 2) / (n - 1)
        for _v, i in known[pos:end + 1]:
            ranks[i] = shared
        pos = end + 1
    return ranks


def _factors(cache, ticker):
    m = cache["metrics"][ticker]
    q = cache["quotes"][ticker]
    p = cache["profiles"][ticker]
    # growth trio: trailing year, three-year persistence, acceleration
    g_ttm = max(-20.0, min(150.0, m["rev_g"]))
    g_3y = max(-20.0, min(100.0, m["rg3"])) if m.get("rg3") is not None else None
    accel = None
    if m.get("rev_gq") is not None:
        accel = max(-50.0, min(50.0, m["rev_gq"] - m["rev_g"]))
    # momentum, volatility-scaled
    r13 = max(-50.0, min(150.0, m["r13"]))
    r26 = m.get("r26")
    blended = 0.6 * r13 + 0.4 * max(-50.0, min(150.0, r26)) if r26 is not None else r13
    vol = max(VOL_FLOOR, m.get("vol") or VOL_FLOOR)
    momo = blended / vol
    # quality: graded funding — profitable names rank by cash-flow margin;
    # money-losers sit below them all, ordered by months of runway
    funding = None
    if m.get("cfps") is not None:
        if m["cfps"] >= 0:
            margin = m["cfps"] / m["rps"] if m.get("rps") else 0.0
            funding = max(0.0, min(0.6, margin))
        elif m.get("cashps"):
            runway = min(36.0, (m["cashps"] / -m["cfps"]) * 12)
            funding = runway / 36.0 - 1.05       # in [-1.05, -0.05]
        else:
            funding = -1.1
    # quality: margin direction (gross preferred, operating fallback)
    margin_dir = None
    if m.get("gm_t") is not None and m.get("gm_a") is not None:
        margin_dir = m["gm_t"] - m["gm_a"]
    elif m.get("om_t") is not None and m.get("om_a") is not None:
        margin_dir = m["om_t"] - m["om_a"]
    # quality: dilution — measured from our own share-count history once two
    # readings sit >= 60 days apart; the 5-year proxy until then
    dilution = None
    hist = p.get("shist") or []
    if len(hist) >= 2:
        (d0, s0), (d1, s1) = hist[0], hist[-1]
        days = (datetime.fromisoformat(d1) - datetime.fromisoformat(d0)).days
        if days >= 60 and s0:
            dilution = ((s1 / s0) ** (365.0 / days) - 1) * 100
    if dilution is None and m.get("rg5") is not None and m.get("rsg5") is not None:
        dilution = m["rg5"] - m["rsg5"]
    # display-only extras; a price above the recorded 52-week high is a stale
    # record (new listing), shown as unknown rather than an impossible number
    from_high = None
    if m.get("hi52") and q["px"] <= m["hi52"]:
        from_high = (q["px"] / m["hi52"] - 1) * 100
    return {
        "g_ttm": g_ttm, "g_3y": g_3y, "accel": accel, "momo": momo,
        "funding": funding, "dte": m.get("dte"), "margin_dir": margin_dir,
        "dilution": dilution, "from_high": from_high, "ev_rev": m.get("ev_rev"),
        "px": q["px"], "dp": q.get("dp"),
        "group": industry_group(p.get("ind")),
    }


def _grouped_ranks(values, groups):
    """Percentile ranks within industry group (>= MIN_GROUP members), else global."""
    out = _percentile_ranks(values)
    by_group = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    for idxs in by_group.values():
        if len(idxs) >= MIN_GROUP:
            sub = _percentile_ranks([values[i] for i in idxs])
            for j, i in enumerate(idxs):
                out[i] = sub[j]
    return out


def _dedupe_by_company(cache, tickers):
    """One security per company: the shortest ticker (the common stock)."""
    by_name = {}
    for t in tickers:
        key = _name_key(cache["profiles"][t].get("name") or t)
        best = by_name.get(key)
        if best is None or (len(t), t) < (len(best), best):
            by_name[key] = t
    return sorted(by_name.values())


def compute_screen(cache, prev_candidates=None, prev_published=None):
    """Returns (published top-25, candidate top-40)."""
    tickers = _dedupe_by_company(
        cache, [t for t in cache["metrics"] if _eligible(cache, t)])
    if not tickers:
        return [], []
    f = {t: _factors(cache, t) for t in tickers}
    groups = [f[t]["group"] for t in tickers]
    g1 = _grouped_ranks([f[t]["g_ttm"] for t in tickers], groups)
    g3 = _grouped_ranks([f[t]["g_3y"] for t in tickers], groups)
    ga = _grouped_ranks([f[t]["accel"] for t in tickers], groups)
    mo = _percentile_ranks([f[t]["momo"] for t in tickers])
    q1 = _percentile_ranks([f[t]["funding"] for t in tickers])
    q2 = _grouped_ranks([-f[t]["dte"] if f[t]["dte"] is not None else None
                         for t in tickers], groups)
    q3 = _percentile_ranks([f[t]["margin_dir"] for t in tickers])
    q4 = _percentile_ranks([-f[t]["dilution"] if f[t]["dilution"] is not None
                            else None for t in tickers])
    prev_candidates = set(prev_candidates or [])
    today = _now().strftime("%Y-%m-%d")

    rows = []
    for i, t in enumerate(tickers):
        growth = 0.5 * g1[i] + 0.3 * g3[i] + 0.2 * ga[i]
        quality = 0.35 * q1[i] + 0.25 * q2[i] + 0.2 * q3[i] + 0.2 * q4[i]
        score = 100 * (0.4 * growth + 0.4 * mo[i] + 0.2 * quality)
        if prev_candidates and t not in prev_candidates:
            score *= NEWCOMER_PENALTY
        p, ft, m = cache["profiles"][t], f[t], cache["metrics"][t]
        flags = []
        edate = cache.get("earn_map", {}).get(t)
        if edate:
            days_out = (datetime.fromisoformat(edate).date()
                        - datetime.fromisoformat(today).date()).days
            if 0 <= days_out <= 7:
                flags.append(f"E-{days_out}d")
        ins = cache.get("insider", {}).get(t) or {}
        if (ins.get("net30") or 0) > 0 and _age_h(ins.get("t")) < 48:
            flags.append("ins+")
        secf = cache.get("sec_filings") or {}
        for cat, flag in (("material", "8-K"), ("activist", "act+"),
                          ("offering", "offer")):
            # distinct names throughout this loop: `f` is the factors dict and
            # `days_out` above is the earnings countdown — both were shadowed
            # here before, which is how an earlier NoneType crash happened
            fil = (secf.get(cat) or {}).get(t)
            window = FILING_CATS[cat]["days"]
            if fil and fil.get("date", "") >= (
                    _now() - timedelta(days=window)).strftime("%Y-%m-%d"):
                flags.append(flag)
        rows.append({
            "ticker": t, "name": p.get("name") or t, "ind": p.get("ind") or "—",
            "group": ft["group"], "mcap": p["mcap"],
            "rev_g": m["rev_g"], "accel": ft["accel"],
            "r13": m["r13"], "momo": round(ft["momo"], 2),
            "from_high": ft["from_high"], "ev_rev": ft["ev_rev"],
            "px": ft["px"], "dp": ft["dp"],
            "score": round(score, 1),
            "sub": {"g": round(100 * growth), "m": round(100 * mo[i]),
                    "q": round(100 * quality)},
            "flags": flags,
            # the raw inputs behind the score, the position size and the stop.
            # Published so the per-company page can SHOW its arithmetic instead
            # of asserting a conclusion the reader has to take on trust.
            "why": {"vol": m.get("vol"), "r26": m.get("r26"),
                    "rg3": m.get("rg3"), "rev_gq": m.get("rev_gq"),
                    "dte": m.get("dte"), "gm_t": m.get("gm_t"),
                    "om_t": m.get("om_t"), "om_a": m.get("om_a"),
                    "cashps": m.get("cashps"), "rps": m.get("rps"),
                    "adv": m.get("adv"), "hi52": m.get("hi52"),
                    "shares": p.get("shares"), "exch": p.get("exch")},
        })
    rows.sort(key=lambda r: -r["score"])
    candidates = rows[:CANDIDATES]

    published, per_grp = [], {}
    for r in rows:
        if r["rev_g"] <= 0:
            continue        # a growth screen publishes growers only
        grp = r["group"]
        if per_grp.get(grp, 0) >= SECTOR_CAP:
            continue
        per_grp[grp] = per_grp.get(grp, 0) + 1
        if prev_published is not None and r["ticker"] not in prev_published:
            r["flags"] = ["new"] + r["flags"]
        published.append(r)
        if len(published) >= SCREEN_SIZE:
            break
    return published, candidates


def movers(cache):
    fresh = [t for t, q in cache["quotes"].items()
             if q.get("dp") is not None and q.get("px")
             and in_band(cache["profiles"].get(t))
             and _age_h(q.get("t")) < 26]
    rows = []
    for t in _dedupe_by_company(cache, fresh):
        q = cache["quotes"][t]
        rows.append({"ticker": t,
                     "name": cache["profiles"][t].get("name") or t,
                     "px": q["px"], "dp": q["dp"]})
    rows.sort(key=lambda r: -r["dp"])
    up = rows[:5]
    # the two lists must be disjoint: with only a handful of fresh names the
    # same stock used to appear as both a top gainer and a top loser
    down = rows[len(up):][-5:][::-1]
    return up, down


# --------------------------------------------------------- evaluation --------


def _freshness(cache):
    """How old the INPUTS behind this page are.

    A static page cannot tell how long it has been sitting there — it is
    rendered once and served unchanged until the next run, so any 'age'
    computed here is zero by construction. What it CAN report honestly is the
    age of the data it was built from, and that is where this system's real
    silent failure lives: the job keeps running and publishing while a source
    behind it has been failing for days."""
    q = cache.get("quotes") or {}
    ages = sorted(_age_h(v.get("t")) for v in q.values()) or [None]
    med = ages[len(ages) // 2] if ages[0] is not None else None
    return {
        "last_full_h": round(_age_h(cache.get("last_full")), 1),
        "quote_median_h": round(med, 1) if med is not None else None,
        "quote_count": len(q),
        "bench_h": round(_age_h((cache.get("bench") or {}).get("t")), 1),
        "universe_h": round(_age_h(cache.get("universe_fetched")), 1),
        "earnings_h": round(_age_h(cache.get("earnings_fetched")), 1),
        # the gate the books themselves apply: past this the benchmark is not
        # trusted and the simulation stops marking against it
        "bench_gate_h": 72,
    }


def _screen_changes(log, published):
    """Which names entered and left the screen since the previous logged day.

    Compared against the most recent PRIOR day that actually published a
    screen, not simply yesterday: the market closes at weekends, and calling a
    Monday screen 'unchanged since Sunday' would be describing a day that never
    produced one."""
    today = _now().strftime("%Y-%m-%d")
    prior = [d for d in sorted(log) if d < today and (log[d].get("pub"))]
    if not prior or not published:
        return None
    prev_day = prior[-1]
    was = {p[0] for p in log[prev_day].get("pub", [])}
    now = {r["ticker"] for r in published}
    by_tick = {r["ticker"]: r for r in published}
    return {
        "prev_day": prev_day,
        "entered": [{"ticker": t, "name": by_tick[t].get("name") or t,
                     "score": by_tick[t].get("score"),
                     "ind": by_tick[t].get("ind") or ""}
                    for t in sorted(now - was, key=lambda t: -by_tick[t]["score"])],
        "left": sorted(was - now),
        "held": len(now & was),
    }


def update_log(cache, published, candidates):
    log = load_log()
    today = _now().strftime("%Y-%m-%d")
    bench = cache.get("bench") or {}
    prior = [d for d in sorted(log) if d < today]
    prev_bench = None
    if prior:
        b = log[prior[-1]].get("bench")
        prev_bench = b.get("iwo") if isinstance(b, dict) else None  # v2 logged a bare float
    # market closed (weekend/holiday) => benchmark unchanged: no phantom entry
    if (today not in log and prev_bench is not None
            and prev_bench == bench.get("iwo")):
        return log
    log[today] = {
        "v": MODEL_VERSION,
        "pub": [[r["ticker"], r["score"], r["px"]] for r in published],
        "cand": [r["ticker"] for r in candidates],
        "bench": {"iwo": bench.get("iwo"), "iwm": bench.get("iwm")},
    }
    # keep a year of history
    for day in sorted(log)[:-370]:
        del log[day]
    save_log(log)
    return log


def _cohort_excess(cache, entry, bench_now):
    """One published cohort's return minus the benchmark's, at today's prices.
    Returns (excess, dropped), or (None, dropped) when too few of its names can
    still be priced for the reading to be trustworthy."""
    b0 = (entry.get("bench") or {}).get("iwo")
    if not b0 or not bench_now:
        return None, 0
    rets, carried, gone, fresh = [], 0, 0, 0
    for tick, _score, px0 in entry.get("pub", []):
        if not px0:
            continue
        q = cache["quotes"].get(tick) or {}
        px = q.get("px")
        if px and _age_h(q.get("t")) < 30:
            rets.append((px / px0 - 1) * 100)
            fresh += 1
        elif px:
            # a stale quote is a COVERAGE gap, not an outcome: the budget did
            # not get round to refreshing this name. Carry its last known
            # price rather than deleting the name from the average.
            rets.append((px / px0 - 1) * 100)
            carried += 1
        else:
            # No price at all, and the company has left the profiled universe:
            # treat it as the loss it almost certainly is. Dropping it instead
            # would quietly lift the average every time a holding disappeared,
            # and in small caps disappearing is overwhelmingly a downside
            # event — delisting, failure, a collapse into a shell. The precise
            # figure is a convention; silently excluding it is a lie.
            rets.append(-DELIST_ASSUMED_LOSS * 100)
            gone += 1
    if len(rets) < MIN_PRICEABLE or fresh < MIN_FRESH:
        return None, {"carried": carried, "gone": gone, "priced": len(rets)}
    return (sum(rets) / len(rets) - (bench_now / b0 - 1) * 100,
            {"carried": carried, "gone": gone, "priced": len(rets)})


def snapshot_readings(cache, log):
    """Freeze each cohort's forward reading the first time it reaches a horizon.

    Readings must be SNAPSHOT once, never recomputed. Recomputing them daily
    from whatever cohorts happened to sit in the age window meant (a) the
    published number drifted every day while adding no new evidence — it was
    re-measuring the same cohorts against newer prices — and (b) every reading
    in a window was only a few days from every other, so the "independent
    readings" count could never exceed 1 and the freeze criterion built on it
    was unsatisfiable. Frozen readings accumulate instead, as a record should.
    """
    bench_now = (cache.get("bench") or {}).get("iwo")
    today = _now().date()
    changed = False
    for day in sorted(log):
        entry = log[day]
        if entry.get("v") != MODEL_VERSION:
            continue
        age = (today - datetime.fromisoformat(day).date()).days
        for horizon, lo, hi, _gap in HORIZONS:
            key = f"read_{horizon}"
            if key in entry or not (lo <= age <= hi):
                continue
            excess, counts = _cohort_excess(cache, entry, bench_now)
            if excess is None:
                continue
            entry[key] = {"excess": round(excess, 2), "age": age,
                          "dropped": 0, **counts}
            changed = True
    if changed:
        save_log(log)
    return log


def evaluate(cache, log):
    """The live track record: every cohort reading frozen so far for this model
    version, aggregated. 'days' counts frozen cohort readings; 'indep' counts
    only those far enough apart in time to be genuinely separate evidence."""
    out = {}
    for horizon, _lo, _hi, gap in HORIZONS:
        key = f"read_{horizon}"
        got = [(day, log[day][key]) for day in sorted(log)
               if log[day].get("v") == MODEL_VERSION and key in log[day]]
        if not got:
            continue
        indep, last, indep_vals = 0, None, []
        for day, r in got:
            d = datetime.fromisoformat(day).date()
            if last is None or (d - last).days >= gap:
                indep += 1
                indep_vals.append(r["excess"])
                last = d
        out[horizon] = {
            "excess": round(sum(r["excess"] for _d, r in got) / len(got), 2),
            "days": len(got),
            "indep": indep,
            "dropped": sum(r.get("dropped", 0) for _d, r in got),
            "carried": sum(r.get("carried", 0) for _d, r in got),
            "gone": sum(r.get("gone", 0) for _d, r in got),
            # every frozen reading, so the spread can be measured rather than
            # only the mean — a mean with no spread cannot be judged. The
            # INDEPENDENT subset is the one the decision rule uses: overlapping
            # cohorts share most of their names and most of their week, so
            # counting them all would shrink the error bar on evidence that
            # is not actually there.
            "values": [r["excess"] for _d, r in got],
            "indep_values": indep_vals,
        }
    return out


def _prev_log_entry(log):
    today = _now().strftime("%Y-%m-%d")
    prior = [d for d in sorted(log) if d < today]
    return log[prior[-1]] if prior else None


# ------------------------------------------------------------- output --------


def summarize(cache, note=None, published=None, candidates=None, log=None):
    if published is None or candidates is None:
        log = log or load_log()
        prev = _prev_log_entry(log)
        published, candidates = compute_screen(
            cache,
            prev_candidates=(prev or {}).get("cand"),
            prev_published={p[0] for p in (prev or {}).get("pub", [])} or None)
    band = [t for t in cache["profiles"] if in_band(cache["profiles"][t])]
    up, down = movers(cache)
    return {
        "note": note,
        "asof": _iso(),
        "coverage": {
            "universe": len(cache["universe"]),
            "profiled": len(cache["profiles"]),
            "in_band": len(band),
            "measured": sum(1 for t in band if t in cache["metrics"]),
            "scored": sum(1 for t in cache["metrics"] if _eligible(cache, t)),
            "below_floor": sum(1 for t in cache["metrics"]
                               if _base_eligible(cache, t)
                               and (rev_ttm(cache, t) or REV_FLOOR) < REV_FLOOR),
        },
        "screen": published,
        "freshness": _freshness(cache),
        "changes": _screen_changes(log or load_log(), published),
        "below_floor": below_floor(cache),
        "movers_up": up,
        "movers_down": down,
        "earnings": cache.get("earnings", []),
        "evaluation": evaluate(cache, log or load_log()),
        "regime": cache.get("regime") or None,
        **{f"filings_{cat}": sorted(
            ({"ticker": t, "name": (cache["profiles"].get(t) or {}).get("name") or t,
              "date": f.get("date", ""), "link": f.get("link", ""),
              "form": f.get("form", "")}
             for t, f in ((cache.get("sec_filings") or {}).get(cat) or {}).items()),
            key=lambda r: r["date"], reverse=True)[:10]
           for cat in FILING_CATS},
    }


# SEC filing categories matched against the band, all display/flag only (never
# scored). Each: which form-title prefixes belong to it, which party names the
# band company, and how many days it stays flagged.
#   material  8-K       — a material corporate event; filer IS the company
#   activist  SCHEDULE 13D/13G — a 5%+ stake; the SUBJECT is the target company
#   offering  S-1/424B  — a securities registration/prospectus (dilution); filer
FILING_CATS = {
    "material": {"prefixes": ("8-K",),                    "party": "filer",   "days": 3},
    # EDGAR currently titles these "SCHEDULE 13D"; the short form code is
    # accepted too so a feed-format change cannot silently zero the flag
    "activist": {"prefixes": ("SCHEDULE 13D", "SCHEDULE 13G", "SC 13D", "SC 13G"),
                 "party": "subject", "days": 7},
    "offering": {"prefixes": ("S-1", "424B"),             "party": "filer",   "days": 7},
}

# form and company are separated by a spaced " - "; requiring spaces avoids
# splitting hyphenated form codes like "8-K" and "S-1" at their internal hyphen
_FILING_RE = re.compile(r"^(.+?)\s+-\s+(.+?)\s*\(\d{10}\)\s*\((.*?)\)\s*$")


def _categorize_form(form):
    up = form.upper()
    for cat, spec in FILING_CATS.items():
        if any(up.startswith(p) for p in spec["prefixes"]):
            return cat
    return None


def record_filings(filing_items):
    """Match fresh SEC filings to band companies by category and remember them.
    Titles look like 'FORM - COMPANY NAME (0001234567) (Filer|Subject)'."""
    cache = load_cache()
    # longest tickers first, so the shortest — the common stock, matching
    # _dedupe_by_company — wins any name collision between share classes.
    # Otherwise a filing could be attributed to the class that never reaches
    # the page, and the flag would be silently lost.
    name_to_ticker = {}
    for t in sorted((t for t, p in cache["profiles"].items() if in_band(p)),
                    key=lambda t: (len(t), t), reverse=True):
        p = cache["profiles"][t]
        for key in (_name_key(p.get("name") or ""),
                    _name_key(cache["universe"].get(t) or "")):
            if key:
                name_to_ticker[key] = t

    store = cache.get("sec_filings") or {c: {} for c in FILING_CATS}
    for c in FILING_CATS:
        store.setdefault(c, {})
    matched = {c: 0 for c in FILING_CATS}

    for it in filing_items:
        m = _FILING_RE.match(it.get("title", ""))
        if not m:
            continue
        form, name, party = m.group(1), m.group(2), m.group(3).lower()
        cat = _categorize_form(form)
        if not cat:
            continue
        want = FILING_CATS[cat]["party"]
        # 13D/13G list two parties; only the "subject" is the target company
        if want == "subject" and "subject" not in party:
            continue
        if want == "filer" and "subject" in party:
            continue
        tick = name_to_ticker.get(_name_key(name))
        if not tick:
            continue
        day = (it.get("published") or _iso())[:10]
        prev = store[cat].get(tick)
        if not prev or day >= prev.get("date", ""):
            store[cat][tick] = {"date": day, "link": it.get("link", ""),
                                "form": form.strip()}
            matched[cat] += 1

    for cat, spec in FILING_CATS.items():
        floor = (_now() - timedelta(days=spec["days"])).strftime("%Y-%m-%d")
        store[cat] = {t: f for t, f in store[cat].items()
                      if f.get("date", "") >= floor}
    cache["sec_filings"] = store
    cache.pop("filings8k", None)  # migrated into sec_filings["material"]
    save_cache(cache)
    return matched


def match_news(items, cache=None):
    """Headlines from the main pipeline that mention in-band companies."""
    cache = cache or load_cache()
    band_names = {t: (cache["profiles"][t].get("name") or "")
                  for t in cache["profiles"] if in_band(cache["profiles"][t])}
    if not band_names:
        return []
    tickers = set(band_names)
    hits, seen = [], set()
    tick_pat = re.compile(
        r"\((?:(?:NYSE|NASDAQ|Nasdaq|AMEX|NYSE American|CBOE)[:\s]+)?([A-Z]{1,5})\)")
    for it in items:
        title = it.get("title", "")
        matched = None
        for tick in tick_pat.findall(title):
            if tick in tickers:
                matched = tick
                break
        if not matched:
            tl = title.lower()
            for tick, name in band_names.items():
                if len(name) >= 8 and name.lower() in tl:
                    matched = tick
                    break
        if matched and it.get("link") not in seen:
            seen.add(it.get("link"))
            hits.append({**it, "ticker": matched})
        if len(hits) >= 8:
            break
    return hits


# ------------------------------------------------------------- driver --------


def _distributed(tickers):
    """Stable, evenly-spread ordering (hash of ticker) instead of alphabetical,
    so partial bootstrap coverage is a representative cross-section of the whole
    market rather than front-loaded on A–G. Deterministic across runs, so the
    discovery sweep advances instead of re-profiling the same names."""
    return sorted(tickers, key=lambda t: hashlib.md5(t.encode()).hexdigest())


def _recent_cohort_tickers(days):
    """Tickers from screens published in the last `days` — the names the live
    track record has to price forward. If their quotes go stale the evaluation
    drops them, so they are refreshed ahead of routine upkeep."""
    floor = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = set()
    for day, entry in load_log().items():
        if day >= floor:
            out.update(p[0] for p in entry.get("pub", []))
    return out


def _spend_budget(fh, cache, budget):
    """Priority-ordered data refresh within the per-run call budget.

    Two things are protected above all: the quotes the live track record needs
    to price past published screens, and — while the market map is incomplete —
    a reserved share for discovering companies never looked at. Before that
    reserve existed, upkeep of the ~1,100 known small-caps consumed every call
    and discovery starved to ~8 companies/day (measured Sept 2026).
    """
    universe = _distributed(cache["universe"])
    start_budget = budget

    def spend(task_iter, fetch, cap=None):
        nonlocal budget
        allowed = budget if cap is None else min(budget, cap)
        for t in task_iter:
            if allowed <= 0 or budget <= 0:
                return
            fetch(fh, cache, t)
            budget -= 1
            allowed -= 1

    band = [t for t in universe if in_band(cache["profiles"].get(t))]
    unprofiled = [t for t in universe if t not in cache["profiles"]]
    # 1. keep the current candidates' quotes fresh
    spend((t for t in cache.get("last_screen", [])
           if _age_h(cache["quotes"].get(t, {}).get("t")) > QUOTE_STALE_SCREEN_H),
          _fetch_quote)
    # 2. quotes for names in recently published screens, so the track record
    #    can still price those cohorts forward
    spend((t for t in _recent_cohort_tickers(COHORT_LOOKBACK_DAYS)
           if _age_h(cache["quotes"].get(t, {}).get("t")) > QUOTE_STALE_COHORT_H),
          _fetch_quote)
    # The discovery reserve is genuinely fenced off: EVERY maintenance step
    # below is capped so it cannot spend into it. Capping only the quote upkeep
    # was not enough — a large metrics backfill ran earlier and uncapped, and
    # could still swallow a whole run, recreating the starvation it was meant
    # to prevent. Steps 1-2 above stay uncapped: they are small (tens of names)
    # and protect the published screen and the track record.
    reserve = int(start_budget * DISCOVERY_RESERVE) if unprofiled else 0

    def maint_cap():
        """How much a maintenance step may spend without touching the reserve."""
        return max(0, budget - reserve)

    # 3. metrics missing for known in-band names (screen grows early); entries
    #    from before v3 lack the leverage/valuation fields — refetch those too
    spend((t for t in band
           if t not in cache["metrics"] or "ev_rev" not in cache["metrics"][t]),
          _fetch_metrics, cap=maint_cap())
    # 3b. band profiles from before the v2 format lack the share count the
    #     revenue floor needs — re-profile them now, not at the weekly refresh
    spend((t for t in band if "shares" not in (cache["profiles"].get(t) or {})),
          _fetch_profile, cap=maint_cap())
    # 4. RESERVED discovery slice
    if unprofiled:
        spend(iter(unprofiled), _fetch_profile, cap=reserve)
    # 5. routine quote upkeep for the rest of the band, oldest first
    band_by_quote_age = sorted(band, key=lambda t: cache["quotes"].get(t, {}).get("t") or "")
    spend((t for t in band_by_quote_age
           if t in cache["metrics"]
           and _age_h(cache["quotes"].get(t, {}).get("t")) > QUOTE_STALE_BAND_H),
          _fetch_quote)
    # 6. any budget still unspent goes back to discovery
    spend((t for t in unprofiled if t not in cache["profiles"]), _fetch_profile)
    # 7. insider transactions for current candidates (daily)
    spend((t for t in cache.get("last_screen", [])
           if _age_h(cache["insider"].get(t, {}).get("t")) > 24), _fetch_insider)
    # 6. slow refresh: in-band metrics every 3 days
    spend((t for t in band
           if _age_h(cache["metrics"].get(t, {}).get("t")) > 72), _fetch_metrics)

    # 7. slow refresh of profiles: blank lookups retry in 48h, in-band weekly,
    #    out-of-band monthly
    def profile_stale_h(p):
        if p.get("mcap") is None:
            return 48
        return 168 if in_band(p) else 720

    spend((t for t in universe
           if t in cache["profiles"]
           and _age_h(cache["profiles"][t].get("t")) > profile_stale_h(cache["profiles"][t])),
          _fetch_profile)


def _choose_budget(cache):
    """Pick this run's spending mode. Bootstrap and catch-up always go full;
    otherwise a full run whenever enough time has passed since the last one
    (runs fire irregularly, so fixed clock hours were mostly missed)."""
    universe = cache.get("universe") or {}
    if universe:
        if any(t not in cache["profiles"] for t in universe):
            return CALL_BUDGET, "bootstrap"
        band = [t for t in universe if in_band(cache["profiles"].get(t))]
        if any(t not in cache["metrics"] or "ev_rev" not in cache["metrics"][t]
               for t in band):
            return CALL_BUDGET, "catch-up"
    last_full = cache.get("last_full")
    if not last_full or _age_h(last_full) >= HOURS_BETWEEN_FULL:
        return CALL_BUDGET, "full"
    return TRICKLE_BUDGET, "trickle"


def update(budget=None):
    """Full update cycle. Safe without a key (returns cached state + note)."""
    cache = load_cache()
    try:
        refresh_universe(cache)
    except Exception:  # noqa: BLE001 — keep whatever universe we had
        pass
    key = read_key("FINNHUB_API_KEY", "finnhub.key")
    if not key:
        save_cache(cache)
        return summarize(cache, note="waiting-for-key"), 0
    if budget is None:
        if "SMALLCAP_BUDGET" in os.environ:
            budget, mode = CALL_BUDGET, "env-override"
        else:
            budget, mode = _choose_budget(cache)
    else:
        mode = "explicit"
    fh = Finnhub(key)
    try:
        _spend_budget(fh, cache, budget)
        if mode in ("bootstrap", "catch-up", "full"):
            cache["last_full"] = _iso()
        refresh_earnings(fh, cache)
        refresh_bench(fh, cache)
        refresh_regime(fh, cache)
    finally:
        log = load_log()
        prev = _prev_log_entry(log)
        published, candidates = compute_screen(
            cache,
            prev_candidates=(prev or {}).get("cand"),
            prev_published={p[0] for p in (prev or {}).get("pub", [])} or None)
        if published:
            log = update_log(cache, published, candidates)
        # freeze any cohort that has just reached a horizon — must happen before
        # summarize(), which reports the aggregated record
        log = snapshot_readings(cache, log)
        cache["last_screen"] = [r["ticker"] for r in candidates] or cache["last_screen"]
        summary = summarize(cache, published=published, candidates=candidates, log=log)
        summary["budget_mode"] = mode
        save_cache(cache)
    return summary, fh.calls


def summary_from_cache():
    return summarize(load_cache())
