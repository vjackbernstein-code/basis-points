#!/usr/bin/env python3
"""The December decision rule, PRE-REGISTERED.

Written on 2026-09-23, with the live record eight readings old and the paper
books three days old — that is, before anyone involved knows how it comes out.
That timing is the whole point of the file.

Without a rule fixed in advance, the review on 2026-12-14 is a person looking
at a number and deciding what it means, having already seen it. That is the
moment motivated reasoning does its work: +1.5% becomes "promising, extend the
test", -1.5% becomes "the regime was unfavourable, extend the test", and the
project never ends, never concludes, and never costs anyone the discomfort of
being wrong. Every outcome below is reachable, and one of them is "stop".

The rule is published on the site and its current standing is rendered on every
build, so it cannot be revised quietly: changing it means changing this file in
a public repository, with the old version in the history beside it.

WHAT THIS RULE CANNOT DO
    It cannot authorise real money, and no result below does. Twelve weeks is
    not enough evidence to trade on, whatever it says. A strategy with a real
    edge and a strategy with none look very similar over twelve weeks; the best
    available outcome here is "keep going, with the rules still frozen".
    Pretending otherwise now would be setting a trap for December.
"""

import math

REVIEW_DATE = "2026-12-14"
WRITTEN_ON = "2026-09-23"
# Named NOW, for the same reason the rest of this file is. Three of the four
# outcomes below end in "continue to a new checkpoint", and a new checkpoint
# with no date is how a deadline quietly becomes never — each review deferring
# to a next one that is always a comfortable distance away. This is that date.
SECOND_CHECKPOINT = "2027-03-15"

# Gate 1 — is there a signal at all, before costs?
MIN_INDEP_1W = 12          # independent (non-overlapping) 1-week readings
MIN_INDEP_4W = 3           # independent 4-week readings
T_MIN = 2.0                # mean / standard error of the independent readings
# Not a significance claim. A pre-registered threshold, chosen because the
# standard error on twelve small-cap weekly readings is roughly half a point,
# so anything under about +1% cannot be distinguished from luck at this length.

# Gate 3 — is the result broad, or is it a few names?
BREADTH_TOP_N = 3          # remove this many best contributors and re-check

VERDICTS = {
    "insufficient": (
        "Not enough evidence yet",
        "The record did not reach the bar. No rule changes are authorised. "
        "The books continue unchanged to the second checkpoint, "
        f"{SECOND_CHECKPOINT}."),
    "abandon": (
        "The screen is not earning its costs — stop",
        "The evidence is in and it does not support trading this. The honest "
        "action is to stop, not to adjust the rules until it passes. Any "
        "successor must start a new record from zero."),
    "inconclusive": (
        "Inconclusive — continue unchanged",
        "Some gates passed and some did not. This is the most likely outcome "
        "at this length and it authorises nothing: no rule changes, no real "
        f"money, continue to the second checkpoint, {SECOND_CHECKPOINT}."),
    "continue": (
        "Evidence is positive — continue the paper test, still frozen",
        "All three gates passed. This does NOT authorise real money and does "
        "not lift the freeze. It authorises continuing, with a longer record, "
        f"to the second checkpoint, {SECOND_CHECKPOINT}."),
}


def _mean_sd(xs):
    n = len(xs)
    if n == 0:
        return None, None
    m = sum(xs) / n
    if n < 2:
        return m, None
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var)


def gate_signal(ev):
    """Does the screen beat its benchmark before costs, by more than noise?"""
    one = (ev or {}).get("1w") or {}
    four = (ev or {}).get("4w") or {}
    xs = one.get("indep_values") or []
    n, n4 = len(xs), four.get("indep", 0)
    m, sd = _mean_sd(xs)
    # sd is None only when there are too few readings to have a spread at all.
    # sd of exactly zero is the opposite case — a perfectly consistent record,
    # the strongest evidence there is — and must not be treated as unmeasurable
    # or the gate has a branch it can never pass through.
    if sd is None:
        se = t = None
    elif sd == 0:
        se = 0.0
        t = math.inf if m > 0 else (-math.inf if m < 0 else 0.0)
    else:
        se = sd / math.sqrt(n)
        t = m / se
    enough = n >= MIN_INDEP_1W and n4 >= MIN_INDEP_4W
    return {
        "name": "A signal at all",
        "asks": (f"mean of {MIN_INDEP_1W} independent 1-week readings above "
                 f"zero, by at least {T_MIN} standard errors"),
        "n": n, "n4": n4, "mean": round(m, 3) if m is not None else None,
        "sd": round(sd, 3) if sd is not None else None,
        "t": (None if t is None
              else (t if math.isinf(t) else round(t, 2))),
        "enough": enough,
        "passed": bool(enough and m is not None and m > 0 and t is not None
                       and t >= T_MIN),
        "failed": bool(enough and m is not None and m <= 0),
    }


def gate_costs(books):
    """Does anything survive the cost of the trading it takes to run it?

    Judged on excess measured over the window EVERY book shares. A book added
    mid-flight also missed whatever the index did before it opened, so its
    since-inception excess flatters or damns it for nothing it did."""
    live = [b for b in (books or [])
            if b.get("excess_common", b.get("excess")) is not None]
    key_of = lambda b: b.get("excess_common", b.get("excess"))
    best = max(live, key=key_of) if live else None
    return {
        "name": "Survives its costs",
        "asks": ("at least one book ahead of the index after all trading "
                 "costs, over the period every book shares"),
        "best": best["key"] if best else None,
        "best_label": best["label"] if best else None,
        "excess": key_of(best) if best else None,
        "enough": bool(live),
        "passed": bool(best and key_of(best) > 0),
        "failed": bool(live and best and key_of(best) <= 0),
    }


def gate_breadth(books, detail):
    """Is the result the screen working, or is it three lucky names?

    A book that beats the index because of its three best holdings has not
    shown that ranking small companies this way works. It has shown that three
    companies went up. The effective sample size there is three, not twelve.
    """
    best, res = None, None
    for b in (books or []):
        a = ((detail or {}).get(b["key"]) or {}).get("attribution")
        if not a or b.get("excess") is None:
            continue
        if best is None or b["excess"] > best["excess"]:
            best, res = b, a
    if not best:
        return {"name": "Broad, not a few names",
                "asks": (f"the leading book still ahead of the index with its "
                         f"{BREADTH_TOP_N} best holdings removed"),
                "enough": False, "passed": False, "failed": False}
    bench = best.get("bench_ret")
    ex_top = res.get("ex_top_pct")
    survives = (ex_top is not None and bench is not None and ex_top > bench)
    return {
        "name": "Broad, not a few names",
        "asks": (f"the leading book still ahead of the index with its "
                 f"{BREADTH_TOP_N} best holdings removed"),
        "book": best["key"], "ex_top": ex_top, "bench": bench,
        "top_pct": res.get("top_pct"),
        "winners": res.get("winners"), "losers": res.get("losers"),
        "enough": True, "passed": bool(survives), "failed": bool(not survives),
    }


def assess(data):
    """Where the pre-registered rule currently stands. Computed every build, so
    the answer is visible long before the date it is meant to be read on."""
    sc = data.get("smallcap") or {}
    pf = data.get("portfolio") or {}
    g = [gate_signal(sc.get("evaluation")),
         gate_costs(pf.get("books")),
         gate_breadth(pf.get("books"), pf.get("detail"))]
    signal = g[0]
    if not signal["enough"]:
        key = "insufficient"
    elif signal["failed"] or (g[1]["enough"] and g[1]["failed"]):
        # no signal, or nothing survives its costs
        key = "abandon"
    elif all(x["passed"] for x in g):
        key = "continue"
    else:
        key = "inconclusive"
    title, body = VERDICTS[key]
    return {"gates": g, "verdict": key, "title": title, "body": body,
            "review_date": REVIEW_DATE, "written_on": WRITTEN_ON}
