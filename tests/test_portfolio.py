#!/usr/bin/env python3
"""Regression tests for portfolio.py — the paper (simulated) portfolio.

Offline and deterministic: the clock is pinned, the ledger is written to a
temp file, and prices are supplied directly. No network, no real money, and
no dependence on the committed data files.

Run:  python3 -m unittest discover tests
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portfolio  # noqa: E402
import smallcap  # noqa: E402


NOW = datetime(2026, 9, 21, 15, 0, 0, tzinfo=timezone.utc)   # a Monday


def iso(dt=None):
    return (dt or NOW).isoformat(timespec="seconds")


def cache_with(prices, bench=100.0, quote_age_h=1.0):
    # timestamps must follow the (pinned, advanceable) clock, not a constant —
    # otherwise every quote looks stale as soon as a test moves time forward
    now = smallcap._now()
    t = iso(now - timedelta(hours=quote_age_h))
    return {"quotes": {k: {"px": v, "dp": 0.0, "t": t} for k, v in prices.items()},
            "bench": {"iwo": bench, "iwm": 90.0, "t": iso(now)}}


def entry_cost(capital=None):
    """Friction charged to put `capital` fully to work: the cost is taken out
    of the money invested, so it is slightly under capital x bps."""
    cap = portfolio.START_CAPITAL if capital is None else capital
    invested = cap / (1 + portfolio.COST_BPS / 10_000)
    return invested * portfolio.COST_BPS / 10_000


def screen_of(tickers):
    return [{"ticker": t} for t in tickers]


class PaperPortfolioTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        portfolio.LEDGER_PATH = Path(self._tmp.name) / "portfolio.json"
        self._now = NOW
        real_now = smallcap._now
        smallcap._now = lambda: self._now
        self.addCleanup(lambda: setattr(smallcap, "_now", real_now))

    def advance(self, days):
        self._now = self._now + timedelta(days=days)


class InceptionTests(PaperPortfolioTestCase):

    def test_first_rebalance_buys_an_equal_weight_of_every_screened_name(self):
        names = [f"T{i}" for i in range(5)]
        led = portfolio.update(cache_with({n: 10.0 for n in names}), screen_of(names))
        self.assertEqual(len(led["positions"]), 5)
        values = [p["shares"] * p["last_px"] for p in led["positions"].values()]
        for v in values:
            self.assertAlmostEqual(v, values[0], places=6)

    def test_opening_the_book_costs_exactly_the_stated_friction(self):
        names = [f"T{i}" for i in range(4)]
        led = portfolio.update(cache_with({n: 10.0 for n in names}), screen_of(names))
        self.assertAlmostEqual(led["costs_paid"], entry_cost(), places=2)
        # a portfolio starts DOWN by its entry cost, before the market moves
        self.assertAlmostEqual(portfolio.mark_to_market(led, cache_with(
            {n: 10.0 for n in names})),
            portfolio.START_CAPITAL - entry_cost(), places=2)

    def test_a_name_without_a_usable_price_is_never_traded(self):
        cache = cache_with({"GOOD": 10.0})
        cache["quotes"]["STALE"] = {"px": 10.0, "dp": 0.0,
                                    "t": iso(NOW - timedelta(hours=200))}
        led = portfolio.update(cache, screen_of(["GOOD", "STALE", "ABSENT"]))
        self.assertEqual(list(led["positions"]), ["GOOD"])

    def test_nothing_happens_at_all_without_a_screen(self):
        led = portfolio.update(cache_with({"AAA": 10.0}), [])
        self.assertEqual(led["positions"], {})
        self.assertIsNone(led["started"])
        self.assertEqual(portfolio.summarize(led)["status"], "not started")


class RebalanceTests(PaperPortfolioTestCase):

    def _open(self, names, px=10.0):
        return portfolio.update(cache_with({n: px for n in names}), screen_of(names))

    def test_the_book_is_left_alone_between_weekly_rebalances(self):
        self._open(["A", "B"])
        self.advance(2)                       # Wednesday
        led = portfolio.update(cache_with({"A": 10.0, "B": 10.0, "C": 10.0}),
                               screen_of(["A", "C"]))
        self.assertEqual(sorted(led["positions"]), ["A", "B"])  # C not bought yet

    def test_a_week_later_the_book_moves_to_the_new_screen(self):
        self._open(["A", "B"])
        self.advance(7)
        led = portfolio.update(cache_with({"A": 10.0, "B": 10.0, "C": 10.0}),
                               screen_of(["A", "C"]))
        self.assertEqual(sorted(led["positions"]), ["A", "C"])

    def test_selling_a_dropped_name_also_pays_the_friction(self):
        self._open(["A", "B"])
        before = portfolio.load_ledger()["costs_paid"]
        self.advance(7)
        led = portfolio.update(cache_with({"A": 10.0, "B": 10.0}), screen_of(["A"]))
        self.assertGreater(led["costs_paid"], before)
        self.assertIn("sell", [t["side"] for t in led["trades"]])

    def test_a_gain_is_carried_into_the_next_rebalance(self):
        self._open(["A", "B"])
        self.advance(7)
        led = portfolio.update(cache_with({"A": 20.0, "B": 20.0}), screen_of(["A", "B"]))
        self.assertGreater(portfolio.mark_to_market(led, cache_with({"A": 20.0, "B": 20.0})),
                           portfolio.START_CAPITAL * 1.9)


class LedgerIntegrityTests(PaperPortfolioTestCase):

    def test_a_model_version_change_restarts_the_simulation(self):
        portfolio.update(cache_with({"A": 10.0}), screen_of(["A"]))
        real_version = smallcap.MODEL_VERSION
        smallcap.MODEL_VERSION = "v9-different"
        try:
            led = portfolio.update(cache_with({"A": 10.0}), screen_of(["A"]))
        finally:
            smallcap.MODEL_VERSION = real_version
        self.assertEqual(led["restarted_from"], real_version)
        self.assertAlmostEqual(led["costs_paid"], entry_cost(), places=2)

    def test_a_held_name_keeps_its_last_mark_when_its_quote_goes_stale(self):
        portfolio.update(cache_with({"A": 10.0}), screen_of(["A"]))
        self.advance(1)
        led = portfolio.load_ledger()
        value = portfolio.mark_to_market(led, {"quotes": {}, "bench": {}})
        self.assertAlmostEqual(value, portfolio.START_CAPITAL - entry_cost(),
                               places=2)

    def test_the_simulation_never_spends_money_it_does_not_have(self):
        names = [f"T{i}" for i in range(25)]
        led = portfolio.update(cache_with({n: 10.0 for n in names}), screen_of(names))
        self.assertGreaterEqual(led["cash"], -1e-6)


class ReportingTests(PaperPortfolioTestCase):

    def test_it_reports_return_benchmark_and_the_difference_between_them(self):
        portfolio.update(cache_with({"A": 10.0}, bench=100.0), screen_of(["A"]))
        self.advance(7)
        led = portfolio.update(cache_with({"A": 11.0}, bench=105.0), screen_of(["A"]))
        s = portfolio.summarize(led)
        self.assertEqual(s["status"], "running")
        self.assertGreater(s["ret"], 9.0)          # ~+10% less frictions
        self.assertAlmostEqual(s["bench_ret"], 5.0, places=6)
        self.assertAlmostEqual(s["excess"], s["ret"] - s["bench_ret"], places=6)

    def test_the_worst_dip_is_measured_from_the_peak(self):
        portfolio.update(cache_with({"A": 10.0}), screen_of(["A"]))
        self.advance(7)
        portfolio.update(cache_with({"A": 20.0}), screen_of(["A"]))   # peak
        self.advance(1)
        led = portfolio.update(cache_with({"A": 15.0}), screen_of(["A"]))
        self.assertLess(portfolio.summarize(led)["max_drawdown"], -20.0)

    def test_the_assumptions_are_always_published_with_the_numbers(self):
        portfolio.update(cache_with({"A": 10.0}), screen_of(["A"]))
        a = portfolio.summarize()["assumptions"]
        self.assertEqual(a["cost_bps"], portfolio.COST_BPS)
        self.assertEqual(a["capital"], portfolio.START_CAPITAL)
        self.assertIn("weekly", a["cadence"])


if __name__ == "__main__":
    unittest.main()
