#!/usr/bin/env python3
"""
Paper portfolio for Basis Points — a SIMULATION. No money is ever involved.

This layer answers the question the ranked screen cannot: if you actually held
the published names, rebalanced on a schedule, and paid real-world frictions,
what would have happened? It is deliberately the dullest possible translation
of the screen into a portfolio, because every discretionary knob added here is
a parameter nobody has evidence to set:

  * the portfolio IS the published screen — the top SCREEN_SIZE names, equally
    weighted. No conviction sizing (we have no evidence conviction is real),
    no stop-losses, no overlays.
  * rebalanced WEEKLY, not daily. The screen churns a little every day and
    small-cap spreads are wide; daily rebalancing would measure friction
    rather than skill.
  * frictions are charged explicitly on both sides of every trade, as a stated
    assumption rather than a hidden one.
  * the ledger restarts when MODEL_VERSION changes, exactly like the live
    track record — a portfolio spanning two different models means nothing.

Everything here is hypothetical. Simulated results omit the things that hurt
real traders most: the market moving against a real order, borrow costs, tax,
and the discipline required to follow a system through a drawdown.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import smallcap

BASE = Path(__file__).resolve().parent
LEDGER_PATH = BASE / "data" / "portfolio.json"

START_CAPITAL = 100_000.0        # notional; the percentages are what matter
COST_BPS = 25.0                  # per side, in basis points (0.25%). Covers
                                 # spread + slippage; small caps are not free
                                 # to trade. An ASSUMPTION, not a measurement.
REBALANCE_WEEKDAY = 0            # Monday (0=Mon ... 6=Sun)
MAX_POSITIONS = 25               # matches the published screen
HISTORY_DAYS = 400


def _today():
    return smallcap._now().strftime("%Y-%m-%d")


def load_ledger():
    if LEDGER_PATH.exists():
        led = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    else:
        led = {}
    led.setdefault("v", smallcap.MODEL_VERSION)
    led.setdefault("started", None)
    led.setdefault("cash", START_CAPITAL)
    led.setdefault("positions", {})       # ticker -> {shares, entry_px, entry_date, last_px}
    led.setdefault("last_rebalance", None)
    led.setdefault("history", [])         # [{date, value, bench}]
    led.setdefault("trades", [])
    led.setdefault("costs_paid", 0.0)
    return led


def save_ledger(led):
    LEDGER_PATH.parent.mkdir(exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(led, separators=(",", ":")), encoding="utf-8")


def _reset(led):
    """A ledger spanning two model versions measures nothing. Start over."""
    return {
        "v": smallcap.MODEL_VERSION,
        "started": None,
        "cash": START_CAPITAL,
        "positions": {},
        "last_rebalance": None,
        "history": [],
        "trades": [],
        "costs_paid": 0.0,
        "restarted_from": led.get("v"),
    }


def _price(cache, ticker, fallback=None):
    """Latest known price, or the last one we marked. Never invent a price."""
    q = (cache.get("quotes") or {}).get(ticker) or {}
    px = q.get("px")
    if px and smallcap._age_h(q.get("t")) < 72:
        return float(px)
    return fallback


def mark_to_market(led, cache):
    """Value the book at today's prices. Holdings whose quote has gone stale
    keep their last known mark rather than silently vanishing."""
    total = led["cash"]
    for tick, pos in led["positions"].items():
        px = _price(cache, tick, pos.get("last_px"))
        if px:
            pos["last_px"] = px
        total += pos["shares"] * (pos.get("last_px") or pos["entry_px"])
    return total


def _due_for_rebalance(led, today):
    if led["last_rebalance"] is None:
        return True
    last = datetime.fromisoformat(led["last_rebalance"]).date()
    now = datetime.fromisoformat(today).date()
    if (now - last).days >= 7:
        return True
    return now.weekday() == REBALANCE_WEEKDAY and now != last


def rebalance(led, cache, screen, today):
    """Move the book to equal weights across the published screen.

    Sells first (freeing cash), then buys. Costs are charged on both sides.
    """
    target = [r["ticker"] for r in screen[:MAX_POSITIONS]]
    priced = {t: _price(cache, t) for t in target}
    target = [t for t in target if priced.get(t)]      # never trade blind
    if not target:
        return led

    # ---- sell anything no longer in the screen -------------------------------
    for tick in list(led["positions"]):
        if tick in target:
            continue
        pos = led["positions"].pop(tick)
        px = _price(cache, tick, pos.get("last_px")) or pos["entry_px"]
        gross = pos["shares"] * px
        cost = gross * COST_BPS / 10_000
        led["cash"] += gross - cost
        led["costs_paid"] += cost
        led["trades"].append({"date": today, "ticker": tick, "side": "sell",
                              "shares": round(pos["shares"], 4),
                              "px": round(px, 4), "cost": round(cost, 2)})

    # ---- resize everything to an equal share of the book ---------------------
    equity = led["cash"] + sum(p["shares"] * (p.get("last_px") or p["entry_px"])
                               for p in led["positions"].values())
    # Reserve the friction up front. Sizing straight off equity means the last
    # name bought runs out of cash and ends up underweight — the weights must
    # be equal, so the cost comes out of the slot, not out of the tail.
    slot = (equity / (1 + COST_BPS / 10_000)) / len(target)
    for tick in target:
        px = priced[tick]
        held = led["positions"].get(tick)
        have = held["shares"] * px if held else 0.0
        delta_value = slot - have
        if abs(delta_value) < slot * 0.05:      # ignore trivial drift
            continue
        shares = delta_value / px
        gross = abs(delta_value)
        cost = gross * COST_BPS / 10_000
        if shares > 0 and led["cash"] < gross + cost:
            shares = max(0.0, (led["cash"] - cost) / px)
            gross = shares * px
            cost = gross * COST_BPS / 10_000
            if shares <= 0:
                continue
        led["cash"] -= shares * px + cost
        led["costs_paid"] += cost
        if held:
            held["shares"] += shares
            held["last_px"] = px
            if held["shares"] <= 1e-9:
                led["positions"].pop(tick, None)
        else:
            led["positions"][tick] = {"shares": shares, "entry_px": px,
                                      "entry_date": today, "last_px": px}
        led["trades"].append({"date": today, "ticker": tick,
                              "side": "buy" if shares > 0 else "sell",
                              "shares": round(abs(shares), 4),
                              "px": round(px, 4), "cost": round(cost, 2)})

    led["last_rebalance"] = today
    led["trades"] = led["trades"][-300:]
    return led


def update(cache, screen):
    """Run one simulated day. Returns the ledger."""
    led = load_ledger()
    if led.get("v") != smallcap.MODEL_VERSION:
        led = _reset(led)
    today = _today()
    bench = (cache.get("bench") or {}).get("iwo")

    # Value the book BEFORE deciding any trades. Sizing off yesterday's marks
    # while pricing today's trades at today's prices mis-sizes every position
    # after a move — the equity and the holdings must be measured on the same
    # prices for equal weighting to mean anything.
    mark_to_market(led, cache)

    if screen and _due_for_rebalance(led, today):
        if led["started"] is None:
            led["started"] = today
            led["start_bench"] = bench
        rebalance(led, cache, screen, today)

    value = mark_to_market(led, cache)
    if led["started"]:
        hist = led["history"]
        if hist and hist[-1]["date"] == today:
            hist[-1] = {"date": today, "value": round(value, 2), "bench": bench}
        else:
            hist.append({"date": today, "value": round(value, 2), "bench": bench})
        led["history"] = hist[-HISTORY_DAYS:]
    save_ledger(led)
    return led


def summarize(led=None, cache=None):
    """Numbers for the page. All hypothetical."""
    led = led or load_ledger()
    hist = led.get("history") or []
    if not hist or not led.get("started"):
        return {"status": "not started", "v": led.get("v")}
    value = hist[-1]["value"]
    ret = (value / START_CAPITAL - 1) * 100
    start_bench = led.get("start_bench") or (hist[0].get("bench"))
    bench_now = hist[-1].get("bench")
    bench_ret = ((bench_now / start_bench - 1) * 100
                 if start_bench and bench_now else None)
    peak, drawdown = START_CAPITAL, 0.0
    for h in hist:
        peak = max(peak, h["value"])
        drawdown = min(drawdown, (h["value"] / peak - 1) * 100)
    holdings = []
    for tick, p in led.get("positions", {}).items():
        px = p.get("last_px") or p["entry_px"]
        holdings.append({
            "ticker": tick, "shares": round(p["shares"], 2),
            "value": round(p["shares"] * px, 2),
            "entry_date": p.get("entry_date"),
            "ret": (px / p["entry_px"] - 1) * 100 if p.get("entry_px") else 0.0,
        })
    holdings.sort(key=lambda h: -h["value"])
    return {
        "status": "running",
        "v": led.get("v"),
        "started": led["started"],
        "days": len(hist),
        "value": round(value, 2),
        "ret": round(ret, 2),
        "bench_ret": round(bench_ret, 2) if bench_ret is not None else None,
        "excess": round(ret - bench_ret, 2) if bench_ret is not None else None,
        "max_drawdown": round(drawdown, 2),
        "cash": round(led.get("cash", 0.0), 2),
        "costs_paid": round(led.get("costs_paid", 0.0), 2),
        "positions": len(led.get("positions", {})),
        "holdings": holdings[:25],
        "trades": list(reversed(led.get("trades", [])))[:12],
        "last_rebalance": led.get("last_rebalance"),
        "assumptions": {
            "capital": START_CAPITAL,
            "cost_bps": COST_BPS,
            "cadence": "weekly (Monday)",
            "sizing": "equal weight",
        },
    }
