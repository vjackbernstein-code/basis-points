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
                f"SELECT date, ticker, marketcap, ev FROM daily "
                f"WHERE date IN ({ph}) AND marketcap BETWEEN ? AND ?",
                (*part, lo, hi)):
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
            "SELECT ticker, name, exchange, sector, industry, category, "
            "isdelisted, lastpricedate FROM tickers "
            "WHERE category LIKE '%Common Stock%'"):
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
        for tk, d, ca, cu, v in con.execute(
                f"SELECT ticker, date, closeadj, closeunadj, volume "
                f"FROM stocks WHERE ticker IN ({ph}) AND date BETWEEN ? AND ? "
                f"ORDER BY ticker, date", (*part, lo, hi)):
            out.setdefault(tk, []).append((d, ca, cu, v))
    return out


FUND_FIELDS = ("revenue", "cor", "opinc", "ncfo", "cashneq", "de",
               "sharesbas", "equity")


def load_fundamentals(con, tickers, filed_to, since, dimension):
    """{ticker: [row, ...]} filed on or before `filed_to`, oldest first.

    Filtering on the FILING date in the query, not afterwards, is what makes
    this point-in-time. Everything downstream can then only see rows that
    were public."""
    cols = ", ".join(FUND_FIELDS)
    out = {}
    for part in _chunks(tickers):
        ph = ",".join("?" * len(part))
        for r in con.execute(
                f"SELECT ticker, reportperiod, date, {cols} FROM fundamentals "
                f"WHERE dimension=? AND ticker IN ({ph}) "
                f"AND date <= ? AND reportperiod >= ? "
                f"ORDER BY ticker, date", (dimension, *part, filed_to, since)):
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
    return [r for r in rows if r["date"] <= asof]


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

    This is where survivorship bias comes back if it is going to. A name that
    stops trading inside the window has no end price, and quietly leaving it
    out of the average is exactly the flattery the whole exercise exists to
    avoid — it removes the failures and keeps the survivors. A company that
    was delisted and never traded again is booked at the same assumed loss the
    live system books, so the two measurements mean the same thing. A name
    merely missing a print is dropped, and dropped names are counted and
    reported rather than absorbed."""
    rets, dropped, delisted = [], 0, 0
    for tk in tickers:
        px = prices.get(tk) or []
        a = _price_on_or_before(px, start)
        if not a:
            dropped += 1
            continue
        b = _price_on_or_before(px, end)
        if b and b[0] > a[0]:
            rets.append((b[1] / a[1] - 1) * 100)
            continue
        meta = spine.get(tk) or {}
        if meta.get("delisted") and (meta.get("last_price") or "") <= end:
            rets.append(-smallcap.DELIST_ASSUMED_LOSS * 100)
            delisted += 1
        else:
            dropped += 1
    if not rets:
        return None, dropped, delisted
    return sum(rets) / len(rets), dropped, delisted


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
    assert filed, "no filing-date column"
    dates = mondays(max(start, EARLIEST), end)
    if not dates:
        raise RuntimeError("no dates in range")

    readings = {h: [] for h, _ in HORIZONS}
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
        art = load_fundamentals(con, universe, chunk[-1], since, "ART")
        arq = load_fundamentals(con, universe, chunk[-1], since, "ARQ")
        ary = load_fundamentals(con, universe, chunk[-1], since, "ARY")

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
                mine, dropped, dl = _basket_return(prices, spine, names,
                                                   d, endd)
                bm = _bench_return(bench, d, endd)
                if mine is None or bm is None:
                    continue
                row[h] = {"screen": round(mine, 3), "bench": round(bm, 3),
                          "excess": round(mine - bm, 3),
                          "dropped": dropped, "delisted": dl}
                readings[h].append((d, mine - bm))
            cohorts.append(row)
        if verbose:
            print(f"  {year}: {len(chunk)} dates, "
                  f"{len(cohorts)} cohorts so far "
                  f"({time.monotonic() - t0:.0f}s)", flush=True)
    return cohorts, readings


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


def summarise(cohorts, readings):
    out = {"cohorts": len(cohorts), "horizons": {}}
    for h, days in HORIZONS:
        pairs = readings[h]
        if not pairs:
            continue
        vals = [v for _d, v in pairs]
        indep = independent(pairs, days)
        mean = statistics.fmean(vals)
        sd = statistics.stdev(indep) if len(indep) > 1 else None
        se = (sd / math.sqrt(len(indep))) if sd else None
        out["horizons"][h] = {
            "overlapping": len(vals),
            "independent": len(indep),
            "mean_excess": round(mean, 3),
            "mean_excess_independent": (round(statistics.fmean(indep), 3)
                                        if indep else None),
            "sd_independent": round(sd, 3) if sd else None,
            "t": (round(statistics.fmean(indep) / se, 2)
                  if se else None),
            "share_positive": round(
                sum(1 for v in vals if v > 0) / len(vals), 3),
            "worst": round(min(vals), 2), "best": round(max(vals), 2),
        }
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
    cohorts, readings = run(con, start, args.end)
    con.close()
    rep = summarise(cohorts, readings)

    print()
    print(f"  {rep.get('cohorts', 0)} cohorts, "
          f"{rep.get('from')} to {rep.get('to')}, "
          f"mean {rep.get('mean_eligible')} companies eligible")
    print()
    for h, r in rep["horizons"].items():
        print(f"  {h}: mean excess {r['mean_excess']:+.3f}% over "
              f"{r['overlapping']} overlapping readings")
        print(f"      independent: {r['independent']} readings, mean "
              f"{r['mean_excess_independent']:+.3f}%, "
              f"sd {r['sd_independent']}, t {r['t']}")
        print(f"      positive {100 * r['share_positive']:.0f}% of the time, "
              f"worst {r['worst']:+.1f}%, best {r['best']:+.1f}%")
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
