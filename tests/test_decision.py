#!/usr/bin/env python3
"""Tests for the pre-registered December decision rule.

The rule's whole value is that it was fixed before the answer was known. These
tests exist to stop it drifting afterwards — in particular to stop the "stop
doing this" branch becoming unreachable, which is the way such a rule usually
dies: not repealed, just quietly made impossible to trigger.

Run:  python3 -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import decision  # noqa: E402


def data(readings=(), indep4=0, books=(), detail=None):
    return {
        "smallcap": {"evaluation": ({"1w": {"indep_values": list(readings),
                                            "indep": len(readings)},
                                     "4w": {"indep": indep4}}
                                    if readings or indep4 else {})},
        "portfolio": {"books": list(books), "detail": detail or {}},
    }


def book(key, excess, bench=1.0):
    return {"key": key, "label": f"Book {key}", "excess": excess,
            "bench_ret": bench, "ret": excess + bench}


def attrib(ex_top, top_pct=1.0):
    return {"attribution": {"ex_top_pct": ex_top, "top_pct": top_pct,
                            "winners": 10, "losers": 15}}


class PreRegistrationTests(unittest.TestCase):
    """The thresholds themselves, pinned.

    Every other test here would still pass if T_MIN were quietly changed from
    2.0 to 1.0 in November — the gates would simply become easier and the rule
    would still "work". Lowering a bar after the record starts to become
    visible is the precise failure this whole file exists to prevent, so the
    numbers are asserted literally. Changing one means changing this test too,
    in the same commit, in public.
    """

    def test_the_bar_is_where_it_was_set_on_2026_09_23(self):
        self.assertEqual(decision.MIN_INDEP_1W, 12)
        self.assertEqual(decision.MIN_INDEP_4W, 3)
        self.assertEqual(decision.T_MIN, 2.0)
        self.assertEqual(decision.BREADTH_TOP_N, 3)

    def test_the_dates_are_unchanged(self):
        self.assertEqual(decision.WRITTEN_ON, "2026-09-23")
        self.assertEqual(decision.REVIEW_DATE, "2026-12-14")
        self.assertEqual(decision.SECOND_CHECKPOINT, "2027-03-15")

    def test_every_continuing_verdict_names_the_next_date(self):
        # "continue to a new checkpoint" with no date is how a deadline
        # quietly becomes never
        for key in ("insufficient", "inconclusive", "continue"):
            self.assertIn(decision.SECOND_CHECKPOINT, decision.VERDICTS[key][1],
                          f"{key} must name when the next review happens")

    def test_the_review_date_is_not_quietly_pushed_back(self):
        # "extend the test" is the most comfortable wrong answer available
        self.assertEqual(decision.REVIEW_DATE, "2026-12-14")


class GateSignalTests(unittest.TestCase):

    def test_a_short_record_is_neither_a_pass_nor_a_failure(self):
        g = decision.gate_signal(data([-5.0, -4.0, -6.0])["smallcap"]["evaluation"])
        self.assertFalse(g["enough"])
        self.assertFalse(g["passed"])
        self.assertFalse(g["failed"], "a bad start is not yet a verdict")

    def test_a_strong_consistent_edge_passes(self):
        g = decision.gate_signal(
            data([1.2] * 12, indep4=3)["smallcap"]["evaluation"])
        self.assertTrue(g["enough"])
        self.assertTrue(g["passed"])

    def test_a_good_average_with_wild_spread_does_not_pass(self):
        # same mean as a steady +1.2%, but it could be luck
        xs = [14.4, -12.0, 11.0, -9.0, 8.0, -6.0, 9.0, -7.0, 6.0, -5.0, 4.0, 1.0]
        g = decision.gate_signal(data(xs, indep4=3)["smallcap"]["evaluation"])
        self.assertGreater(g["mean"], 0)
        self.assertLess(g["t"], decision.T_MIN)
        self.assertFalse(g["passed"], "a mean without consistency is not evidence")

    def test_the_four_week_readings_are_also_required(self):
        g = decision.gate_signal(
            data([1.2] * 12, indep4=1)["smallcap"]["evaluation"])
        self.assertFalse(g["enough"])

    def test_a_full_record_that_is_negative_is_marked_failed(self):
        g = decision.gate_signal(
            data([-1.0] * 12, indep4=3)["smallcap"]["evaluation"])
        self.assertTrue(g["failed"])


class GateCostTests(unittest.TestCase):

    def test_it_takes_the_best_book_not_the_control(self):
        g = decision.gate_costs([book("A", -1.0), book("E", 0.5)])
        self.assertEqual(g["best"], "E")
        self.assertTrue(g["passed"])

    def test_every_book_behind_the_index_is_a_failure(self):
        g = decision.gate_costs([book("A", -1.0), book("E", -0.2)])
        self.assertTrue(g["failed"])
        self.assertFalse(g["passed"])


class GateBreadthTests(unittest.TestCase):

    def test_an_edge_that_rests_on_three_names_does_not_count(self):
        g = decision.gate_breadth([book("E", 4.0, bench=1.0)],
                                  {"E": attrib(ex_top=0.4)})
        self.assertTrue(g["failed"], "0.4% against a 1.0% index is behind it")

    def test_an_edge_that_survives_losing_its_best_names_counts(self):
        g = decision.gate_breadth([book("E", 4.0, bench=1.0)],
                                  {"E": attrib(ex_top=3.0)})
        self.assertTrue(g["passed"])


class VerdictTests(unittest.TestCase):

    def _verdict(self, **kw):
        return decision.assess(data(**kw))["verdict"]

    def test_too_early_never_reads_as_abandon(self):
        self.assertEqual(
            self._verdict(readings=[-9.0] * 4, books=[book("A", -8.0)]),
            "insufficient")

    def test_a_full_negative_record_reaches_the_stop_verdict(self):
        v = self._verdict(readings=[-1.0] * 12, indep4=3,
                          books=[book("A", -2.0)],
                          detail={"A": attrib(ex_top=-3.0)})
        self.assertEqual(v, "abandon")

    def test_all_gates_passing_reads_as_continue_not_as_success(self):
        v = self._verdict(readings=[1.2] * 12, indep4=3,
                          books=[book("E", 2.0, bench=1.0)],
                          detail={"E": attrib(ex_top=2.5)})
        self.assertEqual(v, "continue")

    def test_a_mixed_result_authorises_nothing(self):
        v = self._verdict(readings=[1.2] * 12, indep4=3,
                          books=[book("E", 2.0, bench=1.0)],
                          detail={"E": attrib(ex_top=0.2)})   # breadth fails
        self.assertEqual(v, "inconclusive")

    def test_every_verdict_is_reachable(self):
        # a rule whose failure branch cannot fire is not a rule
        self.assertEqual(set(decision.VERDICTS), {
            "insufficient", "abandon", "inconclusive", "continue"})

    def test_real_money_is_only_ever_mentioned_to_rule_it_out(self):
        import re
        for key, (title, body) in decision.VERDICTS.items():
            for m in re.finditer(r"real money", (title + " " + body).lower()):
                before = (title + " " + body).lower()[max(0, m.start() - 40):m.start()]
                self.assertRegex(before, r"\bno\b|\bnot\b",
                                 f"{key} mentions real money without ruling it out")

    def test_a_perfectly_consistent_record_is_not_treated_as_unmeasurable(self):
        # zero spread is the STRONGEST evidence, not missing evidence
        g = decision.gate_signal(
            data([1.2] * 12, indep4=3)["smallcap"]["evaluation"])
        self.assertTrue(g["passed"])
        g = decision.gate_signal(
            data([-1.2] * 12, indep4=3)["smallcap"]["evaluation"])
        self.assertTrue(g["failed"])

    def test_the_best_outcome_still_refuses_real_money_and_keeps_the_freeze(self):
        body = decision.VERDICTS["continue"][1].lower()
        self.assertIn("does not authorise real money", body)
        self.assertIn("does not lift the freeze", body)

    def test_the_stop_verdict_forbids_tuning_until_it_passes(self):
        body = decision.VERDICTS["abandon"][1].lower()
        self.assertIn("not to adjust the rules", body)


if __name__ == "__main__":
    unittest.main()
