#!/usr/bin/env python3
"""
Small-cap growth scorecard for Basis Points.

Maintains a rolling scorecard of U.S. small-cap growth candidates:

  universe   the SEC's public list of all listed U.S. companies (keyless)
  measures   size, revenue growth, momentum, liquidity via Finnhub (free key)
  output     a transparent composite score and ranked screen

The Finnhub free tier allows 60 calls/minute, so the scorecard fills over the
first day or two of scheduled runs (a fixed per-run call budget) and stays
fresh with rolling updates afterward. All state lives in data/smallcap.json,
which the cloud workflow commits back to the repository between runs.

Methodology (fixed, disclosed on the page):
  eligibility  market cap $300M-$2B; listed exchange (no OTC); price >= $2;
               10-day average volume >= 50k shares
  score        40% revenue growth (trailing 12mo, year-over-year)
               40% 13-week price momentum
               20% proximity to 52-week high
               each factor percentile-ranked within the eligible set

This reports facts by fixed rules. It is not investment advice.
"""

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

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
UA = "BasisPointsAggregator/1.0 (personal research project)"
FINNHUB = "https://finnhub.io/api/v1"

MCAP_MIN, MCAP_MAX = 300.0, 2000.0      # $ millions
PX_MIN = 2.0                            # dollars
ADV_MIN = 0.05                          # 10-day avg volume, millions of shares
SCREEN_SIZE = 25
CALL_BUDGET = 550                       # per run; ~10 min at the polite rate
                                        # (public-repo Actions minutes are free;
                                        # Finnhub's cap is per-minute, not daily)
CALL_INTERVAL = 1.1                     # seconds between Finnhub calls (55/min)


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


def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {"universe": {}, "universe_fetched": None,
            "profiles": {}, "metrics": {}, "quotes": {},
            "earnings": [], "earnings_fetched": None, "last_screen": []}


def save_cache(cache):
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, separators=(",", ":")),
                          encoding="utf-8")


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


def in_band(profile):
    if not profile or not profile.get("mcap"):
        return False
    exch = profile.get("exch") or ""
    return (MCAP_MIN <= profile["mcap"] <= MCAP_MAX) and "OTC" not in exch.upper()


def _fetch_profile(fh, cache, ticker):
    try:
        p = fh.get("stock/profile2", symbol=ticker)
    except Exception:  # noqa: BLE001 — record the attempt; retry on schedule
        p = {}
    cache["profiles"][ticker] = {
        "mcap": p.get("marketCapitalization") or None,
        "exch": (p.get("exchange") or "")[:40],
        "ind": (p.get("finnhubIndustry") or "")[:28],
        "name": (p.get("name") or cache["universe"].get(ticker, ""))[:60],
        "t": _iso(),
    }


def _fetch_metrics(fh, cache, ticker):
    try:
        m = fh.get("stock/metric", symbol=ticker, metric="all").get("metric", {})
    except Exception:  # noqa: BLE001
        m = {}
    cache["metrics"][ticker] = {
        "rev_g": m.get("revenueGrowthTTMYoy"),
        "r13": m.get("13WeekPriceReturnDaily"),
        "r26": m.get("26WeekPriceReturnDaily"),
        "hi52": m.get("52WeekHigh"),
        "adv": m.get("10DayAverageTradingVolume"),
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


def _eligible(cache, ticker):
    p = cache["profiles"].get(ticker)
    m = cache["metrics"].get(ticker)
    q = cache["quotes"].get(ticker)
    if not (in_band(p) and m and q and q.get("px")):
        return False
    return (q["px"] >= PX_MIN
            and (m.get("adv") or 0) >= ADV_MIN
            and m.get("rev_g") is not None
            and m.get("r13") is not None
            and m.get("hi52"))


def _percentile_ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    n = len(values)
    for pos, i in enumerate(order):
        ranks[i] = pos / (n - 1) if n > 1 else 0.5
    return ranks


def compute_screen(cache):
    tickers = [t for t in cache["metrics"] if _eligible(cache, t)]
    if not tickers:
        return []
    growth, momo, near = [], [], []
    for t in tickers:
        m, q = cache["metrics"][t], cache["quotes"][t]
        growth.append(max(-20.0, min(150.0, m["rev_g"])))
        momo.append(max(-50.0, min(150.0, m["r13"])))
        near.append(min(1.1, q["px"] / m["hi52"]))
    g_r, m_r, n_r = (_percentile_ranks(growth), _percentile_ranks(momo),
                     _percentile_ranks(near))
    rows = []
    for i, t in enumerate(tickers):
        p, m, q = cache["profiles"][t], cache["metrics"][t], cache["quotes"][t]
        score = 100 * (0.4 * g_r[i] + 0.4 * m_r[i] + 0.2 * n_r[i])
        rows.append({
            "ticker": t, "name": p.get("name") or t, "ind": p.get("ind") or "—",
            "mcap": p["mcap"], "rev_g": m["rev_g"], "r13": m["r13"],
            "from_high": (q["px"] / m["hi52"] - 1) * 100,
            "px": q["px"], "dp": q.get("dp"),
            "score": round(score, 1),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows[:SCREEN_SIZE]


def movers(cache):
    rows = []
    for t, q in cache["quotes"].items():
        if (q.get("dp") is not None and q.get("px")
                and in_band(cache["profiles"].get(t))
                and _age_h(q.get("t")) < 26):
            rows.append({"ticker": t,
                         "name": cache["profiles"][t].get("name") or t,
                         "px": q["px"], "dp": q["dp"]})
    rows.sort(key=lambda r: -r["dp"])
    return rows[:5], rows[-5:][::-1] if len(rows) > 5 else []


def refresh_earnings(fh, cache):
    if cache.get("earnings") and _age_h(cache.get("earnings_fetched")) < 12:
        return
    start = _now().strftime("%Y-%m-%d")
    end = (_now() + timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        cal = fh.get("calendar/earnings", **{"from": start, "to": end})
        rows = cal.get("earningsCalendar", [])
    except Exception:  # noqa: BLE001
        return
    keep = []
    for r in rows:
        sym = r.get("symbol", "")
        if in_band(cache["profiles"].get(sym)):
            keep.append({"date": r.get("date"), "ticker": sym,
                         "name": cache["profiles"][sym].get("name") or sym,
                         "hour": r.get("hour") or ""})
    keep.sort(key=lambda r: (r["date"], r["ticker"]))
    cache["earnings"] = keep[:12]
    cache["earnings_fetched"] = _iso()


def _spend_budget(fh, cache, budget):
    """Priority-ordered data refresh within the per-run call budget."""
    universe = sorted(cache["universe"])

    def spend(task_iter, fetch):
        nonlocal budget
        for t in task_iter:
            if budget <= 0:
                return
            fetch(fh, cache, t)
            budget -= 1

    # 1. keep the current screen's quotes fresh
    spend((t for t in cache.get("last_screen", [])
           if _age_h(cache["quotes"].get(t, {}).get("t")) > 3), _fetch_quote)
    # 2. bootstrap: profiles we've never checked
    spend((t for t in universe if t not in cache["profiles"]), _fetch_profile)
    # 3. metrics missing for in-band names
    spend((t for t in universe
           if in_band(cache["profiles"].get(t)) and t not in cache["metrics"]),
          _fetch_metrics)
    # 4. quotes missing or stale for in-band names, oldest first
    band = [t for t in universe if in_band(cache["profiles"].get(t))]
    band.sort(key=lambda t: cache["quotes"].get(t, {}).get("t") or "")
    spend((t for t in band
           if _age_h(cache["quotes"].get(t, {}).get("t")) > 4), _fetch_quote)
    # 5. slow refresh of in-band metrics (3 days) and profiles (7 days)
    spend((t for t in band
           if _age_h(cache["metrics"].get(t, {}).get("t")) > 72), _fetch_metrics)
    def profile_stale_h(p):
        if p.get("mcap") is None:
            return 48       # a blank/failed lookup retries soon, not in 30 days
        return 168 if in_band(p) else 720

    spend((t for t in universe
           if t in cache["profiles"]
           and _age_h(cache["profiles"][t].get("t")) > profile_stale_h(cache["profiles"][t])),
          _fetch_profile)


def summarize(cache, note=None):
    profiled = len(cache["profiles"])
    band = [t for t in cache["profiles"] if in_band(cache["profiles"][t])]
    screen = compute_screen(cache)
    up, down = movers(cache)
    return {
        "note": note,
        "asof": _iso(),
        "coverage": {
            "universe": len(cache["universe"]),
            "profiled": profiled,
            "in_band": len(band),
            "measured": sum(1 for t in band if t in cache["metrics"]),
            "scored": sum(1 for t in cache["metrics"] if _eligible(cache, t)),
        },
        "screen": screen,
        "movers_up": up,
        "movers_down": down,
        "earnings": cache.get("earnings", []),
    }


def match_news(items, cache=None):
    """Headlines from the main pipeline that mention in-band companies."""
    cache = cache or load_cache()
    band_names = {t: (cache["profiles"][t].get("name") or "")
                  for t in cache["profiles"] if in_band(cache["profiles"][t])}
    if not band_names:
        return []
    tickers = set(band_names)
    hits, seen = [], set()
    for it in items:
        title = it.get("title", "")
        matched = None
        for tick in re.findall(r"\(([A-Z]{1,5})\)", title):
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


def update(budget=CALL_BUDGET):
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
    fh = Finnhub(key)
    try:
        _spend_budget(fh, cache, budget)
        refresh_earnings(fh, cache)
    finally:
        summary = summarize(cache)
        cache["last_screen"] = [r["ticker"] for r in summary["screen"]]
        save_cache(cache)
    return summary, fh.calls


def summary_from_cache():
    return summarize(load_cache())
