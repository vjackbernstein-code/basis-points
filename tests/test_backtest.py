#!/usr/bin/env python3
"""Tests for the historical replay.

A backtest is the one piece of this project that cannot be checked by looking
at its output, because every way it can be wrong makes it look BETTER. A
lookahead bug, a dropped failure, a survivor-only universe: none of them
crash, none of them produce an implausible number, and all of them produce a
flattering one. So the parts where that can happen are tested directly and
the tests say what the flattery would have been.

Offline: no database, no network. The two loaders that need a 16GB file
belonging to another project are not exercised here; what is exercised is
every place a result could be quietly improved.

Run:  python3 -m unittest discover tests
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest as B  # noqa: E402
import smallcap  # noqa: E402


class ExchangeTranslationTests(unittest.TestCase):
    """Translating the vendor's exchange codes, without touching the rule.

    The live rule was written against a feed that spells it "NEW YORK STOCK
    EXCHANGE, INC."; this vendor says "NYSE". Left untranslated the rule
    rejects it and 14,117 American companies vanish from the backtest — which
    would not look like a bug, just like a smaller universe.
    """

    def test_every_us_venue_is_accepted_after_translation(self):
        for code in ("NASDAQ", "NYSE", "NYSEMKT", "NYSEARCA", "BATS"):
            self.assertTrue(
                smallcap.is_us_listed(B.EXCHANGE_NAMES[code]),
                f"{code} should read as US-listed once translated")

    def test_the_raw_codes_would_mostly_have_been_rejected(self):
        # the reason the table exists, pinned so it cannot be deleted as
        # redundant later
        self.assertFalse(smallcap.is_us_listed("NYSE"))
        self.assertFalse(smallcap.is_us_listed("NYSEMKT"))

    def test_non_us_venues_stay_rejected(self):
        for code in ("OTC", "TXSE"):
            self.assertFalse(
                smallcap.is_us_listed(B.EXCHANGE_NAMES[code]),
                f"{code} must not become eligible through translation")

    def test_translation_does_not_widen_the_rule(self):
        # the rule itself is frozen; the fix belongs on the input side
        self.assertEqual(
            smallcap.US_EXCHANGES,
            ("NASDAQ", "NEW YORK STOCK EXCHANGE", "NYSE MKT", "NYSE ARCA",
             "BATS EXCHANGE", "CBOE"))


class SectorTranslationTests(unittest.TestCase):

    def test_basic_materials_reaches_a_peer_group(self):
        # growth is ranked WITHIN a group, so 2,483 companies falling into
        # "Other" are not mislabelled, they are compared against the wrong
        # companies
        raw = "Basic Materials"
        self.assertEqual(smallcap.industry_group(raw), "Other")
        self.assertEqual(
            smallcap.industry_group(B.SECTOR_NAMES[raw]), "Materials")

    def test_a_blank_sector_is_left_unmapped(self):
        # the rule treats a blank industry as ineligible, which is how shells
        # and closed-end funds are excluded; translating it would smuggle them
        # back in
        self.assertNotIn(None, B.SECTOR_NAMES)
        self.assertNotIn("", B.SECTOR_NAMES)


class SurvivorshipTests(unittest.TestCase):
    """Where survivorship bias comes back after being carefully excluded.

    The universe can be perfectly survivorship-free and the measurement can
    still throw the failures away: a company that stops trading has no end
    price, and leaving it out of an average silently keeps the survivors and
    drops the disasters. That is the single most flattering bug available
    here, so it is tested from both sides.
    """

    def _prices(self, spec):
        return {tk: [(d, px, px, 1_000_000) for d, px in rows]
                for tk, rows in spec.items()}

    def test_a_normal_pair_of_prices_gives_the_return(self):
        px = self._prices({"AAA": [("2020-01-06", 10.0), ("2020-01-13", 11.0)]})
        r, dropped, dl = B._basket_return(px, {}, ["AAA"],
                                          "2020-01-06", "2020-01-13")
        self.assertAlmostEqual(r, 10.0, places=6)
        self.assertEqual((dropped, dl), (0, 0))

    def test_a_company_that_was_delisted_is_booked_as_a_loss(self):
        # it had a start price and then stopped existing. Dropping it is the
        # flattery; booking it at the same assumed loss the live system uses
        # keeps the two measurements comparable.
        px = self._prices({"AAA": [("2020-01-06", 10.0), ("2020-01-13", 11.0)],
                           "DEAD": [("2020-01-06", 10.0)]})
        spine = {"DEAD": {"delisted": True, "last_price": "2020-01-08"}}
        r, dropped, dl = B._basket_return(px, spine, ["AAA", "DEAD"],
                                          "2020-01-06", "2020-01-13")
        self.assertEqual(dl, 1)
        self.assertEqual(dropped, 0)
        expected = (10.0 + -smallcap.DELIST_ASSUMED_LOSS * 100) / 2
        self.assertAlmostEqual(r, expected, places=6)

    def test_the_delisting_loss_is_the_live_systems_number(self):
        # if these two ever diverge, a backtest reading and a live reading
        # stop meaning the same thing
        self.assertEqual(smallcap.DELIST_ASSUMED_LOSS, 0.60)

    def test_dropping_a_failure_would_have_flattered_the_result(self):
        # states the size of the bug this guards against, so nobody later
        # "simplifies" it away
        px = self._prices({"AAA": [("2020-01-06", 10.0), ("2020-01-13", 11.0)],
                           "DEAD": [("2020-01-06", 10.0)]})
        spine = {"DEAD": {"delisted": True, "last_price": "2020-01-08"}}
        honest, _d, _l = B._basket_return(px, spine, ["AAA", "DEAD"],
                                          "2020-01-06", "2020-01-13")
        survivors_only, _d, _l = B._basket_return(px, spine, ["AAA"],
                                                  "2020-01-06", "2020-01-13")
        self.assertGreater(survivors_only - honest, 30.0)

    def test_a_name_merely_missing_a_print_is_counted_not_absorbed(self):
        # still listed, no price: unknown rather than a 60% loss, but the
        # count is reported so a quiet pile-up is visible
        px = self._prices({"AAA": [("2020-01-06", 10.0), ("2020-01-13", 11.0)],
                           "QUIET": [("2020-01-06", 10.0)]})
        spine = {"QUIET": {"delisted": False, "last_price": None}}
        r, dropped, dl = B._basket_return(px, spine, ["AAA", "QUIET"],
                                          "2020-01-06", "2020-01-13")
        self.assertEqual((dropped, dl), (1, 0))
        self.assertAlmostEqual(r, 10.0, places=6)

    def test_a_basket_with_no_usable_names_returns_nothing(self):
        r, _d, _l = B._basket_return({}, {}, ["AAA"], "2020-01-06",
                                     "2020-01-13")
        self.assertIsNone(r, "an empty basket must not read as 0%")

    def test_a_stale_start_price_is_not_reused_as_the_end_price(self):
        # one print, used for both ends, would read as exactly 0% — a
        # fabricated flat return rather than a missing one
        px = self._prices({"AAA": [("2020-01-06", 10.0)]})
        spine = {"AAA": {"delisted": False, "last_price": None}}
        r, dropped, _l = B._basket_return(px, spine, ["AAA"],
                                          "2020-01-06", "2020-01-13")
        self.assertIsNone(r)
        self.assertEqual(dropped, 1)


class PointInTimeTests(unittest.TestCase):
    """Growth computed only from figures that had been filed."""

    def _rows(self, spec):
        return [{"reportperiod": rp, "date": filed, "revenue": rev}
                for rp, filed, rev in spec]

    def test_one_year_growth_is_a_plain_percentage(self):
        rows = self._rows([("2023-06-30", "2023-08-10", 100.0),
                           ("2024-06-30", "2024-08-10", 150.0)])
        self.assertAlmostEqual(B._growth(rows, 1), 50.0, places=6)

    def test_three_year_growth_is_annualised(self):
        # doubling over three years is ~26% a year, not 100%
        rows = self._rows([("2021-06-30", "2021-08-10", 100.0),
                           ("2024-06-30", "2024-08-10", 200.0)])
        self.assertAlmostEqual(B._growth(rows, 3), 25.992, places=2)

    def test_no_comparable_period_reports_nothing(self):
        # a gap of years compared as if it were one year would invent growth
        rows = self._rows([("2015-06-30", "2015-08-10", 100.0),
                           ("2024-06-30", "2024-08-10", 150.0)])
        self.assertIsNone(B._growth(rows, 1))

    def test_a_single_filing_is_not_enough(self):
        self.assertIsNone(B._growth(
            self._rows([("2024-06-30", "2024-08-10", 150.0)]), 1))

    def test_zero_or_negative_revenue_reports_nothing(self):
        # dividing by a near-zero base is how +13,520% figures appear; zero
        # itself must not produce an infinity
        for base in (0.0, -5.0, None):
            rows = self._rows([("2023-06-30", "2023-08-10", base),
                               ("2024-06-30", "2024-08-10", 150.0)])
            self.assertIsNone(B._growth(rows, 1), repr(base))

    def test_the_latest_filed_row_is_the_one_used(self):
        rows = self._rows([("2023-06-30", "2023-08-10", 100.0),
                           ("2024-06-30", "2024-08-10", 150.0),
                           ("2024-09-30", "2024-11-10", 300.0)])
        # latest period is 2024-09-30; a year before that is 2023-09-30, and
        # the closest available period is 2023-06-30 at 100
        self.assertAlmostEqual(B._growth(rows, 1), 200.0, places=6)

    def test_only_rows_filed_by_the_as_of_date_are_visible(self):
        rows = self._rows([("2023-06-30", "2023-08-10", 100.0),
                           ("2024-06-30", "2024-08-10", 150.0)])
        visible = B._at_or_before(rows, "2024-07-01")
        self.assertEqual(len(visible), 1,
                         "a figure filed in August was used in July")
        self.assertIsNone(B._growth(visible, 1))


class ClockTests(unittest.TestCase):

    def test_the_clock_is_restored_even_if_scoring_raises(self):
        # leaving the clock pinned would silently date every later run
        real = smallcap._now
        with self.assertRaises(KeyError):
            B.screen_on({"profiles": {}}, "2018-06-15")   # missing keys
        self.assertIs(smallcap._now, real)

    def test_scoring_sees_the_historical_date(self):
        seen = {}

        def fake(cache, prev_c=None, prev_p=None):
            seen["today"] = smallcap._now().strftime("%Y-%m-%d")
            return [], []

        real = smallcap.compute_screen
        try:
            smallcap.compute_screen = fake
            B.screen_on({}, "2011-03-07")
        finally:
            smallcap.compute_screen = real
        self.assertEqual(seen["today"], "2011-03-07")


class IndependenceTests(unittest.TestCase):
    """Overlapping readings are not independent evidence."""

    def test_overlapping_four_week_readings_collapse(self):
        # measured weekly, a four-week return counts the same month four
        # times, and averaging those claims four times the evidence it has
        weekly = [(f"2020-01-{d:02d}", 1.0) for d in (6, 13, 20, 27)]
        self.assertEqual(len(B.independent(weekly, 28)), 1)

    def test_readings_a_full_horizon_apart_all_count(self):
        monthly = [("2020-01-06", 1.0), ("2020-02-03", 1.0),
                   ("2020-03-02", 1.0)]
        self.assertEqual(len(B.independent(monthly, 28)), 3)

    def test_one_week_readings_a_week_apart_all_count(self):
        weekly = [(f"2020-01-{d:02d}", 1.0) for d in (6, 13, 20, 27)]
        self.assertEqual(len(B.independent(weekly, 7)), 4)

    def test_order_does_not_matter(self):
        shuffled = [("2020-03-02", 1.0), ("2020-01-06", 1.0),
                    ("2020-02-03", 1.0)]
        self.assertEqual(len(B.independent(shuffled, 28)), 3)


class ScheduleTests(unittest.TestCase):

    def test_dates_are_mondays(self):
        from datetime import date
        for d in B.mondays("2020-01-01", "2020-03-01"):
            self.assertEqual(date.fromisoformat(d).weekday(), 0, d)

    def test_the_range_is_inclusive_of_its_end(self):
        self.assertIn("2020-01-06", B.mondays("2020-01-01", "2020-01-06"))

    def test_the_benchmark_floor_is_respected_by_the_runner(self):
        # IWO's history starts in July 2000; a reading before that would have
        # nothing to be measured against
        self.assertGreaterEqual(B.EARLIEST, "2000-07-28")


class HonestyTests(unittest.TestCase):
    """The limitations have to travel with the numbers."""

    def test_the_caveats_name_the_things_that_matter(self):
        text = " ".join(B.CAVEATS).lower()
        for must in ("vendor", "cost", "frozen", "forward"):
            self.assertIn(must, text, f"the caveats never mention {must}")

    def test_it_refuses_to_claim_authority_over_real_money(self):
        text = " ".join(B.CAVEATS).lower()
        self.assertIn("cannot authorise real money", text)

    def test_the_benchmark_is_the_one_the_live_record_uses(self):
        # a backtest measured against a different index than the live record
        # cannot be compared with it, which is the only thing it is for
        self.assertEqual(B.BENCHMARK, "IWO")

    def test_the_horizons_match_the_live_records_horizons(self):
        live = {h: d for h, _lo, _hi, d in smallcap.HORIZONS}
        self.assertEqual(dict(B.HORIZONS), live)


if __name__ == "__main__":
    unittest.main()
