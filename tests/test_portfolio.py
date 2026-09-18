#!/usr/bin/env python3
"""Regression tests for portfolio.py — the paper (simulated) portfolios.

Offline and deterministic: the clock is pinned, the ledger goes to a temp file,
prices are supplied directly. No network, no real money, no dependence on the
committed data files.

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


def iso(dt):
    return dt.isoformat(timespec="seconds")


def cache_with(prices, bench=100.0, regime="uptrend", quote_age_h=1.0):
    # timestamps follow the pinned, advanceable clock — a constant here would
    # make every quote look stale as soon as a test moved time forward
    now = smallcap._now()
    t = iso(now - timedelta(hours=quote_age_h))
    return {"quotes": {k: {"px": v, "dp": 0.0, "t": t} for k, v in prices.items()},
            "bench": {"iwo": bench, "iwm": 90.0, "t": iso(now)},
            "regime": {"label": regime, "t": iso(now)}}


def screen_of(tickers, scores=None):
    scores = scores or {}
    return [{"ticker": t, "score": scores.get(t, 70.0)} for t in tickers]


def entry_cost(capital=None):
    """Friction to put `capital` fully to work; taken out of the money invested."""
    cap = portfolio.START_CAPITAL if capital is None else capital
    return (cap / (1 + portfolio.COST_BPS / 10_000)) * portfolio.COST_BPS / 10_000


class PaperTestCase(unittest.TestCase):
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

    def book(self, led, key="A"):
        return led["books"][key]


class SharedRuleTests(PaperTestCase):
    """Rules every book obeys, whatever its strategy."""

    def test_every_strategy_gets_its_own_independent_book(self):
        led = portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        self.assertEqual(sorted(led["books"]), sorted(portfolio.STRATEGIES))
        for key in portfolio.STRATEGIES:
            self.assertEqual(len(led["books"][key]["positions"]), 1)

    def test_the_baseline_book_is_equally_weighted(self):
        names = [f"T{i}" for i in range(5)]
        led = portfolio.update(cache_with({n: 10.0 for n in names}), screen_of(names))
        vals = [p["shares"] * p["last_px"] for p in self.book(led)["positions"].values()]
        for v in vals:
            self.assertAlmostEqual(v, vals[0], places=6)

    def test_opening_a_book_costs_exactly_the_stated_friction(self):
        led = portfolio.update(cache_with({"A1": 10.0, "B1": 10.0}),
                               screen_of(["A1", "B1"]))
        self.assertAlmostEqual(self.book(led)["costs_paid"], entry_cost(), places=2)
        self.assertAlmostEqual(portfolio.book_value(self.book(led)),
                               portfolio.START_CAPITAL - entry_cost(), places=2)

    def test_a_name_without_a_usable_price_is_never_traded(self):
        cache = cache_with({"GOOD": 10.0})
        cache["quotes"]["STALE"] = {"px": 10.0, "dp": 0.0,
                                    "t": iso(NOW - timedelta(hours=200))}
        led = portfolio.update(cache, screen_of(["GOOD", "STALE", "ABSENT"]))
        self.assertEqual(list(self.book(led)["positions"]), ["GOOD"])

    def test_books_are_left_alone_between_weekly_rebalances(self):
        portfolio.update(cache_with({"A1": 10.0, "B1": 10.0}), screen_of(["A1", "B1"]))
        self.advance(2)
        led = portfolio.update(cache_with({"A1": 10.0, "B1": 10.0, "C1": 10.0}),
                               screen_of(["A1", "C1"]))
        self.assertEqual(sorted(self.book(led)["positions"]), ["A1", "B1"])

    def test_a_week_later_the_book_moves_to_the_new_screen(self):
        portfolio.update(cache_with({"A1": 10.0, "B1": 10.0}), screen_of(["A1", "B1"]))
        self.advance(7)
        led = portfolio.update(cache_with({"A1": 10.0, "B1": 10.0, "C1": 10.0}),
                               screen_of(["A1", "C1"]))
        self.assertEqual(sorted(self.book(led)["positions"]), ["A1", "C1"])

    def test_no_book_ever_spends_money_it_does_not_have(self):
        names = [f"T{i}" for i in range(25)]
        led = portfolio.update(cache_with({n: 10.0 for n in names}), screen_of(names))
        for key in portfolio.STRATEGIES:
            self.assertGreaterEqual(led["books"][key]["cash"], -1e-6, key)

    def test_a_model_version_change_restarts_every_book(self):
        portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        real = smallcap.MODEL_VERSION
        smallcap.MODEL_VERSION = "v9-different"
        try:
            led = portfolio.load_ledger()
        finally:
            smallcap.MODEL_VERSION = real
        self.assertEqual(led["restarted_from"], real)
        self.assertEqual(led["books"]["A"]["positions"], {})


class ConvictionSizingTests(PaperTestCase):

    def test_a_higher_score_earns_a_bigger_position(self):
        scores = {"HI": 90.0, "MID": 70.0, "LO": 50.0}
        led = portfolio.update(cache_with({t: 10.0 for t in scores}),
                               screen_of(list(scores), scores))
        pos = led["books"]["B"]["positions"]
        val = {t: pos[t]["shares"] * pos[t]["last_px"] for t in scores}
        self.assertGreater(val["HI"], val["MID"])
        self.assertGreater(val["MID"], val["LO"])

    def test_the_tilt_is_capped_so_noise_cannot_dominate(self):
        # one wild score must not take over the book
        scores = {"WILD": 100.0, **{f"T{i}": 1.0 for i in range(9)}}
        w = portfolio.target_weights(screen_of(list(scores), scores),
                                     portfolio.STRATEGIES["B"])
        equal = 1.0 / len(scores)
        self.assertLessEqual(w["WILD"], equal * portfolio.CONVICTION_MAX + 1e-9)
        self.assertGreaterEqual(w["T0"], equal * portfolio.CONVICTION_MIN - 1e-9)
        self.assertAlmostEqual(sum(w.values()), 1.0, places=9)

    def test_the_baseline_book_ignores_scores_entirely(self):
        scores = {"HI": 95.0, "LO": 45.0}
        w = portfolio.target_weights(screen_of(list(scores), scores),
                                     portfolio.STRATEGIES["A"])
        self.assertAlmostEqual(w["HI"], w["LO"], places=9)


class StopLossTests(PaperTestCase):

    def _open(self, px=10.0):
        return portfolio.update(cache_with({"A1": px, "B1": px}),
                                screen_of(["A1", "B1"]))

    def test_a_holding_through_its_stop_is_sold_from_the_risk_managed_book(self):
        self._open()
        self.advance(1)
        led = portfolio.update(cache_with({"A1": 7.0, "B1": 10.0}),
                               screen_of(["A1", "B1"]))
        self.assertNotIn("A1", led["books"]["C"]["positions"])
        self.assertEqual(led["books"]["C"]["stops_hit"], 1)

    def test_the_baseline_book_holds_the_same_falling_name(self):
        self._open()
        self.advance(1)
        led = portfolio.update(cache_with({"A1": 7.0, "B1": 10.0}),
                               screen_of(["A1", "B1"]))
        self.assertIn("A1", led["books"]["A"]["positions"])
        self.assertEqual(led["books"]["A"]["stops_hit"], 0)

    def test_stops_are_checked_every_day_not_only_on_rebalance_days(self):
        self._open()
        self.advance(3)                      # a Thursday, no rebalance due
        led = portfolio.update(cache_with({"A1": 5.0, "B1": 10.0}),
                               screen_of(["A1", "B1"]))
        self.assertEqual(led["books"]["C"]["stops_hit"], 1)

    def test_a_stop_exit_is_charged_extra_for_gapping_through(self):
        self._open()
        self.advance(1)
        led = portfolio.update(cache_with({"A1": 7.0, "B1": 10.0}),
                               screen_of(["A1", "B1"]))
        stop_trade = [t for t in led["books"]["C"]["trades"] if t["why"] == "stop"][0]
        ordinary = stop_trade["shares"] * stop_trade["px"] * portfolio.COST_BPS / 10_000
        self.assertGreater(stop_trade["cost"], ordinary * 1.5)

    def test_a_shallow_dip_does_not_trigger_the_stop(self):
        self._open()
        self.advance(1)
        led = portfolio.update(cache_with({"A1": 9.5, "B1": 10.0}),
                               screen_of(["A1", "B1"]))
        self.assertIn("A1", led["books"]["C"]["positions"])


class RegimeOverlayTests(PaperTestCase):

    def test_a_falling_tape_leaves_the_regime_book_partly_in_cash(self):
        led = portfolio.update(cache_with({"A1": 10.0}, regime="correction"),
                               screen_of(["A1"]))
        d = led["books"]["D"]
        invested = sum(p["shares"] * p["last_px"] for p in d["positions"].values())
        target = portfolio.REGIME_EXPOSURE["correction"]
        self.assertAlmostEqual(invested / portfolio.START_CAPITAL, target, places=2)
        self.assertGreater(d["cash"], portfolio.START_CAPITAL * (1 - target) * 0.9)

    def test_the_regime_response_never_goes_to_zero(self):
        # a filter on one index gives ~2 independent signals a year; a binary
        # switch would need to be right ~74% of the time just to break even
        for exposure in portfolio.REGIME_EXPOSURE.values():
            self.assertGreaterEqual(exposure, 0.5)

    def test_the_baseline_book_stays_fully_invested_in_the_same_tape(self):
        led = portfolio.update(cache_with({"A1": 10.0}, regime="correction"),
                               screen_of(["A1"]))
        self.assertLess(self.book(led)["cash"], portfolio.START_CAPITAL * 0.01)

    def test_an_unknown_regime_label_means_full_exposure(self):
        self.assertEqual(
            portfolio.exposure_for(portfolio.STRATEGIES["D"],
                                   {"regime": {"label": "something new"}}), 1.0)


class ReportingTests(PaperTestCase):

    def test_every_book_is_reported_with_its_rules(self):
        portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        s = portfolio.summarize()
        self.assertEqual(s["status"], "running")
        self.assertEqual({b["key"] for b in s["books"]}, set(portfolio.STRATEGIES))
        for b in s["books"]:
            self.assertTrue(b["note"])

    def test_return_benchmark_and_the_difference_are_reported(self):
        portfolio.update(cache_with({"A1": 10.0}, bench=100.0), screen_of(["A1"]))
        self.advance(7)
        portfolio.update(cache_with({"A1": 11.0}, bench=105.0), screen_of(["A1"]))
        a = [b for b in portfolio.summarize()["books"] if b["key"] == "A"][0]
        self.assertGreater(a["ret"], 9.0)
        self.assertAlmostEqual(a["bench_ret"], 5.0, places=6)
        # GEOMETRIC, not arithmetic: subtracting cumulative percentages
        # overstates whenever the benchmark is up
        expected = ((1 + a["ret"] / 100) / (1 + a["bench_ret"] / 100) - 1) * 100
        self.assertAlmostEqual(a["excess"], round(expected, 2), places=2)
        self.assertLess(a["excess"], a["ret"] - a["bench_ret"])


class AuditedHonestyTests(PaperTestCase):
    """Each of these pins a way the simulation was found to flatter itself."""

    def test_the_worst_dip_is_remembered_after_the_book_recovers(self):
        portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        self.advance(7)
        portfolio.update(cache_with({"A1": 5.0}), screen_of(["A1"]))    # -50%
        self.advance(7)
        portfolio.update(cache_with({"A1": 40.0}), screen_of(["A1"]))   # recovers
        a = [b for b in portfolio.summarize()["books"] if b["key"] == "A"][0]
        self.assertLess(a["max_drawdown"], -40.0)   # the fall is still on record
        self.assertGreater(a["ret"], 0.0)

    def test_a_version_change_retires_the_books_instead_of_erasing_them(self):
        portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        self.advance(7)
        portfolio.update(cache_with({"A1": 4.0}), screen_of(["A1"]))    # a bad run
        real = smallcap.MODEL_VERSION
        smallcap.MODEL_VERSION = "v9-next"
        try:
            led = portfolio.load_ledger()
        finally:
            smallcap.MODEL_VERSION = real
        retired = led["retired"]
        self.assertTrue(retired)
        self.assertTrue(any(r["ret"] < -30 for r in retired))
        self.assertTrue(all(r["v"] == real for r in retired))

    def test_a_stale_benchmark_suspends_the_excess_figure(self):
        portfolio.update(cache_with({"A1": 10.0}, bench=100.0), screen_of(["A1"]))
        self.advance(7)
        stale = cache_with({"A1": 14.0}, bench=100.0)
        stale["bench"]["t"] = iso(smallcap._now() - timedelta(hours=400))
        portfolio.update(stale, screen_of(["A1"]))
        a = [b for b in portfolio.summarize()["books"] if b["key"] == "A"][0]
        self.assertGreater(a["ret"], 0.0)          # the flattering number stays
        self.assertIsNone(a["excess"])             # the accountable one is withheld

    def test_a_holding_that_stops_being_quoted_is_written_down(self):
        portfolio.update(cache_with({"A1": 10.0, "B1": 10.0}), screen_of(["A1", "B1"]))
        before = portfolio.book_value(portfolio.load_ledger()["books"]["A"])
        self.advance(4)                            # A1 goes dark
        portfolio.update(cache_with({"B1": 10.0}), screen_of(["A1", "B1"]))
        after = portfolio.book_value(portfolio.load_ledger()["books"]["A"])
        self.assertLess(after, before * 0.9)

    def test_a_long_dark_holding_is_written_off_entirely(self):
        portfolio.update(cache_with({"A1": 10.0, "B1": 10.0}), screen_of(["A1", "B1"]))
        self.advance(31)
        portfolio.update(cache_with({"B1": 10.0}), screen_of(["B1"]))
        led = portfolio.load_ledger()
        self.assertNotIn("A1", led["books"]["A"]["positions"])   # sold at ~zero

    def test_nothing_is_traded_while_the_market_is_closed(self):
        self._now = NOW.replace(hour=2)            # Monday, 2am UTC
        led = portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        self.assertEqual(led["books"]["A"]["positions"], {})
        self._now = NOW                            # Monday, market open
        led = portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        self.assertIn("A1", led["books"]["A"]["positions"])

    def test_annualised_friction_is_published_beside_the_return(self):
        portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        a = [b for b in portfolio.summarize()["books"] if b["key"] == "A"][0]
        self.assertIn("friction_yr", a)
        self.assertGreater(a["friction_yr"], 0.0)

    def test_the_worst_dip_is_measured_from_the_peak(self):
        portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        self.advance(7)
        portfolio.update(cache_with({"A1": 20.0}), screen_of(["A1"]))
        self.advance(1)
        portfolio.update(cache_with({"A1": 15.0}), screen_of(["A1"]))
        a = [b for b in portfolio.summarize()["books"] if b["key"] == "A"][0]
        self.assertLess(a["max_drawdown"], -20.0)

    def test_the_assumptions_are_always_published_with_the_numbers(self):
        portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        a = portfolio.summarize()["assumptions"]
        self.assertEqual(a["cost_bps"], portfolio.COST_BPS)
        self.assertEqual(a["stop_slippage_bps"], portfolio.STOP_SLIPPAGE_BPS)
        self.assertIn("weekly", a["cadence"])

    def test_nothing_is_reported_before_the_first_screen_exists(self):
        portfolio.update(cache_with({"A1": 10.0}), [])
        self.assertEqual(portfolio.summarize()["status"], "not started")


class ProgressTrackingTests(PaperTestCase):
    """The ledger has to publish enough for a reader to see where the
    experiment has got to — not just where it currently stands."""

    def test_each_book_publishes_its_path_not_only_its_endpoint(self):
        for px in (10.0, 11.0, 12.0):
            portfolio.update(cache_with({"A1": px}), screen_of(["A1"]))
            self.advance(1)
        a = [b for b in portfolio.summarize()["books"] if b["key"] == "A"][0]
        self.assertGreaterEqual(len(a["curve"]), 3)
        self.assertAlmostEqual(a["curve"][0], 100.0, delta=1.0)   # rebased to 100

    def test_the_benchmark_path_is_published_on_the_same_scale(self):
        portfolio.update(cache_with({"A1": 10.0}, bench=100.0), screen_of(["A1"]))
        self.advance(1)
        portfolio.update(cache_with({"A1": 10.0}, bench=110.0), screen_of(["A1"]))
        bc = portfolio.summarize()["bench_curve"]
        self.assertAlmostEqual(bc[0], 100.0, delta=0.01)
        self.assertAlmostEqual(bc[-1], 110.0, delta=0.01)

    def test_a_missing_benchmark_mark_is_skipped_not_carried_forward(self):
        # carrying the last value across a gap would draw the benchmark as
        # having held still, which is a claim the data does not support
        self.assertEqual(portfolio._curve([100.0, None, 120.0], 100.0),
                         [100.0, 120.0])

    def test_a_long_history_is_thinned_but_keeps_its_newest_mark(self):
        vals = [100.0 + i for i in range(400)]
        c = portfolio._curve(vals, 100.0, cap=50)
        self.assertEqual(len(c), 50)
        self.assertAlmostEqual(c[-1], 499.0, delta=0.01)

    def test_a_retired_book_stays_visible_after_a_version_change(self):
        portfolio.update(cache_with({"A1": 10.0}), screen_of(["A1"]))
        self.advance(1)
        portfolio.update(cache_with({"A1": 12.0}), screen_of(["A1"]))
        real = smallcap.MODEL_VERSION
        smallcap.MODEL_VERSION = "v9.9"
        try:
            portfolio.update(cache_with({"A1": 12.0}), screen_of(["A1"]))
            retired = portfolio.summarize()["retired"]
        finally:
            smallcap.MODEL_VERSION = real
        self.assertTrue(retired, "a restart must not erase the run it replaced")
        self.assertTrue(any(r["key"] == "A" for r in retired))
        self.assertIsNotNone(retired[0]["ret"])


if __name__ == "__main__":
    unittest.main()
