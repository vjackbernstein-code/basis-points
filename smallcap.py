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
CALL_BUDGET = int(os.environ.get("SMALLCAP_BUDGET", "550"))
CALL_INTERVAL = 1.1                     # seconds between Finnhub calls (55/min)
BENCHES = ("IWO", "IWM")                # Russell 2000 Growth (primary) + Russell 2000
MODEL_VERSION = "v3"                    # stamped on log entries; the track record is
                                        # reported per version, never blended
MIN_GROUP = 8                           # industry-relative ranks need this many peers

# Coarse industry groups: vendor tags are fragmented ("Banking" vs "Financial
# Services"), so the sector cap and industry-relative ranks use these instead.
INDUSTRY_GROUPS = [
    ("Financials",  ("bank", "financial", "insurance", "capital market", "credit", "thrift")),
    ("Health",      ("biotech", "pharma", "health", "life science", "medical")),
    ("Telecom",     ("telecom",)),
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
    prev = cache["profiles"].get(ticker) or {}
    entry = {
        "mcap": p.get("marketCapitalization") or None,
        "shares": p.get("shareOutstanding") or None,
        "exch": (p.get("exchange") or "")[:40],
        "ind": (p.get("finnhubIndustry") or "")[:28],
        "name": (p.get("name") or cache["universe"].get(ticker, ""))[:60],
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
        if sym and in_band(cache["profiles"].get(sym)) and r.get("date"):
            earn_map[sym] = r["date"]
            keep.append({"date": r["date"], "ticker": sym,
                         "name": cache["profiles"][sym].get("name") or sym,
                         "hour": r.get("hour") or ""})
    keep.sort(key=lambda r: (r["date"], r["ticker"]))
    cache["earn_map"] = earn_map
    cache["earnings"] = keep[:12]
    cache["earnings_fetched"] = _iso()


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
    if m.get("rps") and p.get("shares"):
        return m["rps"] * p["shares"]
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
    """Ranks in [0,1]; None values sit at a neutral 0.5."""
    known = [(v, i) for i, v in enumerate(values) if v is not None]
    ranks = [0.5] * len(values)
    n = len(known)
    for pos, (_, i) in enumerate(sorted(known, key=lambda x: x[0])):
        ranks[i] = pos / (n - 1) if n > 1 else 0.5
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
            days = (datetime.fromisoformat(edate).date()
                    - datetime.fromisoformat(today).date()).days
            if 0 <= days <= 7:
                flags.append(f"E-{days}d")
        ins = cache.get("insider", {}).get(t) or {}
        if (ins.get("net30") or 0) > 0 and _age_h(ins.get("t")) < 48:
            flags.append("ins+")
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
    return rows[:5], rows[-5:][::-1] if len(rows) > 5 else []


# --------------------------------------------------------- evaluation --------


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


def evaluate(cache, log):
    """Forward cohort returns vs the IWO benchmark, current model version only."""
    bench_now = (cache.get("bench") or {}).get("iwo")
    today = _now().date()
    out = {}
    for horizon, lo, hi, gap in (("1w", 6, 9, 7), ("4w", 25, 31, 28)):
        readings = []
        for day in sorted(log):
            entry = log[day]
            if entry.get("v") != MODEL_VERSION:
                continue
            b0 = (entry.get("bench") or {}).get("iwo")
            age = (today - datetime.fromisoformat(day).date()).days
            if not (lo <= age <= hi) or not b0 or not bench_now:
                continue
            rets, dropped = [], 0
            for tick, _score, px0 in entry.get("pub", []):
                q = cache["quotes"].get(tick) or {}
                if px0 and q.get("px") and _age_h(q.get("t")) < 30:
                    rets.append((q["px"] / px0 - 1) * 100)
                else:
                    dropped += 1
            if len(rets) < 20:
                continue    # too many missing names to trust the reading
            cohort = sum(rets) / len(rets)
            readings.append((day, cohort - (bench_now / b0 - 1) * 100, dropped))
        if readings:
            indep, last = 0, None
            for day, _x, _d in readings:
                d = datetime.fromisoformat(day).date()
                if last is None or (d - last).days >= gap:
                    indep += 1
                    last = d
            out[horizon] = {
                "excess": round(sum(x for _, x, _d in readings) / len(readings), 2),
                "days": len(readings),
                "indep": indep,
                "dropped": sum(d for _, _x, d in readings),
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
        "below_floor": below_floor(cache),
        "movers_up": up,
        "movers_down": down,
        "earnings": cache.get("earnings", []),
        "evaluation": evaluate(cache, log or load_log()),
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


# ------------------------------------------------------------- driver --------


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

    band = [t for t in universe if in_band(cache["profiles"].get(t))]
    # 1. keep the current screen's quotes fresh
    spend((t for t in cache.get("last_screen", [])
           if _age_h(cache["quotes"].get(t, {}).get("t")) > 3), _fetch_quote)
    # 2. metrics missing for known in-band names (screen grows early); entries
    #    from before v3 lack the leverage/valuation fields — refetch those too
    spend((t for t in band
           if t not in cache["metrics"] or "ev_rev" not in cache["metrics"][t]),
          _fetch_metrics)
    # 2b. band profiles from before the v2 format lack the share count the
    #     revenue floor needs — re-profile them now, not at the weekly refresh
    spend((t for t in band if "shares" not in (cache["profiles"].get(t) or {})),
          _fetch_profile)
    # 3. quotes for measured band names — before bootstrap, since eligibility
    #    needs a price; otherwise nothing scores until the universe is mapped
    band_by_quote_age = sorted(band, key=lambda t: cache["quotes"].get(t, {}).get("t") or "")
    spend((t for t in band_by_quote_age
           if t in cache["metrics"]
           and _age_h(cache["quotes"].get(t, {}).get("t")) > 4), _fetch_quote)
    # 4. bootstrap: profiles we've never checked
    spend((t for t in universe if t not in cache["profiles"]), _fetch_profile)
    # 5. insider transactions for current candidates (daily)
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
        refresh_bench(fh, cache)
    finally:
        log = load_log()
        prev = _prev_log_entry(log)
        published, candidates = compute_screen(
            cache,
            prev_candidates=(prev or {}).get("cand"),
            prev_published={p[0] for p in (prev or {}).get("pub", [])} or None)
        if published:
            log = update_log(cache, published, candidates)
        cache["last_screen"] = [r["ticker"] for r in candidates] or cache["last_screen"]
        summary = summarize(cache, published=published, candidates=candidates, log=log)
        save_cache(cache)
    return summary, fh.calls


def summary_from_cache():
    return summarize(load_cache())
