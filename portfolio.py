#!/usr/bin/env python3
"""
Paper portfolios for Basis Points — SIMULATIONS. No money is ever involved.

Several books are run side by side over the same screen, the same prices and
the same frictions, differing only in their construction rules. The point is
comparison: a single clever portfolio can look good for reasons that have
nothing to do with its cleverness, and without a plain control there is no way
to tell. Book A is therefore deliberately dull and never changes.

  A  Baseline       equal weight, no stop, full exposure          (the control)
  B  Conviction     weight by RANK (not by raw score — see below)
  C  Risk-managed   equal weight + volatility-scaled trailing stop
  D  Regime-aware   equal weight, exposure shaded in a falling tape
  E  Combined       B + C + D together

The forms follow established practice rather than invention (reviewed Sept
2026). Three corrections came out of that review and are worth remembering:
rank-weighting instead of score-proportional, because a composite score is
ordinal and proportional weighting is not even scale-invariant; stops scaled
to each name's own volatility instead of a flat percentage, because a flat
stop culls the most volatile names and so places a factor bet nobody intended;
and a shallow regime response that never reaches zero, because a filter on one
index yields roughly two independent signals a year.

The same review argues the evidence is AGAINST per-name stops in this exact
setting — long-only, weekly, small-cap, with momentum already in the score.
Book C exists to measure that, not because it is expected to win.

Shared rules: hold the published screen, rebalance weekly, charge COST_BPS per
side, never trade a name without a usable price, restart when MODEL_VERSION
changes (a book spanning two models measures nothing).

HONESTY NOTE ON STOPS. Prices here are sampled a few times a day, not
continuously. A simulated stop therefore assumes an exit at roughly the stop
price, whereas a real stop in a thin small-cap gaps through and fills worse.
STOP_SLIPPAGE_BPS charges extra for that, but the simulation remains
optimistic about stops by construction. Treat book C and E returns as an
upper bound, not an estimate.

Simulated results also omit what hurts real traders most: the market moving
against a real order, borrow costs, tax, and the discipline required to follow
a system through a drawdown.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import smallcap

BASE = Path(__file__).resolve().parent
LEDGER_PATH = BASE / "data" / "portfolio.json"

START_CAPITAL = 100_000.0        # notional; the percentages are what matter
COST_BPS = 40.0                  # per side. Raised from 25 after an audit
                                 # measured this screen's real turnover at
                                 # 18-44x the book per year and found 8 of 25
                                 # published names trading under $5M/day. At
                                 # that churn the friction assumption is not a
                                 # haircut on the answer, it may BE the answer.
COST_BPS_PESSIMISTIC = 60.0      # published alongside, as an upper bound
STOP_SLIPPAGE_BPS = 75.0         # EXTRA cost when a stop fires: real stops gap
                                 # through in thin names. Still optimistic.
REBALANCE_WEEKDAY = 0            # Monday (0=Mon ... 6=Sun)
# Trade only while the US market is open. A quote FETCHED on Sunday carries
# Friday's closing print with an age of zero, so the staleness gate cannot see
# a closed market: the book was buying at exactly the price that put the name
# on the screen, collecting a free trading day of drift on every rebalance.
MARKET_OPEN_UTC, MARKET_CLOSE_UTC = 14, 20
REBALANCE_OVERDUE_DAYS = 10      # failsafe so an outage cannot freeze the book
STALE_BENCH_H = 72               # the benchmark gets the same gate as holdings
# A holding that simply stops being quoted is usually not fine. Write it down
# on a schedule rather than carrying it at full value until the next rebalance.
DELIST_WRITEDOWN = ((3, 0.30), (10, 0.60), (30, 1.00))
MAX_POSITIONS = 25               # matches the published screen
HISTORY_DAYS = 400
LEDGER_FORMAT = 2                # bump to discard incompatible old ledgers

# Conviction sizing is RANK-based, not score-proportional. A 0-100 composite is
# ordinal: its numeric gaps are not calibrated to expected-return magnitudes, so
# proportional weighting is not even scale-invariant (rescaling the same ranking
# to 50-100 would produce a different portfolio). Bounds follow the institutional
# convention of a <=2:1 max/min ratio on a concentrated book.
CONVICTION_MAX = 1.5             # x equal weight  (~6% of a 25-name book)
CONVICTION_MIN = 0.6             # x equal weight  (~2.5%)

# Stops are scaled to each name's OWN volatility and trail the high-water mark.
# A fixed percentage applied across names running 25%-90% annualised volatility
# fires mostly on the volatile ones, which quietly culls exactly the high-beta
# growth names the strategy is built to hold — a factor bet nobody intended.
STOP_SIGMA = 3.0                 # multiples of the name's weekly volatility
STOP_MIN, STOP_MAX = 0.10, 0.40  # floor/ceiling on the resulting distance
STOP_DEFAULT = 0.25              # when a name's volatility is unknown

# Regime response is deliberately shallow and never goes to zero: a filter on a
# single index supplies roughly two independent signals a year, which cannot be
# validated in any reasonable time, and a binary switch would need to be right
# ~74% of the time merely to break even (Sharpe, 1975).
REGIME_EXPOSURE = {              # fraction of the book invested, by tape
    "correction": 0.65,
    "flat/choppy": 0.90,
    "early rebound": 1.0,
    "uptrend": 1.0,
}

# Do not trade a name whose weight has merely drifted. Pure cost control, no
# return forecast attached.
NO_TRADE_BAND = 0.25             # relative deviation from target

STRATEGIES = {
    "A": {"label": "Baseline", "sizing": "equal", "stop": None, "regime": False,
          "note": "the control — equal weight, no stop, always fully invested"},
    "B": {"label": "Conviction", "sizing": "score", "stop": None, "regime": False,
          "note": "weighted by rank, 1.5x down to 0.6x equal weight"},
    "C": {"label": "Risk-managed", "sizing": "equal", "stop": True, "regime": False,
          "note": "equal weight, trailing stop at 3x the name's own weekly volatility"},
    "D": {"label": "Regime-aware", "sizing": "equal", "stop": None, "regime": True,
          "note": "equal weight, exposure cut when the small-cap tape falls"},
    "E": {"label": "Combined", "sizing": "score", "stop": True, "regime": True,
          "note": "conviction + stop + regime together"},
}


def _today():
    return smallcap._now().strftime("%Y-%m-%d")


def _blank_book():
    return {"cash": START_CAPITAL, "positions": {}, "last_rebalance": None,
            "history": [], "trades": [], "costs_paid": 0.0, "stops_hit": 0,
            "started": None, "start_bench": None,
            # carried monotonically, NOT recomputed from the history window:
            # a peak that scrolled out of the window used to be forgotten, so
            # the worst dip could only ever shrink toward zero with time — on
            # the single statistic a reader leans on hardest
            "peak_value": START_CAPITAL, "max_drawdown": 0.0}


def load_ledger():
    led = {}
    if LEDGER_PATH.exists():
        try:
            led = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except ValueError:
            led = {}
    if (led.get("format") != LEDGER_FORMAT
            or led.get("v") != smallcap.MODEL_VERSION):
        # RETIRE, never delete. Wiping the books on a version change and
        # starting clean is mechanically what closing a fund after a bad year
        # and reopening looks like — and the incentive to revise is strongest
        # exactly after a bad stretch. Every closed book stays on the record.
        retired = list(led.get("retired") or [])
        for key, book in (led.get("books") or {}).items():
            st = _stats(book)
            if st:
                retired.append({"v": led.get("v"), "key": key,
                                "ret": st["ret"], "max_drawdown": st["max_drawdown"],
                                "days": st["days"], "started": st["started"],
                                "retired_on": _today()})
        led = {"format": LEDGER_FORMAT, "v": smallcap.MODEL_VERSION,
               "restarted_from": led.get("v"), "books": {},
               "retired": retired[-100:]}
    led.setdefault("books", {})
    led.setdefault("retired", [])
    for key in STRATEGIES:
        led["books"].setdefault(key, _blank_book())
    return led


def save_ledger(led):
    LEDGER_PATH.parent.mkdir(exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(led, separators=(",", ":")), encoding="utf-8")


# ------------------------------------------------------------- pricing -------


def _price(cache, ticker, fallback=None):
    """Latest known price, or the last mark. Never invent a price."""
    q = (cache.get("quotes") or {}).get(ticker) or {}
    px = q.get("px")
    if px and smallcap._age_h(q.get("t")) < 72:
        return float(px)
    return fallback


def refresh_marks(book, cache, today=None):
    """Re-mark every holding at current prices.

    A holding that stops being quoted is written DOWN on a schedule rather than
    carried at its last good mark. The events that make a quote disappear —
    bankruptcy, a fraud halt, deregistration — are exactly the ones that cost
    real money, so carrying the last mark made the book structurally incapable
    of losing anything to them, and made a blow-up impossible to see in the
    drawdown.
    """
    today = today or _today()
    for tick, pos in book["positions"].items():
        px = _price(cache, tick, None)
        if px:
            pos["last_px"] = px
            pos["peak_px"] = max(pos.get("peak_px") or px, px)
            pos["last_quote"] = today
            continue
        seen = pos.get("last_quote") or pos.get("entry_date") or today
        dark = (datetime.fromisoformat(today).date()
                - datetime.fromisoformat(seen).date()).days
        good = pos.get("good_px") or pos.get("last_px") or pos["entry_px"]
        pos["good_px"] = good
        cut = 0.0
        for days, frac in DELIST_WRITEDOWN:
            if dark >= days:
                cut = frac
        pos["last_px"] = good * (1 - cut)


# ------------------------------------------------------------- sizing --------


def target_weights(screen, spec):
    """Fraction of the invested book each name should hold."""
    names = [r["ticker"] for r in screen[:MAX_POSITIONS]]
    if not names:
        return {}
    if spec["sizing"] != "score":
        return {t: 1.0 / len(names) for t in names}
    scores = {r["ticker"]: float(r.get("score") or 0.0) for r in screen[:MAX_POSITIONS]}
    n = len(names)
    lo, hi = CONVICTION_MIN / n, CONVICTION_MAX / n
    # rank-based ramp from the cap down to the floor — the ranking is what the
    # score is entitled to assert; the size of its gaps is not
    order = sorted(names, key=lambda t: (-scores.get(t, 0.0), t))
    span = max(n - 1, 1)
    w = {t: (CONVICTION_MAX - (CONVICTION_MAX - CONVICTION_MIN) * i / span) / n
         for i, t in enumerate(order)}
    # Clamp and redistribute until every weight is inside its bounds AND the
    # weights still sum to 1. Clamping once and then renormalising does NOT
    # work: pushing the small names up inflates the big one back over its cap,
    # so the advertised limit would be false. Bounds of 0.5x-2x equal weight
    # always admit a solution, so this converges.
    for _ in range(50):
        w = {t: min(hi, max(lo, x)) for t, x in w.items()}
        gap = 1.0 - sum(w.values())
        if abs(gap) < 1e-12:
            break
        free = [t for t, x in w.items() if lo < x < hi] or list(w)
        share = gap / len(free)
        w = {t: (x + share if t in free else x) for t, x in w.items()}
    return w


def exposure_for(spec, cache):
    """How much of the book is invested at all (the regime overlay)."""
    if not spec.get("regime"):
        return 1.0
    label = ((cache.get("regime") or {}).get("label") or "").strip()
    return REGIME_EXPOSURE.get(label, 1.0)


# ------------------------------------------------------------- trading -------


def _sell(book, tick, px, today, extra_bps=0.0, reason="rebalance"):
    pos = book["positions"].pop(tick)
    gross = pos["shares"] * px
    cost = gross * (COST_BPS + extra_bps) / 10_000
    book["cash"] += gross - cost
    book["costs_paid"] += cost
    book["trades"].append({"date": today, "ticker": tick, "side": "sell",
                           "shares": round(pos["shares"], 4), "px": round(px, 4),
                           "cost": round(cost, 2), "why": reason})


def stop_distance(cache, ticker):
    """How far below its high-water mark a name may fall before it is sold,
    expressed in its OWN weekly volatility rather than a flat percentage."""
    m = (cache.get("metrics") or {}).get(ticker) or {}
    ann = m.get("vol")
    if not ann:
        return STOP_DEFAULT
    weekly = (float(ann) / 100.0) / (52 ** 0.5)
    return max(STOP_MIN, min(STOP_MAX, STOP_SIGMA * weekly))


def apply_stops(book, spec, cache, today):
    """Sell anything that has fallen through its trailing stop. Runs EVERY day,
    not only on rebalance days — a stop that checked weekly would be fiction.

    The exit is charged STOP_SLIPPAGE_BPS on top of ordinary friction. That is
    a proxy for gap risk: we hold prices sampled a few times a day, so we
    cannot see the intraday path, and a real stop in a thin small-cap fills
    well below its trigger. This remains optimistic.
    """
    if not spec.get("stop"):
        return
    for tick in list(book["positions"]):
        pos = book["positions"][tick]
        px = _price(cache, tick, pos.get("last_px"))
        if not px:
            continue
        dist = stop_distance(cache, tick)
        peak = pos.get("peak_px") or pos["entry_px"]
        if px <= peak * (1 - dist):
            _sell(book, tick, px, today, extra_bps=STOP_SLIPPAGE_BPS, reason="stop")
            book["stops_hit"] += 1


def rebalance(book, spec, cache, screen, today):
    weights = target_weights(screen, spec)
    priced = {t: _price(cache, t) for t in weights}
    weights = {t: w for t, w in weights.items() if priced.get(t)}
    if not weights:
        return
    total_w = sum(weights.values()) or 1.0
    weights = {t: w / total_w for t, w in weights.items()}
    exposure = exposure_for(spec, cache)

    for tick in list(book["positions"]):
        if tick not in weights:
            px = _price(cache, tick, book["positions"][tick].get("last_px"))
            _sell(book, tick, px or book["positions"][tick]["entry_px"], today)

    equity = book["cash"] + sum(p["shares"] * (p.get("last_px") or p["entry_px"])
                                for p in book["positions"].values())
    # reserve the friction so the last name bought is not left underweight
    investable = (equity * exposure) / (1 + COST_BPS / 10_000)
    for tick, w in weights.items():
        px = priced[tick]
        slot = investable * w
        held = book["positions"].get(tick)
        have = held["shares"] * px if held else 0.0
        delta_value = slot - have
        if abs(delta_value) < max(slot, 1.0) * NO_TRADE_BAND:   # ignore drift
            continue
        shares = delta_value / px
        gross = abs(delta_value)
        cost = gross * COST_BPS / 10_000
        if shares > 0 and book["cash"] < gross + cost:
            shares = max(0.0, (book["cash"] - cost) / px)
            if shares <= 0:
                continue
            gross = shares * px
            cost = gross * COST_BPS / 10_000
        book["cash"] -= shares * px + cost
        book["costs_paid"] += cost
        if held:
            held["shares"] += shares
            held["last_px"] = px
            if held["shares"] <= 1e-9:
                book["positions"].pop(tick, None)
        else:
            book["positions"][tick] = {"shares": shares, "entry_px": px,
                                       "peak_px": px, "entry_date": today,
                                       "last_px": px}
        book["trades"].append({"date": today, "ticker": tick,
                               "side": "buy" if shares > 0 else "sell",
                               "shares": round(abs(shares), 4),
                               "px": round(px, 4), "cost": round(cost, 2),
                               "why": "rebalance"})
    book["last_rebalance"] = today
    book["trades"] = book["trades"][-200:]


def _market_open_now():
    """Only trade while the US market is actually open. Quote freshness cannot
    detect a closed market — a price fetched on Sunday carries Friday's close
    with an age of zero — so without this the book bought at precisely the
    price that had put the name on the screen."""
    now = smallcap._now()
    return (now.weekday() < 5
            and MARKET_OPEN_UTC <= now.hour < MARKET_CLOSE_UTC)


def _due_for_rebalance(book, today):
    if book["last_rebalance"] is None:
        return _market_open_now()
    last = datetime.fromisoformat(book["last_rebalance"]).date()
    now = datetime.fromisoformat(today).date()
    gap = (now - last).days
    if gap >= REBALANCE_OVERDUE_DAYS:
        return True                      # failsafe: an outage must not freeze it
    if not _market_open_now():
        return False
    return gap >= 7 or (now.weekday() == REBALANCE_WEEKDAY and now != last)


def book_value(book):
    return book["cash"] + sum(p["shares"] * (p.get("last_px") or p["entry_px"])
                              for p in book["positions"].values())


# ------------------------------------------------------------- driver --------


def update(cache, screen):
    led = load_ledger()
    today = _today()
    # the benchmark gets the same staleness gate as the holdings. Marking the
    # book up against a frozen benchmark inflates excess return one-directionally
    b = cache.get("bench") or {}
    bench = b.get("iwo") if smallcap._age_h(b.get("t")) < STALE_BENCH_H else None

    for key, spec in STRATEGIES.items():
        book = led["books"][key]
        # value first: sizing off stale marks mis-weights every position
        refresh_marks(book, cache, today)
        apply_stops(book, spec, cache, today)
        if screen and _due_for_rebalance(book, today):
            if book["started"] is None:
                book["started"] = today
                book["start_bench"] = bench
            rebalance(book, spec, cache, screen, today)
            refresh_marks(book, cache, today)
        if book["started"]:
            value = book_value(book)
            # peak and worst dip are carried forward, never recomputed from the
            # visible window, so an old peak cannot scroll out of memory
            book["peak_value"] = max(book.get("peak_value") or START_CAPITAL, value)
            book["max_drawdown"] = min(
                book.get("max_drawdown", 0.0),
                (value / book["peak_value"] - 1) * 100)
            hist = book["history"]
            point = {"date": today, "value": round(value, 2), "bench": bench}
            if hist and hist[-1]["date"] == today:
                hist[-1] = point
            else:
                hist.append(point)
            book["history"] = hist[-HISTORY_DAYS:]
    save_ledger(led)
    return led


def _stats(book):
    hist = book.get("history") or []
    if not hist or not book.get("started"):
        return None
    value = hist[-1]["value"]
    ret = (value / START_CAPITAL - 1) * 100
    # the opening benchmark from the median of the first few marks, not one
    # tick: a single reading taken at a market-closed instant is frozen into
    # every excess figure the book will ever publish
    firsts = [h["bench"] for h in hist[:3] if h.get("bench")]
    b0 = book.get("start_bench") or (sorted(firsts)[len(firsts) // 2] if firsts else None)
    # the CURRENT mark must be current: walking back to the last known value
    # would resume marking the book up against a frozen benchmark, which is
    # the exact one-directional flattery the staleness gate exists to stop
    b1 = hist[-1].get("bench")
    bench_ret = (b1 / b0 - 1) * 100 if b0 and b1 else None
    # GEOMETRIC excess. Subtracting cumulative percentages overstates whenever
    # the benchmark is up — +60% against +30% is +23.1%, not +30%.
    excess = (((1 + ret / 100) / (1 + bench_ret / 100) - 1) * 100
              if bench_ret is not None else None)
    days = len(hist)
    friction_yr = (book.get("costs_paid", 0.0) / START_CAPITAL * 100
                   * 365 / max(days, 1))
    return {
        "value": round(value, 2), "ret": round(ret, 2),
        "bench_ret": round(bench_ret, 2) if bench_ret is not None else None,
        "excess": round(excess, 2) if excess is not None else None,
        "friction_yr": round(friction_yr, 1),
        "max_drawdown": round(book.get("max_drawdown", 0.0), 2),
        "positions": len(book.get("positions", {})),
        "cash": round(book.get("cash", 0.0), 2),
        "costs_paid": round(book.get("costs_paid", 0.0), 2),
        "stops_hit": book.get("stops_hit", 0),
        "days": len(hist), "started": book["started"],
    }


def summarize(led=None):
    led = led or load_ledger()
    books = []
    for key, spec in STRATEGIES.items():
        st = _stats(led["books"].get(key) or {})
        if st:
            books.append({"key": key, "label": spec["label"],
                          "note": spec["note"], **st})
    base = led["books"].get("A") or {}
    holdings = []
    for tick, p in (base.get("positions") or {}).items():
        px = p.get("last_px") or p["entry_px"]
        holdings.append({"ticker": tick, "value": round(p["shares"] * px, 2),
                         "entry_date": p.get("entry_date"),
                         "ret": (px / p["entry_px"] - 1) * 100 if p.get("entry_px") else 0.0})
    holdings.sort(key=lambda h: -h["value"])
    trades = sorted((t for b in led["books"].values() for t in b.get("trades", [])),
                    key=lambda t: t["date"], reverse=True)
    return {
        "status": "running" if books else "not started",
        "v": led.get("v"),
        "books": books,
        "holdings": holdings[:12],
        "trades": trades[:10],
        "assumptions": {
            "capital": START_CAPITAL, "cost_bps": COST_BPS,
            "stop_slippage_bps": STOP_SLIPPAGE_BPS,
            "cadence": "weekly (Monday)", "stop_sigma": STOP_SIGMA,
            "conviction_cap": CONVICTION_MAX,
        },
    }
