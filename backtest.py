#!/usr/bin/env python3
"""Replay the live scoring rules over history.

WHAT THIS IS FOR
    The record published on the site is forward evidence, which is the honest
    way round and slow: the first real verdict is months away. This cannot
    replace it and is not offered as a substitute. It answers a narrower and
    cheaper question — have these rules ever worked at all — before another
    year is spent finding out forward.

WHAT MAKES IT WORTH TRUSTING
    It calls `smallcap.compute_screen`, the actual function the live site
    runs, on inputs rebuilt as of each historical date. The scoring is not
    reimplemented here, because a reimplementation would be a different model
    and testing it would answer a different question. Everything in this file
    is input plumbing and measurement.

    Point-in-time. A company's figures are taken from the rows FILED on or
    before the date being scored, never the period they describe. A Q1 revenue
    figure is not knowable in Q1; it is knowable when it is filed, around
    seven weeks later. `sharadar.fundamentals_asof` does this and restatements
    fall out of it correctly.

    Survivorship. The universe on each date is whoever was actually trading
    then, delisted companies included, drawn from a spine where two thirds of
    the common stocks no longer exist. A cohort member that stops trading
    inside a measurement window is NOT dropped — see `_basket_return`, which
    is where survivorship bias normally sneaks back in after being carefully
    excluded everywhere else.

WHAT MAKES IT WORTH LESS THAN THE LIVE RECORD
    State these whenever a result from here is quoted.

    1. DIFFERENT DATA VENDOR. The live screen scores Finnhub's figures; this
       scores Sharadar's, rebuilt into the same shape. Revenue growth from two
       vendors for one company is close but not identical, so this tests the
       RULES on similar inputs, not the live system on its own inputs. The
       bank-growth contradiction found on 2026-10-03 is a standing reminder
       that a vendor's definition of a field is part of the result.
    2. NO NEWS, NO FILINGS, NO INSIDER DATA. Those drive display flags only
       and never the score, so the ranking is unaffected — but the published
       newcomer penalty depends on the previous day's candidate list, which
       this reconstructs from its own history rather than from what the site
       actually published at the time.
    3. BEFORE COSTS. This measures the ranking, exactly as the live
       `evaluation` block does. The live books measure after costs and that is
       the number most likely to decide the question. A ranking that works and
       cannot pay for its own trading is still a no.
    4. ONE PASS, NO TUNING. The rules are frozen. If a result here is used to
       choose a parameter, every number it produces afterwards is worthless
       and so is the live record it was tuned against.

USAGE
    python3 backtest.py --from 2004-01-01 --to 2026-09-01
    python3 backtest.py --quick            # a few years, to check it runs
    python3 backtest.py --one 2018-06-15   # a single date's screen, to inspect
"""

import argparse
import json
import math
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import portfolio  # noqa: E402
import sharadar  # noqa: E402
import smallcap  # noqa: E402

BENCHMARK = "IWO"          # the same benchmark the live record uses
EARLIEST = "2000-08-01"    # IWO's price history starts 2000-07-28

# Sharadar's exchange codes, written as the strings the live allowlist speaks.
# The RULE is not touched — `smallcap.is_us_listed` still decides — but it was
# written against a different vendor's spellings, and left untranslated it
# rejects "NYSE" and with it 14,117 American companies. Translating the input
# is right; loosening the rule to accept both vocabularies would be a change
# to a frozen rule, made for the convenience of the thing testing it.
EXCHANGE_NAMES = {
    "NASDAQ": "NASDAQ NMS - GLOBAL MARKET",
    "NYSE": "NEW YORK STOCK EXCHANGE, INC.",
    "NYSEMKT": "NYSE MKT LLC",
    "NYSEARCA": "NYSE ARCA",
    "BATS": "BATS EXCHANGE",
    # deliberately left to be rejected, as they are live
    "OTC": "OTC MARKETS",
    "TXSE": "TXSE",
}

# Nano Labs Ltd trades on NASDAQ under the two letters N-A, and whatever
# loaded the vendor's CSV into this database read that as "not a value": its
# ticker is SQL NULL in every table. The source is read-only and belongs to
# another project, so the repair happens here, by restoring the symbol rather
# than skipping the rows. Skipping was the tempting option and it is the wrong
# one — it would drop a real NASDAQ company from the universe from July 2022
# onward, silently, which is the same class of error as survivorship bias and
# arrives the same way: by discarding what would not parse.
NULL_TICKER = "NA"


# Measured at the same horizons the live record freezes, so the two sets of
# numbers mean the same thing and can be put beside each other.
HORIZONS = (("1w", 7), ("4w", 28))


# ----------------------------------------------------------- loading ---------

def mondays(start, end):
    d = date.fromisoformat(start)
    d += timedelta(days=(7 - d.weekday()) % 7)
    out = []
    last = date.fromisoformat(end)
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def _chunks(seq, n=800):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_band(con, dates):
    """{as-of date: {ticker: (marketcap, ev)}} for companies in the size band.

    One scan serves every date. The market-cap index is on (ticker, date), so
    a per-date query scans all 40M rows and costs 1.6s; asking for a year of
    dates at once costs 3.5s for the lot."""
    lo, hi = smallcap.MCAP_MIN, smallcap.MCAP_MAX
    out = {d: {} for d in dates}
    for part in _chunks(dates, 400):
        ph = ",".join("?" * len(part))
        for d, tk, mc, ev in con.execute(
                f"SELECT date, COALESCE(ticker, ?), marketcap, ev FROM daily "
                f"WHERE date IN ({ph}) AND marketcap BETWEEN ? AND ?",
                (NULL_TICKER, *part, lo, hi)):
            out[d][tk] = (mc, ev)
    return out


# Which of the vendor's label fields stands in for the live feed's industry
# label. The live rule ranks growth WITHIN an industry group, so getting this
# wrong does not merely mislabel a page — it changes who each company is
# compared against, and so the ranking itself.
#
# The live feed supplies labels at the level of "Banking", "Biotechnology",
# "Aerospace & Defense", and all 914 of its eligible companies map to a real
# group: none fall through to "Other". Sharadar's `industry` is a level finer
# ("Drug Manufacturers - Specialty & Generic") and 24.9% of it falls through;
# its `sector` is the matching level and 5.6% does. So `sector` is the honest
# correspondence. Picking it is translating an input, as with the exchange
# codes above; widening the rule's keyword list to swallow a finer vocabulary
# would be editing a frozen rule to flatter the thing testing it.
LABEL_FIELD = "sector"

# One sector word the live rule does not recognise, translated into one it
# does. Sharadar says "Basic Materials" where the live feed says "Chemicals",
# "Metals & Mining" or "Containers & Packaging"; the rule matches any of those
# keywords to the same Materials group, so which one is used here cannot
# affect a result — only whether 2,483 companies get a peer group at all,
# instead of falling into "Other" and being ranked against a bucket they do
# not belong in. Every other sector word already maps, and a `None` sector
# stays unmapped on purpose: the rule treats a blank industry as ineligible,
# which is how shells and closed-end funds are meant to be excluded.
SECTOR_NAMES = {"Basic Materials": "Chemicals"}


def load_spine(con, label_field=LABEL_FIELD):
    """Ticker metadata: industry label, exchange, name, category, delisting."""
    out = {}
    for r in con.execute(
            "SELECT COALESCE(ticker, ?) AS ticker, name, exchange, sector, "
            "industry, category, isdelisted, lastpricedate FROM tickers "
            "WHERE category LIKE '%Common Stock%'", (NULL_TICKER,)):
        out[r["ticker"]] = {
            "name": r["name"], "exch": EXCHANGE_NAMES.get(r["exchange"],
                                                          r["exchange"]),
            "ind": SECTOR_NAMES.get(r[label_field], r[label_field]),
            "industry": r["industry"],
            "category": r["category"],
            "delisted": r["isdelisted"] == "Y",
            "last_price": r["lastpricedate"]}
    return out


def load_prices(con, tickers, lo, hi):
    """{ticker: [(date, closeadj, closeunadj, volume)]}, ascending.

    Both price columns are kept and they are not interchangeable. Returns and
    volatility must use the ADJUSTED series or a split reads as a 50% crash.
    The price the rules test against a $2 floor, and show on the page, is what
    actually traded, so that one is unadjusted."""
    out = {}
    for part in _chunks(tickers):
        ph = ",".join("?" * len(part))
        null_too = " OR ticker IS NULL" if NULL_TICKER in part else ""
        for tk, d, ca, cu, v in con.execute(
                f"SELECT COALESCE(ticker, ?), date, closeadj, closeunadj, "
                f"volume FROM stocks "
                f"WHERE (ticker IN ({ph}){null_too}) AND date BETWEEN ? AND ? "
                f"ORDER BY ticker, date", (NULL_TICKER, *part, lo, hi)):
            out.setdefault(tk, []).append((d, ca, cu, v))
    return out


FUND_FIELDS = ("revenue", "cor", "opinc", "ncfo", "cashneq", "de",
               "sharesbas", "equity")


def load_fundamentals(con, tickers, filed_to, since, dimension, filed=None):
    """{ticker: [row, ...]} filed on or before `filed_to`, oldest first.

    Filtering on the FILING date in the query, not afterwards, is what makes
    this point-in-time. Everything downstream can then only see rows that
    were public.

    The filing-date column's NAME is resolved, never assumed. This database
    calls it `date`; the vendor calls it `datekey`. An earlier version of this
    function asked `sharadar.filed_column` which it was, ignored the answer,
    and hardcoded `date` — so on a database using the other name the
    verification would have passed while the query used a column that is not
    a filing date at all, which is lookahead with a clean bill of health. It
    is aliased to `filed_on` so nothing downstream has to know either name."""
    filed = filed or sharadar.filed_column(con)
    cols = ", ".join(FUND_FIELDS)
    out = {}
    for part in _chunks(tickers):
        ph = ",".join("?" * len(part))
        null_too = " OR ticker IS NULL" if NULL_TICKER in part else ""
        for r in con.execute(
                f"SELECT COALESCE(ticker, ?) AS ticker, reportperiod, "
                f"{filed} AS filed_on, {cols} FROM fundamentals "
                f"WHERE dimension=? AND (ticker IN ({ph}){null_too}) "
                f"AND {filed} <= ? AND reportperiod >= ? "
                f"ORDER BY ticker, {filed}",
                (NULL_TICKER, dimension, *part, filed_to, since)):
            out.setdefault(r["ticker"], []).append(dict(r))
    return out


def load_benchmark(con, lo, hi):
    return {d: px for d, px in con.execute(
        "SELECT date, closeadj FROM funds WHERE ticker=? "
        "AND date BETWEEN ? AND ? ORDER BY date", (BENCHMARK, lo, hi))}


# -------------------------------------------------------- metric build -------

def _at_or_before(rows, asof):
    """Rows filed on or before `asof`, oldest first. Already filtered in SQL
    to a window; this narrows to the exact date being scored."""
    return [r for r in rows if r["filed_on"] <= asof]


def _growth(rows, years):
    """Annualised revenue growth over `years`, from rows public now.

    Both ends come from the same visible set, so a figure filed after the
    as-of date cannot reach either one."""
    if len(rows) < 2:
        return None
    latest = rows[-1]
    rev_now = latest.get("revenue")
    if not rev_now or rev_now <= 0:
        return None
    target = (date.fromisoformat(latest["reportperiod"])
              - timedelta(days=round(365.25 * years)))
    # the row whose PERIOD sits closest to a year ago, among those public
    prior = min(rows[:-1],
                key=lambda r: abs((date.fromisoformat(r["reportperiod"])
                                   - target).days))
    gap = abs((date.fromisoformat(prior["reportperiod"]) - target).days)
    if gap > 100:                      # no comparable period: say so
        return None
    rev_then = prior.get("revenue")
    if not rev_then or rev_then <= 0:
        return None
    ratio = rev_now / rev_then
    if years == 1:
        return (ratio - 1) * 100
    return ((ratio ** (1.0 / years)) - 1) * 100


def _series_metrics(px, asof):
    """Momentum, volatility, 52-week high and liquidity, from prices only."""
    rows = [r for r in px if r[0] <= asof]
    if len(rows) < 30:
        return None
    adj = [r[1] for r in rows if r[1]]
    if len(adj) < 30:
        return None
    last = adj[-1]

    def back(trading_days):
        if len(adj) <= trading_days:
            return None
        then = adj[-trading_days - 1]
        return (last / then - 1) * 100 if then else None

    # daily log-ish returns over about three months, annualised, as a percent
    window = adj[-64:]
    rets = [(window[i] / window[i - 1] - 1)
            for i in range(1, len(window)) if window[i - 1]]
    vol = (statistics.stdev(rets) * math.sqrt(252) * 100
           if len(rets) > 5 else None)

    year = [r for r in rows if r[0] >= (date.fromisoformat(asof)
                                        - timedelta(days=365)).isoformat()]
    hi52 = max((r[2] for r in year if r[2]), default=None)
    vols = [r[3] for r in rows[-10:] if r[3] is not None]
    return {
        "r13": back(63), "r26": back(126),
        "vol": vol, "hi52": hi52,
        # the live feed reports average daily volume in millions of shares
        "adv": (sum(vols) / len(vols) / 1e6) if vols else None,
        "px": rows[-1][2] or rows[-1][1],
    }


def build_cache(asof, band, spine, prices, art, arq, ary):
    """A cache in the shape `smallcap` expects, as of one historical date."""
    cache = {"profiles": {}, "metrics": {}, "quotes": {},
             "earn_map": {}, "insider": {}, "sec_filings": {}}
    for tk, (mcap, ev) in band.items():
        meta = spine.get(tk)
        if not meta or not meta.get("ind"):
            continue
        s = _series_metrics(prices.get(tk) or [], asof)
        if not s or not s["px"]:
            continue

        a = _at_or_before(art.get(tk) or [], asof)
        q = _at_or_before(arq.get(tk) or [], asof)
        y = _at_or_before(ary.get(tk) or [], asof)
        if not a or not q:
            continue
        ttm, last_q, last_y = a[-1], q[-1], (y[-1] if y else {})

        rev = ttm.get("revenue")
        shares = last_q.get("sharesbas") or ttm.get("sharesbas")
        if not shares or shares <= 0:
            continue

        def margin(row, field):
            r = row.get("revenue")
            v = row.get(field)
            return (v / r * 100) if (r and v is not None and r > 0) else None

        cache["profiles"][tk] = {
            "mcap": mcap, "shares": shares / 1e6,
            "exch": meta["exch"], "ind": meta["ind"],
            "name": meta["name"], "t": asof,
            # real share-count history, which is what the dilution factor
            # measures once two readings sit 60 days apart
            "shist": [[r["reportperiod"], r["sharesbas"]] for r in q[-9:]
                      if r.get("sharesbas")],
        }
        cache["quotes"][tk] = {"px": s["px"], "dp": 0.0,
                               "hi": None, "lo": None, "t": asof}
        cache["metrics"][tk] = {
            "rev_g": _growth(a, 1),
            "rev_gq": _growth(q, 1),
            "rg3": _growth(a, 3),
            "rg5": _growth(a, 5),
            "rsg5": None,          # share growth over 5y; shist covers this
            "r13": s["r13"], "r26": s["r26"], "vol": s["vol"],
            "hi52": s["hi52"], "adv": s["adv"],
            # revenue per share in millions-per-million, matching the live
            # feed's units so rev_ttm() comes out in $M
            "rps": (rev / shares) if (rev is not None and shares) else None,
            "cfps": ((ttm.get("ncfo") / shares)
                     if (ttm.get("ncfo") is not None and shares) else None),
            "cashps": ((last_q.get("cashneq") / shares)
                       if (last_q.get("cashneq") is not None and shares)
                       else None),
            "gm_t": (((rev - ttm["cor"]) / rev * 100)
                     if (rev and ttm.get("cor") is not None and rev > 0)
                     else None),
            "gm_a": (((last_y["revenue"] - last_y["cor"])
                      / last_y["revenue"] * 100)
                     if (last_y.get("revenue") and last_y.get("cor") is not None
                         and last_y["revenue"] > 0) else None),
            "om_t": margin(ttm, "opinc"),
            "om_a": margin(last_y, "opinc") if last_y else None,
            "dte": last_q.get("de"),
            "ev_rev": ((ev / rev) if (ev and rev and rev > 0) else None),
            "t": asof,
        }
    return cache


def screen_on(cache, asof, prev_candidates=None, prev_published=None):
    """The real scoring function, with the clock pinned to the as-of date."""
    real_now = smallcap._now
    try:
        smallcap._now = lambda: datetime.fromisoformat(asof).replace(
            tzinfo=timezone.utc)
        return smallcap.compute_screen(cache, prev_candidates, prev_published)
    finally:
        smallcap._now = real_now


# --------------------------------------------------------- measurement -------

def _price_on_or_before(px, when):
    best = None
    for d, ca, _cu, _v in px:
        if d > when:
            break
        if ca:
            best = (d, ca)
    return best


def _basket_return(prices, spine, tickers, start, end):
    """Equal-weighted return of a basket between two dates, in percent.

    Returns a dict with BOTH defensible treatments of a company that stops
    trading inside the window, because the choice between them decides the
    sign of the whole backtest and must not be hidden inside it:

      "assumed"   a delisting is booked at the live system's
                  DELIST_ASSUMED_LOSS, which is what the published record
                  does with a company that disappears, so the two sets of
                  numbers mean the same thing
      "last_px"   a delisting is measured at the company's final traded
                  price, which is real data rather than an assumption, but
                  flatters the result: those shares halt and then vanish, so
                  that print was not an exit anyone could have taken, and it
                  measures a two-day return as though it were a four-week one

    Over the full history the two differ by about five and a half points a
    year on 0.47% of the measurements. Reporting only one of them would be
    presenting an assumption as a finding, in whichever direction suited.

    This is also where survivorship bias returns if it is going to. Quietly
    leaving an unmeasurable name out of the average keeps the survivors and
    drops the disasters, so dropped names are counted and reported."""
    assumed, last_px, dropped, delisted = [], [], 0, 0
    for tk in tickers:
        px = prices.get(tk) or []
        a = _price_on_or_before(px, start)
        if not a:
            dropped += 1
            continue
        b = _price_on_or_before(px, end)
        measured = (b[1] / a[1] - 1) * 100 if (b and b[0] > a[0]) else None
        meta = spine.get(tk) or {}
        last = meta.get("last_price") or ""
        gone = bool(meta.get("delisted") and last and last < end)

        if gone:
            delisted += 1
            assumed.append(-smallcap.DELIST_ASSUMED_LOSS * 100)
            # its final print if there is one, otherwise the same assumption:
            # with no price at all there is nothing else to use
            last_px.append(measured if measured is not None
                           else -smallcap.DELIST_ASSUMED_LOSS * 100)
        elif measured is not None:
            assumed.append(measured)
            last_px.append(measured)
        else:
            dropped += 1

    def mean(xs):
        return (sum(xs) / len(xs)) if xs else None

    return {"assumed": mean(assumed), "last_px": mean(last_px),
            "dropped": dropped, "delisted": delisted}


def _bench_return(bench, start, end):
    def at(when):
        got = None
        for d in sorted(bench):
            if d > when:
                break
            got = bench[d]
        return got
    a, b = at(start), at(end)
    return ((b / a - 1) * 100) if (a and b) else None


# ---------------------------------------------------------------- run --------

def run(con, start, end, verbose=True):
    """Walk forward, year by year, and collect a reading per cohort."""
    spine = load_spine(con)
    filed = sharadar.filed_column(con)
    dates = mondays(max(start, EARLIEST), end)
    if not dates:
        raise RuntimeError("no dates in range")

    readings = {h: [] for h, _ in HORIZONS}
    readings_alt = {h: [] for h, _ in HORIZONS}
    cohorts = []
    prev_cand = prev_pub = None
    t0 = time.monotonic()

    by_year = {}
    for d in dates:
        by_year.setdefault(d[:4], []).append(d)

    for year in sorted(by_year):
        chunk = by_year[year]
        band = load_band(con, chunk)
        universe = sorted({t for d in chunk for t in band[d]})
        if not universe:
            continue
        # prices reach back far enough for a 52-week high and forward far
        # enough to measure the longest horizon
        px_lo = (date.fromisoformat(chunk[0]) - timedelta(days=430)).isoformat()
        px_hi = (date.fromisoformat(chunk[-1]) + timedelta(days=45)).isoformat()
        prices = load_prices(con, universe, px_lo, px_hi)
        bench = load_benchmark(con, px_lo, px_hi)
        since = (date.fromisoformat(chunk[0])
                 - timedelta(days=7 * 365)).isoformat()
        art = load_fundamentals(con, universe, chunk[-1], since, "ART", filed)
        arq = load_fundamentals(con, universe, chunk[-1], since, "ARQ", filed)
        ary = load_fundamentals(con, universe, chunk[-1], since, "ARY", filed)

        for d in chunk:
            cache = build_cache(d, band[d], spine, prices, art, arq, ary)
            if not cache["metrics"]:
                continue
            pub, cand = screen_on(cache, d, prev_cand, prev_pub)
            if not pub:
                continue
            names = [r["ticker"] for r in pub]
            prev_cand = {r["ticker"] for r in cand}
            prev_pub = set(names)
            row = {"date": d, "n_eligible": len(cache["metrics"]),
                   "n_published": len(names), "tickers": names}
            for h, days in HORIZONS:
                endd = (date.fromisoformat(d)
                        + timedelta(days=days)).isoformat()
                if endd > end:
                    continue
                got = _basket_return(prices, spine, names, d, endd)
                bm = _bench_return(bench, d, endd)
                if got["assumed"] is None or bm is None:
                    continue
                row[h] = {"screen": round(got["assumed"], 3),
                          "screen_last_px": round(got["last_px"], 3),
                          "bench": round(bm, 3),
                          "excess": round(got["assumed"] - bm, 3),
                          "excess_last_px": round(got["last_px"] - bm, 3),
                          "dropped": got["dropped"],
                          "delisted": got["delisted"]}
                readings[h].append((d, got["assumed"] - bm))
                readings_alt[h].append((d, got["last_px"] - bm))
            cohorts.append(row)
        if verbose:
            print(f"  {year}: {len(chunk)} dates, "
                  f"{len(cohorts)} cohorts so far "
                  f"({time.monotonic() - t0:.0f}s)", flush=True)
    return cohorts, readings, readings_alt


def turnover(cohorts, step=1):
    """Share of the book that must be traded between consecutive rebalances.

    Counted as names REPLACED over names held: four of twenty-five swapped is
    16%, which means selling 16% of the book and buying 16% back.

    This lives here, with a test, because it is the number the whole
    conclusion rests on and it was previously computed in a throwaway script
    that nobody could audit or reproduce. `step` samples every nth cohort, so
    the same function answers for a slower cadence."""
    picks = cohorts[::step]
    shares = []
    for a, b in zip(picks, picks[1:]):
        sa, sb = set(a.get("tickers") or ()), set(b.get("tickers") or ())
        if sa and sb:
            shares.append(len(sa ^ sb) / 2 / len(sa))
    return statistics.fmean(shares) if shares else None


def cost_drag(turn, bps=None):
    """Friction per rebalance, as a percent, for that much turnover.

    Doubled because a replacement is two trades on that weight: the name
    leaving is sold and the name arriving is bought, each charged. Checked
    against the live ledger rather than argued: for book A's opening build
    plus one rebalance that swapped 4 of 25 names, this predicts $528 where
    the book was actually charged $525.86."""
    bps = portfolio.COST_BPS if bps is None else bps
    return turn * 2 * bps / 10_000 * 100


def break_even_bps(gross_per_period, turn):
    """The cost per side at which the edge exactly pays for its trading."""
    if not turn:
        return None
    return gross_per_period / (turn * 2) * 10_000 / 100


def confidence(mean, sd, n, z=1.96):
    """The band the evidence actually supports.

    Added because the first write-up of this backtest reported the point
    estimate as though it settled the question. It does not: at a t of 1.2
    the band on the gross edge spans zero, and the break-even cost it implies
    runs from below nothing to close to what the books charge. A point
    estimate quoted without this reads as a finding when it is a lean."""
    if not sd or not n:
        return None, None
    se = sd / math.sqrt(n)
    return mean - z * se, mean + z * se


def independent(pairs, min_gap_days):
    """Non-overlapping readings only: a 4-week return measured every week is
    the same month counted four times, and averaging those pretends to four
    times the evidence it has."""
    out, last = [], None
    for d, v in sorted(pairs):
        dd = date.fromisoformat(d)
        if last is None or (dd - last).days >= min_gap_days:
            out.append(v)
            last = dd
    return out


def summarise(cohorts, readings, readings_alt=None):
    out = {"cohorts": len(cohorts), "horizons": {}}
    tv = {1: turnover(cohorts, 1), 4: turnover(cohorts, 4)}
    out["turnover"] = {f"{k}w": (round(v, 4) if v else None)
                       for k, v in tv.items()}
    for h, days in HORIZONS:
        pairs = readings[h]
        if not pairs:
            continue
        vals = [v for _d, v in pairs]
        indep = independent(pairs, days)
        mean = statistics.fmean(indep) if indep else None
        sd = statistics.stdev(indep) if len(indep) > 1 else None
        se = (sd / math.sqrt(len(indep))) if sd else None
        lo, hi = confidence(mean, sd, len(indep)) if mean is not None \
            else (None, None)
        per_yr = 52 if h == "1w" else 13
        turn = tv[1 if h == "1w" else 4]
        cost = cost_drag(turn) if turn else None
        row = {
            "overlapping": len(vals),
            "independent": len(indep),
            "mean_excess": round(statistics.fmean(vals), 3),
            "mean_excess_independent": round(mean, 3) if mean is not None
            else None,
            "sd_independent": round(sd, 3) if sd else None,
            "t": round(mean / se, 2) if se else None,
            "share_positive": round(
                sum(1 for v in vals if v > 0) / len(vals), 3),
            "worst": round(min(vals), 2), "best": round(max(vals), 2),
            # the band the evidence supports, not just the middle of it
            "ci95_per_period": [round(lo, 4), round(hi, 4)]
            if lo is not None else None,
            "ci95_per_year": [round(lo * per_yr, 2), round(hi * per_yr, 2)]
            if lo is not None else None,
            "gross_per_year": round(mean * per_yr, 2) if mean is not None
            else None,
        }
        if cost is not None and mean is not None:
            row["turnover"] = round(turn, 4)
            row["cost_per_year"] = round(-cost * per_yr, 2)
            row["net_per_year"] = round(mean * per_yr - cost * per_yr, 2)
            row["break_even_bps"] = round(break_even_bps(mean, turn), 1)
            # and the same question asked of the band rather than the point
            row["break_even_bps_ci95"] = [
                round(break_even_bps(lo, turn), 1),
                round(break_even_bps(hi, turn), 1)] if lo is not None else None
        # the same horizon measured the other way, so the choice that
        # decides the sign of this backtest is visible beside the result
        alt = (readings_alt or {}).get(h) or []
        if alt:
            ai = independent(alt, days)
            am = statistics.fmean(ai) if ai else None
            asd = statistics.stdev(ai) if len(ai) > 1 else None
            alo, ahi = confidence(am, asd, len(ai))
            row["if_delistings_measured_at_last_price"] = {
                "mean_excess": round(am, 3) if am is not None else None,
                "gross_per_year": round(am * per_yr, 2) if am is not None
                else None,
                "t": round(am / (asd / math.sqrt(len(ai))), 2)
                if asd else None,
                "ci95_per_year": [round(alo * per_yr, 2),
                                  round(ahi * per_yr, 2)]
                if alo is not None else None,
                "net_per_year": round(am * per_yr - cost * per_yr, 2)
                if (cost is not None and am is not None) else None,
            }
        out["horizons"][h] = row

    # Names that could not be measured, summed rather than left scattered
    # across 1,212 rows. A quiet pile-up of dropped names is how a
    # survivorship-free universe turns into a survivor-only measurement, so
    # the total is reported whether or not anyone asks.
    drop = sum((c.get(h) or {}).get("dropped", 0)
               for c in cohorts for h, _d in HORIZONS)
    dele = sum((c.get(h) or {}).get("delisted", 0)
               for c in cohorts for h, _d in HORIZONS)
    slots = sum(len(c.get(h) or {}) and c["n_published"]
                for c in cohorts for h, _d in HORIZONS)
    out["names_measured"] = {
        "slots": slots, "dropped": drop, "delisted_booked_at_loss": dele,
        "dropped_share": round(drop / slots, 5) if slots else None}
    if cohorts:
        out["from"], out["to"] = cohorts[0]["date"], cohorts[-1]["date"]
        out["mean_eligible"] = round(
            statistics.fmean(c["n_eligible"] for c in cohorts))
    return out


CAVEATS = [
    "A different data vendor from the live screen: this tests the RULES on "
    "similar inputs, not the live system on its own inputs.",
    "Before trading costs, like the live evaluation block. The live books "
    "measure after costs, and that is the number most likely to decide it.",
    "The rules are frozen. If any parameter is chosen using this, every "
    "number here and in the live record becomes worthless.",
    "A backtest is weaker evidence than the forward record no matter how it "
    "comes out, and it cannot authorise real money.",
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="start", default="2004-01-01")
    ap.add_argument("--to", dest="end",
                    default=(date.today() - timedelta(days=35)).isoformat())
    ap.add_argument("--quick", action="store_true",
                    help="three years, to check it runs")
    ap.add_argument("--one", metavar="DATE",
                    help="score a single date and print the screen")
    ap.add_argument("--out", metavar="FILE", help="write the full result as JSON")
    args = ap.parse_args(argv)

    con = sharadar.open_store()
    chk = sharadar.verify_store(con)
    if not chk["ok"]:
        print("  the database failed verification; refusing to backtest on it",
              file=sys.stderr)
        return 2

    if args.one:
        d = args.one
        band = load_band(con, [d])[d]
        spine = load_spine(con)
        uni = sorted(band)
        lo = (date.fromisoformat(d) - timedelta(days=430)).isoformat()
        prices = load_prices(con, uni, lo, d)
        since = (date.fromisoformat(d) - timedelta(days=7 * 365)).isoformat()
        cache = build_cache(
            d, band, spine, prices,
            load_fundamentals(con, uni, d, since, "ART"),
            load_fundamentals(con, uni, d, since, "ARQ"),
            load_fundamentals(con, uni, d, since, "ARY"))
        pub, _cand = screen_on(cache, d)
        print(f"  {d}: {len(band)} in the size band, "
              f"{len(cache['metrics'])} with usable figures, "
              f"{len(pub)} published")
        print()
        print("  %-6s %6s %-26s %-16s %9s %8s" % (
            "tick", "score", "name", "group", "rev growth", "13wk"))
        for r in pub:
            print("  %-6s %6.1f %-26s %-16s %9s %8s" % (
                r["ticker"], r["score"], str(r["name"])[:26],
                str(r["group"])[:16],
                f"{r['rev_g']:+.1f}%" if r.get("rev_g") is not None else "—",
                f"{r['r13']:+.1f}%" if r.get("r13") is not None else "—"))
        con.close()
        return 0

    start = "2023-01-01" if args.quick else args.start
    print(f"  replaying {smallcap.MODEL_VERSION} from {start} to {args.end}")
    print(f"  database: {sharadar.db_path()}")
    print()
    cohorts, readings, readings_alt = run(con, start, args.end)
    con.close()
    rep = summarise(cohorts, readings, readings_alt)

    print()
    print(f"  {rep.get('cohorts', 0)} cohorts, "
          f"{rep.get('from')} to {rep.get('to')}, "
          f"mean {rep.get('mean_eligible')} companies eligible")
    print()
    for h, r in rep["horizons"].items():
        print(f"  {h}: {r['independent']} independent readings, mean "
              f"{r['mean_excess_independent']:+.3f}% per period, "
              f"sd {r['sd_independent']}, t {r['t']}")
        if r.get("ci95_per_year"):
            print(f"      gross a year {r['gross_per_year']:+.2f}%, and the "
                  f"95% band the evidence supports is "
                  f"{r['ci95_per_year'][0]:+.2f}% to "
                  f"{r['ci95_per_year'][1]:+.2f}%")
        if r.get("cost_per_year") is not None:
            print(f"      turnover {100 * r['turnover']:.0f}% per rebalance "
                  f"-> cost {r['cost_per_year']:+.2f}%/yr "
                  f"-> NET {r['net_per_year']:+.2f}%/yr")
            print(f"      break-even cost {r['break_even_bps']:.0f} bps/side "
                  f"(band {r['break_even_bps_ci95'][0]:.0f} to "
                  f"{r['break_even_bps_ci95'][1]:.0f}); the books charge "
                  f"{portfolio.COST_BPS:.0f}")
        print(f"      positive {100 * r['share_positive']:.0f}% of periods, "
              f"worst {r['worst']:+.1f}%, best {r['best']:+.1f}%")
        alt = r.get("if_delistings_measured_at_last_price")
        if alt:
            print(f"      IF a delisting is measured at its last traded "
                  f"price instead of the")
            print(f"      assumed loss: gross {alt['gross_per_year']:+.2f}%/yr "
                  f"(t {alt['t']}), net {alt['net_per_year']:+.2f}%/yr")
            print(f"      -- that choice is an assumption, and it decides "
                  f"the sign. Both are shown.")
        print()
    nm = rep.get("names_measured") or {}
    if nm.get("slots"):
        print(f"  names measured: {nm['slots']:,} holding-periods, "
              f"{nm['dropped']:,} unmeasurable "
              f"({100 * nm['dropped_share']:.2f}%), "
              f"{nm['delisted_booked_at_loss']:,} booked at the delisting loss")
        print()
    print("  Read with these in mind:")
    for c in CAVEATS:
        print(f"    - {c}")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"model": smallcap.MODEL_VERSION, "summary": rep,
             "caveats": CAVEATS, "cohorts": cohorts}, indent=1),
            encoding="utf-8")
        print(f"\n  full result -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
